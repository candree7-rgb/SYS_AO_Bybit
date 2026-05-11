import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import db_export
import telegram_alerts

from config import (
    CATEGORY, ACCOUNT_TYPE, QUOTE, LEVERAGE, RISK_PCT, BOT_ID,
    ENTRY_EXPIRATION_MIN, ENTRY_TOO_FAR_PCT, ENTRY_TRIGGER_BUFFER_PCT, ENTRY_LIMIT_PRICE_OFFSET_PCT,
    ENTRY_EXPIRATION_PRICE_PCT,
    TP_SPLITS, DCA_QTY_MULTS, INITIAL_SL_PCT, FALLBACK_TP_PCT,
    MOVE_SL_TO_BE_ON_TP1, BREAKEVEN_PROFIT_BUFFER_PCT,
    TRAIL_AFTER_TP_INDEX, TRAIL_DISTANCE_PCT, TRAIL_ACTIVATE_ON_TP,
    FIXED_RISK_PROFILE, FIXED_SL_PCT, FIXED_TP_PCTS,
    LEVERAGE_OVERRIDES,
    DRY_RUN
)

def _opposite_side(side: str) -> str:
    return "Sell" if side == "Buy" else "Buy"

def _pos_side(side: str) -> str:
    return "Long" if side == "Buy" else "Short"

class TradeEngine:
    def __init__(self, client, state: dict, logger, entry_watcher=None):
        # `client` is a BinanceFutures instance exposing the Bybit-shaped
        # API (place_order/cancel_order/positions/wallet_equity/etc.).
        # Attribute kept as `self.bybit` to minimise diffs in the methods
        # below; callers never need to know the underlying exchange.
        self.bybit = client
        self.state = state
        self.log = logger
        self.entry_watcher = entry_watcher
        self._instrument_cache: Dict[str, Dict[str, float]] = {}  # symbol -> rules
        self._cache_ttl = 300  # 5 min cache
        self._cache_times: Dict[str, float] = {}
        self._last_stats_day: str = ""
        # Symbols whose leverage we have already set this session
        self._leverage_set: set = set()

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

            # Hydrate the Binance client's SL pointer from open SL orders so
            # set_trading_stop (BE-move on TP1) works after a process
            # restart instead of falling through to the open-orders scan
            # path on every call. Identifies SL orders by either the
            # ":SL"/"|SL" suffix we use or the Binance stopOrderType field.
            if hasattr(self.bybit, "_sl_orders"):
                for pos in open_positions:
                    sym = pos.get("symbol")
                    if not sym:
                        continue
                    try:
                        for o in self.bybit.open_orders(CATEGORY, sym):
                            link = (o.get("orderLinkId") or "")
                            stop_type = (o.get("stopOrderType") or "")
                            if link.endswith(":SL") or link.endswith("|SL") or stop_type == "Stop":
                                oid = str(o.get("orderId", ""))
                                if oid:
                                    with self.bybit._sl_lock:
                                        self.bybit._sl_orders[sym] = oid
                                    self.log.info(f"♻️  Hydrated SL pointer: {sym} → {oid}")
                                break
                    except Exception as e:
                        self.log.debug(f"SL hydration failed for {sym}: {e}")

            # Log performance report at startup
            if self.state.get("trade_history"):
                self.log_performance_report()

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

            # Log full performance report once per day
            self.log_performance_report()

        # Update daily equity snapshot (always, even if no trades)
        if db_export.is_enabled():
            try:
                equity = self.bybit.wallet_equity(ACCOUNT_TYPE)
                # Count yesterday's closed trades (use yesterday, not today)
                yesterday_day = self._last_stats_day if self._last_stats_day else utc_day_key(time.time() - 86400)
                yesterday_closed_trades = sum(1 for tr in self.state.get("open_trades", {}).values()
                                            if utc_day_key(tr.get("closed_ts") or 0) == yesterday_day)
                yesterday_wins = sum(1 for tr in self.state.get("open_trades", {}).values()
                                   if utc_day_key(tr.get("closed_ts") or 0) == yesterday_day and tr.get("is_win"))
                yesterday_losses = yesterday_closed_trades - yesterday_wins
                db_export.update_daily_equity(equity, yesterday_closed_trades, yesterday_wins, yesterday_losses)
                self.log.debug(f"Updated daily equity: ${equity:.2f} ({yesterday_closed_trades} trades yesterday)")
            except Exception as e:
                self.log.debug(f"Failed to update daily equity: {e}")

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
        leverage_filter = info.get("leverageFilter") or {}
        qty_step = float(lot.get("qtyStep") or lot.get("basePrecision") or "0.000001")
        min_qty  = float(lot.get("minOrderQty") or "0")
        tick_size = float(price_filter.get("tickSize") or "0.0001")
        # Bybit returns this as a string like "12.5" or "100"; default to a
        # large value so unknown returns don't accidentally cap leverage.
        max_leverage = float(leverage_filter.get("maxLeverage") or "100")

        rules = {
            "qty_step": qty_step,
            "min_qty": min_qty,
            "tick_size": tick_size,
            "max_leverage": max_leverage,
        }
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

    def _effective_leverage(self, symbol: str):
        """Effective leverage = min(env LEVERAGE, override, exchange max).

        Resolution order:
          1. LEVERAGE_OVERRIDES env (manually configured per symbol)
          2. Bybit instrument-info maxLeverage (auto-detected, cached)
          3. fall back to env LEVERAGE
        Whichever is smallest wins — we never exceed what Bybit allows."""
        base = symbol.replace(QUOTE, "").upper()
        manual = LEVERAGE_OVERRIDES.get(base)
        try:
            exchange_max = self._get_instrument_rules(symbol).get("max_leverage", LEVERAGE)
        except Exception:
            exchange_max = LEVERAGE
        candidates = [LEVERAGE]
        if manual is not None:
            candidates.append(manual)
        candidates.append(exchange_max)
        return min(candidates)

    def _effective_risk_pct(self, symbol: str) -> float:
        """Scale risk_pct so that notional stays constant when leverage is
        capped below the env default. e.g. default 10%/20x → SIREN at 5x →
        40%/5x. Same notional exposure, same $-risk per trade."""
        eff_lev = self._effective_leverage(symbol)
        if eff_lev == LEVERAGE:
            return RISK_PCT
        # notional_default = RISK_PCT * LEVERAGE; keep equal:
        return RISK_PCT * LEVERAGE / eff_lev

    def calc_base_qty(self, symbol: str, entry_price: float) -> float:
        # Risk model: margin = equity * effective_risk_pct;
        #             notional = margin * effective_leverage;
        #             qty = notional / price
        equity = self.bybit.wallet_equity(ACCOUNT_TYPE)  # cached
        eff_risk = self._effective_risk_pct(symbol)
        eff_lev = self._effective_leverage(symbol)
        margin = equity * (eff_risk / 100.0)
        notional = margin * eff_lev
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

    def position_size_avg(self, symbol: str, fresh: bool = False) -> tuple[float, float]:
        # Prefer WS-cached position (sub-ms) over REST. Pass fresh=True for
        # safety-critical paths (orphan detection in cleanup_closed_trades)
        # where we need ground-truth from Bybit, not a possibly-stale cache.
        if not fresh:
            cached = self.bybit.get_cached_position(symbol)
            if cached is not None:
                return cached
        p = self._position(symbol)
        if not p:
            return 0.0, 0.0
        size = float(p.get("size") or 0)
        avg  = float(p.get("avgPrice") or 0)
        return size, avg

    # ---------- core actions ----------
    def place_conditional_entry(self, sig: Dict[str, Any], trade_id: str) -> Optional[str]:
        """Place-first model: minimum work in critical path.

        Skips: last_price call, too_far check, beyond_expiry check.
        The entry_watcher (Bybit public-WS ticker) cancels the order if TP1
        is crossed before the conditional fills — that's the safety net.

        Bybit calls in critical path (warm path, leverage cached, equity cached):
            place_order — that's it (~200ms total).
        Cold path (first trade on new symbol) adds set_leverage (~150ms).
        """
        symbol = sig["symbol"]
        side   = "Sell" if sig["side"] == "sell" else "Buy"
        trigger = float(sig["trigger"])

        # Symbol Locking: Check if another bot is already trading this symbol
        if db_export.is_enabled():
            active_trade = db_export.get_active_trade_for_symbol(symbol)
            if active_trade and active_trade.get("bot_id") != BOT_ID:
                other_bot = active_trade.get("bot_id")
                self.log.info(f"⏭️  SKIP {symbol} – already managed by bot '{other_bot}' (symbol locked)")
                return None

        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]

        # ── Apply fixed risk profile (overrides signal SL/TPs) ──────────────
        # Mutates `sig` so main.py persists the final values into trade state.
        if FIXED_RISK_PROFILE:
            if side == "Sell":
                sl_price = trigger * (1 + FIXED_SL_PCT / 100.0)
                tp_prices = [trigger * (1 - p / 100.0) for p in FIXED_TP_PCTS]
            else:
                sl_price = trigger * (1 - FIXED_SL_PCT / 100.0)
                tp_prices = [trigger * (1 + p / 100.0) for p in FIXED_TP_PCTS]
            sl_price = self._round_price(sl_price, tick_size)
            tp_prices = [self._round_price(p, tick_size) for p in tp_prices]
            sig["sl_price"] = sl_price
            sig["tp_prices"] = tp_prices
        else:
            sl_price = float(sig.get("sl_price") or 0) or None
            if sl_price:
                sl_price = self._round_price(sl_price, tick_size)

        # ── Cold-path: parallelize set_leverage + wallet_equity ────────────
        # On a fresh symbol both set_leverage and wallet_equity may be cold
        # (each ~150ms). They're independent — fire them in parallel via a
        # tiny thread pool. Warm path: both return instantly from cache.
        # SAFETY: if set_leverage fails (and it's not the "already set"
        # 110043 path), we ABORT the trade rather than place at unknown
        # leverage. Otherwise Bybit could open the position with the
        # account-default leverage (e.g. 10x when we wanted 5x for SIREN),
        # which silently breaks the qty calc and risks margin call.
        need_lev = symbol not in self._leverage_set and not DRY_RUN
        if need_lev:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_lev = ex.submit(self._set_leverage_safe, symbol)
                f_eq  = ex.submit(self.bybit.wallet_equity, ACCOUNT_TYPE)
                lev_ok = f_lev.result()
                # equity result discarded — cached for calc_base_qty below
                try:
                    f_eq.result()
                except Exception:
                    pass
                if not lev_ok:
                    self.log.error(
                        f"❌ ABORT {symbol}: set_leverage failed and we don't "
                        f"know what leverage Bybit will apply — refusing to place"
                    )
                    return None
                self._leverage_set.add(symbol)

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

        qty = self.calc_base_qty(symbol, trigger)  # uses cached equity (sub-ms warm)

        # Pre-flight: if market is already past TP1, abort BEFORE
        # submitting. Realistic-filter sweep showed this rejects ~40 % of
        # signals — most of which would still be profitable (price often
        # rallies back to trigger and the move continues to TP2/TP3).
        # Gate behind DISABLE_PREFLIGHT_TP1 so it can be toggled live.
        from config import DISABLE_PREFLIGHT_TP1
        if not DISABLE_PREFLIGHT_TP1:
            try:
                last = self._last_price(symbol)
            except Exception as e:
                self.log.warning(f"last_price lookup failed for {symbol}: {e} — skipping pre-flight check")
                last = None
            tps_for_check = sig.get("tp_prices") or []
            if last is not None and tps_for_check:
                tp1 = float(tps_for_check[0])
                already_past_tp1 = (
                    (side == "Sell" and last <= tp1)
                    or (side == "Buy" and last >= tp1)
                )
                if already_past_tp1:
                    self.log.info(
                        f"⏭️  SKIP {symbol}: market last={last} already past TP1={tp1} "
                        f"({'short' if side == 'Sell' else 'long'} would enter into instant loss)"
                    )
                    return None

        # Plain LIMIT order — no triggerPrice/triggerDirection.
        # Why not conditional?
        #   - Bybit's conditional requires triggerDirection match the market
        #     at place-time (110093 if mismatch). Volatile alts can drop
        #     0.4% in the 500ms place_order roundtrip → race.
        #   - For our use case (entry at trigger price OR better), plain
        #     LIMIT does the same thing: order sits in the orderbook,
        #     fills the moment the market matches our limit_price.
        #   - entry_watcher still cancels on TP1-cross before fill.
        # SHORT @ trigger 0.079341, market 0.07912:
        #   plain limit sell @ 0.079341 → waits for market to rise to 0.079341
        # SHORT @ trigger 0.079341, market 0.080:
        #   plain limit sell @ 0.079341 → fills IMMEDIATELY at best bid
        #   (which is >= 0.079341, so we sell at a BETTER price than trigger)
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": f"{qty:.10f}",
            "price": f"{limit_price:.10f}",
            "timeInForce": "GTC",
            "reduceOnly": False,
            "closeOnTrigger": False,
            "orderLinkId": trade_id,
        }

        # ── Inline stopLoss in the entry order ──────────────────────────────
        # Inline SL: Bybit attaches it to the position when the entry fills;
        # Binance translates this to a batchOrders [LIMIT, STOP_MARKET]
        # submitted in 1 RTT. The exchange may report SL leg failure even
        # though the entry succeeded — see slInlineOk handling below.
        sl_inline_requested = False
        if sl_price:
            body["stopLoss"] = f"{sl_price:.10f}"
            body["slTriggerBy"] = "LastPrice"
            body["tpslMode"] = "Full"
            sl_inline_requested = True
        sig["_base_qty"] = qty          # consumed by main.py — avoid recompute

        if DRY_RUN:
            self.log.info(f"DRY_RUN ENTRY {symbol}: {body}")
            sig["_sl_inline"] = sl_inline_requested
            return "DRY_RUN"

        try:
            self.log.debug(f"place_order request: {body}")
            resp = self.bybit.place_order(body)
            self.log.debug(f"place_order response: {resp}")
            result = resp.get("result") or {}
            oid = result.get("orderId")
            # The Binance client reports whether the inline SL leg actually
            # was accepted. Only mark the trade as "SL set" if it really is —
            # otherwise place_post_entry_orders will re-issue an SL after fill.
            if sl_inline_requested:
                sl_actually_inline = bool(result.get("slInlineOk", True))
                if not sl_actually_inline:
                    self.log.warning(
                        f"⚠️ {symbol}: inline SL leg of batchOrders failed — "
                        f"will re-set SL after entry fill"
                    )
                sig["_sl_inline"] = sl_actually_inline
            else:
                sig["_sl_inline"] = False
            if oid:
                if sig.get("_sl_inline"):
                    self.log.info(f"✅ Order created: {symbol} orderId={oid} (SL inline @ {sl_price})")
                else:
                    self.log.info(f"✅ Order created: {symbol} orderId={oid} (SL will be set post-fill)")
            else:
                self.log.warning(f"⚠️ Order response has no orderId: {resp}")
            return oid
        except Exception as e:
            self.log.error(f"❌ place_order FAILED for {symbol}: {e}")
            return None

    def _last_price(self, symbol: str) -> float:
        """Fetch last price, preferring entry_watcher's WS ticker cache.
        Falls back to REST. Subscribes the symbol on first miss so subsequent
        reads hit the WS cache (sub-ms)."""
        if self.entry_watcher is not None:
            cached = self.entry_watcher.get_last_price(symbol)
            if cached is not None:
                return cached
            try:
                self.entry_watcher.ensure_subscribed(symbol)
            except Exception:
                pass
        return self.bybit.last_price(CATEGORY, symbol)

    def _safe_equity_refresh(self):
        """Background equity cache refresh — swallows errors silently
        because the next REST call will retry anyway."""
        try:
            self.bybit.wallet_equity(ACCOUNT_TYPE, force_refresh=True)
        except Exception:
            pass

    def _set_leverage_safe(self, symbol: str) -> bool:
        """Set margin-mode (idempotent) + leverage. Treats Bybit's 110043
        (not modified) and Binance's -4045/-4046 (already set) as cached
        success. On Binance -4028 (invalid leverage) re-attempts with the
        requested value halved, since some symbols have leverage caps
        below LEVERAGE_OVERRIDES default and our exchangeInfo cache may
        be stale."""
        eff_lev = self._effective_leverage(symbol)
        if eff_lev != LEVERAGE:
            self.log.info(
                f"[engine] {symbol}: leverage override {eff_lev}x "
                f"(default {LEVERAGE}x), risk {self._effective_risk_pct(symbol):.1f}% "
                f"(default {RISK_PCT}%) — same notional"
            )
        # Margin-mode first (Binance rejects mode change once a position
        # exists). Bybit client doesn't have set_margin_mode — guarded.
        try:
            from config import MARGIN_MODE as _mm
            if hasattr(self.bybit, "set_margin_mode"):
                self.bybit.set_margin_mode(symbol, _mm)
        except Exception as e:
            self.log.debug(f"set_margin_mode {symbol}: {e}")

        # Retry-with-halving loop for Binance -4028. After 4 halvings
        # (20 → 10 → 5 → 2 → 1) we give up.
        attempt_lev = eff_lev
        for _ in range(5):
            try:
                self.bybit.set_leverage(CATEGORY, symbol, attempt_lev)
                if attempt_lev != eff_lev:
                    self.log.info(
                        f"[engine] {symbol}: clamped leverage to {attempt_lev}x "
                        f"(symbol cap below requested {eff_lev}x)"
                    )
                return True
            except Exception as e:
                msg = str(e)
                if "110043" in msg or "-4045" in msg or "-4046" in msg:
                    return True
                if "-4028" in msg and attempt_lev > 1:
                    new_lev = max(1, int(attempt_lev) // 2)
                    if new_lev == int(attempt_lev):
                        new_lev = max(1, int(attempt_lev) - 1)
                    attempt_lev = new_lev
                    continue
                self.log.warning(f"set_leverage failed for {symbol}: {e}")
                return False
        self.log.warning(f"set_leverage gave up for {symbol} after halving retries")
        return False

    def cancel_entry(self, symbol: str, order_id: str, trade_id: Optional[str] = None) -> None:
        body = {"category": CATEGORY, "symbol": symbol, "orderId": order_id}
        if DRY_RUN:
            self.log.info(f"DRY_RUN cancel entry: {body}")
        else:
            try:
                self.bybit.cancel_order(body)
            except Exception as e:
                self.log.debug(f"cancel_entry {symbol} {order_id}: {e}")
            # Also kill the inline SL that was placed alongside this entry
            # in the same batchOrders call. Without this it lingers as an
            # orphaned STOP_MARKET closePosition=true and would fire on a
            # subsequent trade for the same symbol (same-symbol race).
            if trade_id:
                try:
                    self.bybit.cancel_order({
                        "category": CATEGORY,
                        "symbol": symbol,
                        "orderLinkId": f"{trade_id}:SL",
                    })
                except Exception as e:
                    self.log.debug(f"orphan-SL cancel for {symbol}: {e}")
        if self.entry_watcher:
            # Pass trade_id so we don't accidentally clear watches for
            # OTHER pending trades on the same symbol (rare but real
            # when MAX_CONCURRENT_TRADES allows multiple).
            self.entry_watcher.unwatch(symbol, trade_id)

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

    def _place_trailing_stop_orders(self, trade: Dict[str, Any], side: str,
                                     entry: float, tick_size: float) -> None:
        """Submit ONE TRAILING_STOP_MARKET with closePosition=true.

        Independently of the trail, ensures a hard SL is on the position:
        if the inline-SL leg of the entry batchOrders failed (-2021 from a
        cross with mark, an exchange validation, etc.), we call
        set_trading_stop here so the position is never left unprotected.
        This is the "belt" — the trail is the "braces".
        """
        from config import TRAIL_ACTIVATION_PCT, TRAIL_CALLBACK_RATE, INITIAL_SL_PCT
        symbol = trade["symbol"]
        # SHORT (side=Sell): activation BELOW entry → trail arms when price
        # drops to it. LONG: activation ABOVE entry. callbackRate is the %
        # retracement from the post-activation extreme that fires market exit.
        if side == "Sell":
            activation = entry * (1 - TRAIL_ACTIVATION_PCT / 100.0)
        else:
            activation = entry * (1 + TRAIL_ACTIVATION_PCT / 100.0)
        activation = self._round_price(activation, tick_size)

        # Position size check — the entry must have filled before we can
        # arm a trail. Backstop in case the WS handler dispatches us early.
        size, _avg = self.position_size_avg(symbol)
        if size <= 0:
            self.log.warning(f"No position size yet for {symbol}; will retry trail-stop")
            return

        # ── Belt: ensure hard SL is on the position ──
        # If inline SL during entry batchOrders succeeded, trust it.
        # Otherwise issue set_trading_stop here, BEFORE attempting the trail,
        # so a failing trail can't leave the position unprotected.
        if not bool(trade.get("sl_set_inline")):
            sl_price = trade.get("sl_price")
            if not sl_price:
                # No signal SL stored — fall back to INITIAL_SL_PCT from entry.
                sl_pct = INITIAL_SL_PCT / 100.0
                sl_price = entry * (1 + sl_pct) if side == "Sell" else entry * (1 - sl_pct)
            sl_price = self._round_price(float(sl_price), tick_size)
            ts_body = {
                "category": CATEGORY,
                "symbol": symbol,
                "positionIdx": 0,
                "stopLoss": f"{sl_price:.10f}",
                "tpslMode": "Full",
                # Pass the position size we just measured for the trail so
                # set_trading_stop doesn't do its own (potentially racing)
                # re-read inside the binance adapter. Both legs of the
                # belt-and-braces protection now cover the exact same qty.
                "qty": f"{size}",
            }
            sl_armed = False
            try:
                if DRY_RUN:
                    self.log.info(f"DRY_RUN set SL (trail-mode fallback): {ts_body}")
                    sl_armed = True
                else:
                    resp = self.bybit.set_trading_stop(ts_body)
                    # set_trading_stop may silently no-op when the position
                    # cache lag / REST fail makes _closing_side return None:
                    # it then returns {"noPosition": True} with retCode 0.
                    # Treat that — and any response without orderId — as
                    # a failure so we don't falsely mark sl_set_inline.
                    result = (resp or {}).get("result") or {}
                    if result.get("noPosition"):
                        raise RuntimeError(
                            f"set_trading_stop returned noPosition for {symbol} — "
                            f"SL was NOT armed"
                        )
                    if not result.get("orderId"):
                        raise RuntimeError(
                            f"set_trading_stop returned no orderId for {symbol}: {result}"
                        )
                    sl_armed = True
                if sl_armed:
                    self.log.info(f"✅ Hard SL set @ {sl_price} (trail-mode fallback)")
                    trade["sl_set_inline"] = True
            except Exception as e:
                self.log.error(
                    f"🚨 CRITICAL: SL fallback FAILED for {symbol} @ {sl_price}: {e} — "
                    f"position may be unprotected if trail also fails"
                )
                try:
                    import telegram_alerts
                    telegram_alerts.send_message(
                        f"🚨 {symbol}: SL set FAILED ({type(e).__name__}: {e}) — "
                        f"check manually!"
                    )
                except Exception:
                    pass

        # ── Idempotency: don't place a second TRAIL on retry/restart ──
        if trade.get("trail_order_id"):
            self.log.info(f"Trail already armed for {symbol} (oid={trade['trail_order_id']}); skipping")
            trade["post_orders_placed"] = True
            return

        # ── Braces: trailing stop ──
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": _opposite_side(side),  # BUY closes SHORT, SELL closes LONG
            "qty": f"{size}",  # enables qty+reduceOnly fallback on -1106/-4136
            "trailingStop": TRAIL_CALLBACK_RATE,
            "activePrice": activation,
            "closeOnTrigger": True,  # maps to closePosition=true on Binance
            "orderLinkId": f"{trade['id']}:TRAIL",
        }
        self.log.info(
            f"📈 TRAIL placed for {symbol}: activation @ {activation} "
            f"({TRAIL_ACTIVATION_PCT}% from entry), callback={TRAIL_CALLBACK_RATE}%"
        )
        if DRY_RUN:
            self.log.info(f"DRY_RUN TRAIL: {body}")
            trade["trail_order_id"] = "DRY_RUN"
        else:
            try:
                resp = self.bybit.place_order(body)
                trail_oid = (resp.get("result") or {}).get("orderId")
                trade["trail_order_id"] = trail_oid
                self.log.info(f"✅ TRAIL armed: {symbol} orderId={trail_oid}")
            except Exception as e:
                self.log.error(
                    f"❌ Failed to place trail-stop for {symbol}: {e} — "
                    f"position protected by hard SL only"
                )
                try:
                    import telegram_alerts
                    telegram_alerts.send_message(
                        f"⚠️ {symbol}: TRAIL place failed ({e}). "
                        f"Hard SL still armed."
                    )
                except Exception:
                    pass

        # Only mark post-orders placed if AT LEAST ONE protection leg armed.
        # If both the hard-SL belt AND the trail failed (e.g. the algo place
        # exhausted its 3 retries on transient 5xx and surfaced as an
        # exception that we caught + telegram-alerted above), leaving this
        # flag False makes the main loop retry on the next tick
        # (main.py:821: `if status==open and not post_orders_placed:
        # place_post_entry_orders(tr)`). Without this gate a failed-retry
        # trade would be permanently stuck unprotected.
        # NB: sl_set_inline is set EITHER by the inline-SL on the entry
        # batchOrders (place_entry path) OR by the trail-mode fallback above
        # at line ~675; both mean a hard STOP_MARKET is live on the
        # position. trail_order_id is set only on successful trail place.
        sl_armed = bool(trade.get("sl_set_inline"))
        trail_armed = bool(trade.get("trail_order_id"))
        if sl_armed or trail_armed:
            trade["post_orders_placed"] = True
        else:
            self.log.error(
                f"🚨 {symbol}: NEITHER hard SL nor trail armed — leaving "
                f"post_orders_placed=False so main loop retries next tick"
            )
            try:
                import telegram_alerts
                telegram_alerts.send_message(
                    f"🚨 {symbol}: BOTH protection legs failed — will retry next tick"
                )
            except Exception:
                pass

    def place_post_entry_orders(self, trade: Dict[str, Any]) -> None:
        """Places SL + TP ladder + DCA conditionals after entry is filled.

        OPTIMIZED: Gets position size first, then places SL + TPs + DCAs in parallel.
        """
        symbol = trade["symbol"]
        side   = trade["order_side"]  # Buy/Sell
        entry  = float(trade["entry_price"])
        base_qty = float(trade["base_qty"])

        # Get instrument rules for price/qty rounding (cached)
        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]
        qty_step = rules["qty_step"]
        min_qty = rules["min_qty"]

        # ── Trailing-stop strategy ─────────────────────────────────────────
        # When USE_TRAIL_AFTER_TP1 is set we skip the TP1/TP2/TP3 ladder
        # entirely and submit a single Binance TRAILING_STOP_MARKET that
        # arms at TP1-distance and trails the lowest mark price thereafter.
        # The initial SL from the entry batchOrders stays armed as the
        # pre-activation fallback. Tick-precise backtest (verified
        # slippage from real aggTrades): +12.25 % EV / sig at trail=0.3 %.
        from config import USE_TRAIL_AFTER_TP1, TRAIL_ACTIVATION_PCT, TRAIL_CALLBACK_RATE
        if USE_TRAIL_AFTER_TP1:
            return self._place_trailing_stop_orders(trade, side, entry, tick_size)

        # ---- Get position size FIRST (needed for TP quantities) ----
        size, _avg = self.position_size_avg(symbol)
        if size <= 0:
            # sometimes position size appears a bit later; retry via main loop
            self.log.warning(f"No position size yet for {symbol}; will retry post-orders")
            return

        # ---- Calculate SL price ----
        # If the SL was already attached inline to the entry order
        # (FIXED_RISK_PROFILE or signal had its own SL), Bybit picks it up
        # automatically when the conditional fills. Skip the redundant
        # set_trading_stop call (~150ms saved).
        sl_already_inline = bool(trade.get("sl_set_inline"))
        if sl_already_inline:
            sl_price = float(trade.get("sl_price") or 0)
            self.log.info(f"📍 SL inline from entry order @ {sl_price} (skipping set_trading_stop)")
        else:
            sl_pct = INITIAL_SL_PCT / 100.0
            sl_price = entry * (1 + sl_pct) if side == "Sell" else entry * (1 - sl_pct)
            sl_price = self._round_price(sl_price, tick_size)
            self.log.info(f"📍 SL at {INITIAL_SL_PCT}% from entry: {sl_price}")

        tp_prices: List[float] = trade.get("tp_prices") or []
        splits: List[float] = trade.get("tp_splits") or TP_SPLITS

        # Fallback TPs if signal has none
        if not tp_prices:
            tp_prices = self._generate_fallback_tps(entry, side, tick_size)
            self.log.info(f"Using fallback TPs for {symbol}: {tp_prices}")

        # Store original TP percentages for recalculation after DCA
        tp_percentages = []
        for tp in tp_prices:
            if side == "Buy":  # Long: TP is above entry
                pct = (float(tp) / entry - 1)
            else:  # Short: TP is below entry
                pct = (1 - float(tp) / entry)
            tp_percentages.append(pct)
        trade["tp_percentages"] = tp_percentages

        # Prepare all orders first, then place in parallel
        tp_orders = []
        dca_orders = []

        # Build TP orders
        tp_to_place = min(len(tp_prices), len(splits))
        self.log.info(f"📊 Placing {tp_to_place} TPs (splits: {splits[:tp_to_place]}, remaining {100-sum(splits[:tp_to_place]):.0f}% runner)")
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
        self.log.info(f"📊 Placing {dca_to_place} DCAs (mults: {DCA_QTY_MULTS[:dca_to_place]})")
        last = self._last_price(symbol)

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

        # Place SL + TPs + DCAs in parallel for speed
        ts_body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "stopLoss": f"{sl_price:.10f}",
            "tpslMode": "Full",
        }

        if DRY_RUN:
            self.log.info(f"DRY_RUN set SL: {ts_body}")
            for o in tp_orders:
                self.log.info(f"DRY_RUN TP{o['idx']+1}: {o['body']}")
                trade.setdefault("tp_order_ids", {})[str(o['idx']+1)] = f"DRY_TP{o['idx']+1}"
                if o['idx'] == 0:
                    trade["tp1_order_id"] = f"DRY_TP1"
            for o in dca_orders:
                self.log.info(f"DRY_RUN DCA{o['idx']}: {o['body']}")
        else:
            # Build list of all operations to run in parallel
            all_orders = [("TP", o) for o in tp_orders] + [("DCA", o) for o in dca_orders]

            def place_order(order_tuple):
                order_type, o = order_tuple
                resp = self.bybit.place_order(o["body"])
                return order_type, o["idx"], (resp.get("result") or {}).get("orderId")

            def set_sl():
                self.bybit.set_trading_stop(ts_body)
                return "SL", 0, None

            # Run SL (if not inline) + all orders in parallel
            with ThreadPoolExecutor(max_workers=6) as executor:
                sl_future = None
                if not sl_already_inline:
                    sl_future = executor.submit(set_sl)
                order_futures = [executor.submit(place_order, o) for o in all_orders]

                if sl_future is not None:
                    try:
                        sl_future.result()
                        self.log.info(f"✅ SL set successfully")
                    except Exception as e:
                        self.log.warning(f"SL setting failed: {e}")

                # Process order results
                for future in as_completed(order_futures):
                    try:
                        order_type, idx, oid = future.result()
                        if order_type == "TP":
                            trade.setdefault("tp_order_ids", {})[str(idx+1)] = oid
                            if idx == 0:
                                trade["tp1_order_id"] = oid
                    except Exception as e:
                        self.log.warning(f"Order placement failed: {e}")

        trade["post_orders_placed"] = True

    def _recalculate_tps_after_dca(self, trade: Dict[str, Any]) -> None:
        """Recalculates and replaces unfilled TPs after DCA fill.

        Uses Bybit's avgPrice (automatically calculated) and original TP percentages
        to place new TPs at correct distances from the new average entry.
        """
        symbol = trade["symbol"]
        side = trade["order_side"]

        # Get new average entry from Bybit position
        size, new_avg = self.position_size_avg(symbol)
        if size <= 0 or new_avg <= 0:
            self.log.warning(f"Cannot recalculate TPs: no position data for {symbol}")
            return

        # Get original TP percentages
        tp_percentages = trade.get("tp_percentages", [])
        if not tp_percentages:
            self.log.debug(f"No TP percentages stored, cannot recalculate")
            return

        # Get unfilled TPs
        filled_tps = trade.get("tp_fills_list", [])
        tp_order_ids = trade.get("tp_order_ids", {})
        splits = trade.get("tp_splits") or TP_SPLITS

        # Get instrument rules
        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]
        qty_step = rules["qty_step"]
        min_qty = rules["min_qty"]

        # Track version for unique orderLinkId (avoids conflicts)
        tp_version = trade.get("tp_version", 1) + 1
        trade["tp_version"] = tp_version

        old_entry = trade.get("entry_price", new_avg)
        self.log.info(f"🔄 Recalculating TPs: entry {old_entry:.4f} → avg {new_avg:.4f}")

        # Calculate new TP prices and replace unfilled TPs
        new_tp_prices = []
        for i, pct in enumerate(tp_percentages):
            tp_num = i + 1

            # Calculate new TP price based on original percentage
            if side == "Buy":  # Long
                new_tp_price = new_avg * (1 + pct)
            else:  # Short
                new_tp_price = new_avg * (1 - pct)
            new_tp_prices.append(new_tp_price)

            # Skip already filled TPs
            if tp_num in filled_tps:
                continue

            # Skip if no split for this TP
            if i >= len(splits) or splits[i] <= 0:
                continue

            # Cancel existing TP order
            old_order_id = tp_order_ids.get(str(tp_num))
            if old_order_id:
                try:
                    self.bybit.cancel_order({
                        "category": CATEGORY,
                        "symbol": symbol,
                        "orderId": old_order_id
                    })
                    self.log.debug(f"Cancelled old TP{tp_num} order {old_order_id}")
                except Exception as e:
                    self.log.debug(f"Failed to cancel TP{tp_num}: {e}")

            # Place new TP order
            new_tp = self._round_price(new_tp_price, tick_size)
            qty = self._round_qty(size * (splits[i] / 100.0), qty_step, min_qty)

            body = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": _opposite_side(side),
                "orderType": "Limit",
                "qty": f"{qty}",
                "price": f"{new_tp:.10f}",
                "timeInForce": "GTC",
                "reduceOnly": True,
                "closeOnTrigger": False,
                "orderLinkId": f"{trade['id']}:TP{tp_num}v{tp_version}",
            }

            if DRY_RUN:
                self.log.info(f"DRY_RUN new TP{tp_num}: {new_tp}")
                continue

            try:
                resp = self.bybit.place_order(body)
                new_oid = (resp.get("result") or {}).get("orderId")
                if new_oid:
                    tp_order_ids[str(tp_num)] = new_oid
                    if tp_num == 1:
                        trade["tp1_order_id"] = new_oid
                    self.log.info(f"   TP{tp_num}: {new_tp:.4f} (was {tp_percentages[i]*100:+.2f}% from entry)")
            except Exception as e:
                self.log.warning(f"Failed to place new TP{tp_num}: {e}")

        # Update trade's TP prices and average entry
        trade["tp_prices"] = new_tp_prices
        trade["avg_entry"] = new_avg

    # ---------- reactive events ----------
    def on_execution(self, ev: Dict[str, Any]) -> None:
        # Called from Bybit private WS thread. Hold state_lock around the
        # whole method so concurrent main-loop / fast_signal_handler
        # readers don't see partial mutations and save_state never
        # serializes a half-updated dict.
        from state import state_lock as _state_lock
        with _state_lock:
            self._on_execution_locked(ev)

    def _on_execution_locked(self, ev: Dict[str, Any]) -> None:
        link = ev.get("orderLinkId") or ev.get("orderLinkID") or ""
        if not link:
            return

        # Entry filled?
        if link in self.state.get("open_trades", {}):
            tr = self.state["open_trades"][link]
            if tr.get("status") == "pending":
                # Only act on a FULLY filled entry. Partial fills generate
                # multiple TRADE events; if we placed TPs after the first
                # partial, the TP qty would be sized to half the position
                # and the rest would drift unprotected. Bybit and Binance
                # both publish status="Filled" / "FILLED" only on full
                # completion. Anything else (PartiallyFilled / PARTIALLY_FILLED)
                # is ignored — we wait for the full-fill event.
                exec_status = (ev.get("orderStatus") or ev.get("execStatus") or "").upper()
                if exec_status not in ("FILLED", ""):
                    # Empty status = legacy Bybit shape; trust it.
                    self.log.debug(
                        f"[entry] partial fill {tr['symbol']} status={exec_status} — waiting for FILLED"
                    )
                    return
                # Prefer the avg fill price (avgPrice) over the last-trade
                # price (lastPrice/execPrice) so partial sequences resolve to
                # the VWAP not the last tick. trigger is the last-resort
                # fallback when the venue did not echo any price.
                exec_price = (
                    ev.get("avgPrice")
                    or ev.get("execPrice")
                    or ev.get("lastPrice")
                    or ev.get("price")
                    or tr.get("trigger")
                )
                try:
                    tr["entry_price"] = float(exec_price)
                except Exception:
                    pass
                tr["status"] = "open"
                tr["filled_ts"] = time.time()
                # Initialize tracking fields
                tr.setdefault("dca_fills", 0)
                tr.setdefault("tp_fills", 0)
                tr.setdefault("tp_fills_list", [])
                self.log.info(f"✅ ENTRY FILLED {tr['symbol']} @ {tr.get('entry_price')}")
                # Stop watching this entry — it's filled, no longer pending.
                if self.entry_watcher:
                    self.entry_watcher.unwatch(tr["symbol"], tr.get("id"))

                # Send Telegram notification
                telegram_alerts.send_trade_opened(
                    symbol=tr["symbol"],
                    side=tr["order_side"],
                    entry=tr.get("entry_price", 0),
                    qty=tr.get("base_qty", 0),
                )

                # Place post-entry orders IMMEDIATELY (SL, TPs, DCAs)
                try:
                    self.place_post_entry_orders(tr)
                except Exception as e:
                    self.log.warning(f"Post-entry orders failed (will retry in main loop): {e}")
            return

        # DCA fills: orderLinkId pattern "<trade_id>:DCA1"
        if ":DCA" in link:
            trade_id, dca_tag = link.split(":", 1)
            tr = self.state.get("open_trades", {}).get(trade_id)
            if not tr:
                return
            import re as _re
            m = _re.search(r"DCA(\d+)", dca_tag)
            if m:
                dca_num = int(m.group(1))
                # Track DCA fill (avoid double counting)
                filled_dcas = tr.get("dca_fills_list", [])
                if dca_num not in filled_dcas:
                    filled_dcas.append(dca_num)
                    tr["dca_fills_list"] = filled_dcas
                    tr["dca_fills"] = len(filled_dcas)
                    dca_count = len(DCA_QTY_MULTS)
                    self.log.info(f"📈 DCA{dca_num} FILLED {tr['symbol']} ({tr['dca_fills']}/{dca_count})")

                    # Recalculate TPs based on new average entry
                    try:
                        self._recalculate_tps_after_dca(tr)
                    except Exception as e:
                        self.log.warning(f"TP recalculation failed after DCA{dca_num}: {e}")

                    # Send Telegram notification for DCA filled
                    try:
                        _, avg_entry = self.position_size_avg(tr["symbol"])
                        telegram_alerts.send_dca_filled(
                            symbol=tr["symbol"],
                            side=tr["order_side"],
                            dca_num=dca_num,
                            dca_fills=tr["dca_fills"],
                            dca_count=dca_count,
                            avg_entry=avg_entry if avg_entry else float(tr.get("entry_price") or 0)
                        )
                    except Exception as e:
                        self.log.debug(f"Failed to send DCA telegram alert: {e}")
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

            # Track TP fill (avoid double counting)
            filled_tps = tr.get("tp_fills_list", [])
            if tp_num not in filled_tps:
                filled_tps.append(tp_num)
                tr["tp_fills_list"] = filled_tps
                tr["tp_fills"] = len(filled_tps)
                tp_count = len(tr.get("tp_prices") or FALLBACK_TP_PCT)
                self.log.info(f"🎯 TP{tp_num} HIT {tr['symbol']} ({tr['tp_fills']}/{tp_count})")

            # TP1 -> SL to BE (use avg_entry if DCAs filled, otherwise entry_price)
            if MOVE_SL_TO_BE_ON_TP1 and tp_num == 1 and not tr.get("sl_moved_to_be"):
                be = float(tr.get("avg_entry") or tr.get("entry_price") or tr.get("trigger"))

                # Add profit buffer to cover fees (Long: +buffer, Short: -buffer)
                side = tr["order_side"]  # Buy or Sell
                if side == "Buy":
                    be_with_buffer = be * (1 + BREAKEVEN_PROFIT_BUFFER_PCT / 100.0)
                else:  # Sell
                    be_with_buffer = be * (1 - BREAKEVEN_PROFIT_BUFFER_PCT / 100.0)

                self._move_sl(tr["symbol"], be_with_buffer)
                tr["sl_moved_to_be"] = True
                self.log.info(f"✅ SL -> BE+buffer {tr['symbol']} @ {be_with_buffer:.6f} (entry: {be:.6f}, buffer: {BREAKEVEN_PROFIT_BUFFER_PCT}%)")

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
                resp = self.bybit.set_trading_stop(body)
                # set_trading_stop returns {"result": {"noPosition": True}}
                # when the position cache reports size=0 — that means the
                # SL was NOT armed (or the position was already closed).
                # Treat as failure and retry; the last-attempt path will
                # log + return False just like an exception would.
                result = (resp or {}).get("result") or {}
                if result.get("noPosition"):
                    if attempt < max_retries - 1:
                        self.log.warning(
                            f"SL move attempt {attempt+1}: noPosition for {symbol} "
                            f"— retrying in 100ms..."
                        )
                        time.sleep(0.1)
                        continue
                    self.log.warning(
                        f"⚠️ SL move {symbol}: position size = 0, no SL armed "
                        f"(position already closed?)"
                    )
                    return False
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
        """Start trailing stop after TPn is hit.

        For Bybit V5:
        - activePrice: price at which trailing activates
        - trailingStop: distance to trail behind price

        For SHORT: activePrice must be BELOW current price to activate later,
                   OR we skip activePrice if price already passed the level.
        For LONG: activePrice must be ABOVE current price to activate later.
        """
        symbol = tr["symbol"]
        side = tr["order_side"]  # Buy/Sell
        tp_prices = tr.get("tp_prices") or []

        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]

        # Get current market price
        current_price = self._last_price(symbol)

        if len(tp_prices) < tp_num:
            anchor = current_price
        else:
            anchor = float(tp_prices[tp_num-1])

        anchor = self._round_price(anchor, tick_size)
        dist = self._round_price(anchor * (TRAIL_DISTANCE_PCT / 100.0), tick_size)

        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
            "trailingStop": f"{dist:.10f}",
        }

        # Only set activePrice if price hasn't already passed the activation level
        # For SHORT: activePrice should be below current price (activate when dropping further)
        # For LONG: activePrice should be above current price (activate when rising further)
        if side == "Sell":  # SHORT
            if anchor < current_price:
                # Price hasn't reached anchor yet - set activation price
                body["activePrice"] = f"{anchor:.10f}"
            # else: price already at/past anchor - don't set activePrice, activate immediately
        else:  # LONG
            if anchor > current_price:
                # Price hasn't reached anchor yet - set activation price
                body["activePrice"] = f"{anchor:.10f}"
            # else: price already at/past anchor - don't set activePrice, activate immediately

        # keep SL at BE if already moved; otherwise keep existing stopLoss unchanged
        if tr.get("sl_moved_to_be"):
            be_price = float(tr.get("avg_entry") or tr.get("entry_price") or tr.get("trigger"))
            be_price = self._round_price(be_price, tick_size)
            body["stopLoss"] = f"{be_price:.10f}"

        if DRY_RUN:
            self.log.info(f"DRY_RUN set trailing: {body}")
            return

        try:
            self.bybit.set_trading_stop(body)
            self.log.info(f"🔄 Trailing started for {symbol} (dist: {dist})")
        except Exception as e:
            self.log.warning(f"Failed to set trailing for {symbol}: {e}")

    # ---------- maintenance ----------
    def check_tp_fills_fallback(self) -> None:
        """Polling fallback: Check if TP1 was filled OR price went through TP1 level.

        This handles two scenarios:
        1. TP1 order was filled but WebSocket missed the event
        2. Price shot through TP1 so fast the limit order wasn't filled

        In both cases, we should move SL to BE.

        Skipped entirely when USE_TRAIL_AFTER_TP1 is on — the bot's
        trailing-stop arms server-side at TP1 distance and replaces the
        BE-move semantics. A BE-move here would cancel the initial SL,
        leaving the trail-stop alone (which is fine until it activates)
        but creating an unnecessary order replace and breaking the
        "two-protection-orders-armed" invariant.
        """
        if DRY_RUN:
            return
        from config import USE_TRAIL_AFTER_TP1
        if USE_TRAIL_AFTER_TP1:
            return

        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") != "open":
                continue
            if not tr.get("post_orders_placed"):
                continue
            if tr.get("sl_moved_to_be"):
                continue  # Already moved

            symbol = tr["symbol"]
            side = tr["order_side"]  # Buy/Sell
            tp_prices = tr.get("tp_prices") or []

            if not tp_prices:
                continue

            tp1_price = float(tp_prices[0])
            should_move_to_be = False

            # Check 1: Did TP1 order get filled?
            tp1_oid = tr.get("tp1_order_id")
            if tp1_oid:
                try:
                    open_orders = self.bybit.open_orders(CATEGORY, symbol)
                    tp1_still_open = any(o.get("orderId") == tp1_oid for o in open_orders)
                    if not tp1_still_open:
                        should_move_to_be = True
                        self.log.debug(f"TP1 order no longer open for {symbol}")
                except Exception as e:
                    self.log.debug(f"TP1 order check failed for {symbol}: {e}")

            # Check 2: Did price go THROUGH TP1 level? (even if order wasn't filled)
            if not should_move_to_be:
                try:
                    current_price = self._last_price(symbol)
                    if side == "Buy":  # LONG: TP1 is above entry
                        if current_price >= tp1_price:
                            should_move_to_be = True
                            self.log.info(f"📈 Price passed TP1 level for {symbol} ({current_price} >= {tp1_price})")
                    else:  # SHORT: TP1 is below entry
                        if current_price <= tp1_price:
                            should_move_to_be = True
                            self.log.info(f"📉 Price passed TP1 level for {symbol} ({current_price} <= {tp1_price})")
                except Exception as e:
                    self.log.debug(f"Price check failed for {symbol}: {e}")

            if should_move_to_be:
                be = float(tr.get("avg_entry") or tr.get("entry_price") or tr.get("trigger"))
                if self._move_sl(symbol, be):
                    tr["sl_moved_to_be"] = True
                    # Also track TP1 as filled if not already
                    if 1 not in tr.get("tp_fills_list", []):
                        tr.setdefault("tp_fills_list", []).append(1)
                        tr["tp_fills"] = len(tr["tp_fills_list"])
                    self.log.info(f"✅ SL -> BE (fallback) {symbol} @ {be}")

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
                        # Send Telegram notification
                        telegram_alerts.send_order_canceled(
                            symbol=tr["symbol"],
                            side=tr["order_side"],
                            reason=f"Entry expired ({ENTRY_EXPIRATION_MIN} min)"
                        )
                    except Exception as e:
                        self.log.warning(f"Cancel failed {tr['symbol']} ({tid}): {e}")
                tr["status"] = "expired"

    def check_entry_order_validity(self) -> None:
        """Cancel entry orders if TP1 was already reached before entry filled.

        This prevents entries from being filled AFTER the signal has already
        moved past TP1 (signal is "expired").
        """
        pending_entries = [tr for tr in self.state.get("open_trades", {}).values() if tr.get("status") == "pending"]
        if not pending_entries:
            return

        self.log.debug(f"Checking {len(pending_entries)} pending entry order(s) for TP1 validity...")

        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") != "pending":
                continue

            symbol = tr["symbol"]
            side = tr["order_side"]  # Buy/Sell
            tp_prices = tr.get("tp_prices") or []

            if not tp_prices:
                self.log.debug(f"   {symbol}: No TP prices, skipping TP1 check")
                continue

            tp1_price = float(tp_prices[0])

            try:
                current_price = self._last_price(symbol)
                if not current_price:
                    self.log.warning(f"   {symbol}: Could not fetch current price for TP1 check")
                    continue

                tp1_reached = False

                if side == "Buy":  # LONG: TP1 is above entry
                    if current_price >= tp1_price:
                        tp1_reached = True
                        self.log.info(f"📈 TP1 reached before entry for {symbol} ({current_price} >= {tp1_price})")
                else:  # SHORT: TP1 is below entry
                    if current_price <= tp1_price:
                        tp1_reached = True
                        self.log.info(f"📉 TP1 reached before entry for {symbol} ({current_price} <= {tp1_price})")

                if tp1_reached:
                    # Cancel entry order
                    oid = tr.get("entry_order_id")
                    if oid and oid != "DRY_RUN":
                        try:
                            self.cancel_entry(symbol, oid)
                            self.log.info(f"🚫 Canceled entry order for {symbol} - TP1 already reached")

                            # Send Telegram notification
                            telegram_alerts.send_order_canceled(
                                symbol=symbol,
                                side=side,
                                reason=f"TP1 reached before entry (Current: ${current_price:.6f}, TP1: ${tp1_price:.6f})"
                            )
                        except Exception as e:
                            self.log.warning(f"Failed to cancel entry for {symbol}: {e}")

                    tr["status"] = "cancelled_tp1_reached"

            except Exception as e:
                self.log.warning(f"Entry validity check failed for {symbol}: {e}")

    def check_position_alerts(self) -> None:
        """Check all open positions and send Telegram alerts if thresholds crossed."""
        if not telegram_alerts.is_enabled():
            return

        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") != "open":
                continue

            symbol = tr["symbol"]
            side = tr["order_side"]
            avg_entry = float(tr.get("avg_entry") or tr.get("entry_price") or 0)

            if not avg_entry:
                continue

            try:
                current_price = self._last_price(symbol)
                if not current_price:
                    continue

                telegram_alerts.check_position_alerts(
                    trade_id=tid,
                    symbol=symbol,
                    side=side,
                    avg_entry=avg_entry,
                    current_price=current_price,
                    leverage=LEVERAGE,
                    dca_fills=tr.get("dca_fills", 0),
                    dca_count=len(DCA_QTY_MULTS),
                )
            except Exception as e:
                self.log.debug(f"Position alert check failed for {symbol}: {e}")

    def cleanup_closed_trades(self) -> None:
        """Remove trades from state if position is closed (size = 0).

        CRITICAL: This function must ensure positions are TRULY closed before
        canceling protection orders. If a position is still open after cleanup,
        we force-close it to prevent unprotected positions.
        """
        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") not in ("open",):
                continue
            try:
                symbol = tr["symbol"]
                size, _ = self.position_size_avg(symbol)

                if size == 0:
                    # Position closed - cancel all pending orders for this trade!
                    self._cancel_all_trade_orders(tr)

                    # SAFETY CHECK: Verify position is REALLY closed after canceling orders.
                    # fresh=True forces a Bybit REST call here (not WS cache) — the WS
                    # event for the close might be in-flight while we read, leading us
                    # to falsely conclude the position is closed.
                    size_verify, _ = self.position_size_avg(tr["symbol"], fresh=True)
                    if size_verify > 0:
                        self.log.error(f"🚨 CRITICAL: Position {tr['symbol']} still open ({size_verify}) after cleanup!")
                        self.log.error(f"   Forcing MARKET CLOSE to protect position...")

                        # Force close the remaining position
                        if not DRY_RUN:
                            try:
                                side = "Buy" if tr["order_side"] == "Sell" else "Sell"  # Opposite side to close
                                self.bybit.place_order({
                                    "category": CATEGORY,
                                    "symbol": tr["symbol"],
                                    "side": side,
                                    "orderType": "Market",
                                    "qty": f"{size_verify}",
                                    "reduceOnly": True,
                                    "closeOnTrigger": True,
                                })
                                self.log.info(f"✅ Emergency position close executed for {tr['symbol']}")
                                # Small delay to allow close to execute
                                time.sleep(0.5)
                            except Exception as e:
                                self.log.error(f"❌ FAILED to force close {tr['symbol']}: {e}")
                                self.log.error(f"   ⚠️ MANUAL INTERVENTION REQUIRED!")
                                # Page operator via Telegram — this is the
                                # last-ditch close attempt for an orphan
                                # position; if it fails the position is
                                # running unprotected until manually closed.
                                try:
                                    import telegram_alerts
                                    telegram_alerts.send_message(
                                        f"🚨 {tr['symbol']}: EMERGENCY CLOSE FAILED ({e}). "
                                        f"Position may be open and unprotected — "
                                        f"close manually in Binance NOW."
                                    )
                                except Exception:
                                    pass
                                # Don't mark trade as closed if we couldn't close the position
                                continue

                    tr["status"] = "closed"
                    tr["closed_ts"] = time.time()

                    # Fetch final PnL from Bybit
                    self._fetch_and_store_trade_stats(tr)

                    # Export to Database IMMEDIATELY (not waiting for archive)
                    if db_export.is_enabled():
                        self._export_trade_to_db(tr)

                    # Background-refresh equity cache so the NEXT signal's
                    # qty calc uses the fresh post-trade balance (not the
                    # stale 60s-old cached value). Fire-and-forget thread,
                    # doesn't block the maintenance loop.
                    import threading as _t
                    _t.Thread(
                        target=lambda: self._safe_equity_refresh(),
                        daemon=True,
                    ).start()

                    # Send Telegram notification
                    telegram_alerts.send_trade_closed(
                        symbol=tr["symbol"],
                        side=tr["order_side"],
                        pnl=tr.get("realized_pnl", 0),
                        exit_reason=tr.get("exit_reason", "unknown"),
                        tp_fills=tr.get("tp_fills", 0),
                        dca_fills=tr.get("dca_fills", 0),
                    )
                    telegram_alerts.clear_alerts_for_trade(tid)

                    self.log.info(f"✅ TRADE CLOSED {tr['symbol']} ({tid})")
            except Exception as e:
                self.log.warning(f"Cleanup check failed for {tr['symbol']}: {e}")

        # Prune old closed/expired trades (keep last 24h for reference)
        cutoff = time.time() - 86400
        for tid, tr in list(self.state.get("open_trades", {}).items()):
            if tr.get("status") in ("closed", "expired"):
                closed_at = tr.get("closed_ts") or tr.get("placed_ts") or 0
                if closed_at < cutoff:
                    # Move to trade_history before deleting
                    self._archive_trade(tr)
                    del self.state["open_trades"][tid]

    def _cancel_all_trade_orders(self, trade: Dict[str, Any]) -> None:
        """Cancel all pending orders for a closed trade — DCA, TP, SL, TRAIL.

        Must sweep BOTH endpoints since the algo-order migration: algo SL
        and algo TRAIL do NOT appear in /fapi/v1/openOrders. Without the
        algo sweep, an algo SL/TRAIL would linger and could misfire on the
        next trade for the same symbol (reduceOnly=true makes the misfire
        a no-op on zero position, but a NEW position would be wrongly
        closed).
        """
        if DRY_RUN:
            self.log.info(f"DRY_RUN: Would cancel orders for {trade['symbol']}")
            return

        symbol = trade["symbol"]
        trade_id = trade["id"]
        cancelled = 0

        # ── Regular orders (LIMIT TPs, conditional DCAs) ──
        try:
            open_orders = self.bybit.open_orders(CATEGORY, symbol)
            for order in open_orders:
                link_id = order.get("orderLinkId") or ""
                if link_id.startswith(trade_id + ":"):
                    order_id = order.get("orderId")
                    if order_id:
                        try:
                            self.bybit.cancel_order({
                                "category": CATEGORY,
                                "symbol": symbol,
                                "orderId": order_id
                            })
                            cancelled += 1
                            self.log.info(f"🗑️ Cancelled orphan order: {link_id}")
                        except Exception as e:
                            if "not found" not in str(e).lower():
                                self.log.warning(f"Failed to cancel {link_id}: {e}")
        except Exception as e:
            self.log.warning(f"Failed to cleanup regular orders for {symbol}: {e}")

        # ── Algo orders (algo SL via set_trading_stop, algo TRAIL) ──
        try:
            algos = self.bybit.open_algo_orders(symbol)
            for ao in algos:
                cid_raw = ao.get("clientAlgoId") or ""
                # Match either trade-id prefix (TRAIL: "<trade_id>:TRAIL")
                # or the sl- prefix used by set_trading_stop for algo SL.
                if cid_raw.startswith(trade_id + ":") or cid_raw.startswith("sl-"):
                    aid = ao.get("algoId")
                    if aid is not None:
                        try:
                            self.bybit._cancel_algo_order_by_id(aid)
                            cancelled += 1
                            self.log.info(f"🗑️ Cancelled orphan algo: {cid_raw} (algoId={aid})")
                        except Exception as e:
                            self.log.warning(f"Failed to cancel algo {cid_raw}: {e}")
        except Exception as e:
            self.log.warning(f"Failed to cleanup algo orders for {symbol}: {e}")

        if cancelled > 0:
            self.log.info(f"🧹 Cleaned up {cancelled} pending order(s) for {symbol}")

    def _export_trade_to_db(self, trade: Dict[str, Any]) -> None:
        """Export trade to PostgreSQL database immediately after close."""
        try:
            # Calculate margin used for PnL % calculation
            entry_price = trade.get("entry_price") or trade.get("trigger") or 0
            base_qty = trade.get("base_qty") or 0
            margin_used = (entry_price * base_qty) / LEVERAGE if entry_price and base_qty else 0

            # Fetch current equity from Bybit for Equity PnL % calculation
            equity_at_close = 0
            try:
                equity_at_close = self.bybit.wallet_equity(ACCOUNT_TYPE)
            except Exception as e:
                self.log.debug(f"Could not fetch equity: {e}")

            # Add margin and equity to trade for export
            trade["margin_used"] = margin_used
            trade["equity_at_close"] = equity_at_close

            if db_export.export_trade(trade):
                self.log.info(f"📊 Trade exported to database")
            else:
                self.log.warning(f"⚠️ Database export failed (check DATABASE_URL)")
        except Exception as e:
            self.log.warning(f"Database export error: {e}")

    def _fetch_and_store_trade_stats(self, trade: Dict[str, Any]) -> None:
        """Fetch final PnL from Bybit and determine exit reason."""
        if DRY_RUN:
            trade["realized_pnl"] = 0.0
            trade["exit_reason"] = "dry_run"
            return

        symbol = trade["symbol"]
        filled_ts = trade.get("filled_ts") or trade.get("placed_ts") or 0

        try:
            # Fetch closed PnL records around the time of this trade
            start_time = int((filled_ts - 60) * 1000) if filled_ts else None
            pnl_records = self.bybit.closed_pnl(CATEGORY, symbol, start_time=start_time, limit=20)

            # Sum all PnL records for this symbol in the timeframe
            total_pnl = 0.0
            for rec in pnl_records:
                rec_time = int(rec.get("createdTime") or 0)
                if rec_time >= int(filled_ts * 1000):
                    total_pnl += float(rec.get("closedPnl") or 0)

            trade["realized_pnl"] = total_pnl
            trade["is_win"] = total_pnl >= 0  # Breakeven counts as win

            # Determine exit reason based on what happened
            trade["exit_reason"] = self._determine_exit_reason(trade)

            # Log trade summary
            self._log_trade_summary(trade)

        except Exception as e:
            self.log.warning(f"Failed to fetch PnL for {symbol}: {e}")
            trade["realized_pnl"] = None
            trade["exit_reason"] = "unknown"

    def _determine_exit_reason(self, trade: Dict[str, Any]) -> str:
        """Determine how the trade was closed."""
        tp_fills = trade.get("tp_fills", 0)
        tp_count = len(trade.get("tp_prices") or FALLBACK_TP_PCT)
        trailing_started = trade.get("trailing_started", False)
        sl_moved_to_be = trade.get("sl_moved_to_be", False)
        pnl = trade.get("realized_pnl", 0)

        if trailing_started and pnl and pnl > 0:
            return "trailing_stop"
        elif tp_fills >= tp_count:
            return "all_tps_hit"
        elif tp_fills > 0 and sl_moved_to_be and pnl is not None and abs(pnl) < 1:
            return "breakeven"
        elif tp_fills > 0:
            return f"tp{tp_fills}_then_sl"
        elif pnl and pnl < 0:
            return "stop_loss"
        else:
            return "unknown"

    def _log_trade_summary(self, trade: Dict[str, Any]) -> None:
        """Log a nice trade summary."""
        symbol = trade["symbol"]
        side = trade.get("pos_side", "")
        entry = trade.get("entry_price", trade.get("trigger"))
        pnl = trade.get("realized_pnl", 0) or 0
        exit_reason = trade.get("exit_reason", "unknown")
        tp_fills = trade.get("tp_fills", 0)
        # TP count = how many we actually placed (limited by TP_SPLITS)
        signal_tp_count = len(trade.get("tp_prices") or FALLBACK_TP_PCT)
        tp_count = min(signal_tp_count, len(TP_SPLITS))
        dca_fills = trade.get("dca_fills", 0)
        dca_count = len(DCA_QTY_MULTS)
        is_win = pnl >= 0  # Breakeven counts as win

        emoji = "🟢" if is_win else "🔴"
        result = "WIN" if is_win else "LOSS"

        self.log.info(f"")
        self.log.info(f"{'='*50}")
        self.log.info(f"{emoji} TRADE {result}: {symbol} {side}")
        self.log.info(f"{'='*50}")
        self.log.info(f"   Entry: ${entry:.6f}")
        self.log.info(f"   PnL: ${pnl:.2f} USDT")
        self.log.info(f"   TPs Hit: {tp_fills}/{tp_count}")
        self.log.info(f"   DCAs Filled: {dca_fills}/{dca_count}")
        self.log.info(f"   Exit: {exit_reason}")
        self.log.info(f"{'='*50}")
        self.log.info(f"")

    def _archive_trade(self, trade: Dict[str, Any]) -> None:
        """Move closed trade to trade_history for long-term stats."""
        history = self.state.setdefault("trade_history", [])

        # TP count = how many we actually placed (limited by TP_SPLITS)
        signal_tp_count = len(trade.get("tp_prices") or FALLBACK_TP_PCT)
        actual_tp_count = min(signal_tp_count, len(TP_SPLITS))

        # Keep only essential fields for history
        archived = {
            "id": trade.get("id"),
            "symbol": trade.get("symbol"),
            "side": trade.get("pos_side"),
            "entry_price": trade.get("entry_price"),
            "trigger": trade.get("trigger"),
            "placed_ts": trade.get("placed_ts"),
            "filled_ts": trade.get("filled_ts"),
            "closed_ts": trade.get("closed_ts"),
            "realized_pnl": trade.get("realized_pnl"),
            "is_win": trade.get("is_win"),
            "exit_reason": trade.get("exit_reason"),
            "tp_fills": trade.get("tp_fills", 0),
            "tp_count": actual_tp_count,
            "dca_fills": trade.get("dca_fills", 0),
            "dca_count": len(DCA_QTY_MULTS),
            "trailing_used": trade.get("trailing_started", False),
        }
        history.append(archived)

        # Note: Google Sheets export happens immediately at trade close,
        # not here at archive time (to avoid 24h delay)

        # Keep max 500 trades in history (oldest pruned)
        if len(history) > 500:
            self.state["trade_history"] = history[-500:]

    def get_trade_stats(self, days: Optional[int] = None) -> Dict[str, Any]:
        """Calculate trade statistics for the given period (None = all time)."""
        history = self.state.get("trade_history", [])
        now = time.time()

        if days:
            cutoff = now - (days * 86400)
            trades = [t for t in history if (t.get("closed_ts") or 0) >= cutoff]
        else:
            trades = history

        if not trades:
            return {
                "period_days": days or "all",
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "avg_tp_fills": 0.0,
                "avg_dca_fills": 0.0,
                "trailing_exits": 0,
                "sl_exits": 0,
                "be_exits": 0,
            }

        wins = [t for t in trades if t.get("is_win")]
        losses = [t for t in trades if not t.get("is_win")]
        pnls = [t.get("realized_pnl") or 0 for t in trades]
        tp_fills = [t.get("tp_fills") or 0 for t in trades]
        dca_fills = [t.get("dca_fills") or 0 for t in trades]

        exit_reasons = [t.get("exit_reason") or "" for t in trades]
        trailing_exits = sum(1 for r in exit_reasons if r == "trailing_stop")
        sl_exits = sum(1 for r in exit_reasons if r == "stop_loss")
        be_exits = sum(1 for r in exit_reasons if r == "breakeven")

        return {
            "period_days": days or "all",
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(trades), 2) if trades else 0.0,
            "best_trade": round(max(pnls), 2) if pnls else 0.0,
            "worst_trade": round(min(pnls), 2) if pnls else 0.0,
            "avg_tp_fills": round(sum(tp_fills) / len(trades), 1) if trades else 0.0,
            "avg_dca_fills": round(sum(dca_fills) / len(trades), 1) if trades else 0.0,
            "trailing_exits": trailing_exits,
            "sl_exits": sl_exits,
            "be_exits": be_exits,
        }

    def log_performance_report(self) -> None:
        """Log a comprehensive performance report."""
        stats_7d = self.get_trade_stats(7)
        stats_30d = self.get_trade_stats(30)
        stats_all = self.get_trade_stats()

        self.log.info("")
        self.log.info("=" * 60)
        self.log.info("📊 PERFORMANCE REPORT")
        self.log.info("=" * 60)

        for label, stats in [("7 Days", stats_7d), ("30 Days", stats_30d), ("All Time", stats_all)]:
            if stats["total_trades"] == 0:
                self.log.info(f"\n{label}: No trades")
                continue

            self.log.info(f"\n📈 {label}:")
            self.log.info(f"   Trades: {stats['total_trades']} | Wins: {stats['wins']} | Losses: {stats['losses']}")
            self.log.info(f"   Win Rate: {stats['win_rate']}%")
            self.log.info(f"   Total PnL: ${stats['total_pnl']:.2f} | Avg: ${stats['avg_pnl']:.2f}")
            self.log.info(f"   Best: ${stats['best_trade']:.2f} | Worst: ${stats['worst_trade']:.2f}")
            self.log.info(f"   Avg TPs Hit: {stats['avg_tp_fills']:.1f} | Avg DCAs: {stats['avg_dca_fills']:.1f}")
            self.log.info(f"   Exits: {stats['trailing_exits']} trailing, {stats['sl_exits']} SL, {stats['be_exits']} BE")

        self.log.info("")
        self.log.info("=" * 60)

        # Note: Daily equity update moved to log_daily_stats() to ensure it runs daily even without trades

    # ---------- signal update methods ----------
    # NOTE: a second `_move_sl` definition used to live here. It silently
    # overrode the retry-enabled implementation above (line ~849), causing
    # the TP1→BE move to lose its 3-attempt retry on volatile markets.
    # Removed — use the canonical `_move_sl(symbol, sl_price, max_retries=3)`.

    def update_tp_orders(self, trade: Dict[str, Any], new_tps: List[float]) -> bool:
        """Cancel old TP orders and place new ones with updated prices."""
        if DRY_RUN:
            self.log.info(f"DRY_RUN: Would update TPs for {trade['symbol']} to {new_tps}")
            return True

        symbol = trade["symbol"]
        side = trade["order_side"]
        size, _ = self.position_size_avg(symbol)

        if size <= 0:
            self.log.warning(f"Cannot update TPs: no position for {symbol}")
            return False

        # Get instrument rules
        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]
        qty_step = rules["qty_step"]
        min_qty = rules["min_qty"]

        # Cancel existing unfilled TP orders
        tp_order_ids = trade.get("tp_order_ids", {})
        filled_tps = trade.get("tp_fills_list", [])
        splits = trade.get("tp_splits") or TP_SPLITS

        for tp_num_str, order_id in list(tp_order_ids.items()):
            tp_num = int(tp_num_str)
            # Skip already filled TPs
            if tp_num in filled_tps:
                continue

            try:
                self.bybit.cancel_order({
                    "category": CATEGORY,
                    "symbol": symbol,
                    "orderId": order_id
                })
                self.log.debug(f"Cancelled old TP{tp_num} order {order_id}")
            except Exception as e:
                self.log.debug(f"Failed to cancel TP{tp_num}: {e}")

        # Place new TP orders
        tp_version = trade.get("tp_version", 1) + 1
        trade["tp_version"] = tp_version

        for i, tp_price in enumerate(new_tps):
            tp_num = i + 1

            # Skip already filled TPs
            if tp_num in filled_tps:
                continue

            # Skip if no split for this TP
            if i >= len(splits) or splits[i] <= 0:
                continue

            tp = self._round_price(float(tp_price), tick_size)
            qty = self._round_qty(size * (splits[i] / 100.0), qty_step, min_qty)

            body = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": _opposite_side(side),
                "orderType": "Limit",
                "qty": f"{qty}",
                "price": f"{tp:.10f}",
                "timeInForce": "GTC",
                "reduceOnly": True,
                "closeOnTrigger": False,
                "orderLinkId": f"{trade['id']}:TP{tp_num}v{tp_version}",
            }

            try:
                resp = self.bybit.place_order(body)
                new_oid = (resp.get("result") or {}).get("orderId")
                if new_oid:
                    tp_order_ids[str(tp_num)] = new_oid
                    if tp_num == 1:
                        trade["tp1_order_id"] = new_oid
                    self.log.info(f"   Updated TP{tp_num}: {tp:.4f}")
            except Exception as e:
                self.log.warning(f"Failed to place updated TP{tp_num}: {e}")

        # Update trade's TP prices
        trade["tp_prices"] = new_tps
        return True

    def place_dca_orders(self, trade: Dict[str, Any]) -> bool:
        """Place DCA orders for a trade that didn't have them initially."""
        if DRY_RUN:
            self.log.info(f"DRY_RUN: Would place DCAs for {trade['symbol']}")
            return True

        symbol = trade["symbol"]
        side = trade["order_side"]
        base_qty = float(trade.get("base_qty", 0))

        if base_qty <= 0:
            self.log.warning(f"Cannot place DCAs: no base qty for {symbol}")
            return False

        dca_prices = trade.get("dca_prices", [])
        if not dca_prices:
            self.log.debug(f"No DCA prices for {symbol}")
            return False

        # Get instrument rules
        rules = self._get_instrument_rules(symbol)
        tick_size = rules["tick_size"]
        qty_step = rules["qty_step"]
        min_qty = rules["min_qty"]

        dca_to_place = min(len(dca_prices), len(DCA_QTY_MULTS))
        last = self._last_price(symbol)

        self.log.info(f"📊 Placing {dca_to_place} DCAs for {symbol}")

        for j in range(1, dca_to_place + 1):
            price = self._round_price(float(dca_prices[j-1]), tick_size)
            mult = DCA_QTY_MULTS[j-1]
            qty = self._round_qty(base_qty * mult, qty_step, min_qty)
            td = self._trigger_direction(last, price)

            body = {
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

            try:
                resp = self.bybit.place_order(body)
                oid = (resp.get("result") or {}).get("orderId")
                if oid:
                    self.log.info(f"   Placed DCA{j}: {price:.4f}")
            except Exception as e:
                self.log.warning(f"Failed to place DCA{j}: {e}")

        trade["dca_orders_placed"] = True
        return True
