from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import asyncio
import random
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("renko-bot")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ----------------------------- Trading Engine -----------------------------
class TradingEngine:
    def __init__(self, database):
        self.db = database
        # settings
        self.settings = {
            "symbol": "NIFTY FUT",
            "brick_size": 50,
            "timeframe": "1m",
            "bar_seconds": 60,            # bar length: brick is checked on each bar CLOSE (TradingView style, true 1-min)
            "lot_size": 65,
            "buffer_points": 5,            # SEBI-safe limit buffer (no market orders)
            "max_red_single_green": 4,     # > this reds => need 2 greens to exit
            "greens_to_exit_extended": 2,
            "tick_interval": 1.0,
        }
        # runtime
        self.running = False
        self.mode = "DEMO"  # DEMO only for now
        self.angel = {"connected": False, "client_id": "", "api_key": ""}

        # price / renko
        self.start_price = 24500.0
        self.price = self.start_price
        self.prev_price = self.start_price
        self.momentum = 0.0
        self.anchor = None            # close level of last brick (Traditional Renko)
        self.direction = 0            # +1 up, -1 down, 0 none
        self.ticks_in_bar = 0         # ticks accumulated in the current bar
        self.bricks: List[Dict[str, Any]] = []
        self.brick_seq = 0

        # strategy state
        self.consec_red = 0
        self.consec_green = 0
        self.down_run_reds = 0
        self.position: Optional[Dict[str, Any]] = None
        self.pending_entry = False
        self.pending_exit = False

        # books
        self.orders: List[Dict[str, Any]] = []
        self.metrics = {"realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}

    # -------- price simulation --------
    def _gen_price(self):
        self.prev_price = self.price
        self.momentum = self.momentum * 0.85 + random.gauss(0, 1) * 6.0
        # gentle mean reversion to keep price in a realistic band
        self.momentum -= (self.price - self.start_price) * 0.0006
        self.price += self.momentum + random.gauss(0, 2.5)

    # -------- renko construction (Traditional Renko, close-based, like TradingView) --------
    # A brick is evaluated only on each bar CLOSE. Continuation needs a 1x brick move;
    # a REVERSAL needs a 2x brick move (the first opposite brick prints only after 2 boxes).
    def _feed_close(self, price):
        bs = self.settings["brick_size"]
        formed = []
        if self.anchor is None:
            self.anchor = round(price / bs) * bs
            self.direction = 0
            return formed
        n = int((price - self.anchor) / bs)   # signed number of full bricks from last close
        if n == 0:
            return formed
        s = 1 if n > 0 else -1
        if self.direction == 0 or s == self.direction:
            count = abs(n)                    # continuation: 1x brick each
        else:
            if abs(n) < 2:                    # reversal needs 2x brick move
                return formed
            count = abs(n) - 1                # first box consumed crossing back over last brick
            self.anchor += s * bs             # reversal "gap" brick (not drawn)
        for _ in range(count):
            o = self.anchor
            c = self.anchor + s * bs
            self.anchor = c
            self.direction = s
            formed.append(self._new_brick("green" if s > 0 else "red", o, c))
        return formed

    def _new_brick(self, color, o, c):
        self.brick_seq += 1
        b = {"index": self.brick_seq, "color": color, "open": round(o, 2),
             "close": round(c, 2), "time": now_iso(), "signal": None}
        self.bricks.append(b)
        if len(self.bricks) > 500:
            self.bricks = self.bricks[-500:]
        return b

    # -------- strategy --------
    def _process_brick(self, brick):
        if brick["color"] == "red":
            self.consec_red += 1
            self.consec_green = 0
            if self.position or self.pending_entry:
                self.down_run_reds += 1
            elif self.consec_red == 2:
                self.down_run_reds = 2
                self.pending_entry = True
                brick["signal"] = "SHORT"
                asyncio.create_task(self._execute_order("SELL", "ENTRY", self.price, brick["index"]))
        else:  # green
            self.consec_green += 1
            self.consec_red = 0
            if self.position and not self.pending_exit:
                need = self.settings["greens_to_exit_extended"] \
                    if self.down_run_reds > self.settings["max_red_single_green"] else 1
                if self.consec_green >= need:
                    self.pending_exit = True
                    brick["signal"] = "COVER"
                    asyncio.create_task(self._execute_order("BUY", "EXIT", self.price, brick["index"]))

    # -------- order execution (demo, SEBI-safe limit + 5s retry) --------
    async def _execute_order(self, side, kind, ref_price, brick_index):
        bs_buffer = self.settings["buffer_points"]
        limit = ref_price - bs_buffer if side == "SELL" else ref_price + bs_buffer
        order = {
            "id": str(uuid.uuid4()), "side": side, "kind": kind,
            "qty": self.settings["lot_size"], "symbol": self.settings["symbol"],
            "order_type": "LIMIT", "ref_price": round(ref_price, 2),
            "limit_price": round(limit, 2), "status": "PENDING", "attempts": 1,
            "brick_index": brick_index, "time": now_iso(), "fill_price": None,
            "note": "Limit order (SEBI compliant - no market order)",
        }
        self.orders.insert(0, order)
        self.orders = self.orders[:60]

        await asyncio.sleep(0.5)  # simulate broker round-trip
        filled = random.random() < 0.75
        if not filled:
            # Not filled -> re-check & re-place after 5 seconds with a wider buffer
            order["status"] = "RETRYING"
            order["note"] = "Not filled in 5s - re-checking & re-placing order"
            await asyncio.sleep(5)
            order["attempts"] = 2
            wider = bs_buffer * 2
            limit = ref_price - wider if side == "SELL" else ref_price + wider
            order["limit_price"] = round(limit, 2)

        order["status"] = "COMPLETE"
        order["fill_price"] = order["limit_price"]
        order["fill_time"] = now_iso()
        order["note"] = "Order filled (demo)"
        self._apply_fill(order)

    def _apply_fill(self, order):
        if order["kind"] == "ENTRY":
            self.position = {
                "side": "SHORT", "qty": order["qty"], "entry_price": order["fill_price"],
                "entry_time": order["fill_time"], "entry_order_id": order["id"],
                "reds_at_entry": self.down_run_reds, "unrealized_pnl": 0.0,
            }
            self.pending_entry = False
        else:  # EXIT
            if self.position:
                entry = self.position["entry_price"]
                exit_p = order["fill_price"]
                qty = order["qty"]
                pnl = round((entry - exit_p) * qty, 2)  # short
                trade = {
                    "id": str(uuid.uuid4()), "side": "SHORT", "qty": qty,
                    "entry_price": entry, "exit_price": exit_p,
                    "entry_time": self.position["entry_time"], "exit_time": order["fill_time"],
                    "pnl": pnl, "reds": self.down_run_reds, "symbol": self.settings["symbol"],
                }
                asyncio.create_task(self._save_trade(trade))
                self.metrics["realized_pnl"] = round(self.metrics["realized_pnl"] + pnl, 2)
                self.metrics["trades"] += 1
                if pnl >= 0:
                    self.metrics["wins"] += 1
                else:
                    self.metrics["losses"] += 1
            self.position = None
            self.pending_exit = False
            self.down_run_reds = 0

    async def _save_trade(self, trade):
        await self.db.trades.insert_one({**trade})

    def _update_unrealized(self):
        if self.position:
            self.position["unrealized_pnl"] = round(
                (self.position["entry_price"] - self.price) * self.position["qty"], 2)

    # -------- main loop --------
    # Ticks accumulate into a bar; the Renko bricks are evaluated ONLY on bar close
    # (every bar_seconds), using that bar's close price - just like TradingView 1m Renko.
    async def run_loop(self):
        while True:
            try:
                if self.running:
                    self._gen_price()
                    self.ticks_in_bar += 1
                    self._update_unrealized()
                    if self.ticks_in_bar >= self.settings["bar_seconds"]:
                        self.ticks_in_bar = 0
                        for b in self._feed_close(self.price):   # feed the BAR CLOSE
                            self._process_brick(b)
            except Exception as e:
                logger.exception("engine tick error: %s", e)
            await asyncio.sleep(self.settings["tick_interval"])

    async def load_metrics(self):
        cur = self.db.trades.find({}, {"_id": 0})
        realized = wins = losses = trades = 0
        async for t in cur:
            trades += 1
            realized += t.get("pnl", 0)
            if t.get("pnl", 0) >= 0:
                wins += 1
            else:
                losses += 1
        self.metrics = {"realized_pnl": round(realized, 2), "trades": trades,
                        "wins": wins, "losses": losses}

    def reset(self):
        self.running = False
        self.price = self.prev_price = self.start_price
        self.momentum = 0.0
        self.anchor = None
        self.direction = 0
        self.ticks_in_bar = 0
        self.bricks = []
        self.brick_seq = 0
        self.consec_red = self.consec_green = self.down_run_reds = 0
        self.position = None
        self.pending_entry = self.pending_exit = False
        self.orders = []

    def snapshot(self):
        m = self.metrics
        total = m["trades"]
        win_rate = round((m["wins"] / total) * 100, 1) if total else 0.0
        return {
            "running": self.running,
            "mode": self.mode,
            "angel": self.angel,
            "price": round(self.price, 2),
            "prev_price": round(self.prev_price, 2),
            "settings": self.settings,
            "bricks": self.bricks[-60:],
            "position": self.position,
            "pending_entry": self.pending_entry,
            "pending_exit": self.pending_exit,
            "consec_red": self.consec_red,
            "consec_green": self.consec_green,
            "down_run_reds": self.down_run_reds,
            "direction": self.direction,
            "ticks_in_bar": self.ticks_in_bar,
            "orders": self.orders[:12],
            "metrics": {**m, "win_rate": win_rate,
                        "unrealized_pnl": self.position["unrealized_pnl"] if self.position else 0.0},
        }


engine = TradingEngine(db)


# ----------------------------- API -----------------------------
class SettingsUpdate(BaseModel):
    brick_size: Optional[int] = None
    bar_seconds: Optional[int] = None
    lot_size: Optional[int] = None
    buffer_points: Optional[float] = None
    max_red_single_green: Optional[int] = None
    greens_to_exit_extended: Optional[int] = None


class AngelConfig(BaseModel):
    api_key: str = ""
    client_id: str = ""


@api_router.get("/")
async def root():
    return {"message": "Renko Algo Trading Bot API"}


@api_router.get("/state")
async def get_state():
    return engine.snapshot()


@api_router.post("/bot/start")
async def start_bot():
    engine.running = True
    return {"running": True}


@api_router.post("/bot/stop")
async def stop_bot():
    engine.running = False
    return {"running": False}


@api_router.post("/bot/reset")
async def reset_bot():
    engine.reset()
    await db.trades.delete_many({})
    await engine.load_metrics()
    return {"ok": True}


@api_router.get("/trades")
async def get_trades():
    trades = await db.trades.find({}, {"_id": 0}).sort("exit_time", -1).to_list(500)
    return trades


@api_router.post("/settings")
async def update_settings(body: SettingsUpdate):
    data = body.model_dump(exclude_none=True)
    engine.settings.update(data)
    return engine.settings


@api_router.post("/angel/config")
async def angel_config(body: AngelConfig):
    # Stored for later use. Bot stays in DEMO mode (no real orders placed).
    engine.angel = {"connected": False, "client_id": body.client_id, "api_key": body.api_key}
    return {"ok": True, "mode": "DEMO", "message": "Saved. Bot remains in DEMO mode - no real orders."}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await engine.load_metrics()
    asyncio.create_task(engine.run_loop())
    logger.info("Trading engine started")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
