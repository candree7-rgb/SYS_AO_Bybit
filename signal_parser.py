"""
Signal parser for the AO Crusher Discord bot embed format.

Example fresh signal (description body of the embed):
    🔴 **SHORT SIGNAL** • Leverage: **25x**
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    📊 **ENTRY**
    `$0.125688`
    🎯 **PROFIT TARGETS**
    ⏳ TP1: `$0.124683`
    ⏳ TP2: `$0.123677`
    ⏳ TP3: `$0.120661`
    ⏳ TP4: `$0.075413`
    🛑 SL: `$0.138257`

The symbol comes from the embed *title*: "AO Crusher • UB #21" → base = UB.

We only parse FRESH signals, not edited ones. Edited messages get markers
like "✅ Triggered", "✅ TP1: ..." (checkmark instead of hourglass), or a
STATUS section ("Trade still active", "Breakeven hit", "Closed P&L: ...").
We skip those — the signal_hash dedupe + last_discord_id windowing handles
re-delivery of the same message.

TP4 is intentionally dropped: at ~40% it never realistically fills; the
remainder runs as a runner / trail after TP3.

This format has no DCAs.
"""

import re
import hashlib
from typing import Any, Dict, List, Optional

NUM = r"([0-9]+(?:\.[0-9]+)?)"

# Symbol from embed title: "AO Crusher • UB #21", "AO Crusher • BILL #1", etc.
RE_TITLE_SYMBOL = re.compile(
    r"AO\s+Crusher\s*[•·\|]\s*([A-Z0-9]+)\s*#\s*\d+",
    re.I,
)
RE_SIDE = re.compile(r"\b(SHORT|LONG)\s+SIGNAL\b", re.I)
# "ENTRY" header followed (possibly across newlines/markdown) by a price.
RE_ENTRY = re.compile(
    r"ENTRY[\s\*\n\r]*`?\$?" + NUM + r"`?",
    re.I,
)
RE_TP = re.compile(r"TP\s*(\d+)\s*:\s*`?\$?" + NUM + r"`?", re.I)
RE_SL = re.compile(r"\bSL\s*:\s*`?\$?" + NUM + r"`?", re.I)

# Markers that mean the message is past the fresh-pending state. If any of
# these is present we do NOT parse it as a new signal (avoids re-entering
# a trade we may have already taken, or chasing an already-filled-out move).
RE_PROGRESSED_MARKERS = re.compile(
    r"Closed\s+P&L|"
    r"TRADE\s+CLOSED|"
    r"Trade\s+still\s+active|"
    r"\b(?:TP\d+|Profit|Breakeven)\s+secured\b|"
    r"Breakeven\s+hit",
    re.I,
)

# Marker that the close action came from the signal provider (used by
# main.py edit-detection to know it should bail out / alert).
RE_TRADE_CLOSED_NEW = re.compile(r"Closed\s+P&L|TRADE\s+CLOSED", re.I)


def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    if not RE_SIDE.search(text):
        return None
    if RE_PROGRESSED_MARKERS.search(text):
        return None

    msym = RE_TITLE_SYMBOL.search(text)
    if not msym:
        return None
    base = msym.group(1).upper()
    symbol = f"{base}{quote}"

    side_word = RE_SIDE.search(text).group(1).upper()
    side = "sell" if side_word == "SHORT" else "buy"

    mentry = RE_ENTRY.search(text)
    if not mentry:
        return None
    trigger = float(mentry.group(1))

    tps: List[float] = []
    for m in RE_TP.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        while len(tps) < idx:
            tps.append(0.0)
        tps[idx - 1] = price
    tps = [p for p in tps if p > 0]
    # Drop TP4 — provider sets it at ~40%, never realistic. Runner replaces it.
    tps = tps[:3]

    sl: Optional[float] = None
    msl = RE_SL.search(text)
    if msl:
        sl = float(msl.group(1))

    return {
        "base": base,
        "symbol": symbol,
        "side": side,
        "trigger": trigger,
        "tp_prices": tps,
        "dca_prices": [],
        "sl_price": sl,
        "raw": text,
    }


def signal_hash(sig: Dict[str, Any]) -> str:
    core = f"{sig.get('symbol')}|{sig.get('side')}|{sig.get('trigger')}|{sig.get('tp_prices')}|{sig.get('dca_prices')}"
    return hashlib.md5(core.encode("utf-8")).hexdigest()


def parse_signal_update(text: str) -> Dict[str, Any]:
    """Re-parse SL/TP from any message (used to detect post-fill SL/TP edits
    on an already-tracked trade). Returns dict with sl_price + tp_prices.
    DCAs are not part of the AO Crusher format, kept empty for compatibility."""
    result = {
        "sl_price": None,
        "tp_prices": [],
        "dca_prices": [],
    }

    msl = RE_SL.search(text)
    if msl:
        result["sl_price"] = float(msl.group(1))

    tp_matches = list(RE_TP.finditer(text))
    if tp_matches:
        tps = [(int(m.group(1)), float(m.group(2))) for m in tp_matches]
        tps.sort(key=lambda x: x[0])
        result["tp_prices"] = [tp[1] for tp in tps][:3]

    return result


def is_trade_closed(text: str) -> bool:
    """True if the message indicates the signal provider closed the trade."""
    return bool(RE_TRADE_CLOSED_NEW.search(text))
