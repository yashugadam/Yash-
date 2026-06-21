from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import asyncio
import random
import calendar
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timezone, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
from angel_broker import AngelBroker


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("renko-bot")

IST = ZoneInfo("Asia/Kolkata")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def last_thursday(year, month):
    # NIFTY futures expire on the last Thursday of the month.
    weeks = calendar.monthcalendar(year, month)
    thursdays = [w[calendar.THURSDAY] for w in weeks if w[calendar.THURSDAY] != 0]
    return date(year, month, thursdays[-1])


def next_expiry(d):
    exp = last_thursday(d.year, d.month)
    if d <= exp:
        return exp
    y = d.year + (1 if d.month == 12 else 0)
    m = 1 if d.month == 12 else d.month + 1
    return last_thursday(y, m)


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
            "square_off_time": "15:20",    # IST: auto square-off time on expiry day
            "auto_square_off": True,
            "daily_max_loss": 5000,        # ₹: auto-stop the bot if day P&L falls to -this
            "circuit_breaker_enabled": True,
        }
        # runtime
        self.running = False
        self.mode = "DEMO"  # order mode: DEMO/paper only (no real orders placed)
        self.feed_mode = "SIM"   # SIM (simulated) or LIVE (real Angel One LTP)
        self.feed_error = ""
        self.broker = AngelBroker()
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

        # safety: duplicate-order protection, expiry square-off, crash recovery
        self.order_lock = asyncio.Lock()
        self.squared_off_date: Optional[str] = None   # date (IST) we already squared off / blocked entries
        self.persist_counter = 0
        # risk: daily max-loss circuit breaker
        self.day_key: Optional[str] = None            # IST date the day P&L belongs to
        self.day_realized = 0.0                       # realized P&L booked today
        self.breaker_tripped = False

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
    def _feed_close(self, price, ts=None):
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
            formed.append(self._new_brick("green" if s > 0 else "red", o, c, ts))
        return formed

    # -------- historical backfill (real Angel One 1-min candles, paginated) --------
    async def load_history(self, days=5, from_date=None):
        if not self.broker.connected:
            return {"ok": False, "error": "Connect Angel One first."}
        now = datetime.now(IST)
        if from_date:
            try:
                start = datetime.strptime(from_date, "%Y-%m-%d").replace(
                    hour=9, minute=15, tzinfo=IST)
            except Exception:
                return {"ok": False, "error": "Bad from_date (use YYYY-MM-DD)"}
        else:
            start = (now - timedelta(days=days)).replace(hour=9, minute=15)
        # cap total span to ~70 days for sanity
        if (now - start).days > 70:
            start = now - timedelta(days=70)

        # Angel caps ONE_MINUTE at ~1500 candles/call (~4 trading days). Paginate in 4-day windows.
        all_candles = []
        seen = set()
        cur = start
        calls = 0
        while cur < now and calls < 40:
            chunk_end = min(cur + timedelta(days=4), now)
            from_dt = cur.strftime("%Y-%m-%d %H:%M")
            to_dt = chunk_end.strftime("%Y-%m-%d %H:%M")
            candles = await asyncio.to_thread(self.broker.get_history, "ONE_MINUTE", from_dt, to_dt)
            calls += 1
            if candles:
                for c in candles:
                    if c[0] not in seen:
                        seen.add(c[0])
                        all_candles.append(c)
            cur = chunk_end + timedelta(minutes=1)
            await asyncio.sleep(0.4)  # respect rate limit (3 req/sec)

        if not all_candles:
            return {"ok": False, "error": self.broker.error or "No historical candles returned"}
        all_candles.sort(key=lambda c: c[0])

        # rebuild the renko chart from real candle closes (no strategy/orders on history)
        self.anchor = None
        self.direction = 0
        self.bricks = []
        self.brick_seq = 0
        last_close = None
        for c in all_candles:
            close = float(c[4])
            last_close = close
            self._feed_close(close, c[0])
        if last_close is not None:
            self.price = self.prev_price = last_close
        self.ticks_in_bar = 0
        self.consec_red = self.consec_green = self.down_run_reds = 0
        if self.bricks:
            last_color = self.bricks[-1]["color"]
            run = 0
            for b in reversed(self.bricks):
                if b["color"] == last_color:
                    run += 1
                else:
                    break
            if last_color == "red":
                self.consec_red = run
            else:
                self.consec_green = run
        await self._persist_state()
        return {"ok": True, "candles": len(all_candles), "bricks": len(self.bricks),
                "from": start.strftime("%Y-%m-%d"), "to": now.strftime("%Y-%m-%d"),
                "symbol": self.broker.fut_symbol}

    def _new_brick(self, color, o, c, ts=None):
        self.brick_seq += 1
        b = {"index": self.brick_seq, "color": color, "open": round(o, 2),
             "close": round(c, 2), "time": ts or now_iso(), "signal": None}
        self.bricks.append(b)
        if len(self.bricks) > 1500:
            self.bricks = self.bricks[-1500:]
        return b

    # -------- strategy --------
    def _process_brick(self, brick):
        if brick["color"] == "red":
            self.consec_red += 1
            self.consec_green = 0
            if self.position or self.pending_entry:
                self.down_run_reds += 1
            elif self.consec_red == 2 and not self._entries_blocked():
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

    # -------- order execution (demo, SEBI-safe limit + 5s retry, duplicate-protected) --------
    async def _execute_order(self, side, kind, ref_price, brick_index, reason="SIGNAL"):
        # Duplicate-order protection: only one order may be in-flight at a time, and we
        # re-validate state inside the lock so a stale/duplicate trigger is dropped.
        async with self.order_lock:
            if kind == "ENTRY" and self.position is not None:
                logger.warning("Duplicate ENTRY dropped - position already open")
                self.pending_entry = False
                return
            if kind == "EXIT" and self.position is None:
                logger.warning("EXIT dropped - no open position")
                self.pending_exit = False
                return

            bs_buffer = self.settings["buffer_points"]
            limit = ref_price - bs_buffer if side == "SELL" else ref_price + bs_buffer
            order = {
                "id": str(uuid.uuid4()), "side": side, "kind": kind, "reason": reason,
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
        asyncio.create_task(self._persist_state())

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
                    "exit_reason": order.get("reason", "SIGNAL"),
                }
                asyncio.create_task(self._save_trade(trade))
                self.metrics["realized_pnl"] = round(self.metrics["realized_pnl"] + pnl, 2)
                self.metrics["trades"] += 1
                if pnl >= 0:
                    self.metrics["wins"] += 1
                else:
                    self.metrics["losses"] += 1
                # track today's realized P&L for the circuit breaker
                today = datetime.now(IST).date().isoformat()
                if self.day_key != today:
                    self.day_key = today
                    self.day_realized = 0.0
                    self.breaker_tripped = False
                self.day_realized = round(self.day_realized + pnl, 2)
            self.position = None
            self.pending_exit = False
            self.down_run_reds = 0

    async def _save_trade(self, trade):
        await self.db.trades.insert_one({**trade})

    def _update_unrealized(self):
        if self.position:
            self.position["unrealized_pnl"] = round(
                (self.position["entry_price"] - self.price) * self.position["qty"], 2)

    # -------- expiry / square-off --------
    def _expiry_status(self):
        ist = datetime.now(IST)
        today = ist.date()
        exp = next_expiry(today)
        is_today = (today == exp)
        try:
            hh, mm = map(int, str(self.settings["square_off_time"]).split(":"))
        except Exception:
            hh, mm = 15, 20
        past_cut = ist.time() >= dtime(hh, mm)
        return ist, today, exp, is_today, past_cut

    def _entries_blocked(self):
        # No new entries once we've hit the expiry-day square-off window or the circuit breaker.
        _, _, _, is_today, past_cut = self._expiry_status()
        return bool((is_today and past_cut) or self.breaker_tripped)

    # -------- risk: daily max-loss circuit breaker --------
    def _check_circuit_breaker(self):
        if not self.settings.get("circuit_breaker_enabled", True):
            return
        today = datetime.now(IST).date().isoformat()
        if self.day_key != today:          # new trading day -> reset day P&L + re-arm
            self.day_key = today
            self.day_realized = 0.0
            self.breaker_tripped = False
        if self.breaker_tripped:
            self.running = False           # stay halted for the rest of the day
            return
        unreal = self.position["unrealized_pnl"] if self.position else 0.0
        day_total = self.day_realized + unreal
        if day_total <= -abs(self.settings["daily_max_loss"]):
            self.breaker_tripped = True
            logger.warning("CIRCUIT BREAKER tripped: day P&L %.2f <= -%s", day_total,
                           self.settings["daily_max_loss"])
            if self.position and not self.pending_exit:
                self._force_exit("CIRCUIT_BREAKER")
            self.running = False
            asyncio.create_task(self._persist_state())

    def _maybe_square_off(self):
        if not self.settings.get("auto_square_off", True):
            return
        _, today, _, is_today, past_cut = self._expiry_status()
        if is_today and past_cut and self.squared_off_date != str(today):
            if self.position and not self.pending_exit:
                self.squared_off_date = str(today)
                logger.warning("EXPIRY square-off triggered at %s IST", self.settings["square_off_time"])
                self._force_exit("EXPIRY_SQUAREOFF")
            elif self.position is None and not self.pending_exit:
                self.squared_off_date = str(today)  # nothing to exit; just block new entries

    def _force_exit(self, reason):
        if not self.position or self.pending_exit:
            return
        self.pending_exit = True
        asyncio.create_task(self._execute_order("BUY", "EXIT", self.price, -1, reason))

    # -------- crash / restart recovery --------
    def _state_doc(self):
        return {
            "_id": "singleton", "saved_at": now_iso(),
            "running": self.running, "price": self.price,
            "anchor": self.anchor, "direction": self.direction, "brick_seq": self.brick_seq,
            "bricks": self.bricks[-800:],
            "consec_red": self.consec_red, "consec_green": self.consec_green,
            "down_run_reds": self.down_run_reds, "position": self.position,
            "squared_off_date": self.squared_off_date,
            "day_key": self.day_key, "day_realized": self.day_realized,
            "breaker_tripped": self.breaker_tripped,
        }

    async def _persist_state(self):
        try:
            await self.db.engine_state.replace_one({"_id": "singleton"}, self._state_doc(), upsert=True)
        except Exception as e:
            logger.exception("state persist failed: %s", e)

    async def _load_state(self):
        doc = await self.db.engine_state.find_one({"_id": "singleton"})
        if not doc:
            return
        self.anchor = doc.get("anchor")
        self.direction = doc.get("direction", 0)
        self.brick_seq = doc.get("brick_seq", 0)
        self.bricks = doc.get("bricks") or []
        self.consec_red = doc.get("consec_red", 0)
        self.consec_green = doc.get("consec_green", 0)
        self.down_run_reds = doc.get("down_run_reds", 0)
        self.position = doc.get("position")
        self.squared_off_date = doc.get("squared_off_date")
        self.day_key = doc.get("day_key")
        self.day_realized = doc.get("day_realized", 0.0)
        self.breaker_tripped = doc.get("breaker_tripped", False)
        self.price = self.prev_price = doc.get("price", self.start_price)
        self.running = doc.get("running", False)
        # In-flight orders cannot be trusted across a crash -> clear flags.
        # LIVE NOTE: reconcile self.position against Angel One's actual positions here.
        self.pending_entry = self.pending_exit = False
        if self.position:
            logger.warning("RECOVERED open position from disk: %s", self.position)
        logger.info("Engine state restored (running=%s, bricks=%d)", self.running, len(self.bricks))

    # -------- price source (live Angel LTP or simulated) --------
    async def _next_price(self):
        if self.feed_mode == "LIVE" and self.broker.connected:
            ltp = await asyncio.to_thread(self.broker.get_ltp)
            if ltp is not None:
                self.prev_price = self.price
                self.price = ltp
                self.feed_error = ""
            else:
                self.feed_error = self.broker.error or "No LTP (market closed?)"
                return  # hold last price; Renko just won't update
        else:
            self._gen_price()

    # -------- main loop --------
    # Ticks accumulate into a bar; the Renko bricks are evaluated ONLY on bar close
    # (every bar_seconds), using that bar's close price - just like TradingView 1m Renko.
    async def run_loop(self):
        while True:
            try:
                if self.running:
                    await self._next_price()
                    self.ticks_in_bar += 1
                    self._update_unrealized()
                    if self.ticks_in_bar >= self.settings["bar_seconds"]:
                        self.ticks_in_bar = 0
                        for b in self._feed_close(self.price):   # feed the BAR CLOSE
                            self._process_brick(b)
                    self._maybe_square_off()
                    self._check_circuit_breaker()
                    # periodic crash-recovery snapshot (~every 15s)
                    self.persist_counter += 1
                    if self.persist_counter >= 15:
                        self.persist_counter = 0
                        asyncio.create_task(self._persist_state())
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
        self.squared_off_date = None
        self.day_key = None
        self.day_realized = 0.0
        self.breaker_tripped = False
        self.orders = []

    def snapshot(self):
        m = self.metrics
        total = m["trades"]
        win_rate = round((m["wins"] / total) * 100, 1) if total else 0.0
        ist, today, exp, is_today, past_cut = self._expiry_status()
        unreal = self.position["unrealized_pnl"] if self.position else 0.0
        day_real = self.day_realized if self.day_key == today.isoformat() else 0.0
        breaker = self.breaker_tripped if self.day_key == today.isoformat() else False
        return {
            "running": self.running,
            "mode": self.mode,
            "feed_mode": self.feed_mode,
            "feed_error": self.feed_error,
            "angel": self.broker.status(),
            "price": round(self.price, 2),
            "prev_price": round(self.prev_price, 2),
            "settings": self.settings,
            "bricks": self.bricks[-400:],
            "position": self.position,
            "pending_entry": self.pending_entry,
            "pending_exit": self.pending_exit,
            "consec_red": self.consec_red,
            "consec_green": self.consec_green,
            "down_run_reds": self.down_run_reds,
            "direction": self.direction,
            "ticks_in_bar": self.ticks_in_bar,
            "orders": self.orders[:12],
            "expiry": {
                "next": str(exp),
                "is_today": is_today,
                "square_off_time": self.settings["square_off_time"],
                "auto_square_off": self.settings.get("auto_square_off", True),
                "squared_off": self.squared_off_date == str(today),
                "ist_time": ist.strftime("%H:%M:%S"),
                "entries_blocked": bool(is_today and past_cut),
            },
            "risk": {
                "daily_max_loss": self.settings["daily_max_loss"],
                "circuit_breaker_enabled": self.settings.get("circuit_breaker_enabled", True),
                "day_realized": round(day_real, 2),
                "day_total": round(day_real + unreal, 2),
                "breaker_tripped": breaker,
            },
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
    square_off_time: Optional[str] = None
    auto_square_off: Optional[bool] = None
    daily_max_loss: Optional[float] = None
    circuit_breaker_enabled: Optional[bool] = None


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
    await db.engine_state.delete_one({"_id": "singleton"})
    await engine.load_metrics()
    return {"ok": True}


@api_router.post("/bot/square-off")
async def square_off():
    if not engine.position:
        return {"ok": False, "message": "No open position to square off."}
    engine._force_exit("MANUAL_SQUAREOFF")
    return {"ok": True, "message": "Square-off order placed (demo)."}


@api_router.post("/bot/arm")
async def arm_breaker():
    # Manually re-arm the circuit breaker (clears the tripped state).
    engine.breaker_tripped = False
    engine.day_key = datetime.now(IST).date().isoformat()
    await engine._persist_state()
    return {"ok": True, "message": "Circuit breaker re-armed."}


@api_router.get("/trades")
async def get_trades():
    trades = await db.trades.find({}, {"_id": 0}).sort("exit_time", -1).to_list(500)
    return trades


@api_router.post("/settings")
async def update_settings(body: SettingsUpdate):
    data = body.model_dump(exclude_none=True)
    engine.settings.update(data)
    return engine.settings


@api_router.post("/angel/connect")
async def angel_connect():
    """Log in to Angel One using credentials from backend .env and switch the
    price feed to LIVE. Orders still stay in PAPER/DEMO mode (no real orders)."""
    api_key = os.environ.get("ANGEL_API_KEY", "").strip()
    client_code = os.environ.get("ANGEL_CLIENT_CODE", "").strip()
    pin = os.environ.get("ANGEL_PIN", "").strip()
    totp_secret = os.environ.get("ANGEL_TOTP_SECRET", "").strip()
    missing = [k for k, v in {
        "ANGEL_API_KEY": api_key, "ANGEL_CLIENT_CODE": client_code,
        "ANGEL_PIN": pin, "ANGEL_TOTP_SECRET": totp_secret}.items() if not v]
    if missing:
        return {"connected": False, "error": f"Missing credentials in .env: {', '.join(missing)}"}
    res = await asyncio.to_thread(engine.broker.login, api_key, client_code, pin, totp_secret)
    if res.get("connected"):
        engine.feed_mode = "LIVE"
    return res


@api_router.post("/angel/disconnect")
async def angel_disconnect():
    await asyncio.to_thread(engine.broker.logout)
    engine.feed_mode = "SIM"
    return {"connected": False, "feed_mode": "SIM"}


@api_router.post("/angel/load-history")
async def angel_load_history(body: dict = None):
    body = body or {}
    from_date = body.get("from_date")
    days = int(body.get("days", 5))
    days = max(1, min(days, 70))
    return await engine.load_history(days=days, from_date=from_date)


@api_router.post("/feed/mode")
async def set_feed_mode(body: dict):
    mode = (body.get("feed_mode") or "SIM").upper()
    if mode == "LIVE" and not engine.broker.connected:
        return {"ok": False, "feed_mode": engine.feed_mode, "error": "Connect Angel One first."}
    engine.feed_mode = "LIVE" if mode == "LIVE" else "SIM"
    return {"ok": True, "feed_mode": engine.feed_mode}


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
    await engine._load_state()   # crash/restart recovery
    asyncio.create_task(engine.run_loop())
    logger.info("Trading engine started")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
