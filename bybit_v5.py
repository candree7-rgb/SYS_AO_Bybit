import time, hmac, hashlib, json
import requests
from typing import Any, Dict, Optional, List, Callable
from websocket import WebSocketApp


class BybitV5:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, recv_window: str = "5000"):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.recv_window = str(recv_window)
        self.base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self.ws   = "wss://stream-testnet.bybit.com/v5/private" if testnet else "wss://stream.bybit.com/v5/private"

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ---------- Signing ----------
    def _sign(self, ts: str, recv_window: str, payload: str) -> str:
        msg = ts + self.api_key + recv_window + payload
        return hmac.new(self.api_secret, msg.encode(), hashlib.sha256).hexdigest()

    def _headers(self, payload: str, recv_window: Optional[str] = None) -> Dict[str, str]:
        rw = str(recv_window or self.recv_window)
        ts = str(int(time.time() * 1000))
        sign = self._sign(ts, rw, payload)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": rw,
            "Content-Type": "application/json"
        }

    # ---------- HTTP helpers (retry) ----------
    def _request(self, method: str, path: str, *, params: Optional[dict] = None, body: Optional[dict] = None, timeout: int = 15) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        payload = ""
        data = None
        headers = None

        if body is not None:
            payload = json.dumps(body, separators=(",", ":"))
            data = payload
            headers = self._headers(payload)
        else:
            headers = self._headers("")

        for attempt in range(5):
            try:
                r = self._session.request(method, url, params=params, data=data, headers=headers, timeout=timeout)

                # Bybit rate-limit / transient issues
                if r.status_code in (429, 502, 503, 504):
                    wait = min(8.0, 0.75 * (attempt + 1))
                    time.sleep(wait)
                    continue

                r.raise_for_status()
                js = r.json()

                # Bybit returns retCode
                ret = js.get("retCode")
                if ret not in (0, "0", None):
                    # some non-0 can still be “ok-ish” but generally treat as error
                    raise RuntimeError(f"Bybit retCode={ret} retMsg={js.get('retMsg')} result={js.get('result')}")

                return js

            except requests.RequestException as e:
                if attempt == 4:
                    raise
                time.sleep(min(6.0, 0.75 * (attempt + 1)))

        raise RuntimeError("HTTP request failed after retries")

    # ---------- Market data ----------
    def last_price(self, category: str, symbol: str) -> float:
        js = self._request("GET", "/v5/market/tickers", params={"category": category, "symbol": symbol}, timeout=10)
        lst = (js.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("No ticker data")
        return float(lst[0]["lastPrice"])

    # ---------- Orders ----------
    def place_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/v5/order/create", body=body, timeout=15)

    def cancel_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/v5/order/cancel", body=body, timeout=15)

    def open_orders(self, category: str, symbol: str) -> List[Dict[str, Any]]:
        js = self._request("GET", "/v5/order/realtime", params={"category": category, "symbol": symbol}, timeout=15)
        return ((js.get("result") or {}).get("list") or [])

    # ---------- Positions ----------
    def positions(self, category: str, symbol: str) -> List[Dict[str, Any]]:
        js = self._request("GET", "/v5/position/list", params={"category": category, "symbol": symbol}, timeout=15)
        return ((js.get("result") or {}).get("list") or [])

    # ---------- Trading stop (SL/TP/BE/Trailing) ----------
    def set_trading_stop(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        /v5/position/trading-stop
        Use for:
          - stopLoss / takeProfit
          - slTriggerBy / tpTriggerBy
          - tpslMode
        """
        return self._request("POST", "/v5/position/trading-stop", body=body, timeout=15)

    def set_trailing_stop(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Also /v5/position/trading-stop.
        Important:
          trailingStop is ABSOLUTE distance (price units), not percent.
        Example:
          {"category":"linear","symbol":"BTCUSDT","positionIdx":0,"tpslMode":"Full","trailingStop":"150.0"}
        """
        return self._request("POST", "/v5/position/trading-stop", body=body, timeout=15)

    # ---------- WebSocket (private executions/orders) ----------
    def run_private_ws(
        self,
        on_execution: Callable[[Dict[str, Any]], None],
        on_order: Optional[Callable[[Dict[str, Any]], None]] = None,
        reconnect: bool = True,
        reconnect_delay: float = 2.0,
    ):
        """
        Subscribes to:
          - execution
          - order

        reconnect=True => keeps trying forever on disconnect.
        """

        def _make_ws():
            expires = int(time.time() * 1000) + 10_000
            sign_payload = f"GET/realtime{expires}"
            sig = hmac.new(self.api_secret, sign_payload.encode(), hashlib.sha256).hexdigest()

            def _on_open(ws):
                ws.send(json.dumps({"op": "auth", "args": [self.api_key, expires, sig]}))
                ws.send(json.dumps({"op": "subscribe", "args": ["execution", "order"]}))

            def _on_message(ws, message):
                try:
                    msg = json.loads(message)
                except:
                    return

                topic = msg.get("topic", "")
                data = msg.get("data")

                if topic.startswith("execution") and data:
                    for ev in (data if isinstance(data, list) else [data]):
                        try:
                            on_execution(ev)
                        except Exception:
                            pass

                if topic.startswith("order") and data and on_order:
                    for ev in (data if isinstance(data, list) else [data]):
                        try:
                            on_order(ev)
                        except Exception:
                            pass

            def _on_error(ws, error):
                # optional log
                # print("WS error:", error)
                pass

            def _on_close(ws, code, msg):
                # optional log
                # print("WS closed:", code, msg)
                pass

            return WebSocketApp(
                self.ws,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )

        while True:
            ws = _make_ws()
            ws.run_forever(ping_interval=20, ping_timeout=10)

            if not reconnect:
                break

            time.sleep(reconnect_delay)
