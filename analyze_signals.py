"""
analyze_signals.py — backtest helper for the AO Crusher signal history.

Reads discord_signals from Postgres (populated by EXPORT_HISTORY=1),
computes win-rate, TP-hit-rates, and EV under several strategy
variants. Outputs:

  • Railway logs  (always — readable on mobile via Railway dashboard)
  • Telegram      (when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID set)
  • analysis.json (committed-friendly JSON dump with full numbers)

Trigger via ANALYZE_HISTORY=1 in Railway env (main.py runs this on boot
then exits, just like EXPORT_HISTORY).

Strategies simulated:

  splits        TP_SPLITS allocation         description
  -------       ---------------------------  ------------------------------
  20/30/50      0.8% × 20 / 1.6% × 30 / 4% × 50  current default
  33/33/34      0.8% × 33 / 1.6% × 33 / 4% × 34  evenly distributed
  100/0/0       0.8% × 100                       all out at TP1
  0/100/0       1.6% × 100                       all out at TP2
  50/50/0       0.8% × 50 / 1.6% × 50            ignore TP3 entirely

  sl_policy     BE@TP1                       move SL to BE+0.1% on TP1 fill (current)
                BE@TP2                       move SL to BE only on TP2 fill (worse for "TP1 only" outcomes)
                TP1@TP2                      move SL to TP1 price on TP2 fill (more locked profit, more retest stops)
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import db_export

# Per-trade gross % return on margin for a given exit at level i (0..3).
# i=0 → TP1, i=1 → TP2, i=2 → TP3, i=3 → SL hit.
# % values match user's setup: SL=1%, TPs=[0.8,1.6,4.0]; leverage=20.
SL_PCT  = 1.0
TP_PCTS = [0.8, 1.6, 4.0]
LEVERAGE = 20.0

# Bybit fees on margin (%): Maker 0.4%, Taker 1.1% (= rate × leverage).
# Slippage on stop-market exit: 1% margin (modest avg for vola alts).
FEE_MAKER = 0.02 * LEVERAGE
FEE_TAKER = 0.055 * LEVERAGE
SLIPPAGE  = 0.05 * LEVERAGE


def _result_for_outcome(splits: List[float], outcome: str, sl_policy: str) -> float:
    """Compute net % margin return for one trade given:
      splits     [w1,w2,w3]   percent of position closed at TP1, TP2, TP3
      outcome    one of: 'loss', 'tp1_only', 'tp1_tp2', 'tp1_tp2_tp3'
      sl_policy  'be_at_tp1' | 'be_at_tp2' | 'tp1_at_tp2'
    Returns net % return on margin (after fees + slippage)."""
    s1, s2, s3 = splits
    rest = max(0.0, 100.0 - s1 - s2 - s3)  # runner % (only relevant if BE/SL retest after final TP)

    # Entry fees: Limit Maker 100% of position
    fees = FEE_MAKER

    if outcome == "loss":
        # SL hits before TP1 → entire 100% closes via stop-market
        gross = -SL_PCT * LEVERAGE
        fees += FEE_TAKER + SLIPPAGE
        return gross - fees

    if outcome == "tp1_only":
        # TP1 fills, then SL retest closes the rest
        gross = TP_PCTS[0] * (s1 / 100.0) * LEVERAGE
        fees += FEE_MAKER * (s1 / 100.0)  # TP1 limit close
        rest_closed = 100.0 - s1
        # SL price depends on policy
        if sl_policy == "be_at_tp1":
            # SL = entry + 0.1% buffer → minimal profit on rest
            gross += 0.1 * (rest_closed / 100.0) * LEVERAGE
        elif sl_policy == "be_at_tp2":
            # SL was NOT moved (TP2 not hit) → still at -1%
            gross += -SL_PCT * (rest_closed / 100.0) * LEVERAGE
        elif sl_policy == "tp1_at_tp2":
            # SL was NOT moved (TP2 not hit) → still at -1%
            gross += -SL_PCT * (rest_closed / 100.0) * LEVERAGE
        fees += (FEE_TAKER + SLIPPAGE) * (rest_closed / 100.0)
        return gross - fees

    if outcome == "tp1_tp2":
        # Both TP1 and TP2 fill, then SL retest closes the rest
        gross = TP_PCTS[0] * (s1 / 100.0) * LEVERAGE + TP_PCTS[1] * (s2 / 100.0) * LEVERAGE
        fees += FEE_MAKER * ((s1 + s2) / 100.0)
        rest_closed = 100.0 - s1 - s2
        if sl_policy == "be_at_tp1":
            gross += 0.1 * (rest_closed / 100.0) * LEVERAGE
        elif sl_policy == "be_at_tp2":
            gross += 0.1 * (rest_closed / 100.0) * LEVERAGE
        elif sl_policy == "tp1_at_tp2":
            # SL moved to TP1 price (= 0.8% profit)
            gross += TP_PCTS[0] * (rest_closed / 100.0) * LEVERAGE
        fees += (FEE_TAKER + SLIPPAGE) * (rest_closed / 100.0)
        return gross - fees

    if outcome == "tp1_tp2_tp3":
        # All three TPs hit (rest = runner percent — for splits summing 100, 0)
        gross = (TP_PCTS[0] * (s1 / 100.0) + TP_PCTS[1] * (s2 / 100.0) + TP_PCTS[2] * (s3 / 100.0)) * LEVERAGE
        fees += FEE_MAKER * ((s1 + s2 + s3) / 100.0)
        # If splits don't sum to 100, leave remainder un-modeled (= conservative)
        return gross - fees

    return 0.0


def fetch_signal_stats() -> Dict[str, Any]:
    """Pull aggregated counts from discord_signals table."""
    conn = db_export._get_connection()
    if not conn:
        raise RuntimeError("Could not connect to Postgres")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN tp1_hit THEN 1 ELSE 0 END) AS tp1,
                    SUM(CASE WHEN tp2_hit THEN 1 ELSE 0 END) AS tp2,
                    SUM(CASE WHEN tp3_hit THEN 1 ELSE 0 END) AS tp3,
                    SUM(CASE WHEN status = 'win' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN status = 'loss' THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN status = 'breakeven' THEN 1 ELSE 0 END) AS breakeven,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
                FROM discord_signals
            """)
            row = cur.fetchone()
            total, tp1, tp2, tp3, wins, losses, be, cancelled, active = row

            cur.execute("""
                SELECT symbol, COUNT(*) AS n,
                    SUM(CASE WHEN tp1_hit THEN 1 ELSE 0 END) AS tp1,
                    SUM(CASE WHEN tp2_hit THEN 1 ELSE 0 END) AS tp2,
                    SUM(CASE WHEN tp3_hit THEN 1 ELSE 0 END) AS tp3,
                    SUM(CASE WHEN status='loss' THEN 1 ELSE 0 END) AS losses
                FROM discord_signals
                WHERE symbol IS NOT NULL
                GROUP BY symbol
                HAVING COUNT(*) >= 5
                ORDER BY n DESC
                LIMIT 20
            """)
            top_symbols = [
                {"symbol": r[0], "n": int(r[1]), "tp1": int(r[2] or 0),
                 "tp2": int(r[3] or 0), "tp3": int(r[4] or 0), "losses": int(r[5] or 0)}
                for r in cur.fetchall()
            ]
        return {
            "total":      int(total or 0),
            "tp1_count":  int(tp1 or 0),
            "tp2_count":  int(tp2 or 0),
            "tp3_count":  int(tp3 or 0),
            "wins":       int(wins or 0),
            "losses":     int(losses or 0),
            "breakeven":  int(be or 0),
            "cancelled":  int(cancelled or 0),
            "active":     int(active or 0),
            "top_symbols": top_symbols,
        }
    finally:
        db_export._release_connection(conn)


def _outcome_distribution(stats: Dict[str, Any]) -> Dict[str, float]:
    """Convert raw counts into the 4-outcome probability distribution
    used by the EV calculator."""
    total = stats["total"] or 1
    tp1 = stats["tp1_count"]
    tp2 = stats["tp2_count"]
    tp3 = stats["tp3_count"]
    losses = stats["losses"]

    # Use the heuristic that a "loss" message ≈ TP1 was never hit.
    # Some "active"/"unknown" trades lack TP1 — treat them as
    # incomplete and exclude from the EV sample.
    completed = stats["wins"] + stats["losses"] + stats["breakeven"]
    if completed == 0:
        completed = total

    p_loss   = losses / total
    p_tp3    = tp3 / total                              # p(reached TP3)
    p_tp2_only = (tp2 - tp3) / total                    # p(TP2 hit but not TP3)
    p_tp1_only = (tp1 - tp2) / total                    # p(TP1 hit but not TP2)
    return {
        "loss":           max(0.0, p_loss),
        "tp1_only":       max(0.0, p_tp1_only),
        "tp1_tp2":        max(0.0, p_tp2_only),
        "tp1_tp2_tp3":    max(0.0, p_tp3),
    }


def simulate(splits: List[float], sl_policy: str, distribution: Dict[str, float]) -> Dict[str, float]:
    ev = sum(distribution[o] * _result_for_outcome(splits, o, sl_policy) for o in distribution)
    return {
        "ev_pct_per_trade": round(ev, 3),
        "splits": splits,
        "sl_policy": sl_policy,
    }


def run_analysis(logger=None) -> Dict[str, Any]:
    def _log(s):
        if logger: logger.info(s)
        else: print(s, file=sys.stderr)

    if not db_export.is_enabled():
        _log("[analyze] DATABASE_URL not set — cannot run analysis. Set DATABASE_URL and EXPORT_HISTORY first.")
        return {}

    _log("[analyze] querying discord_signals…")
    stats = fetch_signal_stats()
    if stats["total"] == 0:
        _log("[analyze] discord_signals table is EMPTY — run EXPORT_HISTORY=1 first.")
        return {}

    dist = _outcome_distribution(stats)

    strategies = []
    for splits in [[20, 30, 50], [33, 33, 34], [100, 0, 0], [0, 100, 0], [50, 50, 0]]:
        for policy in ["be_at_tp1", "be_at_tp2", "tp1_at_tp2"]:
            strategies.append(simulate(splits, policy, dist))
    # Sort by EV descending to make the best stand out
    strategies.sort(key=lambda s: -s["ev_pct_per_trade"])

    result = {
        "stats": stats,
        "outcome_distribution": dist,
        "strategies_ranked": strategies,
        "best": strategies[0] if strategies else None,
    }

    # ── Logs ────────────────────────────────────────────────────────────
    _log("=" * 60)
    _log(f"📊 SIGNAL HISTORY ANALYSIS — {stats['total']} signals scanned")
    _log("=" * 60)
    _log(f"Win:        {stats['wins']:>5}  ({stats['wins']*100/stats['total']:.1f}%)")
    _log(f"Loss:       {stats['losses']:>5}  ({stats['losses']*100/stats['total']:.1f}%)")
    _log(f"Breakeven:  {stats['breakeven']:>5}  ({stats['breakeven']*100/stats['total']:.1f}%)")
    _log(f"Active:     {stats['active']:>5}  (still running, excluded from EV)")
    _log(f"Cancelled:  {stats['cancelled']:>5}")
    _log("")
    _log(f"TP1 hit rate:  {stats['tp1_count']*100/stats['total']:.1f}%   ({stats['tp1_count']} trades)")
    _log(f"TP2 hit rate:  {stats['tp2_count']*100/stats['total']:.1f}%   ({stats['tp2_count']} trades)")
    _log(f"TP3 hit rate:  {stats['tp3_count']*100/stats['total']:.1f}%   ({stats['tp3_count']} trades)")
    _log("")
    _log(f"Outcome distribution used for EV math:")
    for k, v in dist.items():
        _log(f"  P({k}) = {v*100:.1f}%")
    _log("")
    _log("Strategy ranking (EV % margin per trade, after fees + slippage):")
    _log(f"  {'splits':<14} {'sl_policy':<14} {'EV/trade':>10}")
    _log(f"  {'-'*14} {'-'*14} {'-'*10}")
    for s in strategies:
        _log(f"  {str(s['splits']):<14} {s['sl_policy']:<14} {s['ev_pct_per_trade']:>9.2f}%")
    _log("")
    if strategies:
        b = strategies[0]
        _log(f"🏆 BEST: TP_SPLITS={b['splits']}  SL_POLICY={b['sl_policy']}  → +{b['ev_pct_per_trade']:.2f}% margin / trade")
    _log("")
    _log("Top 10 symbols by trade count (for warmup/blacklist tuning):")
    _log(f"  {'symbol':<14} {'n':>5} {'TP1%':>6} {'TP2%':>6} {'TP3%':>6} {'loss%':>6}")
    for s in stats["top_symbols"][:10]:
        n = s["n"]
        _log(f"  {s['symbol']:<14} {n:>5} {s['tp1']*100/n:>5.0f}% {s['tp2']*100/n:>5.0f}% {s['tp3']*100/n:>5.0f}% {s['losses']*100/n:>5.0f}%")
    _log("=" * 60)

    # ── JSON dump ───────────────────────────────────────────────────────
    out_path = os.getenv("ANALYSIS_JSON", "analysis.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        _log(f"[analyze] wrote {out_path}")
    except Exception as e:
        _log(f"[analyze] couldn't write {out_path}: {e}")

    # ── Telegram ────────────────────────────────────────────────────────
    try:
        import telegram_alerts
        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            top5 = strategies[:5]
            msg_lines = [
                f"📊 <b>Signal Analysis ({stats['total']} signals)</b>",
                "",
                f"Win: {stats['wins']*100/stats['total']:.1f}% · "
                f"Loss: {stats['losses']*100/stats['total']:.1f}% · "
                f"BE: {stats['breakeven']*100/stats['total']:.1f}%",
                f"TP1: {stats['tp1_count']*100/stats['total']:.1f}% · "
                f"TP2: {stats['tp2_count']*100/stats['total']:.1f}% · "
                f"TP3: {stats['tp3_count']*100/stats['total']:.1f}%",
                "",
                "<b>Top 5 strategies (EV % margin/trade):</b>",
            ]
            for s in top5:
                msg_lines.append(
                    f"<code>{str(s['splits']):<12} {s['sl_policy']:<12} "
                    f"{s['ev_pct_per_trade']:+.2f}%</code>"
                )
            telegram_alerts.send_message("\n".join(msg_lines))
            _log("[analyze] sent Telegram summary")
    except Exception as e:
        _log(f"[analyze] telegram send failed: {e}")

    return result


def main():
    run_analysis()


if __name__ == "__main__":
    main()
