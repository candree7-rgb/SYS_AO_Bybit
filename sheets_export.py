"""
Google Sheets Export Module

Exports trade data to Google Sheets for visualization in Looker Studio.

Setup:
1. Go to Google Cloud Console -> APIs & Services -> Credentials
2. Create a Service Account
3. Download JSON key file
4. Enable Google Sheets API
5. Create a Google Sheet and share it with the service account email (Editor)
6. Set env vars:
   - GOOGLE_SHEETS_CREDS: Base64 encoded JSON key OR path to JSON file
   - GOOGLE_SHEET_ID: The spreadsheet ID from the URL
"""

import os
import json
import base64
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List

log = logging.getLogger("sheets")

# Sheet column headers
TRADE_HEADERS = [
    "Trade ID",
    "Symbol",
    "Side",
    "Entry Price",
    "Trigger Price",
    "Placed Time",
    "Filled Time",
    "Closed Time",
    "Duration (min)",
    "Realized PnL",
    "PnL % (Margin)",
    "Equity at Close",
    "PnL % (Equity)",
    "Win/Loss",
    "Exit Reason",
    "TPs Hit",
    "TP Count",
    "DCAs Filled",
    "DCA Count",
    "Trailing Used",
]


def _get_credentials():
    """Get Google credentials from env var (base64 JSON or file path)."""
    creds_env = os.getenv("GOOGLE_SHEETS_CREDS", "")

    if not creds_env:
        return None

    # Try as file path first
    if os.path.isfile(creds_env):
        with open(creds_env, 'r') as f:
            return json.load(f)

    # Try as base64 encoded JSON
    try:
        decoded = base64.b64decode(creds_env)
        return json.loads(decoded)
    except Exception:
        pass

    # Try as raw JSON
    try:
        return json.loads(creds_env)
    except Exception:
        log.error("Failed to parse GOOGLE_SHEETS_CREDS - must be file path, base64 JSON, or raw JSON")
        return None


def _get_sheet():
    """Get the Google Sheet worksheet."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log.warning("gspread not installed. Run: pip install gspread google-auth")
        return None

    creds_data = _get_credentials()
    if not creds_data:
        return None

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        log.warning("GOOGLE_SHEET_ID not set")
        return None

    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        credentials = Credentials.from_service_account_info(creds_data, scopes=scopes)
        gc = gspread.authorize(credentials)
        spreadsheet = gc.open_by_key(sheet_id)

        # Get or create "Trades" worksheet
        try:
            worksheet = spreadsheet.worksheet("Trades")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title="Trades", rows=1000, cols=20)
            # Add headers
            worksheet.update('A1:T1', [TRADE_HEADERS])
            worksheet.format('A1:T1', {'textFormat': {'bold': True}})
            log.info("Created 'Trades' worksheet with headers")

        return worksheet
    except Exception as e:
        log.error(f"Failed to connect to Google Sheets: {e}")
        return None


def _ts_to_datetime(ts: float) -> str:
    """Convert timestamp to readable datetime string."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _trade_to_row(trade: Dict[str, Any]) -> List[Any]:
    """Convert trade dict to spreadsheet row."""
    filled_ts = trade.get("filled_ts") or 0
    closed_ts = trade.get("closed_ts") or 0
    duration_min = round((closed_ts - filled_ts) / 60, 1) if filled_ts and closed_ts else 0

    # Calculate PnL % relative to margin used
    pnl = trade.get("realized_pnl", 0) or 0
    margin_used = trade.get("margin_used", 0) or 0
    pnl_margin_pct = round((pnl / margin_used) * 100, 2) if margin_used > 0 else 0

    # Calculate PnL % relative to total equity BEFORE the trade
    # equity_at_close already includes the PnL, so we subtract it to get equity before
    equity_after = trade.get("equity_at_close", 0) or 0
    equity_before = equity_after - pnl  # Equity before this trade's PnL
    pnl_equity_pct = round((pnl / equity_before) * 100, 4) if equity_before > 0 else 0

    return [
        trade.get("id", ""),
        trade.get("symbol", ""),
        trade.get("side", ""),
        trade.get("entry_price", ""),
        trade.get("trigger", ""),
        _ts_to_datetime(trade.get("placed_ts")),
        _ts_to_datetime(filled_ts),
        _ts_to_datetime(closed_ts),
        duration_min,
        pnl,
        pnl_margin_pct,
        equity_after,
        pnl_equity_pct,
        "WIN" if trade.get("is_win") else "LOSS",
        trade.get("exit_reason", "unknown"),
        trade.get("tp_fills", 0),
        trade.get("tp_count", 3),
        trade.get("dca_fills", 0),
        trade.get("dca_count", 2),
        "Yes" if trade.get("trailing_used") else "No",
    ]


def export_trade(trade: Dict[str, Any]) -> bool:
    """Export a single trade to Google Sheets. Returns True on success."""
    worksheet = _get_sheet()
    if not worksheet:
        return False

    try:
        row = _trade_to_row(trade)
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        log.info(f"Exported trade {trade.get('id')} to Google Sheets")
        return True
    except Exception as e:
        log.error(f"Failed to export trade to Sheets: {e}")
        return False


def export_trades_batch(trades: List[Dict[str, Any]]) -> int:
    """Export multiple trades at once. Returns count of exported trades."""
    worksheet = _get_sheet()
    if not worksheet:
        return 0

    try:
        rows = [_trade_to_row(t) for t in trades]
        if rows:
            worksheet.append_rows(rows, value_input_option='USER_ENTERED')
            log.info(f"Exported {len(rows)} trades to Google Sheets")
        return len(rows)
    except Exception as e:
        log.error(f"Failed to batch export trades: {e}")
        return 0


def export_stats_summary(stats_7d: Dict, stats_30d: Dict, stats_all: Dict) -> bool:
    """Export stats summary to a separate 'Stats' worksheet."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return False

    creds_data = _get_credentials()
    if not creds_data:
        return False

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        return False

    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        credentials = Credentials.from_service_account_info(creds_data, scopes=scopes)
        gc = gspread.authorize(credentials)
        spreadsheet = gc.open_by_key(sheet_id)

        # Get or create "Stats" worksheet
        try:
            ws = spreadsheet.worksheet("Stats")
        except gspread.exceptions.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title="Stats", rows=20, cols=10)

        # Update stats
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = [
            ["Last Updated", now],
            [""],
            ["Period", "Trades", "Wins", "Losses", "Win Rate", "Total PnL", "Avg PnL", "Best", "Worst"],
            ["7 Days", stats_7d["total_trades"], stats_7d["wins"], stats_7d["losses"],
             f"{stats_7d['win_rate']}%", f"${stats_7d['total_pnl']:.2f}", f"${stats_7d['avg_pnl']:.2f}",
             f"${stats_7d['best_trade']:.2f}", f"${stats_7d['worst_trade']:.2f}"],
            ["30 Days", stats_30d["total_trades"], stats_30d["wins"], stats_30d["losses"],
             f"{stats_30d['win_rate']}%", f"${stats_30d['total_pnl']:.2f}", f"${stats_30d['avg_pnl']:.2f}",
             f"${stats_30d['best_trade']:.2f}", f"${stats_30d['worst_trade']:.2f}"],
            ["All Time", stats_all["total_trades"], stats_all["wins"], stats_all["losses"],
             f"{stats_all['win_rate']}%", f"${stats_all['total_pnl']:.2f}", f"${stats_all['avg_pnl']:.2f}",
             f"${stats_all['best_trade']:.2f}", f"${stats_all['worst_trade']:.2f}"],
        ]

        ws.update('A1:I6', data)
        ws.format('A3:I3', {'textFormat': {'bold': True}})
        log.info("Updated stats summary in Google Sheets")
        return True

    except Exception as e:
        log.error(f"Failed to export stats: {e}")
        return False


def is_enabled() -> bool:
    """Check if Google Sheets export is configured."""
    return bool(os.getenv("GOOGLE_SHEETS_CREDS")) and bool(os.getenv("GOOGLE_SHEET_ID"))
