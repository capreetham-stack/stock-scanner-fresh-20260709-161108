#!/usr/bin/env python3
"""
Create end-of-day follow-up from morning scan tabs.

Reads today's PRE_MARKET_* or legacy PRE915_* tab from Google Sheets, fetches latest prices,
and writes a single combined worksheet:
- EOD_NEXTDAY_YYYY-MM-DD
"""

from __future__ import annotations

import os
import re
import json
import datetime as dt
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.nse_fetcher import NSEFetcher
from main import load_env_file, get_env_value_from_file, auto_detect_gsheet_creds


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def extract_sheet_key(url_or_key: str) -> str:
    if "/spreadsheets/d/" not in url_or_key:
        return url_or_key.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_key)
    if not m:
        raise ValueError("Invalid Google Sheet URL")
    return m.group(1)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).replace(",", "").strip()
        return float(text) if text else default
    except Exception:
        return default


def row_value(row: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return default


def fmt_pct(value: Any) -> str:
    try:
        if value in (None, ""):
            return "N/A"
        return f"{float(value):+.2f}%"
    except Exception:
        return str(value)


def open_sheet(sheet_url_or_key: str, creds_path: str):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(creds)
    if "/spreadsheets/d/" in sheet_url_or_key:
        return client.open_by_url(sheet_url_or_key)
    return client.open_by_key(extract_sheet_key(sheet_url_or_key))


def pick_latest_tab(spreadsheet, prefix: str | list[str]) -> gspread.Worksheet:
    prefixes = [prefix] if isinstance(prefix, str) else prefix
    tabs = [ws for ws in spreadsheet.worksheets() if any(ws.title.startswith(p) for p in prefixes)]
    if not tabs:
        raise RuntimeError(f"No worksheet found with prefix(es) {prefixes}")
    tabs.sort(key=lambda ws: ws.title)
    return tabs[-1]


def remove_worksheet_if_exists(spreadsheet, title: str) -> None:
    for ws in spreadsheet.worksheets():
        if ws.title == title:
            spreadsheet.del_worksheet(ws)
            return


def write_rows(spreadsheet, title: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    remove_worksheet_if_exists(spreadsheet, title)
    headers = list(rows[0].keys())
    values = [headers] + [[rows_i.get(h, "") for h in headers] for rows_i in rows]
    ws = spreadsheet.add_worksheet(title=title, rows=max(100, len(values) + 10), cols=max(20, len(headers) + 2))
    ws.update(range_name="A1", values=values, value_input_option="RAW")
    ws.freeze(rows=1)
    return ws.title


def classify_nextday(score: float, eod_change_pct: float) -> str:
    if score >= 60 and eod_change_pct >= -2.0:
        return "HIGH_CONVICTION"
    if score >= 45 and eod_change_pct >= -3.0:
        return "WATCHLIST"
    return "SKIP"


def main() -> None:
    load_env_file()
    sheet_target = (os.getenv("GOOGLE_SHEET_URL", "") or os.getenv("GOOGLE_SHEET_KEY", "")).strip()
    if not sheet_target:
        sheet_target = (get_env_value_from_file("GOOGLE_SHEET_URL") or get_env_value_from_file("GOOGLE_SHEET_KEY")).strip()

    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not creds_path:
        creds_path = get_env_value_from_file("GOOGLE_APPLICATION_CREDENTIALS").strip()

    if not creds_path:
        creds_path = auto_detect_gsheet_creds()

    if not sheet_target:
        raise RuntimeError("Missing GOOGLE_SHEET_URL or GOOGLE_SHEET_KEY")
    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError("Missing/invalid GOOGLE_APPLICATION_CREDENTIALS path")

    today = dt.datetime.now(IST).strftime("%Y-%m-%d")
    morning_prefixes = [f"PRE_MARKET_{today}", f"PRE915_{today}"]

    sh = open_sheet(sheet_target, creds_path)
    morning_ws = pick_latest_tab(sh, morning_prefixes)
    morning_rows = morning_ws.get_all_records()
    if not morning_rows:
        raise RuntimeError(f"Morning worksheet {morning_ws.title} has no data")

    fetcher = NSEFetcher()
    eod_rows: list[dict[str, Any]] = []

    for row in morning_rows:
        symbol = str(row_value(row, "Symbol", "symbol", default="")).strip()
        if not symbol or symbol.upper() == "NONE":
            continue
        decision = str(row_value(row, "Decision", "buy_heading", default="")).strip()
        why_buy = str(row_value(row, "Why Buy", "why_buy", default="")).strip()
        cautions = str(row_value(row, "Cautions", "cautions_summary", default="")).strip()
        trend_check = str(row_value(row, "Trend Check", default="")).strip()
        trade_plan = str(row_value(row, "Trade Plan", default="")).strip()
        score = to_float(row_value(row, "Score", "score", default=0.0), 0.0)
        entry = to_float(row_value(row, "Entry", "entry", "Current Price", "current_price", default=0.0), 0.0)

        last_price = entry
        quote = fetcher.get_quote(symbol) if symbol else {}
        if isinstance(quote, dict):
            price_info = quote.get("priceInfo", {}) if isinstance(quote.get("priceInfo", {}), dict) else {}
            last_price = to_float(price_info.get("lastPrice"), entry)
            if last_price <= 0:
                last_price = entry

        change_pct = ((last_price - entry) / entry * 100) if entry > 0 else 0.0
        rec = classify_nextday(score, change_pct)

        eod_rows.append({
            "Rank": 0,
            "Symbol": symbol,
            "Morning Decision": decision,
            "Why Buy": why_buy,
            "Cautions": cautions,
            "Morning Score": round(score, 2),
            "Morning Entry": round(entry, 2),
            "EOD Price": round(last_price, 2),
            "EOD Change %": fmt_pct(round(change_pct, 2)),
            "_eod_change_sort": round(change_pct, 2),
            "Next Day Action": rec,
            "Trend Check": trend_check,
            "Trade Plan": trade_plan,
        })

    eod_rows.sort(key=lambda r: (-r["Morning Score"], -r["_eod_change_sort"]))

    for rank, row in enumerate(eod_rows, 1):
        row["Rank"] = rank
        row.pop("_eod_change_sort", None)

    # Cleanup legacy split tabs from older script versions.
    remove_worksheet_if_exists(sh, f"EOD_{today}")
    remove_worksheet_if_exists(sh, f"NEXTDAY_{today}")

    combined_title = f"EOD_NEXTDAY_{today}"
    written_combined = write_rows(sh, combined_title, eod_rows)

    summary = {
        "morning_tab": morning_ws.title,
        "combined_tab": written_combined,
        "rows": len(eod_rows),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
