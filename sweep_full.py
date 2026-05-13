"""sweep_full.py — comprehensive sweep over TP1/TP2/TP3 distances,
splits, SL widths, and BE buffers using v3 events. Runs in seconds.
"""
import gzip, json, sys
from event_sweep_v3 import sim
from tick_precise_backtest import compound

with gzip.open('data/tick_events_v3.json.gz','rt') as f:
    events = json.load(f)
print(f"Loaded {len(events)} events")

# Sweep matrix
TP1S = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
TP2S = [1.0, 1.2, 1.4, 1.6, 2.0]
TP3S = [2.0, 3.0, 4.0]
SLS = [0.7, 1.0, 1.2, 1.5, 2.0]
SPLITS = [
    [100,0,0],
    [70,30,0], [50,50,0], [30,70,0],
    [10,90,0], [0,100,0],
    [50,30,20], [33,33,34], [40,40,20], [25,25,50],
    [20,30,50], [10,30,60],
]
BES = [0.0, 0.3, 0.5]

print(f"Sweeping TP1×TP2×TP3×SL×splits×BE...")
results = []
n_total = 0; n_skipped = 0
for tp1 in TP1S:
    for tp2 in TP2S:
        if tp2 <= tp1: continue  # TP2 must be > TP1
        for tp3 in TP3S:
            if tp3 <= tp2: continue
            for sl in SLS:
                for sp in SPLITS:
                    for be in BES:
                        if be >= tp1: continue  # BE must be < TP1 (placeable)
                        n_total += 1
                        pnls = [sim(ev, tp1, tp2, tp3, sl, sp, be) for ev in events]
                        evavg = sum(pnls)/len(pnls)
                        wr = sum(1 for p in pnls if p>0)/len(pnls)*100
                        final, dd = compound(pnls)
                        results.append((evavg, tp1, tp2, tp3, sl, sp, be, wr, final, dd))

print(f"Tested {n_total} configs")
results.sort(key=lambda x: -x[0])
print(f"\n{len([r for r in results if r[0]>0])}/{len(results)} configs profitable\n")

print("="*110)
print("TOP 30 — TP1 × TP2 × TP3 × SL × splits × BE sweep (tick-precise, 1013 sigs)")
print("="*110)
print(f"{'TP1':>4} {'TP2':>4} {'TP3':>4} {'SL':>4} {'splits':<14} {'BE':>4} | {'EV/sig':>9} {'WR':>6} {'$1k →':>11} {'DD':>5}")
print('-'*110)
for ev,tp1,tp2,tp3,sl,sp,be,wr,f,dd in results[:30]:
    print(f"{tp1:>3.1f}% {tp2:>3.1f}% {tp3:>3.1f}% {sl:>3.1f}% {str(sp):<14} {be:>3.1f}% | "
          f"{ev:>+8.3f}% {wr:>5.1f}% ${f:>9,.0f} {dd*100:>4.0f}%")

# Best per SL
print("\n" + "="*110)
print("BEST CONFIG PER SL-WIDTH")
print("="*110)
for sl in SLS:
    best = max((r for r in results if r[4]==sl), key=lambda x:x[0])
    ev,tp1,tp2,tp3,_,sp,be,wr,f,dd = best
    print(f"SL={sl}%  TP1={tp1}/TP2={tp2}/TP3={tp3} {sp} BE+{be}%  →  EV {ev:+.3f}% WR {wr:.0f}% ${f:,.0f} DD {dd*100:.0f}%")

# Best per WR-tier
print("\nBEST DD-ADJUSTED (sorted by EV/MaxDD ratio):")
for r in sorted(results[:50], key=lambda x: -x[0]/max(x[9]*100,1))[:10]:
    ev,tp1,tp2,tp3,sl,sp,be,wr,f,dd = r
    print(f"  TP={tp1}/{tp2}/{tp3} SL={sl}% {sp} BE+{be}% | "
          f"EV {ev:+.3f}% / DD {dd*100:.0f}% = {ev/max(dd*100,1):.3f}, ${f:,.0f}")
