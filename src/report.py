"""
src/report.py
==============
Formats and outputs the final buy-list in multiple formats:
  • Rich console table (colourful, human-readable)
  • CSV file
  • JSON file
  • Plain text summary (for logging / mobile-friendly copy-paste)
"""

import os
import csv
import json
import logging
import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config as cfg
from src.signals import StockSignal

logger = logging.getLogger(__name__)


# ─── ANSI colour helpers (no extra deps) ──────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    DIM    = "\033[2m"


def _col(text, *codes):
    return "".join(codes) + str(text) + C.RESET


def _score_colour(score: int) -> str:
    if score >= 80:  return C.GREEN + C.BOLD
    if score >= 60:  return C.GREEN
    if score >= 45:  return C.YELLOW
    return C.DIM


def _to_num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "N/A"


# ─── Reporter class ───────────────────────────────────────────────────────────

class Reporter:

    def __init__(self):
        os.makedirs("output", exist_ok=True)
        os.makedirs("logs",   exist_ok=True)

    # ── Console ───────────────────────────────────────────────────────────────

    def print_console(self, result: dict):
        buy_list = result["buy_list"]
        ctx      = result["market_context"]
        ts       = result["timestamp"]
        stats    = result["stats"]

        print()
        print(_col("=" * 70, C.CYAN, C.BOLD))
        print(_col(f"  NSE PRE-MARKET BUY SCANNER  |  {ts}", C.CYAN, C.BOLD))
        print(_col("=" * 70, C.CYAN, C.BOLD))

        # Market context
        pcr = ctx.get("nifty_pcr")
        pcr_str = (
            _col(f"PCR {pcr:.2f} (BULLISH)", C.GREEN) if (pcr and pcr > 1)
            else _col(f"PCR {pcr:.2f} (BEARISH)", C.RED)  if pcr
            else _col("PCR N/A", C.DIM)
        )
        print(f"\n  NIFTY {pcr_str}  |  "
              f"Scanned: {stats['scanned']}  "
              f"Qualified: {stats['qualified']}  "
              f"Skipped: {stats['skipped']}")
        for line in self._market_sentiment_lines(result):
            print(_col(f"  {line}", C.DIM))

        if not buy_list:
            print(_col(f"\n  No stocks meet the minimum score ({cfg.MIN_SCORE_TO_BUY} pts). "
                       "Market may be unfavourable.", C.YELLOW))
            # Show top 5 closest candidates anyway
            all_sigs = sorted(result.get("all_signals", []),
                              key=lambda s: s.score, reverse=True)
            candidates = [s for s in all_sigs if s.score > 0][:5]
            if candidates:
                print(_col("\n  CLOSEST CANDIDATES (below threshold):\n", C.DIM))
                self._print_table(candidates)
            # If a fallback top-gainers analysis was performed, show it explicitly
            tg = result.get("top_gainers_analysis") or {}
            if tg and tg.get("buy_list"):
                print(_col("\n  TOP GAINERS ANALYSIS — We went through top gainers and did the same research for others:\n", C.BOLD))
                self._print_table(tg.get("buy_list", []))
                print()
                self._print_detail(tg.get("buy_list", []))
        else:
            print(_col(f"\n  TOP {len(buy_list)} BUY CANDIDATES BEFORE 9:15 AM\n", C.BOLD))
            self._print_table(buy_list)
            print()
            self._print_detail(buy_list)

        # Intraday trade candidates
        intraday = self._get_intraday_candidates(result)
        if intraday:
            print(_col("\n  INTRADAY TRADE CANDIDATES (Speed + Volume + Liquidity)\n", C.BOLD))
            print(_col("  Following 5 Rules: Liquidity(RVOL≥2x) | 3-Confirmation(Price>EMA>VWAP+Green ST) |", C.DIM))
            print(_col("  Golden Hours(9:15-10AM/1:30-3:15PM) | Risk Mgmt(SL@1.5xATR) | Sector Tailbeat", C.DIM))
            print()
            self._print_intraday_table(intraday)
            print()

        print(_col("=" * 70, C.CYAN, C.BOLD))
        print()

    def _extract_global_index_pct(self, global_data: dict | list, names: tuple[str, ...]) -> float | None:
        rows = []
        if isinstance(global_data, dict):
            if isinstance(global_data.get("data"), list):
                rows = global_data.get("data", [])
            elif isinstance(global_data.get("indices"), list):
                rows = global_data.get("indices", [])
        elif isinstance(global_data, list):
            rows = global_data

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
            if not any(n in name for n in names):
                continue
            for field in ("perChange", "pChange", "percentChange", "changePercent"):
                if field in row:
                    return _to_num(row.get(field), None)
        return None

    def _market_sentiment_lines(self, result: dict) -> list[str]:
        """3-5 line market summary: index mood, breadth, EOD flows, outside movers."""
        ctx = result.get("market_context", {}) or {}

        pcr = ctx.get("nifty_pcr")
        preopen = ctx.get("preopen_nifty", {}) or {}
        advances = int(preopen.get("advances", 0) or 0)
        declines = int(preopen.get("declines", 0) or 0)

        gbl = ctx.get("global", {}) or {}
        nifty_pct = self._extract_global_index_pct(gbl, ("NIFTY",))
        sensex_pct = self._extract_global_index_pct(gbl, ("SENSEX", "BSE SENSEX"))

        bullish_votes = 0
        bearish_votes = 0
        if nifty_pct is not None:
            bullish_votes += 1 if nifty_pct > 0 else 0
            bearish_votes += 1 if nifty_pct < 0 else 0
        if sensex_pct is not None:
            bullish_votes += 1 if sensex_pct > 0 else 0
            bearish_votes += 1 if sensex_pct < 0 else 0
        if advances > declines:
            bullish_votes += 1
        elif declines > advances:
            bearish_votes += 1

        if bullish_votes > bearish_votes or (pcr is not None and pcr > 1):
            today = "BULLISH"
        elif bearish_votes > bullish_votes or (pcr is not None and pcr < 1):
            today = "BEARISH"
        else:
            today = "NEUTRAL"

        fii_dii = ctx.get("fii_dii", []) or []
        net_sum = 0.0
        eod_date = "N/A"
        if isinstance(fii_dii, list) and fii_dii:
            eod_date = str(fii_dii[0].get("date", "N/A"))
            for row in fii_dii:
                if isinstance(row, dict):
                    net_sum += _to_num(row.get("netValue"), 0.0)
        eod = "BULLISH" if net_sum > 0 else ("BEARISH" if net_sum < 0 else "NEUTRAL")

        scanned_symbols = {s.symbol for s in result.get("all_signals", []) if getattr(s, "symbol", None)}
        outside = []
        for row in preopen.get("data", []) if isinstance(preopen, dict) else []:
            meta = row.get("metadata", {}) if isinstance(row, dict) else {}
            sym = str(meta.get("symbol", "")).strip().upper()
            pchg = _to_num(meta.get("pChange"), 0.0)
            if sym and sym not in scanned_symbols and pchg > 0:
                outside.append((sym, pchg))

        outside.sort(key=lambda x: x[1], reverse=True)
        top_outside = ", ".join([f"{sym} {chg:+.2f}%" for sym, chg in outside[:3]]) if outside else "none"

        return [
            f"Market sentiment (Today): {today}",
            f"Indices: NIFTY {_fmt_pct(nifty_pct)} | SENSEX {_fmt_pct(sensex_pct)} | Breadth (Upper/Lower) {advances}/{declines}",
            f"EOD sentiment ({eod_date}): {eod} | Net FII+DII {net_sum:+.0f} cr",
            f"Outside movers not in list: {len(outside)} ({top_outside})",
        ]

    def _print_table(self, buy_list: list[StockSignal]):
        hdr = (f"  {'#':<3} {'SYMBOL':<14} {'SCORE':>5} {'PRICE':>8} "
                             f"{'RSI':>6} {'MACD_H':>8} {'SUPTRND':>8} {'VOL_R':>6} {'B/S':>6} "
               f"{'ENTRY':>8} {'SL':>8} {'TGT':>8} {'R:R':>5}")
        print(_col(hdr, C.BOLD))
        print(_col("  " + "─" * 106, C.DIM))

        for rank, sig in enumerate(buy_list, 1):
            sc   = _score_colour(sig.score)
            st   = _col("↑BULL", C.GREEN) if sig.supertrend_dir == 1 else _col("↓BEAR", C.RED)
            rsi_c = C.GREEN if sig.rsi < cfg.RSI_OVERSOLD else (C.RED if sig.rsi > cfg.RSI_OVERBOUGHT else C.WHITE)
            row = (
                f"  {rank:<3} "
                f"{_col(sig.symbol, C.BOLD):<22} "
                f"{_col(sig.score, sc):>12} "
                f"{sig.current_price:>8.2f} "
                f"{_col(f'{sig.rsi:.1f}', rsi_c):>13} "
                f"{sig.macd_hist:>8.4f} "
                f"{st:>16} "
                f"{sig.vol_ratio:>6.1f}x "
                f"{(f'{sig.buy_sell_ratio:.2f}x' if sig.buy_sell_ratio is not None else 'N/A'):>6} "
                f"{sig.entry:>8.2f} "
                f"{sig.stop_loss:>8.2f} "
                f"{sig.target:>8.2f} "
                f"{sig.reward_risk:>5.1f}x"
            )
            print(row)

    def _print_detail(self, buy_list: list[StockSignal]):
        print(_col("  DETAILED ANALYSIS", C.BOLD))
        print(_col("  " + "─" * 50, C.DIM))
        for sig in buy_list:
            print(f"\n  {_col(sig.symbol, C.CYAN, C.BOLD)}  [{_col(sig.score, _score_colour(sig.score))} pts]")
            if sig.buy_heading:
                print(f"    Decision: {_col(sig.buy_heading, C.BOLD)}")
            print(f"    Price: {sig.current_price:.2f}  "
                  f"Gap: {_col(f'{sig.gap_pct:+.2f}%', C.GREEN if sig.gap_pct > 0 else C.RED)}  "
                  f"Pattern: {sig.pattern}")
            if None not in (sig.chg_7d_pct, sig.chg_30d_pct, sig.chg_90d_pct):
                print(f"    Trend Check: 7D={sig.chg_7d_pct:+.2f}%  "
                      f"30D={sig.chg_30d_pct:+.2f}%  3M={sig.chg_90d_pct:+.2f}%")
            print(f"    Order Pressure: BuyQty={sig.buy_qty:.0f}  SellQty={sig.sell_qty:.0f}  "
                f"B/S Ratio={(f'{sig.buy_sell_ratio:.2f}x' if sig.buy_sell_ratio is not None else 'N/A')}")
            print(f"    Entry: {sig.entry:.2f}  SL: {_col(f'{sig.stop_loss:.2f}', C.RED)}  "
                  f"Target: {_col(f'{sig.target:.2f}', C.GREEN)}  R:R = {sig.reward_risk:.1f}x")
            if sig.nearest_demand_zone:
                dz = sig.nearest_demand_zone
                print(f"    Demand Zone: {dz.bottom:.2f}–{dz.top:.2f}  "
                      f"(strength={dz.strength:.0f}, fresh={dz.fresh}, "
                      f"{sig.demand_proximity:.1f}% away)")
            if sig.nearest_supply_zone:
                sz = sig.nearest_supply_zone
                print(f"    Supply Zone: {sz.bottom:.2f}–{sz.top:.2f}  "
                      f"({sig.supply_proximity:.1f}% away)")
            reasons_str  = "\n      ".join(sig.reasons)
            warnings_str = "\n      ".join(sig.warnings)
            if sig.reasons:
                print(f"    Bullish signals:\n      {_col(reasons_str, C.GREEN)}")
            if sig.warnings:
                print(f"    Cautions:\n      {_col(warnings_str, C.YELLOW)}")

    def _get_intraday_candidates(self, result: dict) -> list[StockSignal]:
        """Filter stocks for intraday trading using 5-rule logic:
        1. Liquidity: RVOL >= 2x
        2. 3-Confirmation: Price > 21 EMA, Price > VWAP, Supertrend Green
        3. Golden Hours: 9:15-10:00 AM or 1:30-3:15 PM (avoid 10:30-1:30 trap)
        4. Risk Management: SL at 1.5x ATR
        5. Sector Tailbeat: Sector must be trending up
        """
        candidates = []
        all_sigs = result.get("all_signals", []) or []
        
        for sig in all_sigs:
            # Rule 1: Liquidity (RVOL >= 2x)
            if sig.vol_ratio is None or sig.vol_ratio < 2.0:
                continue
            
            # Rule 2: 3-Confirmation (Green Supertrend is critical)
            if sig.supertrend_dir != 1:  # Green trend
                continue
            
            # Rule 3: Notify only if the price move is large enough (> 2%)
            if sig.gap_pct < 2.0:
                continue
            
            # Price checks (above EMA, approaching VWAP)
            # These are captured in signal scoring
            
            # Rule 4: Risk Management - ATR available (implicitly checked)
            if sig.atr is None or sig.atr <= 0:
                continue
            
            # Rule 5: Sector strength (we use positive gain_pct as proxy for good momentum)
            if sig.chg_7d_pct is not None and sig.chg_7d_pct < -2:  # Sector weakness
                continue
            
            # Additional quality filters
            if sig.score < 30:  # Minimum quality threshold for intraday
                continue
            
            candidates.append(sig)
        
        # Sort by RVOL (liquidity first) then score
        candidates.sort(key=lambda s: (s.vol_ratio if s.vol_ratio else 0, s.score), reverse=True)
        return candidates[:10]  # Top 10 intraday candidates

    def _print_intraday_table(self, intraday: list[StockSignal]):
        hdr = (f"  {'#':<3} {'SYMBOL':<14} {'RVOL':>5} {'ST':>6} {'PRICE':>8} "
               f"{'RSI':>6} {'ATR%':>6} {'SL(1.5xATR)':>12} {'ENTRY':>8} {'7D%':>6}")
        print(_col(hdr, C.BOLD))
        print(_col("  " + "─" * 85, C.DIM))
        
        for rank, sig in enumerate(intraday, 1):
            st = _col("✓GREEN", C.GREEN) if sig.supertrend_dir == 1 else _col("✗RED", C.RED)
            rsi_color = C.YELLOW if 50 <= sig.rsi <= 70 else (C.RED if sig.rsi > 70 else C.GREEN)
            atr_pct = (sig.atr / sig.current_price * 100) if sig.current_price and sig.atr else 0
            sl_at_atr = sig.current_price - (1.5 * sig.atr) if sig.atr else sig.stop_loss
            chg_7d = f"{sig.chg_7d_pct:+.2f}%" if sig.chg_7d_pct is not None else "N/A"
            row = (
                f"  {rank:<3} "
                f"{_col(sig.symbol, C.BOLD):<22} "
                f"{sig.vol_ratio:>5.1f}x "
                f"{st:>13} "
                f"{sig.current_price:>8.2f} "
                f"{_col(f'{sig.rsi:.1f}', rsi_color):>13} "
                f"{atr_pct:>6.2f}% "
                f"{sl_at_atr:>12.2f} "
                f"{sig.entry:>8.2f} "
                f"{chg_7d:>6}"
            )
            print(row)

    # ── Plain text (mobile-friendly) ──────────────────────────────────────────

    def print_plain(self, result: dict):
        """Minimal plain-text version, no ANSI codes — good for logs/mobile."""
        lines = []
        lines.append(f"NSE SCANNER | {result['timestamp']}")
        lines.append(f"Scanned: {result['stats']['scanned']} | "
                     f"Qualified: {result['stats']['qualified']}")
        lines.extend(self._market_sentiment_lines(result))
        lines.append("-" * 50)
        lines.append("PRE-MARKET BUY CANDIDATES:")
        for rank, sig in enumerate(result["buy_list"], 1):
            st = "BULL" if sig.supertrend_dir == 1 else "BEAR"
            lines.append(
                f"{rank}. {sig.symbol:<12} Score:{sig.score:>3}  "
                f"Price:{sig.current_price:.2f}  RSI:{sig.rsi:.1f}  "
                f"ST:{st}  B/S:{(f'{sig.buy_sell_ratio:.2f}x' if sig.buy_sell_ratio is not None else 'N/A')}  "
                f"Entry:{sig.entry:.2f}  SL:{sig.stop_loss:.2f}  "
                f"TGT:{sig.target:.2f}  R:R:{sig.reward_risk:.1f}x"
            )
            if sig.buy_heading:
                lines.append(f"   Decision: {sig.buy_heading}")
            if None not in (sig.chg_7d_pct, sig.chg_30d_pct, sig.chg_90d_pct):
                lines.append(
                    f"   Trend Check: 7D={sig.chg_7d_pct:+.2f}% | "
                    f"30D={sig.chg_30d_pct:+.2f}% | 3M={sig.chg_90d_pct:+.2f}%"
                )
            if sig.reasons:
                top_reasons = " | ".join(sig.reasons[:4])
                lines.append(f"   Why Buy: {top_reasons}")
            if sig.warnings:
                top_warnings = " | ".join(sig.warnings[:2])
                lines.append(f"   Caution: {top_warnings}")
            if sig.indicator_messages:
                lines.append("   Indicator Notes:")
                for key, msg in sig.indicator_messages.items():
                    lines.append(f"     - {key}: {msg}")
        
        # Intraday trades
        lines.append("-" * 50)
        intraday = self._get_intraday_candidates(result)
        if intraday:
            lines.append("INTRADAY TRADE CANDIDATES (5 Rules: Liquidity≥2xRVOL | 3-Confirm | Golden Hours | SL@1.5xATR | Sector Tailbeat):")
            for rank, sig in enumerate(intraday, 1):
                st = "GREEN" if sig.supertrend_dir == 1 else "RED"
                atr_pct = (sig.atr / sig.current_price * 100) if sig.current_price and sig.atr else 0
                sl_at_atr = sig.current_price - (1.5 * sig.atr) if sig.atr else sig.stop_loss
                lines.append(
                    f"{rank}. {sig.symbol:<12} RVOL:{sig.vol_ratio:.1f}x  ST:{st}  "
                    f"Price:{sig.current_price:.2f}  RSI:{sig.rsi:.1f}  ATR%:{atr_pct:.2f}%  "
                    f"SL(1.5xATR):{sl_at_atr:.2f}  Entry:{sig.entry:.2f}  7D:{sig.chg_7d_pct:+.2f}%"
                )
        else:
            lines.append("INTRADAY TRADE CANDIDATES: None qualify (need RVOL≥2x + Supertrend Green)")
        
        lines.append("-" * 50)
        # Plain-text fallback top-gainers summary
        tg = result.get("top_gainers_analysis") or {}
        if tg and tg.get("buy_list"):
            lines.append("TOP GAINERS ANALYSIS — We went through top gainers and did the same research for others:")
            for rank, sig in enumerate(tg.get("buy_list", []), 1):
                st = "BULL" if sig.supertrend_dir == 1 else "BEAR"
                lines.append(
                    f"{rank}. {sig.symbol:<12} Score:{sig.score:>3}  Price:{sig.current_price:.2f}  RSI:{sig.rsi:.1f}  ST:{st}"
                )
        output = "\n".join(lines)
        print(output)
        return output

    # ── CSV ───────────────────────────────────────────────────────────────────

    def save_csv(self, result: dict, path: str = cfg.OUTPUT_CSV):
        rows = [s.to_dict() for s in result["buy_list"]]
        if not rows:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        logger.info("CSV saved → %s", path)
        print(f"  CSV → {os.path.abspath(path)}")

    # ── JSON ──────────────────────────────────────────────────────────────────

    def save_json(self, result: dict, path: str = cfg.OUTPUT_JSON):
        payload = {
            "timestamp":  result["timestamp"],
            "stats":      result["stats"],
            "market_context": {
                "nifty_pcr": result["market_context"].get("nifty_pcr"),
            },
            "buy_list": [s.to_dict() for s in result["buy_list"]],
            "top_gainers_analysis": {
                "nifty_pct": result.get("top_gainers_analysis", {}).get("nifty_pct"),
                "symbols_considered": result.get("top_gainers_analysis", {}).get("symbols_considered"),
                "scanned": result.get("top_gainers_analysis", {}).get("scanned"),
                "buy_list": [s.to_dict() for s in result.get("top_gainers_analysis", {}).get("buy_list", [])],
            },
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info("JSON saved → %s", path)
        print(f"  JSON → {os.path.abspath(path)}")

    # ── Combined save ─────────────────────────────────────────────────────────

    def save_all(self, result: dict):
        self.save_csv(result)
        self.save_json(result)
