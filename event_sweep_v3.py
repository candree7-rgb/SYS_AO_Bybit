"""event_sweep_v3.py — extends event-building to track FIRST-cross times
for CUSTOM TP-levels (0.5/0.7/0.8/0.9/1.0/1.1/1.2/1.4/1.6/2.0/3.0/4.0 %),
so we can test arbitrary TP1 distances without re-walking ticks per config.

Sweep then tests TP1 distance × splits × BE-buffer combinations to find
the truly optimal config (not just the [100,0,0]-vs-default we saw in v2).
"""
import gzip, json, sys, os, time
from collections import Counter, defaultdict
from tick_precise_backtest import (
    load_ticks, load_klines, find_entry_and_tp1_time,
    cache_path_tick, date_str, compound, LEV, FEE_M, FEE_T, SLIP
)

EVENTS_PATH = 'data/tick_events_v3.json.gz'

# Each TP level traced as a downward-cross (price <= trigger × (1 - X/100))
TP_LEVELS = [0.5, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6, 2.0, 3.0, 4.0]
SL_LEVELS = [0.7, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]
BE_LEVELS = [0.0, 0.1, 0.3, 0.5, 0.7]


def build_events():
    if os.path.exists(EVENTS_PATH):
        print(f"Loading cached v3 events", file=sys.stderr)
        with gzip.open(EVENTS_PATH, 'rt') as f:
            return json.load(f)

    print("Building v3 events (more TP/SL levels, post-tp1 BE timing)...", file=sys.stderr)
    with gzip.open('data/binance_clean.json.gz','rt') as f:
        sigs = [s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data']

    PARTIAL = EVENTS_PATH + '.partial'
    events = []
    done_ids = set()
    if os.path.exists(PARTIAL):
        try:
            with gzip.open(PARTIAL,'rt') as f: events = json.load(f)
            done_ids = {e['msg_id'] for e in events}
            print(f"  Resume from {len(done_ids)} events", file=sys.stderr)
        except: events = []; done_ids = set()

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
        ev = {'msg_id': sig['msg_id'], 'sym': sym}
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
        ev['entry_t'] = entry_t

        tp_hit_t = {tp: None for tp in TP_LEVELS}
        sl_hit_t = {sl: None for sl in SL_LEVELS}
        tp_pcts = {tp: trigger * (1 - tp/100) for tp in TP_LEVELS}
        sl_pcts = {sl: trigger * (1 + sl/100) for sl in SL_LEVELS}
        first_tp_t = None  # first tp-cross (tp1 = 0.8% provider default)
        for tm, p in all_ticks:
            for tp in TP_LEVELS:
                if tp_hit_t[tp] is None and p <= tp_pcts[tp]:
                    tp_hit_t[tp] = tm
            for sl in SL_LEVELS:
                if sl_hit_t[sl] is None and p >= sl_pcts[sl]:
                    sl_hit_t[sl] = tm
            # First-tp = tp = 0.8 (provider's TP1)
            if first_tp_t is None and tp_hit_t[0.8] is not None:
                first_tp_t = tp_hit_t[0.8]
            # Early exit: if everything tracked
            if all(v is not None for v in tp_hit_t.values()) and all(v is not None for v in sl_hit_t.values()):
                break

        # BE levels: record FIRST cross post-first_tp_t (matches bot's BE arm time)
        be_hit_t = {be: None for be in BE_LEVELS}
        if first_tp_t is not None:
            be_lvls = {be: trigger * (1 - be/100) for be in BE_LEVELS}
            for tm, p in all_ticks:
                if tm < first_tp_t: continue
                for be in BE_LEVELS:
                    if be_hit_t[be] is None and p >= be_lvls[be]:
                        be_hit_t[be] = tm
                if all(v is not None for v in be_hit_t.values()): break

        ev['tp_hit_t'] = tp_hit_t
        ev['sl_hit_t'] = sl_hit_t
        ev['be_hit_t'] = be_hit_t
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


def sim(ev, tp1, tp2, tp3, sl, splits, be):
    """Sim with arbitrary TP1/TP2/TP3 distances + SL + splits + BE."""
    state = ev.get('state')
    if state in ('never_filled', 'no_ticks'):
        return 0.0
    tp_hit_t = ev.get('tp_hit_t') or {}
    sl_hit_t = ev.get('sl_hit_t') or {}
    be_hit_t = ev.get('be_hit_t') or {}
    tp1_t = tp_hit_t.get(str(tp1)) or tp_hit_t.get(tp1)
    tp2_t = tp_hit_t.get(str(tp2)) or tp_hit_t.get(tp2)
    tp3_t = tp_hit_t.get(str(tp3)) or tp_hit_t.get(tp3)
    sl_t = sl_hit_t.get(str(sl)) or sl_hit_t.get(sl)
    be_t = be_hit_t.get(str(be)) or be_hit_t.get(be)

    # Phase 1: pre-tp1
    if sl_t is not None and (tp1_t is None or sl_t < tp1_t):
        return -sl * LEV - (FEE_T + SLIP) - FEE_M
    if tp1_t is None:
        return 0.0

    s1, s2, s3 = splits
    pnl = tp1 * (s1/100) * LEV
    fees = FEE_M + FEE_M * (s1/100)
    pos = 100 - s1

    # Phase 2: post-tp1, BE armed if placeable (be must be ABOVE tp1 level)
    # For SHORT: BE level = trigger×(1-be/100). For BE to be ABOVE current
    # TP1 price (trigger×(1-tp1/100)): (1-be/100) > (1-tp1/100) → be < tp1.
    be_placeable = be < tp1

    candidates = []
    if be_placeable and be_t and be_t > tp1_t:
        candidates.append(('be', be_t))
    if not be_placeable and sl_t and sl_t > tp1_t:
        candidates.append(('sl_orig', sl_t))
    if tp2_t and tp2_t > tp1_t:
        candidates.append(('tp2', tp2_t))
    if tp3_t and tp3_t > tp1_t:
        candidates.append(('tp3', tp3_t))
    candidates.sort(key=lambda x: x[1])

    for kind, _ in candidates:
        if kind == 'be':
            pnl += be * (pos/100) * LEV
            fees += (FEE_T + SLIP) * (pos/100)
            return pnl - fees
        if kind == 'sl_orig':
            pnl += -sl * LEV * (pos/100)
            fees += (FEE_T + SLIP) * (pos/100)
            return pnl - fees
        if kind == 'tp2' and pos > 0 and s2 > 0:
            pnl += tp2 * (s2/100) * LEV
            fees += FEE_M * (s2/100)
            pos -= s2
            if pos <= 0.001: return pnl - fees
            continue
        if kind == 'tp3' and pos > 0 and s3 > 0:
            pnl += tp3 * (s3/100) * LEV
            fees += FEE_M * (s3/100)
            pos -= s3
            if pos <= 0.001: return pnl - fees
            continue
    return pnl - fees


def main():
    events = build_events()
    print(f"\nLoaded {len(events)} events")

    # Sweep: TP1 distance × splits × BE × (fixed TP2=1.6, TP3=4.0, SL=1.0)
    print("\n" + "="*100)
    print("TP1-DISTANCE SWEEP (SL=1.0%, TP2=1.6%, TP3=4.0%, BE varies)")
    print("="*100)
    print(f"{'TP1%':>5} {'splits':<14} {'BE+':>5} | {'EV/sig':>9} {'WR':>6} {'$1k →':>11} {'MaxDD':>7}")
    print('-'*80)
    results = []
    for tp1 in [0.7, 0.8, 0.9, 1.0, 1.1, 1.2]:
        for splits in [[100,0,0], [70,30,0], [50,50,0], [30,70,0], [10,90,0], [0,100,0],
                       [50,30,20], [33,33,34], [10,30,60]]:
            for be in [0.0, 0.1, 0.3, 0.5]:
                # Only use BE if be < tp1 (placeable)
                if be >= tp1: continue
                pnls = [sim(ev, tp1, 1.6, 4.0, 1.0, splits, be) for ev in events]
                evavg = sum(pnls)/len(pnls)
                wr = sum(1 for p in pnls if p>0)/len(pnls)*100
                final, dd = compound(pnls)
                results.append((evavg, tp1, splits, be, wr, final, dd))

    results.sort(key=lambda x: -x[0])
    print("\nTOP 25:")
    for ev, tp1, sp, be, wr, f, dd in results[:25]:
        print(f"{tp1:>4.1f}% {str(sp):<14} {be:>4.1f}% | {ev:>+8.3f}% {wr:>5.1f}% ${f:>9,.0f} {dd*100:>6.0f}%")

    print(f"\n{len([r for r in results if r[0]>0])}/{len(results)} configs profitable")


if __name__ == '__main__':
    main()
