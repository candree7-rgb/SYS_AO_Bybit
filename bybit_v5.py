import time
import hmac
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
import requests
from websocket import WebSocketApp

class BybitV5:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, demo: bool = False, recv_window: str = "5000"):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.recv_window = str(recv_window)

        # wallet_equity cache — equity changes slowly and is needed on every
        # trade for qty calculation. Caching for 60s removes ~150ms of API
        # latency from the critical Discord-push → Bybit-order path.
        self._equity_cache: Dict[str, Tuple[float, float]] = {}  # account_type -> (value, ts)
        self._equity_ttl = 60.0

        # Shared HTTP session: reuses TCP connections + TLS handshake,
        # saves 30-80ms on cold calls and 5-10ms on warm calls. The default
        # urllib3 pool size is 10 — bump to 20 since we fire several
        # concurrent calls at once during warmup and trade placement.
        from requests.adapters import HTTPAdapter
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # Private-WS pushed caches — Bybit pushes wallet/position updates
        # over the same WS that delivers `execution`. When fresh, these
        # eliminate REST roundtrips entirely. Both have their own lock to
        # avoid contention with the equity TTL cache above.
        import threading as _t
        self._ws_equity: Dict[str, Tuple[float, float]] = {}     # acct -> (equity, ts)
        self._ws_equity_lock = _t.Lock()
        self._ws_equity_max_age = 30.0
        self._ws_positions: Dict[str, Tuple[float, float, float]] = {}  # symbol -> (size, avgPrice, ts)
        self._ws_pos_lock = _t.Lock()
        self._ws_pos_max_age = 10.0

        # Demo trading uses different endpoints (paper trading on live market data)
        if demo:
            self.base = "https://api-demo.bybit.com"
            self.ws   = "wss://stream-demo.bybit.com/v5/private"
            self.ws_public = "wss://stream.bybit.com/v5/public/linear"  # demo has no public stream — use mainnet
        elif testnet:
            self.base = "https://api-testnet.bybit.com"
            self.ws   = "wss://stream-testnet.bybit.com/v5/private"
            self.ws_public = "wss://stream-testnet.bybit.com/v5/public/linear"
        else:
            self.base = "https://api.bybit.com"
            self.ws   = "wss://stream.bybit.com/v5/private"
            self.ws_public = "wss://stream.bybit.com/v5/public/linear"

    # ---------- signing ----------
    def _sign(self, ts: str, recv_window: str, payload: str) -> str:
        msg = ts + self.api_key + recv_window + payload
        return hmac.new(self.api_secret, msg.encode(), hashlib.sha256).hexdigest()

    def _headers(self, payload: str) -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        sign = self._sign(ts, self.recv_window, payload)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json",
        }

    def _build_query_string(self, params: Dict[str, Any]) -> str:
        """Build sorted query string for GET request signatures."""
        return "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    def _check(self, data: Dict[str, Any]) -> Dict[str, Any]:
        # Bybit returns retCode/retMsg
        if isinstance(data, dict) and data.get("retCode", 0) not in (0, "0"):
            raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')} | {data}")
        return data

    # ---------- Market data ----------
    def last_price(self, category: str, symbol: str) -> float:
        r = self._session.get(f"{self.base}/v5/market/tickers", params={"category": category, "symbol": symbol}, timeout=10)
        r.raise_for_status()
        data = self._check(r.json())
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("No ticker data")
        return float(lst[0]["lastPrice"])

    def instruments_info(self, category: str, symbol: str) -> Dict[str, Any]:
        r = self._session.get(f"{self.base}/v5/market/instruments-info", params={"category": category, "symbol": symbol}, timeout=10)
        r.raise_for_status()
        data = self._check(r.json())
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("No instrument info")
        return lst[0]

    # ---------- Account ----------
    def get_cached_position(self, symbol: str):
        """Return (size, avgPrice) from WS cache if fresh, else None.
        Reads are lock-protected so the WS thread can write atomically."""
        with self._ws_pos_lock:
            t = self._ws_positions.get(symbol)
        if t and (time.time() - t[2]) < self._ws_pos_max_age:
            return t[0], t[1]
        return None

    def wallet_equity(self, account_type: str = "UNIFIED", force_refresh: bool = False) -> float:
        # Prefer the WS-pushed value if it's fresh — sub-ms read, no REST.
        if not force_refresh:
            with self._ws_equity_lock:
                ws = self._ws_equity.get(account_type)
            if ws and (time.time() - ws[1]) < self._ws_equity_max_age:
                return ws[0]
            cached = self._equity_cache.get(account_type)
            if cached is not None and (time.time() - cached[1]) < self._equity_ttl:
                return cached[0]

        params = {"accountType": account_type}
        query_string = self._build_query_string(params)
        # Use query string in URL (not params=) to ensure order matches signature
        r = self._session.get(
            f"{self.base}/v5/account/wallet-balance?{query_string}",
            headers=self._headers(query_string),
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("No wallet balance")
        item = lst[0]
        # prefer totalEquity if present
        val = item.get("totalEquity") or item.get("totalWalletBalance") or item.get("totalAvailableBalance")
        value = float(val)
        self._equity_cache[account_type] = (value, time.time())
        return value

    def set_leverage(self, category: str, symbol: str, leverage) -> Dict[str, Any]:
        # leverage may be int (e.g. 20) or float (e.g. 12.5 for B/FHE/HIGH).
        # Bybit accepts both as strings; format float without trailing zeros.
        lev_str = f"{leverage:g}" if isinstance(leverage, float) else str(leverage)
        body = {
            "category": category,
            "symbol": symbol,
            "buyLeverage": lev_str,
            "sellLeverage": lev_str,
        }
        payload = json.dumps(body, separators=(",", ":"))
        r = self._session.post(f"{self.base}/v5/position/set-leverage", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return self._check(r.json())

    # ---------- Orders ----------
    def place_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        r = self._session.post(f"{self.base}/v5/order/create", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return self._check(r.json())

    def cancel_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        r = self._session.post(f"{self.base}/v5/order/cancel", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return self._check(r.json())

    def open_orders(self, category: str, symbol: str) -> List[Dict[str, Any]]:
        params = {"category": category, "symbol": symbol}
        query_string = self._build_query_string(params)
        r = self._session.get(
            f"{self.base}/v5/order/realtime?{query_string}",
            headers=self._headers(query_string),
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        return ((data.get("result") or {}).get("list") or [])

    def order_history(self, category: str, symbol: str, order_link_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        params = {"category": category, "symbol": symbol, "limit": limit}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        query_string = self._build_query_string(params)
        r = self._session.get(
            f"{self.base}/v5/order/history?{query_string}",
            headers=self._headers(query_string),
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        return ((data.get("result") or {}).get("list") or [])

    # ---------- Positions ----------
    def positions(self, category: str, symbol: str = "") -> List[Dict[str, Any]]:
        params = {"category": category}
        if symbol:  # Only add symbol if specified
            params["symbol"] = symbol
        params["settleCoin"] = "USDT"  # Required for fetching all positions
        query_string = self._build_query_string(params)
        r = self._session.get(
            f"{self.base}/v5/position/list?{query_string}",
            headers=self._headers(query_string),
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        return ((data.get("result") or {}).get("list") or [])

    def set_trading_stop(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        r = self._session.post(f"{self.base}/v5/position/trading-stop", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        # 34040 = "not modified" - SL/TP already set to same value, ignore this
        if data.get("retCode") == 34040:
            return data
        return self._check(data)

    def closed_pnl(self, category: str, symbol: str, start_time: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get closed PnL records for a symbol."""
        params = {"category": category, "symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        query_string = self._build_query_string(params)
        r = self._session.get(
            f"{self.base}/v5/position/closed-pnl?{query_string}",
            headers=self._headers(query_string),
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        return ((data.get("result") or {}).get("list") or [])

    # ---------- WebSocket (private executions & orders) ----------
    def run_private_ws(self, on_execution, on_order=None, on_wallet=None,
                       on_position=None, on_error=None, account_type: str = "UNIFIED"):
        expires = int(time.time() * 1000) + 10_000
        sign_payload = f"GET/realtime{expires}"
        sig = hmac.new(self.api_secret, sign_payload.encode(), hashlib.sha256).hexdigest()

        def _on_open(ws):
            ws.send(json.dumps({"op": "auth", "args": [self.api_key, expires, sig]}))
            # Subscribe to all 4 topics we care about. wallet+position
            # eliminate REST polls in the hot path; Bybit pushes a
            # snapshot on auth-success so caches re-seed automatically
            # after every reconnect.
            ws.send(json.dumps({"op": "subscribe", "args": ["execution", "order", "wallet", "position"]}))

        def _on_message(ws, message):
            try:
                msg = json.loads(message)
            except Exception:
                return
            if msg.get("op") == "auth" and msg.get("success") is False and on_error:
                on_error(RuntimeError(f"WS auth failed: {msg}"))
                return
            topic = msg.get("topic", "")
            data = msg.get("data")
            if topic.startswith("execution") and data:
                for ev in (data if isinstance(data, list) else [data]):
                    on_execution(ev)
            if topic.startswith("order") and data and on_order:
                for ev in (data if isinstance(data, list) else [data]):
                    on_order(ev)
            if topic == "wallet" and data:
                for ev in (data if isinstance(data, list) else [data]):
                    try:
                        val = float(ev.get("totalEquity") or 0)
                        if val > 0:
                            acct = ev.get("accountType", account_type)
                            ts = time.time()
                            with self._ws_equity_lock:
                                self._ws_equity[acct] = (val, ts)
                            # Also poke the TTL cache so REST callers hit hot
                            self._equity_cache[acct] = (val, ts)
                    except (TypeError, ValueError):
                        pass
                    if on_wallet:
                        on_wallet(ev)
            if topic.startswith("position") and data:
                for ev in (data if isinstance(data, list) else [data]):
                    sym = ev.get("symbol")
                    if sym:
                        try:
                            size = float(ev.get("size") or 0)
                            avg = float(ev.get("avgPrice") or ev.get("entryPrice") or 0)
                            with self._ws_pos_lock:
                                self._ws_positions[sym] = (size, avg, time.time())
                        except (TypeError, ValueError):
                            pass
                    if on_position:
                        on_position(ev)

        def _on_err(ws, err):
            if on_error:
                on_error(err)

        ws = WebSocketApp(self.ws, on_open=_on_open, on_message=_on_message, on_error=_on_err)
        ws.run_forever(ping_interval=20, ping_timeout=10)

    # ---------- WebSocket (public market data) ----------
    def run_public_ws(self, on_open, on_message_raw, on_error=None):
        """Run a public market WebSocket. Caller manages subscriptions via on_open
        (which receives the WSApp so it can ws.send subscribe payloads).

        on_message_raw(ws, parsed_json_msg) — called for every parsed message.
        Caller is responsible for filtering by topic.
        """
        def _on_open(ws):
            try:
                on_open(ws)
            except Exception:
                pass

        def _on_message(ws, message):
            try:
                msg = json.loads(message)
            except Exception:
                return
            on_message_raw(ws, msg)

        def _on_err(ws, err):
            if on_error:
                on_error(err)

        ws = WebSocketApp(self.ws_public, on_open=_on_open, on_message=_on_message, on_error=_on_err)
        ws.run_forever(ping_interval=20, ping_timeout=10)
