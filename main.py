import sys
import time
import random
import threading
import logging

from config import (
    DISCORD_TOKEN, CHANNEL_ID,
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_DEMO, RECV_WINDOW, ACCOUNT_TYPE,
    CATEGORY, QUOTE, LEVERAGE, RISK_PCT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_DAY, TC_MAX_LAG_SEC,
    POLL_SECONDS, POLL_JITTER_MAX, SIGNAL_UPDATE_INTERVAL_SEC, SIGNAL_UPDATE_INTERVAL_OPEN_SEC,
    STATE_FILE, DRY_RUN, LOG_LEVEL
)
from bybit_v5 import BybitV5
from discord_reader import DiscordReader
from signal_parser import parse_signal, signal_hash, parse_signal_update
from state import load_state, save_state, utc_day_key
from trade_engine import TradeEngine
import db_export
import telegram_alerts

def setup_logger() -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)  # stdout so Railway shows INFO as normal (not red)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    log.handlers[:] = [h]
    return log

def check_signal_updates(discord, engine, st, log):
    """Re-read Discord messages for open/pending trades and apply SL/TP/DCA updates."""

    # Find all active trades that have a discord_msg_id
    active_trades = [
        tr for tr in st.get("open_trades", {}).values()
        if tr.get("status") in ("pending", "open") and tr.get("discord_msg_id")
    ]

    if not active_trades:
        return

    log.info(f"🔍 Checking {len(active_trades)} trade(s) for signal updates...")

    for tr in active_trades:
        try:
            msg_id = tr.get("discord_msg_id")
            if not msg_id:
                continue

            # Fetch the current Discord message
            msg = discord.fetch_message(str(msg_id))
            if not msg:
                log.warning(f"   {tr.get('symbol')}: Could not fetch msg {msg_id}")
                continue

            # Extract text from the message
            txt = discord.extract_text(msg)
            if not txt:
                continue

            # Check for TRADE CLOSED (manual close by signal provider)
            if "TRADE CLOSED" in txt.upper():
                log.warning(f"🚨 Signal CLOSED detected for {tr['symbol']} - sending Telegram alert")

                # Send Telegram warning (don't auto-close position)
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
                    # Cancel entry order for pending trades
                    entry_oid = tr.get("entry_order_id")
                    if entry_oid and entry_oid != "DRY_RUN":
                        try:
                            engine.cancel_entry(tr["symbol"], entry_oid)
                            telegram_alerts.send_order_canceled(
                                symbol=tr["symbol"],
                                side=tr["order_side"],
                                reason="Signal provider closed trade"
                            )
                        except Exception as e:
                            log.warning(f"Failed to cancel entry for {tr['symbol']}: {e}")
                    tr["status"] = "cancelled"
                    tr["exit_reason"] = "signal_closed"
                continue

            # Check for TRADE CANCELLED
            if "TRADE CANCELLED" in txt.upper() or "CLOSED WITHOUT ENTRY" in txt.upper():
                log.warning(f"❌ Signal CANCELLED for {tr['symbol']} - cancelling all orders")
                # Cancel Entry Order if pending
                if tr.get("status") == "pending":
                    entry_oid = tr.get("entry_order_id")
                    if entry_oid:
                        engine.cancel_entry(tr["symbol"], entry_oid)
                # Cancel all TP/DCA Orders if open
                if tr.get("status") == "open":
                    engine._cancel_all_trade_orders(tr)
                tr["status"] = "cancelled"
                tr["exit_reason"] = "signal_cancelled"
                continue

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
                # Limit to max 3 TPs (ignore TP4+ because we trail after TP3)
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

            # DCA Update Check (detect ANY change in DCA prices, not just additions)
            dcas_changed = False
            if new_dcas:
                # Always use all available DCAs (config allows up to 3)
                if len(new_dcas) != len(old_dcas):
                    dcas_changed = True
                elif old_dcas and any(abs(float(new_dcas[i]) - float(old_dcas[i])) > 0.0000001
                                     for i in range(min(len(new_dcas), len(old_dcas)))):
                    dcas_changed = True

            if dcas_changed or (new_dcas and not old_dcas):
                log.info(f"🔄 Signal DCA updated for {tr['symbol']}: {old_dcas} → {new_dcas}")
                tr["dca_prices"] = new_dcas
                # If DCAs not placed yet or changed significantly, place/update them
                if is_open and not tr.get("dca_orders_placed"):
                    engine.place_dca_orders(tr)
                # Note: If DCAs change after placement, we log but don't cancel/replace
                # (safer to let existing DCAs stay active)

        except Exception as e:
            log.debug(f"Signal update check failed for {tr.get('symbol')}: {e}")

    # Save state
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

    st = load_state(STATE_FILE)

    bybit = BybitV5(BYBIT_API_KEY, BYBIT_API_SECRET, testnet=BYBIT_TESTNET, demo=BYBIT_DEMO, recv_window=RECV_WINDOW)
    discord = DiscordReader(DISCORD_TOKEN, CHANNEL_ID)
    engine = TradeEngine(bybit, st, log)

    log.info("="*58)
    mode_str = " | DRY_RUN" if DRY_RUN else ""
    mode_str += " | DEMO" if BYBIT_DEMO else ""
    mode_str += " | TESTNET" if BYBIT_TESTNET else ""
    log.info("Discord → Bybit Bot (One-way)" + mode_str)
    log.info("="*58)
    log.info(f"Config: CATEGORY={CATEGORY}, QUOTE={QUOTE}, LEVERAGE={LEVERAGE}x")
    log.info(f"Config: RISK_PCT={RISK_PCT}%, MAX_CONCURRENT={MAX_CONCURRENT_TRADES}, MAX_DAILY={MAX_TRADES_PER_DAY}")
    log.info(f"Config: POLL_SECONDS={POLL_SECONDS}, TC_MAX_LAG_SEC={TC_MAX_LAG_SEC}")
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

    # Heartbeat tracking
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 300  # Log heartbeat every 5 minutes

    # Signal update tracking (dynamic intervals: 60s for pending, 10s for open)
    last_signal_update_check_pending = time.time() - (SIGNAL_UPDATE_INTERVAL_SEC - 5)  # First check after 5 seconds
    last_signal_update_check_open = time.time() - (SIGNAL_UPDATE_INTERVAL_OPEN_SEC - 3)  # First check after 3 seconds

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
                bybit.run_private_ws(on_execution=on_execution, on_order=on_order, on_error=on_ws_error)
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

    # ----- main loop -----
    while True:
        try:
            # Heartbeat log every 5 minutes
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
                log.info(f"💓 Heartbeat: {len(active)} active trade(s), {trades_today()} today")
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

            # entry-fill fallback (polling) and post-orders placement
            for tid, tr in list(st.get("open_trades", {}).items()):
                if tr.get("status") == "pending":
                    # if position opened but ws missed: detect via positions size > 0
                    sz, avg = engine.position_size_avg(tr["symbol"])
                    if sz > 0 and avg > 0:
                        tr["status"] = "open"
                        tr["entry_price"] = avg
                        tr["filled_ts"] = time.time()
                        log.info(f"✅ ENTRY (poll) {tr['symbol']} @ {avg}")
                if tr.get("status") == "open" and not tr.get("post_orders_placed"):
                    engine.place_post_entry_orders(tr)

            # enforce concurrent trades
            active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
            if len(active) >= MAX_CONCURRENT_TRADES:
                log.info(f"Active trades {len(active)}/{MAX_CONCURRENT_TRADES} → skip new signals")
            elif trades_today() >= MAX_TRADES_PER_DAY:
                log.info(f"Trades today {trades_today()}/{MAX_TRADES_PER_DAY} → skip new signals")
            else:
                # read discord
                after = st.get("last_discord_id")
                log.debug(f"Polling Discord (after={after})...")
                try:
                    msgs = discord.fetch_after(after, limit=50)
                except Exception as e:
                    log.warning(f"Discord fetch failed: {e}")
                    msgs = []

                log.debug(f"Fetched {len(msgs)} message(s) from Discord")
                msgs_sorted = sorted(msgs, key=lambda m: int(m.get("id","0")))
                max_seen = int(after or 0)

                for m in msgs_sorted:
                    mid = int(m.get("id","0"))
                    max_seen = max(max_seen, mid)

                    # ignore very old messages
                    ts = discord.message_timestamp_unix(m)
                    age = time.time() - ts if ts else 0
                    if ts and age > TC_MAX_LAG_SEC:
                        log.debug(f"Skipping old message (age={age:.0f}s > {TC_MAX_LAG_SEC}s)")
                        continue

                    txt = discord.extract_text(m)
                    if not txt:
                        log.debug(f"Message {mid}: empty text, skipping")
                        continue

                    # Log first 200 chars of message for debugging
                    log.debug(f"Message {mid}: {txt[:200]}...")

                    sig = parse_signal(txt, quote=QUOTE)
                    if not sig:
                        # Check if it looks like a signal but failed to parse
                        if "SIGNAL" in txt.upper() or "ENTRY" in txt.upper():
                            log.warning(f"⚠️ Possible signal NOT parsed: {txt[:300]}...")
                        else:
                            log.debug(f"Message {mid}: not a signal")
                        continue

                    log.info(f"📨 Signal parsed: {sig['symbol']} {sig['side'].upper()} @ {sig['trigger']}")

                    sh = signal_hash(sig)
                    seen = set(st.get("seen_signal_hashes", []))
                    if sh in seen:
                        log.debug(f"Signal {sig['symbol']} already seen, skipping")
                        continue

                    # mark seen early
                    seen.add(sh)
                    st["seen_signal_hashes"] = list(seen)[-500:]

                    trade_id = f"{sig['symbol']}|{sig['side']}|{int(time.time())}"
                    log.info(f"🔄 Placing entry order for {sig['symbol']}...")
                    oid = engine.place_conditional_entry(sig, trade_id)
                    if not oid:
                        log.warning(f"❌ Entry order failed for {sig['symbol']}")
                        continue

                    # Get current equity for risk tracking
                    try:
                        equity_now = bybit.wallet_equity(ACCOUNT_TYPE)
                    except Exception:
                        equity_now = 0

                    # store trade
                    st.setdefault("open_trades", {})[trade_id] = {
                        "id": trade_id,
                        "symbol": sig["symbol"],
                        "order_side": "Sell" if sig["side"] == "sell" else "Buy",
                        "pos_side": "Short" if sig["side"] == "sell" else "Long",
                        "trigger": float(sig["trigger"]),
                        "tp_prices": sig.get("tp_prices") or [],
                        "tp_splits": None,  # engine uses config
                        "dca_prices": sig.get("dca_prices") or [],
                        "sl_price": sig.get("sl_price"),
                        "entry_order_id": oid,
                        "status": "pending",
                        "placed_ts": time.time(),
                        "base_qty": engine.calc_base_qty(sig["symbol"], float(sig["trigger"])),
                        "raw": sig.get("raw", ""),
                        "discord_msg_id": mid,  # Store Discord message ID for signal updates
                        # Risk & Leverage tracking (captured at trade creation)
                        "risk_pct": RISK_PCT,
                        "risk_amount": round(equity_now * RISK_PCT / 100, 2) if equity_now > 0 else None,
                        "equity_at_entry": round(equity_now, 2) if equity_now > 0 else None,
                        "leverage": LEVERAGE,
                    }
                    inc_trades_today()
                    log.info(f"🟡 ENTRY PLACED {sig['symbol']} {sig['side'].upper()} trigger={sig['trigger']} (id={trade_id})")

                    # Send Telegram notification for pending entry
                    telegram_alerts.send_entry_pending(
                        symbol=sig["symbol"],
                        side="Sell" if sig["side"] == "sell" else "Buy",
                        entry=float(sig["trigger"]),
                        qty=st["open_trades"][trade_id]["base_qty"]
                    )

                    # stop if we hit limits mid-batch
                    active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
                    if len(active) >= MAX_CONCURRENT_TRADES or trades_today() >= MAX_TRADES_PER_DAY:
                        break

                st["last_discord_id"] = str(max_seen) if max_seen else after

            save_state(STATE_FILE, st)

        except KeyboardInterrupt:
            log.info("Bye")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")
            time.sleep(3)

        time.sleep(max(1, POLL_SECONDS + random.uniform(0, max(0, POLL_JITTER_MAX))))

if __name__ == "__main__":
    main()
