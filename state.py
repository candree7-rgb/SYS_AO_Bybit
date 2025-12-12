import json
from pathlib import Path
from datetime import datetime, timezone

STATE_FILE = Path("state.json")

def _utc_day_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {
        "last_discord_id": None,
        "open_trades": {},          # symbol -> trade dict
        "daily_counts": {},         # day -> count
        "seen_hashes": []           # dedupe
    }

def save_state(st: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def incr_daily(st: dict):
    day = _utc_day_key()
    st["daily_counts"].setdefault(day, 0)
    st["daily_counts"][day] += 1
    return st["daily_counts"][day]

def get_daily(st: dict):
    return st["daily_counts"].get(_utc_day_key(), 0)
