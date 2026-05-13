import { Pool } from 'pg'

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: 10,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 2000,
})

// ── Interfaces ──────────────────────────────────────────────────────────

export interface Trade {
  trade_id: string
  symbol: string
  side: string
  entry_price: number
  avg_price: number
  close_price: number
  total_qty: number
  total_margin: number
  leverage: number
  realized_pnl: number
  pnl_pct_margin: number
  pnl_pct_equity: number
  equity_at_entry: number
  equity_at_close: number
  is_win: boolean
  tp1_hit: boolean
  tps_hit: number
  trail_pnl_pct: number
  close_reason: string
  signal_leverage: number
  equity_pct_per_trade: number | null
  timeframe: string | null
  bot_id: string
  opened_at: Date
  closed_at: Date
  duration_minutes: number
}

export interface DailyEquity {
  date: Date
  equity: number
  daily_pnl: number
  daily_pnl_pct: number
  trades_count: number
  wins_count: number
  losses_count: number
  created_at: Date
}

export interface Stats {
  total_trades: number
  wins: number
  losses: number
  breakeven: number
  win_rate: number
  total_pnl: number
  total_pnl_pct: number
  avg_pnl: number
  avg_pnl_pct: number
  avg_win: number
  avg_win_pct: number
  avg_loss: number
  avg_loss_pct: number
  win_loss_ratio: number
  profit_factor: number
  best_trade: number
  worst_trade: number
  tp_rate: number
  sl_rate: number
  avg_duration: number
  trailing_exits: number
  sl_exits: number
  be_exits: number
}

export interface ExitDistribution {
  level: string
  count: number
  percentage: number
}

// ── Queries ─────────────────────────────────────────────────────────────

export async function getTrades(
  limit: number = 50,
  days?: number,
  from?: string,
  to?: string,
  excludeWeekends?: boolean
): Promise<Trade[]> {
  const client = await pool.connect()
  try {
    let whereClause = 'WHERE closed_at IS NOT NULL'
    if (from && to) {
      whereClause += ` AND closed_at >= '${from}' AND closed_at <= '${to}'`
    } else if (days) {
      whereClause += ` AND closed_at >= NOW() - INTERVAL '${days} days'`
    }
    if (excludeWeekends) {
      whereClause += ` AND EXTRACT(DOW FROM opened_at) NOT IN (0, 6)`
    }

    const result = await client.query(
      `SELECT * FROM trades ${whereClause} ORDER BY closed_at DESC LIMIT $1`,
      [limit]
    )
    return result.rows
  } finally {
    client.release()
  }
}

export async function getDailyEquity(
  days?: number,
  from?: string,
  to?: string
): Promise<DailyEquity[]> {
  const client = await pool.connect()
  try {
    let query = `SELECT * FROM daily_equity`
    const params: any[] = []
    const conditions: string[] = []

    if (from && to) {
      conditions.push(`date >= $1`)
      conditions.push(`date <= $2`)
      params.push(from, to)
    }

    if (conditions.length > 0) {
      query += ` WHERE ${conditions.join(' AND ')}`
    }

    if (days && !from && !to) {
      const limitIndex = params.length + 1
      query += ` ORDER BY date DESC LIMIT $${limitIndex}`
      params.push(days)
      const result = await client.query(query, params)
      return result.rows.reverse()
    }

    query += ` ORDER BY date ASC`
    const result = await client.query(query, params)
    return result.rows
  } finally {
    client.release()
  }
}

export async function getStats(
  days?: number,
  from?: string,
  to?: string,
  excludeWeekends?: boolean
): Promise<Stats> {
  const client = await pool.connect()
  try {
    let dateFilter = ''
    if (from && to) {
      dateFilter = ` AND closed_at >= '${from}' AND closed_at <= '${to}'`
    } else if (days) {
      dateFilter = ` AND closed_at >= NOW() - INTERVAL '${days} days'`
    }
    if (excludeWeekends) {
      dateFilter += ` AND EXTRACT(DOW FROM opened_at) NOT IN (0, 6)`
    }

    // BE/SL/TP1-TP3 exit classification (cumulative TPs: TP3 implies TP1+TP2)
    const query = `
      WITH enriched AS (
        SELECT *,
          CASE
            WHEN close_reason ILIKE '%trail%' AND tp1_hit THEN 3
            WHEN close_reason ILIKE '%tp3%' THEN 3
            WHEN close_reason ILIKE '%tp2%' THEN 2
            WHEN tp1_hit THEN 1
            ELSE 0
          END as tp_fills
        FROM trades
        WHERE closed_at IS NOT NULL AND side != 'update'${dateFilter}
      )
      SELECT
        COUNT(*) as total_trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN tp_fills = 0 AND realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
        SUM(CASE WHEN tp_fills >= 1 AND realized_pnl <= 0 THEN 1 ELSE 0 END) as breakeven,
        SUM(realized_pnl) as total_pnl,
        AVG(NULLIF(equity_at_entry, 0)) as avg_equity,
        AVG(realized_pnl) as avg_pnl,
        AVG(pnl_pct_equity) as avg_pnl_pct,
        AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
        AVG(CASE WHEN realized_pnl > 0 THEN pnl_pct_equity END) as avg_win_pct,
        AVG(CASE WHEN tp_fills = 0 AND realized_pnl < 0 THEN realized_pnl END) as avg_loss,
        AVG(CASE WHEN tp_fills = 0 AND realized_pnl < 0 THEN pnl_pct_equity END) as avg_loss_pct,
        MAX(realized_pnl) as best_trade,
        MIN(realized_pnl) as worst_trade,
        SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) as gross_profit,
        ABS(SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END)) as gross_loss,
        AVG(duration_minutes) as avg_duration,
        SUM(CASE WHEN close_reason ILIKE '%trail%' THEN 1 ELSE 0 END) as trailing_exits,
        SUM(CASE WHEN (close_reason ILIKE '%sl%' OR close_reason ILIKE '%stop%') AND realized_pnl < 0 AND tp_fills = 0 THEN 1 ELSE 0 END) as sl_exits,
        SUM(CASE WHEN close_reason ILIKE '%be%' THEN 1 ELSE 0 END) as be_exits,
        SUM(CASE WHEN tp_fills >= 1 THEN 1 ELSE 0 END) as tp_hits
      FROM enriched
    `

    const result = await client.query(query)
    const row = result.rows[0]

    if (!row || parseInt(row.total_trades) === 0) {
      return {
        total_trades: 0, wins: 0, losses: 0, breakeven: 0, win_rate: 0,
        total_pnl: 0, total_pnl_pct: 0, avg_pnl: 0, avg_pnl_pct: 0,
        avg_win: 0, avg_win_pct: 0, avg_loss: 0, avg_loss_pct: 0,
        win_loss_ratio: 0, profit_factor: 0, best_trade: 0, worst_trade: 0,
        tp_rate: 0, sl_rate: 0, avg_duration: 0,
        trailing_exits: 0, sl_exits: 0, be_exits: 0,
      }
    }

    const wins = parseInt(row.wins || 0)
    const losses = parseInt(row.losses || 0)
    const breakeven = parseInt(row.breakeven || 0)
    const total_trades = parseInt(row.total_trades)
    const avg_win = parseFloat(row.avg_win || 0)
    const avg_loss = parseFloat(row.avg_loss || 0)
    const win_loss_ratio = avg_loss !== 0 ? Math.abs(avg_win / avg_loss) : 0
    const total_pnl = parseFloat(row.total_pnl || 0)
    const avg_equity = parseFloat(row.avg_equity || 0)
    const total_pnl_pct = avg_equity > 0 ? (total_pnl / avg_equity) * 100 : 0
    const win_rate = total_trades > 0 ? ((wins + breakeven) / total_trades) * 100 : 0
    const gross_profit = parseFloat(row.gross_profit || 0)
    const gross_loss = parseFloat(row.gross_loss || 0)
    const profit_factor = gross_loss > 0 ? gross_profit / gross_loss : gross_profit > 0 ? Infinity : 0
    const tp_hits = parseInt(row.tp_hits || 0)
    const sl_exits_count = parseInt(row.sl_exits || 0)
    const tp_rate = total_trades > 0 ? (tp_hits / total_trades) * 100 : 0
    const sl_rate = total_trades > 0 ? (sl_exits_count / total_trades) * 100 : 0
    const avg_duration = parseFloat(row.avg_duration || 0)

    return {
      total_trades,
      wins,
      losses,
      breakeven,
      win_rate: parseFloat(win_rate.toFixed(1)),
      total_pnl,
      total_pnl_pct: parseFloat(total_pnl_pct.toFixed(2)),
      avg_pnl: parseFloat(row.avg_pnl || 0),
      avg_pnl_pct: parseFloat(row.avg_pnl_pct || 0),
      avg_win,
      avg_win_pct: parseFloat(parseFloat(row.avg_win_pct || 0).toFixed(2)),
      avg_loss,
      avg_loss_pct: parseFloat(parseFloat(row.avg_loss_pct || 0).toFixed(2)),
      win_loss_ratio: parseFloat(win_loss_ratio.toFixed(2)),
      profit_factor: parseFloat(profit_factor.toFixed(2)),
      best_trade: parseFloat(row.best_trade || 0),
      worst_trade: parseFloat(row.worst_trade || 0),
      tp_rate: parseFloat(tp_rate.toFixed(1)),
      sl_rate: parseFloat(sl_rate.toFixed(1)),
      avg_duration,
      trailing_exits: parseInt(row.trailing_exits || 0),
      sl_exits: sl_exits_count,
      be_exits: parseInt(row.be_exits || 0),
    }
  } finally {
    client.release()
  }
}

export async function getExitDistribution(
  days?: number,
  from?: string,
  to?: string,
  excludeWeekends?: boolean
): Promise<ExitDistribution[]> {
  const client = await pool.connect()
  try {
    let dateFilter = ''
    if (from && to) {
      dateFilter = ` AND closed_at >= '${from}' AND closed_at <= '${to}'`
    } else if (days) {
      dateFilter = ` AND closed_at >= NOW() - INTERVAL '${days} days'`
    }
    if (excludeWeekends) {
      dateFilter += ` AND EXTRACT(DOW FROM opened_at) NOT IN (0, 6)`
    }

    // BE/SL/TP1-TP3: cumulative — TP3 implies TP1+TP2
    const result = await client.query(`
      WITH trade_data AS (
        SELECT
          CASE
            WHEN close_reason ILIKE '%trail%' AND tp1_hit THEN 3
            WHEN close_reason ILIKE '%tp3%' THEN 3
            WHEN close_reason ILIKE '%tp2%' THEN 2
            WHEN tp1_hit THEN 1
            ELSE 0
          END as max_tp_hit,
          CASE
            WHEN close_reason ILIKE '%sl%' OR (close_reason ILIKE '%stop%' AND close_reason NOT ILIKE '%trail%') THEN true
            ELSE false
          END as is_sl
        FROM trades
        WHERE closed_at IS NOT NULL AND side != 'update'${dateFilter}
      ),
      total AS (SELECT COUNT(*) as cnt FROM trade_data)
      SELECT level, count FROM (
        SELECT 'TP1' as level, COUNT(*) as count, 1 as sort_order FROM trade_data WHERE max_tp_hit >= 1
        UNION ALL
        SELECT 'TP2', COUNT(*), 2 FROM trade_data WHERE max_tp_hit >= 2
        UNION ALL
        SELECT 'TP3', COUNT(*), 3 FROM trade_data WHERE max_tp_hit >= 3
        UNION ALL
        SELECT 'Stop Loss', COUNT(*), 4 FROM trade_data WHERE max_tp_hit = 0 AND is_sl
        UNION ALL
        SELECT 'Other', COUNT(*), 5 FROM trade_data WHERE max_tp_hit = 0 AND NOT is_sl
      ) sub
      ORDER BY sort_order
    `)

    const totalTrades = await client.query(`
      SELECT COUNT(*) as cnt FROM trades WHERE closed_at IS NOT NULL AND side != 'update'${dateFilter}
    `)
    const total = parseInt(totalTrades.rows[0]?.cnt || '0')

    return result.rows
      .map((r: any) => ({
        level: r.level,
        count: parseInt(r.count),
        percentage: total > 0 ? (parseInt(r.count) / total) * 100 : 0,
      }))
      .filter((d: ExitDistribution) => d.count > 0)
  } finally {
    client.release()
  }
}

export { pool }
