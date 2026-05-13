"""
PostgreSQL Database Export Module

Exports trade data to PostgreSQL database for dashboard visualization.

Setup:
1. Add PostgreSQL to Railway project
2. Set env var:
   - DATABASE_URL: PostgreSQL connection string (auto-set by Railway)

Schema (database/schema.sql) is hype-style: 2 tables (trades, daily_equity),
adapted for BE/SL/TP1-TP3 strategy (no DCA / no zones).
"""

import os
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional, List

log = logging.getLogger("db_export")

# Try to import psycopg2, fall back gracefully
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import SimpleConnectionPool
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    psycopg2 = None
    RealDictCursor = None
    SimpleConnectionPool = None

_connection_pool = None  # type: Optional[SimpleConnectionPool]


def _get_connection_pool():
    """Get or create PostgreSQL connection pool."""
    global _connection_pool

    if _connection_pool is not None:
        return _connection_pool

    if not PSYCOPG2_AVAILABLE:
        return None

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        log.warning("DATABASE_URL not set")
        return None

    try:
        _connection_pool = SimpleConnectionPool(1, 5, db_url)
        log.info("PostgreSQL connection pool created")
        return _connection_pool
    except Exception as e:
        log.error(f"Failed to create connection pool: {e}")
        return None


def _get_connection():
    pool = _get_connection_pool()
    if not pool:
        return None
    try:
        return pool.getconn()
    except Exception as e:
        log.error(f"Failed to get connection from pool: {e}")
        return None


def _release_connection(conn):
    if conn and _connection_pool:
        _connection_pool.putconn(conn)


def init_database() -> bool:
    """Initialize database schema. Returns True on success."""
    conn = _get_connection()
    if not conn:
        return False

    try:
        schema_path = os.path.join(os.path.dirname(__file__), "database", "schema.sql")
        if not os.path.exists(schema_path):
            log.error(f"Schema file not found: {schema_path}")
            return False

        with open(schema_path, 'r') as f:
            schema_sql = f.read()

        with conn.cursor() as cur:
            cur.execute(schema_sql)
            conn.commit()
            log.info("Database schema initialized successfully")
            return True
    except Exception as e:
        log.error(f"Failed to initialize database: {e}")
        conn.rollback()
        return False
    finally:
        _release_connection(conn)


def _ts_to_datetime(ts: Optional[float]) -> Optional[datetime]:
    """Convert Unix timestamp to datetime object."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts)


def _norm_side(pos_side: Optional[str]) -> str:
    """Normalize pos_side to 'long'/'short'."""
    if not pos_side:
        return "long"
    return str(pos_side).strip().lower()


def export_trade(trade: Dict[str, Any]) -> bool:
    """Export a single trade to database. Returns True on success."""
    conn = _get_connection()
    if not conn:
        return False

    try:
        from config import BOT_ID

        filled_ts = trade.get("filled_ts") or 0
        closed_ts = trade.get("closed_ts") or 0
        opened_at = _ts_to_datetime(filled_ts)
        closed_at = _ts_to_datetime(closed_ts)
        duration_min = round((closed_ts - filled_ts) / 60) if filled_ts and closed_ts else None

        pnl = float(trade.get("realized_pnl") or 0)
        margin_used = float(trade.get("margin_used") or 0)
        pnl_margin_pct = (pnl / margin_used) * 100 if margin_used > 0 else 0

        equity_after = float(trade.get("equity_at_close") or 0)
        equity_before = equity_after - pnl
        pnl_equity_pct = (pnl / equity_before) * 100 if equity_before > 0 else 0

        tp_fills = int(trade.get("tp_fills") or 0)
        tp1_hit = tp_fills >= 1
        tps_hit = min(tp_fills, 3)

        leverage = trade.get("leverage") or 0
        signal_leverage = trade.get("signal_leverage") or leverage

        # 0 unless we wired up trailing peak tracking; keeps schema compatible.
        trail_pnl_pct = float(trade.get("trail_pnl_pct") or 0)

        avg_price = trade.get("avg_entry") or trade.get("entry_price")
        bot_id = trade.get("bot_id") or BOT_ID

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trades (
                    trade_id, symbol, side,
                    entry_price, avg_price, close_price,
                    total_qty, total_margin, leverage,
                    realized_pnl, pnl_pct_margin, pnl_pct_equity,
                    equity_at_entry, equity_at_close, is_win,
                    tp1_hit, tps_hit, trail_pnl_pct, close_reason,
                    signal_leverage, equity_pct_per_trade, timeframe,
                    bot_id,
                    opened_at, closed_at, duration_minutes
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s,
                    %s, %s, %s
                )
                ON CONFLICT (trade_id) DO UPDATE SET
                    avg_price = EXCLUDED.avg_price,
                    close_price = EXCLUDED.close_price,
                    total_qty = EXCLUDED.total_qty,
                    total_margin = EXCLUDED.total_margin,
                    realized_pnl = EXCLUDED.realized_pnl,
                    pnl_pct_margin = EXCLUDED.pnl_pct_margin,
                    pnl_pct_equity = EXCLUDED.pnl_pct_equity,
                    equity_at_close = EXCLUDED.equity_at_close,
                    is_win = EXCLUDED.is_win,
                    tp1_hit = EXCLUDED.tp1_hit,
                    tps_hit = EXCLUDED.tps_hit,
                    trail_pnl_pct = EXCLUDED.trail_pnl_pct,
                    close_reason = EXCLUDED.close_reason,
                    closed_at = EXCLUDED.closed_at,
                    duration_minutes = EXCLUDED.duration_minutes
            """, (
                trade.get("id"), trade.get("symbol"), _norm_side(trade.get("pos_side")),
                trade.get("entry_price"), avg_price, trade.get("close_price"),
                trade.get("base_qty"), margin_used, leverage,
                pnl, pnl_margin_pct, pnl_equity_pct,
                trade.get("equity_at_entry"), equity_after, bool(trade.get("is_win")),
                tp1_hit, tps_hit, trail_pnl_pct, trade.get("exit_reason", "unknown"),
                signal_leverage, trade.get("risk_pct"), trade.get("timeframe"),
                bot_id,
                opened_at, closed_at, duration_min
            ))
            conn.commit()
            log.info(f"Exported trade {trade.get('id')} to database")
            return True
    except Exception as e:
        log.error(f"Failed to export trade to database: {e}")
        conn.rollback()
        return False
    finally:
        _release_connection(conn)


def update_daily_equity(equity: float, trades_today: int = 0, wins_today: int = 0, losses_today: int = 0) -> bool:
    """Update daily equity snapshot. Returns True on success."""
    conn = _get_connection()
    if not conn:
        return False

    try:
        today = date.today()

        with conn.cursor() as cur:
            cur.execute("""
                SELECT equity FROM daily_equity
                WHERE date < %s
                ORDER BY date DESC
                LIMIT 1
            """, (today,))
            prev = cur.fetchone()
            prev_equity = float(prev[0]) if prev else equity

            daily_pnl = equity - prev_equity
            daily_pnl_pct = (daily_pnl / prev_equity * 100) if prev_equity > 0 else 0

            cur.execute("""
                INSERT INTO daily_equity (date, equity, daily_pnl, daily_pnl_pct, trades_count, wins_count, losses_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (date) DO UPDATE SET
                    equity = EXCLUDED.equity,
                    daily_pnl = EXCLUDED.daily_pnl,
                    daily_pnl_pct = EXCLUDED.daily_pnl_pct,
                    trades_count = EXCLUDED.trades_count,
                    wins_count = EXCLUDED.wins_count,
                    losses_count = EXCLUDED.losses_count
            """, (today, equity, daily_pnl, daily_pnl_pct, trades_today, wins_today, losses_today))
            conn.commit()
            log.debug(f"Updated daily equity: ${equity:.2f} (PnL: ${daily_pnl:+.2f})")
            return True
    except Exception as e:
        log.error(f"Failed to update daily equity: {e}")
        conn.rollback()
        return False
    finally:
        _release_connection(conn)


def get_trades(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Get trades from database. Returns list of trade dicts."""
    conn = _get_connection()
    if not conn:
        return []

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM trades
                ORDER BY closed_at DESC NULLS LAST, opened_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            return [dict(t) for t in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch trades: {e}")
        return []
    finally:
        _release_connection(conn)


def get_daily_equity(days: int = 30) -> List[Dict[str, Any]]:
    """Get daily equity snapshots."""
    conn = _get_connection()
    if not conn:
        return []

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM daily_equity
                ORDER BY date DESC
                LIMIT %s
            """, (days,))
            return [dict(e) for e in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch daily equity: {e}")
        return []
    finally:
        _release_connection(conn)


def get_stats(days: Optional[int] = None) -> Dict[str, Any]:
    """Get trade statistics. days=None for all time."""
    conn = _get_connection()
    if not conn:
        return {}

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            date_filter = ""
            params = []
            if days:
                date_filter = "WHERE closed_at >= NOW() - INTERVAL '%s days'"
                params = [days]

            cur.execute(f"""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN is_win THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN NOT is_win THEN 1 ELSE 0 END) as losses,
                    SUM(realized_pnl) as total_pnl,
                    AVG(realized_pnl) as avg_pnl,
                    MAX(realized_pnl) as best_trade,
                    MIN(realized_pnl) as worst_trade,
                    AVG(tps_hit) as avg_tps_hit,
                    SUM(CASE WHEN close_reason ILIKE '%%trail%%' THEN 1 ELSE 0 END) as trailing_exits,
                    SUM(CASE WHEN close_reason ILIKE '%%sl%%' OR close_reason ILIKE '%%stop%%' THEN 1 ELSE 0 END) as sl_exits,
                    SUM(CASE WHEN close_reason ILIKE '%%be%%' THEN 1 ELSE 0 END) as be_exits
                FROM trades
                {date_filter}
            """, params)
            stats = cur.fetchone()

            if not stats or stats['total_trades'] == 0:
                return {"total_trades": 0}

            stats = dict(stats)
            stats['win_rate'] = round(float(stats['wins']) / float(stats['total_trades']) * 100, 1)
            stats['total_pnl'] = float(stats['total_pnl'] or 0)
            stats['avg_pnl'] = float(stats['avg_pnl'] or 0)
            stats['best_trade'] = float(stats['best_trade'] or 0)
            stats['worst_trade'] = float(stats['worst_trade'] or 0)
            stats['avg_tps_hit'] = float(stats['avg_tps_hit'] or 0)

            return stats
    except Exception as e:
        log.error(f"Failed to fetch stats: {e}")
        return {}
    finally:
        _release_connection(conn)


def get_active_trade_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Check if there's an active trade for this symbol (from any bot).
    Active = closed_at IS NULL. Used for symbol locking across bots.
    """
    conn = _get_connection()
    if not conn:
        return None

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT trade_id, symbol, bot_id, opened_at
                FROM trades
                WHERE symbol = %s AND closed_at IS NULL
                ORDER BY opened_at DESC
                LIMIT 1
                """,
                (symbol,)
            )
            result = cur.fetchone()
            return dict(result) if result else None
    except Exception as e:
        log.error(f"Failed to check active trade for {symbol}: {e}")
        return None
    finally:
        _release_connection(conn)


def is_enabled() -> bool:
    """Check if database export is configured."""
    if not PSYCOPG2_AVAILABLE and os.getenv("DATABASE_URL"):
        if not hasattr(is_enabled, '_warned'):
            log.warning("DATABASE_URL set but psycopg2 not installed. Install with: pip install psycopg2-binary")
            is_enabled._warned = True
    return bool(os.getenv("DATABASE_URL")) and PSYCOPG2_AVAILABLE


def upsert_signal(channel_id: str, signal: Dict[str, Any]) -> bool:
    """Insert or update a Discord-signal row. `signal` is the dict
    produced by export_signals.export_message(). Idempotent on msg_id."""
    if not is_enabled():
        return False
    conn = _get_connection()
    if not conn:
        return False
    try:
        tps = signal.get("tp_prices") or []
        hit = signal.get("tps_hit") or {}
        ts_unix = signal.get("timestamp_unix") or 0
        ts_iso = signal.get("timestamp_iso") or None
        ed_iso = signal.get("edited_timestamp") or None
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discord_signals (
                    msg_id, channel_id, timestamp_iso, edited_iso,
                    base_symbol, symbol, side, trigger_price, sl_price,
                    tp1, tp2, tp3, tp4,
                    tp1_hit, tp2_hit, tp3_hit, tp4_hit,
                    status, closed_pnl_pct, open_pnl_pct,
                    fresh_parsable, raw_text
                ) VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s,
                          %s,%s,%s,%s, %s,%s,%s,%s,
                          %s,%s,%s, %s,%s)
                ON CONFLICT (msg_id) DO UPDATE SET
                    edited_iso     = EXCLUDED.edited_iso,
                    tp1_hit        = EXCLUDED.tp1_hit,
                    tp2_hit        = EXCLUDED.tp2_hit,
                    tp3_hit        = EXCLUDED.tp3_hit,
                    tp4_hit        = EXCLUDED.tp4_hit,
                    status         = EXCLUDED.status,
                    closed_pnl_pct = EXCLUDED.closed_pnl_pct,
                    open_pnl_pct   = EXCLUDED.open_pnl_pct,
                    fresh_parsable = EXCLUDED.fresh_parsable,
                    raw_text       = EXCLUDED.raw_text
                """,
                (
                    signal["msg_id"], channel_id, ts_iso, ed_iso or None,
                    signal.get("base_symbol"), signal.get("symbol"), signal.get("side"),
                    signal.get("trigger"), signal.get("sl_price"),
                    tps[0] if len(tps) > 0 else None,
                    tps[1] if len(tps) > 1 else None,
                    tps[2] if len(tps) > 2 else None,
                    tps[3] if len(tps) > 3 else None,
                    bool(hit.get(1, False)), bool(hit.get(2, False)),
                    bool(hit.get(3, False)), bool(hit.get(4, False)),
                    signal.get("status"),
                    signal.get("closed_pnl_pct"),
                    signal.get("open_pnl_pct"),
                    bool(signal.get("fresh_parsable")),
                    signal.get("raw_text"),
                )
            )
            conn.commit()
            return True
    except Exception as e:
        log.error(f"upsert_signal failed for {signal.get('msg_id')}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        _release_connection(conn)


def signals_count() -> int:
    """Returns total rows in discord_signals (for progress logging)."""
    if not is_enabled():
        return 0
    conn = _get_connection()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM discord_signals")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        _release_connection(conn)
