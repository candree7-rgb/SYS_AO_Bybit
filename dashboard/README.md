# Trading Dashboard

Modern Next.js dashboard for visualizing trading bot performance.

## Features

- **Stats Cards**: Total trades, win rate, P&L, avg TPs/DCAs
- **Equity Curve**: Daily equity tracking with P&L visualization
- **Trade History Table**: Bybit-style trade list with sorting
- **TP Distribution**: Bar chart showing how often each TP was hit
- **DCA Distribution**: Bar chart showing DCA fill frequency
- **Real-time Updates**: Auto-refresh every 30-60 seconds

## Tech Stack

- Next.js 14 (App Router)
- TypeScript
- Tailwind CSS
- Recharts (for charts)
- PostgreSQL (via pg)

## Local Development

1. Install dependencies:
```bash
cd dashboard
npm install
```

2. Set environment variables:
```bash
cp .env.example .env
# Edit .env and add your DATABASE_URL
```

3. Run development server:
```bash
npm run dev
```

4. Open http://localhost:3000

## Build for Production

```bash
npm run build
npm start
```

## Environment Variables

- `DATABASE_URL` - PostgreSQL connection string (required)
- `PORT` - Port to run on (default: 3000, set by Railway)

## Database Schema

The dashboard expects these tables:

- `trades` - Individual trade records
- `daily_equity` - Daily equity snapshots

Schema is created automatically by the bot's `db_export.py` module.

## Deployment

### Railway (Recommended)

1. Create a new service from this repo
2. Set root directory to `dashboard`
3. Add `DATABASE_URL` environment variable
4. Railway auto-detects Next.js and deploys

### Vercel

1. Import repo to Vercel
2. Set root directory to `dashboard`
3. Add `DATABASE_URL` to environment variables
4. Deploy

### Docker

```bash
docker build -t trading-dashboard .
docker run -p 3000:3000 -e DATABASE_URL="postgres://..." trading-dashboard
```

## Customization

### Colors

Edit `tailwind.config.js` to change color scheme.

### Charts

Modify chart components in `components/` directory.

### Refresh Intervals

- Stats Cards: 30 seconds
- Charts: 60 seconds
- Trade Table: 30 seconds

Change in respective component files.

## Troubleshooting

### No data showing

- Verify `DATABASE_URL` is correct
- Check bot is running and writing to database
- Open browser console for errors

### Charts not rendering

- Ensure `recharts` is installed
- Check browser console for errors
- Verify data format from API

### Slow loading

- Check database performance
- Add indexes to frequently queried columns
- Consider caching with Redis
