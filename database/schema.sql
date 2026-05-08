-- SYS_AO_Bybit - PostgreSQL Schema (migrated from hype design, adapted for BE/SL/TP1-TP3)
-- Railway PostgreSQL: auto-created on first startup via db_export.init_database()
-- Manual setup: psql $DATABASE_URL -f database/schema.sql

-- ══════════════════════════════════════════════════════════════════════════
-- TRADES: Every closed trade with full P&L details
-- ══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trades (
    trade_id            VARCHAR(100) PRIMARY KEY,
    symbol              VARCHAR(30) NOT NULL,
    side                VARCHAR(10) NOT NULL,       -- 'long' or 'short'

    -- Pricing
    entry_price         DECIMAL(20, 8),             -- Signal entry price
    avg_price           DECIMAL(20, 8),             -- Weighted avg (= entry_price when no DCA, else weighted)
    close_price         DECIMAL(20, 8),

    -- Position
    total_qty           DECIMAL(20, 8),
    total_margin        DECIMAL(20, 8),
    leverage            INTEGER DEFAULT 5,

    -- P&L
    realized_pnl        DECIMAL(20, 8) DEFAULT 0,
    pnl_pct_margin      DECIMAL(10, 4),             -- PnL % of margin used
    pnl_pct_equity      DECIMAL(10, 6),             -- PnL % of equity
    equity_at_entry     DECIMAL(12, 2),
    equity_at_close     DECIMAL(12, 2),
    is_win              BOOLEAN,

    -- Exit details (BE / SL / TP1-TP3 strategy)
    tp1_hit             BOOLEAN DEFAULT FALSE,
    tps_hit             INTEGER DEFAULT 0,          -- Total TPs filled (0-3)
    trail_pnl_pct       DECIMAL(10, 4) DEFAULT 0,   -- Trail price-% from avg
    close_reason        VARCHAR(200),               -- 'TP1+trail', 'BE-trail', 'Hard SL', etc.

    -- Config / signal context
    signal_leverage     INTEGER DEFAULT 0,          -- Original signal leverage
    equity_pct_per_trade DECIMAL(5, 2) DEFAULT 5.0, -- Bot's risk % when trade was opened
    timeframe           VARCHAR(10),                -- Signal timeframe (H1, M15, H4, ...)

    -- Multi-bot support
    bot_id              VARCHAR(50) DEFAULT 'ao',

    -- Timing
    opened_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    duration_minutes    INTEGER,

    -- Metadata
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_is_win ON trades(is_win);
CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side);
CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id);
CREATE INDEX IF NOT EXISTS idx_trades_timeframe ON trades(timeframe);


-- ══════════════════════════════════════════════════════════════════════════
-- DAILY EQUITY: Snapshot for equity chart in dashboard
-- ══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS daily_equity (
    date            DATE PRIMARY KEY,
    equity          DECIMAL(20, 8) NOT NULL,
    daily_pnl       DECIMAL(20, 8),
    daily_pnl_pct   DECIMAL(10, 4),
    trades_count    INTEGER DEFAULT 0,
    wins_count      INTEGER DEFAULT 0,
    losses_count    INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_equity_date ON daily_equity(date);


-- ══════════════════════════════════════════════════════════════════════════
-- AUTO-UPDATE TRIGGER
-- ══════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_trades_updated_at') THEN
        CREATE TRIGGER update_trades_updated_at
            BEFORE UPDATE ON trades
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    END IF;
END
$$;


-- ══════════════════════════════════════════════════════════════════════════
-- MIGRATIONS: Safe ADD COLUMN (idempotent, skips if exists)
-- ══════════════════════════════════════════════════════════════════════════

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='tps_hit') THEN
        ALTER TABLE trades ADD COLUMN tps_hit INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='trail_pnl_pct') THEN
        ALTER TABLE trades ADD COLUMN trail_pnl_pct DECIMAL(10,4) DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='equity_pct_per_trade') THEN
        ALTER TABLE trades ADD COLUMN equity_pct_per_trade DECIMAL(5,2) DEFAULT 5.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='timeframe') THEN
        ALTER TABLE trades ADD COLUMN timeframe VARCHAR(10);
        CREATE INDEX IF NOT EXISTS idx_trades_timeframe ON trades(timeframe);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='bot_id') THEN
        ALTER TABLE trades ADD COLUMN bot_id VARCHAR(50) DEFAULT 'ao';
        CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id);
    END IF;
END
$$;
