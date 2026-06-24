# Renko Algo Trading Bot — PRD

## Original Problem Statement
Build an algo trading bot for NIFTY Futures that places orders using a Renko-chart strategy on the user's Angel One account (keys added later). Strategy: Renko brick size 50, 1-min timeframe, NIFTY futures (lot size 65), carry-forward positions.
- Entry: SHORT when 2 consecutive RED bricks form.
- Exit: if ≤4 reds in the down-run → exit (cover) on the FIRST green brick; if >4 reds → wait for 2 green bricks to exit.
- Must test with DEMO orders in real time (NO real Angel One orders yet).
- SEBI compliance: no market orders — place LIMIT orders with a price buffer so they fill; if not filled, re-check & re-place after 5 seconds.

## User Choices
- Simulated price feed (random walk), DEMO orders only, lot size 65, SEBI-safe limit orders with buffer + 5s retry.

## Architecture
- Backend: FastAPI + in-memory TradingEngine (async loop, 1s tick), MongoDB for trade log. Routes under `/api`.
- Frontend: React + Tailwind "Control Room" dashboard, polls `/api/state` (1s) and `/api/trades` (3s).
- Engine: simulated price → simple Renko builder (brick_size step) → strategy state machine → demo LIMIT order executor (buffer + 5s retry).

## Implemented (2026-06-21)
- Simulated NIFTY futures price feed (momentum random walk + mean reversion).
- Renko brick construction — **Traditional Renko (TradingView-style)**: 1× continuation, 2× reversal, close-based on each bar (default **60s = 1-min**).
- Strategy state machine: short on 2 reds; exit on 1 green (≤4 reds) or 2 greens (>4 reds).
- SEBI-safe LIMIT order simulation: SELL=ref−buffer, BUY=ref+buffer; ~25% orders go to 5s RETRY then COMPLETE.
- **Crash/restart recovery**: engine state (position, bricks, anchor, counters) persisted to Mongo `engine_state` and restored on startup. In-flight order flags cleared (LIVE: must reconcile with broker positions).
- **Duplicate-order protection**: single async order lock + in-flight state re-validation drops stale/duplicate triggers.
- **Auto square-off**: monthly expiry = last Thursday; auto-exits open position at 15:20 IST on expiry day and blocks new entries that day; carry-forward all other days. Manual `POST /api/bot/square-off` button too.
- Trade log, P&L (realized + unrealized), win-rate; expiry/square-off info card; editable strategy + square-off settings.
- Angel One config form (stores key/client id, stays DEMO — no real orders).
- Tested: crash recovery, manual square-off (trade recorded w/ exit_reason), duplicate guard, expiry calc all verified.

## Backlog
- P1: Real Angel One SmartAPI integration (login, LTP feed, order placement) with DEMO↔LIVE toggle; reconcile recovered position against broker on startup.
- P1: Risk controls — daily max-loss circuit breaker, hard stop-loss, max-trades/day, overnight gap guard.
- P1: Replace polling with WebSocket/SSE live stream.
- P2: Partial-fill handling; multi-retry with max attempts + REJECTED state; market-hours/holiday calendar; brokerage/STT in P&L.
- P2: Split backend into engine.py / routes.py / models.py; Pydantic Trade model.
- P2: Historical analytics (equity curve, per-day P&L).

## Implemented (2026-06-24)
- **Stop-button confirmation**: clicking Stop now opens a confirmation dialog. On confirm, if a position is open it force-exits (square-off, MANUAL_SQUAREOFF, 25-pt forced slippage cap) and then halts; if flat it just stops. Backend `/api/bot/stop` accepts `{square_off: bool}` → returns `{running, squared_off}`. Prevents accidental halts that disable safety logic.
- **Aggressive re-entry**: entry condition changed from exactly 2 reds (`consec_red == 2`) to `consec_red >= 2`. If the bot (re)starts mid-downtrend with a run already >2 reds, it enters SHORT immediately on the next red instead of waiting for a green reset — so an in-progress downtrend isn't missed (user-requested). `down_run_reds` now seeds from the actual red run so the exit rule (>4 reds → 2 greens) stays correct.

## Implemented (2026-06-24) — Real Order Execution + Reconciliation
- **PAPER ↔ LIVE trade mode** (`engine.mode`, persisted): simple header toggle. LIVE places REAL Angel One orders; guarded so LIVE only activates when broker connected + feed=LIVE, and live order placement only runs when `mode==LIVE AND feed_mode==LIVE AND connected` (SIM feed always uses the simulated paper-fill path → safe). Frontend shows a LIVE confirmation dialog; "Enable LIVE" is disabled with an inline reason until broker connected + feed LIVE.
- **Real order placement** (`angel_broker.py`): `place_limit_order` (variety NORMAL, NFO, LIMIT, **CARRYFORWARD/NRML**, DAY), `modify_order_price` (re-price working order to escalate buffer), `get_order_status` (orderBook lookup), `cancel_order`, `get_net_position` (position() netqty for selected token). Engine `_live_fill` mirrors the paper escalating-buffer/5s-retry logic against a single broker order id (modify on retry to avoid double-fill); unfilled orders are cancelled. `_paper_fill` extracted for SIM/paper.
- **Broker reconciliation on restart** (`/api/bot/reconcile`, `/api/bot/reconcile/resolve`): compares bot position vs Angel One net qty → 3 states: GOOD ("everything is good"), ENTRY_MISSED ("short trade missed" → "take trade again"/reenter), EXIT_MISSED ("exit missed" → "exit trade again"/reexit). UI "Broker Reconciliation" widget with Check Angel One button + action buttons.
- **Tested** (iteration_2.json): 11/12 backend passed (1 skipped — random-walk signal timing), all frontend flows passed. SAFETY: real-money LIVE+LIVE order fill path is implemented but NOT validated against a real exchange fill (would place real orders; market closed). Email alerts for missed fills: SKIPPED per user.

## Implemented (2026-06-24) — Immediate entry on Start
- **Enter-on-Start**: clicking Start now checks the current brick run; if flat and already in a **2+ red down-run** (and entries not blocked), it places a SHORT **immediately at market** (`reason=START_IMMEDIATE`) instead of waiting for the next brick to print. Works in PAPER (simulated) and LIVE (real order). Implemented as `_maybe_enter_on_start()` fired from `/api/bot/start`. Verified via curl: starting at consec_red=3 opened a SHORT instantly.

## Next Tasks
- Await user's Angel One API credentials, then integrate SmartAPI (keep DEMO default).
