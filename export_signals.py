"""
Export the entire history of an AO Crusher Discord channel to JSON + CSV
for backtesting.

Walks backwards through the channel via Discord's REST API (`before`
cursor pagination, 100 msgs/page), parses each message with the same
signal_parser the live bot uses, and writes:

  signals.jsonl   — one line per parsed signal (raw text included for
                    debugging). Latest first.
  signals.csv     — flattened table with key fields. Open in Excel/sheets.

Edits are NOT historically retrievable from Discord's API — what you get
is the FINAL state of each message (i.e. signals that closed will show
"Closed P&L" in their text; pending/active will show their current
status). For backtesting that's actually what you want: ground truth of
how the trade ended up.

Usage:
    DISCORD_TOKEN=... CHANNEL_ID=... python export_signals.py
    # optional:
    DISCORD_TOKEN=... CHANNEL_ID=... LIMIT=2000 python export_signals.py
        # stop after N messages instead of full history
    DISCORD_TOKEN=... CHANNEL_ID=... AFTER_ID=1234... python export_signals.py
        # only fetch messages newer than this Discord message ID

Discord rate limit: ~50 requests/sec per token. Script auto-throttles
via the existing _request_with_retry on 429.
"""

import csv
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from discord_reader import DiscordReader
from signal_parser import (
    parse_signal,
    parse_signal_update,
    is_trade_closed,
    RE_TITLE_SYMBOL,
    RE_SIDE,
)

OUT_JSONL = "signals.jsonl"
OUT_CSV   = "signals.csv"


def classify_status(text: str) -> str:
    """Classify the trade status from final message text."""
    upper = text.upper()
    if is_trade_closed(text):
        # closed — look for win/loss markers
        if "STOP LOSS HIT" in upper or "X LOSS" in upper or "❌ LOSS" in text:
            return "loss"
        if "BREAKEVEN" in upper:
            return "breakeven"
        if "WIN" in upper or "PROFIT SECURED" in upper:
            return "win"
        return "closed"
    if "ACTIVE" in upper or "TRADE STILL ACTIVE" in upper:
        return "active"
    if "TRADE CANCELLED" in upper or "CLOSED WITHOUT ENTRY" in upper:
        return "cancelled"
    return "unknown"


def closed_pnl_from_text(text: str) -> Optional[float]:
    """Extract Closed P&L percentage (signed) from final text. None if absent."""
    m = re.search(r"Closed\s+P&L:\s*([+-]?\d+(?:\.\d+)?)\s*%", text, re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def open_pnl_from_text(text: str) -> Optional[float]:
    m = re.search(r"Open\s+P&L:\s*([+-]?\d+(?:\.\d+)?)\s*%", text, re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def tps_status_from_text(text: str) -> Dict[int, bool]:
    """Returns {1: True, 2: False, ...} indicating which TP levels are hit
    (have ✅) vs pending (⏳)."""
    out: Dict[int, bool] = {}
    # Pattern catches both "✅ TP1: $..." and "⏳ TP1: $..."
    for m in re.finditer(r"(✅|⏳|🎯)\s*TP\s*(\d+)\s*:", text):
        marker, idx = m.group(1), int(m.group(2))
        out[idx] = (marker == "✅")
    return out


def export_message(msg: Dict[str, Any], reader: DiscordReader) -> Optional[Dict[str, Any]]:
    """Convert a raw Discord message into a backtest-friendly record.
    Returns None if the message doesn't look like a signal at all."""
    txt = reader.extract_text(msg)
    if not txt:
        return None
    if not RE_SIDE.search(txt) and not RE_TITLE_SYMBOL.search(txt):
        return None

    # Try the strict parser first (only matches "fresh" un-progressed signals).
    fresh = parse_signal(txt, quote="USDT")
    # Fall back to the lenient parser (handles edited / closed signals).
    upd = parse_signal_update(txt)

    side_m = RE_SIDE.search(txt)
    sym_m = RE_TITLE_SYMBOL.search(txt)
    side = side_m.group(1).upper() if side_m else None
    base = sym_m.group(1).upper() if sym_m else None

    # Trigger price extraction from text — the lenient parser drops it,
    # so we look for the dollar amount near "ENTRY".
    trigger = None
    me = re.search(r"ENTRY[\s\*\n\r]*`?\$?(\d+(?:\.\d+)?)`?", txt, re.I)
    if me:
        try:
            trigger = float(me.group(1))
        except ValueError:
            pass

    return {
        "msg_id": str(msg.get("id") or ""),
        "timestamp_iso": msg.get("timestamp") or "",
        "timestamp_unix": reader.message_timestamp_unix(msg),
        "edited_timestamp": msg.get("edited_timestamp") or "",
        "base_symbol": base,
        "symbol": f"{base}USDT" if base else None,
        "side": side,
        "trigger": trigger,
        "tp_prices": upd.get("tp_prices") or [],
        "sl_price": upd.get("sl_price"),
        "tps_hit": tps_status_from_text(txt),
        "status": classify_status(txt),
        "closed_pnl_pct": closed_pnl_from_text(txt),
        "open_pnl_pct": open_pnl_from_text(txt),
        "fresh_parsable": fresh is not None,
        "raw_text": txt,
    }


def main():
    token = os.getenv("DISCORD_TOKEN", "").strip()
    channel = os.getenv("CHANNEL_ID", "").strip()
    if not token or not channel:
        print("ERROR: set DISCORD_TOKEN and CHANNEL_ID env vars", file=sys.stderr)
        sys.exit(1)

    limit_total = int(os.getenv("LIMIT", "0"))  # 0 = unbounded
    after_id    = os.getenv("AFTER_ID", "").strip() or None

    reader = DiscordReader(token, channel)

    records: List[Dict[str, Any]] = []
    raw_count = 0
    parsed_count = 0
    cursor: Optional[str] = None  # message-id; None = start from latest

    print(f"[export] starting, channel={channel}", file=sys.stderr)
    while True:
        page = reader.fetch_before(cursor, limit=100)
        if not page:
            break

        for msg in page:
            raw_count += 1
            mid = msg.get("id") or ""

            # If user requested a backstop, stop once we go past it
            if after_id and int(mid or "0") <= int(after_id):
                page = []
                break

            rec = export_message(msg, reader)
            if rec is None:
                continue
            records.append(rec)
            parsed_count += 1

        if not page:
            break
        # Page is sorted newest→oldest; cursor for next page = oldest id
        oldest = min(int(m.get("id", "0")) for m in page)
        cursor = str(oldest)

        if limit_total and raw_count >= limit_total:
            print(f"[export] reached LIMIT={limit_total}, stopping", file=sys.stderr)
            break

        if raw_count % 500 == 0:
            print(f"[export] scanned {raw_count} msgs, {parsed_count} signals so far...", file=sys.stderr)

        # Polite pacing — Discord rate limit is ~50/s, we use 1 call per
        # 100 msgs so this is well under, but small sleep helps avoid bursts.
        time.sleep(0.1)

    print(f"[export] done. scanned={raw_count} signals_parsed={parsed_count}", file=sys.stderr)

    # --- write outputs ---
    with open(OUT_JSONL, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[export] wrote {len(records)} → {OUT_JSONL}", file=sys.stderr)

    csv_fields = [
        "msg_id", "timestamp_iso", "timestamp_unix", "edited_timestamp",
        "base_symbol", "symbol", "side", "trigger",
        "tp1", "tp2", "tp3", "tp4", "sl_price",
        "tp1_hit", "tp2_hit", "tp3_hit", "tp4_hit",
        "status", "closed_pnl_pct", "open_pnl_pct", "fresh_parsable",
    ]
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for r in records:
            tps = r.get("tp_prices") or []
            hit = r.get("tps_hit") or {}
            row = {
                "msg_id":          r["msg_id"],
                "timestamp_iso":   r["timestamp_iso"],
                "timestamp_unix":  r["timestamp_unix"],
                "edited_timestamp": r["edited_timestamp"],
                "base_symbol":     r["base_symbol"],
                "symbol":          r["symbol"],
                "side":            r["side"],
                "trigger":         r["trigger"],
                "tp1":             tps[0] if len(tps) > 0 else None,
                "tp2":             tps[1] if len(tps) > 1 else None,
                "tp3":             tps[2] if len(tps) > 2 else None,
                "tp4":             tps[3] if len(tps) > 3 else None,
                "sl_price":        r["sl_price"],
                "tp1_hit":         hit.get(1, False),
                "tp2_hit":         hit.get(2, False),
                "tp3_hit":         hit.get(3, False),
                "tp4_hit":         hit.get(4, False),
                "status":          r["status"],
                "closed_pnl_pct":  r["closed_pnl_pct"],
                "open_pnl_pct":    r["open_pnl_pct"],
                "fresh_parsable":  r["fresh_parsable"],
            }
            w.writerow(row)
    print(f"[export] wrote {len(records)} rows → {OUT_CSV}", file=sys.stderr)


if __name__ == "__main__":
    main()
