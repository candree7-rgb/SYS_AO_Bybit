import time
import hmac
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
import requests
from websocket import WebSocketApp

class BybitV5:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, recv_window: str = "5000"):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.recv_window = str(recv_window)
        self.base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self.ws   = "wss://stream-testnet.bybit.com/v5/private" if testnet else "wss://stream.bybit.com/v5/private"

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

    def _check(self, data: Dict[str, Any]) -> Dict[str, Any]:
        # Bybit returns retCode/retMsg
        if isinstance(data, dict) and data.get("retCode", 0) not in (0, "0"):
            raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')} | {data}")
        return data

    # ---------- Market data ----------
    def last_price(self, category: str, symbol: str) -> float:
        r = requests.get(f"{self.base}/v5/market/tickers", params={"category": category, "symbol": symbol}, timeout=10)
        r.raise_for_status()
        data = self._check(r.json())
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("No ticker data")
        return float(lst[0]["lastPrice"])

    def instruments_info(self, category: str, symbol: str) -> Dict[str, Any]:
        r = requests.get(f"{self.base}/v5/market/instruments-info", params={"category": category, "symbol": symbol}, timeout=10)
        r.raise_for_status()
        data = self._check(r.json())
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("No instrument info")
        return lst[0]

    # ---------- Account ----------
    def wallet_equity(self, account_type: str = "UNIFIED") -> float:
        r = requests.get(
            f"{self.base}/v5/account/wallet-balance",
            headers=self._headers(""),
            params={"accountType": account_type},
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
        return float(val)

    def set_leverage(self, category: str, symbol: str, leverage: int) -> Dict[str, Any]:
        body = {
            "category": category,
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        payload = json.dumps(body, separators=(",", ":"))
        r = requests.post(f"{self.base}/v5/position/set-leverage", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return self._check(r.json())

    # ---------- Orders ----------
    def place_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        r = requests.post(f"{self.base}/v5/order/create", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return self._check(r.json())

    def cancel_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        r = requests.post(f"{self.base}/v5/order/cancel", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return self._check(r.json())

    def open_orders(self, category: str, symbol: str) -> List[Dict[str, Any]]:
        r = requests.get(
            f"{self.base}/v5/order/realtime",
            headers=self._headers(""),
            params={"category": category, "symbol": symbol},
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        return ((data.get("result") or {}).get("list") or [])

    def order_history(self, category: str, symbol: str, order_link_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        params = {"category": category, "symbol": symbol, "limit": limit}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        r = requests.get(
            f"{self.base}/v5/order/history",
            headers=self._headers(""),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        return ((data.get("result") or {}).get("list") or [])

    # ---------- Positions ----------
    def positions(self, category: str, symbol: str) -> List[Dict[str, Any]]:
        r = requests.get(
            f"{self.base}/v5/position/list",
            headers=self._headers(""),
            params={"category": category, "symbol": symbol},
            timeout=15,
        )
        r.raise_for_status()
        data = self._check(r.json())
        return ((data.get("result") or {}).get("list") or [])

    def set_trading_stop(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        r = requests.post(f"{self.base}/v5/position/trading-stop", headers=self._headers(payload), data=payload, timeout=15)
        r.raise_for_status()
        return self._check(r.json())

    # ---------- WebSocket (private executions & orders) ----------
    def run_private_ws(self, on_execution, on_order=None, on_error=None):
        expires = int(time.time() * 1000) + 10_000
        sign_payload = f"GET/realtime{expires}"
        sig = hmac.new(self.api_secret, sign_payload.encode(), hashlib.sha256).hexdigest()

        def _on_open(ws):
            ws.send(json.dumps({"op": "auth", "args": [self.api_key, expires, sig]}))
            ws.send(json.dumps({"op": "subscribe", "args": ["execution", "order"]}))

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

        def _on_err(ws, err):
            if on_error:
                on_error(err)

        ws = WebSocketApp(self.ws, on_open=_on_open, on_message=_on_message, on_error=_on_err)
        ws.run_forever(ping_interval=20, ping_timeout=10)
