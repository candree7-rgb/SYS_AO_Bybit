# Railway Deployment Guide

This guide explains how to deploy the trading bot and dashboard to Railway.

## Prerequisites

- Railway account (https://railway.app)
- GitHub repository connected to Railway

## Architecture

The deployment consists of 3 services:

1. **PostgreSQL Database** - Stores trades and daily equity
2. **Trading Bot** (Python) - Runs the bot, writes to database
3. **Dashboard** (Next.js) - Visualizes trading data

## Deployment Steps

### 1. Create Railway Project

1. Go to Railway.app → New Project
2. Select "Deploy from GitHub repo"
3. Select your repository: `candree7-rgb/SYS_AO_Bybit`

### 2. Add PostgreSQL Database

1. In your Railway project, click "+ New"
2. Select "Database" → "PostgreSQL"
3. Railway will automatically create a `DATABASE_URL` environment variable

### 3. Deploy Trading Bot

1. In your project, click "+ New" → "GitHub Repo"
2. Select your repo again (this creates a second service)
3. Configure the bot service:
   - **Root Directory**: `/` (default)
   - **Start Command**: `python main.py`
   - **Environment Variables**:
     ```
     DISCORD_TOKEN=your_discord_token
     CHANNEL_ID=your_channel_id
     BYBIT_API_KEY=your_api_key
     BYBIT_API_SECRET=your_api_secret
     DATABASE_URL=${{Postgres.DATABASE_URL}}  # Reference to PostgreSQL
     TELEGRAM_BOT_TOKEN=your_telegram_token (optional)
     TELEGRAM_CHAT_ID=your_chat_id (optional)
     ```

4. The bot will automatically:
   - Install dependencies from `requirements.txt`
   - Initialize the database schema on first run
   - Start trading and exporting to PostgreSQL

### 4. Deploy Dashboard

1. In your project, click "+ New" → "GitHub Repo"
2. Select your repo again (this creates a third service)
3. Configure the dashboard service:
   - **Root Directory**: `dashboard`
   - **Build Command**: `npm run build`
   - **Start Command**: `npm start`
   - **Environment Variables**:
     ```
     DATABASE_URL=${{Postgres.DATABASE_URL}}  # Reference to PostgreSQL
     PORT=3000
     ```

4. Railway will:
   - Install npm dependencies
   - Build the Next.js app
   - Expose the dashboard on a public URL

### 5. Access Your Dashboard

1. Go to your dashboard service in Railway
2. Click "Settings" → "Generate Domain"
3. Railway will give you a URL like: `https://your-dashboard.up.railway.app`
4. Open the URL to see your dashboard! 🎉

## Database Initialization

The database schema is automatically created when the bot starts for the first time.

If you need to manually initialize:

```bash
# SSH into the bot service (Railway CLI)
railway run python -c "import db_export; db_export.init_database()"
```

## Environment Variables Summary

### Bot Service
- `DISCORD_TOKEN` - Discord bot token
- `CHANNEL_ID` - Discord channel ID for signals
- `BYBIT_API_KEY` - Bybit API key
- `BYBIT_API_SECRET` - Bybit API secret
- `DATABASE_URL` - PostgreSQL connection (auto-linked)
- `TELEGRAM_BOT_TOKEN` - (Optional) Telegram bot token
- `TELEGRAM_CHAT_ID` - (Optional) Telegram chat ID
- `DRY_RUN` - (Optional) Set to "true" for testing

### Dashboard Service
- `DATABASE_URL` - PostgreSQL connection (auto-linked)
- `PORT` - Port to run on (set by Railway automatically)

## Monitoring

### Bot Logs
- Go to bot service → "Deployments" → View logs
- Look for: "✅ Database ready" on startup

### Dashboard Logs
- Go to dashboard service → "Deployments" → View logs
- Check for successful build and start

### Database
- Go to PostgreSQL service → "Data" to view tables
- Tables: `trades`, `daily_equity`

## Troubleshooting

### Bot won't start
- Check environment variables are set correctly
- Verify `DATABASE_URL` is linked to PostgreSQL service
- Check logs for error messages

### Dashboard shows no data
- Verify bot is running and has executed trades
- Check `DATABASE_URL` is linked
- Use Railway's "Data" tab to verify trades exist in database

### Database connection errors
- Ensure all services use `${{Postgres.DATABASE_URL}}` reference
- Restart services after adding DATABASE_URL

## Cost

Railway offers:
- **Free Tier**: $5 credit/month (enough for small bots)
- **Pro Plan**: $20/month for production use

Estimated usage:
- PostgreSQL: ~$5/month
- Bot service: ~$5/month (minimal CPU/RAM)
- Dashboard: ~$5/month (minimal traffic)

**Total**: ~$15/month for all services

## Migrating from Google Sheets

If you were using Google Sheets before:

1. Historical data will **NOT** be migrated automatically
2. New trades will go to PostgreSQL
3. Old sheets_export.py can be removed (kept for reference)
4. To migrate old data: manually export Sheets → CSV → import to PostgreSQL

## Support

For issues:
- Railway Docs: https://docs.railway.app
- GitHub Issues: https://github.com/candree7-rgb/SYS_AO_Bybit/issues
