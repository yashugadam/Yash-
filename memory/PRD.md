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
- BACKTEST (July 2026): simulation-only, NO orders. `engine.backtest(from_date,to_date,
  brick_size|brick_sizes,days,source)` + `engine._simulate(candles,bs,...)`. source='index'
  uses NIFTY 50 index (token 99926000, exch NSE) for CONTINUOUS multi-year 1-min history
  (up to 760 days); source='future' uses the selected contract (limited lifespan). Fetches
  candles once (paginate 25-day chunks, ≤70 calls) then simulates each brick size; sweep
  returns per-brick comparison + best_brick_size + best trades/equity. Fills = brick close
  (excludes slippage/brokerage). Runs as a BACKGROUND task on the leader (never blocks the
  trading loop). Submit+poll flow: `POST /api/backtest` -> {job_id}; `GET /api/backtest/
  result/{job_id}` -> running|done. UI: `Backtest.js` (source toggle, date range, brick-size
  list, sweep table, summary, SVG equity curve, trades). Verified: 2-yr NIFTY index sweep
  [30,40,50] = 184,623 candles, best brick 30 (net ~Rs12.3L, PF 2.66, 459 trades) — costs
  excluded; brick 30 trades ~2x more so real slippage/brokerage matters.
- AUTHENTICATION (July 2026, SEC-001 FIXED): single-user JWT login. Creds in backend `.env`
  (`AUTH_USERNAME`, `AUTH_PASSWORD`, `JWT_SECRET`); seeded to Mongo `auth_user` (`_id: singleton`,
  bcrypt hash) on startup. `POST /api/auth/login` -> `{token}` (HS256, 12h); `GET /api/auth/me`.
  Middleware `auth_gate` requires `Authorization: Bearer` on EVERY /api route except public
  `/api/auth/login` + `/api/keepalive`. Frontend `Login.js` + `App.js` gate (token in
  localStorage, axios default header, 401->logout), logout button in Dashboard header.
  `/api/keepalive` stripped to `{ok, server_time}` (public, no sensitive data).
  Login user in /app/memory/test_credentials.md.
- MULTI-POD SAFE ARCHITECTURE (July 2026): Emergent production runs MULTIPLE backend
  pods (support confirmed single-pod is NOT supported). The bot was re-architected so
  only ONE pod trades:
  * **Leader election** via MongoDB `leader_lock` (`_id: trading_engine`, atomic
    find_one_and_update lease, LEADER_LEASE_SEC=15, renewed each loop tick). Each process
    has a module-level `INSTANCE_ID`. Only the leader: connects Angel One, runs the strategy
    loop, and places ANY order. Followers idle (no broker session, no trading) → NO duplicate
    real orders even across pods/restarts.
  * **Single source of truth**: leader writes full snapshot to `db.live_state` every tick;
    ALL pods answer `/api/state` by reading it (kills the 15000↔10000 & Start/Stop flicker).
    `/state` also returns `is_leader`, `leader_id`, `state_age_sec`.
  * **Command relay**: mutating/broker endpoints (start, stop, settings, adopt, reconcile,
    reconcile_resolve, manual_order, connect, disconnect, load_history, instruments,
    select_instrument, reset, square_off, arm, clear_order_log) enqueue a doc in `db.commands`;
    the leader executes it sequentially (no order interleaving) and writes back the result,
    which `_relay()` polls (~0.2s) and returns. Adds ~1-2s to actions. `/trades` & `/orders/log`
    stay direct DB reads.
  * Boot auto-connect REMOVED; leader connects on `_on_become_leader` (also `_load_state`);
    `_on_lose_leadership` logs out so only one Angel session exists (fixes invalid-token/
    rate-limit from multiple sessions).
  * UI: top-bar red "Trading pod idle" badge shows when `state_age_sec > 30` (no active leader).
  * ORDER IDEMPOTENCY (July 2026): `_execute_order` + `manual_order` persist a deterministic
    client order id to `db.order_keys` (unique `_id`) BEFORE the broker call; if it already
    exists the duplicate is suppressed. Brick-triggered signals key on date+brick index (fire
    once); retries/forced/manual use short time-buckets (8s/5s) so genuine sequential retries
    still go through. TTL index expires keys after 2 days. Closes the leader-failover window.
  * KEEP-ALIVE: `GET /api/keepalive` (cheap) is pinged by an external scheduler (user's Azure
    VM cron, every ~30s) so a pod stays warm and the leader loop keeps trading even with NO
    browser open (user is on mobile, can't keep a tab open). Returns leader/state_age/running.
  * VERIFIED (single-pod preview): leadership acquired, /state consistent (age 0), relay works
    for settings/reconcile/instruments. NOT tested: start/manual_order/square_off (real money).
    Full multi-pod validation happens on production after redeploy.
- ⚠️ IDLE-POD RISK: if the pod sleeps with zero traffic, the leader loop stops (no trading,
  no exit monitoring). Mitigation: keep the dashboard tab open during market hours (polls
  /state every 1s = keep-alive), OR run an external keep-alive (Azure VM cron hitting
  /api/state every ~30s during 09:15-15:30 IST). RECOMMENDED FOLLOW-UP: order idempotency
  keys (persist client-order-id before broker call) to close the narrow leader-failover window.
- LIVE MARKET DATA FEED: SmartWebSocketV2 streaming (leader only); get_ltp falls back to REST.
- LIVE REAL MONEY ONLY. Never place/modify/cancel orders or Start the bot without
  explicit user consent.
- Production env vars are managed separately from preview .env; code fixes need a
  redeploy to reach production.


## Changelog
- 2026-07-06 — CODE REFACTOR (behavior-preserving, no logic change):
  * Backend: `server.py` (1912 lines) split into modules — `config.py` (constants/logging/IST),
    `db.py` (Mongo client), `security.py` (JWT + password + seed), `utils.py` (now_iso/expiry),
    `engine.py` (TradingEngine + `engine` singleton), `routes.py` (API router + `_relay`).
    `server.py` is now a 58-line entrypoint (app, CORS, auth-gate middleware, startup/shutdown)
    and re-exports `engine/TradingEngine/db/app/IST/MAX_EXIT_RETRIES/EXIT_RETRY_MIN_GAP` so the
    existing `tests/` import paths keep working. Only test change: `test_market_freeze.py` now
    patches `engine.datetime` (function moved out of `server`).
  * Frontend: `Dashboard.js` (831 lines) reduced to a 273-line container. Extracted 13 panel
    components + shared `Widget` under `src/components/dashboard/`, and `fmt/pnlClass/sign`
    into `src/lib/format.js`. All data-testids preserved; UI renders identically.
  * Verification: backend unit tests at exact baseline parity (66 passed / 74 failed — the 74
    are pre-existing failures from API tests that don't send a JWT / need a live broker, NOT
    caused by this refactor); end-to-end curl (login → auth/me → /state) OK; frontend compiles
    and full dashboard renders with all panels.
  * Fixed `.gitignore` blocking `.env` (deployment blocker) so production redeploy can inject
    env values.
- FOLLOW-UP: `engine.py` (1498 lines, the TradingEngine class) is cohesive but could be split
  further (renko/strategy vs. leadership vs. backtest vs. reconciliation) in a later pass.
