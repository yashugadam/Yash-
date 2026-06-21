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
- Renko brick construction (brick size 50) with live SVG chart + SHORT/COVER signal markers.
- Strategy state machine: short on 2 reds; exit on 1 green (≤4 reds) or 2 greens (>4 reds).
- SEBI-safe LIMIT order simulation: SELL=ref−buffer, BUY=ref+buffer; ~25% orders go to 5s RETRY then COMPLETE.
- Trade log, P&L (realized + unrealized), win-rate, win/loss metrics persisted in Mongo.
- Strategy settings panel (brick size, lot size, buffer, exit thresholds), bot Start/Stop/Reset.
- Angel One config form (stores key/client id, stays DEMO — no real orders).
- Tested: backend 8/8 pytest passed; frontend core flows verified.

## Backlog
- P1: Real Angel One SmartAPI integration (login, LTP feed, order placement) with a DEMO↔LIVE toggle.
- P1: Replace polling with WebSocket/SSE live stream.
- P2: Entry invalidation if a green brick prints before the SELL fills.
- P2: Multi-retry with max attempts + final REJECTED order state.
- P2: Split backend into engine.py / routes.py / models.py; add Pydantic Trade model.
- P2: Historical analytics (equity curve, per-day P&L).

## Next Tasks
- Await user's Angel One API credentials, then integrate SmartAPI (keep DEMO default).
