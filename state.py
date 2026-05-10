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

def _empty_state() -> Dict[str, Any]:
    return {
        "last_discord_id": None,
        "open_trades": {},            # trade_id -> trade dict
        "daily_counts": {},           # yyyy-mm-dd -> int
        "seen_signal_hashes": [],     # dedupe
    }


def load_state(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return _empty_state()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # Refusing to start with a fresh empty state on a live account —
        # otherwise we'd lose track of any open positions on the exchange,
        # and they'd run unmanaged. Back up the corrupt file so the user
        # can inspect / restore manually.
        import shutil, sys
        backup = p.with_suffix(f".corrupt.{int(time.time())}.json")
        try:
            shutil.copy(p, backup)
            backup_msg = f"backup at {backup}"
        except Exception:
            backup_msg = "backup failed"
        msg = (
            f"state.json is corrupt ({e}). {backup_msg}. Restore from a known-"
            f"good copy or delete state.json after manually verifying open "
            f"positions on the exchange."
        )
        print(f"[state] FATAL: {msg}", file=sys.stderr)
        raise RuntimeError(msg) from e

def save_state(path: str, st: Dict[str, Any]) -> None:
    # Serialize under the same lock all mutations use, so json.dumps never
    # observes a dict mid-mutation. Atomic write via tmp + rename.
    # Disk failures (Railway transient FS) are caught + logged so the bot
    # doesn't crash; state.json may be momentarily out-of-date but the
    # next save attempt recovers.
    try:
        with state_lock:
            payload = json.dumps(st, ensure_ascii=False, separators=(",",":"))
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        # No logger here at module level; print to stderr so Railway
        # surfaces it. Caller code should not depend on save success.
        import sys
        print(f"[state] save_state failed: {e}", file=sys.stderr)
