import time, random, requests
from typing import Optional, List
from config import DISCORD_TOKEN, CHANNEL_ID

HEADERS = {"Authorization": DISCORD_TOKEN, "User-Agent": "AO-Discord-Reader/1.0"}

def fetch_messages(after_id: Optional[str], limit: int = 50) -> List[dict]:
    params = {"limit": max(1, min(limit, 100))}
    if after_id:
        params["after"] = str(after_id)

    r = requests.get(
        f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
        headers=HEADERS, params=params, timeout=15
    )
    if r.status_code == 429:
        retry = float(r.json().get("retry_after", 5))
        time.sleep(retry + 0.25)
        return []
    r.raise_for_status()
    return r.json() or []

def extract_text(msg: dict) -> str:
    parts = [msg.get("content") or ""]
    for e in (msg.get("embeds") or []):
        if isinstance(e, dict):
            parts += [e.get("title") or "", e.get("description") or ""]
            for f in (e.get("fields") or []):
                if isinstance(f, dict):
                    parts += [f.get("name") or "", f.get("value") or ""]
            footer = (e.get("footer") or {}).get("text")
            if footer: parts.append(str(footer))
    return "\n".join([p for p in parts if p]).strip()
