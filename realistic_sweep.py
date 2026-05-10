"""realistic_sweep.py — applies the live bot's filter rules to the
tick-precise event log. Tests each filter ON vs OFF separately so we
can see where the bot's defaults are leaving EV on the table.

Filters modelled:
1. PRE_FLIGHT — skip if first 1m candle's open after `post` is <= TP1
   (= price already past TP1 at signal time, "stale" signal).
2. ENTRY_WATCHER_CANCEL — if any 1m candle between `post` and `entry_t`
   has `low <= tp1`, the order would have been cancelled by the live
   ticker watcher before it could fill. Treat as 'cancelled'.
3. ENTRY_TOO_FAR — skip if first kline's open is more than
   ENTRY_TOO_FAR_PCT (default 0.8%) past trigger.
4. MAX_CONCURRENT_TRADES (state-machine): chronological pass over
   signals, track currently-open positions, skip when limit reached.
5. MAX_TRADES_PER_DAY (state-machine): per-day counter.
6. BLACKLIST_SYMBOLS: drop signals whose base symbol is blacklisted.
"""
import gzip, json, sys, os, time
from collections import defaultdict
from event_sweep import simulate_from_events
from tick_precise_backtest import load_klines, date_str, compound

# Best config from sweep
SL = 1.0
SPLITS = [10, 30, 60]
BE = 0.7

ENTRY_TOO_FAR_PCT = 0.8
MAX_CONCURRENT = 4
MAX_DAILY = 25
BLACKLIST_DEFAULT = {'HIGH'}


def first_kline_after(sym, post_ms):
    """Return first 1m kline at-or-after post_ms across day-of and day-after."""
    candles = []
    for off in (0, 86400):
        d = date_str((post_ms / 1000) + off)
        kl = load_klines(sym, d)
        if kl: candles.extend(kl)
    candles.sort(key=lambda k: k[0])
    for k in candles:
        if k[0] >= post_ms:
            return k
    return None


def load_signals_with_meta():
    with gzip.open('data/binance_clean.json.gz', 'rt') as f:
        sigs = [s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data']
    with gzip.open('data/tick_events.json.gz', 'rt') as f:
        events = json.load(f)
    by_id = {e['msg_id']: e for e in events}
    out = []
    for s in sigs:
        ev = by_id.get(s['msg_id'])
        if not ev: continue
        out.append({**s, '_event': ev})
    return out


def apply_filters_static(sig, *, pre_flight, too_far, blacklist, watcher_cancel):
    """Returns (skip_reason, pnl). pnl is 0 if skipped, else from event sim."""
    base = sig['base'].upper()
    if blacklist and base in blacklist:
        return ('blacklist', 0.0)

    sym = sig['sym']
    trigger = float(sig['trigger']); tp1 = float(sig['tp1'])
    post_ms = int(sig['post']) * 1000

    # Pre-flight & too-far need the first kline open price
    if pre_flight or too_far:
        fk = first_kline_after(sym, post_ms)
        if fk:
            open_price = fk[1]
            if pre_flight and open_price <= tp1:
                return ('preflight_past_tp1', 0.0)
            if too_far:
                # SHORT: skip if open already 0.8% below trigger
                if open_price <= trigger * (1 - ENTRY_TOO_FAR_PCT / 100):
                    return ('too_far', 0.0)

    # Entry-watcher cancel: any candle between post and entry_t with low <= tp1
    if watcher_cancel:
        ev = sig['_event']
        entry_t = ev.get('entry_t')
        if entry_t is None:
            # Never filled in tick data — bot would also expire it. Treat as 0.
            return ('never_filled', 0.0)
        # Walk klines in [post_ms, entry_t)
        candles = []
        for off in (0, 86400):
            d = date_str((post_ms / 1000) + off)
            kl = load_klines(sym, d)
            if kl: candles.extend(kl)
        for k in candles:
            kt = k[0]; low = k[3]
            if kt >= post_ms and kt < entry_t:
                if low <= tp1:
                    return ('watcher_cancel', 0.0)

    # Run simulation
    pnl = simulate_from_events(sig['_event'], SL, SPLITS, BE)
    return (None, pnl)


def apply_filters_with_state(sigs_with_meta, **kwargs):
    """Chronological pass with MAX_CONCURRENT + MAX_DAILY state machine."""
    sigs_sorted = sorted(sigs_with_meta, key=lambda s: int(s['post']))
    use_concurrent = kwargs.pop('max_concurrent', None)
    use_daily = kwargs.pop('max_daily', None)

    open_trades = []  # list of (close_t_ms, pnl)
    daily_count = defaultdict(int)
    results = []

    for sig in sigs_sorted:
        post_ms = int(sig['post']) * 1000
        d = date_str(sig['post'])

        # Close any trades that ended before this signal
        open_trades = [(ct, p) for ct, p in open_trades if ct > post_ms]

        # Apply non-state filters first
        reason, pnl = apply_filters_static(sig, **kwargs)
        if reason:
            results.append((sig, reason, 0.0))
            continue

        # State filters
        if use_concurrent is not None and len(open_trades) >= use_concurrent:
            results.append((sig, 'max_concurrent', 0.0))
            continue
        if use_daily is not None and daily_count[d] >= use_daily:
            results.append((sig, 'max_daily', 0.0))
            continue

        # Trade taken — determine close time from event
        ev = sig['_event']
        entry_t = ev.get('entry_t')
        # estimate close time as max of relevant event times
        close_t = entry_t if entry_t else post_ms
        for k in ('tp2_t', 'tp3_t'):
            t = ev.get(k)
            if t and t > close_t: close_t = t
        for d_be, t in (ev.get('be_hit_t') or {}).items():
            if t and t > close_t:
                close_t = t
        if not close_t or close_t == entry_t:
            # No exit found — assume 4h hold
            close_t = (entry_t or post_ms) + 4 * 3600_000

        open_trades.append((close_t, pnl))
        daily_count[d] += 1
        results.append((sig, None, pnl))

    return results


def report(label, results):
    pnls = [r[2] for r in results]
    skip_reasons = defaultdict(int)
    for s, reason, p in results:
        if reason: skip_reasons[reason] += 1
    n = len(results)
    n_traded = sum(1 for s, r, _ in results if r is None)
    n_skip = n - n_traded
    ev = sum(pnls) / n if n else 0
    ev_per_traded = sum(pnls) / n_traded if n_traded else 0
    wr_traded = sum(1 for s, r, p in results if r is None and p > 0) / max(n_traded, 1) * 100
    final, dd = compound(pnls)

    print(f"\n{'='*72}\n{label}\n{'='*72}")
    print(f"  Signals: {n}, Traded: {n_traded}, Skipped: {n_skip}")
    print(f"  Skip reasons: {dict(skip_reasons)}")
    print(f"  EV/sig: {ev:+.3f}%   EV/traded: {ev_per_traded:+.3f}%")
    print(f"  WR (of traded): {wr_traded:.1f}%   $1k → ${final:,.0f}   MaxDD: {dd*100:.0f}%")


def main():
    print("Loading signals + events...", file=sys.stderr)
    sigs = load_signals_with_meta()
    print(f"  → {len(sigs)} signals", file=sys.stderr)

    # Baseline: nothing filtered (matches earlier sweep result)
    res = apply_filters_with_state(sigs, pre_flight=False, too_far=False,
                                    blacklist=None, watcher_cancel=False)
    report("BASELINE — no filters (matches event_sweep result)", res)

    # ON only Pre-flight (matches bot's check)
    res = apply_filters_with_state(sigs, pre_flight=True, too_far=False,
                                    blacklist=None, watcher_cancel=False)
    report("PRE-FLIGHT ON (skip if first-kline open <= TP1)", res)

    # OFF Pre-flight (test if disabling helps)
    res = apply_filters_with_state(sigs, pre_flight=False, too_far=False,
                                    blacklist=None, watcher_cancel=False,
                                    max_concurrent=MAX_CONCURRENT, max_daily=MAX_DAILY)
    report("PRE-FLIGHT OFF + MAX_CONCURRENT/DAILY (current bot)", res)

    # FULL bot config: pre-flight + too-far + watcher-cancel + max_concurrent + daily + HIGH-blacklist
    res = apply_filters_with_state(sigs, pre_flight=True, too_far=True,
                                    blacklist=BLACKLIST_DEFAULT, watcher_cancel=True,
                                    max_concurrent=MAX_CONCURRENT, max_daily=MAX_DAILY)
    report("FULL BOT REALITY (all filters + HIGH blacklist)", res)

    # FULL bot but WITHOUT pre-flight
    res = apply_filters_with_state(sigs, pre_flight=False, too_far=True,
                                    blacklist=BLACKLIST_DEFAULT, watcher_cancel=True,
                                    max_concurrent=MAX_CONCURRENT, max_daily=MAX_DAILY)
    report("FULL BOT minus pre-flight (test if disabling helps)", res)

    # FULL bot + HIGH unblocked (test if HIGH viable with new config)
    res = apply_filters_with_state(sigs, pre_flight=True, too_far=True,
                                    blacklist=set(), watcher_cancel=True,
                                    max_concurrent=MAX_CONCURRENT, max_daily=MAX_DAILY)
    report("FULL BOT + HIGH unblocked", res)

    # Just HIGH signals
    high_sigs = [s for s in sigs if s['base'].upper() == 'HIGH']
    if high_sigs:
        res_high = apply_filters_with_state(high_sigs, pre_flight=False, too_far=False,
                                            blacklist=None, watcher_cancel=False)
        report(f"HIGH-only ({len(high_sigs)} signals, no filters)", res_high)


if __name__ == '__main__':
    main()
