"""
EntryWatcher: live-cancels pending entry limit orders when TP1 is hit
before the entry fills.

Subscribes to Binance's public `<symbol>@aggTrade` stream for every
symbol with a pending entry. On every trade tick, checks whether the
price has crossed TP1; if so, cancels the entry via the supplied callback
and removes the watch.

One WebSocket connection serves all symbols. Subscriptions are managed
via JSON-RPC SUBSCRIBE/UNSUBSCRIBE messages. Reconnects automatically
and re-subscribes to the currently-watched symbols.
"""

import json
import threading
import time
from typing import Any, Callable, Dict, Optional


class EntryWatcher:
    def __init__(
        self,
        bybit,
        on_tp1_cross: Callable[[str, str, str, str], None],
        log,
    ):
        """
        on_tp1_cross(trade_id, symbol, side, entry_oid) is called when TP1 is
        crossed for a watched pending entry. The callback is responsible for
        cancelling the entry and updating trade state.
        """
        self.bybit = bybit
        self.on_tp1_cross = on_tp1_cross
        self.log = log

        self._lock = threading.Lock()
        # symbol -> { trade_id: {"side": "Buy"/"Sell", "tp1": float, "entry_oid": str} }
        self._watches: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # symbol -> (last_price, ts) — populated on every ticker tick.
        # Other code (trade_engine) reads this to skip last_price REST.
        self._last_prices: Dict[str, Any] = {}
        self._lp_max_age = 5.0
        # symbols subscribed for price-cache only (no TP1-cross watch).
        # Kept across reconnects via _on_open resubscribe.
        self._kept_symbols: set = set()
        self._ws = None  # active WebSocketApp instance
        self._subscribed: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------- public API ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def watch(self, trade_id: str, symbol: str, side: str, tp1_price: float, entry_oid: str):
        """Start watching this symbol for TP1 cross. Idempotent per (symbol, trade_id)."""
        if not tp1_price or tp1_price <= 0:
            self.log.debug(f"[watcher] skip {symbol}: no tp1_price")
            return
        with self._lock:
            self._watches.setdefault(symbol, {})[trade_id] = {
                "side": side,
                "tp1": float(tp1_price),
                "entry_oid": entry_oid,
            }
            need_subscribe = symbol not in self._subscribed and self._ws is not None
        if need_subscribe:
            self._send_subscribe([symbol])
            with self._lock:
                self._subscribed.add(symbol)
            self.log.info(f"[watcher] subscribed {symbol}@aggTrade (tp1={tp1_price}, side={side})")

    def unwatch(self, symbol: str, trade_id: Optional[str] = None):
        """Remove one trade (if trade_id given) or all trades for a symbol.
        Does NOT unsubscribe if the symbol is in _kept_symbols (price-cache use)."""
        with self._lock:
            trades = self._watches.get(symbol)
            if not trades:
                return
            if trade_id:
                trades.pop(trade_id, None)
            else:
                trades.clear()
            symbol_empty = not trades
            keep_for_cache = symbol in self._kept_symbols
            if symbol_empty:
                self._watches.pop(symbol, None)
                if not keep_for_cache:
                    already_subscribed = symbol in self._subscribed
                    self._subscribed.discard(symbol)
                else:
                    already_subscribed = False  # keep subscribed for cache
            else:
                already_subscribed = False
        if symbol_empty and already_subscribed and self._ws:
            self._send_unsubscribe([symbol])
            self.log.debug(f"[watcher] unsubscribed {symbol}@aggTrade")

    # ---------- internals ----------

    def _send(self, payload: dict):
        ws = self._ws
        if not ws:
            return
        try:
            ws.send(json.dumps(payload))
        except Exception as e:
            self.log.warning(f"[watcher] send failed: {e}")

    def _send_subscribe(self, symbols: list):
        if not symbols:
            return
        params = [f"{s.lower()}@aggTrade" for s in symbols]
        self._send({
            "method": "SUBSCRIBE",
            "params": params,
            "id": int(time.time() * 1000) & 0xFFFFFFFF,
        })

    def _send_unsubscribe(self, symbols: list):
        if not symbols:
            return
        params = [f"{s.lower()}@aggTrade" for s in symbols]
        self._send({
            "method": "UNSUBSCRIBE",
            "params": params,
            "id": int(time.time() * 1000) & 0xFFFFFFFF,
        })

    def _on_open(self, ws):
        self._ws = ws
        with self._lock:
            # Resubscribe to BOTH active watches AND price-cache-only symbols
            # so the cache survives WS reconnect.
            symbols = list(set(self._watches.keys()) | self._kept_symbols)
            self._subscribed.clear()
        # Binance's combined-stream WS allows many subscribes per message.
        # Chunk to 50 to stay well under the per-frame size limit.
        for i in range(0, len(symbols), 50):
            chunk = symbols[i : i + 50]
            self._send_subscribe(chunk)
            with self._lock:
                self._subscribed.update(chunk)
        if symbols:
            self.log.info(f"[watcher] connected, resubscribed to {len(symbols)} symbol(s)")

    def _on_message(self, ws, msg):
        # Binance @aggTrade payload (no envelope):
        #   {"e":"aggTrade","s":"BTCUSDT","p":"65000.10",...}
        # Subscribe-ack messages have {"result":null,"id":...} — skip.
        if msg.get("e") != "aggTrade":
            return
        symbol = msg.get("s")
        price_str = msg.get("p")
        if not symbol or price_str is None:
            return
        try:
            last = float(price_str)
        except (TypeError, ValueError):
            return
        # Cache last-price for any other consumer (trade_engine) to read.
        with self._lock:
            self._last_prices[symbol] = (last, time.time())
        self._check_cross(symbol, last)

    def get_last_price(self, symbol: str):
        """Return cached last-price if fresh (< _lp_max_age), else None.
        Read by trade_engine to skip last_price REST."""
        with self._lock:
            t = self._last_prices.get(symbol)
        if t and (time.time() - t[1]) < self._lp_max_age:
            return t[0]
        return None

    def ensure_subscribed(self, symbol: str):
        """Subscribe to a symbol's ticker for price-cache only (no TP1-cross
        watch). Idempotent. Survives WS reconnect via _kept_symbols."""
        with self._lock:
            self._kept_symbols.add(symbol)
            already = symbol in self._subscribed or self._ws is None
            if not already:
                self._subscribed.add(symbol)
        if not already and self._ws:
            self._send_subscribe([symbol])

    def _check_cross(self, symbol: str, last: float):
        with self._lock:
            symbol_watches = list((self._watches.get(symbol) or {}).items())
        for trade_id, w in symbol_watches:
            side = w["side"]
            tp1 = w["tp1"]
            crossed = (
                (side == "Sell" and last <= tp1)
                or (side == "Buy" and last >= tp1)
            )
            if not crossed:
                continue
            self.log.warning(
                f"🚫 [WS] {symbol} TP1 reached (last={last}, tp1={tp1}) — cancelling pending entry {trade_id}"
            )
            try:
                self.on_tp1_cross(trade_id, symbol, side, w["entry_oid"])
            except Exception as e:
                self.log.warning(f"[watcher] on_tp1_cross callback failed: {e}")
            # Remove this watch (callback is expected to also call unwatch, but
            # we drop locally too in case the callback raises).
            with self._lock:
                trades = self._watches.get(symbol)
                if trades:
                    trades.pop(trade_id, None)
                    if not trades:
                        self._watches.pop(symbol, None)
                        self._subscribed.discard(symbol)
                        unsub = True
                    else:
                        unsub = False
                else:
                    unsub = False
            if unsub and self._ws:
                self._send_unsubscribe([symbol])

    def _on_error(self, err):
        self.log.debug(f"[watcher] WS error (will reconnect): {err}")

    def _run_loop(self):
        from websocket import WebSocketApp
        while not self._stop.is_set():
            try:
                def _open(ws): self._on_open(ws)
                def _msg(ws, msg): self._on_message(ws, msg)
                def _err(err): self._on_error(err)
                self.bybit.run_public_ws(on_open=_open, on_message_raw=_msg, on_error=_err)
            except Exception as e:
                self.log.warning(f"[watcher] WS loop crashed: {e}")
            self._ws = None
            with self._lock:
                self._subscribed.clear()
            time.sleep(3)
