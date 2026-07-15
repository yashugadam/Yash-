"""FastAPI routes: auth, bot control, orders, settings, Angel One, backtesting.
Follower pods relay user actions to the leader pod via a MongoDB command queue."""
import asyncio
import time
import uuid
import hmac
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from config import MAX_ORDER_QTY
from db import db
from utils import now_iso
from engine import engine
from security import _verify_password, _create_token, _decode_token, _bearer_token

api_router = APIRouter(prefix="/api")


async def _relay(ctype, payload=None, timeout=12.0):
    """Enqueue a user action for the leader pod to execute, then wait for its result.
    This is how a read-only (follower) pod performs broker/trading actions safely."""
    cmd_id = str(uuid.uuid4())
    await db.commands.insert_one({
        "_id": cmd_id, "type": ctype, "payload": payload or {},
        "status": "pending", "created_at": now_iso(), "created_ts": time.time(),
    })
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = await db.commands.find_one({"_id": cmd_id})
        if doc and doc.get("status") == "done":
            await db.commands.delete_one({"_id": cmd_id})
            return doc.get("result")
        await asyncio.sleep(0.2)
    await db.commands.delete_one({"_id": cmd_id})
    return {"ok": False, "message": "No active trading pod handled this in time — please retry."}


# ----------------------------- API -----------------------------
class SettingsUpdate(BaseModel):
    brick_size: Optional[int] = Field(None, ge=1, le=2000)
    entry_bricks: Optional[int] = Field(None, ge=1, le=10)
    chop_filter: Optional[bool] = None
    chop_lookback: Optional[int] = Field(None, ge=2, le=500)
    chop_threshold: Optional[float] = Field(None, ge=0, le=1)
    macro_mult: Optional[int] = Field(None, ge=0, le=10)
    bar_seconds: Optional[int] = Field(None, ge=1, le=3600)
    lot_size: Optional[int] = Field(None, ge=1, le=MAX_ORDER_QTY)
    buffer_points: Optional[float] = Field(None, ge=0, le=2000)
    max_slippage: Optional[float] = Field(None, ge=0, le=2000)
    forced_exit_slippage: Optional[float] = Field(None, ge=0, le=3000)
    retry_seconds: Optional[float] = Field(None, ge=0, le=600)
    max_order_attempts: Optional[int] = Field(None, ge=1, le=20)
    max_red_single_green: Optional[int] = Field(None, ge=1, le=50)
    greens_to_exit_extended: Optional[int] = Field(None, ge=1, le=50)
    square_off_time: Optional[str] = None
    auto_square_off: Optional[bool] = None
    auto_roll: Optional[bool] = None
    rollover_position: Optional[bool] = None
    daily_max_loss: Optional[float] = Field(None, ge=0, le=100_000_000)
    circuit_breaker_enabled: Optional[bool] = None


class AngelConfig(BaseModel):
    api_key: str = ""
    client_id: str = ""


@api_router.get("/")
async def root():
    return {"message": "Renko Algo Trading Bot API"}


class LoginRequest(BaseModel):
    username: str
    password: str


@api_router.post("/auth/login")
async def auth_login(body: LoginRequest):
    user = await db.auth_user.find_one({"_id": "singleton"})
    ok = bool(user) and hmac.compare_digest((body.username or "").strip(), user.get("username", "")) \
        and _verify_password(body.password or "", user.get("password_hash", ""))
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"token": _create_token(user["username"]), "username": user["username"]}


@api_router.get("/auth/me")
async def auth_me(request: Request):
    token = _bearer_token(request)
    payload = _decode_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": payload.get("sub")}


@api_router.get("/keepalive")
async def keepalive():
    """PUBLIC, minimal endpoint for an external scheduler to ping (~1/min) so a pod stays
    warm and the leader loop keeps trading even with no browser open. Intentionally returns
    NO sensitive data (it's unauthenticated)."""
    return {"ok": True, "server_time": now_iso()}


@api_router.get("/state")
async def get_state():
    # Every pod serves the SAME state by reading the leader's published snapshot.
    doc = await db.live_state.find_one({"_id": "singleton"})
    if doc and doc.get("snapshot"):
        snap = doc["snapshot"]
        snap["leader_id"] = doc.get("leader")
        snap["state_age_sec"] = round(time.time() - doc.get("ts", 0), 1)
        return snap
    return engine.snapshot()   # fallback before any leader has published


@api_router.post("/bot/start")
async def start_bot():
    return await _relay("start")


class AdoptRequest(BaseModel):
    confirm: bool = False


@api_router.post("/bot/adopt")
async def adopt_position(req: AdoptRequest):
    return await _relay("adopt", {"confirm": req.confirm})


class StopRequest(BaseModel):
    square_off: Optional[bool] = False


@api_router.post("/bot/stop")
async def stop_bot(req: StopRequest = StopRequest()):
    return await _relay("stop", {"square_off": bool(req.square_off)})


@api_router.post("/bot/reset")
async def reset_bot():
    return await _relay("reset")


@api_router.post("/bot/square-off")
async def square_off():
    return await _relay("square_off")


@api_router.post("/bot/trade-mode")
async def set_trade_mode(body: dict):
    # LIVE-only app: mode is always LIVE (real money). Kept for backward compatibility.
    return {"ok": True, "mode": "LIVE"}


@api_router.get("/bot/reconcile")
async def get_reconcile():
    return await _relay("reconcile")


@api_router.post("/bot/reconcile/resolve")
async def post_reconcile_resolve(body: dict):
    return await _relay("reconcile_resolve", {"action": (body.get("action") or "").lower()})


@api_router.post("/bot/arm")
async def arm_breaker():
    return await _relay("arm")


@api_router.get("/trades")
async def get_trades():
    trades = await db.trades.find({}, {"_id": 0}).sort("exit_time", -1).to_list(500)
    return trades


@api_router.get("/orders/log")
async def get_order_log():
    return await db.order_log.find({}, {"_id": 0}).sort("time", -1).to_list(150)


@api_router.post("/orders/log/clear")
async def clear_order_log():
    return await _relay("clear_order_log")


@api_router.post("/orders/manual")
async def post_manual_order(body: dict):
    side = (body.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return {"ok": False, "message": "side must be BUY or SELL"}
    return await _relay("manual_order", {"side": side, "qty": body.get("qty")})


@api_router.post("/settings")
async def update_settings(body: SettingsUpdate):
    data = body.model_dump(exclude_none=True)
    return await _relay("settings", {"data": data})


@api_router.post("/angel/connect")
async def angel_connect():
    """Ask the leader pod to log in to Angel One using credentials from backend .env."""
    return await _relay("connect", timeout=25.0)


@api_router.post("/angel/disconnect")
async def angel_disconnect():
    return await _relay("disconnect")


@api_router.post("/angel/load-history")
async def angel_load_history(body: dict = None):
    body = body or {}
    days = max(1, min(int(body.get("days", 5)), 70))
    return await _relay("load_history", {"days": days, "from_date": body.get("from_date")}, timeout=45.0)


@api_router.post("/backtest")
async def run_backtest(body: dict = None):
    """Submit a backtest job (runs in the background on the leader). Returns a job_id to poll —
    avoids holding the HTTP connection open for long multi-year runs."""
    body = body or {}
    job_id = str(uuid.uuid4())
    await db.commands.insert_one({
        "_id": job_id, "type": "backtest", "status": "pending",
        "created_at": now_iso(), "created_ts": time.time(),
        "payload": {
            "from_date": body.get("from_date"), "to_date": body.get("to_date"),
            "brick_size": body.get("brick_size"), "brick_sizes": body.get("brick_sizes"),
            "source": body.get("source", "future"), "days": int(body.get("days", 30)),
            "entry_bricks": int(body.get("entry_bricks", 2)),
            "exit_bricks": int(body.get("exit_bricks", 1)),
            "cost_per_trade": float(body.get("cost_per_trade", 0) or 0),
            "trend_ema": int(body.get("trend_ema", 0) or 0),
            "variants": body.get("variants"),
        },
    })
    return {"ok": True, "job_id": job_id}


@api_router.get("/backtest/result/{job_id}")
async def backtest_result(job_id: str):
    doc = await db.commands.find_one({"_id": job_id})
    if not doc:
        return {"status": "not_found"}
    if doc.get("status") == "done":
        result = doc.get("result")
        await db.commands.delete_one({"_id": job_id})
        return {"status": "done", "result": result}
    return {"status": "running"}


@api_router.get("/angel/instruments")
async def angel_instruments(q: str = ""):
    return await _relay("instruments", {"q": q})


@api_router.post("/angel/select-instrument")
async def angel_select_instrument(body: dict):
    return await _relay("select_instrument", {"token": str(body.get("token", ""))})


@api_router.post("/feed/mode")
async def set_feed_mode(body: dict):
    # LIVE-only app: feed is always real Angel One data. Kept for backward compatibility.
    return {"ok": True, "feed_mode": "LIVE"}

