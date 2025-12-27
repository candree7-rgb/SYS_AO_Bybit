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
  win_rate: number;
  total_pnl: number;
  avg_pnl: number;
  avg_win: number;
  avg_loss: number;
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

export async function getDailyEquity(days: number = 30): Promise<DailyEquity[]> {
  const client = await pool.connect();
  try {
    const result = await client.query(
      `SELECT * FROM daily_equity
       ORDER BY date DESC
       LIMIT $1`,
      [days]
    );
    return result.rows.reverse(); // Return chronological order for charts
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
        SUM(CASE WHEN is_win THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN NOT is_win THEN 1 ELSE 0 END) as losses,
        SUM(realized_pnl) as total_pnl,
        AVG(realized_pnl) as avg_pnl,
        AVG(CASE WHEN is_win THEN realized_pnl END) as avg_win,
        AVG(CASE WHEN NOT is_win THEN realized_pnl END) as avg_loss,
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
        win_rate: 0,
        total_pnl: 0,
        avg_pnl: 0,
        avg_win: 0,
        avg_loss: 0,
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

    const avg_win = parseFloat(row.avg_win || 0);
    const avg_loss = parseFloat(row.avg_loss || 0);
    const win_loss_ratio = avg_loss !== 0 ? Math.abs(avg_win / avg_loss) : 0;

    return {
      total_trades: parseInt(row.total_trades),
      wins: parseInt(row.wins),
      losses: parseInt(row.losses),
      win_rate: parseFloat(((row.wins / row.total_trades) * 100).toFixed(1)),
      total_pnl: parseFloat(row.total_pnl || 0),
      avg_pnl: parseFloat(row.avg_pnl || 0),
      avg_win,
      avg_loss,
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
    const queries: string[] = [];
    for (let i = 0; i <= dcaCount; i++) {
      queries.push(`SELECT ${i} as dca_level, COUNT(*) as count FROM trades WHERE dca_fills >= ${i} AND closed_at IS NOT NULL ${botFilter}`);
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
