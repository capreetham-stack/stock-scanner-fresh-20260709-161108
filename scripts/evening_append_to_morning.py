#!/usr/bin/env python3
"""
Create the daily EOD follow-up worksheet.

This script reads the latest intraday recommendation tab for the day
(PRE_MARKET_YYYY-MM-DD, legacy PRE915_YYYY-MM-DD, or HOURLY_YYYY-MM-DD),
then writes a dedicated EOD_NEXTDAY_YYYY-MM-DD worksheet with the follow-up
movement summary.
"""

from __future__ import annotations

import os
import re
import datetime as dt
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.nse_fetcher import NSEFetcher
from main import load_env_file, get_env_value_from_file, auto_detect_gsheet_creds


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SECTION_MARKER = "EOD FOLLOW-UP (Morning recommendations vs current price)"


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


def fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


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


def ensure_worksheet(spreadsheet, title: str, rows: int = 200, cols: int = 20) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def col_letter(index_1_based: int) -> str:
    letters = ""
    n = index_1_based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def make_row(values: list[Any], width: int) -> list[Any]:
    row = list(values)
    if len(row) < width:
        row += [""] * (width - len(row))
    return row[:width]


def find_marker_row(ws: gspread.Worksheet, marker: str) -> int | None:
    col_a = ws.col_values(1)
    for idx, value in enumerate(col_a, 1):
        if str(value).strip() == marker:
            return idx
    return None


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
    source_prefixes = [f"PRE_MARKET_{today}", f"PRE915_{today}", f"HOURLY_{today}"]
    eod_title = f"EOD_NEXTDAY_{today}"

    sh = open_sheet(sheet_target, creds_path)
    try:
        source_ws = pick_latest_tab(sh, source_prefixes)
        source_rows = source_ws.get_all_records()
        source_title = source_ws.title
    except RuntimeError:
        source_ws = None
        source_rows = []
        source_title = "(no source tab found)"

    eod_ws = ensure_worksheet(sh, eod_title)
    eod_ws.clear()

    fetcher = NSEFetcher()
    movement_rows: list[list[Any]] = []

    for idx, row in enumerate(source_rows, 1):
        symbol = str(row_value(row, "Symbol", "symbol", default="")).strip()
        if not symbol or symbol.upper() == "NONE":
            continue

        score = to_float(row_value(row, "Score", "score", default=0.0), 0.0)
        entry = to_float(
            row_value(row, "Entry", "entry", "Current Price", "current_price", default=0.0),
            0.0,
        )

        last_price = entry
        quote = fetcher.get_quote(symbol)
        if isinstance(quote, dict):
            price_info = quote.get("priceInfo", {}) if isinstance(quote.get("priceInfo", {}), dict) else {}
            fetched_price = to_float(price_info.get("lastPrice"), entry)
            if fetched_price > 0:
                last_price = fetched_price

        move_abs = last_price - entry if entry > 0 else 0.0
        move_pct = (move_abs / entry * 100.0) if entry > 0 else 0.0
        status = "UP" if move_abs > 0 else ("DOWN" if move_abs < 0 else "FLAT")

        movement_rows.append([
            idx,
            symbol,
            round(score, 2),
            round(entry, 2),
            round(last_price, 2),
            round(move_abs, 2),
            fmt_pct(move_pct),
            status,
        ])

    headers = [
        "Rank",
        "Symbol",
        "Morning Score",
        "Morning Entry",
        "Evening Price",
        "Move (Rs)",
        "Move %",
        "Status",
    ]
    width = len(headers)

    generated_at = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    values = [
        make_row([SECTION_MARKER], width),
        make_row([f"Source tab: {source_title}"], width),
        make_row([f"Generated: {generated_at}"], width),
        make_row([], width),
        make_row(headers, width),
    ]

    if movement_rows:
        values.extend(make_row(r, width) for r in movement_rows)
    else:
        values.append(make_row(["No source rows found for follow-up"], width))

    start_row = 1

    end_row = start_row + len(values) - 1
    end_col = col_letter(width)
    eod_ws.update(range_name=f"A{start_row}:{end_col}{end_row}", values=values, value_input_option="RAW")
    eod_ws.freeze(rows=1)

    print(
        {
            "source_tab": source_title,
            "eod_tab": eod_ws.title,
            "section_start_row": start_row,
            "rows_written": len(movement_rows),
        }
    )


if __name__ == "__main__":
    main()
