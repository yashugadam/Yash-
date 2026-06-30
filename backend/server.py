from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import asyncio
import time
import calendar
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timezone, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
from angel_broker import AngelBroker, safe_err


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

# Hard cap on any single order quantity (units) — guards against a typo/abusive
# request submitting an oversized real-money order. ~75 lots of NIFTY (lot=65).
MAX_ORDER_QTY = 5000

# Exit-retry safety: when a square-off (EXIT) order is rejected, retry — but
# THROTTLED and CAPPED so a persistent broker rejection can't hammer the API
# once per tick. After the cap, auto-retry halts and the position is held for
# manual intervention.
MAX_EXIT_RETRIES = 8
EXIT_RETRY_MIN_GAP = 15        # seconds between auto exit-retries


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
            "buffer_points": 20,           # SEBI-safe limit buffer (no market orders)
            "max_slippage": 20,            # hard cap: never fill more than this far from signal (pts)
            "forced_exit_slippage": 25,    # wider cap for forced exits (expiry/breaker/manual square-off)
            "retry_seconds": 5,            # wait between re-pricing attempts
            "max_order_attempts": 5,       # max placement attempts before alerting
            "max_red_single_green": 4,     # > this reds => need 2 greens to exit
            "greens_to_exit_extended": 2,
            "tick_interval": 1.0,
            "square_off_time": "15:20",    # IST: auto square-off time on expiry day
            "auto_square_off": True,
            "auto_roll": True,             # auto-switch to next month once current contract expires
            "rollover_position": True,     # at expiry square-off, immediately re-open the short on next month
            "daily_max_loss": 10000,       # ₹: auto-stop the bot if day P&L falls to -this
            "circuit_breaker_enabled": True,
        }
        # runtime
        self.running = False
        self.mode = "LIVE"  # LIVE-only: always places REAL orders on Angel One (no paper/demo)
        self.feed_mode = "LIVE"   # LIVE-only: always real Angel One LTP (no simulation)
        self._saved_feed_mode = "LIVE"
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
        self.exit_retry_pending = False
        self.forced_exit_pending = False
        self.alert = None

        # books
        self.orders: List[Dict[str, Any]] = []
        self.metrics = {"realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}

        # safety: duplicate-order protection, expiry square-off, crash recovery
        self.order_lock = asyncio.Lock()
        self.squared_off_date: Optional[str] = None   # date (IST) we already squared off / blocked entries
        self._rollover_armed = False                   # set at expiry square-off to re-short next month
        # On Start: if Angel One holds a short we didn't open (e.g. a manual trade taken while the
        # bot was off), surface it for the user to ADOPT so the bot manages its exit per strategy.
        self.pending_adoption = None                   # {qty, avgprice, netqty, declined}
        self.persist_counter = 0
        # risk: daily max-loss circuit breaker
        self.day_key: Optional[str] = None            # IST date the day P&L belongs to
        self.day_realized = 0.0                       # realized P&L booked today
        self.breaker_tripped = False
        self._last_reconnect = 0.0                     # throttle for auto-reconnect
        # real broker P&L (from Angel One position book — reflects manual + bot fills)
        self.broker_pnl = {"found": False, "realised": 0.0, "unrealised": 0.0, "total": 0.0}
        self._last_pnl_fetch = 0.0
        self._mkt_paused = False                       # True while strategy is frozen (market closed)
        self._open_recon_date: Optional[str] = None     # IST date we already ran the market-open safety reconcile
        self._exit_retry_count = 0                     # consecutive rejected EXITs (hammer guard)
        self._last_exit_retry = 0.0                    # epoch of last auto exit-retry
        self._last_reject_note = ""                    # last broker rejection reason (for alerts)
        self._disc_flagged = False                     # True while a DISCONNECTED-pause alert is active

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
            elif self.consec_red >= 2 and not (self.position or self.pending_entry) \
                    and not self._entries_blocked():
                # Aggressive entry: short on 2+ reds. If the bot (re)starts mid-downtrend
                # with a run already >2 reds, it enters immediately on the next red instead
                # of waiting for a green reset — so an in-progress downtrend isn't missed.
                self.down_run_reds = self.consec_red
                self.pending_entry = True
                brick["signal"] = "SHORT"
                asyncio.create_task(self._execute_order("SELL", "ENTRY", self.price, brick["index"]))
        else:  # green
            self.consec_green += 1
            self.consec_red = 0
            if self.position and not self.pending_exit:
                # Option B: 4 or more reds (incl. the 2 entry reds) -> wait for 2 greens;
                # fewer than 4 reds (i.e. 2 or 3) -> exit on the 1st green.
                need = self.settings["greens_to_exit_extended"] \
                    if self.down_run_reds >= self.settings["max_red_single_green"] else 1
                if self.consec_green >= need:
                    self.pending_exit = True
                    brick["signal"] = "COVER"
                    asyncio.create_task(self._execute_order("BUY", "EXIT", self.price, brick["index"]))

    # -------- order execution: escalating limit with hard slippage cap + retries --------
    async def _cur_price(self):
        if self.feed_mode == "LIVE" and self.broker.connected:
            ltp = await asyncio.to_thread(self.broker.get_ltp)
            if ltp is not None and ltp > 0:
                return ltp
        return self.price

    async def _execute_order(self, side, kind, ref_price, brick_index, reason="SIGNAL"):
        # Duplicate-order protection: only one order in-flight; re-validate inside the lock.
        async with self.order_lock:
            if kind == "ENTRY" and self.position is not None:
                logger.warning("Duplicate ENTRY dropped - position already open")
                self.pending_entry = False
                return
            if kind == "EXIT" and self.position is None:
                logger.warning("EXIT dropped - no open position")
                self.pending_exit = False
                return
            # A fresh exit signal (not an auto-retry) gets a full retry budget.
            if kind == "EXIT" and reason != "EXIT_RETRY":
                self._exit_retry_count = 0

            base = self.settings["buffer_points"]
            forced = reason in ("EXPIRY_SQUAREOFF", "CIRCUIT_BREAKER", "MANUAL_SQUAREOFF",
                                "RECONCILE_REEXIT") \
                or (kind == "EXIT" and self.forced_exit_pending)
            cap = self.settings.get("forced_exit_slippage", 25) if forced \
                else self.settings.get("max_slippage", 10)   # hard slippage cap (pts)
            max_attempts = self.settings.get("max_order_attempts", 5)
            retry_secs = self.settings.get("retry_seconds", 5)
            # worst acceptable fill = ref +/- cap (never fill beyond this)
            floor = ref_price - cap                            # SELL won't go below this
            ceil = ref_price + cap                             # BUY won't go above this

            order = {
                "id": str(uuid.uuid4()), "side": side, "kind": kind, "reason": reason,
                "qty": self.settings["lot_size"], "symbol": self.settings["symbol"],
                "order_type": "LIMIT", "ref_price": round(ref_price, 2),
                "limit_price": None, "status": "PENDING", "attempts": 0,
                "brick_index": brick_index, "time": now_iso(), "fill_price": None,
                "mode": "LIVE", "broker_order_id": None,
                "note": "Limit order (SEBI compliant - no market order)",
            }
            self.orders.insert(0, order)
            self.orders = self.orders[:60]

            # LIVE-only: a real broker connection is required to place any order.
            if not self.broker.connected:
                order["status"] = "REJECTED"
                order["note"] = "Angel One not connected — cannot place LIVE order."
                if kind == "ENTRY":
                    self.pending_entry = False
                else:
                    self.pending_exit = False
                    self.exit_retry_pending = True
                self._set_alert("Order NOT placed — Angel One disconnected. Auto-reconnecting; "
                                "will retry.", "error")
                asyncio.create_task(self._save_order(order))
                asyncio.create_task(self._persist_state())
                return

            filled = await self._live_fill(order, side, base, cap, max_attempts, retry_secs, floor, ceil)

            if filled:
                order["status"] = "COMPLETE"
                if order.get("fill_price") is None:
                    order["fill_price"] = order["limit_price"]
                order["fill_time"] = now_iso()
                self.exit_retry_pending = False
                self._exit_retry_count = 0
                if kind == "EXIT":
                    self.forced_exit_pending = False
                self._apply_fill(order)
            else:
                order["status"] = "REJECTED"
                if not order.get("note", "").startswith(("LIVE", "Broker")):
                    order["note"] = f"NOT FILLED after {max_attempts} tries - price moved > {cap}pt from signal"
                if kind == "ENTRY":
                    self.pending_entry = False
                    self._set_alert(f"ENTRY order failed — {order.get('note', 'unknown reason')}. "
                                    f"No position opened.", "warning")
                else:  # EXIT must keep trying - position is still open (throttled + capped)
                    self.pending_exit = False
                    self.exit_retry_pending = True
                    self._last_reject_note = order.get("note", "unknown reason")
                    self._set_alert(f"EXIT order failed — {self._last_reject_note}. "
                                    f"Position still OPEN — auto-retrying (throttled).", "error")
            asyncio.create_task(self._save_order(order))
        asyncio.create_task(self._persist_state())

    def _set_alert(self, msg, level="info"):
        self.alert = {"id": str(uuid.uuid4()), "msg": msg, "level": level, "time": now_iso()}
        logger.warning("ALERT(%s): %s", level, msg)

    async def _live_fill(self, order, side, base, cap, max_attempts, retry_secs, floor, ceil):
        """Real Angel One LIMIT order with escalating buffer + re-pricing.
        One broker order id is kept and modified across retries to avoid double fills;
        if it gets rejected/cancelled we place a fresh one. Unfilled orders are cancelled."""
        angel_side = "SELL" if side == "SELL" else "BUY"
        qty = self.settings["lot_size"]
        broker_id = None
        for attempt in range(1, max_attempts + 1):
            buffer = min(base + (attempt - 1) * 5, cap)
            cur = await self._cur_price()
            if side == "SELL":
                limit = max(round(cur - buffer, 2), round(floor, 2))
            else:
                limit = min(round(cur + buffer, 2), round(ceil, 2))
            order["attempts"] = attempt
            order["limit_price"] = limit
            order["status"] = "PENDING" if attempt == 1 else "RETRYING"
            if broker_id is None:
                res = await asyncio.to_thread(self.broker.place_limit_order, angel_side, limit, qty)
                if not res.get("ok"):
                    order["note"] = f"Broker reject: {safe_err(res.get('error'))}"
                    if attempt < max_attempts:
                        await asyncio.sleep(retry_secs)
                    continue
                broker_id = res.get("orderid")
                order["broker_order_id"] = broker_id
            else:
                await asyncio.to_thread(self.broker.modify_order_price, broker_id, limit, qty, angel_side)
            await asyncio.sleep(min(2.0, retry_secs))          # let the order work
            st = await asyncio.to_thread(self.broker.get_order_status, broker_id)
            s = (st.get("status") or "").lower()
            if "complet" in s or "filled" in s or "executed" in s:
                order["fill_price"] = st.get("avgprice") or limit
                order["note"] = f"LIVE filled @ {order['fill_price']} (order {broker_id})"
                return True
            if "reject" in s or "cancel" in s:
                order["note"] = f"Broker {s}: {st.get('text', '')}"
                broker_id = None                               # place fresh next attempt
            else:
                order["note"] = f"LIVE working/unfilled - re-pricing, attempt {attempt}/{max_attempts}"
            if attempt < max_attempts:
                await asyncio.sleep(max(retry_secs - 2.0, 1.0))
        if broker_id:   # cancel the dangling working order so it can't fill later unexpectedly
            await asyncio.to_thread(self.broker.cancel_order, broker_id)
            order["note"] = f"LIVE not filled in {max_attempts} tries - order {broker_id} cancelled"
        return False

    # -------- LIVE position reconciliation (on restart) --------
    async def reconcile(self):
        """Compare the bot's recorded position with Angel One's actual net position."""
        if not (self.broker.connected and self.broker.fut_token):
            return {"available": False, "reason": "Angel One not connected."}
        np = await asyncio.to_thread(self.broker.get_net_position)
        if not np.get("found"):
            return {"available": False, "reason": np.get("error") or "Could not read positions."}
        broker_qty = int(np.get("netqty", 0))
        bot_short = bool(self.position)
        if bot_short and broker_qty == 0:
            state, msg = "ENTRY_MISSED", ("Bot shows an open SHORT but Angel One shows NO position - "
                                          "the entry order never executed. You can re-enter the trade.")
        elif (not bot_short) and broker_qty != 0:
            state, msg = "EXIT_MISSED", ("Angel One still shows an OPEN position but the bot is flat - "
                                         "the exit never executed. You can exit the trade again.")
        else:
            state, msg = "GOOD", "Bot and Angel One match - everything is in sync."
        return {"available": True, "state": state, "message": msg, "mode": self.mode,
                "bot_position": self.position, "broker_netqty": broker_qty,
                "broker_avgprice": np.get("avgprice")}

    async def reconcile_resolve(self, action):
        if action == "reenter":
            if not self.position:
                return {"ok": False, "message": "No bot position to re-enter."}
            reds = self.position.get("reds_at_entry", 2)
            self.position = None
            self.pending_entry = True
            self.down_run_reds = reds
            asyncio.create_task(self._execute_order("SELL", "ENTRY", self.price, -1, "RECONCILE_REENTRY"))
            await self._persist_state()
            return {"ok": True, "message": "Re-entry SHORT order placed."}
        if action == "reexit":
            np = await asyncio.to_thread(self.broker.get_net_position)
            qty = abs(int(np.get("netqty", 0)))
            if qty == 0:
                return {"ok": False, "message": "Angel One shows no open position to exit."}
            self.position = {
                "side": "SHORT", "qty": qty, "entry_price": np.get("avgprice") or self.price,
                "entry_time": now_iso(), "entry_order_id": "RECONCILE",
                "reds_at_entry": self.down_run_reds, "unrealized_pnl": 0.0,
            }
            self.pending_exit = False
            self._force_exit("RECONCILE_REEXIT")
            await self._persist_state()
            return {"ok": True, "message": "Exit order placed to flatten the broker position."}
        if action == "accept":
            # Accept Angel One as the source of truth and SYNC the bot to it, so the
            # mismatch is genuinely resolved (otherwise the warning just recomputes).
            np = await asyncio.to_thread(self.broker.get_net_position)
            if not np.get("found"):
                return {"ok": False, "message": np.get("error") or "Could not read Angel One positions."}
            qty = abs(int(np.get("netqty", 0)))
            self.pending_entry = self.pending_exit = False
            self.exit_retry_pending = False
            self._exit_retry_count = 0
            if qty == 0:
                # broker is flat -> clear any stale bot position (broker already closed it)
                self.position = None
                await self._persist_state()
                return {"ok": True, "message": "Synced — bot set to FLAT to match Angel One "
                        "(the broker already closed the position)."}
            # broker holds a position -> adopt it so the bot manages/exits it per strategy
            self.position = {
                "side": "SHORT", "qty": qty, "entry_price": np.get("avgprice") or self.price,
                "entry_time": now_iso(), "entry_order_id": "RECONCILE_ADOPT",
                "reds_at_entry": self.down_run_reds or 2, "unrealized_pnl": 0.0,
            }
            await self._persist_state()
            return {"ok": True, "message": f"Synced — adopted Angel One's open position ({qty} qty) into the bot."}
        return {"ok": False, "message": "Unknown action."}

    async def manual_order(self, side, qty=None):
        """Place a single REAL one-off LIMIT order near LTP for manual/test use.
        Independent of the strategy state machine (does not set engine.position)."""
        if not self.broker.connected:
            return {"ok": False, "message": "Angel One not connected — cannot place order."}
        try:
            qty = self.settings["lot_size"] if qty is None else int(qty)
        except (TypeError, ValueError):
            return {"ok": False, "message": "qty must be a whole number."}
        if qty <= 0 or qty > MAX_ORDER_QTY:
            return {"ok": False, "message": f"qty must be between 1 and {MAX_ORDER_QTY}."}
        cur = await self._cur_price()
        buf = self.settings.get("max_slippage", 20)
        limit = cur + buf if side == "BUY" else cur - buf   # marketable limit for a quick fill
        order = {
            "id": str(uuid.uuid4()), "side": side, "kind": "MANUAL", "reason": "MANUAL_TEST",
            "qty": qty, "symbol": self.settings["symbol"], "order_type": "LIMIT",
            "ref_price": round(cur, 2), "limit_price": round(limit, 2), "status": "PENDING",
            "attempts": 1, "brick_index": -1, "time": now_iso(), "fill_price": None,
            "mode": "LIVE", "broker_order_id": None, "note": "Manual order",
        }
        self.orders.insert(0, order)
        self.orders = self.orders[:60]
        res = await asyncio.to_thread(self.broker.place_limit_order, side, limit, qty)
        if not res.get("ok"):
            order["status"] = "REJECTED"
            order["note"] = f"Broker reject: {safe_err(res.get('error'))}"
            await self._save_order(order)
            return {"ok": False, "message": safe_err(res.get("error")), "note": order["note"]}
        oid = res.get("orderid")
        order["broker_order_id"] = oid
        await asyncio.sleep(2)                               # let it work
        st = await asyncio.to_thread(self.broker.get_order_status, oid)
        s = (st.get("status") or "").lower()
        if "complet" in s or "executed" in s or "filled" in s:
            order["status"] = "COMPLETE"
            order["fill_price"] = st.get("avgprice") or limit
            order["note"] = f"MANUAL {side} filled @ {order['fill_price']} (order {oid})"
        elif "reject" in s or "cancel" in s:
            order["status"] = "REJECTED"
            order["note"] = f"Broker {s}: {st.get('text', '')}"
        else:
            order["status"] = st.get("status") or "OPEN"
            order["note"] = f"MANUAL {side} placed (order {oid}); status: {order['status']}"
        await self._save_order(order)
        return {"ok": True, "order_status": order["status"], "limit_price": order["limit_price"],
                "fill_price": order["fill_price"], "broker_order_id": oid, "note": order["note"]}

    async def _maybe_enter_on_start(self):
        """On Start (catching up to a move already in progress): mirror the EXIT rule to
        decide if a down-move is still 'short-biased'. Look at the most recent red down-run
        and the green pullback after it:
          - down-run of 2-3 reds  -> short-biased unless 1 green has printed
          - down-run of 4+ reds   -> short-biased unless 2 greens have printed
        If still short-biased and we're flat, enter SHORT immediately. (Only on Start.)"""
        if not self.running or self.position or self.pending_entry or self.pending_exit:
            return
        if self._entries_blocked():
            return
        # count trailing greens, then the red run immediately before them
        trailing_greens, i = 0, len(self.bricks) - 1
        while i >= 0 and self.bricks[i]["color"] == "green":
            trailing_greens += 1
            i -= 1
        reds_before = 0
        while i >= 0 and self.bricks[i]["color"] == "red":
            reds_before += 1
            i -= 1
        if reds_before < 2:
            return  # no valid short setup
        required = self.settings["greens_to_exit_extended"] \
            if reds_before >= self.settings["max_red_single_green"] else 1
        if trailing_greens >= required:
            return  # reversal already confirmed -> do not short
        # down-bias still active -> enter SHORT, carrying the already-printed greens so the
        # exit rule continues correctly (e.g. 5 reds + 1 green -> 1 more green will exit)
        self.down_run_reds = reds_before
        self.consec_red = 0 if trailing_greens > 0 else reds_before
        self.consec_green = trailing_greens
        self.pending_entry = True
        last_idx = self.bricks[-1]["index"] if self.bricks else -1
        self._set_alert(f"Started mid-downtrend ({reds_before} reds, {trailing_greens} green "
                        f"pullback — reversal not confirmed) — entering SHORT at market.", "info")
        await self._execute_order("SELL", "ENTRY", self.price, last_idx, "START_IMMEDIATE")

    def _apply_fill(self, order):
        if order["kind"] == "ENTRY":
            self.position = {
                "side": "SHORT", "qty": order["qty"], "entry_price": order["fill_price"],
                "entry_time": order["fill_time"], "entry_order_id": order["id"],
                "reds_at_entry": self.down_run_reds, "unrealized_pnl": 0.0,
            }
            self.pending_entry = False
            self.exit_retry_pending = False
            self._exit_retry_count = 0
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

    async def _save_order(self, order):
        """Persist a terminal order (COMPLETE/REJECTED) to the order_log for an
        auditable history, especially the exact rejection reason."""
        try:
            doc = {k: v for k, v in order.items() if k != "_id"}
            await self.db.order_log.insert_one(doc)
            # keep the collection bounded
            cnt = await self.db.order_log.estimated_document_count()
            if cnt > 1000:
                old = await self.db.order_log.find({}, {"_id": 1}).sort("time", 1).limit(cnt - 1000).to_list(cnt)
                if old:
                    await self.db.order_log.delete_many({"_id": {"$in": [o["_id"] for o in old]}})
        except Exception as e:
            logger.warning("order_log save failed: %s", e)

    def _update_unrealized(self):
        if self.position:
            self.position["unrealized_pnl"] = round(
                (self.position["entry_price"] - self.price) * self.position["qty"], 2)

    # -------- expiry / square-off --------
    def _market_open(self):
        """NSE F&O trading window: Mon–Fri, 09:15–15:30 IST. Outside this window the
        strategy freezes (no brick formation, no exits, no circuit-breaker action) so
        after-hours/overnight garbage LTP from Angel One cannot create bricks or touch
        an open carry-forward position."""
        ist = datetime.now(IST)
        if ist.weekday() >= 5:        # 5 = Saturday, 6 = Sunday
            return False
        return dtime(9, 15) <= ist.time() <= dtime(15, 30)

    def _expiry_status(self):
        ist = datetime.now(IST)
        today = ist.date()
        # prefer the SELECTED contract's real expiry (from Angel); fallback to calc
        exp = None
        if self.broker.connected and self.broker.fut_expiry:
            try:
                exp = date.fromisoformat(self.broker.fut_expiry)
            except Exception:
                exp = None
        if exp is None:
            exp = next_expiry(today)
        is_today = (today == exp)
        try:
            hh, mm = map(int, str(self.settings["square_off_time"]).split(":"))
        except Exception:
            hh, mm = 15, 0
        past_cut = ist.time() >= dtime(hh, mm)
        return ist, today, exp, is_today, past_cut

    async def _on_start(self):
        """Called when the bot is turned ON. First reconcile with Angel One: if the broker
        holds a position on our instrument that the bot isn't managing (e.g. a manual trade
        taken while the bot was off), surface it for adoption (no auto-entry, no stacking).
        Otherwise, run the normal enter-on-start logic."""
        self.pending_adoption = None
        if self.broker.connected and self.position is None:
            np = await asyncio.to_thread(self.broker.get_net_position)
            if np.get("found"):
                qty = int(np.get("netqty") or 0)
                if qty < 0:   # SHORT on Angel One that the bot isn't tracking -> ask to adopt
                    self.pending_adoption = {"qty": abs(qty), "avgprice": np.get("avgprice"),
                                             "netqty": qty, "side": "SHORT", "declined": False}
                    self._set_alert(f"Found an existing SHORT of {abs(qty)} qty on Angel One. "
                                    f"Adopt it so the bot manages the exit?", "warning")
                    await self._persist_state()
                    return
                if qty > 0:   # LONG is outside the short-only strategy
                    self.pending_adoption = {"qty": qty, "avgprice": np.get("avgprice"),
                                             "netqty": qty, "side": "LONG", "declined": False}
                    self._set_alert(f"A LONG position ({qty} qty) exists on Angel One — outside the "
                                    f"short-only strategy. Bot will NOT trade until it's resolved.", "error")
                    await self._persist_state()
                    return
        await self._maybe_enter_on_start()

    async def _market_open_reconcile(self):
        """Once per day, the first time the market is open AND Angel One is connected,
        auto-reconcile: if the broker holds a position the bot isn't tracking (e.g. a manual
        short carried overnight), surface it for adoption so the bot never stacks a new short
        on top of it. Runs only while the bot is flat; if already managing a position, just
        marks the day done. Replaces the old continuous 2-min polling with a single safe check."""
        today = datetime.now(IST).date().isoformat()
        if self._open_recon_date == today:
            return
        if not self.broker.connected:
            return
        if self.position is not None or self.pending_adoption is not None:
            self._open_recon_date = today
            return
        np = await asyncio.to_thread(self.broker.get_net_position)
        if not np.get("found"):
            return  # broker read failed — retry next tick, don't burn today's check
        self._open_recon_date = today
        qty = int(np.get("netqty") or 0)
        if qty < 0:
            self.pending_adoption = {"qty": abs(qty), "avgprice": np.get("avgprice"),
                                     "netqty": qty, "side": "SHORT", "declined": False}
            self._set_alert(f"Market open safety check: found an existing SHORT of {abs(qty)} qty on "
                            f"Angel One (manual/carry-forward). Adopt it so the bot manages the exit — "
                            f"new entries are paused until you decide.", "warning")
            await self._persist_state()
        elif qty > 0:
            self.pending_adoption = {"qty": qty, "avgprice": np.get("avgprice"),
                                     "netqty": qty, "side": "LONG", "declined": False}
            self._set_alert(f"Market open safety check: a LONG position ({qty} qty) exists on Angel One — "
                            f"outside the short-only strategy. Bot will NOT trade until it's resolved.", "error")
            await self._persist_state()


    async def adopt_position(self, confirm: bool):
        """User's decision on the existing Angel One position found at Start."""
        pa = self.pending_adoption
        if not pa:
            return {"ok": False, "message": "No position pending adoption."}
        if not confirm:
            self.pending_adoption = {**pa, "declined": True}  # keep blocking entries, hide prompt
            self._set_alert("Position not adopted — bot will NOT open new trades while this "
                            "Angel One position is open. Close it manually or Stop & Start to re-check.", "warning")
            await self._persist_state()
            return {"ok": True, "message": "Declined — new entries stay blocked to avoid stacking."}
        if pa.get("side") == "LONG":
            self.pending_adoption = {**pa, "declined": True}
            return {"ok": False, "message": "Long positions aren't supported by the short-only strategy."}
        # adopt the SHORT and let the strategy manage its exit
        self.position = {
            "side": "SHORT", "qty": pa["qty"], "entry_price": pa.get("avgprice") or self.price,
            "entry_time": now_iso(), "entry_order_id": "ADOPTED_MANUAL",
            "reds_at_entry": self.down_run_reds or 2, "unrealized_pnl": 0.0,
        }
        self.pending_adoption = None
        self.pending_entry = self.pending_exit = False
        self._set_alert(f"Adopted existing SHORT ({self.position['qty']} qty @ "
                        f"{self.position['entry_price']}). Bot will exit it per the strategy.", "info")
        await self._persist_state()
        return {"ok": True, "message": "Adopted — managing exit per strategy."}

    def _entries_blocked(self):
        # No new entries once we've hit the expiry-day square-off window, the circuit breaker,
        # or while an un-adopted Angel One position is awaiting the user's decision (no stacking).
        _, _, _, is_today, past_cut = self._expiry_status()
        return bool((is_today and past_cut) or self.breaker_tripped or self.pending_adoption is not None)

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
                # arm true position-rollover: re-open the short on next month after exit fills
                if self.settings.get("rollover_position", True) and self.settings.get("auto_roll", True):
                    self._rollover_armed = True
                logger.warning("EXPIRY square-off triggered at %s IST", self.settings["square_off_time"])
                self._force_exit("EXPIRY_SQUAREOFF")
            elif self.position is None and not self.pending_exit:
                self.squared_off_date = str(today)  # nothing to exit; just block new entries

    def _force_exit(self, reason):
        if not self.position or self.pending_exit:
            return
        self.pending_exit = True
        self.forced_exit_pending = True
        asyncio.create_task(self._execute_order("BUY", "EXIT", self.price, -1, reason))

    def _maybe_auto_roll(self):
        """Switch to the next-month contract once the active one has expired — including
        ON expiry day after the square-off cutoff, so the bot can resume trading the new
        contract the same session instead of staying stuck on the expired one."""
        if not self.settings.get("auto_roll", True):
            return
        if not self.broker.connected or not self.broker.fut_expiry:
            return
        if self.position or self.pending_exit or self.pending_entry:
            return  # never roll mid-trade
        try:
            exp = date.fromisoformat(self.broker.fut_expiry)
        except Exception:
            return
        _, _, _, is_today, past_cut = self._expiry_status()
        today = datetime.now(IST).date()
        # Roll if the contract already expired (next day) OR it expires today and we're
        # past the square-off cutoff (position is now flat after the expiry square-off).
        if exp < today or (exp == today and past_cut):
            res = self.broker.roll_to_next()
            if res.get("ok"):
                self.settings["instrument_token"] = res["token"]
                self.anchor = None
                self.direction = 0
                self.bricks = []
                self.brick_seq = 0
                self.consec_red = self.consec_green = self.down_run_reds = 0
                self.squared_off_date = None
                self._set_alert(f"Auto-rolled to next contract: {res['symbol']} "
                                f"(previous expired). Chart reset for new series.", "info")
                asyncio.create_task(self._persist_state())
                asyncio.create_task(self._autoload_after_roll())

    async def _autoload_after_roll(self):
        await self.load_history(days=5)
        # True position rollover: if we squared off an OPEN short at expiry, immediately
        # re-open the short on the just-rolled next-month contract (carry across expiry).
        if self._rollover_armed:
            self._rollover_armed = False
            await self._rollover_enter()

    async def _rollover_enter(self):
        """Open a fresh SHORT on the newly-rolled next-month contract right after the
        expiry square-off — independent of a brick signal (expiry position rollover)."""
        if self.position or self.pending_entry or not self.broker.connected:
            return
        if not self._market_open():
            self._set_alert("Expiry rollover skipped — market closed; will resume next session.", "warning")
            return
        # treat like a standard 2-red short so the exit logic behaves normally
        self.consec_red = self.down_run_reds = 2
        self.consec_green = 0
        self.pending_entry = True
        cur = await self._cur_price()
        self._set_alert(f"Expiry rollover — opening new SHORT on {self.broker.fut_symbol}.", "info")
        await self._execute_order("SELL", "ENTRY", cur, -1, "EXPIRY_ROLLOVER")

    # -------- crash / restart recovery --------
    def _state_doc(self):
        return {
            "_id": "singleton", "saved_at": now_iso(),
            "running": self.running, "price": self.price, "mode": self.mode,
            "settings": self.settings,
            "anchor": self.anchor, "direction": self.direction, "brick_seq": self.brick_seq,
            "bricks": self.bricks[-800:],
            "consec_red": self.consec_red, "consec_green": self.consec_green,
            "down_run_reds": self.down_run_reds, "position": self.position,
            "squared_off_date": self.squared_off_date, "feed_mode": self.feed_mode,
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
        if doc.get("settings"):
            self.settings.update(doc["settings"])
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
        self.mode = "LIVE"          # LIVE-only app: always real-money mode
        self._saved_feed_mode = "LIVE"
        self.feed_mode = "LIVE"
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
        # LIVE-only: always use real Angel One LTP. If the session has dropped,
        # auto-reconnect (throttled) instead of ever simulating a price.
        if not self.broker.connected:
            self.feed_error = "Angel One disconnected — auto-reconnecting…"
            await self._auto_reconnect()
            return
        ltp = await asyncio.to_thread(self.broker.get_ltp)
        if ltp is not None and ltp > 0:
            self.prev_price = self.price
            self.price = ltp
            self.feed_error = ""
        else:
            self.feed_error = self.broker.error or "No LTP (market closed?)"

    async def _auto_reconnect(self):
        """Re-login to Angel One when the session drops (throttled ~every 20s)."""
        now = time.time()
        if now - self._last_reconnect < 20:
            return
        self._last_reconnect = now
        ok = await asyncio.to_thread(self.broker.relogin)
        if ok:
            saved = self.settings.get("instrument_token")
            if saved:
                self.broker.select_instrument(saved)
            self.feed_error = ""
            self._set_alert("Angel One session reconnected automatically.", "info")
            logger.info("Auto-reconnected Angel One (session had dropped)")

    async def _refresh_broker_pnl(self):
        """Pull real day P&L from Angel One (throttled ~8s). Runs whether or not the
        bot is 'running', so manual-panel trades are reflected too."""
        if not self.broker.connected:
            return
        now = time.time()
        if now - self._last_pnl_fetch < 8:
            return
        self._last_pnl_fetch = now
        try:
            pnl = await asyncio.to_thread(self.broker.get_day_pnl)
            if pnl.get("found"):
                self.broker_pnl = pnl
        except Exception as e:
            logger.warning("broker pnl refresh failed: %s", e)

    # -------- main loop --------
    # Ticks accumulate into a bar; the Renko bricks are evaluated ONLY on bar close
    # (every bar_seconds), using that bar's close price - just like TradingView 1m Renko.
    async def run_loop(self):
        while True:
            try:
                await self._refresh_broker_pnl()
                if self.running:
                    if self._market_open():
                        if self._mkt_paused:
                            self._mkt_paused = False
                            self._set_alert("Market open — strategy resumed.", "info")
                        await self._next_price()        # auto-reconnects if the session dropped
                        if not self.broker.connected:
                            # SESSION HEALTH GUARD: while Angel One is disconnected, prioritise
                            # reconnecting and PAUSE all order activity — no bricks, no entries,
                            # no exit retries — so we never spam rejected "not connected" orders.
                            # The open position is held; auto-reconnect runs via _next_price.
                            self.ticks_in_bar = 0
                            if not self._disc_flagged:
                                self._disc_flagged = True
                                self._set_alert("Angel One DISCONNECTED — reconnecting; orders paused, "
                                                "position held.", "error")
                        else:
                            if self._disc_flagged:
                                self._disc_flagged = False
                                self._set_alert("Angel One reconnected — strategy resumed.", "info")
                            # One-time daily safety reconcile at market open: adopt any
                            # untracked broker position (manual/carry-forward) so the bot
                            # never stacks a new short on top of it.
                            await self._market_open_reconcile()
                            self.ticks_in_bar += 1
                            self._update_unrealized()
                            if self.ticks_in_bar >= self.settings["bar_seconds"]:
                                self.ticks_in_bar = 0
                                for b in self._feed_close(self.price):   # feed the BAR CLOSE
                                    self._process_brick(b)
                            self._maybe_square_off()
                            self._check_circuit_breaker()
                            self._maybe_auto_roll()
                            # Retry a failed EXIT — THROTTLED (>= EXIT_RETRY_MIN_GAP apart)
                            # and CAPPED (MAX_EXIT_RETRIES) so a persistent broker rejection
                            # can't hammer the API once per tick. After the cap, halt and hold.
                            if self.exit_retry_pending and self.position and not self.pending_exit:
                                if self._exit_retry_count >= MAX_EXIT_RETRIES:
                                    self.exit_retry_pending = False
                                    self._set_alert(
                                        f"EXIT rejected {self._exit_retry_count}× — auto-retry HALTED to "
                                        f"protect your account. Position is STILL OPEN and held. "
                                        f"Last reason: {self._last_reject_note or 'unknown'}. Use Check Angel One "
                                        f"to reconcile, then square off manually if needed.", "error")
                                else:
                                    gap = max(self.settings.get("retry_seconds", 5), EXIT_RETRY_MIN_GAP)
                                    if time.time() - self._last_exit_retry >= gap:
                                        self._last_exit_retry = time.time()
                                        self._exit_retry_count += 1
                                        self.pending_exit = True
                                        asyncio.create_task(self._execute_order("BUY", "EXIT", self.price, -1, "EXIT_RETRY"))
                    else:
                        # Market CLOSED: freeze the strategy entirely. No new bricks, no
                        # exits, no circuit-breaker action — the open position is held
                        # untouched (carry-forward) until the next session at 09:15 IST.
                        self.ticks_in_bar = 0
                        if not self._mkt_paused:
                            self._mkt_paused = True
                            self._set_alert("Market closed — strategy paused; position held. "
                                            "No bricks will form until 09:15 IST.", "info")
                    # periodic crash-recovery snapshot (~every 15s), regardless of market state
                    self.persist_counter += 1
                    if self.persist_counter >= 15:
                        self.persist_counter = 0
                        asyncio.create_task(self._persist_state())
            except Exception as e:
                logger.exception("engine tick error: %s", e)
            await asyncio.sleep(self.settings["tick_interval"])

    async def load_metrics(self):
        pipeline = [{
            "$group": {
                "_id": None,
                "realized": {"$sum": "$pnl"},
                "trades": {"$sum": 1},
                "wins": {"$sum": {"$cond": [{"$gte": ["$pnl", 0]}, 1, 0]}},
                "losses": {"$sum": {"$cond": [{"$lt": ["$pnl", 0]}, 1, 0]}},
            }
        }]
        res = await self.db.trades.aggregate(pipeline).to_list(1)
        if res:
            r = res[0]
            self.metrics = {"realized_pnl": round(r["realized"], 2), "trades": r["trades"],
                            "wins": r["wins"], "losses": r["losses"]}
        else:
            self.metrics = {"realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}

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
        self.exit_retry_pending = False
        self._exit_retry_count = 0
        self._rollover_armed = False
        self.pending_adoption = None
        self.forced_exit_pending = False
        self.alert = None
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
            "market_open": self._market_open(),
            "pending_adoption": self.pending_adoption,
            "feed_mode": self.feed_mode,
            "feed_error": self.feed_error,
            "alert": self.alert,
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
                "auto_roll": self.settings.get("auto_roll", True),
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
                "broker_pnl": self.broker_pnl,
            },
            "metrics": {**m, "win_rate": win_rate,
                        "unrealized_pnl": self.position["unrealized_pnl"] if self.position else 0.0},
        }


engine = TradingEngine(db)


# ----------------------------- API -----------------------------
class SettingsUpdate(BaseModel):
    brick_size: Optional[int] = Field(None, ge=1, le=2000)
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


@api_router.get("/state")
async def get_state():
    return engine.snapshot()


@api_router.post("/bot/start")
async def start_bot():
    engine.running = True
    await engine._persist_state()
    # reconcile with Angel One (adopt a manual position if present), else enter-on-start
    asyncio.create_task(engine._on_start())
    return {"running": True}


class AdoptRequest(BaseModel):
    confirm: bool = False


@api_router.post("/bot/adopt")
async def adopt_position(req: AdoptRequest):
    return await engine.adopt_position(req.confirm)


class StopRequest(BaseModel):
    square_off: Optional[bool] = False


@api_router.post("/bot/stop")
async def stop_bot(req: StopRequest = StopRequest()):
    squared = False
    if req.square_off and engine.position and not engine.pending_exit:
        engine._force_exit("MANUAL_SQUAREOFF")  # force-cover open position before halting
        squared = True
    engine.running = False
    await engine._persist_state()
    return {"running": False, "squared_off": squared}


@api_router.post("/bot/reset")
async def reset_bot():
    engine.reset()
    await db.trades.delete_many({})
    await db.order_log.delete_many({})
    await db.engine_state.delete_one({"_id": "singleton"})
    await engine.load_metrics()
    return {"ok": True}


@api_router.post("/bot/square-off")
async def square_off():
    if not engine.position:
        return {"ok": False, "message": "No open position to square off."}
    engine._force_exit("MANUAL_SQUAREOFF")
    return {"ok": True, "message": "Square-off order placed (demo)."}


@api_router.post("/bot/trade-mode")
async def set_trade_mode(body: dict):
    # LIVE-only app: mode is always LIVE (real money). Kept for backward compatibility.
    engine.mode = "LIVE"
    return {"ok": True, "mode": "LIVE"}


@api_router.get("/bot/reconcile")
async def get_reconcile():
    return await engine.reconcile()


@api_router.post("/bot/reconcile/resolve")
async def post_reconcile_resolve(body: dict):
    action = (body.get("action") or "").lower()
    return await engine.reconcile_resolve(action)


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


@api_router.get("/orders/log")
async def get_order_log():
    return await db.order_log.find({}, {"_id": 0}).sort("time", -1).to_list(150)


@api_router.post("/orders/log/clear")
async def clear_order_log():
    """Clear historical order-log rows (e.g. old rejections). Does NOT touch any open
    position, trades, or broker state — purely cleans the display log."""
    res = await db.order_log.delete_many({})
    engine.orders = []
    return {"ok": True, "cleared": res.deleted_count}


@api_router.post("/orders/manual")
async def post_manual_order(body: dict):
    side = (body.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return {"ok": False, "message": "side must be BUY or SELL"}
    return await engine.manual_order(side, body.get("qty"))


@api_router.post("/settings")
async def update_settings(body: SettingsUpdate):
    data = body.model_dump(exclude_none=True)
    engine.settings.update(data)
    await engine._persist_state()   # persist so settings survive restarts
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
        saved = engine.settings.get("instrument_token")
        if saved:
            sel = engine.broker.select_instrument(saved)
            if sel.get("ok"):
                res["future"] = sel["symbol"]
        engine.feed_mode = "LIVE"
        await engine._persist_state()
    return res


@api_router.post("/angel/disconnect")
async def angel_disconnect():
    await asyncio.to_thread(engine.broker.logout)
    await engine._persist_state()
    return {"connected": False, "feed_mode": "LIVE"}


@api_router.post("/angel/load-history")
async def angel_load_history(body: dict = None):
    body = body or {}
    from_date = body.get("from_date")
    days = int(body.get("days", 5))
    days = max(1, min(days, 70))
    return await engine.load_history(days=days, from_date=from_date)


@api_router.get("/angel/instruments")
async def angel_instruments(q: str = ""):
    if not engine.broker.connected:
        return {"ok": False, "error": "Connect Angel One first.", "items": []}
    return {"ok": True, "items": engine.broker.search_futures(q)}


@api_router.post("/angel/select-instrument")
async def angel_select_instrument(body: dict):
    token = str(body.get("token", ""))
    res = engine.broker.select_instrument(token)
    if res.get("ok"):
        engine.settings["instrument_token"] = token
        # switching contract -> reset the renko chart for the new price series
        engine.anchor = None
        engine.direction = 0
        engine.bricks = []
        engine.brick_seq = 0
        engine.consec_red = engine.consec_green = engine.down_run_reds = 0
        await engine._persist_state()
    return res


@api_router.post("/feed/mode")
async def set_feed_mode(body: dict):
    # LIVE-only app: feed is always real Angel One data. Kept for backward compatibility.
    engine.feed_mode = "LIVE"
    return {"ok": True, "feed_mode": "LIVE"}


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
    asyncio.create_task(_auto_connect_angel())   # resume LIVE feed after a restart
    logger.info("Trading engine started")


async def _auto_connect_angel():
    """If Angel creds exist in env, auto-login on boot so a LIVE session survives restarts."""
    api_key = os.environ.get("ANGEL_API_KEY", "").strip()
    client_code = os.environ.get("ANGEL_CLIENT_CODE", "").strip()
    pin = os.environ.get("ANGEL_PIN", "").strip()
    totp_secret = os.environ.get("ANGEL_TOTP_SECRET", "").strip()
    if not all([api_key, client_code, pin, totp_secret]):
        return
    res = await asyncio.to_thread(engine.broker.login, api_key, client_code, pin, totp_secret)
    if res.get("connected"):
        saved = engine.settings.get("instrument_token")
        if saved:
            engine.broker.select_instrument(saved)   # restore user's chosen contract
        engine.feed_mode = "LIVE"
        logger.info("Angel One connected on boot; LIVE feed active")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
