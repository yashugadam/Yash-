# Renko Nifty Bot — PRD

## Original Problem Statement
Algo trading bot that places real orders on the user's Angel One account using a
Renko strategy. Brick size 50, 1-minute timeframe, NIFTY Future lot (qty 65),
carry-forward. **Symmetric long+short strategy (updated 2026-07-10):**
- SHORT: enter on 2 red bricks; exit (BUY/cover) on the 1st green brick.
- LONG: enter on 2 green bricks; exit (SELL) on the 1st red brick.
- Exit is always the FIRST opposite brick regardless of run length (simplified from the
  earlier ≤4 / >4 rule to capture moves earlier).
- One position at a time; flips to the opposite side immediately when the opposite
  2-brick entry condition is met after an exit.
(Originally short-only; changed to symmetric, then simplified exit to 1st-opposite-brick.)

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

- 2026-07-10 — SYMMETRIC LONG+SHORT STRATEGY (real-money engine change, live engine + recon only;
  backtest module intentionally left short-only for now):
  * `engine.py::_process_brick` rewritten: SHORT on 2 reds AND LONG on 2 greens; exit on 1st
    opposite brick when the trend-run ≤ max_red_single_green, else wait greens_to_exit_extended
    opposite bricks. Flips to the opposite side immediately after an exit when the 2-brick setup
    is met (never both sides at once).
  * `_apply_fill` (side + P&L sign), `_update_unrealized` (side-aware), `_force_exit`
    (BUY to cover short / SELL to close long) generalised to both directions.
  * `_maybe_enter_on_start` now replays bricks via new `_replay_position()` to catch up to the
    correct side on Start (both directions).
  * Reconciliation & adoption now support LONG too (`reconcile` bot_open, `reconcile_resolve`
    reenter/reexit/accept side-aware, `adopt_position` accepts LONG, `_on_start` /
    `_market_open_reconcile` surface any non-zero broker qty for adoption).
  * Expiry position-rollover re-opens the SAME side that was squared off (`_rollover_side`).
  * Frontend `OpenPositionPanel` is side-aware (green LONG / red SHORT); start-confirm and
    reconcile copy updated for both directions.
  * Tests: new `tests/test_symmetric_strategy.py` (11 passing, broker mocked — NO real orders);
    updated `test_adopt_on_start.py` LONG cases to the new adoptable behaviour. Existing
    in-process engine tests (adopt/reconcile) pass. NOTE: needs a PRODUCTION REDEPLOY to go live.
- 2026-07-14 — GAP FLIP + CODE-REVIEW FIXES (real-money engine):
  * Gap flip: after a strategy EXIT fills, if the market has ALREADY printed >=2 consecutive
    opposite bricks (gap up/down), the reversal is opened immediately (no waiting for a new
    brick). Fires only after the exit fill is confirmed, outside order_lock (no deadlock),
    excluded for forced exits (expiry/breaker/manual), and guarded by idempotency + entries-blocked.
  * FIX (HIGH): EXIT orders now size to the ACTUAL open position qty (`self.position["qty"]`),
    not hard-coded lot_size. Previously an adopted/reconciled/carry-forward position >1 lot
    (e.g. 130) was only covered 65, leaving a naked remainder while the bot thought it was flat —
    a later signal re-opened 65 -> back to 130. This is the likely root cause of the reported
    "broker net qty 130 vs 65". ENTRY orders remain exactly one lot. Capped at MAX_ORDER_QTY.
  * FIX (HIGH): `angel_broker.get_net_position` now SUMS netqty across all rows for the token,
    deduped by producttype (NRML/MIS/day/carry-forward), instead of returning only the first row;
    logs the raw rows once for diagnosis.
  * FIX (MEDIUM): idempotency order key now includes the contract token so a brick_seq reset
    after a rollover/instrument change cannot collide with an earlier same-day key (still
    deterministic across pods, so cross-pod dedup holds).
  * FIX (LOW): corrected the stale angel_broker.py header that wrongly said orders were PAPER/not
    wired — they are REAL CARRYFORWARD LIMIT orders.
  * Tests: `tests/test_symmetric_strategy.py` now 20 passing (added gap-flip x3, exit-sizing x2,
    get_net_position multi-row/dedup x3, order-key-token x1). adopt/reconcile/rollover in-process
    tests pass. Remaining failures are the pre-existing `test_api_*`/`TestSettingsAPI` 401 auth tests.
    Needs a PRODUCTION REDEPLOY to go live. No real orders placed in dev.

- 2026-07-14 — CODE-REVIEW #2 FIXES (real-money engine):
  * FIX (CRITICAL/P0): the auto exit-retry in `run_loop` hard-coded BUY. For a LONG whose exit
    was rejected, the retry placed a BUY -> DOUBLED the long. Now derives side from the open
    position (BUY covers short, SELL closes long), mirroring `_force_exit`.
  * FIX (HIGH): cancel/fill race in `_live_fill`. If an order filled in the moment before/at the
    final `cancel_order`, it was reported as REJECTED (bot/broker desync, could trigger a wrong
    extra order). Now re-queries `get_order_status` after a failed cancel and treats a completed
    order as FILLED.
  * Tests: added `test_long_exit_retry_uses_sell_not_buy`, `test_short_exit_retry_uses_buy`,
    `test_live_fill_detects_fill_in_cancel_window`. `test_symmetric_strategy.py` now 23 passing.
  * Verified-correct by review (no change): get_net_position sum/dedup, gap-flip re-entrancy,
    _order_key determinism, EXIT sizing, non-leader relay-only.
  * KNOWN FOLLOW-UP (open question, not a confirmed defect): leader failover mid-command leaves a
    command stuck in `processing`; relay times out and the user retries (self-healing, not silent
    for real-money). Consider reclaiming stale `processing` commands with a safe threshold later.
  * LOW (deferred): /api/auth/login has no rate limiting; CORS default '*' with credentials (app
    uses Bearer tokens, so low risk) — restrict via CORS_ORIGINS in production.
  * Needs PRODUCTION REDEPLOY to go live. No real orders placed in dev.


- 2026-07-15 — MACRO TREND FILTER (advanced whipsaw-reduction strategy):
  * Backtest (2yr NIFTY index proxy, ₹200/trade, 65 qty) confirmed a multi-timeframe Renko
    macro filter reduces whipsaws. bs50 baseline: 446 trades / 41.7% win / PF 2.51 / DD -48,100.
    bs50 + macro×2: 344 trades (-23%) / 45.3% win / PF 2.89 / DD -41,200 (chosen default).
  * ENGINE: added `_feed_macro_close()` building a larger Renko (brick_size × macro_mult) from the
    same closes; tracks `macro_dir` (+1 up / -1 down / 0 forming). `_process_brick` now gates
    entries: longs only when macro_dir>0, shorts only when macro_dir<0. macro_mult=0 => filter OFF
    (default). Setting change/toggle rebuilds macro trend from existing bricks (no cold-start).
    macro state persisted in engine_state and reset on rollover/select-instrument/reset.
  * SETTINGS: new `macro_mult` field (0-10) in SettingsUpdate + engine settings. /api/state exposes
    `macro_dir` and `macro_mult`.
  * UI: StrategySettingsPanel has a "Macro trend filter" toggle (on => macro_mult 2, off => 0), a
    macro-multiplier input, and a live macro-trend indicator (UP/DOWN/forming).
  * TESTS: 7 new macro tests; `test_symmetric_strategy.py` now 30 passing.
  * DEFAULT: filter OFF (manual toggle per user). Needs PRODUCTION REDEPLOY to go live.

- 2026-07-15 — ER "CHOP FILTER" ENTRY STRATEGY (whipsaw fix, macro dropped by user):
  * User dropped the macro filter; asked to filter false entries using price-action (range/high-low)
    and reported two pain points: (1) market reverses right after 2 bricks, (2) market range shrinking.
  * Ran 4 backtest matrices (2-yr NIFTY index, ₹200/trade, 50-pt bricks) covering: range-breakout,
    chop/efficiency filter, structure/direction filter, entry_bricks sweep (1/2/3/4), chop-threshold
    sweep, lookback sweep, and an adaptive ER-exit. Findings:
    - Chop filter (Kaufman Efficiency Ratio = |net move|/total path over N brick closes) is the best
      false-entry killer. Range-breakout HURT (win% dropped to 37.7%). Direction filter HURT.
    - Best config: 2 bricks + ER>=0.30, lookback 50 → 48.6% win, PF 3.46, DD -17K (vs baseline
      41.7%/2.51/-48K). 1-brick+ER = more trades/profit but higher DD. 3-brick = fewer trades, no gain.
    - ER threshold 0.30 is the sweet spot (0.40/0.50 collapse trades). Adaptive ER-EXIT does NOT beat
      the existing 1st-opposite-brick exit → EXIT LEFT UNCHANGED.
  * ENGINE: added _chop_ok() (ER over last chop_lookback brick closes from self.bricks). _process_brick
    entry now gated by entry_bricks (configurable, default 2) AND _chop_ok(). _replay_position /
    _maybe_enter_on_start honor entry_bricks + chop filter. New settings: chop_filter(True),
    chop_lookback(50), chop_threshold(0.30), entry_bricks(2). /api/state exposes chop_filter, chop_er,
    chop_threshold, entry_bricks. Backtest _simulate gained lookback/range_breakout/chop_filter/
    chop_thr/structure/exit_chop/exit_thr params (backtest-only).
  * ROUTES: SettingsUpdate + entry_bricks(1-10), chop_filter, chop_lookback(2-500), chop_threshold(0-1).
  * UI: StrategySettingsPanel — entry-bricks input, chop-filter toggle, ER threshold + lookback inputs,
    live ER indicator (trading / chop-blocked / warming up).
  * TESTS: 6 new tests (ER math, range block/allow, warming-up, entry_bricks=3). Suite now 36 passing.
    Testing agent iteration_18: 100% backend (6/6) + frontend, no bugs.
  * DEFAULT: chop_filter ON (this IS the chosen strategy). Needs PRODUCTION REDEPLOY to go live.

- 2026-07-15 — REMOVED MACRO TREND FILTER (user request): fully deleted from engine.py
  (settings macro_mult, _feed_macro_close, macro_anchor/macro_dir, entry gate, state persist/load,
  resets, settings-command rebuild, /api/state exposure, and backtest _simulate macro params),
  routes.py (SettingsUpdate.macro_mult), Dashboard.js (saveSettings), StrategySettingsPanel.jsx
  (macro toggle/indicator UI), tests (7 macro tests removed → 29 passing), and cleaned the stale
  macro_mult key from the persisted engine_state doc in Mongo. Active strategy is unchanged:
  2-brick entry + ER chop filter (lookback 50, threshold 0.30), exit on 1st opposite brick.

- 2026-07-15 — SKIP DIAGNOSTIC + ADAPTIVE ER + REGIME TUNING:
  * User asked "did the system skip trades?". Built POST /api/analyze/skips (engine.analyze_skips):
    replays recent real candles through the live strategy and reports TAKEN vs ER-SKIPPED signals
    with counterfactual outcomes. Diagnostic only, no orders.
  * FINDING: with strict lb50/t0.30, the ER filter took 0 trades over the last 2 months — NIFTY
    range-bound so ER never reached 0.30 over a 50-brick (~13 trading-day) window. lb50 is a
    "trend-only" switch that goes dormant in quiet regimes.
  * Added ADAPTIVE ER threshold to backtest _simulate (er_adaptive/adapt_window/adapt_pct): trades
    when current ER >= p-th percentile of its own recent ER distribution (regime-relative). Also
    added include_trades flag to the variant matrix to return full per-variant trade logs.
  * Regime comparison (recent 60d future vs 2yr index) produced. USER CHOSE **Fixed lb20 / t0.20**
    as the live config (recent 60d: 20 trades, 40% win, PF 1.92, +Rs38,250, DD -16,850; 2yr: 295
    trades, PF 2.62, +Rs11.2L, DD -48,900). Applied live in PREVIEW via /api/settings:
    chop_filter=true, chop_lookback=20, chop_threshold=0.20, entry_bricks=2.
  * NOTE: engine default is still lb50/0.30 — production must set lb20/0.20 in Settings after redeploy.
  * Loaded last 60 days (194 bricks) into the live chart via /api/angel/load-history.

- 2026-07-15 — CODE REVIEW FIX + CHOP FILTER MANDATORY:
  * Code review (READY WITH FIXES): confirmed ER filter/entry gating/exit sizing/side-aware retries/
    idempotency/leader-election/macro-removal all correct. Fixed 1 MEDIUM: gap-flip re-entry
    (engine.py ~829) bypassed the ER chop filter + entry_bricks — now requires _chop_ok() and uses
    configurable entry_bricks. Added tests test_gap_flip_blocked_by_chop_filter + _honors_entry_bricks.
  * User requested the chop filter be NON-DISABLEABLE (safety). Made it mandatory:
    - _chop_ok() clamps threshold to min 0.05 (threshold 0 can no longer disable it).
    - _load_state() and settings command force self.settings["chop_filter"]=True.
    - routes SettingsUpdate: removed chop_filter field; chop_threshold now ge=0.05 (rejects 0 w/ 422).
    - Dashboard saveSettings: removed chop_filter. UI: removed toggle, shows "ALWAYS ON" badge;
      ER threshold + lookback + live ER indicator always visible.
  * Live config remains lb20 / t0.20 / entry_bricks 2 (preview). 31/31 unit tests pass.
  * NOTE: engine default chop_threshold still 0.30, chop_lookback 50 — production must set 0.20/20.

- 2026-07-15 — CODE REVIEW FIXES (bugs found + fixed):
  * HIGH: Leader failover could strand a position / skip expiry square-off — pending flags weren't
    persisted and no broker reconcile on takeover. FIX: _state_doc now persists exit_retry_pending,
    forced_exit_pending, rollover_armed/side, exit_retry_count, pending_adoption; _load_state
    restores them; new _reconcile_on_takeover() (called in _on_become_leader) reads Angel One net
    position and clears/adopts/re-arms exit accordingly.
  * HIGH: Non-variant /backtest ignored the ER filter -> misrepresented the live strategy. FIX:
    plain backtest now applies chop_filter with live lookback/threshold/entry_bricks. Verified: 60d
    future now returns 20 trades / Rs38,250 (matches live) instead of the unfiltered 38-trade result.
  * MEDIUM: load_history/analyze_skips ran inline (could pause the trading loop) and load_history
    reset bricks under an open position. FIX: both now run as background tasks (_run_command_bg);
    load_history uses a scratch-pass rebuild that never touches position/consec state when a
    position/pending exit is open (returns a distinct note).
  * Verified: 31/31 unit tests pass; /analyze/skips, /angel/load-history, /backtest, /state all OK.
  * DEFERRED (refactor, not a bug): Renko brick-building is duplicated across _feed_close/_simulate/
    analyze_skips/load_history — candidate for a shared helper, but risky on the live money path
    without brick-parity tests; proposed to user, not done.
