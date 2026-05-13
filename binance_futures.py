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

import logging
import os
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

# Module-level logger. Used for telemetry that doesn't have a caller-supplied
# logger (e.g. WS-event handlers that fire on the WS thread). Falls back to
# root logger config from main.py setup_logging().
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# orderLinkId ↔ clientOrderId codec                                          #
# --------------------------------------------------------------------------- #
def _encode_link_id(link_id: str) -> str:
    """Bybit `|`/`:` → Binance `_`/`-`. Truncate to 36 chars (Binance limit).

    When the input exceeds 36 chars (typical for long-symbol trade-ids
    like ``1000PEPEUSDT|sell|1762847234567:TRAIL`` = 37 chars), naive
    right-truncation would chop the suffix character that _handle_order_
    update relies on to detect SL/TRAIL fires. So if there's a short
    trailing ``-SUFFIX`` we keep it intact and truncate the trade-id
    portion from the front of the suffix instead.
    """
    cid = link_id.replace("|", "_").replace(":", "-")
    if len(cid) <= 36:
        return cid
    idx = cid.rfind("-")
    if idx > 0 and len(cid) - idx <= 10:
        suffix = cid[idx:]
        prefix = cid[:idx]
        prefix = prefix[: 36 - len(suffix)]
        return prefix + suffix
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


def _decimals_for_step(step: str) -> int:
    """Number of fractional digits implied by a step/tick string like '0.001'."""
    if "." not in step:
        return 0
    frac = step.split(".", 1)[1].rstrip("0")
    return len(frac)


def _quantize_to_step(value: float, step: str) -> str:
    """Round `value` DOWN to a multiple of `step` and format with the
    exact decimal-precision Binance expects. Prevents -1111 / -1013
    rejections from mis-aligned quantities or prices."""
    try:
        s = float(step)
    except (TypeError, ValueError):
        return _fmt_num(value)
    if s <= 0:
        return _fmt_num(value)
    import math as _math
    n = _math.floor(float(value) / s) * s
    decimals = _decimals_for_step(step)
    if decimals == 0:
        return str(int(round(n)))
    return f"{n:.{decimals}f}"


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

        # symbol -> (size, avg_price, side, ts). Side stored as "Buy"/"Sell"
        # so set_trading_stop can derive the closing-side without a REST call.
        self._ws_positions: Dict[str, Tuple[float, float, str, float]] = {}
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

        # Tracks order ids that live on /fapi/v1/algoOrder (algoId values
        # are LONG ints just like orderIds, but cancel/query needs a different
        # endpoint). Used by _cancel_by_order_id to route correctly.
        # Per Binance 2025-12-09 migration: TRAILING_STOP_MARKET and
        # STOP_MARKET conditional types are now placed via the algo endpoint.
        self._algo_orders: set = set()
        self._algo_lock = threading.Lock()

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

    def compute_rsi_1m(
        self, symbol: str, period: int = 14, lookback: int = 30, timeout: int = 2
    ) -> Optional[float]:
        """Fetch the last `lookback` 1m candles and return Wilder-smoothed RSI.

        Used pre-place-order by fast_signal_handler when RSI_FILTER_MAX_1M
        is set. Returns None if too few candles are available so the caller
        can fail-open (don't filter out a signal just because the kline
        endpoint hiccuped). Cost: one public GET, ~50ms RTT.

        `timeout` defaults to 2s (not the standard 10s) because this runs
        in the hot signal-handling path — a hung request would cost us the
        fill, which is worse than skipping the filter for one signal.
        """
        data = self._public_request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1m", "limit": lookback},
            timeout=timeout,
        )
        if not isinstance(data, list) or len(data) < period + 1:
            return None
        try:
            closes = [float(k[4]) for k in data]
        except (IndexError, ValueError, TypeError):
            return None
        # Wilder RSI: simple-average for first `period`, then exponential
        gains = []
        losses = []
        for i in range(1, period + 1):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        for i in range(period + 1, len(closes)):
            d = closes[i] - closes[i - 1]
            avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
            avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - 100.0 / (1.0 + rs)

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
        # Defensive: scan ALL brackets for the highest initialLeverage. The
        # response is typically sorted with bracket 1 = highest leverage,
        # but some symbols have caps that don't match bracket-1 default.
        if isinstance(data, list) and data:
            brackets = data[0].get("brackets", [])
            if brackets:
                cands = []
                for b in brackets:
                    try:
                        cands.append(int(b.get("initialLeverage", 0)))
                    except (TypeError, ValueError):
                        pass
                if cands:
                    return max(cands)
        return 20  # conservative fallback (was 100 — caused -4028 surprises)

    # ====================================================================== #
    # Account                                                                 #
    # ====================================================================== #
    def get_cached_position(self, symbol: str):
        """Returns (size, avg_price) or None if cache is empty/stale.
        The full 4-tuple (size, avg, side, ts) is internal."""
        with self._ws_pos_lock:
            t = self._ws_positions.get(symbol)
        if t and (time.time() - t[3]) < self._ws_pos_max_age:
            return t[0], t[1]
        return None

    def get_cached_position_full(self, symbol: str):
        """Returns (size, avg_price, side) or None. Side is 'Buy'/'Sell'/''."""
        with self._ws_pos_lock:
            t = self._ws_positions.get(symbol)
        if t and (time.time() - t[3]) < self._ws_pos_max_age:
            return t[0], t[1], t[2]
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
    # Filter helpers (used to defensively re-quantize qty/price)             #
    # ====================================================================== #
    def _filter_steps(self, symbol: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (tick_size, step_size) string-formatted for the symbol,
        or (None, None) if exchangeInfo is unreachable. Does not throw."""
        try:
            s = self._symbol_filters(symbol)
        except Exception:
            return (None, None)
        tick = None
        step = None
        for f in s.get("filters", []):
            ft = f.get("filterType")
            if ft == "PRICE_FILTER":
                tick = f.get("tickSize")
            elif ft == "LOT_SIZE":
                step = f.get("stepSize")
        return (tick, step)

    def _qty_str(self, symbol: str, qty: Any) -> str:
        _tick, step = self._filter_steps(symbol)
        if step is None:
            return _fmt_num(qty)
        return _quantize_to_step(float(qty), step)

    def _price_str(self, symbol: str, price: Any) -> str:
        tick, _step = self._filter_steps(symbol)
        if tick is None:
            return _fmt_num(price)
        return _quantize_to_step(float(price), tick)

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
        qty = body["qty"] if "qty" in body else None
        price = body.get("price")
        tif = body.get("timeInForce", "GTC").upper()
        reduce_only = bool(body.get("reduceOnly"))
        link_id = body.get("orderLinkId", "")
        trigger_price = body.get("triggerPrice")
        sl_inline = body.get("stopLoss")
        trailing = body.get("trailingStop")

        # Trailing-stop standalone — used by trade_engine's
        # place_post_entry_orders when USE_TRAIL_AFTER_TP1 is set. Submitted
        # as a separate order from the initial inline SL (both armed; the
        # one that fires first closes the position, the other becomes a
        # closePosition no-op until cleanup_closed_trades cancels it).
        if trailing is not None and sl_inline is None and trigger_price is None:
            return self._place_trailing_stop_market(
                symbol=symbol,
                side=side,
                callback_rate=trailing,
                activation_price=body.get("activePrice"),
                close_position=bool(body.get("closeOnTrigger") or body.get("closePosition")),
                quantity=qty,  # always pass — enables fallback to qty+reduceOnly on -4136/-1106
                client_order_id=_encode_link_id(link_id),
            )

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
            "quantity": self._qty_str(symbol, qty),
            "newClientOrderId": _encode_link_id(link_id),
            "newOrderRespType": "RESULT",
        }
        if order_type_raw == "LIMIT":
            params["price"] = self._price_str(symbol, price)
            params["timeInForce"] = tif
        if reduce_only:
            params["reduceOnly"] = "true"
        resp = self._signed_request("POST", "/fapi/v1/order", params)
        return _wrap_order_response(resp)

    # ====================================================================== #
    # Algo Order endpoint (/fapi/v1/algoOrder)                                #
    # ----------------------------------------------------------------------- #
    # Binance migrated conditional order types (STOP, STOP_MARKET, TAKE_PROFIT,
    # TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET) to a separate "algo" endpoint
    # starting 2025-12-09. The legacy /fapi/v1/order endpoint still accepts
    # these types on some symbols/accounts but is documented to return
    # -4120 STOP_ORDER_SWITCH_ALGO going forward. The algo endpoint:
    #   - is signed identically to /fapi/v1/order (HMAC, timestamp, recvWindow)
    #   - returns `algoId` instead of `orderId`
    #   - takes `triggerPrice` for STOP_MARKET (not `stopPrice`)
    #   - takes `activatePrice` for TRAILING_STOP_MARKET (not `activationPrice`)
    #   - does NOT support closePosition=true on TRAILING_STOP_MARKET → quantity
    #     + reduceOnly required for one-way mode
    #   - is NOT returned by GET /fapi/v1/openOrders — use openAlgoOrders
    # ====================================================================== #
    def _place_algo_order(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """POST /fapi/v1/algoOrder. Returns raw algo response with `algoId`.

        Robustness features for live SL/TRAIL placement:
          - Bounded retry on transient failures (HTTP 5xx, requests
            ConnectionError/Timeout, Binance -1003 TOO_MANY_REQUESTS).
            2 attempts with 80ms / 200ms backoff so total wall time
            stays within our ~500ms post-fill protection budget.
            Signed-validation errors (-1102, -4xxx etc) are NOT retried.
          - -4015 / -1102 duplicate-clientAlgoId is handled by looking
            up the existing algo via openAlgoOrders. Makes retries safe
            against a network timeout that was actually delivered.
          - -4061 ORDER_HEDGE_MODE_NOT_MATCH (user toggled Hedge Mode in
            the Binance app mid-run): every reduceOnly algo place would
            fail forever. On first occurrence we force One-Way via
            /fapi/v1/positionSide/dual (idempotent) and retry the place
            ONCE. A second -4061 in the same call means the recovery
            didn't stick (e.g. account has open Hedge-Mode positions
            blocking the switch — Binance rejects the toggle with -4068
            then) → raise so the caller can surface it. Reference:
            https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code
        """
        last_err: Optional[Exception] = None
        backoffs = [0.0, 0.08, 0.20]  # 3 attempts incl. first
        resp: Optional[Dict[str, Any]] = None
        hedge_recovery_tried = False
        for delay in backoffs:
            if delay:
                time.sleep(delay)
            try:
                resp = self._signed_request("POST", "/fapi/v1/algoOrder", params)
                break
            except BinanceAPIError as e:
                if e.code in (-4015, -1102) and params.get("clientAlgoId"):
                    cid = params["clientAlgoId"]
                    sym = params.get("symbol")
                    try:
                        for ao in self.open_algo_orders(sym):
                            if ao.get("clientAlgoId") == cid:
                                resp = ao
                                break
                        if resp is not None:
                            break
                    except Exception:
                        pass
                if e.code == -4061 and not hedge_recovery_tried:
                    # Hedge-Mode-vs-One-Way mismatch. Try to force One-Way
                    # mode account-wide and retry the place exactly once.
                    hedge_recovery_tried = True
                    sym = params.get("symbol", "?")
                    try:
                        self.set_position_mode_one_way()
                        try:
                            import telegram_alerts
                            telegram_alerts.send_message(
                                f"⚠️ {sym}: -4061 Hedge-Mode mismatch detected — "
                                f"forced One-Way mode and retrying algo place. "
                                f"Check that Binance app didn't toggle Hedge Mode mid-run."
                            )
                        except Exception:
                            pass
                        last_err = e
                        continue
                    except Exception as recovery_err:
                        # Recovery itself failed (e.g. -4068 open positions
                        # blocking the toggle). Surface the original -4061.
                        try:
                            import telegram_alerts
                            telegram_alerts.send_message(
                                f"🚨 {sym}: -4061 recovery FAILED ({recovery_err}). "
                                f"Algo place will fail — fix Hedge Mode manually."
                            )
                        except Exception:
                            pass
                        raise e
                if e.code == -1003:  # TOO_MANY_REQUESTS — retry
                    last_err = e
                    continue
                raise
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                continue
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", 0) if e.response is not None else 0
                if 500 <= status < 600:
                    last_err = e
                    continue
                raise
        if resp is None:
            # All retries exhausted. last_err must be set because the only
            # path that breaks out of the loop without resp is the one that
            # raises directly (handled above) — so if we got here without a
            # resp it's because every attempt recorded a retryable error.
            # Use a real raise instead of assert (which is stripped under
            # `python -O`).
            if last_err is None:
                raise RuntimeError(
                    "_place_algo_order: loop exited with no response and no recorded error"
                )
            raise last_err
        # Check algoStatus — Binance can return 200 OK with algoStatus=REJECTED
        # or EXPIRED at place time (e.g. reduceOnly fails because position is
        # already closed). Without this check the caller would believe the SL
        # was armed and write the algoId into _sl_orders / sl_set_inline=True,
        # leaving the next trade unprotected. Treat terminal-non-active states
        # as a placement failure.
        algo_status = (resp.get("algoStatus") or "").upper()
        if algo_status in ("REJECTED", "EXPIRED", "CANCELED"):
            raise BinanceAPIError(
                -1,
                f"algo place returned algoStatus={algo_status}: {resp.get('msg', '')}",
                resp,
            )
        algo_id = resp.get("algoId")
        if algo_id is not None:
            with self._algo_lock:
                self._algo_orders.add(str(algo_id))
        return resp

    def _cancel_algo_order_by_id(self, algo_id: Any) -> Dict[str, Any]:
        """DELETE /fapi/v1/algoOrder by algoId. Idempotent on
        'unknown algo order' errors. Note: this endpoint does NOT take symbol.
        Error codes (Binance USDS-M Futures error-code page):
          -2011 UNKNOWN_ORDER (algo already filled or cancelled)
          -2013 ORDER_DOES_NOT_EXIST
        Both mean the algo is already gone — treat as idempotent success.
        """
        try:
            resp = self._signed_request(
                "DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}
            )
            with self._algo_lock:
                self._algo_orders.discard(str(algo_id))
            return resp
        except BinanceAPIError as e:
            if e.code in (-2011, -2013):
                with self._algo_lock:
                    self._algo_orders.discard(str(algo_id))
                return {"alreadyClosed": True}
            raise

    def open_algo_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /fapi/v1/openAlgoOrders. Algos do NOT appear in regular
        openOrders, so any orphan-cleanup pass must also iterate this list.
        """
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = self._signed_request("GET", "/fapi/v1/openAlgoOrders", params)
        return data if isinstance(data, list) else []

    def _is_algo_order_id(self, order_id: str) -> bool:
        with self._algo_lock:
            return str(order_id) in self._algo_orders

    def prime_algo_orders(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """Populate the in-memory _algo_orders set and _sl_orders dict from
        whatever algo orders are currently live on Binance. Call this once
        on boot (between state-load and gateway-start) so the post-restart
        bot can:

          1. Route _cancel_by_order_id straight to /fapi/v1/algoOrder
             without paying the regular-endpoint -2013 RTT fallback first.

          2. Skip placing a duplicate SL when set_trading_stop is invoked
             on a position whose SL was already armed before the restart
             (the new algo-SL cid `sl-{symbol}-{ts}-{rnd}` doesn't match
             the legacy `:SL` / `-SL` suffix scan, so without priming the
             scan misses it → bot might place a second algo SL.)

          3. Skip placing a duplicate TRAIL by hydrating
             trade["trail_order_id"] in the caller (main.py). The trail
             cid is `{trade_id}:TRAIL` which decodes from `-TRAIL` after
             _encode_link_id. Caller looks up algoId via the returned
             `trails_by_trade_id` mapping and writes it into the trade
             state BEFORE place_post_entry_orders runs.

        Args:
            symbols: list to limit the query to (one /fapi/v1/openAlgoOrders
                call per symbol). If None, makes a single call without a
                symbol param which returns ALL open algos account-wide.
        Returns:
            dict with:
              - "count": total algos primed
              - "sl_count": algo-SLs added to _sl_orders
              - "trails_by_trade_id": {trade_id: algoId} for TRAIL algos —
                caller must apply these to the corresponding state entries.
        Errors are swallowed; this is best-effort.
        Reference: /fapi/v1/openAlgoOrders (no `symbol` param → all open).
        """
        primed = 0
        sl_primed = 0
        trails_by_trade_id: Dict[str, str] = {}
        try:
            if symbols:
                algos: List[Dict[str, Any]] = []
                for sym in symbols:
                    try:
                        algos.extend(self.open_algo_orders(sym))
                    except Exception:
                        continue
            else:
                algos = self.open_algo_orders()
        except Exception:
            return {"count": 0, "sl_count": 0, "trails_by_trade_id": {}}

        for ao in algos:
            aid = str(ao.get("algoId") or "")
            if not aid:
                continue
            with self._algo_lock:
                self._algo_orders.add(aid)
                primed += 1
            sym = ao.get("symbol")
            cid_raw = _decode_client_id(ao.get("clientAlgoId") or "")
            cid_parts = cid_raw.split(":")
            is_sl = (
                cid_raw.startswith("sl-")
                or cid_raw.endswith("-SL")
                or (len(cid_parts) == 2 and cid_parts[1] == "SL")
            )
            is_trail = (
                cid_raw.endswith("-TRAIL")
                or (len(cid_parts) == 2 and cid_parts[1] == "TRAIL")
            )
            if is_sl and sym:
                with self._sl_lock:
                    self._sl_orders.setdefault(sym, aid)
                    sl_primed += 1
            elif is_trail:
                # Recover the trade_id from the cid. Both forms produce a
                # `{trade_id}:TRAIL` or `{trade_id}-TRAIL` shape. Strip suffix.
                if cid_raw.endswith("-TRAIL"):
                    trade_id = cid_raw[: -len("-TRAIL")]
                elif cid_raw.endswith(":TRAIL"):
                    trade_id = cid_raw[: -len(":TRAIL")]
                else:
                    trade_id = ""
                if trade_id:
                    trails_by_trade_id[trade_id] = aid
        return {
            "count": primed,
            "sl_count": sl_primed,
            "trails_by_trade_id": trails_by_trade_id,
        }

    def _place_trailing_stop_market(
        self,
        symbol: str,
        side: str,
        callback_rate: Any,
        activation_price: Optional[Any],
        close_position: bool,  # kept for API compat; ignored — algo never accepts it
        quantity: Optional[Any],
        client_order_id: str,
    ) -> Dict[str, Any]:
        """Place a TRAILING_STOP_MARKET via /fapi/v1/algoOrder.

        Per Binance docs (algo endpoint, 2025-12-09 migration):
          - algoType=CONDITIONAL, type=TRAILING_STOP_MARKET
          - callbackRate: 0.1 .. 10.0 (1 dp, 1 = 1%)
          - activatePrice (NOT activationPrice): optional; defaults to mark
            at place-time. For BUY (closing SHORT) activatePrice must be
            < latest price; for SELL it must be > latest price. Violations
            return -4135 INVALID_ACTIVATION_PRICE.
          - closePosition is NOT supported on algo trailing — must use
            quantity + reduceOnly. reduceOnly=true is mandatory in our flow
            as a safety belt against an orphan trigger opening a reverse
            position if the underlying position has already been closed.
          - workingType: MARK_PRICE or CONTRACT_PRICE (default).

        Fallback: if activatePrice would immediately trigger (-4135), retry
        without activatePrice so Binance defaults to the current mark.
        """
        if quantity is None:
            raise RuntimeError(
                "TRAILING_STOP_MARKET via algoOrder requires quantity "
                "(closePosition is not supported on the algo endpoint)"
            )

        def _build(use_activation: bool) -> Dict[str, Any]:
            p: Dict[str, Any] = {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": side,
                "type": "TRAILING_STOP_MARKET",
                "quantity": self._qty_str(symbol, quantity),
                "reduceOnly": "true",
                "callbackRate": _fmt_num(callback_rate),
                "workingType": "MARK_PRICE",
                "clientAlgoId": client_order_id,
                "newOrderRespType": "ACK",
            }
            if use_activation and activation_price is not None:
                p["activatePrice"] = self._price_str(symbol, activation_price)
            return p

        try:
            resp = self._place_algo_order(_build(True))
            return _wrap_algo_response(resp)
        except BinanceAPIError as e:
            # Activation-price already crossed: Binance returns
            # -4135 INVALID_ACTIVATION_PRICE at validation OR
            # -2021 ORDER_WOULD_IMMEDIATELY_TRIGGER at trigger-check,
            # depending on whether the cross is detected pre- or post-
            # placement validation. Retry without activatePrice so the
            # exchange defaults it to current mark.
            if e.code in (-4135, -2021) and activation_price is not None:
                resp = self._place_algo_order(_build(False))
                return _wrap_algo_response(resp)
            raise

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
            "quantity": self._qty_str(symbol, qty),
            "price": self._price_str(symbol, price),
            "stopPrice": self._price_str(symbol, stop_price),
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
            "quantity": self._qty_str(symbol, qty),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "ACK",  # ACK is ~50ms faster; orderId is included
        }
        if order_type == "LIMIT":
            entry_order["price"] = self._price_str(symbol, price)
            entry_order["timeInForce"] = tif
        sl_order = {
            "symbol": symbol,
            "side": sl_side,
            "type": "STOP_MARKET",
            "stopPrice": self._price_str(symbol, sl_price),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "newClientOrderId": sl_cid,
            "newOrderRespType": "ACK",
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
        sl_ok = True
        if isinstance(sl_resp, dict) and sl_resp.get("code", 0) and int(sl_resp.get("code", 0)) < 0:
            # Entry placed, SL leg failed. We MUST signal this back so
            # trade_engine's place_post_entry_orders re-issues an SL after
            # fill — otherwise the position would run unprotected.
            sl_ok = False
        else:
            with self._sl_lock:
                self._sl_orders[symbol] = str(sl_resp.get("orderId"))
        wrapped = _wrap_order_response(entry_resp)
        wrapped["result"]["slInlineOk"] = sl_ok
        return wrapped

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
        """Cancel an order by id. Routes to /fapi/v1/algoOrder when the id
        is tracked in _algo_orders (STOP_MARKET / TRAILING_STOP_MARKET on
        the algo endpoint), otherwise to /fapi/v1/order. Errors are
        swallowed — used by cleanup paths that should never raise.
        """
        if self._is_algo_order_id(order_id):
            try:
                self._cancel_algo_order_by_id(order_id)
            except Exception:
                pass
            return
        try:
            self._signed_request(
                "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
            )
        except BinanceAPIError as e:
            # If the regular endpoint says -2011 UNKNOWN_ORDER or -2013
            # ORDER_DOES_NOT_EXIST, the id may actually be an algo we
            # forgot to track (e.g. across a restart with an empty
            # in-memory _algo_orders set). Try the algo endpoint as
            # fallback. -1102 is "malformed query" — that's a real bug,
            # not a routing miss, so we let it propagate to logs.
            if e.code in (-2011, -2013):
                try:
                    self._cancel_algo_order_by_id(order_id)
                except Exception:
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
        """Replace the active SL order for a symbol via the algo endpoint.

        Binance migrated STOP_MARKET / TRAILING_STOP_MARKET to
        /fapi/v1/algoOrder on 2025-12-09 (closePosition is forbidden there
        on TRAILING_STOP_MARKET; STOP_MARKET also moved). This function
        places a fresh STOP_MARKET (or TRAILING_STOP_MARKET) algo order
        with reduceOnly=true matching the current position size, and only
        THEN cancels any previously tracked SL.

        Recognised fields: symbol, stopLoss, takeProfit (optional),
        trailingStop (optional), activePrice (optional), qty (optional —
        if provided the caller's size is used, avoiding a second
        position-size lookup race).

        Returns {"retCode":0, "result":{"orderId": "<algoId>", ...}}.
        `noPosition: True` is returned when there is no position to
        protect — caller must treat that as 'SL NOT armed'. The existing
        SL is preserved in that case (we do NOT cancel-and-not-replace).

        Ordering: PLACE-NEW-FIRST, then CANCEL-OLD.
        Rationale: the previous order (cancel-then-place) opened a
        ~50–100 ms window with no SL armed. If the place leg's retry
        budget was exhausted (transient 5xx) the position would have
        been naked until the next caller invocation. By placing the new
        SL first, the worst case is a brief window where the position
        has TWO reduceOnly STOP_MARKET algo orders on it — Binance
        accepts multiple STOP_MARKET orders per position (no closePosition
        flag is set; reduceOnly enforces no over-close) so the duplicate
        is harmless: whichever triggers first closes the position, and
        the second naturally becomes a no-op (reduceOnly fires against
        zero size).
        Refs:
          - /fapi/v1/algoOrder (algo orders coexist; reduceOnly enforces)
          - error code -2022 ReduceOnly Order Failed (harmless on already-closed)
        """
        symbol = body["symbol"]
        new_sl = body.get("stopLoss")
        trailing = body.get("trailingStop")

        # 1. POSITION CHECK FIRST — bail early if no live position to protect.
        # Otherwise resolve closing side + qty for the new order.
        if new_sl or trailing:
            side_close = self._closing_side(symbol)
            qty_override = body.get("qty")
            try:
                pos_size = float(qty_override) if qty_override is not None else self._position_size(symbol)
            except (TypeError, ValueError):
                pos_size = self._position_size(symbol)
            if side_close is None or pos_size <= 0:
                return {"retCode": 0, "result": {"noPosition": True}}
        else:
            side_close = None
            pos_size = 0.0

        # Snapshot the currently tracked SL id (we'll cancel it AFTER the
        # new SL is confirmed live). Captured under the lock to stay
        # consistent with concurrent ALGO_UPDATE handler mutations.
        with self._sl_lock:
            old_sl = self._sl_orders.get(symbol)

        # If caller only wanted to clear SL (no new SL/trail) → cancel any
        # tracked SL and exit; no new order to place.
        if not new_sl and not trailing:
            if old_sl:
                self._cancel_by_order_id(symbol, old_sl)
                with self._sl_lock:
                    if self._sl_orders.get(symbol) == old_sl:
                        self._sl_orders.pop(symbol, None)
            else:
                # No tracked SL — scan for an SL-tagged orphan to clean up.
                self._cleanup_orphan_sl(symbol)
            return {"retCode": 0, "result": {}}

        # 2. PLACE the new algo order FIRST (before cancelling old).
        # Random 4-char suffix on cid prevents collisions when two
        # SLs are placed in the same millisecond on the same symbol.
        # Truncate symbol (not the timestamp) so the suffix is preserved
        # on long symbols like 1000PEPEUSDT. Route through _encode_link_id
        # so any future colon/pipe in `symbol` is regex-sanitised.
        rnd = os.urandom(2).hex()
        cid = _encode_link_id(f"sl-{symbol[:14]}-{int(time.time()*1000)}-{rnd}")

        params: Dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side_close,
            "quantity": self._qty_str(symbol, pos_size),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
            "clientAlgoId": cid,
            "newOrderRespType": "ACK",
        }
        if trailing and not new_sl:
            params["type"] = "TRAILING_STOP_MARKET"
            params["callbackRate"] = _fmt_num(trailing)
            if body.get("activePrice"):
                params["activatePrice"] = self._price_str(symbol, body["activePrice"])
        else:
            params["type"] = "STOP_MARKET"
            params["triggerPrice"] = self._price_str(symbol, new_sl)
            params["priceProtect"] = "true"

        # If the place leg raises, the OLD SL is still live — position
        # remains protected. Caller (set_trading_stop callers in
        # trade_engine) will see the exception and decide whether to
        # retry. This is the whole point of place-before-cancel.
        resp = self._place_algo_order(params)
        algo_id = str(resp.get("algoId", ""))
        if algo_id:
            with self._sl_lock:
                self._sl_orders[symbol] = algo_id

        # 3. NEW SL is now confirmed live. Cancel the old one.
        # Errors are swallowed by _cancel_by_order_id; worst case is a
        # stale algo that triggers later against a closed position
        # (-2022 ReduceOnly Order Failed — handled idempotently in
        # ALGO_UPDATE / _cancel_algo_order_by_id).
        if old_sl and old_sl != algo_id:
            self._cancel_by_order_id(symbol, old_sl)
        else:
            # No tracked SL — scan for an SL-tagged orphan (legacy
            # inline-SL cid suffix :SL OR new algo SL cid prefix "sl-").
            self._cleanup_orphan_sl(symbol, exclude_algo_id=algo_id)

        return {"retCode": 0, "result": {"orderId": algo_id, "isAlgo": True, **resp}}

    def _cleanup_orphan_sl(self, symbol: str, exclude_algo_id: Optional[str] = None) -> None:
        """Scan algo + regular open orders for an SL-tagged clientId we
        lost track of, and cancel it. Used by set_trading_stop when the
        in-memory _sl_orders has no entry (e.g. across restart).

        `exclude_algo_id` lets the caller skip the algo just placed —
        critical for place-before-cancel so we don't immediately
        cancel the SL we just armed.

        Recognises:
          - legacy inline-SL cid suffix ":SL" / "-SL" (regular endpoint)
          - new algo SL cid prefix "sl-" (algo endpoint)
        Errors are swallowed — this is a best-effort cleanup.
        """
        try:
            for o in self.open_orders("linear", symbol):
                cid_raw = o.get("orderLinkId") or ""
                cid_parts = cid_raw.split(":")
                if (len(cid_parts) == 2 and cid_parts[1] == "SL") or cid_raw.endswith("-SL"):
                    self._cancel_by_order_id(symbol, str(o.get("orderId", "")))
                    break
        except Exception:
            pass
        try:
            for ao in self.open_algo_orders(symbol):
                aid = str(ao.get("algoId") or "")
                if exclude_algo_id and aid == exclude_algo_id:
                    continue
                cid_raw = _decode_client_id(ao.get("clientAlgoId") or "")
                cid_parts = cid_raw.split(":")
                if ((len(cid_parts) == 2 and cid_parts[1] == "SL")
                        or cid_raw.startswith("sl-")
                        or cid_raw.endswith("-SL")):
                    self._cancel_algo_order_by_id(ao.get("algoId"))
                    break
        except Exception:
            pass

    def _position_size(self, symbol: str) -> float:
        """Returns the open position size for `symbol`, or 0 if none.
        Prefers WS cache, falls back to REST. Swallows network errors so
        callers can treat 0 as 'no position'."""
        cached = self.get_cached_position_full(symbol)
        if cached is not None:
            size, _avg, _side = cached
            if size > 0:
                return float(size)
        try:
            for p in self.positions("linear", symbol):
                if p.get("symbol") == symbol:
                    sz = abs(float(p.get("size", 0) or 0))
                    if sz > 0:
                        return sz
        except Exception:
            pass
        return 0.0

    def _closing_side(self, symbol: str) -> Optional[str]:
        """Return the order side that would CLOSE the open position on this
        symbol (BUY for SHORT, SELL for LONG). None if no position open."""
        cached = self.get_cached_position_full(symbol)
        if cached is not None:
            size, _avg, side = cached
            if size > 0 and side:
                return "BUY" if side == "Sell" else "SELL"
            # size 0 or empty side → fall through to REST (cache may be
            # stale right after a fill / reconnect)
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
            elif ev_type == "ALGO_UPDATE":
                self._handle_algo_update(msg.get("o") or {}, on_order)
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
            # Position-closing order just fired (SL or TRAIL). Binance does
            # NOT auto-cancel sibling reduceOnly/closePosition orders when
            # one closes the position to 0 — without explicit cleanup the
            # orphans sit and could misfire on the next trade for the same
            # symbol. Cancel ALL open closers in that case.
            #
            # IMPORTANT: distinguish a true position-closing event from a
            # partial TP LIMIT fill. A LIMIT reduceOnly TP that fills its
            # own qty completely is `closed_fully=True` but the underlying
            # position likely still has volume (DCA-grown size, or another
            # TP slice). Sweeping reduceOnly siblings on a TP fill would
            # cancel the still-needed SL/TRAIL → naked position.
            #
            # The narrow set of events that DO justify a sweep:
            #   - cid suffix marks our SL/TRAIL (legacy inline-SL had :SL,
            #     trail body has :TRAIL, set_trading_stop algo SL has
            #     prefix "sl-")
            #   - order_type is STOP_MARKET / TRAILING_STOP_MARKET (the
            #     legacy /fapi/v1/order path before the algo migration)
            #   - order_type is MARKET AND reduceOnly AND closed_fully —
            #     this is the spawned market order created when a
            #     /fapi/v1/algoOrder STOP_MARKET or TRAILING_STOP_MARKET
            #     triggers. (Binance docs don't enumerate the spawned
            #     order's fields; the cid is auto-generated, so neither
            #     suffix nor type matches without this branch.)
            cid_str = str(cid)
            is_sl = (cid_str.endswith("-SL")
                     or cid_str.endswith(":SL")
                     or cid_str.startswith("sl-"))
            is_trail = cid_str.endswith("-TRAIL") or cid_str.endswith(":TRAIL")
            order_type = (o.get("o") or "").upper()
            reduce_only = bool(o.get("R"))
            try:
                cum_qty = float(o.get("z") or 0)
                orig_qty = float(o.get("q") or 0)
            except (TypeError, ValueError):
                cum_qty = orig_qty = 0.0
            closed_fully = orig_qty > 0 and cum_qty >= orig_qty
            is_algo_spawn = (order_type == "MARKET" and reduce_only and closed_fully)
            if (is_sl or is_trail
                    or order_type in ("STOP_MARKET", "TRAILING_STOP_MARKET")
                    or is_algo_spawn):
                sym = o.get("s")
                # Log when the narrow algo-spawn branch fires (MARKET +
                # reduceOnly + closed_fully, NOT cid-tagged as SL/TRAIL).
                # This is the only path that catches the auto-spawned
                # market order produced when a /fapi/v1/algoOrder
                # STOP_MARKET / TRAILING_STOP_MARKET triggers — Binance
                # docs don't enumerate the spawned order's fields, so
                # we want production telemetry to confirm it's actually
                # catching real algo triggers (not over-firing on TP fills).
                if is_algo_spawn and not (is_sl or is_trail
                        or order_type in ("STOP_MARKET", "TRAILING_STOP_MARKET")):
                    logger.warning(
                        f"[algo-spawn cleanup] {sym} cid={cid_str} type={order_type} "
                        f"reduce_only={reduce_only} cum_qty={cum_qty}/{orig_qty} — "
                        f"sweeping reduce-only siblings"
                    )
                if is_sl:
                    with self._sl_lock:
                        self._sl_orders.pop(sym, None)
                if sym:
                    self._cancel_reduce_only_for_symbol(sym)
        if on_order:
            try:
                on_order(ev)
            except Exception:
                pass

    def _cancel_reduce_only_for_symbol(self, symbol: str) -> None:
        """Cancel every open protective order on this symbol after one of
        them fires (TP/DCA siblings of a just-fired SL, OR an orphan
        TRAILING_STOP_MARKET / STOP_MARKET that didn't fire).

        Sweeps BOTH endpoints:
          - /fapi/v1/openOrders   — regular orders (TPs, DCAs, legacy SL/TRAIL)
          - /fapi/v1/openAlgoOrders — algo orders (SL/TRAIL post 2025-12-09)

        Algos do NOT show up in the regular openOrders list, so without
        this dual-sweep an algo-SL would linger as an orphan after the
        position closes and could misfire on the next trade.
        """
        try:
            opens = self.open_orders("linear", symbol)
        except Exception:
            opens = []
        for o in opens:
            # Cancel anything that's a position-closer: reduce-only OR
            # close-position (returned as closeOnTrigger in our Bybit-
            # shaped dict by _to_bybit_order_dict).
            if not (o.get("reduceOnly") or o.get("closeOnTrigger")):
                continue
            oid = str(o.get("orderId", ""))
            if oid:
                self._cancel_by_order_id(symbol, oid)

        # Algo orders are reduce-only by construction in our flow (SL +
        # TRAIL only). Cancel all of them.
        try:
            algos = self.open_algo_orders(symbol)
        except Exception:
            algos = []
        for a in algos:
            aid = a.get("algoId")
            if aid is None:
                continue
            try:
                self._cancel_algo_order_by_id(aid)
            except Exception:
                pass

    def cancel_all_open_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """One-call cancel of every open order on a symbol. Hits BOTH
        /fapi/v1/allOpenOrders (regular) AND every entry in
        /fapi/v1/openAlgoOrders (algo). After 2025-12-09 algo-SL/TRAIL
        orders are NOT cancelled by /fapi/v1/allOpenOrders, so this dual
        path is required for true 'everything gone' semantics.

        Returns {"code": 0, "msg": "ok"} only when BOTH sweeps succeeded.
        Partial outcomes are signalled so callers like cleanup_closed_trades
        know to retry instead of marking the symbol clean. Orphan algos
        cause cross-trade misfires — a partial failure must never look
        like success.
        """
        regular_ok = True
        algo_ok = True
        algo_count = 0
        try:
            self._signed_request(
                "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}
            )
        except BinanceAPIError as e:
            if e.code not in (-2011, -2013):
                regular_ok = False

        # Enumerate algos. Retry once on transient failure before declaring
        # algo-partial — an orphan algo here means the next trade on this
        # symbol can be wrongly closed by a leftover trail.
        algos: List[Dict[str, Any]] = []
        for _attempt in range(2):
            try:
                algos = self.open_algo_orders(symbol)
                break
            except Exception:
                if _attempt == 1:
                    algo_ok = False
                time.sleep(0.1)

        for a in algos:
            aid = a.get("algoId")
            if aid is None:
                continue
            try:
                self._cancel_algo_order_by_id(aid)
                algo_count += 1
            except Exception:
                algo_ok = False

        with self._sl_lock:
            self._sl_orders.pop(symbol, None)

        if regular_ok and algo_ok:
            return {"code": 0, "msg": "ok", "algoCancelled": algo_count}
        msg_parts = []
        if not regular_ok:
            msg_parts.append("regular-partial")
        if not algo_ok:
            msg_parts.append("algo-partial")
        return {"code": -1, "msg": ",".join(msg_parts), "algoCancelled": algo_count}

    def _handle_algo_update(self, o: Dict[str, Any], on_order):
        """ALGO_UPDATE event payload — algo-order lifecycle transitions
        (NEW → TRIGGERING → TRIGGERED / CANCELED / REJECTED / EXPIRED /
        FINISHED). Replaces the deprecated CONDITIONAL_ORDER_TRIGGER_REJECT
        event since 2025-12-09.

        We use this purely for state-tracking and visibility:
          - Drop the algoId from _algo_orders / _sl_orders on terminal
            states so a stale id can't survive into the next trade.
          - Log REJECTED loudly — a rejection here (e.g. reduceOnly fails
            because position already closed) is a safety event that the
            old code would have silently missed.

        Actual position close is still driven by the ORDER_TRADE_UPDATE
        for the spawned market order — that's where _cancel_reduce_only_
        for_symbol() fires from. This handler is supplementary, not
        load-bearing on the protective path.
        """
        aid = str(o.get("aid", ""))
        sym = o.get("s")
        status = (o.get("X") or "").upper()
        cid = o.get("caid") or ""
        ot = (o.get("o") or "").upper()
        if status in ("CANCELED", "FINISHED", "EXPIRED", "REJECTED"):
            with self._algo_lock:
                self._algo_orders.discard(aid)
            if sym:
                with self._sl_lock:
                    tracked = self._sl_orders.get(sym)
                    if tracked == aid:
                        self._sl_orders.pop(sym, None)
        if status == "REJECTED":
            # Surface this — a silent reject would have hidden e.g. a
            # reduceOnly-on-zero-position rejection that means our SL
            # never armed. Forwarded via on_order so trade_engine can
            # alert if it cares.
            try:
                if on_order:
                    on_order({
                        "algoUpdate": True,
                        "algoId": aid,
                        "clientAlgoId": _decode_client_id(cid),
                        "symbol": sym,
                        "orderType": ot,
                        "status": status,
                    })
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
            side = "Sell" if pa < 0 else ("Buy" if pa > 0 else "")
            with self._ws_pos_lock:
                self._ws_positions[sym] = (size, ep, side, time.time())
            if on_position:
                try:
                    on_position({
                        "symbol": sym,
                        "size": size,
                        "avgPrice": ep,
                        "side": side,
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


def _wrap_algo_response(resp: Any) -> Dict[str, Any]:
    """Convert an algo-order response → Bybit-style envelope.
    The unique id field is `algoId` (LONG), not `orderId`. We surface it
    in the `orderId` slot so downstream code that stores `trade["sl_order_id"]`
    / `trade["trail_order_id"]` works unchanged. The cancel path uses
    _is_algo_order_id() to route to the correct DELETE endpoint.
    """
    if not isinstance(resp, dict):
        return {"retCode": 0, "result": {}}
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "orderId": str(resp.get("algoId", "")),
            "orderLinkId": _decode_client_id(resp.get("clientAlgoId", "")),
            "isAlgo": True,
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
