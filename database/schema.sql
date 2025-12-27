-- Trading Bot Database Schema
-- PostgreSQL Schema for Railway deployment

-- Trades Table: Stores all trade details
CREATE TABLE IF NOT EXISTS trades (
    id VARCHAR(100) PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(10) NOT NULL,  -- 'Long' or 'Short'
    order_side VARCHAR(10) NOT NULL,  -- 'Buy' or 'Sell'

    -- Pricing
    entry_price DECIMAL(20, 8),
    trigger_price DECIMAL(20, 8),
    avg_entry DECIMAL(20, 8),  -- After DCA fills

    -- Timing
    placed_at TIMESTAMP WITH TIME ZONE,
    filled_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,
    duration_minutes INTEGER,

    -- Performance
    realized_pnl DECIMAL(20, 8),
    pnl_pct_margin DECIMAL(10, 4),  -- PnL % relative to margin used
    pnl_pct_equity DECIMAL(10, 6),  -- PnL % relative to equity before trade
    margin_used DECIMAL(20, 8),
    equity_at_close DECIMAL(20, 8),
    is_win BOOLEAN,

    -- Trade Details
    exit_reason VARCHAR(50),
    tp_fills INTEGER DEFAULT 0,
    tp_count INTEGER DEFAULT 3,
    dca_fills INTEGER DEFAULT 0,
    dca_count INTEGER DEFAULT 2,
    trailing_used BOOLEAN DEFAULT FALSE,

    -- Multi-bot support
    bot_id VARCHAR(50) DEFAULT 'ao',

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Daily Equity Table: Stores end-of-day equity snapshots
CREATE TABLE IF NOT EXISTS daily_equity (
    date DATE PRIMARY KEY,
    equity DECIMAL(20, 8) NOT NULL,
    daily_pnl DECIMAL(20, 8),  -- Change from previous day
    daily_pnl_pct DECIMAL(10, 4),  -- % change from previous day
    trades_count INTEGER DEFAULT 0,
    wins_count INTEGER DEFAULT 0,
    losses_count INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_is_win ON trades(is_win);
CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id);
CREATE INDEX IF NOT EXISTS idx_daily_equity_date ON daily_equity(date);

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger to auto-update updated_at
DROP TRIGGER IF EXISTS update_trades_updated_at ON trades;
CREATE TRIGGER update_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Migration: Add bot_id column if it doesn't exist (for existing databases)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'trades' AND column_name = 'bot_id') THEN
        ALTER TABLE trades ADD COLUMN bot_id VARCHAR(50) DEFAULT 'ao';
        CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id);
    END IF;
END $$;
