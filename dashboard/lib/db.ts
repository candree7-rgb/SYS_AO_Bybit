import { Pool } from 'pg';

// Create PostgreSQL connection pool
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: 10,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 2000,
});

export interface Trade {
  id: string;
  symbol: string;
  side: string;
  order_side: string;
  entry_price: number;
  trigger_price: number;
  avg_entry: number | null;
  placed_at: Date;
  filled_at: Date | null;
  closed_at: Date | null;
  duration_minutes: number | null;
  realized_pnl: number;
  pnl_pct_margin: number;
  pnl_pct_equity: number;
  margin_used: number;
  equity_at_close: number;
  is_win: boolean;
  exit_reason: string;
  tp_fills: number;
  tp_count: number;
  dca_fills: number;
  dca_count: number;
  trailing_used: boolean;
  bot_id: string;
  created_at: Date;
  updated_at: Date;
}

export interface DailyEquity {
  date: Date;
  equity: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  trades_count: number;
  wins_count: number;
  losses_count: number;
  created_at: Date;
}

export interface Stats {
  total_trades: number;
  wins: number;
  losses: number;
  breakeven: number;  // TP1+ reached but closed at/below 0
  win_rate: number;
  total_pnl: number;
  total_pnl_pct: number;  // Total PnL as % of average equity
  avg_pnl: number;
  avg_win: number;
  avg_win_pct: number;  // Average win as % of equity
  avg_loss: number;
  avg_loss_pct: number;  // Average loss as % of equity (only pure SL trades)
  win_loss_ratio: number;
  best_trade: number;
  worst_trade: number;
  avg_tp_fills: number;
  avg_dca_fills: number;
  trailing_exits: number;
  sl_exits: number;
  be_exits: number;
}

export interface TPDistribution {
  tp_level: number;
  count: number;
}

export interface DCADistribution {
  dca_level: number;
  count: number;
}

export async function getTrades(limit: number = 100, offset: number = 0, botId?: string): Promise<Trade[]> {
  const client = await pool.connect();
  try {
    let query = `SELECT * FROM trades`;
    const params: any[] = [];

    if (botId) {
      query += ` WHERE bot_id = $1`;
      params.push(botId);
      query += ` ORDER BY closed_at DESC NULLS LAST, placed_at DESC LIMIT $2 OFFSET $3`;
      params.push(limit, offset);
    } else {
      query += ` ORDER BY closed_at DESC NULLS LAST, placed_at DESC LIMIT $1 OFFSET $2`;
      params.push(limit, offset);
    }

    const result = await client.query(query, params);
    return result.rows;
  } finally {
    client.release();
  }
}

export async function getDailyEquity(days?: number): Promise<DailyEquity[]> {
  const client = await pool.connect();
  try {
    let query = `SELECT * FROM daily_equity ORDER BY date DESC`;
    const params: any[] = [];

    if (days) {
      query += ` LIMIT $1`;
      params.push(days);
    }

    const result = await client.query(query, params);
    return result.rows.reverse(); // Return chronological order for charts
  } finally {
    client.release();
  }
}

/**
 * Get cumulative PnL curve for a specific bot by summing up realized_pnl from trades
 * This creates a pseudo-equity curve showing the bot's performance over time
 */
export async function getBotCumulativePnL(botId: string, days?: number): Promise<DailyEquity[]> {
  const client = await pool.connect();
  try {
    // Use CTE to first aggregate by day, then apply window function for cumulative sum
    let query = `
      WITH daily_aggregates AS (
        SELECT
          DATE(closed_at) as date,
          SUM(realized_pnl) as daily_pnl,
          COUNT(*) as trades_count,
          SUM(CASE WHEN is_win THEN 1 ELSE 0 END) as wins_count,
          SUM(CASE WHEN NOT is_win THEN 1 ELSE 0 END) as losses_count,
          MIN(closed_at) as created_at
        FROM trades
        WHERE bot_id = $1 AND closed_at IS NOT NULL
    `;

    const params: any[] = [botId];

    if (days) {
      query += ` AND closed_at >= NOW() - INTERVAL '${days} days'`;
    }

    query += `
        GROUP BY DATE(closed_at)
      )
      SELECT
        date,
        SUM(daily_pnl) OVER (ORDER BY date) as equity,
        daily_pnl,
        0 as daily_pnl_pct,
        trades_count,
        wins_count,
        losses_count,
        created_at
      FROM daily_aggregates
      ORDER BY date ASC
    `;

    const result = await client.query(query, params);

    // Calculate daily_pnl_pct based on previous day's equity
    const rows = result.rows.map((row, idx) => {
      const prevEquity = idx > 0 ? parseFloat(result.rows[idx - 1].equity) : 0;
      const dailyPnl = parseFloat(row.daily_pnl);
      const dailyPnlPct = prevEquity > 0 ? (dailyPnl / prevEquity) * 100 : 0;

      return {
        ...row,
        equity: parseFloat(row.equity),
        daily_pnl: dailyPnl,
        daily_pnl_pct: parseFloat(dailyPnlPct.toFixed(4)),
        trades_count: parseInt(row.trades_count),
        wins_count: parseInt(row.wins_count),
        losses_count: parseInt(row.losses_count),
      };
    });

    return rows;
  } finally {
    client.release();
  }
}

export async function getStats(days?: number, botId?: string): Promise<Stats> {
  const client = await pool.connect();
  try {
    let query = `
      SELECT
        COUNT(*) as total_trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN tp_fills = 0 AND realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
        SUM(CASE WHEN tp_fills >= 1 AND realized_pnl <= 0 THEN 1 ELSE 0 END) as breakeven,
        SUM(realized_pnl) as total_pnl,
        AVG(equity_at_close) as avg_equity,
        AVG(realized_pnl) as avg_pnl,
        AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
        AVG(CASE WHEN realized_pnl > 0 THEN pnl_pct_equity END) as avg_win_pct,
        AVG(CASE WHEN tp_fills = 0 AND realized_pnl < 0 THEN realized_pnl END) as avg_loss,
        AVG(CASE WHEN tp_fills = 0 AND realized_pnl < 0 THEN pnl_pct_equity END) as avg_loss_pct,
        MAX(realized_pnl) as best_trade,
        MIN(realized_pnl) as worst_trade,
        AVG(tp_fills) as avg_tp_fills,
        AVG(dca_fills) as avg_dca_fills,
        SUM(CASE WHEN exit_reason = 'trailing_stop' THEN 1 ELSE 0 END) as trailing_exits,
        SUM(CASE WHEN exit_reason = 'stop_loss' THEN 1 ELSE 0 END) as sl_exits,
        SUM(CASE WHEN exit_reason = 'breakeven' THEN 1 ELSE 0 END) as be_exits
      FROM trades
    `;

    const conditions: string[] = [];
    if (days) {
      conditions.push(`closed_at >= NOW() - INTERVAL '${days} days'`);
    }
    if (botId) {
      conditions.push(`bot_id = '${botId}'`);
    }
    if (conditions.length > 0) {
      query += ` WHERE ${conditions.join(' AND ')}`;
    }

    const result = await client.query(query);
    const row = result.rows[0];

    if (!row || row.total_trades === 0) {
      return {
        total_trades: 0,
        wins: 0,
        losses: 0,
        breakeven: 0,
        win_rate: 0,
        total_pnl: 0,
        total_pnl_pct: 0,
        avg_pnl: 0,
        avg_win: 0,
        avg_win_pct: 0,
        avg_loss: 0,
        avg_loss_pct: 0,
        win_loss_ratio: 0,
        best_trade: 0,
        worst_trade: 0,
        avg_tp_fills: 0,
        avg_dca_fills: 0,
        trailing_exits: 0,
        sl_exits: 0,
        be_exits: 0,
      };
    }

    const wins = parseInt(row.wins || 0);
    const losses = parseInt(row.losses || 0);
    const breakeven = parseInt(row.breakeven || 0);
    const total_trades = parseInt(row.total_trades);

    const avg_win = parseFloat(row.avg_win || 0);
    const avg_win_pct = parseFloat(row.avg_win_pct || 0);
    const avg_loss = parseFloat(row.avg_loss || 0);
    const avg_loss_pct = parseFloat(row.avg_loss_pct || 0);
    const win_loss_ratio = avg_loss !== 0 ? Math.abs(avg_win / avg_loss) : 0;

    const total_pnl = parseFloat(row.total_pnl || 0);
    const avg_equity = parseFloat(row.avg_equity || 0);
    const total_pnl_pct = avg_equity > 0 ? (total_pnl / avg_equity) * 100 : 0;

    // Win rate includes wins + breakeven (TP1 reached = strategic success)
    const win_rate = total_trades > 0 ? ((wins + breakeven) / total_trades) * 100 : 0;

    return {
      total_trades,
      wins,
      losses,
      breakeven,
      win_rate: parseFloat(win_rate.toFixed(1)),
      total_pnl,
      total_pnl_pct: parseFloat(total_pnl_pct.toFixed(2)),
      avg_pnl: parseFloat(row.avg_pnl || 0),
      avg_win,
      avg_win_pct: parseFloat(avg_win_pct.toFixed(2)),
      avg_loss,
      avg_loss_pct: parseFloat(avg_loss_pct.toFixed(2)),
      win_loss_ratio: parseFloat(win_loss_ratio.toFixed(2)),
      best_trade: parseFloat(row.best_trade || 0),
      worst_trade: parseFloat(row.worst_trade || 0),
      avg_tp_fills: parseFloat(row.avg_tp_fills || 0),
      avg_dca_fills: parseFloat(row.avg_dca_fills || 0),
      trailing_exits: parseInt(row.trailing_exits || 0),
      sl_exits: parseInt(row.sl_exits || 0),
      be_exits: parseInt(row.be_exits || 0),
    };
  } finally {
    client.release();
  }
}

export async function getTPDistribution(tpCount: number = 3, botId?: string): Promise<TPDistribution[]> {
  const client = await pool.connect();
  try {
    const botFilter = botId ? `AND bot_id = '${botId}'` : '';

    // Build dynamic query based on tpCount
    const queries: string[] = [];
    for (let i = 1; i <= tpCount; i++) {
      queries.push(`SELECT ${i} as tp_level, COUNT(*) as count FROM trades WHERE tp_fills >= ${i} AND closed_at IS NOT NULL ${botFilter}`);
    }

    const result = await client.query(`
      ${queries.join(' UNION ALL ')}
      ORDER BY tp_level
    `);
    return result.rows;
  } finally {
    client.release();
  }
}

export async function getDCADistribution(dcaCount: number = 2, botId?: string): Promise<DCADistribution[]> {
  const client = await pool.connect();
  try {
    const botFilter = botId ? `AND bot_id = '${botId}'` : '';

    // Build dynamic query based on dcaCount
    // DCA0 = exactly 0 DCAs filled, DCA1 = exactly 1 DCA filled, etc.
    const queries: string[] = [];
    for (let i = 0; i <= dcaCount; i++) {
      queries.push(`SELECT ${i} as dca_level, COUNT(*) as count FROM trades WHERE dca_fills = ${i} AND closed_at IS NOT NULL ${botFilter}`);
    }

    const result = await client.query(`
      ${queries.join(' UNION ALL ')}
      ORDER BY dca_level
    `);
    return result.rows;
  } finally {
    client.release();
  }
}

export { pool };
