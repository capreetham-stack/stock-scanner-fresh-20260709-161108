"""
src/scanner.py
===============
Pre-market scanner — orchestrates data fetching + signal scoring for the
entire watchlist and returns ranked buy recommendations by 9:15 AM.
"""

from __future__ import annotations

import logging
import datetime
import concurrent.futures
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config as cfg
from src.nse_fetcher import NSEFetcher
from src.signals     import SignalEngine, StockSignal

logger = logging.getLogger(__name__)


class PreMarketScanner:
    """
    Runs the full scan pipeline:
    1.  Fetch daily + intraday OHLCV for every symbol in WATCHLIST.
    2.  Score each stock via SignalEngine.
    3.  Enrich with delivery %, PCR, FII/DII sentiment.
    4.  Return top-N buy recommendations.
    """

    def __init__(self, watchlist: list[str] | None = None, max_workers: int = 8, run_type: str = "morning"):
        self.watchlist   = watchlist or cfg.WATCHLIST
        self.max_workers = max_workers
        self.run_type    = run_type
        self._fetcher    = NSEFetcher()
        self._engine     = SignalEngine()

    # ── market-level context ─────────────────────────────────────────────────

    def _market_context(self) -> dict:
        """Gather NIFTY PCR, FII/DII, pre-open data, global cues."""
        ctx = {}
        try:
            preopen = self._fetcher.get_premarket_data("NIFTY")
            ctx["preopen_nifty"] = preopen
        except Exception as e:
            logger.debug("Pre-open NIFTY failed: %s", e)
            ctx["preopen_nifty"] = {}

        try:
            ctx["nifty_pcr"] = self._fetcher.get_pcr("NIFTY")
        except Exception as e:
            logger.debug("PCR failed: %s", e)
            ctx["nifty_pcr"] = None

        try:
            fii_dii = self._fetcher.get_fii_dii()
            ctx["fii_dii"] = fii_dii
        except Exception as e:
            logger.debug("FII/DII failed: %s", e)
            ctx["fii_dii"] = {}

        try:
            ctx["global"] = self._fetcher.get_global_snapshot()
        except Exception as e:
            logger.debug("Global snapshot failed: %s", e)
            ctx["global"] = {}

        try:
            ctx["52wk"] = self._fetcher.get_52week_high_low()
        except Exception as e:
            logger.debug("52wk data failed: %s", e)
            ctx["52wk"] = {}

        try:
            ctx["indices"] = self._fetcher.get_all_indices()
        except Exception as e:
            logger.debug("allIndices failed: %s", e)
            ctx["indices"] = {}

        try:
            ctx["sector_constituents"] = {
                "NIFTY IT": self._fetcher.get_index_constituents("NIFTY IT"),
                "NIFTY FMCG": self._fetcher.get_index_constituents("NIFTY FMCG"),
                "NIFTY PHARMA": self._fetcher.get_index_constituents("NIFTY PHARMA"),
                "NIFTY BANK": self._fetcher.get_index_constituents("NIFTY BANK"),
                "NIFTY METAL": self._fetcher.get_index_constituents("NIFTY METAL"),
            }
        except Exception as e:
            logger.debug("sector constituents failed: %s", e)
            ctx["sector_constituents"] = {}

        return ctx

    # ── per-symbol analysis ───────────────────────────────────────────────────

    def _analyse_symbol(self, symbol: str, pcr: float | None, ctx: dict = None) -> StockSignal | None:
        try:
            daily_df = self._fetcher.get_historical_ohlcv(symbol, days=cfg.HISTORICAL_DAYS)
            if daily_df.empty:
                logger.warning("%s: empty OHLCV, skipping", symbol)
                return None

            intraday_df = None
            if self.run_type == "hourly":
                # Fetch 2 days of 5m data for intraday/MTF analysis
                intraday_df = self._fetcher.get_historical_ohlcv(symbol, days=2, interval="5m")

            delivery_pct = 0.0
            try:
                del_data = self._fetcher.get_delivery_data(symbol)
                if del_data and "data" in del_data:
                    records = del_data["data"]
                    if records:
                        delivery_pct = float(records[0].get("deliveryToTradedQuantity", 0))
            except Exception:
                pass

            pressure = {
                "buy_qty": 0.0,
                "sell_qty": 0.0,
                "buy_sell_ratio": None,
            }
            try:
                pressure = self._fetcher.get_buy_sell_pressure(symbol)
            except Exception:
                pass

            sig = self._engine.score_stock(
                symbol        = symbol,
                daily_df      = daily_df,
                intraday_df   = intraday_df,
                delivery_pct  = delivery_pct,
                pcr           = pcr,
                buy_qty       = float(pressure.get("buy_qty", 0.0) or 0.0),
                sell_qty      = float(pressure.get("sell_qty", 0.0) or 0.0),
                buy_sell_ratio= pressure.get("buy_sell_ratio"),
                market_context= ctx,
            )
            return sig
        except Exception as exc:
            logger.error("Error analysing %s: %s", symbol, exc, exc_info=True)
            return None

    # ── main scan ─────────────────────────────────────────────────────────────

    def run(self, top_n: int = cfg.TOP_N_STOCKS) -> dict:
        """
        Full pre-market scan.

        Returns
        -------
        {
            "timestamp":     str,
            "market_context": dict,
            "all_signals":   [StockSignal, ...],
            "buy_list":      [StockSignal, ...],   # top N filtered
            "stats": {
                "scanned": int,
                "qualified": int,
                "skipped":  int,
            }
        }
        """
        start_t  = datetime.datetime.now()
        logger.info("=== PRE-MARKET SCAN STARTED at %s ===", start_t.strftime("%H:%M:%S"))

        # 1. Market-level context
        logger.info("Fetching market context …")
        ctx = self._market_context()
        pcr = ctx.get("nifty_pcr")
        if pcr:
            logger.info("NIFTY PCR: %.2f (%s)", pcr, "BULLISH" if pcr > 1 else "BEARISH")

        # Helper: try to extract a quick NIFTY percent move from global snapshot
        def _extract_nifty_pct(global_snapshot: dict | list) -> float | None:
            rows = []
            if isinstance(global_snapshot, dict):
                if isinstance(global_snapshot.get("data"), list):
                    rows = global_snapshot.get("data", [])
                elif isinstance(global_snapshot.get("indices"), list):
                    rows = global_snapshot.get("indices", [])
            elif isinstance(global_snapshot, list):
                rows = global_snapshot

            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = " ".join([
                    str(row.get("index", "")),
                    str(row.get("indexSymbol", "")),
                    str(row.get("key", "")),
                    str(row.get("name", "")),
                    str(row.get("symbol", "")),
                ]).upper()
                if "NIFTY" not in name:
                    continue
                for field in ("perChange", "pChange", "percentChange", "changePercent"):
                    if field in row:
                        try:
                            return float(row.get(field) or 0.0)
                        except Exception:
                            continue
            return None

        nifty_pct = _extract_nifty_pct(ctx.get("global", {}) or {})
        if nifty_pct is not None:
            logger.info("NIFTY pct (global snapshot): %.2f", nifty_pct)

        # 2. Parallel symbol analysis
        all_signals : list[StockSignal] = []
        skipped = 0
        logger.info("Scanning %d symbols …", len(self.watchlist))

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._analyse_symbol, sym, pcr, ctx): sym
                for sym in self.watchlist
            }
            for future in concurrent.futures.as_completed(futures):
                sym = futures[future]
                try:
                    sig = future.result()
                    if sig is not None:
                        all_signals.append(sig)
                    else:
                        skipped += 1
                except Exception as exc:
                    logger.error("Future error for %s: %s", sym, exc)
                    skipped += 1

        # 3. Rank + filter
        qualified = self._engine.rank(all_signals, market_context=ctx)
        buy_list = qualified[:top_n]

        elapsed = (datetime.datetime.now() - start_t).seconds
        logger.info("=== SCAN COMPLETE in %ds | %d scanned | %d qualifies | "
                    "%d in top list ===",
                    elapsed, len(all_signals), len(qualified),
                    len(buy_list))

        # If NIFTY is down, run a focused analysis on top gainers (NIFTY 500)
        top_gainers_result = {}
        try:
            # Only run top-gainers fallback during hourly scans
            if self.run_type == "hourly" and nifty_pct is not None and nifty_pct < 0:
                logger.info("NIFTY is down (%.2f%%). Running fallback scan on top gainers (hourly run).", nifty_pct)
            else:
                if nifty_pct is not None and nifty_pct < 0:
                    logger.info("NIFTY is down (%.2f%%) but skipping top-gainers fallback (not an hourly run).", nifty_pct)
                # proceed only if hourly and negative (handled above)
            if not (self.run_type == "hourly" and nifty_pct is not None and nifty_pct < 0):
                # Skip fallback
                top_gainers_result = {}
            else:
                logger.info("Proceeding with top-gainers fallback (hourly).")
                gainers_payload = self._fetcher.get_gainers_losers("NIFTY 500") or {}
                # gainers_payload already obtained above when proceeding
                # Try to extract symbols from common payload shapes, but only include gainers (positive pct)
                symbols = []
                def _row_pchg(r):
                    for field in ("pChange", "percentChange", "perChange", "changePercent"):
                        if isinstance(r, dict) and field in r:
                            try:
                                return float(r.get(field) or 0.0)
                            except Exception:
                                return 0.0
                    # metadata shapes
                    if isinstance(r, dict) and "metadata" in r and isinstance(r.get("metadata"), dict):
                        for k in ("pChange", "percentChange"):
                            try:
                                return float(r["metadata"].get(k) or 0.0)
                            except Exception:
                                continue
                    return 0.0

                if isinstance(gainers_payload, dict):
                    data = gainers_payload.get("data") or gainers_payload.get("gainers") or []
                    if isinstance(data, list) and data:
                        for row in data:
                            if isinstance(row, dict):
                                pchg = _row_pchg(row)
                                sym = (row.get("symbol") or row.get("symbolName") or row.get("scrip") or "").strip().upper()
                                if sym and pchg > 0:
                                    symbols.append(sym)
                # Fallback: try list-shaped payloads
                if not symbols and isinstance(gainers_payload, list):
                    for row in gainers_payload:
                        if isinstance(row, dict):
                            pchg = _row_pchg(row)
                            sym = (row.get("symbol") or row.get("scrip") or "").strip().upper()
                            if sym and pchg > 0:
                                symbols.append(sym)

                # If still empty, try preopen NIFTY data (some feeds include movers there)
                if not symbols:
                    preopen = ctx.get("preopen_nifty", {}) or {}
                    pdata = preopen.get("data") if isinstance(preopen, dict) else None
                    if isinstance(pdata, list):
                        tmp = []
                        for row in pdata:
                            meta = row.get("metadata") if isinstance(row, dict) else {}
                            sym = (meta.get("symbol") or "").strip().upper()
                            try:
                                pchg = float(meta.get("pChange") or 0.0)
                            except Exception:
                                pchg = 0.0
                            if sym and pchg > 0:
                                tmp.append((sym, pchg))
                        tmp.sort(key=lambda x: x[1], reverse=True)
                        symbols = [s for s, _ in tmp[:20]]

                # Deduplicate and limit
                symbols = list(dict.fromkeys(symbols))[:20]
                gainers_signals = []
                for sym in symbols:
                    try:
                        sig = self._analyse_symbol(sym, pcr, ctx)
                        if sig is not None:
                            gainers_signals.append(sig)
                    except Exception:
                        continue

                qualified_gainers = self._engine.rank(gainers_signals, market_context=ctx)
                top_gainers_buy = qualified_gainers[:top_n]
                top_gainers_result = {
                    "nifty_pct": nifty_pct,
                    "symbols_considered": len(symbols),
                    "scanned": len(gainers_signals),
                    "buy_list": top_gainers_buy,
                }
                # Attach to market context so reporters can read it too
                ctx["_top_gainers_analysis"] = top_gainers_result
        except Exception as exc:
            logger.exception("Top gainers fallback scan failed: %s", exc)

        return {
            "timestamp":      start_t.strftime("%Y-%m-%d %H:%M:%S"),
            "market_context": ctx,
            "all_signals":    all_signals,
            "buy_list":       buy_list,
            # Optional: fallback analysis on top gainers when NIFTY is down
            "top_gainers_analysis": ctx.get("_top_gainers_analysis", {}),
            "stats": {
                "scanned":   len(all_signals),
                "qualified": len(qualified),
                "skipped":   skipped,
            },
        }

    # ── quick single-stock analysis ───────────────────────────────────────────

    def analyse_one(self, symbol: str) -> StockSignal | None:
        """Convenience method to analyse a single stock."""
        pcr = None
        try:
            pcr = self._fetcher.get_pcr(symbol)
        except Exception:
            pass
        return self._analyse_symbol(symbol, pcr, self._market_context())
