"""
DiscordGateway: receives new Discord messages via Gateway WebSocket (push)
instead of REST polling. ~50-300ms push latency vs 0-4s polling.

Wraps discord.py-self in a dedicated thread with its own asyncio event loop
so the rest of the codebase (which is threaded, see entry_watcher.py /
bybit_v5.py) stays unchanged. Messages are converted back to the same raw
dict format that DiscordReader.fetch_after returns and dropped into a
thread-safe queue. The main loop drains the queue.

Reconnect/Resume is handled by discord.py-self automatically. We track
consecutive identify-failures so main.py can fall back to REST polling
after N strikes.

User-Token only — this is a self-bot. Token must be obtained from a real
browser session (DevTools → Network → Authorization header). Logging in
with email/password triggers hCaptcha and is not supported.
"""

import asyncio
import queue
import threading
import time
from typing import Any, Dict, Optional


class DiscordGateway:
    def __init__(self, token: str, channel_id: str, log, max_queue_size: int = 200,
                 on_signal_callback=None, on_edit_callback=None):
        self.token = token
        self.channel_id = int(channel_id)
        self.log = log
        # Optional: synchronous callback invoked from a thread-pool worker
        # (via asyncio.to_thread) for every inbound message. Lets the main
        # loop's queue path be bypassed entirely for the hot Discord-push →
        # Bybit-order path. The callback is responsible for its own state
        # locking; the gateway just dispatches.
        self.on_signal_callback = on_signal_callback
        # Optional: same dispatch pattern but for MESSAGE_UPDATE events
        # (provider edits an existing message — usually adding TRADE CLOSED
        # or changing SL/TP). Handler should look up the trade by
        # discord_msg_id and apply diff.
        self.on_edit_callback = on_edit_callback

        self.msg_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue_size)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._connected = threading.Event()
        self._consecutive_failures = 0
        self._last_event_ts = 0.0
        self._ready_ts = 0.0
        # Fires on every newly-received message; main loop blocks on this
        # so that pushes wake processing instantly without busy-polling.
        self.msg_event = threading.Event()

    # ---------- public API ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="discord-gateway")
        self._thread.start()

    def stop(self):
        self._stop.set()
        loop = self._loop
        client = self._client
        if loop and client:
            try:
                asyncio.run_coroutine_threadsafe(client.close(), loop)
            except Exception:
                pass

    def is_healthy(self) -> bool:
        """True if connected and we've seen READY."""
        return self._connected.is_set() and self._ready_ts > 0

    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def reset_failures(self):
        self._consecutive_failures = 0

    def get_message_nowait(self) -> Optional[Dict[str, Any]]:
        """Non-blocking pop from the queue. Returns None if empty."""
        try:
            return self.msg_queue.get_nowait()
        except queue.Empty:
            return None

    def queue_size(self) -> int:
        return self.msg_queue.qsize()

    # ---------- internals ----------

    def _run_forever(self):
        try:
            import discord  # provided by discord.py-self
        except ImportError:
            self.log.error(
                "[gateway] discord.py-self not installed. "
                "Run: pip install discord.py-self==2.1.0"
            )
            return

        while not self._stop.is_set():
            try:
                self._run_once(discord)
            except Exception as e:
                self._consecutive_failures += 1
                self._connected.clear()
                self.log.warning(
                    f"[gateway] connect error #{self._consecutive_failures}: {type(e).__name__}: {e}"
                )
            finally:
                self._connected.clear()
                self._ready_ts = 0.0
                self._client = None
                if self._loop is not None:
                    try:
                        self._loop.close()
                    except Exception:
                        pass
                    self._loop = None

            if self._stop.is_set():
                break

            backoff = min(60.0, 2.0 ** min(self._consecutive_failures, 6))
            backoff += (time.time() % 1.0)  # jitter
            self.log.info(f"[gateway] reconnecting in {backoff:.1f}s (failures={self._consecutive_failures})")
            # Sleep cooperatively so stop() interrupts quickly.
            self._stop.wait(backoff)

    def _run_once(self, discord_module):
        """Single connect/listen cycle. Returns when the client disconnects."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        client = discord_module.Client()
        self._client = client

        @client.event
        async def on_ready():
            user = client.user
            uname = f"{user}" if user else "?"
            self.log.info(f"[gateway] READY as {uname} — listening on channel {self.channel_id}")
            self._connected.set()
            self._ready_ts = time.time()
            self._last_event_ts = time.time()
            self.reset_failures()

        @client.event
        async def on_resumed():
            self.log.info("[gateway] session RESUMED")
            self._connected.set()
            self._last_event_ts = time.time()

        @client.event
        async def on_disconnect():
            # Routine reconnects also call this; keep at debug.
            self.log.debug("[gateway] disconnected")
            self._connected.clear()

        @client.event
        async def on_message(message):
            try:
                if int(message.channel.id) != self.channel_id:
                    return
                now = time.time()
                self._last_event_ts = now
                # Discord-server → bot push latency. created_at is the
                # Discord-server timestamp of the message; subtract from
                # local time. Assumes NTP-synced clock (Railway is).
                lag_ms = -1.0
                try:
                    if message.created_at is not None:
                        lag_ms = (now - message.created_at.timestamp()) * 1000.0
                except Exception:
                    pass
                self.log.info(
                    f"[gateway] msg {message.id} received "
                    f"(push_lag={lag_ms:.0f}ms, qsize={self.msg_queue.qsize()})"
                )
                raw = self._message_to_dict(message)
                self._enqueue(raw)

                # Direct fast path: dispatch to thread-pool worker so the
                # Bybit place_order doesn't block the asyncio event loop
                # (which would stall the WS heartbeat). Fire-and-forget —
                # the callback owns its own error handling and state lock.
                cb = self.on_signal_callback
                if cb is not None:
                    asyncio.create_task(asyncio.to_thread(cb, raw))
            except Exception as e:
                self.log.warning(f"[gateway] on_message handler error: {e}")

        @client.event
        async def on_message_edit(before, after):
            try:
                if int(after.channel.id) != self.channel_id:
                    return
                self._last_event_ts = time.time()
                raw = self._message_to_dict(after)
                self.log.debug(f"[gateway] msg {after.id} edited")
                cb = self.on_edit_callback
                if cb is not None:
                    asyncio.create_task(asyncio.to_thread(cb, raw))
            except Exception as e:
                self.log.warning(f"[gateway] on_message_edit error: {e}")

        try:
            loop.run_until_complete(client.start(self.token))
        finally:
            try:
                if not client.is_closed():
                    loop.run_until_complete(client.close())
            except Exception:
                pass

    def _enqueue(self, raw: Dict[str, Any]):
        try:
            self.msg_queue.put_nowait(raw)
        except queue.Full:
            # Drop oldest, push newest. Better than blocking the WS thread.
            try:
                self.msg_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.msg_queue.put_nowait(raw)
                self.log.warning("[gateway] queue full — dropped oldest message")
            except queue.Full:
                pass
        finally:
            # Wake the main loop regardless of put outcome.
            self.msg_event.set()

    @staticmethod
    def _message_to_dict(message) -> Dict[str, Any]:
        """Convert a discord.py-self Message into the raw-dict shape that
        DiscordReader.fetch_after returns, so signal_parser/extract_text work
        unchanged."""
        embeds = []
        for e in message.embeds or []:
            ed: Dict[str, Any] = {}
            if getattr(e, "title", None):
                ed["title"] = e.title
            if getattr(e, "description", None):
                ed["description"] = e.description
            fields = []
            for f in getattr(e, "fields", []) or []:
                fields.append({"name": getattr(f, "name", ""), "value": getattr(f, "value", "")})
            if fields:
                ed["fields"] = fields
            footer = getattr(e, "footer", None)
            footer_text = getattr(footer, "text", None) if footer else None
            if footer_text:
                ed["footer"] = {"text": footer_text}
            embeds.append(ed)

        ts_iso = ""
        try:
            if message.created_at is not None:
                ts_iso = message.created_at.isoformat()
        except Exception:
            pass

        return {
            "id": str(message.id),
            "timestamp": ts_iso,
            "content": message.content or "",
            "embeds": embeds,
        }


# ---------- standalone smoke test ----------
if __name__ == "__main__":
    import logging
    import os
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("smoke")

    token = os.getenv("DISCORD_TOKEN", "").strip()
    channel = os.getenv("CHANNEL_ID", "").strip()
    if not token or not channel:
        log.error("Need DISCORD_TOKEN and CHANNEL_ID in env")
        sys.exit(1)

    gw = DiscordGateway(token, channel, log)
    gw.start()

    log.info("Smoke test running for 5min. Post messages in the channel to see them...")
    try:
        deadline = time.time() + 300
        while time.time() < deadline:
            m = gw.get_message_nowait()
            if m:
                log.info(f"GOT msg id={m['id']} ts={m['timestamp']} content={m['content'][:80]!r} embeds={len(m['embeds'])}")
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        gw.stop()
        log.info("smoke done")
