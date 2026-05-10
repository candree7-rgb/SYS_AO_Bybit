"""Binance USDT-M Futures client with a Bybit-V5-compatible interface.

Drop-in replacement for `bybit_v5.BybitV5`. Translates Bybit-style call
shapes (orderLinkId, retCode, category="linear", inline stopLoss, etc.)
to Binance Futures REST + User-Data-Stream WS, and translates Binance
events back to Bybit shapes for the existing `trade_engine.py`.

Key translations
----------------
* `category` argument → ignored (Binance Futures USDT-M is implicit linear)
* `account_type` argument → ignored (Binance has unified margin per asset)
* `orderLinkId` ↔ `clientOrderId`: Bybit uses `|` and `:`; Binance allows
  alphanumerics + `.`, `_`, `-` only. Pipes are encoded `|` → `_`,
  colons `:` → `-`. Reversible because trade_ids never contain `_`/`-`.
* Inline `stopLoss` on entry → `batchOrders` with [entry, STOP_MARKET
  closePosition=true]. The SL's clientOrderId gets the `_SL` suffix and
  is tracked in `_sl_orders[symbol]` so `set_trading_stop` can replace it
  later.
* `set_trading_stop` (Bybit position-level SL) → cancel old SL order +
  place new STOP_MARKET closePosition=true.
* `closed_pnl` → fapi `/income` endpoint filtered to REALIZED_PNL.
"""
from __future__ import annotations

import time
import hmac
import hashlib
import json
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from websocket import WebSocketApp


# --------------------------------------------------------------------------- #
# orderLinkId ↔ clientOrderId codec                                          #
# --------------------------------------------------------------------------- #
def _encode_link_id(link_id: str) -> str:
    """Bybit `|`/`:` → Binance `_`/`-`. Truncate to 36 chars (Binance limit)."""
    cid = link_id.replace("|", "_").replace(":", "-")
    return cid[:36]


def _decode_client_id(client_id: str) -> str:
    """Reverse of _encode_link_id. Trade-ids never contain `_` or `-`."""
    return client_id.replace("-", ":").replace("_", "|")


# --------------------------------------------------------------------------- #
# Filter / qty / price helpers                                                #
# --------------------------------------------------------------------------- #
def _strip_trailing_zeros(s: str) -> str:
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".") or "0"


def _fmt_num(x: float | str) -> str:
    """Binance rejects scientific notation; format plain. Strip trailing zeros."""
    s = f"{float(x):.10f}"
    return _strip_trailing_zeros(s)


# --------------------------------------------------------------------------- #
class BinanceFutures:
    """Binance USDT-M Futures wrapper exposing the Bybit V5 method shape."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        demo: bool = False,
        recv_window: str | int = "5000",
    ):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.recv_window = int(recv_window)

        # Binance has no separate "demo" mode — fall back to testnet.
        if testnet or demo:
            self.base = "https://testnet.binancefuture.com"
            self.ws_user = "wss://stream.binancefuture.com/ws"
            self.ws_public = "wss://stream.binancefuture.com/ws"
        else:
            self.base = "https://fapi.binance.com"
            self.ws_user = "wss://fstream.binance.com/ws"
            self.ws_public = "wss://fstream.binance.com/ws"

        # Connection-pooled HTTP session (saves 30-80ms vs cold).
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._session.headers.update({"X-MBX-APIKEY": api_key})

        # ----- Caches (parity with bybit_v5.py) -----
        self._equity_cache: Dict[str, Tuple[float, float]] = {}
        self._equity_ttl = 60.0

        self._ws_equity: Dict[str, Tuple[float, float]] = {}
        self._ws_equity_lock = threading.Lock()
        self._ws_equity_max_age = 30.0

        self._ws_positions: Dict[str, Tuple[float, float, float]] = {}
        self._ws_pos_lock = threading.Lock()
        self._ws_pos_max_age = 10.0

        # exchangeInfo cache (the whole symbols list)
        self._exchange_info: Optional[Dict[str, Any]] = None
        self._exchange_info_ts: float = 0.0
        self._exchange_info_ttl = 3600.0  # 1h

        # Per-symbol max-leverage cache from /leverageBracket
        self._max_lev: Dict[str, int] = {}

        # Track the SL & TP orderIds so set_trading_stop can replace.
        # Maps symbol → orderId for the active SL on Binance.
        self._sl_orders: Dict[str, str] = {}
        self._sl_lock = threading.Lock()

        # listenKey state for User-Data-Stream
        self._listen_key: Optional[str] = None
        self._listen_key_ts: float = 0.0

    # ====================================================================== #
    # Signing                                                                 #
    # ====================================================================== #
    def _sign(self, query: str) -> str:
        return hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()

    def _signed_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window
        query = urlencode(params, doseq=True)
        sig = self._sign(query)
        url = f"{self.base}{path}?{query}&signature={sig}"
        r = self._session.request(method, url, timeout=timeout)
        # Binance returns JSON for both success and error. HTTP code is
        # often 200 even on logical error (e.g. "leverage not modified")
        # — defer to the JSON `code`/`msg` parsing.
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            return {}
        if isinstance(data, dict) and "code" in data and "msg" in data:
            # Binance error envelope. Some are ignorable (handled by callers).
            err_code = int(data.get("code", 0))
            if err_code < 0:
                raise BinanceAPIError(err_code, data.get("msg", ""), data)
        return data

    def _public_request(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 10
    ) -> Any:
        url = f"{self.base}{path}"
        r = self._session.request(method, url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # ====================================================================== #
    # exchangeInfo + filter helpers                                           #
    # ====================================================================== #
    def _get_exchange_info(self) -> Dict[str, Any]:
        now = time.time()
        if self._exchange_info and (now - self._exchange_info_ts) < self._exchange_info_ttl:
            return self._exchange_info
        info = self._public_request("GET", "/fapi/v1/exchangeInfo")
        self._exchange_info = info
        self._exchange_info_ts = now
        return info

    def _symbol_filters(self, symbol: str) -> Dict[str, Any]:
        info = self._get_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                return s
        raise RuntimeError(f"Symbol not found in exchangeInfo: {symbol}")

    # ====================================================================== #
    # Market data                                                             #
    # ====================================================================== #
    def last_price(self, category: str, symbol: str) -> float:  # noqa: ARG002
        data = self._public_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def instruments_info(self, category: str, symbol: str) -> Dict[str, Any]:  # noqa: ARG002
        """Return Bybit-shaped dict: priceFilter.tickSize, lotSizeFilter.qtyStep,
        lotSizeFilter.minOrderQty, leverageFilter.maxLeverage."""
        s = self._symbol_filters(symbol)
        tick_size = "0.0001"
        qty_step = "0.000001"
        min_qty = "0"
        for f in s.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                tick_size = f.get("tickSize", tick_size)
            elif f.get("filterType") == "LOT_SIZE":
                qty_step = f.get("stepSize", qty_step)
                min_qty = f.get("minQty", min_qty)
        max_lev = self._max_lev.get(symbol)
        if max_lev is None:
            try:
                max_lev = self._fetch_max_leverage(symbol)
            except Exception:
                max_lev = 100
            self._max_lev[symbol] = max_lev
        return {
            "symbol": symbol,
            "priceFilter": {"tickSize": tick_size},
            "lotSizeFilter": {
                "qtyStep": qty_step,
                "minOrderQty": min_qty,
            },
            "leverageFilter": {"maxLeverage": str(max_lev)},
        }

    def _fetch_max_leverage(self, symbol: str) -> int:
        data = self._signed_request(
            "GET", "/fapi/v1/leverageBracket", {"symbol": symbol}
        )
        # Returns a list with a single entry for the symbol
        if isinstance(data, list) and data:
            brackets = data[0].get("brackets", [])
            if brackets:
                return int(brackets[0].get("initialLeverage", 100))
        return 100

    # ====================================================================== #
    # Account                                                                 #
    # ====================================================================== #
    def get_cached_position(self, symbol: str):
        with self._ws_pos_lock:
            t = self._ws_positions.get(symbol)
        if t and (time.time() - t[2]) < self._ws_pos_max_age:
            return t[0], t[1]
        return None

    def wallet_equity(self, account_type: str = "USDT", force_refresh: bool = False) -> float:
        """Returns USDT margin balance (wallet + unrealized PnL).

        `account_type` is treated as the asset (default USDT). Bybit-style
        callers pass "UNIFIED" → translated to USDT.
        """
        asset = "USDT" if account_type.upper() in ("UNIFIED", "CONTRACT", "USDT") else account_type.upper()
        if not force_refresh:
            with self._ws_equity_lock:
                ws = self._ws_equity.get(asset)
            if ws and (time.time() - ws[1]) < self._ws_equity_max_age:
                return ws[0]
            cached = self._equity_cache.get(asset)
            if cached is not None and (time.time() - cached[1]) < self._equity_ttl:
                return cached[0]

        # /fapi/v2/account returns aggregated totals incl. unrealized PnL
        data = self._signed_request("GET", "/fapi/v2/account")
        total = data.get("totalMarginBalance") or data.get("totalWalletBalance") or "0"
        value = float(total)
        ts = time.time()
        self._equity_cache[asset] = (value, ts)
        return value

    def set_leverage(self, category: str, symbol: str, leverage) -> Dict[str, Any]:  # noqa: ARG002
        """Binance leverage must be int 1-125."""
        lev_int = int(leverage) if isinstance(leverage, (int, float)) else int(float(leverage))
        if lev_int < 1:
            lev_int = 1
        try:
            return self._signed_request(
                "POST",
                "/fapi/v1/leverage",
                {"symbol": symbol, "leverage": lev_int},
            )
        except BinanceAPIError as e:
            # No "already set" error code on Binance — every call works.
            # If the requested leverage exceeds tier max, Binance returns -4028.
            raise

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED") -> Dict[str, Any]:
        """Set ISOLATED or CROSSED. Idempotent: ignores -4046 'no need'."""
        try:
            return self._signed_request(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": mode.upper()},
            )
        except BinanceAPIError as e:
            if e.code == -4046:  # "No need to change margin type"
                return {"code": 0, "msg": "ok"}
            raise

    def set_position_mode_one_way(self) -> Dict[str, Any]:
        """Force One-Way mode account-wide. Idempotent."""
        try:
            return self._signed_request(
                "POST",
                "/fapi/v1/positionSide/dual",
                {"dualSidePosition": "false"},
            )
        except BinanceAPIError as e:
            if e.code == -4059:  # "No need to change position side"
                return {"code": 0, "msg": "ok"}
            raise

    # ====================================================================== #
    # Order placement (Bybit-shaped body → Binance translation)              #
    # ====================================================================== #
    def place_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Accept a Bybit-style body and translate to Binance.

        Recognised body fields:
        - symbol, side ("Buy"/"Sell"), orderType ("Limit"/"Market")
        - qty, price
        - timeInForce ("GTC"/"IOC"/"FOK")
        - reduceOnly, closeOnTrigger
        - orderLinkId
        - triggerPrice + triggerDirection + triggerBy → conditional STOP/TAKE_PROFIT
        - stopLoss + slTriggerBy + tpslMode → inline SL via batchOrders

        Returns Bybit-shaped: {retCode: 0, result: {orderId: "..."}}.
        """
        symbol = body["symbol"]
        side = "BUY" if body["side"] == "Buy" else "SELL"
        order_type_raw = body.get("orderType", "Limit").upper()  # LIMIT or MARKET
        qty = body["qty"]
        price = body.get("price")
        tif = body.get("timeInForce", "GTC").upper()
        reduce_only = bool(body.get("reduceOnly"))
        link_id = body.get("orderLinkId", "")
        trigger_price = body.get("triggerPrice")
        sl_inline = body.get("stopLoss")

        # Conditional (DCA): triggerPrice present → STOP / STOP_MARKET
        if trigger_price is not None and not sl_inline:
            return self._place_conditional_limit(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                stop_price=trigger_price,
                tif=tif,
                reduce_only=reduce_only,
                client_order_id=_encode_link_id(link_id),
            )

        # Inline SL on entry: use batchOrders so entry + SL submit in 1 RTT
        if sl_inline:
            return self._place_entry_with_sl(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                tif=tif,
                client_order_id=_encode_link_id(link_id),
                sl_price=sl_inline,
                order_type=order_type_raw,
            )

        # Plain LIMIT or MARKET (TPs, market closes, etc.)
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type_raw,
            "quantity": _fmt_num(qty),
            "newClientOrderId": _encode_link_id(link_id),
            "newOrderRespType": "RESULT",
        }
        if order_type_raw == "LIMIT":
            params["price"] = _fmt_num(price)
            params["timeInForce"] = tif
        if reduce_only:
            params["reduceOnly"] = "true"
        resp = self._signed_request("POST", "/fapi/v1/order", params)
        return _wrap_order_response(resp)

    def _place_conditional_limit(
        self,
        symbol: str,
        side: str,
        qty: Any,
        price: Any,
        stop_price: Any,
        tif: str,
        reduce_only: bool,
        client_order_id: str,
    ) -> Dict[str, Any]:
        """Bybit conditional-Limit → Binance STOP order."""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP",
            "quantity": _fmt_num(qty),
            "price": _fmt_num(price),
            "stopPrice": _fmt_num(stop_price),
            "timeInForce": tif,
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        resp = self._signed_request("POST", "/fapi/v1/order", params)
        return _wrap_order_response(resp)

    def _place_entry_with_sl(
        self,
        symbol: str,
        side: str,
        qty: Any,
        price: Any,
        tif: str,
        client_order_id: str,
        sl_price: Any,
        order_type: str,
    ) -> Dict[str, Any]:
        """Submit entry + SL_MARKET (closePosition) in one batchOrders call.

        SL closes the WHOLE position. Tracks SL orderId so set_trading_stop()
        can later cancel & replace it.
        """
        sl_side = "BUY" if side == "SELL" else "SELL"
        sl_cid = (client_order_id + "-SL")[:36]

        entry_order: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,  # LIMIT / MARKET
            "quantity": _fmt_num(qty),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if order_type == "LIMIT":
            entry_order["price"] = _fmt_num(price)
            entry_order["timeInForce"] = tif
        sl_order = {
            "symbol": symbol,
            "side": sl_side,
            "type": "STOP_MARKET",
            "stopPrice": _fmt_num(sl_price),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "newClientOrderId": sl_cid,
            "newOrderRespType": "RESULT",
        }
        params = {
            "batchOrders": json.dumps([entry_order, sl_order], separators=(",", ":")),
        }
        resp = self._signed_request("POST", "/fapi/v1/batchOrders", params)
        # Response is a list of order objects (or {code, msg} on error per slot)
        if not isinstance(resp, list) or len(resp) < 2:
            raise RuntimeError(f"Unexpected batchOrders response: {resp}")
        entry_resp, sl_resp = resp[0], resp[1]
        if isinstance(entry_resp, dict) and entry_resp.get("code", 0) and int(entry_resp.get("code", 0)) < 0:
            # Entry failed — try to cancel the SL if it succeeded
            if isinstance(sl_resp, dict) and sl_resp.get("orderId"):
                try:
                    self._cancel_by_order_id(symbol, sl_resp["orderId"])
                except Exception:
                    pass
            raise BinanceAPIError(
                int(entry_resp["code"]), entry_resp.get("msg", ""), entry_resp
            )
        if isinstance(sl_resp, dict) and sl_resp.get("code", 0) and int(sl_resp.get("code", 0)) < 0:
            # Entry placed, SL failed → keep entry but log; caller's
            # set_trading_stop fallback in trade_engine will retry SL.
            # Don't cancel entry: the bot's safety net will set SL after fill.
            pass
        else:
            with self._sl_lock:
                self._sl_orders[symbol] = str(sl_resp.get("orderId"))
        return _wrap_order_response(entry_resp)

    def cancel_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        symbol = body["symbol"]
        oid = body.get("orderId")
        link_id = body.get("orderLinkId")
        params: Dict[str, Any] = {"symbol": symbol}
        if oid:
            params["orderId"] = oid
        elif link_id:
            params["origClientOrderId"] = _encode_link_id(link_id)
        else:
            raise RuntimeError("cancel_order requires orderId or orderLinkId")
        try:
            resp = self._signed_request("DELETE", "/fapi/v1/order", params)
            return {"retCode": 0, "result": resp}
        except BinanceAPIError as e:
            # -2011 "Unknown order sent" → treat as already-cancelled (idempotent)
            if e.code == -2011:
                return {"retCode": 0, "result": {"alreadyClosed": True}}
            raise

    def _cancel_by_order_id(self, symbol: str, order_id: str) -> None:
        try:
            self._signed_request(
                "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
            )
        except BinanceAPIError:
            pass

    def open_orders(self, category: str, symbol: str) -> List[Dict[str, Any]]:  # noqa: ARG002
        """Return list of open orders in Bybit shape (orderId, orderLinkId, ...)."""
        data = self._signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol})
        return [_to_bybit_order_dict(o) for o in (data if isinstance(data, list) else [])]

    def order_history(
        self,
        category: str,  # noqa: ARG002
        symbol: str,
        order_link_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"symbol": symbol, "limit": min(limit, 1000)}
        # Binance allOrders does not support filtering by clientOrderId directly;
        # we filter client-side after fetching.
        data = self._signed_request("GET", "/fapi/v1/allOrders", params)
        out = [_to_bybit_order_dict(o) for o in (data if isinstance(data, list) else [])]
        if order_link_id:
            target = _encode_link_id(order_link_id)
            out = [o for o in out if o.get("orderLinkId") == _decode_client_id(target)]
        return out

    # ====================================================================== #
    # Positions                                                               #
    # ====================================================================== #
    def positions(self, category: str, symbol: str = "") -> List[Dict[str, Any]]:  # noqa: ARG002
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = self._signed_request("GET", "/fapi/v2/positionRisk", params)
        out: List[Dict[str, Any]] = []
        for p in data if isinstance(data, list) else []:
            out.append(_position_to_bybit(p))
        return out

    # ====================================================================== #
    # SL/TP at position level (Bybit set_trading_stop equivalent)            #
    # ====================================================================== #
    def set_trading_stop(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Replace the active SL order for a symbol (Binance has no
        position-level SL — emulate by cancel-and-replace STOP_MARKET).

        Recognised fields: symbol, stopLoss, takeProfit (optional),
        trailingStop (optional), activePrice (optional).
        """
        symbol = body["symbol"]
        new_sl = body.get("stopLoss")
        trailing = body.get("trailingStop")
        # Active TP at position-level is rare in our flow; ignore for now.

        # 1. Cancel existing SL order (tracked by us, falls back to scanning
        #    open orders for an SL-tagged clientOrderId).
        with self._sl_lock:
            old_sl = self._sl_orders.get(symbol)
        if old_sl:
            self._cancel_by_order_id(symbol, old_sl)
            with self._sl_lock:
                self._sl_orders.pop(symbol, None)
        else:
            # Scan open orders for any *-SL clientOrderId we may have lost
            try:
                for o in self.open_orders("linear", symbol):
                    cid = (o.get("orderLinkId") or "").split(":")
                    # match Bybit-style "trade_id:SL" suffix only — leave others
                    if len(cid) == 2 and cid[1] == "SL":
                        self._cancel_by_order_id(symbol, str(o.get("orderId", "")))
                        break
            except Exception:
                pass

        if not new_sl and not trailing:
            return {"retCode": 0, "result": {}}

        # 2. Determine SL side from current position
        side_close = self._closing_side(symbol)
        if side_close is None:
            # No position → nothing to protect; return ok.
            return {"retCode": 0, "result": {"noPosition": True}}

        cid = f"sl-{symbol}-{int(time.time()*1000)}"[:36]

        if trailing and not new_sl:
            params = {
                "symbol": symbol,
                "side": side_close,
                "type": "TRAILING_STOP_MARKET",
                "callbackRate": _fmt_num(trailing),
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "newClientOrderId": cid,
                "newOrderRespType": "RESULT",
            }
            if body.get("activePrice"):
                params["activationPrice"] = _fmt_num(body["activePrice"])
        else:
            params = {
                "symbol": symbol,
                "side": side_close,
                "type": "STOP_MARKET",
                "stopPrice": _fmt_num(new_sl),
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "priceProtect": "true",
                "newClientOrderId": cid,
                "newOrderRespType": "RESULT",
            }
        resp = self._signed_request("POST", "/fapi/v1/order", params)
        with self._sl_lock:
            self._sl_orders[symbol] = str(resp.get("orderId"))
        return {"retCode": 0, "result": resp}

    def _closing_side(self, symbol: str) -> Optional[str]:
        """Return the order side that would CLOSE the open position on this
        symbol (BUY for SHORT, SELL for LONG). None if no position open."""
        # Prefer cached
        cached = self.get_cached_position(symbol)
        if cached is not None:
            size, _ = cached
            if size == 0:
                # WS-cached zero may be stale; fall through to REST
                pass
            else:
                return None  # cache stores size+avg, not side; fall through
        try:
            poss = self.positions("linear", symbol)
        except Exception:
            return None
        for p in poss:
            if p.get("symbol") == symbol and float(p.get("size", 0)) > 0:
                return "BUY" if p.get("side") == "Sell" else "SELL"
        return None

    # ====================================================================== #
    # Closed PnL                                                              #
    # ====================================================================== #
    def closed_pnl(
        self,
        category: str,  # noqa: ARG002
        symbol: str,
        start_time: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Map Binance income (REALIZED_PNL) records to Bybit closed_pnl shape."""
        params: Dict[str, Any] = {
            "symbol": symbol,
            "incomeType": "REALIZED_PNL",
            "limit": min(limit, 1000),
        }
        if start_time:
            params["startTime"] = int(start_time)
        data = self._signed_request("GET", "/fapi/v1/income", params)
        out: List[Dict[str, Any]] = []
        for ev in data if isinstance(data, list) else []:
            out.append({
                "symbol": ev.get("symbol"),
                "closedPnl": str(ev.get("income", "0")),
                "createdTime": str(ev.get("time", "")),
                "side": "",
            })
        return out

    # ====================================================================== #
    # WebSocket: User-Data Stream                                             #
    # ====================================================================== #
    def _start_listen_key(self) -> str:
        data = self._signed_request("POST", "/fapi/v1/listenKey", {})
        # Some libraries return {"listenKey": "..."}. /fapi returns plain object.
        key = data.get("listenKey") if isinstance(data, dict) else None
        if not key:
            raise RuntimeError(f"Failed to obtain listenKey: {data}")
        self._listen_key = key
        self._listen_key_ts = time.time()
        return key

    def _keepalive_listen_key(self) -> None:
        try:
            self._signed_request("PUT", "/fapi/v1/listenKey", {})
            self._listen_key_ts = time.time()
        except Exception:
            pass

    def run_private_ws(
        self,
        on_execution,
        on_order=None,
        on_wallet=None,
        on_position=None,
        on_error=None,
        account_type: str = "USDT",  # noqa: ARG002
    ):
        """Open Binance User-Data Stream, translate events to Bybit shape."""
        listen_key = self._start_listen_key()
        url = f"{self.ws_user}/{listen_key}"

        # Background keepalive thread (every 30 min)
        stop_evt = threading.Event()

        def _keepalive_loop():
            while not stop_evt.is_set():
                if stop_evt.wait(1800):
                    return
                self._keepalive_listen_key()

        ka_thread = threading.Thread(target=_keepalive_loop, daemon=True)
        ka_thread.start()

        def _on_message(ws, message):
            try:
                msg = json.loads(message)
            except Exception:
                return
            ev_type = msg.get("e")
            if ev_type == "ORDER_TRADE_UPDATE":
                self._handle_order_update(msg.get("o") or {}, on_execution, on_order)
            elif ev_type == "ACCOUNT_UPDATE":
                self._handle_account_update(msg.get("a") or {}, on_wallet, on_position)
            elif ev_type == "listenKeyExpired":
                # Force reconnect via on_error
                if on_error:
                    on_error(RuntimeError("listenKey expired"))

        def _on_err(ws, err):
            stop_evt.set()
            if on_error:
                on_error(err)

        def _on_close(ws, code, reason):
            stop_evt.set()

        ws = WebSocketApp(
            url,
            on_message=_on_message,
            on_error=_on_err,
            on_close=_on_close,
        )
        ws.run_forever(ping_interval=180, ping_timeout=10)

    def _handle_order_update(self, o: Dict[str, Any], on_execution, on_order):
        """Convert ORDER_TRADE_UPDATE.o → Bybit-shape execution event."""
        x = o.get("x")  # executionType: NEW|TRADE|CANCELED|EXPIRED|...
        cid = o.get("c") or ""
        link_id = _decode_client_id(cid)
        ev = {
            "orderLinkId": link_id,
            "orderId": str(o.get("i", "")),
            "symbol": o.get("s"),
            "side": "Buy" if (o.get("S") == "BUY") else "Sell",
            "execPrice": str(o.get("L") or o.get("ap") or "0"),
            "lastPrice": str(o.get("L") or "0"),
            "price": str(o.get("p") or "0"),
            "execQty": str(o.get("l") or "0"),
            "cumExecQty": str(o.get("z") or "0"),
            "avgPrice": str(o.get("ap") or "0"),
            "execStatus": o.get("X"),
            "orderStatus": o.get("X"),
            "execType": x,
        }
        if x == "TRADE":
            on_execution(ev)
            # When a TP/SL/entry fully fills and was tracked as the SL order,
            # forget the SL pointer (so the next set_trading_stop doesn't try
            # to cancel a non-existent order).
            if cid.endswith("-SL") or cid.endswith(":SL"):
                with self._sl_lock:
                    self._sl_orders.pop(o.get("s"), None)
        if on_order:
            try:
                on_order(ev)
            except Exception:
                pass

    def _handle_account_update(self, a: Dict[str, Any], on_wallet, on_position):
        """ACCOUNT_UPDATE.a contains B (balances) and P (positions)."""
        for b in a.get("B", []):
            asset = b.get("a")
            try:
                wb = float(b.get("wb", "0") or 0)  # wallet balance
                cw = float(b.get("cw", "0") or 0)  # cross wallet
            except (TypeError, ValueError):
                continue
            value = wb if wb > 0 else cw
            if asset:
                ts = time.time()
                with self._ws_equity_lock:
                    self._ws_equity[asset] = (value, ts)
                self._equity_cache[asset] = (value, ts)
                if on_wallet:
                    try:
                        on_wallet({"accountType": asset, "totalEquity": value})
                    except Exception:
                        pass

        for p in a.get("P", []):
            sym = p.get("s")
            if not sym:
                continue
            try:
                pa = float(p.get("pa") or 0)  # position amount (signed)
                ep = float(p.get("ep") or 0)  # entry price
            except (TypeError, ValueError):
                continue
            size = abs(pa)
            with self._ws_pos_lock:
                self._ws_positions[sym] = (size, ep, time.time())
            if on_position:
                try:
                    on_position({
                        "symbol": sym,
                        "size": size,
                        "avgPrice": ep,
                        "side": "Sell" if pa < 0 else ("Buy" if pa > 0 else ""),
                    })
                except Exception:
                    pass

    # ====================================================================== #
    # WebSocket: public market streams (used by entry_watcher)                #
    # ====================================================================== #
    def run_public_ws(self, on_open, on_message_raw, on_error=None):
        """Public combined-stream WS. Caller handles subscribe via on_open(ws).

        Caller subscribes by sending: {"method": "SUBSCRIBE",
        "params": ["btcusdt@bookTicker"], "id": 1}.
        Each message comes as Binance native JSON (no envelope).
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

        ws = WebSocketApp(
            self.ws_public,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_err,
        )
        ws.run_forever(ping_interval=180, ping_timeout=10)


# --------------------------------------------------------------------------- #
# Helpers — response shape conversion                                         #
# --------------------------------------------------------------------------- #
class BinanceAPIError(RuntimeError):
    def __init__(self, code: int, msg: str, raw: Any = None):
        self.code = code
        self.msg = msg
        self.raw = raw
        super().__init__(f"Binance error {code}: {msg}")


def _wrap_order_response(resp: Any) -> Dict[str, Any]:
    """Convert a Binance order response → Bybit-style envelope."""
    if not isinstance(resp, dict):
        return {"retCode": 0, "result": {}}
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "orderId": str(resp.get("orderId", "")),
            "orderLinkId": _decode_client_id(resp.get("clientOrderId", "")),
        },
    }


def _to_bybit_order_dict(o: Dict[str, Any]) -> Dict[str, Any]:
    """Map Binance order JSON → Bybit-shape order dict."""
    return {
        "orderId": str(o.get("orderId", "")),
        "orderLinkId": _decode_client_id(o.get("clientOrderId", "")),
        "symbol": o.get("symbol"),
        "side": "Buy" if o.get("side") == "BUY" else "Sell",
        "orderType": (o.get("type") or "").title(),
        "price": str(o.get("price", "0")),
        "qty": str(o.get("origQty", "0")),
        "leavesQty": str(float(o.get("origQty", 0)) - float(o.get("executedQty", 0))),
        "cumExecQty": str(o.get("executedQty", "0")),
        "orderStatus": o.get("status"),
        "stopOrderType": _stop_order_type(o.get("type")),
        "triggerPrice": str(o.get("stopPrice", "0")),
        "reduceOnly": bool(o.get("reduceOnly")),
        "closeOnTrigger": bool(o.get("closePosition")),
    }


def _stop_order_type(binance_type: Optional[str]) -> str:
    if not binance_type:
        return ""
    if binance_type in ("STOP", "STOP_MARKET"):
        return "Stop"
    if binance_type in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
        return "TakeProfit"
    if binance_type == "TRAILING_STOP_MARKET":
        return "TrailingStop"
    return ""


def _position_to_bybit(p: Dict[str, Any]) -> Dict[str, Any]:
    pa = float(p.get("positionAmt", "0") or 0)
    ep = float(p.get("entryPrice", "0") or 0)
    upnl = float(p.get("unRealizedProfit", "0") or 0)
    side = "Sell" if pa < 0 else ("Buy" if pa > 0 else "")
    return {
        "symbol": p.get("symbol"),
        "size": abs(pa),
        "avgPrice": ep,
        "side": side,
        "unrealisedPnl": upnl,
        "positionIdx": 0,
    }
