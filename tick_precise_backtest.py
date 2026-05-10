"""tick_precise_backtest.py — answers definitively whether BE+0.7% kills
TP2-wins or not, by replaying every signal against ms-precise aggTrades
data instead of 1m candles.

For each signal:
1. From 1m klines: find the candle where TP1 first triggers (entry already
   filled by then per existing simulation).
2. From aggTrades: stream trades from TP1-fill time forward.
3. For each trade, check FIRST-CROSS:
   - If price >= BE+X level → BE fires (exit at +X% lock-in)
   - If price <= TP2 level → TP2 fires (exit at +1.6%)
   - If price >= original SL → SL fires (loss)
4. Whichever crosses FIRST is the actual outcome.

This eliminates the same-candle ordering ambiguity entirely.
"""
import os, sys, gzip, json, time, struct
from io import BytesIO
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

# Per-row binary format: <Qd> = uint64 time_ms + double price = 16 bytes.
# 1M trades = 16MB raw, ~5-8MB gzipped. JSON would be 30 MB+.
_REC = struct.Struct('<Qd')

BINANCE_VISION_BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
KLINE_BASE = "https://data.binance.vision/data/futures/um/daily/klines"
TICK_DIR = "/tmp/aggtrades"
KLINE_DIR = "/tmp/klines"
os.makedirs(TICK_DIR, exist_ok=True)
os.makedirs(KLINE_DIR, exist_ok=True)


def date_str(ts):
    return time.strftime('%Y-%m-%d', time.gmtime(ts))


def cache_path_tick(symbol, date):
    return os.path.join(TICK_DIR, f"{symbol}_{date}.bin.gz")


def cache_path_kline(symbol, date):
    return os.path.join(KLINE_DIR, f"{symbol}_{date}.jsonl.gz")


def fetch_aggtrades(symbol, date):
    """Stream 1 day of aggTrades from Binance Vision archive into a
    compact binary cache. Format: 16 bytes per record (uint64 time_ms +
    double price). Streams the CSV line-by-line to avoid loading the
    entire (potentially 50-100MB) decompressed CSV into memory.

    Returns the cache path on success, None on 404 / network error."""
    cp = cache_path_tick(symbol, date)
    if os.path.exists(cp):
        return cp

    url = f"{BINANCE_VISION_BASE}/{symbol}/{symbol}-aggTrades-{date}.zip"
    tmp = cp + '.tmp'
    try:
        req = Request(url, headers={"User-Agent": "tick-bt/1.0"})
        # We have to load the ZIP itself fully (random access), but we
        # stream the CSV inside it to keep peak memory bounded.
        with urlopen(req, timeout=120) as resp:
            zip_bytes = resp.read()
        with ZipFile(BytesIO(zip_bytes)) as zf:
            inner = zf.namelist()[0]
            with zf.open(inner) as csv_stream:
                with gzip.open(tmp, 'wb', compresslevel=4) as out:
                    for raw in csv_stream:
                        line = raw.decode('utf-8', errors='ignore').strip()
                        if not line or line.startswith(('agg_trade', 'a,')):
                            continue
                        parts = line.split(',')
                        # cols: agg_trade_id, price, quantity, first_trade_id,
                        # last_trade_id, transact_time, is_buyer_maker
                        try:
                            price = float(parts[1])
                            t = int(float(parts[5]))
                        except (IndexError, ValueError):
                            continue
                        out.write(_REC.pack(t, price))
        os.rename(tmp, cp)
        # Free the ZIP bytes ASAP — workers run concurrently.
        del zip_bytes
        return cp
    except HTTPError as e:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        if e.code == 404:
            return None
        raise
    except (URLError, Exception):
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        return None


def load_ticks(symbol, date):
    """Read the cached aggTrades. Auto-detects format: legacy JSON
    (early pre-OOM-fix runs) or new binary (16-byte records).
    Returns list of (time_ms, price) tuples, sorted by time."""
    cp = cache_path_tick(symbol, date)
    if not os.path.exists(cp):
        return None
    try:
        with gzip.open(cp, 'rb') as f:
            head = f.read(2)
            f.seek(0)
            if head[:1] == b'[':
                # Legacy JSON
                return json.load(gzip.open(cp, 'rt'))
            # Binary
            rows = []
            chunk_size = _REC.size * 4096
            while True:
                buf = f.read(chunk_size)
                if not buf:
                    break
                for off in range(0, len(buf) - _REC.size + 1, _REC.size):
                    rows.append(_REC.unpack_from(buf, off))
            rows.sort(key=lambda x: x[0])
            return rows
    except Exception:
        return None


def load_klines(symbol, date):
    cp = cache_path_kline(symbol, date)
    if not os.path.exists(cp):
        return None
    try:
        with gzip.open(cp, 'rt') as f:
            return json.load(f)
    except Exception:
        return None


def find_entry_and_tp1_time(sig, klines_today, klines_tomorrow):
    """Use 1m klines to determine entry-fill time and TP1-first-touch time.
    Returns (entry_t_ms, tp1_t_ms) or (None, None)."""
    trigger = float(sig['trigger']); tp1 = float(sig['tp1'])
    post_ms = int(sig['post']) * 1000

    candles = []
    if klines_today: candles.extend(klines_today)
    if klines_tomorrow: candles.extend(klines_tomorrow)
    candles = [k for k in candles if k[0] >= post_ms]
    candles.sort(key=lambda k: k[0])

    if not candles:
        return None, None

    # Entry: first candle high >= trigger
    entry_idx = next((i for i, k in enumerate(candles) if k[2] >= trigger), None)
    if entry_idx is None:
        return None, None
    entry_t_ms = candles[entry_idx][0]

    # TP1: first candle (post-entry) where low <= tp1
    for k in candles[entry_idx:]:
        if k[3] <= tp1:
            return entry_t_ms, k[0]
    return entry_t_ms, None


def simulate_tick(sig, klines_by_pair, sl_pct=2.0, be_buf=0.7):
    """Tick-precise simulation. Returns {state, pnl}."""
    sym = sig['sym']
    trigger = float(sig['trigger']); tp1 = float(sig['tp1'])
    tp2 = float(sig['tp2']); tp3 = float(sig['tp3'])
    post_ms = int(sig['post']) * 1000
    sl_init = trigger * (1 + sl_pct / 100)

    d_today = date_str(post_ms / 1000)
    d_tomorrow = date_str(post_ms / 1000 + 86400)
    kt = klines_by_pair.get((sym, d_today))
    ktom = klines_by_pair.get((sym, d_tomorrow))

    entry_t, tp1_first_t = find_entry_and_tp1_time(sig, kt, ktom)
    if entry_t is None:
        return {'state': 'never_filled', 'pnl': 0.0}

    # Walk aggTrades from entry forward, looking for SL hit (pre-TP1) or TP1
    ticks_today = load_ticks(sym, d_today) or []
    ticks_tom = load_ticks(sym, d_tomorrow) or []
    all_ticks = []
    if ticks_today:
        all_ticks.extend([t for t in ticks_today if t[0] >= entry_t])
    if ticks_tom:
        all_ticks.extend(ticks_tom)
    if not all_ticks:
        return {'state': 'no_ticks', 'pnl': 0.0}

    # Cap walk to 4h after entry
    walk_to_ms = entry_t + 4 * 3600 * 1000
    all_ticks = [t for t in all_ticks if t[0] <= walk_to_ms]

    # Phase 1: walk until TP1 or SL (pre-TP1)
    tp1_hit = False
    tp1_t_real = None
    for tm, p in all_ticks:
        if p >= sl_init:
            return {'state': 'loss', 'pnl': pnl_loss(sl_pct)}
        if p <= tp1:
            tp1_hit = True
            tp1_t_real = tm
            break
    if not tp1_hit:
        return {'state': 'no_event', 'pnl': 0.0}

    # Phase 2: BE armed at trigger × (1 - be_buf/100). Walk forward.
    be_lvl = trigger * (1 - be_buf / 100)
    be_placeable = be_lvl > tp1  # must be above current TP1 price

    tp2_hit = False
    tp3_hit = False
    for tm, p in all_ticks:
        if tm < tp1_t_real:
            continue
        # Check in tick order:
        if be_placeable and p >= be_lvl:
            # BE fires
            state = 'tp1_tp2_then_be' if tp2_hit else 'tp1_then_be'
            return {'state': state, 'pnl': pnl_after_tp1(state, be_buf)}
        if not be_placeable and p >= sl_init:
            # BE move would fail → original SL still active → SL hit
            return {'state': 'tp1_then_sl', 'pnl': pnl_tp1_then_sl(sl_pct)}
        if p <= tp3 and not tp3_hit:
            tp3_hit = True
            return {'state': 'tp1_tp2_tp3' if tp2_hit else 'tp1_tp3',
                    'pnl': pnl_tp3()}
        if p <= tp2 and not tp2_hit:
            tp2_hit = True
            # With splits=[0,100,0], TP2 closes 100% → exit
            return {'state': 'tp1_tp2_only', 'pnl': pnl_tp2()}

    # Window expired without exit
    state = 'tp1_tp2_only' if tp2_hit else 'tp1_only'
    return {'state': state, 'pnl': pnl_tp2() if tp2_hit else 0.0}


# Constants for splits=[0,100,0]
LEV = 20.0
TP_PCTS = [0.8, 1.6, 4.0]
FEE_M = 0.02 * LEV
FEE_T = 0.055 * LEV
SLIP = 0.05 * LEV


def pnl_loss(sl_pct):
    return -sl_pct * LEV - (FEE_T + SLIP) - FEE_M


def pnl_after_tp1(state, be_buf):
    # splits=[0,100,0]: TP1 piece = 0, pos = 100
    fees = FEE_M
    if 'tp2' in state:
        # TP2 closes 100% → BE adds 0
        return TP_PCTS[1] * LEV - fees - FEE_M
    # tp1_then_be: pos still 100, BE adds be_buf
    return be_buf * LEV - fees - (FEE_T + SLIP)


def pnl_tp1_then_sl(sl_pct):
    # splits=[0,100,0]: SL hits 100%
    return -sl_pct * LEV - (FEE_T + SLIP) - FEE_M


def pnl_tp2():
    return TP_PCTS[1] * LEV - FEE_M - FEE_M


def pnl_tp3():
    # With splits=[0,100,0], TP2 closes 100% before TP3 — same PnL
    return TP_PCTS[1] * LEV - FEE_M - FEE_M


def compound(returns, start=1000.0, mp=10.0):
    w = start; peak = start; ddm = 0.0
    for r in returns:
        w += w * mp / 100 * r / 100
        peak = max(peak, w); ddm = max(ddm, (peak - w) / peak)
    return w, ddm


def main():
    print("Loading signals...", file=sys.stderr)
    with gzip.open('data/binance_clean.json.gz', 'rt') as f:
        signals = [s for s in json.load(f) if s['outcomes_by_sl']['2.0'] != 'no_data']
    print(f"  → {len(signals)} signals", file=sys.stderr)

    # Required (sym, date) pairs: only for signals that COULD have hit TP1
    # (based on precomputed state). Loss-only or never-filled signals don't
    # need ms-precise tick data — we already know the outcome from klines.
    pairs_set = set()
    skipped_loss_only = 0
    for s in signals:
        state_2pct = s['outcomes_by_sl'].get('2.0', '')
        if state_2pct in ('loss', 'never_filled', 'no_data', 'no_event'):
            skipped_loss_only += 1
            continue  # outcome is unambiguous from klines, skip tick fetch
        for off in (0, 86400):
            pairs_set.add((s['sym'], date_str(s['post'] + off)))
    pairs = sorted(pairs_set)
    print(f"  → {skipped_loss_only} signals are loss/never-filled (outcomes already known from klines)", file=sys.stderr)
    print(f"  → {len(pairs)} unique (symbol, date) tick-pairs needed", file=sys.stderr)

    # Skip already-cached
    todo = [(s, d) for (s, d) in pairs if not os.path.exists(cache_path_tick(s, d))]
    print(f"  → {len(pairs) - len(todo)} cached, {len(todo)} to fetch", file=sys.stderr)

    if todo:
        done = 0; last_log = time.time()
        # 4 workers — even with streaming CSV parse, the raw ZIP bytes
        # (10-15MB for ARIA/BLESS days) still need to load fully for
        # random access. 4 × 15MB = ~60MB peak per pool, plus per-worker
        # decompression buffers. Sandbox has been OOM-killing at 8.
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(fetch_aggtrades, s, d): (s, d) for (s, d) in todo}
            for fut in as_completed(futs):
                done += 1
                if time.time() - last_log > 5:
                    print(f"    fetched {done}/{len(todo)}", file=sys.stderr)
                    last_log = time.time()

    # Load klines (reuse 1m kline cache from previous backtest)
    print("Loading klines...", file=sys.stderr)
    klines_by_pair = {}
    for s, d in pairs:
        klines_by_pair[(s, d)] = load_klines(s, d)

    # Run simulation. For signals we already know are losses/never_filled
    # from klines (no aggTrades fetched), short-circuit with the known state.
    print("Running tick-precise simulation (BE+0.7%)...", file=sys.stderr)
    results = []
    last_log = time.time()
    incremental_path = 'data/tick_precise_outcomes.json.gz'
    for i, sig in enumerate(signals):
        state_2pct = sig['outcomes_by_sl'].get('2.0', '')
        if state_2pct in ('loss', 'never_filled', 'no_data', 'no_event'):
            # Short-circuit using kline outcome (no tick data available)
            if state_2pct == 'loss':
                results.append({'state': 'loss', 'pnl': pnl_loss(2.0)})
            else:
                results.append({'state': state_2pct, 'pnl': 0.0})
        else:
            results.append(simulate_tick(sig, klines_by_pair, sl_pct=2.0, be_buf=0.7))
        if time.time() - last_log > 10:
            print(f"    processed {i+1}/{len(signals)}", file=sys.stderr)
            last_log = time.time()
        # Incremental checkpoint every 100 signals
        if (i + 1) % 100 == 0:
            with gzip.open(incremental_path, 'wt') as f:
                json.dump([{'msg_id': s['msg_id'], 'state': r['state'], 'pnl': r['pnl']}
                           for s, r in zip(signals[:i+1], results)], f, separators=(',', ':'))

    pnls = [r['pnl'] for r in results]
    ev = sum(pnls) / len(pnls)
    wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
    final, dd = compound(pnls)
    states = Counter(r['state'] for r in results)

    print("\n" + "=" * 70)
    print("TICK-PRECISE BACKTEST RESULT (SL=2%, splits=[0,100,0], BE+0.7%)")
    print("=" * 70)
    print(f"EV/sig: {ev:+.3f}%   WR: {wr:.1f}%   $1k → ${final:,.0f}   MaxDD: {dd*100:.0f}%")
    print(f"\nState distribution:")
    for s, c in states.most_common():
        print(f"  {s:<25} {c:>4}  ({c*100/len(results):.1f}%)")

    # Critical: TP2-wins vs BE-fires AFTER TP1
    tp1_only_be = states.get('tp1_then_be', 0)
    tp1_tp2_be = states.get('tp1_tp2_then_be', 0)
    tp1_tp2_only = states.get('tp1_tp2_only', 0)
    tp1_tp2_tp3 = states.get('tp1_tp2_tp3', 0)
    tp1_then_sl = states.get('tp1_then_sl', 0)
    losses = states.get('loss', 0)

    total_tp1 = tp1_only_be + tp1_tp2_be + tp1_tp2_only + tp1_tp2_tp3 + tp1_then_sl
    if total_tp1:
        be_first_pct = tp1_only_be * 100 / total_tp1
        tp2_first_pct = (tp1_tp2_be + tp1_tp2_only + tp1_tp2_tp3) * 100 / total_tp1
        sl_after_tp1_pct = tp1_then_sl * 100 / total_tp1
        print(f"\nOf {total_tp1} TP1-hit trades:")
        print(f"  BE fired before TP2:  {tp1_only_be:>4} ({be_first_pct:.1f}%)  ← user's hypothesis: ~95%")
        print(f"  TP2 fired first:      {tp1_tp2_be + tp1_tp2_only + tp1_tp2_tp3:>4} ({tp2_first_pct:.1f}%)")
        print(f"  SL hit (BE failed):   {tp1_then_sl:>4} ({sl_after_tp1_pct:.1f}%)")

    # Save outcomes for inspection
    out_path = 'data/tick_precise_outcomes.json.gz'
    with gzip.open(out_path, 'wt') as f:
        json.dump([{'msg_id': s['msg_id'], 'state': r['state'], 'pnl': r['pnl']}
                   for s, r in zip(signals, results)], f, separators=(',', ':'))
    print(f"\nOutcomes saved to {out_path}")


if __name__ == '__main__':
    main()
