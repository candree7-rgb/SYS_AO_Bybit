# Database Schema & Migrations

## Current Schema

The database has two main tables:
- `trades` - Individual trade records
- `daily_equity` - Daily equity snapshots

## Multi-Bot Support (Future)

The code is **ready for multi-bot support** via `bot_id` field.

### Current Status
- ✅ Code supports `bot_id`
- ❌ Database column **NOT YET ADDED** (backward compatible)
- ✅ Works without migration (defaults to 'main')

### When to Migrate

Run migration when you want to add a **second bot** to the same database.

### How to Migrate

**1. Connect to PostgreSQL:**
```bash
# Railway:
railway connect Postgres

# Or use psql:
psql $DATABASE_URL
```

**2. Run Migration:**
```sql
\i database/migration_add_bot_id.sql
```

**3. Verify:**
```sql
\d trades  -- Should show bot_id column
SELECT DISTINCT bot_id FROM trades;  -- Should show 'main'
```

**4. Start Second Bot:**
```bash
# In Railway, add new bot service with:
BOT_ID=scalping

# Or for local testing:
export BOT_ID=scalping
python main.py
```

### Dashboard Multi-Bot Support

After migration, the dashboard can filter by bot:
- All bots combined view
- Individual bot view
- Comparison view (Bot A vs Bot B)

**Note:** Dashboard multi-bot UI is not yet implemented. After migration, you'll need to:
1. Add dropdown/tabs to dashboard
2. Add `bot_id` filter to all queries
3. Optionally: add comparison charts

## Manual SQL Operations

### Check which bots exist:
```sql
SELECT bot_id, COUNT(*) as trades, SUM(realized_pnl) as total_pnl
FROM trades
GROUP BY bot_id;
```

### View specific bot's trades:
```sql
SELECT * FROM trades WHERE bot_id = 'scalping' ORDER BY closed_at DESC LIMIT 10;
```

### Compare bot performance:
```sql
SELECT
  bot_id,
  COUNT(*) as total_trades,
  SUM(CASE WHEN is_win THEN 1 ELSE 0 END)::FLOAT / COUNT(*) * 100 as win_rate,
  SUM(realized_pnl) as total_pnl
FROM trades
GROUP BY bot_id;
```
