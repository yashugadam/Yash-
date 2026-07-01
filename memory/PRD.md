# Renko Nifty Bot — PRD

## Original Problem Statement
Algo trading bot that places real orders on the user's Angel One account using a
Renko strategy. Brick size 50, 1-minute timeframe, NIFTY Future lot (qty 65),
carry-forward. Entry: short when 2 red bricks form. Exit: <=4 reds → exit on 1st
green; >4 reds → wait for 2 greens.

## Product Requirements
- Real money execution only (no demo/simulation).
- Broker reconciliation panel on restart for missed fills.
- Auto-reconnect watchdog for Angel One websocket drops.
- Order placement routed via external static-IP proxy (Azure VM) for SEBI compliance.
- Order and rejection log UI.

## Architecture
- Backend: FastAPI + MongoDB (motor). `server.py` (engine/state machine/API),
  `angel_broker.py` (SmartAPI wrapper + order proxy routing).
- Frontend: React + Tailwind. `Dashboard.js`, `RenkoChart.js`.
- Proxy: Azure VM `tinyproxy` on port 8888 (BasicAuth `algouser:yashgadam`),
  reachable at `4.188.96.104`. `ANGEL_PROXY_URL` routes ONLY order place/modify/
  cancel + position fetch; login & market data go direct.

## Environments
- PREVIEW (dev): worked on directly.
- PRODUCTION: https://renko-nifty-bot.emergent.host (separate deploy env vars).

## Implemented (this session — June 2026)
- ✅ Resolved Azure proxy chain end-to-end: opened NSG port 8888 (Source=Any),
  removed tinyproxy IP whitelist, added BasicAuth. `_angel_proxies()` injects
  credentials (URL or ANGEL_PROXY_USER/PASS) → no more 407. Orders egress from
  Azure static IP `4.188.96.104`.
- ✅ Fixed production 407: requires `ANGEL_PROXY_URL=http://algouser:yashgadam@4.188.96.104:8888`
  in the production deploy env var (user updated + redeployed).
- ✅ SEO: real robots.txt, sitemap.xml, llms.txt, title/meta/OG tags in public/.
- ✅ Real Daily P&L from Angel One: `get_day_pnl()` reads position book (realised/
  unrealised), engine refreshes throttled ~8s (`_refresh_broker_pnl`), exposed at
  `risk.broker_pnl`, shown on dashboard as "Angel One Day P&L (live)" block.
  Reflects manual-panel + bot fills. Verified live (iteration_3.json, 5/5 backend).

## Backlog / Next Tasks
- P2: Dashboard indicator "Order route: Azure proxy ✓ + live outbound IP".
- P2 (review nits, optional): add `last_synced_at` + STALE state to broker P&L
  badge; filter `get_day_pnl` by `fut_token` if multiple instruments held;
  expose top-level `broker.connected` in /api/state.
- Refactor: `Dashboard.js` (>600 lines) → split into OrderLog / Reconciliation /
  Controls / RiskWidget components.

## Critical Notes
- LIVE MARKET DATA FEED (July 2026): migrated from per-second REST `ltpData` polling to
  **SmartWebSocketV2 streaming** (`angel_broker.start_feed`, daemon thread, LTP mode,
  exchangeType=2 NFO). Root cause of "no bricks": Angel One `position()` endpoint is capped
  at 1 req/sec and shares throttle budget; per-tick polling starved the feed. Now:
  * `get_ltp()` returns the streamed LTP (fresh < FEED_STALE_SEC=15s), else falls back to
    REST `_get_ltp_rest()` (which also auto-relogins + restarts the feed with new tokens).
  * jwt from generateSession must strip "Bearer " prefix; feedToken captured at login.
  * on_data LTP is in paise → /100. Re-subscribes on expiry rollover (`_ensure_subscription`).
  * status() exposes `streaming` + `feed_ltp`. UI badge shows "Streaming" when live.
  * Verified in preview: feed_ltp streamed live (24117→24108→24107). Could NOT e2e-test a
    RUNNING bot (would place a real order) — engine loop unchanged, still calls get_ltp().
- P&L polling: position() fetched every 20s and only when running/holding (well under 1/sec).
- Market-open safety reconcile: once/day, after brick-building, error-guarded.
- Alerts expire from /api/state after ALERT_TTL_SEC=20 (stop repeating toasts on mobile).
- LIVE REAL MONEY ONLY. Never place/modify/cancel orders or Start the bot without
  explicit user consent.
- Production env vars are managed separately from preview .env; code fixes need a
  redeploy to reach production.
