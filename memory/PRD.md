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

## Next Tasks
- Await user's Angel One API credentials, then integrate SmartAPI (keep DEMO default).
