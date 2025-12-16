import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from config import (
    CATEGORY, ACCOUNT_TYPE, QUOTE, LEVERAGE, RISK_PCT,
    ENTRY_EXPIRATION_MIN, ENTRY_TOO_FAR_PCT, ENTRY_TRIGGER_BUFFER_PCT, ENTRY_LIMIT_PRICE_OFFSET_PCT,
    ENTRY_EXPIRATION_PRICE_PCT,
    TP_SPLITS, DCA_QTY_MULTS, INITIAL_SL_PCT, FALLBACK_TP_PCT,
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
        self._instrument_cache: Dict[str, Dict[str, float]] = {}  # symbol -> rules
        self._cache_ttl = 300  # 5 min cache
        self._cache_times: Dict[str, float] = {}
        self._last_stats_day: str = ""

    # ---------- startup sync ----------
    def startup_sync(self) -> None:
        """Check for orphaned positions at startup and log warnings."""
        if DRY_RUN:
            self.log.info("DRY_RUN: Skipping startup sync")
            return

        try:
            # Get all open positions
            positions = self.bybit.positions(CATEGORY, "")  # Empty = all symbols
            open_positions = [p for p in positions if float(p.get("size") or 0) > 0]

            if not open_positions:
                self.log.info("✅ Startup sync: No open positions found")
                return

            # Check which positions are tracked in state
            tracked_symbols = set()
            for tr in self.state.get("open_trades", {}).values():
                if tr.get("status") in ("pending", "open"):
                    tracked_symbols.add(tr.get("symbol"))

            orphaned = []
            for pos in open_positions:
                symbol = pos.get("symbol")
                size = float(pos.get("size") or 0)
                side = pos.get("side")
                entry = float(pos.get("avgPrice") or 0)
                pnl = float(pos.get("unrealisedPnl") or 0)

                if symbol not in tracked_symbols:
                    orphaned.append(f"{symbol} ({side} {size} @ {entry}, PnL: {pnl:.2f})")

            if orphaned:
                self.log.warning(f"⚠️ Orphaned positions (not tracked by bot):")
                for o in orphaned:
                    self.log.warning(f"   → {o}")
                self.log.warning("   These positions will NOT be managed automatically!")
            else:
                self.log.info(f"✅ Startup sync: {len(open_positions)} position(s), all tracked")

        except Exception as e:
            self.log.warning(f"Startup sync failed: {e}")

    def log_daily_stats(self) -> None:
        """Log daily trade statistics once per day."""
        from state import utc_day_key
        today = utc_day_key()

        if self._last_stats_day == today:
            return  # Already logged today

        # Get yesterday's stats
        yesterday_trades = 0
        for tr in self.state.get("open_trades", {}).values():
            placed_ts = tr.get("placed_ts") or 0
            if placed_ts:
                trade_day = utc_day_key(placed_ts)
                if trade_day == self._last_stats_day:
                    yesterday_trades += 1

        if self._last_stats_day and yesterday_trades > 0:
            daily_count = self.state.get("daily_counts", {}).get(self._last_stats_day, 0)
            self.log.info(f"📊 Stats for {self._last_stats_day}: {daily_count} trades placed")

        self._last_stats_day = today

    # ---------- precision helpers ----------
    @staticmethod
    def _floor_to_step(x: float, step: float) -> float:
        if step <= 0:
            return x
        return math.floor(x / step) * step

    def _get_instrument_rules(self, symbol: str) -> Dict[str, float]:
        """Get instrument rules with caching to avoid repeated API calls."""
        now = time.time()
        cached_time = self._cache_times.get(symbol, 0)

        if symbol in self._instrument_cache and (now - cached_time) < self._cache_ttl:
            return self._instrument_cache[symbol]

        info = self.bybit.instruments_info(CATEGORY, symbol)
        lot = info.get("lotSizeFilter") or {}
        price_filter = info.get("priceFilter") or {}
        qty_step = float(lot.get("qtyStep") or lot.get("basePrecision") or "0.000001")
        min_qty  = float(lot.get("minOrderQty") or "0")
        tick_size = float(price_filter.get("tickSize") or "0.0001")

        rules = {"qty_step": qty_step, "min_qty": min_qty, "tick_size": tick_size}
        self._instrument_cache[symbol] = rules
        self._cache_times[symbol] = now
        return rules

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

        try:
            self.log.debug(f"Bybit place_order request: {body}")
            resp = self.bybit.place_order(body)
            self.log.debug(f"Bybit place_order response: {resp}")
            oid = (resp.get("result") or {}).get("orderId")
            if oid:
                self.log.info(f"✅ Bybit order created: {symbol} orderId={oid}")
            else:
                self.log.warning(f"⚠️ Bybit response has no orderId: {resp}")
            return oid
        except Exception as e:
            self.log.error(f"❌ Bybit place_order FAILED for {symbol}: {e}")
            return None

    def cancel_entry(self, symbol: str, order_id: str) -> None:
        body = {"category": CATEGORY, "symbol": symbol, "orderId": order_id}
        if DRY_RUN:
            self.log.info(f"DRY_RUN cancel entry: {body}")
            return
        self.bybit.cancel_order(body)

    def _generate_fallback_tps(self, entry: float, side: str, tick_size: float) -> List[float]:
        """Generate fallback TP prices based on % distance from entry."""
        tps = []
        for pct in FALLBACK_TP_PCT:
            if side == "Sell":  # SHORT: TPs are below entry
                tp = entry * (1 - pct / 100.0)
            else:  # LONG: TPs are above entry
                tp = entry * (1 + pct / 100.0)
            tps.append(self._round_price(tp, tick_size))
        return tps

    def place_post_entry_orders(self, trade: Dict[str, Any]) -> None:
        """Places SL + TP ladder + DCA conditionals after entry is filled."""
        symbol = trade["symbol"]
        side   = trade["order_side"]  # Buy/Sell
        entry  = float(trade["entry_price"])
        base_qty = float(trade["base_qty"])

        # Get instrument rules for price/qty rounding (cached)
        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]
        qty_step = rules["qty_step"]
        min_qty = rules["min_qty"]

        # ---- SL (position-level) ----
        sl_price = trade.get("sl_price")
        if sl_price is None:
            # Use configurable SL %
            sl_pct = INITIAL_SL_PCT / 100.0
            sl_price = entry * (1 + sl_pct) if side == "Sell" else entry * (1 - sl_pct)
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

        # Fallback TPs if signal has none
        if not tp_prices:
            tp_prices = self._generate_fallback_tps(entry, side, tick_size)
            self.log.info(f"Using fallback TPs for {symbol}: {tp_prices}")

        # Prepare all orders first, then place in parallel
        tp_orders = []
        dca_orders = []

        # Build TP orders
        tp_to_place = min(len(tp_prices), len(splits))
        for idx in range(tp_to_place):
            pct = float(splits[idx])
            if pct <= 0:
                continue
            tp = self._round_price(float(tp_prices[idx]), tick_size)
            qty = self._round_qty(size * (pct / 100.0), qty_step, min_qty)
            tp_orders.append({
                "idx": idx,
                "body": {
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
            })

        # Build DCA orders
        dca_prices: List[float] = trade.get("dca_prices") or []
        dca_to_place = min(len(dca_prices), len(DCA_QTY_MULTS))
        last = self.bybit.last_price(CATEGORY, symbol)

        for j in range(1, dca_to_place + 1):
            price = self._round_price(float(dca_prices[j-1]), tick_size)
            mult = DCA_QTY_MULTS[j-1]
            qty = self._round_qty(base_qty * mult, qty_step, min_qty)
            td = self._trigger_direction(last, price)
            dca_orders.append({
                "idx": j,
                "body": {
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
            })

        # Place orders in parallel for speed
        if DRY_RUN:
            for o in tp_orders:
                self.log.info(f"DRY_RUN TP{o['idx']+1}: {o['body']}")
                trade.setdefault("tp_order_ids", {})[str(o['idx']+1)] = f"DRY_TP{o['idx']+1}"
                if o['idx'] == 0:
                    trade["tp1_order_id"] = f"DRY_TP1"
            for o in dca_orders:
                self.log.info(f"DRY_RUN DCA{o['idx']}: {o['body']}")
        else:
            all_orders = [("TP", o) for o in tp_orders] + [("DCA", o) for o in dca_orders]

            def place_order(order_tuple):
                order_type, o = order_tuple
                resp = self.bybit.place_order(o["body"])
                return order_type, o["idx"], (resp.get("result") or {}).get("orderId")

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(place_order, o) for o in all_orders]
                for future in as_completed(futures):
                    try:
                        order_type, idx, oid = future.result()
                        if order_type == "TP":
                            trade.setdefault("tp_order_ids", {})[str(idx+1)] = oid
                            if idx == 0:
                                trade["tp1_order_id"] = oid
                    except Exception as e:
                        self.log.warning(f"Order placement failed: {e}")

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

    def _move_sl(self, symbol: str, sl_price: float, max_retries: int = 3) -> bool:
        """Move SL with retry logic for volatile markets."""
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
            return True

        for attempt in range(max_retries):
            try:
                self.bybit.set_trading_stop(body)
                return True
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log.warning(f"SL move attempt {attempt+1} failed for {symbol}: {e} - retrying in 100ms...")
                    time.sleep(0.1)  # 100ms wait
                else:
                    self.log.error(f"❌ SL move FAILED after {max_retries} attempts for {symbol}: {e}")
                    self.log.error(f"   Trade continues with original SL!")
                    return False
        return False

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
    def check_tp_fills_fallback(self) -> None:
        """Polling fallback: Check if TP1 was filled but WS missed it."""
        if DRY_RUN:
            return

        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") != "open":
                continue
            if not tr.get("post_orders_placed"):
                continue
            if tr.get("sl_moved_to_be"):
                continue  # Already moved

            # Check if TP1 order still exists
            tp1_oid = tr.get("tp1_order_id")
            if not tp1_oid:
                continue

            try:
                open_orders = self.bybit.open_orders(CATEGORY, tr["symbol"])
                tp1_still_open = any(o.get("orderId") == tp1_oid for o in open_orders)

                if not tp1_still_open:
                    # TP1 was filled (or cancelled) - move SL to BE
                    be = float(tr.get("entry_price") or tr.get("trigger"))
                    if self._move_sl(tr["symbol"], be):
                        tr["sl_moved_to_be"] = True
                        self.log.info(f"✅ SL -> BE (poll fallback) {tr['symbol']} @ {be}")
            except Exception as e:
                self.log.debug(f"TP fill check failed for {tr['symbol']}: {e}")

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
