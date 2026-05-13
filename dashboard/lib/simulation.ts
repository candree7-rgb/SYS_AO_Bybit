import { Trade } from './db'

export interface SimSettings {
  equity: number
  tradePct: number
  compounding: boolean
  excludeWeekends: boolean
  singlePerBatch: boolean
}

/** Check if a trade was opened on a weekend (Saturday or Sunday UTC) */
export function isWeekendTrade(trade: { opened_at: Date | string }): boolean {
  const d = new Date(trade.opened_at)
  const dow = d.getUTCDay() // 0 = Sunday, 6 = Saturday
  return dow === 0 || dow === 6
}

/**
 * Filter trades to keep only 1 per batch.
 * Batch = trades opened within 60s of each other (signal buffer is 5s,
 * but limit fills can lag a few seconds).
 * Keeps the first trade per batch (earliest opened_at).
 */
export function filterSinglePerBatch(trades: Trade[]): Trade[] {
  const real = trades.filter(t => t.side !== 'update')
  const updates = trades.filter(t => t.side === 'update')

  const sorted = [...real].sort((a, b) =>
    new Date(a.opened_at).getTime() - new Date(b.opened_at).getTime()
  )

  const BATCH_WINDOW_MS = 60_000
  const result: Trade[] = []
  let lastBatchTime = 0

  for (const trade of sorted) {
    const t = new Date(trade.opened_at).getTime()
    if (result.length === 0 || t - lastBatchTime > BATCH_WINDOW_MS) {
      result.push(trade)
      lastBatchTime = t
    }
  }

  // Re-add update rows (they don't affect simulation but may be shown in table)
  return [...result, ...updates]
}

/** Derive highest TP level from close_reason + tp1_hit (mirrors DB SQL logic) */
function getTpFills(trade: Trade): number {
  const reason = (trade.close_reason || '').toLowerCase()
  if (reason.includes('trail') && trade.tp1_hit) return 3
  if (reason.includes('tp3')) return 3
  if (reason.includes('tp2')) return 2
  if (trade.tp1_hit) return 1
  return 0
}

/** Check if trade exited via stop loss (mirrors DB SQL logic) */
function isSlExit(trade: Trade): boolean {
  const r = (trade.close_reason || '').toLowerCase()
  return (r.includes('sl') || (r.includes('stop') && !r.includes('trail')))
    && getTpFills(trade) === 0
    && parseFloat(trade.realized_pnl?.toString() || '0') < 0
}

/**
 * Compute structural stats from trades client-side.
 * Used when singlePerBatch filter is active (can't use DB stats endpoint).
 * Mirrors the SQL logic in getStats().
 */
export function computeClientStats(trades: Trade[]) {
  const real = trades.filter(t => t.side !== 'update')
  if (real.length === 0) return null

  const total_trades = real.length
  const wins = real.filter(t => parseFloat(t.realized_pnl?.toString() || '0') > 0).length
  const losses = real.filter(t => getTpFills(t) === 0 && parseFloat(t.realized_pnl?.toString() || '0') < 0).length
  const breakeven = real.filter(t => getTpFills(t) >= 1 && parseFloat(t.realized_pnl?.toString() || '0') <= 0).length
  const win_rate = total_trades > 0 ? ((wins + breakeven) / total_trades) * 100 : 0

  const sl_exits = real.filter(t => isSlExit(t)).length
  const sl_rate = total_trades > 0 ? (sl_exits / total_trades) * 100 : 0

  const avg_duration = real.reduce((sum, t) => sum + (parseFloat(t.duration_minutes?.toString() || '0')), 0) / real.length

  return {
    total_trades,
    wins,
    losses,
    breakeven,
    win_rate: parseFloat(win_rate.toFixed(1)),
    sl_rate: parseFloat(sl_rate.toFixed(1)),
    avg_duration,
  }
}

/** Compute TP exit distribution client-side (mirrors getExitDistribution SQL) */
export function computeTPDistribution(trades: Trade[]): { level: string; count: number; percentage: number }[] {
  const real = trades.filter(t => t.side !== 'update')
  const total = real.length
  if (total === 0) return []

  const tp1 = real.filter(t => getTpFills(t) >= 1).length
  const tp2 = real.filter(t => getTpFills(t) >= 2).length
  const tp3 = real.filter(t => getTpFills(t) >= 3).length
  const sl = real.filter(t => getTpFills(t) === 0 && isSlExit(t)).length
  const other = real.filter(t => getTpFills(t) === 0 && !isSlExit(t)).length

  return [
    { level: 'TP1', count: tp1, percentage: (tp1 / total) * 100 },
    { level: 'TP2', count: tp2, percentage: (tp2 / total) * 100 },
    { level: 'TP3', count: tp3, percentage: (tp3 / total) * 100 },
    { level: 'Stop Loss', count: sl, percentage: (sl / total) * 100 },
    { level: 'Other', count: other, percentage: (other / total) * 100 },
  ].filter(d => d.count > 0)
}

export interface SimTradeResult {
  sim_pnl: number
  sim_pnl_pct: number         // pnl_pct_equity scaled by tradePct/originalPct
  sim_equity_after: number
}

export interface SimSummary {
  total_sim_pnl: number
  final_equity: number
  total_return_pct: number
  max_drawdown: number      // absolute $ drawdown from peak
  max_drawdown_pct: number  // drawdown as % of peak equity
  per_trade: Map<string, SimTradeResult>
}

// Fallback for trades recorded before equity_pct_per_trade was stored in DB.
// Reads from NEXT_PUBLIC_BOT_EQUITY_PCT so it stays in sync with the bot's actual config.
const DEFAULT_EQUITY_PCT = Number(process.env.NEXT_PUBLIC_BOT_EQUITY_PCT) || 5

export function runSimulation(trades: Trade[], settings: SimSettings): SimSummary {
  // Sort chronologically (oldest first) for correct compounding order
  const sorted = [...trades].sort((a, b) =>
    new Date(a.closed_at).getTime() - new Date(b.closed_at).getTime()
  )

  let runningEquity = settings.equity
  let totalSimPnl = 0
  let peakEquity = settings.equity
  let maxDrawdown = 0
  let maxDrawdownPct = 0
  const perTrade = new Map<string, SimTradeResult>()

  for (const trade of sorted) {
    const baseEquity = settings.compounding ? runningEquity : settings.equity

    // Scale PnL proportionally when user simulates a different equity % per trade.
    // Each trade stores the ACTUAL equity_pct it was recorded at (falls back to 5%
    // for trades before this column existed). This handles mid-run config changes
    // correctly: e.g. first 50 trades at 5%, then switch to 10%.
    const originalPct = parseFloat(trade.equity_pct_per_trade?.toString() || '0') || DEFAULT_EQUITY_PCT
    const scaleFactor = settings.tradePct / originalPct

    // Use pnl_pct_equity (return on account equity) instead of pnl_pct_margin.
    // pnl_pct_margin is the return on DEPLOYED margin only, which is ~1/3 of
    // the slot allocation when DCA doesn't fill (DCA weights [1,2], E1=1/3).
    // Using pnl_pct_equity correctly accounts for the actual margin deployed.
    const pnlPctEquity = parseFloat(trade.pnl_pct_equity?.toString() || '0')
    const simPnl = baseEquity * (pnlPctEquity / 100) * scaleFactor

    runningEquity += simPnl
    totalSimPnl += simPnl

    // Track max drawdown (peak-to-trough)
    if (runningEquity > peakEquity) {
      peakEquity = runningEquity
    }
    const drawdown = peakEquity - runningEquity
    const drawdownPct = peakEquity > 0 ? (drawdown / peakEquity) * 100 : 0
    if (drawdown > maxDrawdown) {
      maxDrawdown = drawdown
      maxDrawdownPct = drawdownPct
    }

    perTrade.set(trade.trade_id, {
      sim_pnl: simPnl,
      sim_pnl_pct: pnlPctEquity * scaleFactor,
      sim_equity_after: runningEquity,
    })
  }

  return {
    total_sim_pnl: totalSimPnl,
    final_equity: runningEquity,
    total_return_pct: settings.equity > 0 ? (totalSimPnl / settings.equity) * 100 : 0,
    max_drawdown: maxDrawdown,
    max_drawdown_pct: maxDrawdownPct,
    per_trade: perTrade,
  }
}
