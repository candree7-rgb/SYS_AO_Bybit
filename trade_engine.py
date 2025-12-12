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

    def _get_instrument_rules(self, symbol: str) -> Dict[str, float]:
        info = self.bybit.instruments_info(CATEGORY, symbol)
        lot = info.get("lotSizeFilter") or {}
        price_filter = info.get("priceFilter") or {}
        qty_step = float(lot.get("qtyStep") or lot.get("basePrecision") or "0.000001")
        min_qty  = float(lot.get("minOrderQty") or "0")
        tick_size = float(price_filter.get("tickSize") or "0.0001")
        return {"qty_step": qty_step, "min_qty": min_qty, "tick_size": tick_size}

    def _round_price(self, price: float, tick_size: float) -> float:
        """Round price to valid tick size."""
        if tick_size <= 0:
            return price
        return round(round(price / tick_size) * tick_size, 10)

    def _round_qty(self, qty: float, qty_step: float, min_qty: float) -> float:
        """Round qty down to valid step and ensure min qty."""
        qty = self._floor_to_step(qty, qty_step)
        if qty < min_qty:
            qty = min_qty
        return float(f"{qty:.10f}")

    def calc_base_qty(self, symbol: str, entry_price: float) -> float:
        # Risk model: margin = equity * RISK_PCT; notional = margin * LEVERAGE; qty = notional / price
        equity = self.bybit.wallet_equity(ACCOUNT_TYPE)
        margin = equity * (RISK_PCT / 100.0)
        notional = margin * LEVERAGE
        qty = notional / entry_price

        rules = self._get_instrument_rules(symbol)
        return self._round_qty(qty, rules["qty_step"], rules["min_qty"])

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

        # Get instrument rules for price/qty rounding
        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]

        # buffer: slightly earlier trigger if desired
        trigger_adj = trigger * (1 - ENTRY_TRIGGER_BUFFER_PCT / 100.0) if side == "Buy" else trigger * (1 + ENTRY_TRIGGER_BUFFER_PCT / 100.0)
        trigger_adj = self._round_price(trigger_adj, tick_size)

        # We use LIMIT conditional by default for exact pricing; optionally offset the limit to improve fill odds
        limit_price = trigger
        if ENTRY_LIMIT_PRICE_OFFSET_PCT != 0:
            off = abs(ENTRY_LIMIT_PRICE_OFFSET_PCT) / 100.0
            if side == "Sell":
                limit_price = trigger * (1 + off)
            else:
                limit_price = trigger * (1 - off)
        limit_price = self._round_price(limit_price, tick_size)

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

        # Get instrument rules for price/qty rounding
        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]
        qty_step = rules["qty_step"]
        min_qty = rules["min_qty"]

        # ---- SL (position-level) ----
        sl_price = trade.get("sl_price")
        if sl_price is None:
            # default: 19% style is signal-specific; if missing, do nothing
            sl_price = entry * (1 + 0.19) if side == "Sell" else entry * (1 - 0.19)
        sl_price = self._round_price(float(sl_price), tick_size)

        ts_body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "stopLoss": f"{sl_price:.10f}",
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
            tp = self._round_price(float(tp_prices[idx]), tick_size)
            qty = self._round_qty(size * (pct / 100.0), qty_step, min_qty)
            o = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": _opposite_side(side),
                "orderType": "Limit",
                "qty": f"{qty}",
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
        # Only place as many DCAs as we have multipliers configured
        dca_prices: List[float] = trade.get("dca_prices") or []
        dca_to_place = min(len(dca_prices), len(DCA_QTY_MULTS))
        last = self.bybit.last_price(CATEGORY, symbol)

        for j in range(1, dca_to_place + 1):
            price = self._round_price(float(dca_prices[j-1]), tick_size)
            mult = DCA_QTY_MULTS[j-1]
            qty = self._round_qty(base_qty * mult, qty_step, min_qty)
            td = self._trigger_direction(last, price)
            o = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": f"{qty}",
                "price": f"{price:.10f}",
                "timeInForce": "GTC",
                "triggerDirection": td,
                "triggerPrice": f"{price:.10f}",
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
        rules = self._get_instrument_rules(symbol)
        sl_price = self._round_price(sl_price, rules["tick_size"])
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "stopLoss": f"{sl_price:.10f}",
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

        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]

        if len(tp_prices) < tp_num:
            # fallback: use current market
            anchor = self.bybit.last_price(CATEGORY, symbol)
        else:
            anchor = float(tp_prices[tp_num-1])

        anchor = self._round_price(anchor, tick_size)
        dist = self._round_price(anchor * (TRAIL_DISTANCE_PCT / 100.0), tick_size)

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
            be_price = self._round_price(float(tr['entry_price']), tick_size)
            body["stopLoss"] = f"{be_price:.10f}"

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

    def cleanup_closed_trades(self) -> None:
        """Remove trades from state if position is closed (size = 0)."""
        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") not in ("open",):
                continue
            try:
                size, _ = self.position_size_avg(tr["symbol"])
                if size == 0:
                    tr["status"] = "closed"
                    tr["closed_ts"] = time.time()
                    self.log.info(f"✅ TRADE CLOSED {tr['symbol']} ({tid})")
            except Exception as e:
                self.log.warning(f"Cleanup check failed for {tr['symbol']}: {e}")

        # Prune old closed/expired trades (keep last 24h for reference)
        cutoff = time.time() - 86400
        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") in ("closed", "expired"):
                closed_at = tr.get("closed_ts") or tr.get("placed_ts") or 0
                if closed_at < cutoff:
                    del self.state["open_trades"][tid]
