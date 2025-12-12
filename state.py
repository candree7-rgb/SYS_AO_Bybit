import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

def utc_day_key(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return time.strftime("%Y-%m-%d", time.gmtime(ts))

def load_state(path: str) -> Dict[str, Any]:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "last_discord_id": None,
        "open_trades": {},            # trade_id -> trade dict
        "daily_counts": {},           # yyyy-mm-dd -> int
        "seen_signal_hashes": [],     # dedupe
    }

def save_state(path: str, st: Dict[str, Any]) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, separators=(",",":")), encoding="utf-8")
    tmp.replace(p)
