"""trail_test_fast.py — single tick-walk per signal, records trail-exit
times for many trail-pct values in one pass. Then sweep is seconds.
"""
import gzip, json, sys, os, time
from collections import Counter
from tick_precise_backtest import (
    load_ticks, load_klines, find_entry_and_tp1_time,
    date_str, compound, LEV, FEE_M, FEE_T, SLIP
)

EVENTS_PATH = 'data/trail_events.json.gz'
TP1_PCTS = [0.8, 0.9, 1.0, 1.1, 1.2]
SL_PCTS = [0.7, 1.0]
TRAIL_PCTS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5]


def build_events():
    if os.path.exists(EVENTS_PATH):
        with gzip.open(EVENTS_PATH,'rt') as f: return json.load(f)

    PARTIAL = EVENTS_PATH + '.partial'
    events = []; done_ids = set()
    if os.path.exists(PARTIAL):
        try:
            with gzip.open(PARTIAL,'rt') as f: events = json.load(f)
            done_ids = {e['msg_id'] for e in events}
            print(f"Resume from {len(done_ids)} events", file=sys.stderr)
        except: events=[]; done_ids=set()

    with gzip.open('data/binance_clean.json.gz','rt') as f:
        sigs = [s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data']

    kbp = {}
    for s in sigs:
        if s['msg_id'] in done_ids: continue
        for off in (0, 86400):
            d = date_str(s['post']+off)
            kbp[(s['sym'], d)] = load_klines(s['sym'], d)

    last_log = 0; last_save = 0
    for i, sig in enumerate(sigs):
        if sig['msg_id'] in done_ids: continue
        if time.time() - last_log > 5:
            print(f"  {i}/{len(sigs)} (built {len(events)})", file=sys.stderr); last_log = time.time()

        sym = sig['sym']; trigger = float(sig['trigger'])
        post_ms = int(sig['post']) * 1000
        d_today = date_str(post_ms/1000); d_tom = date_str(post_ms/1000+86400)
        ev = {'msg_id': sig['msg_id'], 'sym': sym, 'trigger': trigger}
        entry_t, _ = find_entry_and_tp1_time(sig, kbp.get((sym, d_today)), kbp.get((sym, d_tom)))
        if entry_t is None:
            ev['state'] = 'never_filled'; events.append(ev); continue
        ticks_today = load_ticks(sym, d_today) or []
        ticks_tom = load_ticks(sym, d_tom) or []
        all_ticks = sorted([t for t in ticks_today if t[0]>=entry_t]+ticks_tom, key=lambda x:x[0])
        walk_to = entry_t + 4*3600_000
        all_ticks = [t for t in all_ticks if t[0]<=walk_to]
        if not all_ticks:
            ev['state'] = 'no_ticks'; events.append(ev); continue

        # SL levels (price-up): trigger × (1+sl/100)
        sl_prices = {sl: trigger * (1 + sl/100) for sl in SL_PCTS}
        # TP1 levels (price-down): trigger × (1-tp1/100)
        tp1_prices = {tp1: trigger * (1 - tp1/100) for tp1 in TP1_PCTS}

        # Phase 1: pre-TP1 — record first SL-hit, and TP1-hit for each TP1 level
        sl_hit_t = {sl: None for sl in SL_PCTS}
        tp1_hit_t = {tp1: None for tp1 in TP1_PCTS}
        last_idx = len(all_ticks)
        for idx, (tm, p) in enumerate(all_ticks):
            for sl in SL_PCTS:
                if sl_hit_t[sl] is None and p >= sl_prices[sl]:
                    sl_hit_t[sl] = tm
            for tp1 in TP1_PCTS:
                if tp1_hit_t[tp1] is None and p <= tp1_prices[tp1]:
                    tp1_hit_t[tp1] = tm
            # early stop if all tracked
            if all(v is not None for v in sl_hit_t.values()) and all(v is not None for v in tp1_hit_t.values()):
                break

        # Phase 2: trail mode — for each (tp1, trail) pair, find exit
        # Trail-exit price = (post-tp1 min) × (1 + trail/100)
        trail_exit = {}  # (tp1, trail) → (exit_t, exit_price)
        for tp1 in TP1_PCTS:
            tp_t = tp1_hit_t[tp1]
            if tp_t is None: continue
            lowest = tp1_prices[tp1]
            # Init trail-exit-needed thresholds
            trail_done = {trail: None for trail in TRAIL_PCTS}
            for tm, p in all_ticks:
                if tm < tp_t: continue
                if p < lowest:
                    lowest = p
                for trail in TRAIL_PCTS:
                    if trail_done[trail] is not None: continue
                    if p >= lowest * (1 + trail/100):
                        trail_done[trail] = (tm, lowest * (1 + trail/100))
                if all(v is not None for v in trail_done.values()):
                    break
            for trail in TRAIL_PCTS:
                if trail_done[trail] is not None:
                    trail_exit[f"{tp1}_{trail}"] = trail_done[trail]
                else:
                    # No retracement — use last price
                    trail_exit[f"{tp1}_{trail}"] = (all_ticks[-1][0], all_ticks[-1][1])

        ev['sl_hit_t'] = sl_hit_t
        ev['tp1_hit_t'] = tp1_hit_t
        ev['trail_exit'] = trail_exit
        events.append(ev)

        if time.time() - last_save > 30:
            with gzip.open(PARTIAL,'wt') as f:
                json.dump(events, f, separators=(',',':'), default=str)
            last_save = time.time()

    with gzip.open(EVENTS_PATH,'wt') as f:
        json.dump(events, f, separators=(',',':'), default=str)
    try: os.unlink(PARTIAL)
    except: pass
    return events


def simulate(ev, tp1, sl, trail):
    if ev.get('state') in ('never_filled','no_ticks'): return 0.0
    sl_t = (ev.get('sl_hit_t') or {}).get(str(sl)) or (ev.get('sl_hit_t') or {}).get(sl)
    tp1_t = (ev.get('tp1_hit_t') or {}).get(str(tp1)) or (ev.get('tp1_hit_t') or {}).get(tp1)
    if sl_t is not None and (tp1_t is None or sl_t < tp1_t):
        return -sl*LEV - (FEE_T+SLIP) - FEE_M
    if tp1_t is None:
        return 0.0
    # Trail mode
    exit_data = (ev.get('trail_exit') or {}).get(f"{tp1}_{trail}")
    if exit_data is None:
        return tp1*LEV - FEE_M - FEE_M  # fallback: exit at TP1
    exit_t, exit_price = exit_data
    trigger = ev['trigger']
    exit_pct = (trigger - exit_price) / trigger * 100  # positive = profit for SHORT
    return exit_pct*LEV - (FEE_T+SLIP) - FEE_M


def main():
    print("Building trail events (single tick walk per signal)...", file=sys.stderr)
    events = build_events()
    print(f"\nLoaded {len(events)} events. Sweeping configs...\n")

    print("="*90)
    print("TRAILING-STOP SWEEP (tick-precise, single-walk-per-signal)")
    print("="*90)
    print(f"{'TP1':>4} {'SL':>4} {'Trail':>6} | {'EV/sig':>9} {'WR':>6} {'$1k →':>11} {'DD':>5}")
    print('-'*90)
    results = []
    for tp1 in TP1_PCTS:
        for sl in SL_PCTS:
            for trail in TRAIL_PCTS:
                pnls = [simulate(ev, tp1, sl, trail) for ev in events]
                ev_avg = sum(pnls)/len(pnls)
                wr = sum(1 for p in pnls if p>0)/len(pnls)*100
                final, dd = compound(pnls)
                results.append((ev_avg, tp1, sl, trail, wr, final, dd))

    results.sort(key=lambda x: -x[0])
    print("TOP 20:")
    for ev, tp1, sl, trail, wr, f, dd in results[:20]:
        print(f"{tp1:>3.1f}% {sl:>3.1f}% {trail:>5.2f}% | {ev:>+8.3f}% {wr:>5.1f}% ${f:>9,.0f} {dd*100:>4.0f}%")

    print(f"\nBASELINE [100,0,0] no-trail TP=1.1/SL=0.7: +2.98% EV, $17,750, 24% DD")
    print(f"{len([r for r in results if r[0]>0])}/{len(results)} profitable")


if __name__ == '__main__':
    main()
