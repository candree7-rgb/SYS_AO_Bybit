"""event_sweep.py — single-pass tick walk per signal records all critical
events (entry, TP1/2/3 first hits, multiple SL/BE level first crosses).
Then config sweep is just a lookup table — runs in seconds, not hours."""
import gzip, json, sys, os
from collections import Counter
from tick_precise_backtest import (
    load_ticks, load_klines, find_entry_and_tp1_time,
    cache_path_tick, date_str, compound, LEV, TP_PCTS, FEE_M, FEE_T, SLIP
)

EVENTS_PATH = 'data/tick_events.json.gz'

def first_cross_above(ticks, level, t_start):
    for tm, p in ticks:
        if tm < t_start: continue
        if p >= level: return tm
    return None

def first_cross_below(ticks, level, t_start):
    for tm, p in ticks:
        if tm < t_start: continue
        if p <= level: return tm
    return None


def build_events():
    """For each signal, walk ticks once. Record times of:
    - entry_fill (first high >= trigger)
    - tp1_hit, tp2_hit, tp3_hit (first time price drops to each TP)
    - sl_initial_hit at +1.0%, +1.5%, +2.0%, +2.5%, +3.0% (first time price rises to each)
    - be_lvl_hit at trigger × (1 - X/100) for X in [0, 0.1, 0.3, 0.5, 0.7]
    """
    if os.path.exists(EVENTS_PATH):
        print(f"Loading cached events from {EVENTS_PATH}", file=sys.stderr)
        with gzip.open(EVENTS_PATH, 'rt') as f:
            return json.load(f)

    print("Building events from tick walks...", file=sys.stderr)
    with gzip.open('data/binance_clean.json.gz','rt') as f:
        sigs = [s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data']

    # Resume from partial checkpoint if exists
    PARTIAL = EVENTS_PATH + '.partial'
    events = []
    done_ids = set()
    if os.path.exists(PARTIAL):
        try:
            with gzip.open(PARTIAL, 'rt') as f:
                events = json.load(f)
            done_ids = {e.get('msg_id') for e in events}
            print(f"  Resuming from partial: {len(done_ids)} events already built", file=sys.stderr)
        except Exception as e:
            print(f"  Partial corrupt ({e}), starting fresh", file=sys.stderr)
            events = []; done_ids = set()

    # Pre-load klines (only for sigs we still need)
    kbp = {}
    for s in sigs:
        if s['msg_id'] in done_ids: continue
        for off in (0, 86400):
            d = date_str(s['post']+off)
            kbp[(s['sym'], d)] = load_klines(s['sym'], d)

    SLS = [1.0, 1.5, 2.0, 2.5, 3.0]
    BES = [0.0, 0.1, 0.3, 0.5, 0.7]

    last_log = 0
    last_save = 0
    import time as _t
    for i, sig in enumerate(sigs):
        if sig['msg_id'] in done_ids:
            continue
        if _t.time() - last_log > 5:
            print(f"  {i}/{len(sigs)} (built so far: {len(events)})", file=sys.stderr); last_log = _t.time()
        sym = sig['sym']; trigger = float(sig['trigger'])
        tp1, tp2, tp3 = float(sig['tp1']), float(sig['tp2']), float(sig['tp3'])
        post_ms = int(sig['post']) * 1000
        d_today = date_str(post_ms/1000); d_tom = date_str(post_ms/1000 + 86400)

        ev = {'msg_id': sig['msg_id'], 'sym': sym}
        entry_t, _ = find_entry_and_tp1_time(sig, kbp.get((sym, d_today)), kbp.get((sym, d_tom)))
        if entry_t is None:
            ev['state'] = 'never_filled'
            events.append(ev); continue

        ticks_today = load_ticks(sym, d_today) or []
        ticks_tom = load_ticks(sym, d_tom) or []
        all_ticks = sorted([t for t in ticks_today if t[0] >= entry_t] + ticks_tom,
                            key=lambda x: x[0])
        walk_to = entry_t + 4*3600_000
        all_ticks = [t for t in all_ticks if t[0] <= walk_to]
        if not all_ticks:
            ev['state'] = 'no_ticks'; events.append(ev); continue

        ev['entry_t'] = entry_t

        # SINGLE pass: track all interesting first-cross times
        sl_levels = {sl: trigger * (1 + sl/100) for sl in SLS}
        be_levels = {be: trigger * (1 - be/100) for be in BES}

        sl_hit_t = {sl: None for sl in SLS}
        be_hit_t = {be: None for be in BES}
        tp1_t = tp2_t = tp3_t = None

        for tm, p in all_ticks:
            if tp1_t is None and p <= tp1:
                tp1_t = tm
            if tp2_t is None and p <= tp2:
                tp2_t = tm
            if tp3_t is None and p <= tp3:
                tp3_t = tm
            for sl in SLS:
                if sl_hit_t[sl] is None and p >= sl_levels[sl]:
                    sl_hit_t[sl] = tm
            for be in BES:
                if be_hit_t[be] is None and p >= be_levels[be]:
                    be_hit_t[be] = tm
            # Early exit: if everything tracked, stop
            if (tp1_t and tp2_t and tp3_t and
                all(v is not None for v in sl_hit_t.values()) and
                all(v is not None for v in be_hit_t.values())):
                break

        ev['tp1_t'] = tp1_t; ev['tp2_t'] = tp2_t; ev['tp3_t'] = tp3_t
        ev['sl_hit_t'] = sl_hit_t
        ev['be_hit_t'] = be_hit_t
        events.append(ev)

        # Checkpoint every 30s — survives container restarts
        if _t.time() - last_save > 30:
            with gzip.open(PARTIAL, 'wt') as f:
                json.dump(events, f, separators=(',', ':'), default=str)
            last_save = _t.time()

    with gzip.open(EVENTS_PATH, 'wt') as f:
        json.dump(events, f, separators=(',', ':'), default=str)
    try: os.unlink(PARTIAL)
    except FileNotFoundError: pass
    print(f"Saved events to {EVENTS_PATH}", file=sys.stderr)
    return events


def simulate_from_events(ev, sl_pct, splits, be_buf):
    """Compute PnL using only the recorded event timeline. No tick walking."""
    state = ev.get('state')
    if state in ('never_filled','no_ticks'):
        return 0.0
    sl_hit_t = ev.get('sl_hit_t') or {}
    be_hit_t = ev.get('be_hit_t') or {}
    sl_t = sl_hit_t.get(str(sl_pct)) or sl_hit_t.get(sl_pct)
    be_t = be_hit_t.get(str(be_buf)) or be_hit_t.get(be_buf)
    tp1_t = ev.get('tp1_t')
    tp2_t = ev.get('tp2_t')
    tp3_t = ev.get('tp3_t')

    # Phase 1: pre-TP1 — does SL hit before TP1?
    if sl_t is not None and (tp1_t is None or sl_t < tp1_t):
        return -sl_pct*LEV - (FEE_T+SLIP) - FEE_M
    if tp1_t is None:
        return 0.0  # no TP1, no SL → no event

    # TP1 hit
    s1, s2, s3 = splits
    pnl = TP_PCTS[0]*(s1/100)*LEV
    fees = FEE_M + FEE_M*(s1/100)
    pos = 100 - s1

    # Phase 2: post-TP1, BE armed (if placeable)
    be_lvl_above_tp1 = (1 - be_buf/100) > (1 - 0.8/100)  # be < tp1_pct = 0.8
    # Find next event AFTER tp1_t: BE-fire (if placeable) OR original SL OR TP2/3
    candidates = []
    if be_lvl_above_tp1 and be_t is not None and be_t > tp1_t:
        candidates.append(('be', be_t))
    if not be_lvl_above_tp1 and sl_t is not None and sl_t > tp1_t:
        candidates.append(('sl_orig', sl_t))
    if tp2_t is not None and tp2_t > tp1_t:
        candidates.append(('tp2', tp2_t))
    if tp3_t is not None and tp3_t > tp1_t:
        candidates.append(('tp3', tp3_t))

    candidates.sort(key=lambda x: x[1])

    for kind, _ in candidates:
        if kind == 'be':
            pnl += be_buf*(pos/100)*LEV
            fees += (FEE_T+SLIP)*(pos/100)
            return pnl - fees
        if kind == 'sl_orig':
            pnl += -sl_pct*LEV*(pos/100)
            fees += (FEE_T+SLIP)*(pos/100)
            return pnl - fees
        if kind == 'tp2' and pos > 0 and s2 > 0:
            pnl += TP_PCTS[1]*(s2/100)*LEV
            fees += FEE_M*(s2/100)
            pos -= s2
            if pos < 0.001: return pnl - fees
            continue
        if kind == 'tp3' and pos > 0 and s3 > 0:
            pnl += TP_PCTS[2]*(s3/100)*LEV
            fees += FEE_M*(s3/100)
            pos -= s3
            if pos < 0.001: return pnl - fees
            continue

    return pnl - fees


def main():
    events = build_events()

    print(f"\nSweeping configs over {len(events)} events...", file=sys.stderr)
    SLS = [1.0, 1.5, 2.0, 2.5, 3.0]
    BES = [0.0, 0.1, 0.3, 0.5, 0.7]
    SPLITS = [
        [100,0,0], [50,50,0], [33,33,34], [20,30,50], [10,30,60],
        [0,100,0], [5,95,0], [10,90,0], [25,75,0]
    ]

    results = []
    for sl in SLS:
        for sp in SPLITS:
            for be in BES:
                pnls = [simulate_from_events(ev, sl, sp, be) for ev in events]
                evavg = sum(pnls)/len(pnls)
                wr = sum(1 for p in pnls if p > 0)/len(pnls)*100
                final, dd = compound(pnls)
                results.append((evavg, sl, sp, be, wr, final, dd))

    results.sort(key=lambda x: -x[0])
    print(f"\n{'='*80}\nTOP 20 CONFIGS (tick-precise truth, {len(events)} signals)\n{'='*80}")
    print(f"{'SL%':>5} {'splits':<14} {'BE+':>5} | {'EV/sig':>9} {'WR':>6} {'$1k →':>11} {'MaxDD':>7}")
    print('-'*75)
    for ev, sl, sp, bb, wr, f, dd in results[:20]:
        print(f"{sl:>4.1f}% {str(sp):<14} {bb:>4.1f}% | {ev:>+8.3f}% {wr:>5.1f}% ${f:>9,.0f} {dd*100:>6.0f}%")

    print(f"\nBOTTOM 5 (worst):")
    for ev, sl, sp, bb, wr, f, dd in results[-5:]:
        print(f"{sl:>4.1f}% {str(sp):<14} {bb:>4.1f}% | {ev:>+8.3f}% {wr:>5.1f}% ${f:>9,.0f} {dd*100:>6.0f}%")

    # Summary
    pos = [r for r in results if r[0] > 0]
    print(f"\n{len(pos)}/{len(results)} configs are profitable (positive EV)")


if __name__ == '__main__':
    main()
