# Landing Page (Railway service)

Static landing page for Systemic Trading Systems. Hosted as its own Railway service.

## Files
- `index.html` — single-page entry, fetches `/api/stats` from the dashboard service
- `images/` — logo + favicon

## Railway setup
Create a new service in the Railway project pointing at this repo:
- **Root Directory**: `landingpage`
- **Build Command**: `npm install`
- **Start Command**: `npm start` (binds to `$PORT`, served by `serve`)
- **Public Domain**: generate one in Settings to expose the page

The "See Results" button hard-links to `https://trades.systemic.systems/`. Update
`index.html` if the dashboard domain changes.
