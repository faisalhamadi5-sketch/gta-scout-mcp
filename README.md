# GTA Scout MCP Server — Repliers Edition
## Live TRREB data. No CSV exports. No Google Sheets. Ever.

---

## What changed from v1

The server now calls the **Repliers API** directly on every query.
Real-time TRREB/RAHB data flows straight to Claude — no manual exports needed.

---

## Step 1 — Get your Repliers API key (5 minutes)

1. Go to **repliers.com** → click Get Started (free Preview plan)
2. Sign up with your email
3. Go to your dashboard → copy your **API Key**
4. Keep it handy for Step 2

---

## Step 2 — Deploy to Railway (10 minutes, free)

1. Go to **railway.app** → sign up with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Upload this folder to a new GitHub repo (drag-drop or push)
4. Railway auto-detects Python and deploys
5. Go to **Settings → Variables** and add:
   ```
   REPLIERS_API_KEY = your_repliers_key_here
   PORT = 8000
   ```
6. Your server goes live at: `https://gta-scout-mcp.up.railway.app`
7. Test it: open `https://gta-scout-mcp.up.railway.app/health`
   You should see: `{"status":"ok","api_key_set":true,"tools":4}`

---

## Step 3 — Connect to Claude (2 minutes)

1. Go to **claude.ai → Settings → Integrations**
2. Click **Add custom MCP server**
3. Paste your Railway URL
4. Claude auto-discovers all 4 tools
5. Start querying: *"Find Power of Sale listings in Burlington under $2M"*

---

## The 4 live tools

| Tool | What it queries live from Repliers |
|------|-----------------------------------|
| `search_expired_listings` | Expired listings near any lat/lng within a radius |
| `search_pos_listings` | POS keyword search across Halton (active + expired) |
| `search_development_land` | Dev land by zoning/OPA/assembly keywords |
| `get_market_stats` | Live active count, avg price, DOM, expired count |

---

## Running locally for testing

```bash
export REPLIERS_API_KEY=your_key_here
python server.py
```

Test a live query:
```bash
curl -X POST http://localhost:8000/mcp/tools/call \
  -H "Content-Type: application/json" \
  -d '{"name":"search_pos_listings","arguments":{"city":"Burlington","max_price":2000000}}'
```

---

## Upgrading Repliers plan

- **Preview ($0):** Sample data only — use for testing the integration
- **Standard ($199/mo):** Live production TRREB data — flip to this when pitching brokerages
- One environment variable change (`REPLIERS_API_KEY`) is all that changes between plans

---

## Data refresh

None needed. Every Claude query hits Repliers in real time.
Listings update as fast as TRREB pushes them to Repliers (typically within minutes).
