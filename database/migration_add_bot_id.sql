-- Migration: Add bot_id support for multi-bot dashboard
-- Run this when you want to add a second bot to the same database

-- Add bot_id column to trades table
ALTER TABLE trades ADD COLUMN IF NOT EXISTS bot_id VARCHAR(50) DEFAULT 'main';

-- Add bot_id column to daily_equity table
ALTER TABLE daily_equity ADD COLUMN IF NOT EXISTS bot_id VARCHAR(50) DEFAULT 'main';

-- Create index for faster bot_id filtering
CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id);
CREATE INDEX IF NOT EXISTS idx_daily_equity_bot_id ON daily_equity(bot_id);

-- Update primary key for daily_equity to include bot_id
-- (Allows multiple bots to have separate daily snapshots)
ALTER TABLE daily_equity DROP CONSTRAINT IF EXISTS daily_equity_pkey;
ALTER TABLE daily_equity ADD PRIMARY KEY (date, bot_id);

-- Update existing trades to have bot_id = 'main'
UPDATE trades SET bot_id = 'main' WHERE bot_id IS NULL;
UPDATE daily_equity SET bot_id = 'main' WHERE bot_id IS NULL;
