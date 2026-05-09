import json
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

# Module-level lock that EVERY state mutation + save_state acquires.
# RLock so the same thread can re-enter (e.g. fast_signal_handler holds
# it across check + persist; engine.on_execution acquires it inside
# callbacks). Concurrent JSON serialization during mutation would
# otherwise raise "dict changed size during iteration" and corrupt
# state.json.
state_lock = threading.RLock()


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
    # Serialize under the same lock all mutations use, so json.dumps never
    # observes a dict mid-mutation. Atomic write via tmp + rename.
    with state_lock:
        payload = json.dumps(st, ensure_ascii=False, separators=(",",":"))
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(p)
