"""smart_sl_backtest.py — fetch 1m klines for every signal's window, then
replay each signal with multiple "smart SL" placements (recent high +
buffer, capped at MAX_SL_PCT) and compare against the fixed-2% baseline.

For SHORT signals only (provider is SHORT-only):
  smart_sl = max(highs in last LOOKBACK_MIN minutes before entry) * (1 + BUFFER_PCT/100)
  smart_sl = min(smart_sl, trigger * (1 + MAX_SL_PCT/100))
  smart_sl = max(smart_sl, trigger * (1 + MIN_SL_PCT/100))   # floor

Then walks 1m klines POST entry to determine outcome (TP1/2/3 hit, SL hit,
BE-stop). Replays full TP_SPLITS strategy with fees + compounding.
"""
import json, gzip, os, sys, time
from io import BytesIO
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

BINANCE_VISION_BASE = "https://data.binance.vision/data/futures/um/daily/klines"
KLINE_DIR = "/tmp/klines"
os.makedirs(KLINE_DIR, exist_ok=True)


def url_for(symbol, date):
    return f"{BINANCE_VISION_BASE}/{symbol}/1m/{symbol}-1m-{date}.zip"


def cache_path(symbol, date):
    return os.path.join(KLINE_DIR, f"{symbol}_{date}.jsonl.gz")


def fetch_klines(symbol, date, retry=2):
    """Download 1 day of 1m klines for `symbol`. Returns list of
    [open_time_ms, open, high, low, close, volume]. Caches to disk."""
    cp = cache_path(symbol, date)
    if os.path.exists(cp):
        try:
            with gzip.open(cp, 'rt') as f:
                return json.load(f)
        except Exception:
            pass

    url = url_for(symbol, date)
    last_err = None
    for attempt in range(retry + 1):
        try:
            req = Request(url, headers={"User-Agent": "smart-sl-backtest/1.0"})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
            with ZipFile(BytesIO(data)) as zf:
                name = zf.namelist()[0]
                csv_bytes = zf.read(name).decode('utf-8')
            klines = []
            for line in csv_bytes.strip().split('\n'):
                # Skip header row if present (Binance added headers in 2024+)
                if line.startswith('open_time'):
                    continue
                parts = line.split(',')
                klines.append([
                    int(float(parts[0])),  # open_time
                    float(parts[1]),       # open
                    float(parts[2]),       # high
                    float(parts[3]),       # low
                    float(parts[4]),       # close
                    float(parts[5]),       # volume
                ])
            with gzip.open(cp, 'wt') as f:
                json.dump(klines, f, separators=(',', ':'))
            return klines
        except HTTPError as e:
            if e.code == 404:
                return None  # no data for this symbol/date
            last_err = e
        except (URLError, Exception) as e:
            last_err = e
        time.sleep(1 + attempt)
    return None


def fetch_all(pairs, max_workers=12):
    """Fetch all (symbol, date) pairs in parallel. Returns dict (sym,date)->klines."""
    out = {}
    todo = [(s, d) for (s, d) in pairs if not os.path.exists(cache_path(s, d))]
    print(f"  → {len(pairs)} pairs, {len(pairs) - len(todo)} cached, {len(todo)} to fetch", file=sys.stderr)

    if todo:
        done = 0
        last_log = time.time()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch_klines, s, d): (s, d) for (s, d) in todo}
            for fut in as_completed(futures):
                s, d = futures[fut]
                done += 1
                if time.time() - last_log > 5:
                    print(f"  → fetched {done}/{len(todo)}", file=sys.stderr)
                    last_log = time.time()

    # Load all from cache
    for s, d in pairs:
        try:
            with gzip.open(cache_path(s, d), 'rt') as f:
                out[(s, d)] = json.load(f)
        except Exception:
            out[(s, d)] = None
    return out


def date_str(ts):
    return time.strftime('%Y-%m-%d', time.gmtime(ts))


def get_klines_window(klines_by_pair, symbol, t_from_ms, t_to_ms):
    """Return klines (open_time_ms, o, h, l, c, v) intersecting [t_from_ms, t_to_ms].
    Pulls from klines for date(t_from) and date(t_to) since the window may straddle midnight."""
    result = []
    seen_dates = set()
    for ts in (t_from_ms // 1000, t_to_ms // 1000):
        d = date_str(ts)
        if d in seen_dates:
            continue
        seen_dates.add(d)
        kl = klines_by_pair.get((symbol, d))
        if not kl:
            continue
        for k in kl:
            ot = k[0]
            if t_from_ms <= ot <= t_to_ms:
                result.append(k)
    result.sort(key=lambda k: k[0])
    return result


# ============================================================================
# Trade simulation
# ============================================================================

LEV = 20.0
TP_PCTS = [0.8, 1.6, 4.0]
FEE_M = 0.02 * LEV
FEE_T = 0.055 * LEV
SLIP = 0.05 * LEV
SAME_CANDLE_PESSIMISTIC = True  # if both SL and TP touched in same candle: SL wins


def simulate(sig, klines_by_pair, sl_pct, splits, be_buf, entry_window_min=180):
    """Simulate one signal with given SL%. Returns dict with outcome + PnL.

    sig fields: sym, base, trigger, tp1, tp2, tp3, post (Unix sec)
    """
    sym = sig['sym']
    trigger = float(sig['trigger'])
    tp1, tp2, tp3 = float(sig['tp1']), float(sig['tp2']), float(sig['tp3'])
    post_ms = int(sig['post']) * 1000

    # Entry-fill window: [post, post + entry_window_min minutes]
    entry_to_ms = post_ms + entry_window_min * 60_000
    entry_klines = get_klines_window(klines_by_pair, sym, post_ms, entry_to_ms)
    if not entry_klines:
        return {'state': 'no_data', 'pnl': 0.0}

    # SHORT entry: limit @ trigger fills when high >= trigger
    entry_idx = None
    for i, k in enumerate(entry_klines):
        if k[2] >= trigger:  # high
            entry_idx = i
            break
    if entry_idx is None:
        return {'state': 'never_filled', 'pnl': 0.0}

    entry_t_ms = entry_klines[entry_idx][0]

    # SL: ABOVE trigger for SHORT
    sl_price = trigger * (1 + sl_pct / 100.0)

    # Walk forward from entry candle
    tp1_hit = False
    tp2_hit = False
    tp3_hit = False
    sl_hit = False
    be_stop_hit = False
    sl_active = sl_price
    state = 'open'

    # Use a window of 4h after entry (typical signal lifetime)
    walk_to_ms = entry_t_ms + 4 * 60 * 60_000
    walk_klines = get_klines_window(klines_by_pair, sym, entry_t_ms, walk_to_ms)

    for k in walk_klines:
        _ot, _o, h, l, _c, _v = k
        # SHORT: TP if low <= TP (price dropped); SL if high >= SL
        # Same-candle ambiguity: pessimistic = SL wins
        sl_touched = h >= sl_active
        tp1_touched = l <= tp1
        tp2_touched = l <= tp2
        tp3_touched = l <= tp3

        if not tp1_hit:
            if sl_touched and tp1_touched and SAME_CANDLE_PESSIMISTIC:
                sl_hit = True; state = 'loss'
                break
            if sl_touched:
                sl_hit = True; state = 'loss'
                break
            if tp1_touched:
                tp1_hit = True
                # BE-stop activation
                sl_active = trigger * (1 - be_buf / 100.0)  # BE+buffer for SHORT means BELOW entry
                # Wait — for SHORT, BE+buffer profit means SL moved DOWN
                # entry=trigger, BE+1% means we want to lock in 1% profit
                # → SL_new = trigger * (1 - 0.01) = below entry
                # If price retraces UP past trigger * (1 - 0.01), we exit at +1% profit.
                if tp2_touched:
                    tp2_hit = True
                if tp3_touched:
                    tp3_hit = True; state = 'tp1_tp2_tp3'; break
                # In same candle, check if BE-stop fires AFTER TP1
                # Conservative: if h after TP1 hit, we don't know order
                # → assume BE doesn't fire in same candle as TP1
        else:
            # Post-TP1 phase
            if not tp2_hit and tp2_touched:
                tp2_hit = True
            if not tp3_hit and tp3_touched:
                tp3_hit = True; state = 'tp1_tp2_tp3' if tp2_hit else 'tp1_tp3'; break
            # BE-stop: need price to RISE above sl_active (BE+buf below entry)
            if h >= sl_active:
                be_stop_hit = True
                state = 'tp1_tp2_then_be' if tp2_hit else 'tp1_then_be'
                break

    # Determine final state if loop ended without break
    if state == 'open':
        if tp1_hit and tp2_hit:
            state = 'tp1_tp2_only'
        elif tp1_hit:
            state = 'tp1_only'
        else:
            state = 'no_event'

    # Compute PnL using TP_SPLITS
    s1, s2, s3 = splits
    if state in ('no_data', 'never_filled', 'no_event'):
        return {'state': state, 'pnl': 0.0}

    fees = FEE_M  # entry maker
    if state == 'loss':
        return {'state': state, 'pnl': -sl_pct * LEV - (FEE_T + SLIP) - fees}

    pnl = TP_PCTS[0] * (s1 / 100) * LEV
    fees += FEE_M * (s1 / 100)
    pos = 100 - s1

    if 'tp2' in state:
        pnl += TP_PCTS[1] * (s2 / 100) * LEV
        fees += FEE_M * (s2 / 100)
        pos -= s2
    if 'tp3' in state:
        pnl += TP_PCTS[2] * (s3 / 100) * LEV
        fees += FEE_M * (s3 / 100)
        pos -= s3

    if 'be' in state and pos > 0.001:
        pnl += be_buf * (pos / 100) * LEV
        fees += (FEE_T + SLIP) * (pos / 100)
        pos = 0
    elif state == 'tp1_only' and pos > 0.001:
        # Position still open at end of window — assume close at entry (BE)
        pass

    return {'state': state, 'pnl': pnl - fees}


def compute_smart_sl(sig, klines_by_pair, lookback_min, buffer_pct,
                     min_sl_pct, max_sl_pct):
    """Compute smart SL = max(high in lookback) * (1 + buffer/100), clamped.
    Returns SL distance from trigger as percentage."""
    sym = sig['sym']
    trigger = float(sig['trigger'])
    post_ms = int(sig['post']) * 1000
    lookback_ms = lookback_min * 60_000

    # Lookback window: [post - lookback_min, post]
    pre_klines = get_klines_window(klines_by_pair, sym, post_ms - lookback_ms, post_ms)
    if not pre_klines:
        return None

    high_window = max(k[2] for k in pre_klines)  # max of all candle highs
    # SL = high * (1 + buffer)
    sl_price = high_window * (1 + buffer_pct / 100.0)
    # SL distance from trigger as %
    sl_pct = (sl_price - trigger) / trigger * 100.0
    # Clamp
    sl_pct = max(min_sl_pct, min(sl_pct, max_sl_pct))
    return sl_pct


def compound(returns, start=1000.0, margin_pct=10.0):
    w = start; peak = start; ddmax = 0.0
    for r in returns:
        w += w * margin_pct / 100 * r / 100
        peak = max(peak, w)
        dd = (peak - w) / peak
        if dd > ddmax: ddmax = dd
    return w, ddmax


def main():
    print("Loading signals...", file=sys.stderr)
    with gzip.open('data/binance_clean.json.gz', 'rt') as f:
        signals = json.load(f)
    # Only signals with binance data
    signals = [s for s in signals if s['outcomes_by_sl']['2.0'] != 'no_data']
    print(f"  → {len(signals)} signals with data", file=sys.stderr)

    print("Computing required (symbol, date) pairs...", file=sys.stderr)
    pairs = set()
    for s in signals:
        post = s['post']
        # Need lookback day + post day + walk day
        for offset in (-86400, 0, 14400):  # 1 day before, day of, +4h
            d = date_str(post + offset)
            pairs.add((s['sym'], d))
    print(f"  → {len(pairs)} unique pairs", file=sys.stderr)

    print("Fetching klines (parallel)...", file=sys.stderr)
    klines_by_pair = fetch_all(pairs, max_workers=16)
    valid = sum(1 for v in klines_by_pair.values() if v)
    print(f"  → {valid}/{len(pairs)} pairs have data", file=sys.stderr)

    # Test matrix: lookback windows × buffers × max_sl
    LOOKBACKS = [15, 30, 60, 120, 240]   # minutes
    BUFFERS = [0.1, 0.3, 0.5]            # %
    MAX_SLS = [3.0, 4.0, 5.0]            # %
    MIN_SL = 0.5                          # floor: never less than 0.5%
    SPLITS = [0, 100, 0]
    BE_BUF = 1.0                          # BE+1% (matches deployed config)

    print("\n" + "=" * 90)
    print("BASELINE: fixed SL=2.0% with TP_SPLITS=[0,100,0] + BE+1%")
    print("=" * 90)
    baseline_results = []
    for s in signals:
        r = simulate(s, klines_by_pair, sl_pct=2.0, splits=SPLITS, be_buf=BE_BUF)
        baseline_results.append(r)
    bl_pnls = [r['pnl'] for r in baseline_results]
    bl_ev = sum(bl_pnls) / len(bl_pnls)
    bl_wr = sum(1 for r in baseline_results if r['pnl'] > 0) / len(baseline_results) * 100
    bl_final, bl_dd = compound(bl_pnls)
    print(f"  EV/sig: {bl_ev:+.3f}%  WR: {bl_wr:.1f}%  $1k → ${bl_final:,.0f}  MaxDD: {bl_dd*100:.0f}%")

    print("\n" + "=" * 90)
    print(f"SMART SL: max(high in lookback) * (1 + buffer/100), clamped [MIN, MAX]")
    print(f"TP_SPLITS={SPLITS}, BE+{BE_BUF}%, MIN_SL={MIN_SL}%")
    print("=" * 90)
    print(f"{'lookback':>10} {'buffer':>7} {'max_sl':>7} | {'EV/sig':>9} {'WR':>6} {'$1k →':>11} {'MaxDD':>7}  {'avg_smart_sl':>12}")
    print('-' * 90)

    best = []
    for lb in LOOKBACKS:
        for bf in BUFFERS:
            for max_sl in MAX_SLS:
                results = []
                smart_sls = []
                for s in signals:
                    smart = compute_smart_sl(s, klines_by_pair, lb, bf, MIN_SL, max_sl)
                    if smart is None:
                        results.append({'state': 'no_data', 'pnl': 0.0})
                        continue
                    smart_sls.append(smart)
                    r = simulate(s, klines_by_pair, sl_pct=smart, splits=SPLITS, be_buf=BE_BUF)
                    results.append(r)
                pnls = [r['pnl'] for r in results]
                ev = sum(pnls) / len(pnls)
                wr = sum(1 for r in results if r['pnl'] > 0) / len(results) * 100
                final, dd = compound(pnls)
                avg_sl = sum(smart_sls) / max(len(smart_sls), 1)
                print(f"{lb:>9}m {bf:>6.1f}% {max_sl:>6.1f}% | {ev:>+8.3f}% {wr:>5.1f}% ${final:>9,.0f} {dd*100:>6.0f}%  {avg_sl:>11.2f}%")
                best.append((ev, lb, bf, max_sl, final, dd, avg_sl))

    best.sort(key=lambda x: -x[0])
    print(f"\nTOP 5 by EV/sig (vs baseline {bl_ev:+.3f}%):")
    for ev, lb, bf, max_sl, final, dd, avg_sl in best[:5]:
        delta = ev - bl_ev
        print(f"  lb={lb}m buf={bf}% cap={max_sl}%  →  EV={ev:+.3f}% (Δ={delta:+.3f}pp)  ${final:,.0f}  DD={dd*100:.0f}%  avg_SL={avg_sl:.2f}%")


if __name__ == '__main__':
    main()
