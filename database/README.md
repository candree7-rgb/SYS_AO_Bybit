# Database Schema

PostgreSQL schema, automatically initialized on first bot start via
`db_export.init_database()`. Manual setup: `psql $DATABASE_URL -f database/schema.sql`

## Tables

### `trades`
One row per closed trade.

Key columns:
- `trade_id` — primary key
- `symbol`, `side` (`'long'` / `'short'`)
- `entry_price`, `avg_price`, `close_price`
- `total_qty`, `total_margin`, `leverage`
- `realized_pnl`, `pnl_pct_margin`, `pnl_pct_equity`
- `equity_at_entry`, `equity_at_close`, `is_win`
- `tp1_hit` (bool), `tps_hit` (0-3), `trail_pnl_pct`, `close_reason`
- `signal_leverage`, `equity_pct_per_trade`, `timeframe`
- `bot_id` — multi-bot support (default `'ao'`)
- `opened_at`, `closed_at`, `duration_minutes`

### `daily_equity`
End-of-day equity snapshots used by the dashboard's equity chart.

## Multi-bot

`bot_id` is included from the start. Run a second bot with `BOT_ID=<name>` env
var; both bots share the same DB and the dashboard can filter by `bot_id`.

## Useful queries

Per-bot performance:
```sql
SELECT bot_id, COUNT(*) as trades,
       SUM(CASE WHEN is_win THEN 1 ELSE 0 END)::FLOAT / COUNT(*) * 100 as win_rate,
       SUM(realized_pnl) as total_pnl
FROM trades GROUP BY bot_id;
```

TP-fill distribution:
```sql
SELECT tps_hit, COUNT(*) FROM trades WHERE closed_at IS NOT NULL GROUP BY tps_hit;
```
