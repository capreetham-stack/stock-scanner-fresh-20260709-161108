"""
src/nse_fetcher.py
==================
Fetches data from NSE India portal (with a yfinance fallback).

Strategy
--------
1. Try NSE's unofficial JSON API with a proper browser Session.
2. If NSE blocks / rate-limits, fall back to yfinance for OHLCV history.
3. Cache raw responses to disk to avoid hammering the portal.
"""

from __future__ import annotations

import os
import json
import time
import logging
import datetime
import requests
import yfinance as yf
import pandas as pd
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config as cfg

logger = logging.getLogger(__name__)


# ─── NSE Session helper ───────────────────────────────────────────────────────

NSE_BASE      = "https://www.nseindia.com"
NSE_API_BASE  = "https://www.nseindia.com/api"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # Avoid brotli payloads here; gzip/deflate decode reliably via requests.
    "Accept-Encoding": "gzip, deflate",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


class NSESession:
    """Maintains a persistent requests.Session with NSE cookies."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._cookies_loaded = False

    def _warm_up(self):
        """Hit the homepage to obtain cookies before calling the API."""
        try:
            r = self._session.get(NSE_BASE, timeout=8)
            # NSE homepage may return 403 for scripted clients while API still works.
            # Do not treat homepage 403 as a hard API block.
            if r.status_code in (200, 401, 403):
                self._cookies_loaded = True
                time.sleep(0.2)
                return
            r.raise_for_status()
            self._cookies_loaded = True
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("NSE warm-up failed: %s", exc)
            # Keep going; API endpoints can still be reachable.

    # Track whether NSE is reachable at all to skip retries when blocked
    _nse_blocked: bool = False

    def get(self, endpoint: str, params: dict | None = None, retries: int = 2) -> dict | None:
        # If NSE is known to be blocked, bail immediately
        if self.__class__._nse_blocked:
            return None
        if not self._cookies_loaded:
            self._warm_up()
        url = f"{NSE_API_BASE}/{endpoint}"
        for attempt in range(1, retries + 1):
            try:
                r = self._session.get(url, params=params, timeout=8)
                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception:
                        logger.warning("NSE %s: invalid JSON response", endpoint)
                        if attempt == retries:
                            return None
                        time.sleep(0.5 * attempt)
                        continue
                elif r.status_code in (403, 429):
                        # Retry once; only mark blocked if we are still denied at final attempt.
                        logger.warning("NSE %s → HTTP %d", endpoint, r.status_code)
                        if attempt == retries:
                            self.__class__._nse_blocked = True
                            logger.warning("NSE API appears blocked; switching to yfinance fallback")
                            return None
                elif r.status_code == 401:
                    logger.info("Session expired – re-warming (attempt %d)", attempt)
                    self._warm_up()
                else:
                    logger.warning("NSE %s → HTTP %d", endpoint, r.status_code)
                    if attempt == retries:
                        return None
            except Exception as exc:
                logger.warning("NSE request error (attempt %d): %s", attempt, exc)
            time.sleep(0.8 * attempt)
        return None


# ─── Main Fetcher class ────────────────────────────────────────────────────────

class NSEFetcher:
    """High-level interface: historical OHLCV + market-breadth data."""

    def __init__(self):
        self._nse   = NSESession()
        self._cache = Path(cfg.DATA_CACHE_DIR)
        self._cache.mkdir(parents=True, exist_ok=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace(":", "-")
        return self._cache / f"{safe}.json"

    def _load_cache(self, key: str, max_age_secs: int = 3600) -> dict | None:
        p = self._cache_path(key)
        if p.exists():
            age = time.time() - p.stat().st_mtime
            if age < max_age_secs:
                try:
                    return json.loads(p.read_text())
                except Exception:
                    pass
        return None

    def _save_cache(self, key: str, data: dict):
        try:
            self._cache_path(key).write_text(json.dumps(data))
        except Exception as exc:
            logger.debug("Cache write failed: %s", exc)

    @staticmethod
    def _nse_to_yf_symbol(symbol: str) -> str:
        """Convert NSE symbol to Yahoo Finance ticker (append .NS)."""
        return f"{symbol}.NS"

    @staticmethod
    def _to_float(value, default: float = 0.0) -> float:
        """Robust numeric parser for API fields that may contain commas/strings."""
        try:
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return float(value)
            text = str(value).replace(",", "").strip()
            return float(text) if text else default
        except Exception:
            return default

    # ── Market status ────────────────────────────────────────────────────────

    def market_status(self) -> dict:
        """Returns market open/closed status from NSE."""
        data = self._nse.get("marketStatus")
        return data or {}

    # ── Quote (live) ─────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> dict:
        """Fetch live quote for a stock from NSE."""
        cache_key = f"quote_{symbol}"
        cached = self._load_cache(cache_key, max_age_secs=60)
        if cached:
            return cached

        data = self._nse.get("quote-equity", params={"symbol": symbol})
        if data:
            self._save_cache(cache_key, data)
            return data

        # fallback: yfinance
        logger.info("Falling back to yfinance for quote: %s", symbol)
        try:
            tk = yf.Ticker(self._nse_to_yf_symbol(symbol))
            info = tk.fast_info
            result = {
                "priceInfo": {
                    "lastPrice":   getattr(info, "last_price", None),
                    "open":        getattr(info, "open", None),
                    "previousClose": getattr(info, "previous_close", None),
                    "high":        getattr(info, "day_high", None),
                    "low":         getattr(info, "day_low", None),
                    "totalTradedVolume": getattr(info, "three_month_average_volume", None),
                },
                "source": "yfinance",
            }
            return result
        except Exception as exc:
            logger.warning("yfinance quote failed for %s: %s", symbol, exc)
            return {}

    def get_buy_sell_pressure(self, symbol: str) -> dict:
        """
        Returns order-book pressure from NSE quote-equity.
        Output:
            {
                "buy_qty": float,
                "sell_qty": float,
                "buy_sell_ratio": float | None,
                "net_qty": float,
            }
        """
        quote = self.get_quote(symbol)
        if not quote:
            return {
                "buy_qty": 0.0,
                "sell_qty": 0.0,
                "buy_sell_ratio": None,
                "net_qty": 0.0,
            }

        # NSE usually provides this in marketDeptOrderBook.
        mdob = quote.get("marketDeptOrderBook", {}) if isinstance(quote, dict) else {}
        buy_qty = self._to_float(mdob.get("totalBuyQuantity", 0.0), 0.0)
        sell_qty = self._to_float(mdob.get("totalSellQuantity", 0.0), 0.0)

        # Fallback for alternate payload shapes.
        if buy_qty == 0.0 and sell_qty == 0.0:
            buy_qty = self._to_float(quote.get("totalBuyQuantity", 0.0), 0.0)
            sell_qty = self._to_float(quote.get("totalSellQuantity", 0.0), 0.0)

        ratio = (buy_qty / sell_qty) if sell_qty > 0 else (None if buy_qty <= 0 else 9.99)
        net = buy_qty - sell_qty
        return {
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "buy_sell_ratio": round(ratio, 2) if ratio is not None else None,
            "net_qty": net,
        }

    # ── Historical OHLCV ─────────────────────────────────────────────────────

    def get_historical_ohlcv(
        self,
        symbol:   str,
        days:     int = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        Returns a DataFrame with columns: open, high, low, close, volume.
        interval = '1d' for daily, '5m' for 5-minute intraday.
        """
        days = days or cfg.HISTORICAL_DAYS
        cache_key = f"ohlcv_{symbol}_{interval}_{days}d"
        cached = self._load_cache(cache_key, max_age_secs=300 if "m" in interval else 3600)
        if cached:
            try:
                df = pd.DataFrame(cached)
                df.index = pd.to_datetime(df.index)
                return df
            except Exception:
                pass

        df = self._fetch_nse_historical(symbol, days, interval)
        if df is None or df.empty:
            df = self._fetch_yf_historical(symbol, days, interval)

        if df is not None and not df.empty:
            self._save_cache(cache_key, df.to_dict())
        return df if df is not None else pd.DataFrame()

    def _fetch_nse_historical(self, symbol: str, days: int, interval: str) -> pd.DataFrame | None:
        """Try NSE chart-data3 endpoint (daily only)."""
        if "m" in interval:   # NSE API doesn't serve intraday easily
            return None
        end_date   = datetime.date.today()
        start_date = end_date - datetime.timedelta(days=days + 30)
        params = {
            "symbol":   symbol,
            "series":   "EQ",
            "from":     start_date.strftime("%d-%m-%Y"),
            "to":       end_date.strftime("%d-%m-%Y"),
        }
        data = self._nse.get("historical/cm/equity", params=params)
        if not data or "data" not in data:
            return None
        try:
            rows = data["data"]
            df = pd.DataFrame(rows)
            df.rename(columns={
                "CH_TIMESTAMP":          "date",
                "CH_OPENING_PRICE":      "open",
                "CH_TRADE_HIGH_PRICE":   "high",
                "CH_TRADE_LOW_PRICE":    "low",
                "CH_CLOSING_PRICE":      "close",
                "CH_TOT_TRADED_QTY":     "volume",
                "CH_LAST_TRADED_PRICE":  "ltp",
                "CH_PREVIOUS_CLS_PRICE": "prev_close",
                "CH_52WEEK_HIGH_PRICE":  "52w_high",
                "CH_52WEEK_LOW_PRICE":   "52w_low",
            }, inplace=True)
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)
            df = df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
            return df.tail(days)
        except Exception as exc:
            logger.warning("NSE historical parse error for %s: %s", symbol, exc)
            return None

    def _fetch_yf_historical(self, symbol: str, days: int, interval: str) -> pd.DataFrame | None:
        """Fallback to yfinance."""
        yf_symbol = self._nse_to_yf_symbol(symbol)
        period_map = {
            "1d":  f"{days}d",
            "5m":  "5d",
            "15m": "5d",
            "1h":  "30d",
        }
        period = period_map.get(interval, f"{days}d")
        try:
            tk = yf.Ticker(yf_symbol)
            df = tk.history(period=period, interval=interval, auto_adjust=True)
            if df is None or df.empty:
                return None
            # Flatten multi-level columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower()
                              for c in df.columns]
            else:
                df.columns = [str(c).lower() for c in df.columns]
            # Rename yfinance column names to our standard names
            df.rename(columns={
                "stock splits": "stock_splits",
                "dividends":    "dividends",
            }, inplace=True)
            available = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
            df = df[available].dropna()
            return df
        except Exception as exc:
            logger.warning("yfinance OHLCV failed for %s: %s", symbol, exc)
            return None

    # ── Options chain ─────────────────────────────────────────────────────────

    def get_option_chain(self, symbol: str) -> dict:
        """Fetch options chain for a symbol (equity or index)."""
        cache_key = f"optchain_{symbol}"
        cached = self._load_cache(cache_key, max_age_secs=120)
        if cached:
            return cached

        endpoint = "option-chain-indices" if symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY") \
                   else "option-chain-equities"
        data = self._nse.get(endpoint, params={"symbol": symbol})
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    def get_pcr(self, symbol: str) -> float | None:
        """Put-Call Ratio from options chain. > 1 = bullish sentiment."""
        chain = self.get_option_chain(symbol)
        if not chain:
            return None
        try:
            records = chain["records"]["data"]
            total_ce_oi = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
            total_pe_oi = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
            return round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else None
        except Exception:
            return None

    # ── Delivery / FII-DII data ───────────────────────────────────────────────

    def get_delivery_data(self, symbol: str) -> dict:
        """Fetch delivery percentage data for equity."""
        cache_key = f"delivery_{symbol}"
        cached = self._load_cache(cache_key, max_age_secs=3600)
        if cached:
            return cached
        data = self._nse.get("deliveryTrade", params={"symbol": symbol, "series": "EQ"})
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    def get_fii_dii(self) -> dict:
        """Fetch latest FII/DII activity."""
        cache_key = "fii_dii"
        cached = self._load_cache(cache_key, max_age_secs=3600)
        if cached:
            return cached
        data = self._nse.get("fiidiiTradeReact")
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    # ── Index movers ──────────────────────────────────────────────────────────

    def get_gainers_losers(self, index: str = "NIFTY 50") -> dict:
        """Top gainers and losers in a given index."""
        cache_key = f"gainers_losers_{index.replace(' ', '_')}"
        cached = self._load_cache(cache_key, max_age_secs=120)
        if cached:
            return cached
        data = self._nse.get("live-analysis-variations", params={"index": index})
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    def get_most_active(self, by: str = "volume") -> dict:
        """Most active stocks by volume/value."""
        cache_key  = f"most_active_{by}"
        cached = self._load_cache(cache_key, max_age_secs=120)
        if cached:
            return cached
        endpoint = "live-analysis-stocksVolume" if by == "volume" else "live-analysis-stocksValue"
        data = self._nse.get(endpoint)
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    def get_52week_high_low(self) -> dict:
        """Stocks hitting 52-week high or low."""
        cache_key = "52wk_hl"
        cached = self._load_cache(cache_key, max_age_secs=300)
        if cached:
            return cached
        data = self._nse.get("live-analysis-52Week")
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    # ── Pre-market / SGX snapshots ────────────────────────────────────────────

    def get_premarket_data(self, key: str = "NIFTY") -> dict:
        """Pre-open session data for NIFTY / BANKNIFTY."""
        cache_key = f"premarket_{key}"
        cached = self._load_cache(cache_key, max_age_secs=60)
        if cached:
            return cached
        data = self._nse.get("market-data-pre-open", params={"key": key})
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    def get_global_snapshot(self) -> dict:
        """SGX Nifty / global indices snapshot."""
        cache_key = "global_snapshot"
        cached = self._load_cache(cache_key, max_age_secs=120)
        if cached:
            return cached
        data = self._nse.get("globalIndices")
        if data:
            self._save_cache(cache_key, data)
        return data or {}

    def get_all_indices(self) -> dict:
        """Fetch performance of all NSE indices."""
        cache_key = "all_indices"
        cached = self._load_cache(cache_key, max_age_secs=120)
        if cached:
            return cached
        data = self._nse.get("allIndices")
        res = {}
        if data and isinstance(data, dict) and "data" in data:
            for row in data["data"]:
                sym = row.get("indexSymbol") or row.get("index")
                if sym:
                    res[sym.upper()] = {
                        "last": self._to_float(row.get("last")),
                        "pchg": self._to_float(row.get("percentChange") or row.get("perChange"))
                    }
            self._save_cache(cache_key, res)
        return res

    def get_index_constituents(self, index_name: str = "NIFTY 500") -> list[str]:
        """Fetch index constituents from NSE and return clean symbol list."""
        key = f"index_constituents_{index_name.replace(' ', '_')}"
        cached = self._load_cache(key, max_age_secs=1800)
        if cached and isinstance(cached, dict) and isinstance(cached.get("symbols"), list):
            return cached["symbols"]

        data = None
        # Dedicated direct path: often more reliable than shared session state.
        for _ in range(3):
            try:
                s = requests.Session()
                s.headers.update(_HEADERS)
                r = s.get(
                    f"{NSE_API_BASE}/equity-stockIndices",
                    params={"index": index_name},
                    timeout=12,
                )
                if r.status_code == 200:
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "json" in ctype or (r.text and r.text[:1] == "{"):
                        data = r.json()
                        break
            except Exception:
                continue

        # Last fallback via session helper.
        if not data:
            data = self._nse.get("equity-stockIndices", params={"index": index_name})

        if not data:
            return []

        rows = data.get("data", []) if isinstance(data, dict) else []
        symbols: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol", "")).strip().upper()
            if not sym:
                continue
            # Skip index/header pseudo-rows that appear in payload.
            if sym.startswith("NIFTY ") or sym in {"NIFTY", "BANKNIFTY", "FINNIFTY"}:
                continue
            symbols.append(sym)

        symbols = list(dict.fromkeys(symbols))
        if symbols:
            self._save_cache(key, {"symbols": symbols})
        return symbols
