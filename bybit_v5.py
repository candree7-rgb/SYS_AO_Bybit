import time, hmac, hashlib, json
import requests
from typing import Any, Dict, Optional, List
from websocket import WebSocketApp

class BybitV5:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self.ws   = "wss://stream-testnet.bybit.com/v5/private" if testnet else "wss://stream.bybit.com/v5/private"

    def _sign(self, ts: str, recv_window: str, payload: str) -> str:
        msg = ts + self.api_key + recv_window + payload
        return hmac.new(self.api_secret, msg.encode(), hashlib.sha256).hexdigest()

    def _headers(self, payload: str, recv_window: str = "5000") -> Dict[str,str]:
        ts = str(int(time.time() * 1000))
        sign = self._sign(ts, recv_window, payload)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json"
        }

    # ---------- Market data ----------
    def last_price(self, category: str, symbol: str) -> float:
        r = requests.get(f"{self.base}/v5/market/tickers", params={"category": category, "symbol": symbol}, timeout=10)
        r.raise_for_status()
        data = r.json()
        lst = (data.get("result") or {}).get("list") or []
        if not lst: raise RuntimeError("No ticker data")
        return float(lst[0]["lastPrice"])

    # ---------- Orders ----------
    def place_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",",":"))
        r = requests.post(f"{self.base}/v5/order/create", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",",":"))
        r = requests.post(f"{self.base}/v5/order/cancel", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def open_orders(self, category: str, symbol: str) -> List[Dict[str,Any]]:
        r = requests.get(f"{self.base}/v5/order/realtime",
                         headers=self._headers(""),
                         params={"category": category, "symbol": symbol}, timeout=15)
        r.raise_for_status()
        return ((r.json().get("result") or {}).get("list") or [])

    # ---------- Positions ----------
    def positions(self, category: str, symbol: str) -> List[Dict[str,Any]]:
        r = requests.get(f"{self.base}/v5/position/list",
                         headers=self._headers(""),
                         params={"category": category, "symbol": symbol}, timeout=15)
        r.raise_for_status()
        return ((r.json().get("result") or {}).get("list") or [])

    def set_trading_stop(self, body: Dict[str,Any]) -> Dict[str,Any]:
        payload = json.dumps(body, separators=(",",":"))
        r = requests.post(f"{self.base}/v5/position/trading-stop", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    # ---------- WebSocket (private executions) ----------
    def run_private_ws(self, on_execution, on_order=None):
        # Auth flow: op=auth args=[api_key, expires, signature]
        expires = int(time.time() * 1000) + 10_000
        sign_payload = f"GET/realtime{expires}"
        sig = hmac.new(self.api_secret, sign_payload.encode(), hashlib.sha256).hexdigest()

        def _on_open(ws):
            ws.send(json.dumps({"op":"auth","args":[self.api_key, expires, sig]}))
            # executions + orders
            ws.send(json.dumps({"op":"subscribe","args":["execution","order"]}))

        def _on_message(ws, message):
            try:
                msg = json.loads(message)
            except:
                return
            topic = msg.get("topic","")
            data  = msg.get("data")
            if topic.startswith("execution") and data:
                for ev in (data if isinstance(data, list) else [data]):
                    on_execution(ev)
            if topic.startswith("order") and data and on_order:
                for ev in (data if isinstance(data, list) else [data]):
                    on_order(ev)

        ws = WebSocketApp(self.ws, on_open=_on_open, on_message=_on_message)
        ws.run_forever(ping_interval=20, ping_timeout=10)
