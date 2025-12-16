import re
import hashlib
from typing import Any, Dict, Optional, List

NUM = r"([0-9]+(?:\.[0-9]+)?)"

RE_SYMBOL_SIDE = re.compile(r"\*\*([A-Z0-9]+)\*\*\s+(LONG|SHORT)\s+Signal", re.I)
RE_ENTER_TRIGGER = re.compile(r"Enter\s+on\s+Trigger\s*:\s*`?\$?"+NUM+r"`?", re.I)
# Support both: "Entry $0.08850" (header) and "**Entry:** `$0.08850000`" (body)
RE_ENTRY = re.compile(r"(?:\*\*)?Entry:?(?:\*\*)?\s*`?\$?"+NUM+r"`?", re.I)

RE_TP = re.compile(r"\*\*TP(\d+)\:\*\*\s*`?\$?"+NUM+r"`?", re.I)
RE_DCA = re.compile(r"\*\*DCA\s*#?\s*(\d+)\:\*\*\s*`?\$?"+NUM+r"`?", re.I)
RE_SL = re.compile(r"\*\*Stop\s+Loss\:\*\*\s*`?\$?"+NUM+r"`?", re.I)

RE_AWAITING = re.compile(r"AWAITING\s+ENTRY", re.I)
RE_CLOSED = re.compile(r"TRADE\s+CLOSED", re.I)

def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    # We only want fresh "NEW SIGNAL" entries, not closed summaries
    if "NEW SIGNAL" not in text.upper():
        return None
    if RE_CLOSED.search(text):
        return None

    ms = RE_SYMBOL_SIDE.search(text)
    if not ms:
        return None
    base = ms.group(1).upper()
    side_word = ms.group(2).upper()
    side = "sell" if side_word == "SHORT" else "buy"
    symbol = f"{base}{quote}"

    mtr = RE_ENTER_TRIGGER.search(text) or RE_ENTRY.search(text)
    if not mtr:
        return None
    trigger = float(mtr.group(1))

    tps: List[float] = []
    for m in RE_TP.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        # keep in order
        while len(tps) < idx:
            tps.append(0.0)
        tps[idx-1] = price
    tps = [p for p in tps if p > 0]

    dcas: List[float] = []
    for m in RE_DCA.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        while len(dcas) < idx:
            dcas.append(0.0)
        dcas[idx-1] = price
    dcas = [p for p in dcas if p > 0]

    sl = None
    msl = RE_SL.search(text)
    if msl:
        sl = float(msl.group(1))

    return {
        "base": base,
        "symbol": symbol,
        "side": side,          # buy/ sell
        "trigger": trigger,
        "tp_prices": tps,
        "dca_prices": dcas,
        "sl_price": sl,
        "raw": text,
    }

def signal_hash(sig: Dict[str, Any]) -> str:
    core = f"{sig.get('symbol')}|{sig.get('side')}|{sig.get('trigger')}|{sig.get('tp_prices')}|{sig.get('dca_prices')}"
    return hashlib.md5(core.encode("utf-8")).hexdigest()
