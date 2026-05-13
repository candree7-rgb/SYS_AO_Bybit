"""trail_test.py — tick-precise simulation of trailing stop after TP1.

Logic:
1. Place LIMIT @ trigger; SL @ trigger × (1 + sl_pct/100).
2. If pre-TP1 SL hits → loss.
3. Once TP1 hits at price tp1_p:
   - Switch to TRAILING_STOP mode.
   - Track post-TP1 lowest price (SHORT favours lower prices).
   - Exit on FIRST tick where price >= lowest × (1 + trail_pct/100).
4. PnL = (entry - exit_price) / entry × 100 × LEV - fees.
"""
import gzip, json, sys, os
from tick_precise_backtest import (
    load_ticks, load_klines, find_entry_and_tp1_time,
    date_str, compound, LEV, FEE_M, FEE_T, SLIP
)


def simulate_trail(sig, klines_by_pair, tp1_pct, sl_pct, trail_pct):
    sym = sig['sym']; trigger = float(sig['trigger'])
    tp1_price = trigger * (1 - tp1_pct/100)
    sl_price = trigger * (1 + sl_pct/100)
    post_ms = int(sig['post']) * 1000
    d_today = date_str(post_ms/1000); d_tom = date_str(post_ms/1000 + 86400)
    kt = klines_by_pair.get((sym, d_today)); ktom = klines_by_pair.get((sym, d_tom))
    entry_t, _ = find_entry_and_tp1_time(sig, kt, ktom)
    if entry_t is None:
        return ('never_filled', 0.0)
    ticks_today = load_ticks(sym, d_today) or []
    ticks_tom = load_ticks(sym, d_tom) or []
    all_ticks = sorted([t for t in ticks_today if t[0] >= entry_t] + ticks_tom, key=lambda x: x[0])
    walk_to = entry_t + 4 * 3600_000
    all_ticks = [t for t in all_ticks if t[0] <= walk_to]
    if not all_ticks:
        return ('no_ticks', 0.0)

    # Phase 1: pre-TP1
    tp1_hit_t = None
    for tm, p in all_ticks:
        if p >= sl_price:
            return ('loss', -sl_pct*LEV - (FEE_T+SLIP) - FEE_M)
        if p <= tp1_price:
            tp1_hit_t = tm
            break
    if tp1_hit_t is None:
        return ('no_event', 0.0)

    # Phase 2: trailing mode after TP1
    lowest = tp1_price  # initial extreme
    for tm, p in all_ticks:
        if tm < tp1_hit_t: continue
        if p < lowest:
            lowest = p
        trail_trigger = lowest * (1 + trail_pct/100)
        if p >= trail_trigger:
            # Exit at trail trigger
            exit_pct = (trigger - trail_trigger) / trigger * 100
            return ('trail_exit', exit_pct*LEV - (FEE_T+SLIP) - FEE_M)

    # No trail fire in window — exit at last price
    last_price = all_ticks[-1][1]
    exit_pct = (trigger - last_price) / trigger * 100
    return ('window_exit', exit_pct*LEV - FEE_T - FEE_M)


def main():
    print("Loading signals + klines...", file=sys.stderr)
    with gzip.open('data/binance_clean.json.gz','rt') as f:
        sigs = [s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data']
    kbp = {}
    for s in sigs:
        for off in (0, 86400):
            d = date_str(s['post']+off)
            kbp[(s['sym'], d)] = load_klines(s['sym'], d)

    # Test matrix: TP1 distance × SL × trail percentage
    print("\n" + "="*95)
    print("TRAILING-STOP SWEEP (tick-precise, trail-pct after TP1 reached)")
    print("="*95)
    print(f"{'TP1':>5} {'SL':>5} {'Trail':>6} | {'EV/sig':>9} {'WR':>6} {'$1k →':>11} {'DD':>5}  state breakdown")
    print('-'*120)

    from collections import Counter
    import time as _time
    t_start = _time.time()

    results = []
    for tp1 in [0.8, 0.9, 1.0, 1.1, 1.2]:
        for sl in [0.7, 1.0]:
            for trail in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]:
                pnls = []
                states = Counter()
                for sig in sigs:
                    state, pnl = simulate_trail(sig, kbp, tp1, sl, trail)
                    pnls.append(pnl)
                    states[state] += 1
                ev = sum(pnls)/len(pnls)
                wr = sum(1 for p in pnls if p > 0)/len(pnls)*100
                final, dd = compound(pnls)
                top_states = ', '.join(f"{k}={v}" for k,v in states.most_common(3))
                results.append((ev, tp1, sl, trail, wr, final, dd, top_states))
                if _time.time() - t_start > 5:
                    print(f"  ... done {len(results)} configs", file=sys.stderr)
                    t_start = _time.time()

    results.sort(key=lambda x: -x[0])
    print("\nTOP 15:")
    for ev, tp1, sl, trail, wr, final, dd, st in results[:15]:
        print(f"{tp1:>4.1f}% {sl:>4.1f}% {trail:>5.2f}% | {ev:>+8.3f}% {wr:>5.1f}% ${final:>9,.0f} {dd*100:>4.0f}%  {st}")

    # Compare with [100,0,0] baseline (no trail)
    print(f"\nBASELINE (no trail, splits=[100,0,0], TP=1.1, SL=0.7):  +2.98% EV, $17,750, 24% DD")


if __name__ == '__main__':
    main()
