"""signal_reverse_engineer.py — extract market-state features around each
provider signal and find which features differentiate WINNERS from LOSERS.

For each of the 1013 signals we already have entry/TP/SL outcomes. The
question: what was the market state RIGHT BEFORE the signal that the
provider used to generate it?

Features we can compute from 1m klines + aggTrades:
  - Multi-timeframe RSI (1m, 5m, 15m, 1h)
  - Recent volatility (ATR-style)
  - Pre-signal trend (% move in last N min)
  - Volume spike vs avg
  - Distance from recent high/low
  - Wick-to-body ratio on recent candles
  - Funding rate (from Binance funding-rate endpoint, requires fetch)
  - Higher-timeframe direction (1h close vs 4h ago)

Then split into WIN-bucket vs LOSS-bucket using the v2 events. Per
feature: distribution overlap, mean delta, simple AUC-like separator.
The features with the largest separation are the candidates for the
provider's signal rule.
"""
import gzip, json, statistics, sys, os, time, math
from collections import defaultdict
from tick_precise_backtest import load_klines, date_str

with gzip.open('data/binance_clean.json.gz','rt') as f:
    sigs = sorted([s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data'],
                   key=lambda s: int(s['post']))
with gzip.open('data/tick_events_v3.json.gz','rt') as f:
    events = {e['msg_id']: e for e in json.load(f)}

print(f"Loaded {len(sigs)} signals", file=sys.stderr)

def rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains = []; losses = []
    for i in range(1, period + 1):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i-1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0: return 100
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def atr_pct(klines, period=14):
    """ATR as % of last close."""
    if len(klines) < period + 1: return None
    trs = []
    for i in range(1, len(klines)):
        h = klines[i][2]; l = klines[i][3]; pc = klines[i-1][4]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    return atr / klines[-1][4] * 100


def aggregate_to_tf(klines_1m, period_min):
    """Aggregate 1m klines into period_min candles (close-based)."""
    if not klines_1m: return []
    out = []
    bucket = []
    bucket_start = klines_1m[0][0] - (klines_1m[0][0] % (period_min * 60_000))
    for k in klines_1m:
        bucket_end = bucket_start + period_min * 60_000
        if k[0] >= bucket_end:
            if bucket:
                op = bucket[0][1]; cl = bucket[-1][4]
                hi = max(b[2] for b in bucket); lo = min(b[3] for b in bucket)
                vol = sum(b[5] for b in bucket)
                out.append([bucket[0][0], op, hi, lo, cl, vol])
            bucket = [k]
            bucket_start = bucket_end
        else:
            bucket.append(k)
    if bucket:
        op = bucket[0][1]; cl = bucket[-1][4]
        hi = max(b[2] for b in bucket); lo = min(b[3] for b in bucket)
        vol = sum(b[5] for b in bucket)
        out.append([bucket[0][0], op, hi, lo, cl, vol])
    return out


def extract_features(sig):
    sym = sig['sym']; trigger = float(sig['trigger'])
    post_ms = int(sig['post']) * 1000
    feat = {'sym': sym, 'trigger': trigger, 'post_ms': post_ms}

    # Load klines: day-of + day-before (covers lookback)
    klines = []
    for off in (-86400, 0):
        d = date_str((post_ms / 1000) + off)
        kk = load_klines(sym, d)
        if kk: klines.extend(kk)
    klines = sorted(klines, key=lambda k: k[0])
    pre_klines = [k for k in klines if k[0] < post_ms]
    if len(pre_klines) < 60: return None

    # Multi-TF RSI
    closes_1m = [k[4] for k in pre_klines]
    feat['rsi_1m'] = rsi(closes_1m[-30:])
    klines_5m = aggregate_to_tf(pre_klines, 5)
    feat['rsi_5m'] = rsi([k[4] for k in klines_5m[-30:]])
    klines_15m = aggregate_to_tf(pre_klines, 15)
    feat['rsi_15m'] = rsi([k[4] for k in klines_15m[-30:]])
    klines_1h = aggregate_to_tf(pre_klines, 60)
    feat['rsi_1h'] = rsi([k[4] for k in klines_1h[-30:]])

    # ATR (volatility) %
    feat['atr_1m'] = atr_pct(pre_klines[-30:])
    feat['atr_5m'] = atr_pct(klines_5m[-30:])

    # Distance from recent high/low (last 30min, 2h, 8h)
    last_30 = pre_klines[-30:]
    last_120 = pre_klines[-120:]
    last_480 = pre_klines[-480:] if len(pre_klines) >= 480 else pre_klines
    feat['dist_from_high_30m'] = (max(k[2] for k in last_30) - trigger) / trigger * 100
    feat['dist_from_high_2h']  = (max(k[2] for k in last_120) - trigger) / trigger * 100
    feat['dist_from_high_8h']  = (max(k[2] for k in last_480) - trigger) / trigger * 100
    feat['dist_from_low_30m']  = (trigger - min(k[3] for k in last_30)) / trigger * 100
    feat['dist_from_low_2h']   = (trigger - min(k[3] for k in last_120)) / trigger * 100

    # Pre-signal % move
    feat['move_5m']   = (trigger - pre_klines[-5][4]) / pre_klines[-5][4] * 100 if len(pre_klines) >= 5 else None
    feat['move_15m']  = (trigger - pre_klines[-15][4]) / pre_klines[-15][4] * 100 if len(pre_klines) >= 15 else None
    feat['move_60m']  = (trigger - pre_klines[-60][4]) / pre_klines[-60][4] * 100 if len(pre_klines) >= 60 else None
    feat['move_4h']   = (trigger - pre_klines[-240][4]) / pre_klines[-240][4] * 100 if len(pre_klines) >= 240 else None

    # Volume spike: last 5m vol vs avg of last 30m
    if len(pre_klines) >= 30:
        last5_vol = sum(k[5] for k in pre_klines[-5:])
        avg5_30 = sum(k[5] for k in pre_klines[-30:]) / 30 * 5
        feat['vol_spike'] = last5_vol / max(avg5_30, 1e-9)

    return feat


def classify_outcome(sig):
    """Win/Loss classification for the optimal config (SL=0.7, TP1=0.8 trail 0.3)."""
    ev = events.get(sig['msg_id'])
    if not ev or ev.get('state') in ('never_filled', 'no_ticks'):
        return 'unfilled'
    sl_t = (ev.get('sl_hit_t') or {}).get(str(0.7))
    tp_t = (ev.get('tp_hit_t') or {}).get(str(0.8))
    if sl_t is not None and (tp_t is None or sl_t < tp_t):
        return 'loss'
    if tp_t is not None:
        return 'win'
    return 'unfilled'


def main():
    print("Extracting features for each signal (this takes ~5min)...", file=sys.stderr)
    rows = []
    last_log = time.time()
    for i, sig in enumerate(sigs):
        if time.time() - last_log > 5:
            print(f"  {i}/{len(sigs)} (rows so far: {len(rows)})", file=sys.stderr); last_log = time.time()
        feat = extract_features(sig)
        if feat is None: continue
        outcome = classify_outcome(sig)
        feat['outcome'] = outcome
        rows.append(feat)

    wins = [r for r in rows if r['outcome'] == 'win']
    losses = [r for r in rows if r['outcome'] == 'loss']
    print(f"\nExtracted: {len(rows)} signals — {len(wins)} wins, {len(losses)} losses\n")

    # Per-feature win/loss separation
    feature_names = [
        'rsi_1m', 'rsi_5m', 'rsi_15m', 'rsi_1h',
        'atr_1m', 'atr_5m',
        'dist_from_high_30m', 'dist_from_high_2h', 'dist_from_high_8h',
        'dist_from_low_30m', 'dist_from_low_2h',
        'move_5m', 'move_15m', 'move_60m', 'move_4h',
        'vol_spike',
    ]
    print("=" * 88)
    print(f"{'Feature':<22}  {'WIN avg':>9}  {'LOSS avg':>9}  {'Δ avg':>8}  {'WIN p50':>9}  {'LOSS p50':>9}")
    print('-' * 88)
    sep_table = []
    for f in feature_names:
        w = [r[f] for r in wins if r.get(f) is not None]
        l = [r[f] for r in losses if r.get(f) is not None]
        if len(w) < 20 or len(l) < 20: continue
        wm, lm = statistics.mean(w), statistics.mean(l)
        wp50, lp50 = statistics.median(w), statistics.median(l)
        delta = wm - lm
        sep_table.append((abs(delta), f, wm, lm, delta, wp50, lp50))
    sep_table.sort(reverse=True)
    for absdelta, f, wm, lm, delta, wp50, lp50 in sep_table:
        print(f"{f:<22}  {wm:>+9.3f}  {lm:>+9.3f}  {delta:>+7.3f}  {wp50:>+9.3f}  {lp50:>+9.3f}")

    # Bucket-level analysis for the top 3 separators
    print("\n" + "=" * 88)
    print("BUCKET ANALYSIS: WR per quintile of top-separator features")
    print("=" * 88)
    top_feats = [t[1] for t in sep_table[:5]]
    for f in top_feats:
        vals = [(r[f], r['outcome']) for r in rows if r.get(f) is not None and r['outcome'] in ('win','loss')]
        vals.sort()
        n_per = max(1, len(vals) // 5)
        print(f"\n{f}:")
        for q in range(5):
            chunk = vals[q*n_per:(q+1)*n_per] if q < 4 else vals[q*n_per:]
            if not chunk: continue
            lo = chunk[0][0]; hi = chunk[-1][0]
            wr = sum(1 for _, o in chunk if o == 'win') / len(chunk) * 100
            print(f"  Q{q+1}: [{lo:+.3f} .. {hi:+.3f}]  n={len(chunk):>4}  WR={wr:>5.1f}%")

    # Save features for later ML
    out_path = 'data/signal_features.json.gz'
    with gzip.open(out_path, 'wt') as f:
        json.dump(rows, f, separators=(',', ':'), default=str)
    print(f"\nSaved features to {out_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
