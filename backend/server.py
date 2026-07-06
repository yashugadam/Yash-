"""App entrypoint: wires the FastAPI app, CORS, JWT auth gate, and lifecycle hooks.
Trading logic lives in engine.py; HTTP routes in routes.py."""
import os
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from config import INSTANCE_ID, AUTH_PUBLIC_PATHS, logger, IST, MAX_EXIT_RETRIES, EXIT_RETRY_MIN_GAP
from db import client, db
from security import _bearer_token, _decode_token, _seed_auth_user
from engine import engine, TradingEngine
from routes import api_router

app = FastAPI()
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Require a valid JWT for every /api route except the public login + keepalive.
    Non-/api paths and CORS preflight (OPTIONS) pass through untouched."""
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api") or path in AUTH_PUBLIC_PATHS:
        return await call_next(request)
    token = _bearer_token(request)
    if not token or not _decode_token(token):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return await call_next(request)


@app.on_event("startup")
async def startup():
    await db.leader_lock.update_one(
        {"_id": "trading_engine"},
        {"$setOnInsert": {"holder": "", "expires_at": 0}}, upsert=True)
    try:
        await db.order_keys.create_index("created", expireAfterSeconds=172800)  # auto-expire keys after 2 days
    except Exception:
        pass
    await _seed_auth_user()
    await engine.load_metrics()
    await engine._load_state()   # crash/restart recovery
    asyncio.create_task(engine.run_loop())
    logger.info("Trading engine started (instance %s)", INSTANCE_ID)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
