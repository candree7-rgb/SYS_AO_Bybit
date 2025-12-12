import time
import math
from typing import Any, Dict, List, Optional

from config import (
    CATEGORY, ACCOUNT_TYPE, QUOTE, LEVERAGE, RISK_PCT,
    ENTRY_EXPIRATION_MIN, ENTRY_TOO_FAR_PCT, ENTRY_TRIGGER_BUFFER_PCT, ENTRY_LIMIT_PRICE_OFFSET_PCT,
    ENTRY_EXPIRATION_PRICE_PCT,
    TP_SPLITS, DCA_QTY_MULTS,
    MOVE_SL_TO_BE_ON_TP1,
    TRAIL_AFTER_TP_INDEX, TRAIL_DISTANCE_PCT, TRAIL_ACTIVATE_ON_TP,
    DRY_RUN
)

def _opposite_side(side: str) -> str:
    return "Sell" if side == "Buy" else "Buy"

def _pos_side(side: str) -> str:
    return "Long" if side == "Buy" else "Short"

class TradeEngine:
    def __init__(self, bybit, state: dict, logger):
        self.bybit = bybit
        self.state = state
        self.log = logger

    # ---------- precision helpers ----------
    @staticmethod
    def _floor_to_step(x: float, step: float) -> float:
        if step <= 0:
            return x
        return math.floor(x / step) * step

    def _get_qty_rules(self, symbol: str) -> Dict[str, float]:
        info = self.bybit.instruments_info(CATEGORY, symbol)
        lot = info.get("lotSizeFilter") or {}
        qty_step = float(lot.get("qtyStep") or lot.get("basePrecision") or "0.000001")
        min_qty  = float(lot.get("minOrderQty") or "0")
        return {"qty_step": qty_step, "min_qty": min_qty}

    def calc_base_qty(self, symbol: str, entry_price: float) -> float:
        # Risk model: margin = equity * RISK_PCT; notional = margin * LEVERAGE; qty = notional / price
        equity = self.bybit.wallet_equity(ACCOUNT_TYPE)
        margin = equity * (RISK_PCT / 100.0)
        notional = margin * LEVERAGE
        qty = notional / entry_price

        rules = self._get_qty_rules(symbol)
        qty = self._floor_to_step(qty, rules["qty_step"])
        if qty < rules["min_qty"]:
            qty = rules["min_qty"]
        return float(f"{qty:.10f}")

    # ---------- entry gatekeepers ----------
    def _too_far(self, side: str, last: float, trigger: float) -> bool:
        # If SHORT and price already X% under trigger -> skip
        if side == "Sell":
            return last <= trigger * (1 - ENTRY_TOO_FAR_PCT / 100.0)
        return last >= trigger * (1 + ENTRY_TOO_FAR_PCT / 100.0)

    def _beyond_expiry_price(self, side: str, last: float, trigger: float) -> bool:
        # Extra: if market already beyond trigger by ENTRY_EXPIRATION_PRICE_PCT, skip (avoids bad market fills)
        if ENTRY_EXPIRATION_PRICE_PCT <= 0:
            return False
        if side == "Sell":
            return last <= trigger * (1 - ENTRY_EXPIRATION_PRICE_PCT / 100.0)
        return last >= trigger * (1 + ENTRY_EXPIRATION_PRICE_PCT / 100.0)

    def _trigger_direction(self, last: float, trigger: float) -> int:
        # Bybit: 1=rises to trigger, 2=falls to trigger
        if last < trigger:
            return 1
        if last > trigger:
            return 2
        return 1

    # ---------- order / position helpers ----------
    def _position(self, symbol: str) -> Optional[Dict[str, Any]]:
        plist = self.bybit.positions(CATEGORY, symbol)
        for p in plist:
            if p.get("symbol") == symbol:
                return p
        return None

    def position_size_avg(self, symbol: str) -> tuple[float, float]:
        p = self._position(symbol)
        if not p:
            return 0.0, 0.0
        size = float(p.get("size") or 0)
        avg  = float(p.get("avgPrice") or 0)
        return size, avg

    # ---------- core actions ----------
    def place_conditional_entry(self, sig: Dict[str, Any], trade_id: str) -> Optional[str]:
        symbol = sig["symbol"]
        side   = "Sell" if sig["side"] == "sell" else "Buy"
        trigger = float(sig["trigger"])

        # ensure leverage set
        try:
            if not DRY_RUN:
                self.bybit.set_leverage(CATEGORY, symbol, LEVERAGE)
        except Exception as e:
            self.log.warning(f"set_leverage failed for {symbol}: {e}")

        last = self.bybit.last_price(CATEGORY, symbol)
        if self._too_far(side, last, trigger):
            self.log.info(f"SKIP {symbol} – too far past trigger (last={last}, trigger={trigger})")
            return None
        if self._beyond_expiry_price(side, last, trigger):
            self.log.info(f"SKIP {symbol} – beyond expiry-price rule (last={last}, trigger={trigger})")
            return None

        # buffer: slightly earlier trigger if desired
        trigger_adj = trigger * (1 - ENTRY_TRIGGER_BUFFER_PCT / 100.0) if side == "Buy" else trigger * (1 + ENTRY_TRIGGER_BUFFER_PCT / 100.0)

        # We use LIMIT conditional by default for exact pricing; optionally offset the limit to improve fill odds
        limit_price = trigger
        if ENTRY_LIMIT_PRICE_OFFSET_PCT != 0:
            off = abs(ENTRY_LIMIT_PRICE_OFFSET_PCT) / 100.0
            if side == "Sell":
                limit_price = trigger * (1 + off)
            else:
                limit_price = trigger * (1 - off)

        qty = self.calc_base_qty(symbol, trigger)
        td = self._trigger_direction(last, trigger_adj)

        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": f"{qty:.10f}",
            "price": f"{limit_price:.10f}",
            "timeInForce": "GTC",
            "triggerDirection": td,
            "triggerPrice": f"{trigger_adj:.10f}",
            "triggerBy": "LastPrice",
            "reduceOnly": False,
            "closeOnTrigger": False,
            "orderLinkId": trade_id,
        }

        if DRY_RUN:
            self.log.info(f"DRY_RUN ENTRY {symbol}: {body}")
            return "DRY_RUN"

        resp = self.bybit.place_order(body)
        oid = (resp.get("result") or {}).get("orderId")
        return oid

    def cancel_entry(self, symbol: str, order_id: str) -> None:
        body = {"category": CATEGORY, "symbol": symbol, "orderId": order_id}
        if DRY_RUN:
            self.log.info(f"DRY_RUN cancel entry: {body}")
            return
        self.bybit.cancel_order(body)

    def place_post_entry_orders(self, trade: Dict[str, Any]) -> None:
        """Places SL + TP ladder + DCA conditionals after entry is filled."""
        symbol = trade["symbol"]
        side   = trade["order_side"]  # Buy/Sell
        entry  = float(trade["entry_price"])
        base_qty = float(trade["base_qty"])

        # ---- SL (position-level) ----
        sl_price = trade.get("sl_price")
        if sl_price is None:
            # default: 19% style is signal-specific; if missing, do nothing
            sl_price = entry * (1 + 0.19) if side == "Sell" else entry * (1 - 0.19)

        ts_body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "stopLoss": f"{float(sl_price):.10f}",
            "tpslMode": "Full",
        }
        if DRY_RUN:
            self.log.info(f"DRY_RUN set SL: {ts_body}")
        else:
            self.bybit.set_trading_stop(ts_body)

        # ---- TP ladder (reduce-only LIMITs) ----
        size, _avg = self.position_size_avg(symbol)
        if size <= 0:
            # sometimes position size appears a bit later; retry via main loop
            self.log.warning(f"No position size yet for {symbol}; will retry post-orders")
            return

        tp_prices: List[float] = trade.get("tp_prices") or []
        splits: List[float] = trade.get("tp_splits") or TP_SPLITS

        # ensure we have same length; if signal provides 4 tps, we keep all but only place up to len(splits)
        tp_to_place = min(len(tp_prices), len(splits))
        for idx in range(tp_to_place):
            pct = float(splits[idx])
            if pct <= 0:
                continue
            tp = float(tp_prices[idx])
            qty = size * (pct / 100.0)
            o = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": _opposite_side(side),
                "orderType": "Limit",
                "qty": f"{qty:.10f}",
                "price": f"{tp:.10f}",
                "timeInForce": "GTC",
                "reduceOnly": True,
                "closeOnTrigger": False,
                "orderLinkId": f"{trade['id']}:TP{idx+1}",
            }
            if DRY_RUN:
                self.log.info(f"DRY_RUN TP{idx+1}: {o}")
                oid = f"DRY_TP{idx+1}"
            else:
                resp = self.bybit.place_order(o)
                oid = (resp.get("result") or {}).get("orderId")
            trade.setdefault("tp_order_ids", {})[str(idx+1)] = oid
            if idx == 0:
                trade["tp1_order_id"] = oid

        # ---- DCA conditionals (add) ----
        dca_prices: List[float] = trade.get("dca_prices") or []
        for j, price in enumerate(dca_prices, start=1):
            mult = DCA_QTY_MULTS[min(j-1, len(DCA_QTY_MULTS)-1)]
            qty = base_qty * mult
            last = self.bybit.last_price(CATEGORY, symbol)
            td = self._trigger_direction(last, float(price))
            o = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": f"{qty:.10f}",
                "price": f"{float(price):.10f}",
                "timeInForce": "GTC",
                "triggerDirection": td,
                "triggerPrice": f"{float(price):.10f}",
                "triggerBy": "LastPrice",
                "reduceOnly": False,
                "closeOnTrigger": False,
                "orderLinkId": f"{trade['id']}:DCA{j}",
            }
            if DRY_RUN:
                self.log.info(f"DRY_RUN DCA{j}: {o}")
            else:
                self.bybit.place_order(o)

        trade["post_orders_placed"] = True

    # ---------- reactive events ----------
    def on_execution(self, ev: Dict[str, Any]) -> None:
        link = ev.get("orderLinkId") or ev.get("orderLinkID") or ""
        if not link:
            return

        # Entry filled?
        if link in self.state.get("open_trades", {}):
            tr = self.state["open_trades"][link]
            if tr.get("status") == "pending":
                # some execution payloads contain execPrice/lastPrice
                exec_price = ev.get("execPrice") or ev.get("price") or ev.get("lastPrice") or tr.get("trigger")
                try:
                    tr["entry_price"] = float(exec_price)
                except Exception:
                    pass
                tr["status"] = "open"
                tr["filled_ts"] = time.time()
                self.log.info(f"✅ ENTRY FILLED {tr['symbol']} @ {tr.get('entry_price')}")
            return

        # TP fills / other events: orderLinkId pattern "<trade_id>:TP1"
        if ":TP" in link:
            trade_id, tp_tag = link.split(":", 1)
            tr = self.state.get("open_trades", {}).get(trade_id)
            if not tr:
                return
            tp_num = None
            m = None
            import re as _re
            m = _re.search(r"TP(\d+)", tp_tag)
            if m:
                tp_num = int(m.group(1))
            if not tp_num:
                return

            # TP1 -> SL to BE
            if MOVE_SL_TO_BE_ON_TP1 and tp_num == 1 and not tr.get("sl_moved_to_be"):
                be = float(tr.get("entry_price") or tr.get("trigger"))
                self._move_sl(tr["symbol"], be)
                tr["sl_moved_to_be"] = True
                self.log.info(f"✅ SL -> BE {tr['symbol']} @ {be}")

            # start trailing after TPn
            if TRAIL_ACTIVATE_ON_TP and tp_num == TRAIL_AFTER_TP_INDEX and not tr.get("trailing_started"):
                self._start_trailing(tr, tp_num)
                tr["trailing_started"] = True
                self.log.info(f"✅ TRAILING STARTED {tr['symbol']} after TP{tp_num}")

    def _move_sl(self, symbol: str, sl_price: float) -> None:
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "stopLoss": f"{float(sl_price):.10f}",
            "tpslMode": "Full",
        }
        if DRY_RUN:
            self.log.info(f"DRY_RUN move SL: {body}")
            return
        self.bybit.set_trading_stop(body)

    def _start_trailing(self, tr: Dict[str, Any], tp_num: int) -> None:
        # Bybit trailingStop expects absolute distance (price units), so we convert percent -> price distance
        symbol = tr["symbol"]
        side = tr["order_side"]  # Buy/Sell
        tp_prices = tr.get("tp_prices") or []
        if len(tp_prices) < tp_num:
            # fallback: use current market
            anchor = self.bybit.last_price(CATEGORY, symbol)
        else:
            anchor = float(tp_prices[tp_num-1])

        dist = anchor * (TRAIL_DISTANCE_PCT / 100.0)

        # activation price: anchor (TP level)
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
            "activePrice": f"{anchor:.10f}",
            "trailingStop": f"{dist:.10f}",
        }

        # keep SL at BE if already moved; otherwise keep existing stopLoss unchanged
        if tr.get("sl_moved_to_be") and tr.get("entry_price"):
            body["stopLoss"] = f"{float(tr['entry_price']):.10f}"

        if DRY_RUN:
            self.log.info(f"DRY_RUN set trailing: {body}")
            return
        self.bybit.set_trading_stop(body)

    # ---------- maintenance ----------
    def cancel_expired_entries(self) -> None:
        now = time.time()
        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") != "pending":
                continue
            placed = float(tr.get("placed_ts") or 0)
            if placed and now - placed > ENTRY_EXPIRATION_MIN * 60:
                oid = tr.get("entry_order_id")
                if oid and oid != "DRY_RUN":
                    try:
                        self.cancel_entry(tr["symbol"], oid)
                        self.log.info(f"⏳ Canceled expired entry {tr['symbol']} ({tid})")
                    except Exception as e:
                        self.log.warning(f"Cancel failed {tr['symbol']} ({tid}): {e}")
                tr["status"] = "expired"
