"""
backtest_replay.py — replay multiple TP_SPLITS strategies against the
event-trace data dumped by the candle-fetch step.

Reads /home/user/SYS_AO_Bybit/data/backtest_trades.json.gz and for each
signal walks the trade events to extract: pre-flight skip, entry fill
time/price, TP1/2/3 cross times, SL hit time, BE-stop time. Then
replays the strategy parameter sweep on the SAME outcome traces.
"""
import json, gzip, sys, os
from collections import defaultdict, Counter

LEVERAGE = 20.0
SL_PCT = 1.0
TP_PCTS = [0.8, 1.6, 4.0]
FEE_MAKER = 0.02 * LEVERAGE
FEE_TAKER = 0.055 * LEVERAGE
SLIP = 0.05 * LEVERAGE


def compute_outcome(sig):
    """Walk the signal's filtered trade list and emit:
       result = {pre_flight_skip, entry_fill_t, entry_fill_p,
                 tp1_t, tp2_t, tp3_t, sl_t, be_stop_t, terminal}
       For SHORT plain LIMIT at trigger.
    """
    trades = sig['trades']  # [[ts, price], ...] sorted ascending
    side = sig['side']
    trigger = sig['trigger']
    tp1, tp2, tp3 = sig['tp1'], sig['tp2'], sig['tp3']

    out = {
        'msg_id': sig['msg_id'], 'sym': sig['sym'], 'base': sig['base'],
        'side': side, 'trigger': trigger, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
        'post': sig['post'],
        'pre_flight_skip': False,
        'entry_fill_t': None, 'entry_fill_p': None,
        'tp1_t': None, 'tp2_t': None, 'tp3_t': None,
        'sl_t': None, 'be_stop_t': None,
        'no_data': False,
        'tps_hit_provider': sig['tps_hit_provider'],
        'status_provider': sig['status_provider'],
    }
    if not trades:
        out['no_data'] = True
        return out

    order_live = sig['order_live']
    if side != 'SHORT':
        out['no_data'] = True  # only SHORT in this provider
        return out

    # Pre-flight: first trade at-or-after order_live - small tol
    first_after = next((t for t in trades if t[0] >= order_live - 0.5), None)
    if first_after and first_after[1] <= tp1:
        out['pre_flight_skip'] = True
        return out

    # Entry fill: first trade with price >= trigger after order_live
    for t, p in trades:
        if t < order_live: continue
        if p >= trigger:
            out['entry_fill_t'] = t
            out['entry_fill_p'] = trigger  # passive limit fills at limit
            break

    if out['entry_fill_t'] is None:
        return out  # never filled

    entry_t = out['entry_fill_t']
    be_sl = out['entry_fill_p'] * 1.001  # BE+0.1%
    sl_initial = trigger * 1.01
    tp1_seen = False
    sl_active = sl_initial

    for t, p in trades:
        if t <= entry_t: continue
        # SHORT: TP if price <= TP_n; SL if price >= SL_active
        # Same trade: prefer TP detection (best case)
        if not tp1_seen:
            if p <= tp1:
                out['tp1_t'] = t
                tp1_seen = True
                sl_active = be_sl
                if p <= tp2: out['tp2_t'] = t
                if p <= tp3:
                    out['tp3_t'] = t
                    return out
                continue
            if p >= sl_active:
                out['sl_t'] = t
                return out
        else:
            if out['tp2_t'] is None and p <= tp2:
                out['tp2_t'] = t
            if out['tp3_t'] is None and p <= tp3:
                out['tp3_t'] = t
                return out
            if p >= sl_active:
                out['be_stop_t'] = t
                return out
    return out


def replay_strategy(outcome, splits, sl_policy='be_at_tp1'):
    """Compute net P&L (% margin) for one outcome under a strategy."""
    if outcome['pre_flight_skip']:
        return 0.0  # skipped — no trade
    if outcome['entry_fill_t'] is None:
        return 0.0  # never filled — no trade
    s1, s2, s3 = splits
    rest1 = 100 - s1
    rest12 = 100 - s1 - s2
    rest123 = 100 - s1 - s2 - s3

    fees = FEE_MAKER  # entry maker
    pnl = 0.0
    pos_open = 100.0  # %

    if outcome['sl_t'] is not None and outcome['tp1_t'] is None:
        # Loss before TP1
        pnl = -SL_PCT * LEVERAGE * (pos_open / 100)
        fees += FEE_TAKER + SLIP
        return pnl - fees

    if outcome['tp1_t'] is None:
        # Filled but never closed → assume timeout at trigger (BE)
        # Conservative: small loss
        return -fees

    # TP1 hit
    pnl += TP_PCTS[0] * (s1 / 100) * LEVERAGE
    fees += FEE_MAKER * (s1 / 100)
    pos_open -= s1

    if outcome['tp2_t'] is not None:
        pnl += TP_PCTS[1] * (s2 / 100) * LEVERAGE
        fees += FEE_MAKER * (s2 / 100)
        pos_open -= s2
    if outcome['tp3_t'] is not None:
        pnl += TP_PCTS[2] * (s3 / 100) * LEVERAGE
        fees += FEE_MAKER * (s3 / 100)
        pos_open -= s3

    if outcome['be_stop_t'] is not None:
        # SL @ BE+0.1% triggered for remaining
        pnl += 0.1 * (pos_open / 100) * LEVERAGE
        fees += (FEE_TAKER + SLIP) * (pos_open / 100)
        pos_open = 0.0

    if pos_open > 0.001:
        # Position still open at end of window — assume close at last seen price
        # For simplicity: P&L of the rest = price-of-tp3-or-last vs entry. Use tp3 if hit, else assume be.
        # Conservative: 0% on rest.
        pass

    return pnl - fees


def main():
    src = 'data/backtest_trades.json.gz'
    if not os.path.exists(src):
        print(f'ERROR: {src} not found. Run candle-fetch first.', file=sys.stderr)
        sys.exit(1)

    with gzip.open(src, 'rt', encoding='utf-8') as f:
        sigs = json.load(f)
    print(f'Loaded {len(sigs)} signals from {src}', file=sys.stderr)

    outcomes = []
    for s in sigs:
        outcomes.append(compute_outcome(s))

    n = len(outcomes)
    n_filled    = sum(1 for o in outcomes if o['entry_fill_t'] is not None)
    n_preflight = sum(1 for o in outcomes if o['pre_flight_skip'])
    n_no_data   = sum(1 for o in outcomes if o.get('no_data'))
    n_unfilled  = sum(1 for o in outcomes if not o['pre_flight_skip'] and o['entry_fill_t'] is None and not o.get('no_data'))
    n_tp1 = sum(1 for o in outcomes if o.get('tp1_t'))
    n_tp2 = sum(1 for o in outcomes if o.get('tp2_t'))
    n_tp3 = sum(1 for o in outcomes if o.get('tp3_t'))
    n_sl  = sum(1 for o in outcomes if o.get('sl_t'))
    n_be  = sum(1 for o in outcomes if o.get('be_stop_t'))

    print('\n' + '='*72)
    print('CANDLE-LEVEL BACKTEST RESULTS — sec-precise events')
    print('='*72)
    print(f'Total signals:        {n}')
    print(f'No data (delisted):   {n_no_data}')
    print(f'Pre-flight skipped:   {n_preflight}  ({n_preflight*100/n:.1f}%)  ← TP1 already past at order-live')
    print(f'Filled:               {n_filled}  ({n_filled*100/n:.1f}%)')
    print(f'Never filled (timeout):{n_unfilled}  ({n_unfilled*100/n:.1f}%)')
    print()
    print(f'Of filled trades ({n_filled}):')
    if n_filled > 0:
        print(f'  TP1 hit: {n_tp1}  ({n_tp1*100/n_filled:.1f}%)')
        print(f'  TP2 hit: {n_tp2}  ({n_tp2*100/n_filled:.1f}%)')
        print(f'  TP3 hit: {n_tp3}  ({n_tp3*100/n_filled:.1f}%)')
        print(f'  SL hit (pre-TP1):   {n_sl}  ({n_sl*100/n_filled:.1f}%)')
        print(f'  BE-stop (post-TP1): {n_be}  ({n_be*100/n_filled:.1f}%)')

    # Strategy ranking
    print()
    print('='*72)
    print('STRATEGY RANKING (avg % margin per attempted trade)')
    print('Avg over ALL signals — pre-flight skips and never-fills count as 0%')
    print('='*72)
    SPLITS = [[5,25,70],[10,30,60],[15,30,55],[20,30,50],[25,35,40],[33,33,34],
              [50,50,0],[100,0,0],[0,100,0],[0,50,50],[40,30,30],[10,20,70]]
    strategies = []
    for splits in SPLITS:
        pnls = [replay_strategy(o, splits) for o in outcomes]
        avg = sum(pnls) / len(pnls)
        # Also avg over only filled (more useful for comparison)
        filled_pnls = [pnl for o, pnl in zip(outcomes, pnls) if o['entry_fill_t'] is not None]
        avg_filled = sum(filled_pnls) / len(filled_pnls) if filled_pnls else 0
        strategies.append((splits, avg, avg_filled, sum(pnls)))
    strategies.sort(key=lambda x: -x[1])
    print(f'{"splits":<14} {"avg_all":>10} {"avg_filled":>12} {"sum%":>10}')
    for splits, avg_all, avg_filled, total in strategies:
        print(f'{str(splits):<14} {avg_all:>+9.2f}%  {avg_filled:>+11.2f}%  {total:>+9.0f}%')

    # Compare provider's TP-rates vs ours
    print()
    print('='*72)
    print('COMPARISON: Provider Discord status vs our candle simulation')
    print('='*72)
    p_tp1 = sum(1 for o in outcomes if o['tps_hit_provider'].get('1'))
    p_tp2 = sum(1 for o in outcomes if o['tps_hit_provider'].get('2'))
    p_tp3 = sum(1 for o in outcomes if o['tps_hit_provider'].get('3'))
    print(f'                 Provider says   |   Bot would do (after fill)')
    print(f'TP1:             {p_tp1*100/n:>5.1f}%          |   {n_tp1*100/n:>5.1f}%   (filled+TP1: {n_tp1*100/max(n_filled,1):>5.1f}%)')
    print(f'TP2:             {p_tp2*100/n:>5.1f}%          |   {n_tp2*100/n:>5.1f}%')
    print(f'TP3:             {p_tp3*100/n:>5.1f}%          |   {n_tp3*100/n:>5.1f}%')
    print()
    diff = (n_tp1 - p_tp1)
    print(f'Δ TP1 (bot − provider): {diff:+d}  ({diff*100/n:+.1f}pp)')
    print(f'   = signals where provider claimed TP1 but bot would not have filled OR not have hit TP1')

    # Save outcome traces for further analysis
    os.makedirs('data', exist_ok=True)
    with gzip.open('data/backtest_outcomes.json.gz', 'wt', encoding='utf-8') as f:
        json.dump(outcomes, f, separators=(',', ':'), default=str)
    print(f'\nSaved outcomes to data/backtest_outcomes.json.gz')


if __name__ == '__main__':
    main()
