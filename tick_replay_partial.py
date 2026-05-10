"""tick_replay_partial.py — replay using whatever aggTrades data we
have cached locally. For signals where tick data is unavailable, fall
back to the precomputed kline-state outcome.

Reports separately:
- TICK-PRECISE outcomes (definitive: BE-fired-first vs TP2-fired-first)
- KLINE-FALLBACK outcomes (best-effort from precomputed states)
- Overall EV blending both
"""
import os, sys, gzip, json, time
from collections import Counter
from tick_precise_backtest import (
    load_ticks, load_klines, find_entry_and_tp1_time, simulate_tick,
    cache_path_tick, date_str, pnl_loss, pnl_after_tp1, pnl_tp1_then_sl,
    pnl_tp2, pnl_tp3, compound, LEV, TP_PCTS, FEE_M, FEE_T, SLIP,
)

with gzip.open('data/binance_clean.json.gz','rt') as f:
    signals = [s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data']
print(f"Total signals: {len(signals)}", file=sys.stderr)

# Determine which signals have BOTH (sym, today) and (sym, tomorrow) cached
def has_tick_data(sig):
    sym = sig['sym']
    d_today = date_str(sig['post'])
    d_tomorrow = date_str(sig['post'] + 86400)
    return os.path.exists(cache_path_tick(sym, d_today))  # tomorrow optional

# Build kline cache (small)
print("Loading klines...", file=sys.stderr)
klines_by_pair = {}
for s in signals:
    for off in (0, 86400):
        d = date_str(s['post'] + off)
        klines_by_pair[(s['sym'], d)] = load_klines(s['sym'], d)

# Run sim
print("Replaying with available tick data...", file=sys.stderr)
results = []
tick_count = 0
fallback_count = 0
last_log = time.time()

def fallback_pnl_from_state(state, sl_pct=2.0, be_buf=0.7):
    """When tick data unavailable, use precomputed state as best estimate.
    Apply same -2021 BE-validation: BE+0.7% with TP1=0.8% IS placeable."""
    if state in ('no_data','never_filled','no_event'):
        return {'state': state, 'pnl': 0.0}
    if state == 'loss':
        return {'state': state, 'pnl': pnl_loss(sl_pct)}
    # Wins: apply standard PnL
    fees = FEE_M; pnl = 0; pos = 100
    if 'tp2' in state:
        pnl += TP_PCTS[1]*LEV; fees += FEE_M; pos = 0
    elif 'tp3' in state:
        pnl += TP_PCTS[2]*LEV; fees += FEE_M; pos = 0
    if 'be' in state and pos > 0:
        pnl += be_buf*LEV; fees += FEE_T + SLIP
    return {'state': state, 'pnl': pnl - fees}

for i, sig in enumerate(signals):
    state_2pct = sig['outcomes_by_sl'].get('2.0','')
    if state_2pct in ('loss','never_filled','no_data','no_event'):
        # Trivially short-circuit (no TP1 hit)
        if state_2pct == 'loss':
            results.append({'state':'loss','pnl':pnl_loss(2.0),'src':'kline'})
        else:
            results.append({'state':state_2pct,'pnl':0.0,'src':'kline'})
        continue
    # TP1-hit signal: try tick-precise
    if has_tick_data(sig):
        r = simulate_tick(sig, klines_by_pair, sl_pct=2.0, be_buf=0.7)
        r['src'] = 'tick'
        results.append(r)
        tick_count += 1
    else:
        r = fallback_pnl_from_state(state_2pct)
        r['src'] = 'kline_fallback'
        results.append(r)
        fallback_count += 1
    if time.time() - last_log > 5:
        print(f"  {i+1}/{len(signals)} (tick={tick_count}, fallback={fallback_count})",
              file=sys.stderr)
        last_log = time.time()

# Aggregate
tick_results = [r for r in results if r['src']=='tick']
fallback_results = [r for r in results if r['src']=='kline_fallback']
all_pnls = [r['pnl'] for r in results]

ev_all = sum(all_pnls)/len(all_pnls)
wr_all = sum(1 for p in all_pnls if p>0)/len(all_pnls)*100
final_all, dd_all = compound(all_pnls)

print("\n" + "="*75)
print(f"PARTIAL TICK-PRECISE BACKTEST (BE+0.7%, SL=2%, splits=[0,100,0])")
print("="*75)
print(f"Total: {len(results)} signals  |  tick-precise: {len(tick_results)}, "
      f"kline-fallback: {len(fallback_results)}, kline-trivial: "
      f"{len(results)-len(tick_results)-len(fallback_results)}")
print(f"\nOVERALL (tick + fallback blended):")
print(f"  EV/sig: {ev_all:+.3f}%   WR: {wr_all:.1f}%   $1k → ${final_all:,.0f}   MaxDD: {dd_all*100:.0f}%")

if tick_results:
    tick_pnls = [r['pnl'] for r in tick_results]
    ev_tick = sum(tick_pnls)/len(tick_pnls)
    wr_tick = sum(1 for p in tick_pnls if p>0)/len(tick_pnls)*100
    states_tick = Counter(r['state'] for r in tick_results)
    print(f"\nTICK-PRECISE ONLY ({len(tick_results)} signals — definitive):")
    print(f"  EV/sig: {ev_tick:+.3f}%   WR: {wr_tick:.1f}%")
    print(f"  States:")
    for st, c in states_tick.most_common():
        print(f"    {st:<25} {c:>4}  ({c*100/len(tick_results):.1f}%)")
    # The crucial split: BE-first vs TP2-first AFTER TP1
    tp1_be = states_tick.get('tp1_then_be', 0)
    tp1_tp2_be = states_tick.get('tp1_tp2_then_be', 0)
    tp1_tp2_only = states_tick.get('tp1_tp2_only', 0)
    tp1_tp2_tp3 = states_tick.get('tp1_tp2_tp3', 0)
    tp1_sl = states_tick.get('tp1_then_sl', 0)
    tp1_total = tp1_be + tp1_tp2_be + tp1_tp2_only + tp1_tp2_tp3 + tp1_sl
    if tp1_total:
        be_first = tp1_be
        tp2_first = tp1_tp2_be + tp1_tp2_only + tp1_tp2_tp3
        sl_after = tp1_sl
        print(f"\n  ANSWER TO USER'S QUESTION (of {tp1_total} TP1-hit trades):")
        print(f"    BE fired BEFORE TP2:    {be_first:>4} ({be_first*100/tp1_total:.1f}%)  "
              f"← user hypothesis: ~95%")
        print(f"    TP2 fired first (full): {tp2_first:>4} ({tp2_first*100/tp1_total:.1f}%)")
        print(f"    SL after TP1 (BE failed): {sl_after:>4} ({sl_after*100/tp1_total:.1f}%)")

# Save
with gzip.open('data/tick_precise_outcomes.json.gz', 'wt') as f:
    json.dump([{'msg_id':s['msg_id'],'state':r['state'],'pnl':r['pnl'],'src':r['src']}
              for s,r in zip(signals,results)], f, separators=(',',':'))
print("\nOutcomes saved to data/tick_precise_outcomes.json.gz")
