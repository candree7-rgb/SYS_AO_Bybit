import sys
import time
import random
import threading
import logging

from config import (
    DISCORD_TOKEN, CHANNEL_ID,
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_DEMO, RECV_WINDOW,
    CATEGORY, QUOTE, LEVERAGE, RISK_PCT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_DAY, TC_MAX_LAG_SEC,
    POLL_SECONDS, POLL_JITTER_MAX,
    STATE_FILE, DRY_RUN, LOG_LEVEL
)
from bybit_v5 import BybitV5
from discord_reader import DiscordReader
from signal_parser import parse_signal, signal_hash
from state import load_state, save_state, utc_day_key
from trade_engine import TradeEngine

def setup_logger() -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)  # stdout so Railway shows INFO as normal (not red)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    log.handlers[:] = [h]
    return log

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

    # Startup sync - check for orphaned positions
    engine.startup_sync()

    # Heartbeat tracking
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 300  # Log heartbeat every 5 minutes

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

            # maintenance first
            engine.cancel_expired_entries()
            engine.cleanup_closed_trades()
            engine.check_tp_fills_fallback()  # Catch TP1 fills if WS missed
            engine.log_daily_stats()  # Log stats once per day

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
                    }
                    inc_trades_today()
                    log.info(f"🟡 ENTRY PLACED {sig['symbol']} {sig['side'].upper()} trigger={sig['trigger']} (id={trade_id})")

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
