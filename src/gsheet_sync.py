"""
Google Sheets sync utility.
Creates a new worksheet per day and writes scanner output rows.
"""

from __future__ import annotations

import os
import json
import re
import logging
import datetime as dt
from typing import Any

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)


class GoogleSheetSync:
    def __init__(self, spreadsheet_url: str, creds_path: str):
        self.spreadsheet_url = spreadsheet_url
        self.creds_path = creds_path

    @staticmethod
    def _extract_sheet_key(url_or_key: str) -> str:
        if "/spreadsheets/d/" not in url_or_key:
            return url_or_key.strip()
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_key)
        if not m:
            raise ValueError("Invalid Google Sheet URL")
        return m.group(1)

    def _client(self) -> gspread.Client:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        if not os.path.exists(self.creds_path):
            raise FileNotFoundError(f"Google credentials file not found: {self.creds_path}")
        creds = Credentials.from_service_account_file(self.creds_path, scopes=scopes)
        return gspread.authorize(creds)

    def _open_sheet(self, client: gspread.Client):
        try:
            if "/spreadsheets/d/" in self.spreadsheet_url:
                return client.open_by_url(self.spreadsheet_url)
            sheet_key = self._extract_sheet_key(self.spreadsheet_url)
            return client.open_by_key(sheet_key)
        except PermissionError as exc:
            raise RuntimeError(
                "Google Sheet permission denied. Share the sheet with the service-account "
                "email as Editor."
            ) from exc
        except SpreadsheetNotFound as exc:
            raise RuntimeError(
                "Spreadsheet not found or not shared with the service account. "
                "Share the sheet with the service-account email as Editor."
            ) from exc
        except APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", "unknown")
            body = getattr(getattr(exc, "response", None), "text", "")
            raise RuntimeError(f"Google Sheets API error {status}: {body}") from exc

    @staticmethod
    def _sheet_cell_value(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=True)
        return str(value)

    @staticmethod
    def _fmt_pct(value: Any) -> str:
        if value in (None, ""):
            return "N/A"
        try:
            number = float(value)
            return f"{number:+.2f}%"
        except Exception:
            return str(value)

    @classmethod
    def _format_morning_row(cls, rank: int, row: dict[str, Any]) -> dict[str, Any]:
        trend_summary = (
            f"7D {cls._fmt_pct(row.get('chg_7d_pct'))} | "
            f"30D {cls._fmt_pct(row.get('chg_30d_pct'))} | "
            f"3M {cls._fmt_pct(row.get('chg_90d_pct'))}"
        )
        technical_summary = (
            f"RSI {row.get('rsi', '')} | MACD {row.get('macd_hist', '')} | "
            f"ADX {row.get('adx', '')} | Vol {row.get('vol_ratio', '')}x | "
            f"ST {row.get('supertrend', '')}"
        )
        trade_plan = (
            f"Entry {row.get('entry', '')} | SL {row.get('stop_loss', '')} | "
            f"TGT {row.get('target', '')} | R/R {row.get('reward_risk', '')}"
        )
        zone_summary = (
            f"Demand {row.get('demand_zone', 'N/A')} | "
            f"Supply {row.get('supply_zone', 'N/A')}"
        )
        return {
            "Rank": rank,
            "Symbol": row.get("symbol", ""),
            "Decision": row.get("buy_heading", ""),
            "Why Buy": row.get("why_buy", ""),
            "Cautions": row.get("cautions_summary", ""),
            "Score": row.get("score", ""),
            "Current Price": row.get("current_price", ""),
            "Gap %": cls._fmt_pct(row.get("gap_pct", "")),
            "Trend Check": trend_summary,
            "Trade Plan": trade_plan,
            "Technical Snapshot": technical_summary,
            "Zones": zone_summary,
            "Pattern": row.get("pattern", ""),
            "Buy/Sell Ratio": row.get("buy_sell_ratio", ""),
            "Reasons Detail": row.get("reasons", ""),
            "Warnings Detail": row.get("warnings", ""),
        }

    def sync_daily(self, result: dict[str, Any], prefix: str = "SCAN") -> str:
        client = self._client()
        sh = self._open_sheet(client)

        IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

        existing = {ws.title for ws in sh.worksheets()}
        
        date_title = dt.datetime.now(IST).strftime("%Y-%m-%d")
        final_title = f"{prefix}_{date_title}"
        is_new = final_title not in existing

        if is_new:
            ws = sh.add_worksheet(title=final_title, rows=100, cols=20)
            existing_data = []
        else:
            ws = sh.worksheet(final_title)
            existing_data = ws.get_all_records()

        # Extract previous prices and previously recommended symbols for HOURLY
        prev_prices = {}
        prev_symbols = set()
        if prefix == "HOURLY" and existing_data:
            for row in existing_data:
                sym = row.get("Symbol")
                if sym:
                    sym = str(sym).strip().upper()
                    if sym and sym != "NONE":
                        prev_symbols.add(sym)
                        curr_p = row.get("Current Price")
                        if curr_p not in (None, ""):
                            try:
                                prev_prices[sym] = float(str(curr_p).replace(',', ''))
                            except ValueError:
                                pass

        run_time_str = dt.datetime.now(IST).strftime("%H:%M")

        ctx = result.get("market_context", {})
        pcr = ctx.get("nifty_pcr")
        pcr_val = pcr if pcr is not None else "N/A"
        preopen = ctx.get("preopen_nifty", {})
        advances = preopen.get("advances", 0) if isinstance(preopen, dict) else 0
        declines = preopen.get("declines", 0) if isinstance(preopen, dict) else 0
        market_msg = f"Nifty PCR: {pcr_val} | Advances: {advances} / Declines: {declines}"

        if prefix != "HOURLY":
            if not result.get("buy_list"):
                logger.info("No buy_list rows found. Inserting placeholder row.")
                dummy = {"symbol": "NONE", "buy_heading": "No stocks met criteria"}
                rows_data = [self._format_morning_row(1, dummy)]
            else:
                rows_data = [self._format_morning_row(idx, s.to_dict()) for idx, s in enumerate(result["buy_list"], 1)]
            
            headers = list(rows_data[0].keys())
            values = [[self._sheet_cell_value(r.get(h, "")) for h in headers] for r in rows_data]
            
            if is_new:
                ws.update(range_name="A1", values=[headers] + values, value_input_option="RAW")
                ws.freeze(rows=1)
            else:
                existing_headers = list(existing_data[0].keys()) if existing_data else []
                if len(headers) > len(existing_headers):
                    ws.update(range_name="A1", values=[headers], value_input_option="RAW")
                empty_row = [""] * len(headers)
                ws.append_rows([empty_row] + values, value_input_option="RAW")

            logger.info("Google Sheet rows written: %d", len(rows_data))
            logger.info("Google Sheet sync complete: worksheet '%s'", final_title)
            return final_title

        # --- HOURLY RUN LOGIC ---
        all_signals = result.get("all_signals", [])
        buy_list = result.get("buy_list", [])
        
        # Split signals into "Old" (previously recommended) and "New" (only new to this hour)
        old_signals = [s for s in all_signals if s.symbol in prev_symbols]
        new_signals = [s for s in buy_list if s.symbol not in prev_symbols]
        
        def _build_hourly_row(idx, s_dict, prev_price, curr_price):
            r = self._format_morning_row(idx, s_dict)
            ordered = {"Run Time": run_time_str}
            
            if prev_price is not None and prev_price > 0:
                moved_abs = curr_price - prev_price
                moved_pct = (moved_abs / prev_price) * 100
                ordered["Prev Price"] = prev_price
                ordered["Hourly Move"] = f"{moved_abs:+.2f} ({self._fmt_pct(moved_pct)})"
            else:
                ordered["Prev Price"] = "N/A"
                ordered["Hourly Move"] = "NEW"
                
            ordered.update(r)
            return ordered

        old_rows_data = []
        for idx, s in enumerate(old_signals, 1):
            s_dict = s.to_dict()
            try:
                curr_price = float(str(s_dict.get("current_price", 0)).replace(',', ''))
            except ValueError:
                curr_price = 0.0
            prev = prev_prices.get(s.symbol)
            old_rows_data.append(_build_hourly_row(idx, s_dict, prev, curr_price))
            
        new_rows_data = []
        for idx, s in enumerate(new_signals, 1):
            s_dict = s.to_dict()
            try:
                curr_price = float(str(s_dict.get("current_price", 0)).replace(',', ''))
            except ValueError:
                curr_price = 0.0
            new_rows_data.append(_build_hourly_row(idx, s_dict, None, curr_price))
            
        # Ensure we always have headers, even if empty
        sample_row = old_rows_data[0] if old_rows_data else (new_rows_data[0] if new_rows_data else _build_hourly_row(1, {"symbol": "NONE", "buy_heading": "No stocks met criteria"}, None, 0.0))
        headers = list(sample_row.keys())
        
        if is_new:
            # Very first run of the day - just print headers and new stocks
            matrix = [headers]
            if new_rows_data:
                matrix.extend([[self._sheet_cell_value(r.get(h, "")) for h in headers] for r in new_rows_data])
            else:
                dummy = _build_hourly_row(1, {"symbol": "NONE", "buy_heading": "No stocks met criteria"}, None, 0.0)
                matrix.append([self._sheet_cell_value(dummy.get(h, "")) for h in headers])
                
            ws.update(range_name="A1", values=matrix, value_input_option="RAW")
            ws.freeze(rows=1)
            logger.info("Google Sheet rows written (New Hourly): %d", len(matrix) - 1)
        else:
            # Update header row if columns expanded
            existing_headers = list(existing_data[0].keys()) if existing_data else []
            if len(headers) > len(existing_headers):
                ws.update(range_name="A1", values=[headers], value_input_option="RAW")
            # Appending a subsequent hour's run
            matrix = []
            matrix.append(["", "", ""])
            matrix.append([f"=== HOURLY RUN: {run_time_str} ==="])
            matrix.append(["Market Context:", market_msg])
            matrix.append([""])
            
            if old_rows_data:
                matrix.append(["--- PREVIOUS RECOMMENDATIONS UPDATE ---"])
                matrix.append(headers)
                matrix.extend([[self._sheet_cell_value(r.get(h, "")) for h in headers] for r in old_rows_data])
                matrix.append([""])
                
            if new_rows_data:
                matrix.append(["--- NEW BUY RECOMMENDATIONS ---"])
                matrix.append(headers)
                matrix.extend([[self._sheet_cell_value(r.get(h, "")) for h in headers] for r in new_rows_data])
            else:
                matrix.append(["--- NO NEW RECOMMENDATIONS THIS HOUR ---"])
                
            ws.append_rows(matrix, value_input_option="RAW")
            logger.info("Google Sheet rows appended (Hourly Update): %d", len(old_rows_data) + len(new_rows_data))

        logger.info("Google Sheet sync complete: worksheet '%s'", final_title)
        return final_title
