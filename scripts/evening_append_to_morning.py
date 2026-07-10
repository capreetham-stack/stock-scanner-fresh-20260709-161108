#!/usr/bin/env python3
"""
Append EOD movement summary below the daily recommendations table.

This script does not alter recommendation rows/columns.
It appends (or refreshes) a section below the existing data in today's
PRE_MARKET_YYYY-MM-DD worksheet (legacy PRE915_YYYY-MM-DD is also supported).
If a pre-market tab is unavailable, it falls back to HOURLY_YYYY-MM-DD.
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
    morning_prefixes = [f"PRE_MARKET_{today}", f"PRE915_{today}", f"HOURLY_{today}"]

    sh = open_sheet(sheet_target, creds_path)
    ws = pick_latest_tab(sh, morning_prefixes)
    morning_rows = ws.get_all_records()
    if not morning_rows:
        raise RuntimeError(f"Morning worksheet {ws.title} has no data")

    fetcher = NSEFetcher()
    movement_rows: list[list[Any]] = []

    for idx, row in enumerate(morning_rows, 1):
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

    if not movement_rows:
        print("No valid morning recommendations to append follow-up for.")
        return

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
        make_row([f"Generated: {generated_at}"], width),
        make_row([], width),
        make_row(headers, width),
    ]
    values.extend(make_row(r, width) for r in movement_rows)

    marker_row = find_marker_row(ws, SECTION_MARKER)
    if marker_row is not None:
        start_row = marker_row
        current_rows = ws.row_count
        end_col = col_letter(width)
        ws.batch_clear([f"A{start_row}:{end_col}{current_rows}"])
    else:
        start_row = len(morning_rows) + 3

    end_row = start_row + len(values) - 1
    end_col = col_letter(width)
    ws.update(range_name=f"A{start_row}:{end_col}{end_row}", values=values, value_input_option="RAW")

    print(
        {
            "morning_tab": ws.title,
            "section_start_row": start_row,
            "rows_written": len(movement_rows),
        }
    )


if __name__ == "__main__":
    main()
