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
- Manual trade adoption (June 2026): the every-2-min running-mode auto-polling was
  REMOVED at user request (they rarely place manual trades). Adoption now happens
  ONLY when user clicks broker reconciliation ("Check Angel One" → "Sync to broker"),
  which adopts any open Angel One position into the bot (reconcile_resolve "accept").
  On-Start reconcile/adopt (`_on_start`) still active.
- LIVE REAL MONEY ONLY. Never place/modify/cancel orders or Start the bot without
  explicit user consent.
- Production env vars are managed separately from preview .env; code fixes need a
  redeploy to reach production.
