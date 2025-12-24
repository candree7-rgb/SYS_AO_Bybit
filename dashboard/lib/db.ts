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

export async function getTrades(limit: number = 100, offset: number = 0): Promise<Trade[]> {
  const client = await pool.connect();
  try {
    const result = await client.query(
      `SELECT * FROM trades
       ORDER BY closed_at DESC NULLS LAST, placed_at DESC
       LIMIT $1 OFFSET $2`,
      [limit, offset]
    );
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

export async function getStats(days?: number): Promise<Stats> {
  const client = await pool.connect();
  try {
    let query = `
      SELECT
        COUNT(*) as total_trades,
        SUM(CASE WHEN is_win THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN NOT is_win THEN 1 ELSE 0 END) as losses,
        SUM(realized_pnl) as total_pnl,
        AVG(realized_pnl) as avg_pnl,
        MAX(realized_pnl) as best_trade,
        MIN(realized_pnl) as worst_trade,
        AVG(tp_fills) as avg_tp_fills,
        AVG(dca_fills) as avg_dca_fills,
        SUM(CASE WHEN exit_reason = 'trailing_stop' THEN 1 ELSE 0 END) as trailing_exits,
        SUM(CASE WHEN exit_reason = 'stop_loss' THEN 1 ELSE 0 END) as sl_exits,
        SUM(CASE WHEN exit_reason = 'breakeven' THEN 1 ELSE 0 END) as be_exits
      FROM trades
    `;

    const params = [];
    if (days) {
      query += ` WHERE closed_at >= NOW() - INTERVAL '${days} days'`;
    }

    const result = await client.query(query, params);
    const row = result.rows[0];

    if (!row || row.total_trades === 0) {
      return {
        total_trades: 0,
        wins: 0,
        losses: 0,
        win_rate: 0,
        total_pnl: 0,
        avg_pnl: 0,
        best_trade: 0,
        worst_trade: 0,
        avg_tp_fills: 0,
        avg_dca_fills: 0,
        trailing_exits: 0,
        sl_exits: 0,
        be_exits: 0,
      };
    }

    return {
      total_trades: parseInt(row.total_trades),
      wins: parseInt(row.wins),
      losses: parseInt(row.losses),
      win_rate: parseFloat(((row.wins / row.total_trades) * 100).toFixed(1)),
      total_pnl: parseFloat(row.total_pnl || 0),
      avg_pnl: parseFloat(row.avg_pnl || 0),
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

export async function getTPDistribution(): Promise<TPDistribution[]> {
  const client = await pool.connect();
  try {
    const result = await client.query(`
      SELECT
        generate_series(0, 3) as tp_level,
        COUNT(CASE WHEN tp_fills >= generate_series THEN 1 END) as count
      FROM trades
      WHERE closed_at IS NOT NULL
      GROUP BY tp_level
      ORDER BY tp_level
    `);
    return result.rows;
  } finally {
    client.release();
  }
}

export async function getDCADistribution(): Promise<DCADistribution[]> {
  const client = await pool.connect();
  try {
    const result = await client.query(`
      SELECT
        generate_series(0, 2) as dca_level,
        COUNT(CASE WHEN dca_fills >= generate_series THEN 1 END) as count
      FROM trades
      WHERE closed_at IS NOT NULL
      GROUP BY dca_level
      ORDER BY dca_level
    `);
    return result.rows;
  } finally {
    client.release();
  }
}

export { pool };
