import os
import sys
import time
import random
import threading
import logging
import queue as _queue

from config import (
    DISCORD_TOKEN, CHANNEL_ID,
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_DEMO, RECV_WINDOW, ACCOUNT_TYPE,
    CATEGORY, QUOTE, LEVERAGE, RISK_PCT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_DAY, TC_MAX_LAG_SEC,
    POLL_SECONDS, POLL_JITTER_MAX, SIGNAL_UPDATE_INTERVAL_SEC, SIGNAL_UPDATE_INTERVAL_OPEN_SEC,
    USE_GATEWAY_WS, GATEWAY_FALLBACK_FAILURES, GATEWAY_LOOP_SLEEP_SEC, GATEWAY_INITIAL_BACKFILL,
    WARMUP_SYMBOLS,
    STATE_FILE, DRY_RUN, LOG_LEVEL
)
from bybit_v5 import BybitV5
from discord_reader import DiscordReader
from discord_gateway import DiscordGateway
from signal_parser import parse_signal, signal_hash, parse_signal_update, is_trade_closed
from state import load_state, save_state, utc_day_key, state_lock
from trade_engine import TradeEngine
from entry_watcher import EntryWatcher
import db_export
import telegram_alerts

def setup_logger() -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)  # stdout so Railway shows INFO as normal (not red)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    log.handlers[:] = [h]
    return log

def _apply_signal_update_to_trade(tr, txt, engine, log):
    """Apply parsed-from-text signal-update logic to a single trade.
    Used by both REST polling (check_signal_updates) and Gateway WS edit
    push (fast_edit_handler in main()). Caller holds state_lock."""
    try:
        # Check for TRADE CLOSED (manual close by signal provider).
        # Detects both "TRADE CLOSED" (legacy) and "Closed P&L:" (AO Crusher).
        if is_trade_closed(txt):
            log.warning(f"🚨 Signal CLOSED detected for {tr['symbol']} - sending Telegram alert")
            if tr.get("status") == "open":
                direction = "SHORT" if tr["order_side"] == "Sell" else "LONG"
                message = (
                    f"🚨 <b>Signal Provider Closed Trade</b>\n\n"
                    f"<b>{tr['symbol']}</b> {direction}\n"
                    f"Status: Position still OPEN\n\n"
                    f"⚠️ Consider closing position manually"
                )
                telegram_alerts.send_message(message)
                log.info(f"   Telegram alert sent for {tr['symbol']}")
            elif tr.get("status") == "pending":
                entry_oid = tr.get("entry_order_id")
                if entry_oid and entry_oid != "DRY_RUN":
                    try:
                        engine.cancel_entry(tr["symbol"], entry_oid, tr.get("id"))
                        telegram_alerts.send_order_canceled(
                            symbol=tr["symbol"],
                            side=tr["order_side"],
                            reason="Signal provider closed trade"
                        )
                    except Exception as e:
                        log.warning(f"Failed to cancel entry for {tr['symbol']}: {e}")
                tr["status"] = "cancelled"
                tr["exit_reason"] = "signal_closed"
            return

        # Check for TRADE CANCELLED
        if "TRADE CANCELLED" in txt.upper() or "CLOSED WITHOUT ENTRY" in txt.upper():
            log.warning(f"❌ Signal CANCELLED for {tr['symbol']} - cancelling all orders")
            if tr.get("status") == "pending":
                entry_oid = tr.get("entry_order_id")
                if entry_oid:
                    engine.cancel_entry(tr["symbol"], entry_oid, tr.get("id"))
            if tr.get("status") == "open":
                engine._cancel_all_trade_orders(tr)
            tr["status"] = "cancelled"
            tr["exit_reason"] = "signal_cancelled"
            return

        # Parse SL/TP/DCA from the text
        sig = parse_signal_update(txt)
        new_sl = sig.get("sl_price")
        new_tps = sig.get("tp_prices") or []
        new_dcas = sig.get("dca_prices") or []
        old_sl = tr.get("sl_price")
        old_tps = tr.get("tp_prices") or []
        old_dcas = tr.get("dca_prices") or []
        is_open = tr.get("status") == "open"

        # SL Update Check
        if new_sl and new_sl != old_sl and not tr.get("sl_moved_to_be"):
            log.info(f"🔄 Signal SL updated for {tr['symbol']}: {old_sl} → {new_sl}")
            tr["sl_price"] = new_sl
            if is_open:
                engine._move_sl(tr["symbol"], new_sl)

        # TP Update Check (detect ANY change in TP prices)
        tps_changed = False
        if new_tps and len(new_tps) > 0:
            new_tps = new_tps[:3]
            if len(new_tps) != len(old_tps):
                tps_changed = True
            elif any(abs(float(new_tps[i]) - float(old_tps[i])) > 0.0000001
                     for i in range(min(len(new_tps), len(old_tps)))):
                tps_changed = True
        if tps_changed:
            log.info(f"🔄 Signal TPs changed for {tr['symbol']}: {old_tps} → {new_tps}")
            if is_open and tr.get("post_orders_placed"):
                engine.update_tp_orders(tr, new_tps)
            else:
                tr["tp_prices"] = new_tps

        # DCA Update Check
        dcas_changed = False
        if new_dcas:
            if len(new_dcas) != len(old_dcas):
                dcas_changed = True
            elif old_dcas and any(abs(float(new_dcas[i]) - float(old_dcas[i])) > 0.0000001
                                 for i in range(min(len(new_dcas), len(old_dcas)))):
                dcas_changed = True
        if dcas_changed or (new_dcas and not old_dcas):
            log.info(f"🔄 Signal DCA updated for {tr['symbol']}: {old_dcas} → {new_dcas}")
            tr["dca_prices"] = new_dcas
            if is_open and not tr.get("dca_orders_placed"):
                engine.place_dca_orders(tr)
    except Exception as e:
        log.debug(f"Signal update apply failed for {tr.get('symbol')}: {e}")


def check_signal_updates(discord, engine, st, log):
    """REST-fallback signal-update poller. Re-fetches Discord messages
    for active trades and applies updates via _apply_signal_update_to_trade.
    The Gateway WS MESSAGE_UPDATE push is the primary path; this runs at
    a slower cadence (60s) as a safety net."""
    active_trades = [
        tr for tr in st.get("open_trades", {}).values()
        if tr.get("status") in ("pending", "open") and tr.get("discord_msg_id")
    ]
    if not active_trades:
        return

    log.info(f"🔍 Checking {len(active_trades)} trade(s) for signal updates (REST poll)...")
    for tr in active_trades:
        try:
            msg_id = tr.get("discord_msg_id")
            if not msg_id:
                continue
            msg = discord.fetch_message(str(msg_id))
            if not msg:
                failed_fetches = tr.get("discord_fetch_failures", 0) + 1
                tr["discord_fetch_failures"] = failed_fetches
                if failed_fetches == 1:
                    log.warning(f"   {tr.get('symbol')}: Could not fetch Discord msg {msg_id}")
                elif failed_fetches >= 10:
                    log.warning(f"   {tr.get('symbol')}: Removing discord_msg_id after {failed_fetches} failures")
                    tr["discord_msg_id"] = None
                continue
            txt = discord.extract_text(msg)
            if not txt:
                continue
            with state_lock:
                _apply_signal_update_to_trade(tr, txt, engine, log)
        except Exception as e:
            log.debug(f"Signal update check failed for {tr.get('symbol')}: {e}")
    save_state(STATE_FILE, st)

def main():
    log = setup_logger()

    # basic env checks
    missing = [k for k,v in {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "CHANNEL_ID": CHANNEL_ID,
        "BYBIT_API_KEY": BYBIT_API_KEY,
        "BYBIT_API_SECRET": BYBIT_API_SECRET,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing ENV(s): {', '.join(missing)}")

    # ── EXPORT_HISTORY / ANALYZE_HISTORY one-shot modes ──────────────────
    # Setting either flag in Railway env runs that one-shot job at startup
    # then exits cleanly. Both can be set together: export populates DB,
    # analyze reads from DB, prints results, sends Telegram summary, dumps
    # analysis.json. Idempotent — re-running EXPORT just refreshes rows.
    do_export   = os.getenv("EXPORT_HISTORY", "").strip().lower() in ("1", "true", "yes")
    do_analyze  = os.getenv("ANALYZE_HISTORY", "").strip().lower() in ("1", "true", "yes")
    if do_export or do_analyze:
        if db_export.is_enabled():
            log.info("   db_export enabled — initializing schema")
            db_export.init_database()

        if do_export:
            log.info("📤 EXPORT_HISTORY=1 — dumping channel history to discord_signals table…")
            from export_signals import run_export
            try:
                limit = int(os.getenv("LIMIT", "0"))
            except ValueError:
                limit = 0
            after_id = os.getenv("AFTER_ID", "").strip() or None
            skip_files = os.getenv("SKIP_FILES", "1").strip().lower() in ("1", "true", "yes")
            reader = DiscordReader(DISCORD_TOKEN, CHANNEL_ID)
            run_export(reader, CHANNEL_ID, limit_total=limit,
                       after_id=after_id, skip_files=skip_files, logger=log)
            log.info("📤 EXPORT_HISTORY done.")

        if do_analyze:
            log.info("📊 ANALYZE_HISTORY=1 — running strategy analysis…")
            from analyze_signals import run_analysis
            run_analysis(logger=log)

        log.info("🏁 One-shot job complete. Unset EXPORT_HISTORY / ANALYZE_HISTORY in Railway "
                 "and redeploy to resume normal trading.")
        return  # exit cleanly

    st = load_state(STATE_FILE)

    bybit = BybitV5(BYBIT_API_KEY, BYBIT_API_SECRET, testnet=BYBIT_TESTNET, demo=BYBIT_DEMO, recv_window=RECV_WINDOW)
    discord = DiscordReader(DISCORD_TOKEN, CHANNEL_ID)

    # Discord Gateway WebSocket: push-based new-message receiver. Replaces
    # REST polling for new signals (~50-300ms vs 0-4s). REST fetch_after
    # below stays as fallback if the gateway exceeds GATEWAY_FALLBACK_FAILURES.
    # Fast path: on_signal_callback wires directly into the gateway thread
    # (set after fast_signal_handler is defined below).
    gateway: "DiscordGateway | None" = None
    if USE_GATEWAY_WS:
        gateway = DiscordGateway(DISCORD_TOKEN, CHANNEL_ID, log)

    # Live TP1-cross watcher: cancels pending conditional entries the moment
    # the market last-price crosses TP1 (so we never enter into a trade where
    # the opportunity is already gone). Pure Bybit WS — no Discord polling.
    def on_tp1_cross(trade_id, symbol, side, entry_oid):
        # Called from EntryWatcher's WS thread — must hold state_lock
        # around state mutations to stay consistent with main loop and
        # fast_signal_handler readers.
        try:
            if entry_oid and entry_oid != "DRY_RUN":
                engine.cancel_entry(symbol, entry_oid, trade_id)
        except Exception as e:
            log.warning(f"on_tp1_cross: cancel_entry failed for {symbol}: {e}")
        with state_lock:
            tr = st.get("open_trades", {}).get(trade_id)
            # Guard: only flip status if still pending. If the entry
            # already filled (race between our cancel call and Bybit's
            # fill-then-WS-push) the trade is "open" with a real
            # position — don't lie about it.
            if tr and tr.get("status") == "pending":
                tr["status"] = "cancelled_tp1_hit"
                tr["exit_reason"] = "tp1_hit_before_entry"
            elif tr and tr.get("status") == "open":
                log.warning(f"[on_tp1_cross] {symbol} entry already FILLED before cancel reached Bybit — leaving status=open")
        try:
            telegram_alerts.send_order_canceled(
                symbol=symbol,
                side=side,
                reason="TP1 reached on live ticker before entry filled",
            )
        except Exception:
            pass
        save_state(STATE_FILE, st)

    entry_watcher = EntryWatcher(bybit, on_tp1_cross, log)
    engine = TradeEngine(bybit, st, log, entry_watcher=entry_watcher)
    entry_watcher.start()

    # Backfill via REST once before connecting Gateway so messages posted
    # during downtime aren't lost. Pre-load them straight into the gateway
    # queue if WS is enabled, so the main loop processes them via the same
    # drain path as live messages.
    if gateway and GATEWAY_INITIAL_BACKFILL:
        try:
            after = st.get("last_discord_id")
            backfill = discord.fetch_after(after, limit=50)
            if backfill:
                log.info(f"[gateway] backfilling {len(backfill)} message(s) from REST")
                for m in sorted(backfill, key=lambda x: int(x.get("id", "0"))):
                    try:
                        gateway.msg_queue.put_nowait(m)
                    except Exception:
                        break
        except Exception as e:
            log.warning(f"[gateway] backfill failed: {e}")

    # Re-attach entry_watcher for any trades that were pending when the
    # bot last shut down. Without this, after a restart the conditional
    # entry sits on Bybit but our TP1-cross safety net is gone until the
    # entry fills — opening a window where we can enter into a guaranteed
    # losing trade. With it: watcher resumes the moment the WS connects.
    pending_at_restart = [
        tr for tr in st.get("open_trades", {}).values()
        if tr.get("status") == "pending" and tr.get("entry_order_id")
    ]
    if pending_at_restart:
        log.info(f"♻️  Re-attaching entry_watcher for {len(pending_at_restart)} pending trade(s) from previous session")
        for tr in pending_at_restart:
            tps = tr.get("tp_prices") or []
            tp1 = float(tps[0]) if tps else None
            if tp1:
                try:
                    entry_watcher.watch(
                        tr["id"], tr["symbol"], tr["order_side"], tp1, tr["entry_order_id"]
                    )
                except Exception as e:
                    log.warning(f"   re-attach failed for {tr.get('symbol')}: {e}")

    # gateway.start() is deferred to AFTER fast_signal_handler is defined
    # (further down in main()), so the callback is wired before the WS
    # thread can dispatch its first message.

    log.info("="*58)
    mode_str = " | DRY_RUN" if DRY_RUN else ""
    mode_str += " | DEMO" if BYBIT_DEMO else ""
    mode_str += " | TESTNET" if BYBIT_TESTNET else ""
    log.info("Discord → Bybit Bot (One-way)" + mode_str)
    log.info("="*58)
    log.info(f"Config: CATEGORY={CATEGORY}, QUOTE={QUOTE}, LEVERAGE={LEVERAGE}x")
    log.info(f"Config: RISK_PCT={RISK_PCT}%, MAX_CONCURRENT={MAX_CONCURRENT_TRADES}, MAX_DAILY={MAX_TRADES_PER_DAY}")
    log.info(f"Config: POLL_SECONDS={POLL_SECONDS}, TC_MAX_LAG_SEC={TC_MAX_LAG_SEC}")
    log.info(f"Config: USE_GATEWAY_WS={USE_GATEWAY_WS} (fallback after {GATEWAY_FALLBACK_FAILURES} failures)")
    log.info(f"Config: DRY_RUN={DRY_RUN}, LOG_LEVEL={LOG_LEVEL}")

    # Initialize database if enabled
    if db_export.is_enabled():
        log.info("📊 Initializing database...")
        if db_export.init_database():
            log.info("✅ Database ready")
        else:
            log.warning("⚠️ Database initialization failed (continuing without DB export)")

    # Startup sync - check for orphaned positions
    engine.startup_sync()

    # ── Bybit cache pre-warm ───────────────────────────────────────────────
    # Pre-fetch wallet_equity + (instrument_rules + set_leverage) for the
    # symbols we'll likely trade, all in parallel. Eliminates the ~300ms
    # cold-path penalty on the first trade per symbol after bot start.
    def _bybit_warmup():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        if not WARMUP_SYMBOLS and not DRY_RUN:
            # Always pre-warm equity even if no symbol list provided
            try:
                bybit.wallet_equity(ACCOUNT_TYPE, force_refresh=True)
                log.info("🔥 Warmup: equity cached")
            except Exception as e:
                log.warning(f"warmup: equity failed: {e}")
            return
        if DRY_RUN:
            log.info("🔥 Warmup skipped (DRY_RUN)")
            return

        log.info(f"🔥 Warming up Bybit caches: equity + {len(WARMUP_SYMBOLS)} symbols...")
        t0 = time.time()

        # Per-call light delay to stay under Bybit's per-second POST limit.
        # Each warm_symbol does 2 REST calls (instruments_info GET + set_
        # leverage POST). Bybit's POST limit is ~10/s shared across the
        # account; with 3 workers + 100ms delay we average ~6 POSTs/s,
        # well below the cap. Previous 8-worker burst hit 10006 on ~14
        # of 35 symbols.
        warmup_lock = threading.Lock()
        last_call_ts = [0.0]

        def warm_symbol(base):
            with warmup_lock:
                gap = time.time() - last_call_ts[0]
                if gap < 0.1:
                    time.sleep(0.1 - gap)
                last_call_ts[0] = time.time()
            symbol = f"{base}{QUOTE}"
            try:
                engine._get_instrument_rules(symbol)
                if engine._set_leverage_safe(symbol):
                    engine._leverage_set.add(symbol)
                return (symbol, True, None)
            except Exception as e:
                return (symbol, False, str(e))

        with ThreadPoolExecutor(max_workers=3) as ex:
            eq_f = ex.submit(bybit.wallet_equity, ACCOUNT_TYPE, True)
            sym_futures = [ex.submit(warm_symbol, b) for b in WARMUP_SYMBOLS]
            try:
                eq_f.result()
            except Exception as e:
                log.warning(f"warmup: equity failed: {e}")
            ok = sum(1 for f in sym_futures if (r := f.result())[1])
            failed = [r[0] for f in sym_futures if not (r := f.result())[1]]
        elapsed = (time.time() - t0) * 1000.0
        msg = f"🔥 Warmup done: {ok}/{len(WARMUP_SYMBOLS)} symbols + equity, {elapsed:.0f}ms"
        if failed:
            msg += f" (failed: {','.join(failed[:5])})"
        log.info(msg)

    _bybit_warmup()

    # Heartbeat tracking
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 300  # Log heartbeat every 5 minutes

    # Signal update tracking (dynamic intervals: 60s for pending, 10s for open)
    last_signal_update_check_pending = time.time() - (SIGNAL_UPDATE_INTERVAL_SEC - 5)  # First check after 5 seconds
    last_signal_update_check_open = time.time() - (SIGNAL_UPDATE_INTERVAL_OPEN_SEC - 3)  # First check after 3 seconds

    # State-write throttle: writing state.json on every loop iteration adds
    # 50-200ms on slow filesystems and is wasteful when nothing changed.
    # Throttle to once per second; trade-mutating paths force a save.
    last_state_save = 0.0
    STATE_SAVE_INTERVAL_SEC = 1.0
    def _save_state_throttled(force: bool = False):
        nonlocal last_state_save
        if force or (time.time() - last_state_save) >= STATE_SAVE_INTERVAL_SEC:
            save_state(STATE_FILE, st)
            last_state_save = time.time()

    # ----- WS thread -----
    ws_err = {"err": None}

    def on_execution(ev):
        try:
            engine.on_execution(ev)
        except Exception as e:
            log.warning(f"WS execution handler error: {e}")

    def on_order(ev):
        # optional: could track cancellations etc
        return

    def on_ws_error(err):
        ws_err["err"] = err
        log.debug(f"WS reconnecting: {err}")  # Normal, reduced to DEBUG

    def ws_loop():
        while True:
            try:
                # wallet/position cache writes happen inside bybit_v5.py.
                # No callbacks needed — engine reads from get_cached_position()
                # and bybit.wallet_equity() (which checks WS cache first).
                bybit.run_private_ws(
                    on_execution=on_execution,
                    on_order=on_order,
                    on_error=on_ws_error,
                    account_type=ACCOUNT_TYPE,
                )
            except Exception as e:
                on_ws_error(e)
            time.sleep(3)

    t = threading.Thread(target=ws_loop, daemon=True)
    t.start()

    # ----- helper: limits -----
    def trades_today() -> int:
        return int(st.get("daily_counts", {}).get(utc_day_key(), 0))

    def inc_trades_today():
        k = utc_day_key()
        st.setdefault("daily_counts", {})[k] = int(st.get("daily_counts", {}).get(k, 0)) + 1

    # state_lock is the shared module-level RLock from state.py — every
    # state mutation across all threads (main loop, fast_signal_handler,
    # WS callbacks, on_tp1_cross) acquires the same lock so save_state
    # never sees a dict mid-mutation. RLock allows the same thread to
    # re-enter (e.g. fast_signal_handler holds it across check+persist).

    # ============================================================
    # Fast signal handler — invoked DIRECTLY from the gateway thread
    # via asyncio.to_thread on every Discord WS push. Bypasses the
    # main loop's queue + maintenance ops for minimum push→order
    # latency, especially when the main loop is busy with Bybit
    # API calls for active position monitoring.
    # ============================================================
    def fast_signal_handler(raw_msg):
        try:
            ts = discord.message_timestamp_unix(raw_msg)
            now = time.time()
            age = (now - ts) if ts else 0.0
            if ts and age > TC_MAX_LAG_SEC:
                return  # backfill / replay — too old to act on

            txt = discord.extract_text(raw_msg)
            if not txt:
                return

            sig = parse_signal(txt, quote=QUOTE)
            if not sig:
                if "SIGNAL" in txt.upper() or "ENTRY" in txt.upper():
                    log.warning(f"⚠️ Possible signal NOT parsed: {txt[:300]}...")
                return

            age_ms = age * 1000.0 if ts else -1
            log.info(f"📨 [WS] Signal parsed: {sig['symbol']} {sig['side'].upper()} @ {sig['trigger']} (discord_age={age_ms:.0f}ms)")

            sh = signal_hash(sig)
            mid_str = str(raw_msg.get("id", ""))

            # ── Atomic check-and-mark: dedupe + limits + reserve a slot ──
            # ms precision avoids orderLinkId collisions on rapid bursts
            # (Bybit returns 110072 "duplicate orderLinkId" otherwise).
            trade_id = f"{sig['symbol']}|{sig['side']}|{int(time.time()*1000)}"

            # ── Atomic check-and-RESERVE: dedupe + limits + reserve a slot ──
            # Race fix: we add a placeholder trade with status="reserving"
            # under the lock so a concurrent fast_signal_handler counts us
            # in its active-trades total. inc_trades_today() also runs here
            # so the daily counter is correct before the Bybit place call.
            # On any subsequent failure path (place_order returns None,
            # exception, etc.) we MUST roll back: remove the placeholder
            # and decrement the daily counter.
            with state_lock:
                seen = set(st.get("seen_signal_hashes", []))
                if sh in seen:
                    log.debug(f"Signal {sig['symbol']} already seen, skipping")
                    return
                seen.add(sh)
                st["seen_signal_hashes"] = list(seen)[-500:]

                active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open", "reserving")]
                if len(active) >= MAX_CONCURRENT_TRADES:
                    log.info(f"Active trades {len(active)}/{MAX_CONCURRENT_TRADES} → skip {sig['symbol']}")
                    return
                if trades_today() >= MAX_TRADES_PER_DAY:
                    log.info(f"Trades today {trades_today()}/{MAX_TRADES_PER_DAY} → skip {sig['symbol']}")
                    return

                # Reserve the slot atomically. Counter goes up here too so
                # parallel handlers see the new total.
                st.setdefault("open_trades", {})[trade_id] = {
                    "id": trade_id,
                    "symbol": sig["symbol"],
                    "status": "reserving",
                    "placed_ts": time.time(),
                }
                inc_trades_today()

                # Update last_discord_id so REST backfill doesn't re-deliver
                try:
                    if int(mid_str or "0") > int(st.get("last_discord_id") or "0"):
                        st["last_discord_id"] = mid_str
                except (ValueError, TypeError):
                    pass

            # ── Place order (outside lock — Bybit ~200ms shouldn't block other threads) ──
            log.info(f"🔄 [WS] Placing entry order for {sig['symbol']}...")
            _t = time.time()
            try:
                oid = engine.place_conditional_entry(sig, trade_id)
            except Exception:
                log.exception(f"❌ place_conditional_entry crashed for {sig['symbol']}")
                oid = None
            place_ms = (time.time() - _t) * 1000.0
            e2e_ms = (time.time() - ts) * 1000.0 if ts else -1
            log.info(f"⏱  [WS] place_order took {place_ms:.0f}ms | e2e Discord→Order: {e2e_ms:.0f}ms")

            if not oid:
                log.warning(f"❌ Entry order failed for {sig['symbol']} — rolling back reserved slot")
                with state_lock:
                    st.get("open_trades", {}).pop(trade_id, None)
                    # Decrement daily counter (we incremented it pre-place)
                    k = utc_day_key()
                    cur = int(st.get("daily_counts", {}).get(k, 0))
                    if cur > 0:
                        st.setdefault("daily_counts", {})[k] = cur - 1
                    # Roll back the seen_signal_hashes entry too — otherwise
                    # if Bybit hiccuped (rate limit / network blip), the
                    # provider's signal is silently ignored on re-delivery.
                    seen = set(st.get("seen_signal_hashes", []))
                    seen.discard(sh)
                    st["seen_signal_hashes"] = list(seen)[-500:]
                return

            # ── Promote placeholder to real trade ──
            try:
                equity_now = bybit.wallet_equity(ACCOUNT_TYPE)  # cached
            except Exception:
                equity_now = 0

            mid = int(mid_str or "0")
            with state_lock:
                st["open_trades"][trade_id] = {
                    "id": trade_id,
                    "symbol": sig["symbol"],
                    "order_side": "Sell" if sig["side"] == "sell" else "Buy",
                    "pos_side": "Short" if sig["side"] == "sell" else "Long",
                    "trigger": float(sig["trigger"]),
                    "tp_prices": sig.get("tp_prices") or [],
                    "tp_splits": None,
                    "dca_prices": sig.get("dca_prices") or [],
                    "sl_price": sig.get("sl_price"),
                    "sl_set_inline": bool(sig.get("_sl_inline")),
                    "entry_order_id": oid,
                    "status": "pending",
                    "placed_ts": time.time(),
                    "base_qty": sig.get("_base_qty") or engine.calc_base_qty(sig["symbol"], float(sig["trigger"])),
                    "raw": sig.get("raw", ""),
                    "discord_msg_id": mid,
                    # Per-symbol effective values (account for LEVERAGE_OVERRIDES)
                    "risk_pct": engine._effective_risk_pct(sig["symbol"]),
                    "risk_amount": round(equity_now * engine._effective_risk_pct(sig["symbol"]) / 100, 2) if equity_now > 0 else None,
                    "equity_at_entry": round(equity_now, 2) if equity_now > 0 else None,
                    "leverage": engine._effective_leverage(sig["symbol"]),
                }
                save_state(STATE_FILE, st)
            log.info(f"🟡 [WS] ENTRY PLACED {sig['symbol']} {sig['side'].upper()} trigger={sig['trigger']} (id={trade_id})")

            # ── Watch TP1 cross + Telegram (parallel ok, off critical path) ──
            tps = sig.get("tp_prices") or []
            tp1 = float(tps[0]) if tps else None
            if tp1:
                order_side = "Sell" if sig["side"] == "sell" else "Buy"
                try:
                    entry_watcher.watch(trade_id, sig["symbol"], order_side, tp1, oid)
                except Exception as e:
                    log.warning(f"entry_watcher.watch failed: {e}")

            try:
                telegram_alerts.send_entry_pending(
                    symbol=sig["symbol"],
                    side="Sell" if sig["side"] == "sell" else "Buy",
                    entry=float(sig["trigger"]),
                    qty=st["open_trades"][trade_id]["base_qty"]
                )
            except Exception as e:
                log.warning(f"telegram alert failed: {e}")
        except Exception:
            log.exception("[WS] fast_signal_handler crashed")

    # ============================================================
    # Fast edit handler — invoked DIRECTLY from the gateway thread
    # via asyncio.to_thread on every Discord MESSAGE_UPDATE event.
    # Cuts TRADE CLOSED / SL-edit detection from up-to-60s polling
    # to ~50ms push.
    # ============================================================
    def fast_edit_handler(raw_msg):
        try:
            mid_str = str(raw_msg.get("id") or "")
            if not mid_str:
                return
            txt = discord.extract_text(raw_msg)
            if not txt:
                return
            with state_lock:
                tr = None
                for t in st.get("open_trades", {}).values():
                    if str(t.get("discord_msg_id") or "") == mid_str \
                       and t.get("status") in ("pending", "open"):
                        tr = t
                        break
                if tr is None:
                    return  # not a tracked message — ignore
                log.info(f"📝 [WS-edit] msg {mid_str} (tracked: {tr.get('symbol')})")
                _apply_signal_update_to_trade(tr, txt, engine, log)
            save_state(STATE_FILE, st)
        except Exception:
            log.exception("[WS-edit] fast_edit_handler crashed")

    # Now that both handlers are defined, wire them into the gateway and
    # start the WS thread.
    if gateway:
        gateway.on_signal_callback = fast_signal_handler
        gateway.on_edit_callback = fast_edit_handler
        gateway.start()

    # ----- main loop -----
    while True:
        try:
            # Heartbeat log every 5 minutes
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
                gw_state = "off"
                if gateway is not None:
                    gw_state = "healthy" if gateway.is_healthy() else f"down(fail={gateway.consecutive_failures()})"
                log.info(f"💓 Heartbeat: {len(active)} active trade(s), {trades_today()} today, gateway={gw_state}")
                last_heartbeat = time.time()

            # Check for signal updates (dynamic interval: 60s for pending, 10s for open)
            active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open")]
            has_open_trades = any(tr.get("status") == "open" for tr in active)
            has_pending_trades = any(tr.get("status") == "pending" for tr in active)

            # Check open trades every 10 seconds
            if has_open_trades and time.time() - last_signal_update_check_open > SIGNAL_UPDATE_INTERVAL_OPEN_SEC:
                check_signal_updates(discord, engine, st, log)
                last_signal_update_check_open = time.time()
                last_signal_update_check_pending = time.time()  # Also reset pending timer
            # Check pending trades every 60 seconds (if no open trades)
            elif has_pending_trades and time.time() - last_signal_update_check_pending > SIGNAL_UPDATE_INTERVAL_SEC:
                check_signal_updates(discord, engine, st, log)
                last_signal_update_check_pending = time.time()

            # maintenance first
            engine.cancel_expired_entries()
            engine.check_entry_order_validity()  # Cancel entry if TP1 reached before entry filled
            engine.cleanup_closed_trades()
            engine.check_tp_fills_fallback()  # Catch TP1 fills if WS missed
            engine.check_position_alerts()    # Send Telegram alerts if position P&L crosses thresholds
            engine.log_daily_stats()          # Log stats once per day

            # entry-fill fallback (polling) and post-orders placement.
            # Mutations under state_lock so on_execution / fast_signal_handler
            # / save_state never observe a half-updated trade dict.
            for tid, tr in list(st.get("open_trades", {}).items()):
                if tr.get("status") == "pending":
                    sz, avg = engine.position_size_avg(tr["symbol"])
                    if sz > 0 and avg > 0:
                        with state_lock:
                            tr["status"] = "open"
                            tr["entry_price"] = avg
                            tr["filled_ts"] = time.time()
                        log.info(f"✅ ENTRY (poll) {tr['symbol']} @ {avg}")
                if tr.get("status") == "open" and not tr.get("post_orders_placed"):
                    engine.place_post_entry_orders(tr)

            # ── Drain queue / REST poll, then dispatch via fast_signal_handler ──
            # Note: live WS pushes are already handled DIRECTLY from the
            # gateway thread (via on_signal_callback). This block exists for:
            #   - REST-fallback when gateway is unhealthy
            #   - backfill messages enqueued at startup
            # Limit checks live inside fast_signal_handler so it self-skips.
            use_gateway_now = (
                gateway is not None
                and gateway.consecutive_failures() < GATEWAY_FALLBACK_FAILURES
            )
            msgs = []
            if use_gateway_now:
                while True:
                    m = gateway.get_message_nowait()
                    if m is None:
                        break
                    msgs.append(m)
                if msgs:
                    log.debug(f"[gateway] drained {len(msgs)} backfill/queued message(s)")
            else:
                after = st.get("last_discord_id")
                log.debug(f"Polling Discord REST (after={after})...")
                try:
                    msgs = discord.fetch_after(after, limit=50)
                except Exception as e:
                    log.warning(f"Discord fetch failed: {e}")
                    msgs = []
                log.debug(f"Fetched {len(msgs)} message(s) from Discord")

            for m in sorted(msgs, key=lambda x: int(x.get("id", "0"))):
                fast_signal_handler(m)

            _save_state_throttled()

        except KeyboardInterrupt:
            log.info("Bye")
            if gateway is not None:
                try:
                    gateway.stop()
                except Exception:
                    pass
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")
            time.sleep(3)

        # ── Throttle ──
        # Gateway healthy: block on msg_event so an inbound Discord push
        # wakes the loop instantly (event-driven, ~0ms wait). The timeout
        # also caps maintenance interval at GATEWAY_LOOP_SLEEP_SEC.
        # Gateway down: REST polling cadence.
        gw_healthy = (
            gateway is not None
            and gateway.is_healthy()
            and gateway.consecutive_failures() < GATEWAY_FALLBACK_FAILURES
        )
        if gw_healthy:
            gateway.msg_event.wait(timeout=max(0.05, GATEWAY_LOOP_SLEEP_SEC))
            gateway.msg_event.clear()
        else:
            time.sleep(max(1, POLL_SECONDS + random.uniform(0, max(0, POLL_JITTER_MAX))))

if __name__ == "__main__":
    main()
