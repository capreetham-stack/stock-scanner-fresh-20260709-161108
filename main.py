#!/usr/bin/env python3
"""
main.py — NSE Pre-Market Stock Scanner
=======================================
Run this script before 9:15 AM to get a ranked list of stocks to buy.

Usage
─────
  python main.py                        # full scan, top 10
  python main.py --top 5                # top 5 only
  python main.py --symbol RELIANCE      # analyse one stock
  python main.py --plain                # no ANSI colours (pipe/log friendly)
  python main.py --no-save              # skip CSV / JSON output
  python main.py --watchlist NIFTY50    # only scan NIFTY 50 stocks
  python main.py --schedule             # scheduler mode: runs at 9:00 AM daily
    python main.py --sync-gsheet --gsheet-key <SHEET_KEY> --gsheet-creds <service_account.json>
"""

import os
import sys
import time
import logging
import argparse
import datetime
import glob
import schedule   # pip install schedule
from typing import Optional

# ── project root on path ─────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import config as cfg
from src.scanner import PreMarketScanner
from src.report  import Reporter
from src.nse_fetcher import NSEFetcher
from src.gsheet_sync import GoogleSheetSync


def load_env_file(path: Optional[str] = None) -> None:
    """Load KEY=VALUE pairs from a local .env file without extra dependencies."""
    path = path or os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Allow .env to populate missing or empty env vars.
                if key and not os.environ.get(key):
                    os.environ[key] = val
    except Exception as exc:
        logging.warning("Unable to load %s: %s", path, exc)


def get_env_value_from_file(key: str, path: Optional[str] = None) -> str:
    """Read one env value directly from .env as a fallback."""
    path = path or os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def auto_detect_gsheet_creds() -> str:
    """Best-effort fallback: detect a likely service-account JSON in project root."""
    candidates = []
    for p in glob.glob(os.path.join(PROJECT_ROOT, "*.json")):
        name = os.path.basename(p).lower()
        if any(tok in name for tok in ("service", "account", "google", "apex", "studious")):
            candidates.append(p)
    if len(candidates) == 1:
        return candidates[0]
    return ""


# ─── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO"):
    os.makedirs("logs", exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s – %(message)s"
    logging.basicConfig(
        level   = getattr(logging, level.upper(), logging.INFO),
        format  = fmt,
        handlers=[
            logging.FileHandler(cfg.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # quiet noisy third-party libraries
    for lib in ("yfinance", "urllib3", "requests", "peewee"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ─── Argument parsing ─────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="NSE Pre-Market Stock Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--top",       type=int, default=cfg.TOP_N_STOCKS,
                   help=f"Number of top picks (default: {cfg.TOP_N_STOCKS})")
    p.add_argument("--symbol",    type=str, default=None,
                   help="Analyse a single NSE symbol (e.g. RELIANCE)")
    p.add_argument("--plain",     action="store_true",
                   help="Plain text output (no ANSI colours)")
    p.add_argument("--no-save",   action="store_true",
                   help="Do not save CSV / JSON output")
    p.add_argument("--min-score", type=int, default=None,
                   help=f"Override minimum score threshold (default: {cfg.MIN_SCORE_TO_BUY})")
    p.add_argument("--watchlist", type=str, default="ALL",
                   help="Restrict watchlist (NIFTY50, FNO, NIFTY500, ALL, or comma-separated symbols)")
    p.add_argument("--workers",   type=int, default=16,
                   help="Parallel workers for scanning (default: 16)")
    p.add_argument("--sync-gsheet", action="store_true",
                   help="Sync results to Google Sheet by creating a new daily tab")
    p.add_argument("--gsheet-key", type=str,
                   default=os.getenv("GOOGLE_SHEET_KEY", ""),
                   help="Google Sheet key (preferred when you have only the key)")
    p.add_argument("--gsheet-url", type=str,
                   default=os.getenv("GOOGLE_SHEET_URL", ""),
                   help="Google Sheet URL or key for sync")
    p.add_argument("--gsheet-creds", type=str,
                   default=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
                   help="Path to Google service-account JSON credentials")
    p.add_argument("--schedule",  action="store_true",
                   help="Scheduler mode: auto-run at 9:00 AM every weekday")
    p.add_argument("--log-level", type=str, default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging verbosity")
    p.add_argument("--run-type", type=str, default="morning",
                   choices=["morning", "midday", "evening", "hourly"],
                   help="Run type constraint for scanner mode")
    return p.parse_args()


# ─── Watchlist selector ────────────────────────────────────────────────────────

def get_watchlist(choice: str) -> list[str]:
    if choice == "NIFTY50":
        return cfg.NIFTY50
    if choice == "FNO":
        return cfg.FNO_EXTRAS
    if choice == "NIFTY500":
        fetcher = NSEFetcher()
        symbols = fetcher.get_index_constituents("NIFTY 500")
        if symbols:
            return symbols
        logging.warning("NIFTY500 fetch failed; falling back to default watchlist")
        return cfg.WATCHLIST

    if choice == "ALL":
        return cfg.WATCHLIST
        
    # Support comma-separated symbols for fast local testing
    return [s.strip().upper() for s in choice.split(",") if s.strip()]


# ─── Single scan run ──────────────────────────────────────────────────────────

def run_scan(args) -> None:
    # ── Evening mode (EOD follow-up) ──────────────────────────────────────────
    if args.run_type == "evening":
        logging.info("Running EOD follow-up (Evening mode)")
        try:
            from scripts.evening_append_to_morning import main as run_eod_append
            run_eod_append()
        except Exception as exc:
            logging.exception("EOD follow-up failed: %s", exc)
        return

    reporter = Reporter()

    # ── Single symbol mode ────────────────────────────────────────────────────
    if args.symbol:
        sym    = args.symbol.upper().strip()
        scanner = PreMarketScanner(watchlist=[sym], max_workers=max(1, args.workers))
        result  = scanner.run(top_n=1)
        if result["buy_list"]:
            if args.plain:
                reporter.print_plain(result)
            else:
                reporter.print_console(result)
        else:
            print(f"\n⚠  {sym}: score below threshold "
                  f"({cfg.MIN_SCORE_TO_BUY} pts required).")
            # Still show all signals for this stock
            for sig in result["all_signals"]:
                print(f"\n   Score: {sig.score}")
                print(f"   Reasons : {' | '.join(sig.reasons)}")
                print(f"   Warnings: {' | '.join(sig.warnings)}")
        return

    # ── Full scan mode ────────────────────────────────────────────────────────
    if args.min_score is not None:
        cfg.MIN_SCORE_TO_BUY = args.min_score

    watchlist = get_watchlist(args.watchlist)
    scanner   = PreMarketScanner(watchlist=watchlist, max_workers=max(1, args.workers), run_type=args.run_type)
    logging.info("Selected watchlist '%s' with %d symbols", args.watchlist, len(watchlist))
    result    = scanner.run(top_n=args.top)

    if args.plain:
        reporter.print_plain(result)
    else:
        reporter.print_console(result)

    if not args.no_save:
        reporter.save_all(result)

    gsheet_key = (args.gsheet_key or os.getenv("GOOGLE_SHEET_KEY", "") or
                  get_env_value_from_file("GOOGLE_SHEET_KEY")).strip()
    gsheet_url = (args.gsheet_url or os.getenv("GOOGLE_SHEET_URL", "") or
                  get_env_value_from_file("GOOGLE_SHEET_URL")).strip()
    gsheet_creds = (args.gsheet_creds or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "") or
                    get_env_value_from_file("GOOGLE_APPLICATION_CREDENTIALS")).strip()

    if not gsheet_creds:
        gsheet_creds = auto_detect_gsheet_creds().strip()

    # Prefer URL when both are present; some users store non-sheet IDs in key field.
    sheet_target = (gsheet_url or gsheet_key or "").strip()
    should_sync_gsheet = bool(args.sync_gsheet or sheet_target)

    if should_sync_gsheet:
        if not sheet_target:
            logging.error("Google Sheet sync skipped: missing --gsheet-key/--gsheet-url")
            return
        if not gsheet_creds:
            logging.error("Google Sheet sync skipped: missing --gsheet-creds or GOOGLE_APPLICATION_CREDENTIALS")
        else:
            try:
                sync = GoogleSheetSync(sheet_target, gsheet_creds)
                ws_prefix = "HOURLY" if args.run_type == "hourly" else "PRE_MARKET"
                ws_title = sync.sync_daily(result, prefix=ws_prefix)
                if ws_title:
                    print(f"  GoogleSheet → {ws_title}")
            except Exception as exc:
                logging.exception("Google Sheet sync failed (%s): %s", type(exc).__name__, exc)


# ─── Scheduler ────────────────────────────────────────────────────────────────

def is_weekday() -> bool:
    return datetime.datetime.now().weekday() < 5   # Mon–Fri


def scheduled_run(args, run_type_override=None) -> None:
    if is_weekday():
        rtype = run_type_override or args.run_type
        logging.info("Scheduled %s scan triggered.", rtype.upper())
        
        # Temporarily apply the run type so the scanner and GSheet know which logic to use
        original_type = args.run_type
        args.run_type = rtype
        try:
            run_scan(args)
        finally:
            args.run_type = original_type
    else:
        logging.info("Weekend — skipping scan.")


def scheduled_eod(args) -> None:
    if is_weekday():
        logging.info("Scheduled EOD follow-up triggered.")
        try:
            from scripts.evening_append_to_morning import main as run_eod_append
            run_eod_append()
        except Exception as exc:
            logging.exception("EOD follow-up failed: %s", exc)
    else:
        logging.info("Weekend — skipping EOD follow-up.")


def start_scheduler(args) -> None:
    scan_time = cfg.PRE_MARKET_START
    
    # 1. Schedule the morning pre-market scan
    schedule.every().day.at(scan_time).do(scheduled_run, args=args, run_type_override="morning")
    
    # 2. Schedule the hourly scans specifically for market hours
    market_hours = ["10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    for h in market_hours:
        schedule.every().day.at(h).do(scheduled_run, args=args, run_type_override="hourly")
        
    # 3. Schedule the EOD follow-up after market close
    eod_time = "15:45"
    schedule.every().day.at(eod_time).do(scheduled_eod, args=args)
        
    logging.info("Scheduler started in FULLY AUTOMATIC mode. Press Ctrl+C to stop.")
    logging.info("  -> Pre-Market Scan : %s", scan_time)
    logging.info("  -> Hourly Scans    : %s", ", ".join(market_hours))
    logging.info("  -> EOD Follow-up   : %s", eod_time)
    
    # Execute once immediately to bootstrap tracking if explicitly requested
    if args.run_type == "hourly":
        logging.info("Bootstrapping an immediate hourly scan...")
        scheduled_run(args, run_type_override="hourly")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    load_env_file()
    args = parse_args()
    setup_logging(args.log_level)

    logging.info("NSE Stock Scanner — %s",
                 datetime.datetime.now().strftime("%d %b %Y"))

    if args.schedule:
        start_scheduler(args)
    else:
        run_scan(args)


if __name__ == "__main__":
    main()
