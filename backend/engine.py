"""Renko strategy engine: leadership, order execution, backtesting, reconciliation."""
import os
import asyncio
import time
import uuid
from datetime import datetime, timezone, date, time as dtime, timedelta
from typing import List, Optional, Dict, Any
from pymongo import ReturnDocument

from config import (IST, logger, INSTANCE_ID, LEADER_LEASE_SEC, ALERT_TTL_SEC,
                    MAX_ORDER_QTY, MAX_EXIT_RETRIES, EXIT_RETRY_MIN_GAP)
from utils import now_iso, next_expiry
from db import db
from angel_broker import AngelBroker, safe_err


# ----------------------------- Trading Engine -----------------------------
class TradingEngine:
    def __init__(self, database):
        self.db = database
        # settings
        self.settings = {
            "symbol": "NIFTY FUT",
            "brick_size": 50,
            "entry_bricks": 2,             # consecutive same-colour bricks required to enter
            "chop_filter": True,           # ER (efficiency-ratio) entry filter — block entries in chop
            "chop_lookback": 50,           # bricks in the ER window
            "chop_threshold": 0.30,        # min ER (net move / total path) to allow an entry
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
        self._entry_side: Optional[str] = None    # side of the position being entered (LONG/SHORT)
        self.pending_exit = False
        self.exit_retry_pending = False
        self.forced_exit_pending = False
        self.alert = None
        self._alert_ts = 0.0                           # epoch of last alert; expires from /api/state after ALERT_TTL_SEC

        # books
        self.orders: List[Dict[str, Any]] = []
        self.metrics = {"realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}

        # safety: duplicate-order protection, expiry square-off, crash recovery
        self.order_lock = asyncio.Lock()
        self.squared_off_date: Optional[str] = None   # date (IST) we already squared off / blocked entries
        self._rollover_armed = False                   # set at expiry square-off to re-open on next month
        self._rollover_side: Optional[str] = None      # side to re-open at expiry rollover
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
        self._scrip_refresh_date: Optional[str] = None   # IST date we last refreshed the scrip-master cache
        self.is_leader = False                           # True only on the pod that holds the trading lease
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

        # Never rebuild the chart/counters underneath an OPEN position or an armed rollover — that
        # would desync the strategy from the live trade. Rebuild the ER window in a scratch pass and
        # only publish the fresh bricks (leaving position/consec state intact for the live loop).
        if self.position or self.pending_entry or self.pending_exit:
            saved = (self.anchor, self.direction, self.brick_seq, self.bricks,
                     self.consec_red, self.consec_green, self.down_run_reds, self.ticks_in_bar)
            self.anchor = None; self.direction = 0; self.bricks = []; self.brick_seq = 0
            for c in all_candles:
                self._feed_close(float(c[4]), c[0])
            fresh = self.bricks
            (self.anchor, self.direction, self.brick_seq, self.bricks,
             self.consec_red, self.consec_green, self.down_run_reds, self.ticks_in_bar) = saved
            self.bricks = fresh[-800:] if len(fresh) > len(self.bricks) else self.bricks
            await self._persist_state()
            return {"ok": True, "candles": len(all_candles), "bricks": len(self.bricks),
                    "note": "position open — chart refreshed without touching live strategy state",
                    "from": start.strftime("%Y-%m-%d"), "to": now.strftime("%Y-%m-%d"),
                    "symbol": self.broker.fut_symbol}

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

    async def _load_history_warmup(self):
        """Load enough real history for the CURRENT contract to fully warm up the ER window
        (chop_lookback + buffer bricks). NIFTY prints only ~4 bricks/day at 50-pt, so a fresh
        month contract needs a generous span; we widen the window until we have enough bricks
        (capped at 70 days). Called after an expiry roll or a manual contract change so the chop
        filter is live immediately instead of blocking every entry for days while it warms up."""
        target = int(self.settings.get("chop_lookback", 50) or 50) + 5
        res = {}
        for days in (25, 45, 70):
            res = await self.load_history(days=days)
            if not res.get("ok"):
                return res
            if len(self.bricks) >= target:
                break
        if len(self.bricks) < target:
            self._set_alert(f"New contract warm-up: only {len(self.bricks)} bricks in 70d history "
                            f"(ER needs {target}). Chop filter will finish warming up as live "
                            f"bricks form.", "warning")
        return res

    # -------- strategy backtest (simulation-only: real historical candles, NO orders) --------
    NIFTY_INDEX_TOKEN = "99926000"   # NIFTY 50 index (NSE) — continuous multi-year 1-min history

    def _simulate(self, candles, bs, lot, entry_bricks=2, exit_bricks=1,
                  cost_per_trade=0.0, trend_ema=0,
                  lookback=0, range_breakout=False, chop_filter=False,
                  chop_thr=0.3, structure=False, exit_chop=False, exit_thr=0.30,
                  er_adaptive=False, adapt_window=120, adapt_pct=70,
                  er_rising=False, rise_gap=3):
        """Replay candle closes through the CURRENT live Renko strategy (symmetric long+short:
        enter on `entry_bricks` consecutive same-colour bricks, exit on `exit_bricks` opposite
        bricks, then flip). `cost_per_trade` = round-trip cost subtracted per trade (NET P&L).
        `trend_ema`>0 adds an EMA trend filter on brick closes.

        PRICE-ACTION FILTERS (all use a `lookback` window of the most recent brick closes):
        - `range_breakout`: LONG only if the entry close breaks ABOVE the prior swing high, SHORT
          only if it breaks BELOW the prior swing low (skips entries fired inside a sideways range).
        - `chop_filter`: block ALL entries while the market is choppy — Kaufman efficiency ratio
          (net move / total path over the window) must be >= `chop_thr` (trending, not ranging).
        - `structure`: LONG only if net move over the window is up (close > close `lookback` bricks
          ago), SHORT only if net move is down (trade with the local structure)."""
        anchor = {"v": None}; direction = {"v": 0}

        def make_bricks(state, size):
            def _b(price, ts):
                out = []
                if state["v"] is None:
                    state["v"] = round(price / size) * size; state["d"] = 0; return out
                n = int((price - state["v"]) / size)
                if n == 0:
                    return out
                s = 1 if n > 0 else -1
                if state["d"] == 0 or s == state["d"]:
                    count = abs(n)
                else:
                    if abs(n) < 2:
                        return out
                    count = abs(n) - 1; state["v"] += s * size
                for _ in range(count):
                    c2 = state["v"] + s * size; state["v"] = c2; state["d"] = s
                    out.append({"color": "green" if s > 0 else "red", "close": round(c2, 2), "time": ts})
                return out
            return _b

        micro_state = {"v": None, "d": 0}
        micro = make_bricks(micro_state, bs)

        consec_red = consec_green = 0
        position = None; trades = []
        equity = peak = 0.0; max_dd = 0.0; wins = 0; gross_win = gross_loss = 0.0
        ema = None; alpha = (2.0 / (trend_ema + 1)) if trend_ema else 0.0
        lb = max(0, int(lookback or 0))
        pa_on = lb > 0 and (range_breakout or chop_filter or structure or er_adaptive or er_rising)
        track_hist = lb > 0 and (pa_on or exit_chop)
        hist = []                       # rolling brick closes (prior to the current brick)
        er_hist = []                    # rolling ER values (for the adaptive/regime threshold)

        def _pct(vals, p):
            if not vals:
                return 0.0
            sv = sorted(vals)
            k = (len(sv) - 1) * (p / 100.0)
            lo = int(k); hi = min(lo + 1, len(sv) - 1)
            return sv[lo] + (sv[hi] - sv[lo]) * (k - lo)

        def _er_dir(price_now):
            """Efficiency ratio + net direction over the last `lb` closes (incl. price_now).
            Returns (er, netdir). er in 0..1; netdir +1 up / -1 down / 0 flat."""
            win = hist[-lb:]
            if len(win) < lb:
                return None, 0
            seq = win + [price_now]
            path = sum(abs(seq[i] - seq[i - 1]) for i in range(1, len(seq)))
            er = abs(seq[-1] - seq[0]) / path if path else 0.0
            nd = 1 if seq[-1] > seq[0] else (-1 if seq[-1] < seq[0] else 0)
            return er, nd

        for c in candles:
            price = float(c[4])
            for b in micro(price, c[0]):
                close = b["close"]
                if trend_ema:
                    ema = close if ema is None else ema + alpha * (close - ema)
                if b["color"] == "red":
                    consec_red += 1; consec_green = 0
                else:
                    consec_green += 1; consec_red = 0

                exited = False
                # EXIT on `exit_bricks` opposite brick(s). With `exit_chop`, a SINGLE opposite
                # brick is treated as noise (held) while the ER trend is still strong & aligned
                # with the position — ride the winner. A 2nd opposite brick always forces the exit.
                if position:
                    is_short = position["side"] == "SHORT"
                    opp = (is_short and b["color"] == "green") or (not is_short and b["color"] == "red")
                    consec_opp = consec_green if is_short else consec_red
                    if opp and consec_opp >= exit_bricks:
                        do_exit = True
                        if exit_chop and consec_opp < 2:
                            er, nd = _er_dir(close)
                            aligned = (not is_short and nd > 0) or (is_short and nd < 0)
                            if er is not None and er >= exit_thr and aligned:
                                do_exit = False       # strong aligned trend → ignore this brick
                        if do_exit:
                            pts = (position["entry"] - close) if is_short else (close - position["entry"])
                            pnl = pts * lot - cost_per_trade
                            equity += pnl; peak = max(peak, equity); max_dd = min(max_dd, equity - peak)
                            trades.append({"side": position["side"], "entry_time": position["entry_time"],
                                           "exit_time": b["time"], "entry": position["entry"], "exit": close,
                                           "points": round(pts, 2), "pnl": round(pnl, 2),
                                           "equity": round(equity, 2)})
                            if pnl >= 0:
                                wins += 1; gross_win += pnl
                            else:
                                gross_loss += pnl
                            position = None; exited = True

                # ENTRY when flat (skip the brick we exited on). Gated by EMA + optional
                # price-action filters (range breakout / chop / structure) on `hist`.
                if position is None and not exited:
                    long_ok = (not trend_ema or close > ema)
                    short_ok = (not trend_ema or close < ema)
                    if pa_on:
                        win = hist[-lb:]
                        if len(win) < lb:
                            long_ok = short_ok = False     # not enough history yet
                        else:
                            if range_breakout:
                                long_ok = long_ok and close > max(win)
                                short_ok = short_ok and close < min(win)
                            if structure:
                                long_ok = long_ok and close > win[0]
                                short_ok = short_ok and close < win[0]
                            if chop_filter or er_adaptive or er_rising:
                                er, _ = _er_dir(close)
                                if er is None:
                                    long_ok = short_ok = False
                                else:
                                    if er_adaptive and len(er_hist) >= max(20, lb):
                                        thr_dyn = max(_pct(er_hist, adapt_pct), chop_thr if chop_filter else 0.0)
                                    else:
                                        thr_dyn = chop_thr
                                    if er < thr_dyn:
                                        long_ok = short_ok = False
                                    if er_rising and len(er_hist) >= rise_gap and er <= er_hist[-rise_gap]:
                                        long_ok = short_ok = False
                    if b["color"] == "red" and consec_red >= entry_bricks and short_ok:
                        position = {"side": "SHORT", "entry": close, "entry_time": b["time"]}
                    elif b["color"] == "green" and consec_green >= entry_bricks and long_ok:
                        position = {"side": "LONG", "entry": close, "entry_time": b["time"]}

                if track_hist:
                    hist.append(close)
                    if len(hist) > lb + 5:
                        hist = hist[-(lb + 5):]
                    if er_adaptive or er_rising:
                        e2, _ = _er_dir(close)
                        if e2 is not None:
                            er_hist.append(e2)
                            if len(er_hist) > max(adapt_window, rise_gap + 2):
                                er_hist = er_hist[-max(adapt_window, rise_gap + 2):]

        n = len(trades)
        net = round(sum(t["pnl"] for t in trades), 2)
        summary = {
            "brick_size": bs, "entry_bricks": entry_bricks, "exit_bricks": exit_bricks,
            "cost_per_trade": cost_per_trade, "trend_ema": trend_ema,
            "lookback": lb, "range_breakout": bool(range_breakout),
            "chop_filter": bool(chop_filter), "chop_thr": chop_thr, "structure": bool(structure),
            "exit_chop": bool(exit_chop), "exit_thr": exit_thr,
            "er_adaptive": bool(er_adaptive), "adapt_window": adapt_window, "adapt_pct": adapt_pct,
            "er_rising": bool(er_rising), "rise_gap": rise_gap,
            "trades": n, "wins": wins, "losses": n - wins,
            "win_rate": round(100 * wins / n, 1) if n else 0.0,
            "net_pnl": net, "net_points": round(sum(t["points"] for t in trades), 2),
            "avg_pnl": round(net / n, 2) if n else 0.0,
            "best": round(max((t["pnl"] for t in trades), default=0.0), 2),
            "worst": round(min((t["pnl"] for t in trades), default=0.0), 2),
            "profit_factor": round(gross_win / abs(gross_loss), 2) if gross_loss else None,
            "max_drawdown": round(max_dd, 2), "open_position": bool(position),
        }
        return summary, trades

    async def backtest(self, from_date=None, to_date=None, brick_size=None,
                       brick_sizes=None, days=30, source="future",
                       entry_bricks=2, exit_bricks=1, cost_per_trade=0.0, trend_ema=0,
                       variants=None):
        """Simulation-only backtest — never places an order or mutates live state. Fills use
        brick-close prices; `cost_per_trade` (round-trip brokerage+taxes+slippage) is subtracted
        per trade for NET P&L. source='index' uses the NIFTY 50 index (continuous multi-year
        1-min history) as a proxy; 'future' uses the selected contract (limited to its lifespan).
        Pass brick_sizes=[50,75,100] to sweep and compare."""
        if not self.broker.connected:
            return {"ok": False, "error": "Connect Angel One first."}
        lot = self.settings["lot_size"]
        entry_bricks = max(1, int(entry_bricks)); exit_bricks = max(1, int(exit_bricks))
        cost_per_trade = float(cost_per_trade or 0.0); trend_ema = max(0, int(trend_ema or 0))
        bricks = [int(b) for b in (brick_sizes or [brick_size or self.settings["brick_size"]]) if int(b) > 0]
        if not bricks:
            return {"ok": False, "error": "No valid brick size"}
        now = datetime.now(IST)
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=9, minute=15, tzinfo=IST) \
                if from_date else (now - timedelta(days=int(days))).replace(hour=9, minute=15)
        except Exception:
            return {"ok": False, "error": "Bad from_date (use YYYY-MM-DD)"}
        end = now
        if to_date:
            try:
                end = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
            except Exception:
                return {"ok": False, "error": "Bad to_date (use YYYY-MM-DD)"}
        if end <= start:
            return {"ok": False, "error": "to_date must be after from_date"}
        use_index = (source == "index")
        max_days = 760 if use_index else 70     # index has long history; a future does not
        if (end - start).days > max_days:
            start = end - timedelta(days=max_days)
        exch = "NSE" if use_index else None
        token = self.NIFTY_INDEX_TOKEN if use_index else None
        symbol = "NIFTY 50 (index)" if use_index else self.broker.fut_symbol

        # fetch candles once (Angel caps 1-min history per request → paginate in ~25-day chunks)
        all_candles, seen, cur, calls = [], set(), start, 0
        while cur < end and calls < 70:
            chunk_end = min(cur + timedelta(days=25), end)
            candles = await asyncio.to_thread(self.broker.get_history, "ONE_MINUTE",
                          cur.strftime("%Y-%m-%d %H:%M"), chunk_end.strftime("%Y-%m-%d %H:%M"),
                          exch, token)
            calls += 1
            if candles:
                for c in candles:
                    if c[0] not in seen:
                        seen.add(c[0]); all_candles.append(c)
            cur = chunk_end + timedelta(minutes=1)
            await asyncio.sleep(0.35)
        if not all_candles:
            return {"ok": False, "error": self.broker.error or "No historical candles returned"}
        all_candles.sort(key=lambda c: c[0])

        # Variant matrix: fetch once, simulate many strategy configs for comparison.
        if variants:
            matrix = []
            for v in variants:
                s, _tr = self._simulate(all_candles, int(v.get("brick_size") or self.settings["brick_size"]),
                                      lot, int(v.get("entry_bricks", entry_bricks)),
                                      int(v.get("exit_bricks", exit_bricks)),
                                      float(v.get("cost_per_trade", cost_per_trade)),
                                      int(v.get("trend_ema", trend_ema)),
                                      int(v.get("lookback", 0)),
                                      bool(v.get("range_breakout", False)),
                                      bool(v.get("chop_filter", False)),
                                      float(v.get("chop_thr", 0.3)),
                                      bool(v.get("structure", False)),
                                      bool(v.get("exit_chop", False)),
                                      float(v.get("exit_thr", 0.30)),
                                      bool(v.get("er_adaptive", False)),
                                      int(v.get("adapt_window", 120)),
                                      float(v.get("adapt_pct", 70)),
                                      bool(v.get("er_rising", False)),
                                      int(v.get("rise_gap", 3)))
                s["label"] = v.get("label") or (f"bs{s['brick_size']} e{s['entry_bricks']}"
                                                 f"/x{s['exit_bricks']}" + (f" ema{s['trend_ema']}" if s['trend_ema'] else ""))
                if v.get("include_trades"):
                    s["trades_log"] = _tr
                matrix.append(s)
            matrix.sort(key=lambda r: r["net_pnl"], reverse=True)
            return {"ok": True, "matrix": matrix,
                    "params": {"lot_size": lot, "source": source, "symbol": symbol,
                               "from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"),
                               "candles": len(all_candles)}}

        results = []
        best_trades = None
        # Mirror the LIVE strategy so plain backtests match reality: the ER chop filter is
        # mandatory live, so apply it here too (using the current live lookback/threshold).
        live_lb = max(2, int(self.settings.get("chop_lookback", 50) or 50))
        live_thr = max(0.05, float(self.settings.get("chop_threshold", 0.30) or 0.30))
        live_entry = max(1, int(self.settings.get("entry_bricks", 2) or 2))
        eb = entry_bricks or live_entry
        for bs in bricks:
            summary, trades = self._simulate(all_candles, bs, lot, eb, exit_bricks,
                                             cost_per_trade, trend_ema,
                                             lookback=live_lb, chop_filter=True, chop_thr=live_thr)
            results.append(summary)
            if best_trades is None or summary["net_pnl"] > best_trades[0]["net_pnl"]:
                best_trades = (summary, trades)
        best_bs = max(results, key=lambda r: r["net_pnl"])["brick_size"] if results else None
        params = {"lot_size": lot, "entry_bricks": entry_bricks, "exit_bricks": exit_bricks,
                  "cost_per_trade": cost_per_trade, "trend_ema": trend_ema,
                  "source": source, "symbol": symbol,
                  "from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"),
                  "candles": len(all_candles)}
        if len(bricks) == 1:
            # single-brick: keep the original shape (summary + trades) for the simple view
            summary, trades = best_trades
            return {"ok": True, "summary": summary, "trades": trades[-1000:],
                    "params": {**params, "brick_size": bricks[0]}}
        # sweep: comparison table + trades/equity of the best brick size
        summary, trades = best_trades
        return {"ok": True, "sweep": results, "best_brick_size": best_bs,
                "best_summary": summary, "best_trades": trades[-1000:], "params": params}

    async def analyze_skips(self, days=10, source="future"):
        """Replay recent REAL candles through the CURRENT live strategy (entry_bricks + ER chop
        filter, exit on 1st opposite brick) and report which entry signals were TAKEN vs SKIPPED
        by the ER filter. For each skipped signal, computes the counterfactual outcome (points if
        we HAD entered and exited on the next opposite brick) so you can see whether skipping was
        the right call. Diagnostic only — never places an order."""
        if not self.broker.connected:
            return {"ok": False, "error": "Connect Angel One first."}
        bs = self.settings["brick_size"]
        need = max(1, int(self.settings.get("entry_bricks", 2) or 2))
        lb = max(2, int(self.settings.get("chop_lookback", 50) or 50))
        thr = float(self.settings.get("chop_threshold", 0.30) or 0.30)
        lot = self.settings["lot_size"]
        use_index = (source == "index")
        exch = "NSE" if use_index else None
        token = self.NIFTY_INDEX_TOKEN if use_index else None
        symbol = "NIFTY 50 (index)" if use_index else self.broker.fut_symbol
        now = datetime.now(IST)
        start = now - timedelta(days=int(days))
        all_candles, seen, cur, calls = [], set(), start, 0
        while cur < now and calls < 10:
            chunk_end = min(cur + timedelta(days=25), now)
            candles = await asyncio.to_thread(self.broker.get_history, "ONE_MINUTE",
                          cur.strftime("%Y-%m-%d %H:%M"), chunk_end.strftime("%Y-%m-%d %H:%M"),
                          exch, token)
            calls += 1
            if candles:
                for c in candles:
                    if c[0] not in seen:
                        seen.add(c[0]); all_candles.append(c)
            cur = chunk_end + timedelta(minutes=1)
            await asyncio.sleep(0.3)
        if not all_candles:
            return {"ok": False, "error": self.broker.error or "No historical candles returned"}
        all_candles.sort(key=lambda c: c[0])

        # build the Renko bricks (same close-based traditional Renko as the live engine)
        state = {"v": None, "d": 0}
        bricks = []
        for c in all_candles:
            price = float(c[4]); ts = c[0]
            if state["v"] is None:
                state["v"] = round(price / bs) * bs; state["d"] = 0; continue
            n = int((price - state["v"]) / bs)
            if n == 0:
                continue
            s = 1 if n > 0 else -1
            if state["d"] == 0 or s == state["d"]:
                count = abs(n)
            else:
                if abs(n) < 2:
                    continue
                count = abs(n) - 1; state["v"] += s * bs
            for _ in range(count):
                c2 = state["v"] + s * bs; state["v"] = c2; state["d"] = s
                bricks.append({"color": "green" if s > 0 else "red", "close": round(c2, 2), "time": ts})

        closes = [b["close"] for b in bricks]

        def er_at(i):
            if i < lb:
                return None
            seq = closes[i - lb:i + 1]
            path = sum(abs(seq[k] - seq[k - 1]) for k in range(1, len(seq)))
            return abs(seq[-1] - seq[0]) / path if path else 0.0

        def counterfactual(i, side):
            entry = closes[i]
            for j in range(i + 1, len(bricks)):
                if (side == "SHORT" and bricks[j]["color"] == "green") or \
                   (side == "LONG" and bricks[j]["color"] == "red"):
                    ex = closes[j]
                    return round((entry - ex) if side == "SHORT" else (ex - entry), 2)
            return None   # still open at end of window

        consec_red = consec_green = 0
        position = None
        taken, skipped = [], []
        for i, b in enumerate(bricks):
            color = b["color"]
            if color == "red":
                consec_red += 1; consec_green = 0
            else:
                consec_green += 1; consec_red = 0
            if position:
                if (position == "SHORT" and color == "green") or (position == "LONG" and color == "red"):
                    position = None
                continue
            sig = "SHORT" if (color == "red" and consec_red >= need) else \
                  ("LONG" if (color == "green" and consec_green >= need) else None)
            if not sig:
                continue
            er = er_at(i)
            if er is not None and er >= thr:
                position = sig
                taken.append({"time": b["time"], "side": sig, "price": b["close"],
                              "er": round(er, 3)})
            else:
                pts = counterfactual(i, sig)
                skipped.append({"time": b["time"], "side": sig, "price": b["close"],
                                "er": (round(er, 3) if er is not None else None),
                                "would_be_points": pts,
                                "would_be_pnl": (round(pts * lot, 2) if pts is not None else None),
                                "verdict": ("good skip (would have LOST)" if (pts is not None and pts < 0)
                                            else "missed win" if (pts is not None and pts > 0)
                                            else "neutral/warming-up")})
        good = sum(1 for s in skipped if s["would_be_points"] is not None and s["would_be_points"] < 0)
        missed = sum(1 for s in skipped if s["would_be_points"] is not None and s["would_be_points"] > 0)
        saved_pts = -sum(s["would_be_points"] for s in skipped
                         if s["would_be_points"] is not None and s["would_be_points"] < 0)
        missed_pts = sum(s["would_be_points"] for s in skipped
                         if s["would_be_points"] is not None and s["would_be_points"] > 0)
        return {"ok": True,
                "params": {"symbol": symbol, "source": source, "brick_size": bs, "entry_bricks": need,
                           "chop_threshold": thr, "chop_lookback": lb, "lot_size": lot,
                           "from": start.strftime("%Y-%m-%d"), "to": now.strftime("%Y-%m-%d"),
                           "candles": len(all_candles), "bricks": len(bricks)},
                "summary": {"signals": len(taken) + len(skipped), "taken": len(taken),
                            "skipped": len(skipped), "good_skips": good, "missed_wins": missed,
                            "loss_points_avoided": round(saved_pts, 2),
                            "win_points_missed": round(missed_pts, 2),
                            "net_points_from_filter": round(saved_pts - missed_pts, 2),
                            "net_rupees_from_filter": round((saved_pts - missed_pts) * lot, 2)},
                "taken": taken[-100:], "skipped": skipped[-100:]}



    def _new_brick(self, color, o, c, ts=None):
        self.brick_seq += 1
        b = {"index": self.brick_seq, "color": color, "open": round(o, 2),
             "close": round(c, 2), "time": ts or now_iso(), "signal": None}
        self.bricks.append(b)
        if len(self.bricks) > 1500:
            self.bricks = self.bricks[-1500:]
        return b

    # -------- strategy --------
    def _chop_ok(self):
        """Efficiency-ratio (ER) entry gate. ER = |net move| / total path over the last
        `chop_lookback` brick closes. Low ER = choppy/ranging (block entries); high ER = trending.
        Returns (allowed: bool, er: float|None). If the filter is off, always allowed."""
        if not self.settings.get("chop_filter"):
            return True, None
        lb = max(2, int(self.settings.get("chop_lookback", 50) or 50))
        closes = [b["close"] for b in self.bricks[-(lb + 1):]]
        if len(closes) < lb + 1:
            return False, None            # not enough history yet — sit out
        path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        er = abs(closes[-1] - closes[0]) / path if path else 0.0
        thr = max(0.05, float(self.settings.get("chop_threshold", 0.30) or 0.30))
        return er >= thr, round(er, 3)

    def er_projection(self, max_bricks=15):
        """Forward projection: from the CURRENT bricks, simulate consecutive same-colour bricks
        (each a fixed brick_size step) on both sides and find the first price at which the ER
        entry gate (>= threshold) AND the consecutive-brick entry rule are BOTH satisfied.
        Lets the UI mark the 'LONG arms @ price' and 'SHORT arms @ price' levels ahead of time.
        Purely informational — recomputes every time a new brick forms (window slides)."""
        bs = int(self.settings.get("brick_size", 50) or 50)
        lb = max(2, int(self.settings.get("chop_lookback", 50) or 50))
        thr = max(0.05, float(self.settings.get("chop_threshold", 0.30) or 0.30))
        need = max(1, int(self.settings.get("entry_bricks", 2) or 2))
        on = bool(self.settings.get("chop_filter"))
        closes = [b["close"] for b in self.bricks]
        last = closes[-1] if closes else round(self.price, 2)
        cur_er = self._chop_ok()[1] if on else None

        def _er(seq):
            w = seq[-(lb + 1):]
            if len(w) < lb + 1:
                return None
            p = sum(abs(w[i] - w[i - 1]) for i in range(1, len(w)))
            return abs(w[-1] - w[0]) / p if p else 0.0

        def project(step, base_consec):
            seq = list(closes)
            price = last
            for k in range(1, max_bricks + 1):
                price = round(price + step, 2)
                seq.append(price)
                consec = base_consec + k
                e = _er(seq)
                gate = (e is not None and e >= thr) if on else True
                if gate and consec >= need:
                    return {"unlock_price": price, "bricks": k,
                            "er": (round(e, 3) if e is not None else None)}
            return {"unlock_price": None, "bricks": None, "er": None}

        return {
            "enabled": on,
            "current_er": cur_er,
            "threshold": round(thr, 3),
            "entry_bricks": need,
            "brick_size": bs,
            "last_close": last,
            "long": project(bs, self.consec_green),
            "short": project(-bs, self.consec_red),
        }

    def _process_brick(self, brick):
        color = brick["color"]
        if color == "red":
            self.consec_red += 1
            self.consec_green = 0
        else:
            self.consec_green += 1
            self.consec_red = 0

        # Deepen the trend-run the current (or pending) position is riding (informational only —
        # the exit no longer depends on run length; the 1st opposite brick always exits).
        held_side = self.position["side"] if self.position \
            else (self._entry_side if self.pending_entry else None)
        if held_side and ((held_side == "SHORT" and color == "red")
                          or (held_side == "LONG" and color == "green")):
            self.down_run_reds += 1

        # ---- EXIT: the FIRST opposite brick always closes the position (capture the move
        # early), no matter how long the favourable run was. ----
        if self.position and not self.pending_exit:
            if self.position["side"] == "SHORT" and color == "green":
                self.pending_exit = True
                brick["signal"] = "COVER"
                asyncio.create_task(self._execute_order("BUY", "EXIT", self.price, brick["index"]))
                return
            if self.position["side"] == "LONG" and color == "red":
                self.pending_exit = True
                brick["signal"] = "EXIT_LONG"
                asyncio.create_task(self._execute_order("SELL", "EXIT", self.price, brick["index"]))
                return

        # ---- ENTRY: `entry_bricks` same-direction bricks while flat (symmetric long & short),
        # gated by the ER chop filter (skip choppy/ranging conditions). Flips immediately after an
        # exit once the opposite setup is met. ----
        if not (self.position or self.pending_entry) and not self._entries_blocked():
            need = max(1, int(self.settings.get("entry_bricks", 2) or 2))
            chop_ok, _er = self._chop_ok()
            if color == "red" and self.consec_red >= need and chop_ok:
                self.down_run_reds = self.consec_red
                self.pending_entry = True
                self._entry_side = "SHORT"
                brick["signal"] = "SHORT"
                asyncio.create_task(self._execute_order("SELL", "ENTRY", self.price, brick["index"]))
            elif color == "green" and self.consec_green >= need and chop_ok:
                self.down_run_reds = self.consec_green
                self.pending_entry = True
                self._entry_side = "LONG"
                brick["signal"] = "LONG"
                asyncio.create_task(self._execute_order("BUY", "ENTRY", self.price, brick["index"]))

    # -------- order execution: escalating limit with hard slippage cap + retries --------
    async def _cur_price(self):
        if self.feed_mode == "LIVE" and self.broker.connected:
            ltp = await asyncio.to_thread(self.broker.get_ltp)
            if ltp is not None and ltp > 0:
                return ltp
        return self.price

    def _order_key(self, kind, reason, brick_index):
        """Deterministic client order id. A brick-triggered signal (ENTRY / opposite-brick EXIT)
        fires exactly once for a given brick, so two pods computing it produce the SAME key ->
        the duplicate is suppressed. Retries / forced exits (no brick) use an 8s dedup window,
        so a genuine sequential retry (>=15s later) still goes through. The contract token is
        included so a brick_seq reset after a rollover/instrument change cannot collide with an
        earlier same-day key (token is resolved identically on every pod, so cross-pod dedup
        still holds)."""
        today = datetime.now(IST).date().isoformat()
        tok = str(getattr(self.broker, "fut_token", "") or "")
        if brick_index is not None and brick_index >= 0:
            return f"{kind}:{today}:{tok}:b{brick_index}"
        return f"{kind}:{reason}:{today}:{tok}:t{int(time.time() // 8)}"

    async def _claim_order_key(self, key):
        """Persist the client order id BEFORE the broker call. Returns True if this order may
        be placed, False if it was already placed (prevents duplicate REAL orders across pods
        / restarts / leader-failover)."""
        try:
            await self.db.order_keys.insert_one(
                {"_id": key, "created": datetime.now(timezone.utc), "by": INSTANCE_ID})
            return True
        except Exception:
            logger.warning("Idempotency guard: order key exists, suppressing duplicate -> %s", key)
            return False

    async def _execute_order(self, side, kind, ref_price, brick_index, reason="SIGNAL"):
        # Duplicate-order protection: only one order in-flight; re-validate inside the lock.
        flip_side = None
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

            # Size EXIT orders to the ACTUAL open quantity — a reconciled/adopted or
            # carry-forward position may be more than one lot, so exiting only lot_size would
            # leave a naked remainder (bot thinks flat while the broker still holds qty).
            # ENTRY orders are always exactly one lot. Never exceed the hard safety cap.
            order_qty = self.settings["lot_size"] if kind == "ENTRY" \
                else int(self.position["qty"])
            order_qty = max(1, min(int(order_qty), MAX_ORDER_QTY))

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
                "qty": order_qty, "symbol": self.settings["symbol"],
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

            # Idempotency: persist a deterministic client order id BEFORE the broker call.
            # If it already exists (a leader-failover race placed it), suppress the duplicate.
            okey = self._order_key(kind, reason, brick_index)
            order["client_order_id"] = okey
            if not await self._claim_order_key(okey):
                order["status"] = "SKIPPED_DUPLICATE"
                order["note"] = "Duplicate suppressed by idempotency guard (order already placed)."
                if kind == "ENTRY":
                    self.pending_entry = False
                else:
                    self.pending_exit = False
                    self.exit_retry_pending = True   # keep the position exitable next window
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
                # GAP FLIP: if a strategy exit just filled and the market has ALREADY printed
                # >=2 consecutive opposite bricks (e.g. a gap up/down), open the reversal now —
                # don't wait for another brick. Skipped for forced exits (expiry/breaker/manual)
                # and while entries are blocked.
                if kind == "EXIT" and reason == "SIGNAL" and self.position is None \
                        and not self.pending_entry and not self._entries_blocked() \
                        and self._chop_ok()[0]:
                    need = max(1, int(self.settings.get("entry_bricks", 2) or 2))
                    if self.consec_red >= need:
                        self._entry_side, flip_side = "SHORT", "SELL"
                        self.down_run_reds = self.consec_red
                        self.pending_entry = True
                    elif self.consec_green >= need:
                        self._entry_side, flip_side = "LONG", "BUY"
                        self.down_run_reds = self.consec_green
                        self.pending_entry = True
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
        if flip_side:
            self._set_alert(f"Gap reversal — market already made 2+ opposite bricks; "
                            f"flipping to {self._entry_side} immediately after exit.", "info")
            await self._execute_order(flip_side, "ENTRY", self.price, -1, "GAP_FLIP")

    def _set_alert(self, msg, level="info"):
        self.alert = {"id": str(uuid.uuid4()), "msg": msg, "level": level, "time": now_iso()}
        self._alert_ts = time.time()
        logger.warning("ALERT(%s): %s", level, msg)

    async def _live_fill(self, order, side, base, cap, max_attempts, retry_secs, floor, ceil):
        """Real Angel One LIMIT order with escalating buffer + re-pricing.
        One broker order id is kept and modified across retries to avoid double fills;
        if it gets rejected/cancelled we place a fresh one. Unfilled orders are cancelled."""
        angel_side = "SELL" if side == "SELL" else "BUY"
        qty = order["qty"]
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
            # Race guard: the order may have filled in the moment before/at cancel. Re-read the
            # status; if it actually completed, treat it as FILLED (don't report a false reject —
            # that would desync bot vs broker and could trigger a wrong extra order).
            st = await asyncio.to_thread(self.broker.get_order_status, broker_id)
            s = (st.get("status") or "").lower()
            if "complet" in s or "filled" in s or "executed" in s:
                order["fill_price"] = st.get("avgprice") or limit
                order["note"] = f"LIVE filled @ {order['fill_price']} just before cancel (order {broker_id})"
                return True
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
        bot_open = bool(self.position)
        if bot_open and broker_qty == 0:
            state, msg = "ENTRY_MISSED", ("Bot shows an open position but Angel One shows NO position - "
                                          "the entry order never executed. You can re-enter the trade.")
        elif (not bot_open) and broker_qty != 0:
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
            side = self.position["side"]
            reds = self.position.get("reds_at_entry", 2)
            self.position = None
            self.pending_entry = True
            self._entry_side = side
            self.down_run_reds = reds
            order_side = "SELL" if side == "SHORT" else "BUY"
            asyncio.create_task(self._execute_order(order_side, "ENTRY", self.price, -1, "RECONCILE_REENTRY"))
            await self._persist_state()
            return {"ok": True, "message": f"Re-entry {side} order placed."}
        if action == "reexit":
            np = await asyncio.to_thread(self.broker.get_net_position)
            netqty = int(np.get("netqty", 0))
            qty = abs(netqty)
            if qty == 0:
                return {"ok": False, "message": "Angel One shows no open position to exit."}
            side = "SHORT" if netqty < 0 else "LONG"
            self.position = {
                "side": side, "qty": qty, "entry_price": np.get("avgprice") or self.price,
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
            netqty = int(np.get("netqty", 0))
            qty = abs(netqty)
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
            side = "SHORT" if netqty < 0 else "LONG"
            self.position = {
                "side": side, "qty": qty, "entry_price": np.get("avgprice") or self.price,
                "entry_time": now_iso(), "entry_order_id": "RECONCILE_ADOPT",
                "reds_at_entry": self.down_run_reds or 2, "unrealized_pnl": 0.0,
            }
            await self._persist_state()
            return {"ok": True, "message": f"Synced — adopted Angel One's open {side} ({qty} qty) into the bot."}
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
        # Idempotency: guard against a double-submit / cross-pod duplicate (5s window).
        mkey = f"MANUAL:{side}:{datetime.now(IST).date().isoformat()}:t{int(time.time() // 5)}"
        order["client_order_id"] = mkey
        if not await self._claim_order_key(mkey):
            order["status"] = "SKIPPED_DUPLICATE"
            order["note"] = "Duplicate manual order suppressed (already placed just now)."
            asyncio.create_task(self._save_order(order))
            return {"ok": False, "message": "Duplicate manual order suppressed (already placed just now)."}
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

    def _replay_position(self):
        """Replay the existing bricks through the SAME symmetric entry/exit rules to work out
        which position (if any) the bot should currently be holding. Returns
        (side|None, run_len, consec_red, consec_green). Used on Start to catch up to a move
        already in progress (no orders placed here)."""
        cr = cg = run = 0
        side = None
        for b in self.bricks:
            if b["color"] == "red":
                cr += 1; cg = 0
            else:
                cg += 1; cr = 0
            exited = False
            if side:
                if (side == "SHORT" and b["color"] == "red") or (side == "LONG" and b["color"] == "green"):
                    run += 1
                # exit on the FIRST opposite brick
                if side == "SHORT" and b["color"] == "green":
                    side = None; run = 0; exited = True
                elif side == "LONG" and b["color"] == "red":
                    side = None; run = 0; exited = True
            if side is None and not exited:
                need = max(1, int(self.settings.get("entry_bricks", 2) or 2))
                if b["color"] == "red" and cr >= need:
                    side = "SHORT"; run = cr
                elif b["color"] == "green" and cg >= need:
                    side = "LONG"; run = cg
        return side, run, cr, cg

    async def _maybe_enter_on_start(self):
        """On Start (catching up to a move already in progress): replay the bricks through the
        strategy. If that leaves us in a position while we're flat, enter it now at market."""
        if not self.running or self.position or self.pending_entry or self.pending_exit:
            return
        if self._entries_blocked():
            return
        side, run, cr, cg = self._replay_position()
        if not side:
            return
        # respect the ER chop filter on start too — don't enter mid-trend during chop
        if not self._chop_ok()[0]:
            self.consec_red = cr; self.consec_green = cg
            return
        self.down_run_reds = run
        self.consec_red = cr
        self.consec_green = cg
        self.pending_entry = True
        self._entry_side = side
        last_idx = self.bricks[-1]["index"] if self.bricks else -1
        order_side = "SELL" if side == "SHORT" else "BUY"
        trend = "reds" if side == "SHORT" else "greens"
        self._set_alert(f"Started mid-trend (run of {run} {trend}, reversal not confirmed) — "
                        f"entering {side} at market.", "info")
        await self._execute_order(order_side, "ENTRY", self.price, last_idx, "START_IMMEDIATE")

    def _apply_fill(self, order):
        if order["kind"] == "ENTRY":
            side = "LONG" if order["side"] == "BUY" else "SHORT"
            self.position = {
                "side": side, "qty": order["qty"], "entry_price": order["fill_price"],
                "entry_time": order["fill_time"], "entry_order_id": order["id"],
                "reds_at_entry": self.down_run_reds, "unrealized_pnl": 0.0,
            }
            self.pending_entry = False
            self.exit_retry_pending = False
            self._exit_retry_count = 0
        else:  # EXIT
            if self.position:
                side = self.position["side"]
                entry = self.position["entry_price"]
                exit_p = order["fill_price"]
                qty = order["qty"]
                pnl = round((entry - exit_p) * qty if side == "SHORT" else (exit_p - entry) * qty, 2)
                trade = {
                    "id": str(uuid.uuid4()), "side": side, "qty": qty,
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
            entry = self.position["entry_price"]
            qty = self.position["qty"]
            self.position["unrealized_pnl"] = round(
                (entry - self.price) * qty if self.position["side"] == "SHORT"
                else (self.price - entry) * qty, 2)

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
                if qty != 0:   # a position on Angel One the bot isn't tracking -> ask to adopt
                    side = "SHORT" if qty < 0 else "LONG"
                    self.pending_adoption = {"qty": abs(qty), "avgprice": np.get("avgprice"),
                                             "netqty": qty, "side": side, "declined": False}
                    self._set_alert(f"Found an existing {side} of {abs(qty)} qty on Angel One. "
                                    f"Adopt it so the bot manages the exit?", "warning")
                    await self._persist_state()
                    return
        await self._maybe_enter_on_start()

    async def _market_open_reconcile(self):
        """Once per day, the first time the market is open AND Angel One is connected,
        auto-reconcile: if the broker holds a position the bot isn't tracking (e.g. a manual
        short carried overnight), surface it for adoption so the bot never stacks a new short
        on top of it. Fully defensive — any failure here must NEVER block the strategy loop
        (brick building), so it swallows its own errors and only ever attempts once per day."""
        today = datetime.now(IST).date().isoformat()
        if self._open_recon_date == today:
            return
        if not self.broker.connected:
            return
        # Mark done up-front so this runs at most ONCE per day even if the broker read fails —
        # a failed safety check must not turn into a per-tick network call that stalls bricks.
        self._open_recon_date = today
        if self.position is not None or self.pending_adoption is not None:
            return
        try:
            np = await asyncio.to_thread(self.broker.get_net_position)
        except Exception as e:
            logger.warning("market-open reconcile read failed: %s", e)
            return
        if not np.get("found"):
            return
        qty = int(np.get("netqty") or 0)
        if qty != 0:
            side = "SHORT" if qty < 0 else "LONG"
            self.pending_adoption = {"qty": abs(qty), "avgprice": np.get("avgprice"),
                                     "netqty": qty, "side": side, "declined": False}
            self._set_alert(f"Market open safety check: found an existing {side} of {abs(qty)} qty on "
                            f"Angel One (manual/carry-forward). Adopt it so the bot manages the exit — "
                            f"new entries are paused until you decide.", "warning")
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
        # adopt the position (long or short) and let the strategy manage its exit
        side = pa.get("side", "SHORT")
        self.position = {
            "side": side, "qty": pa["qty"], "entry_price": pa.get("avgprice") or self.price,
            "entry_time": now_iso(), "entry_order_id": "ADOPTED_MANUAL",
            "reds_at_entry": self.down_run_reds or 2, "unrealized_pnl": 0.0,
        }
        self.pending_adoption = None
        self.pending_entry = self.pending_exit = False
        self._set_alert(f"Adopted existing {side} ({self.position['qty']} qty @ "
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
                # arm true position-rollover: re-open the SAME side on next month after exit fills
                if self.settings.get("rollover_position", True) and self.settings.get("auto_roll", True):
                    self._rollover_armed = True
                    self._rollover_side = self.position["side"]
                logger.warning("EXPIRY square-off triggered at %s IST", self.settings["square_off_time"])
                self._force_exit("EXPIRY_SQUAREOFF")
            elif self.position is None and not self.pending_exit:
                self.squared_off_date = str(today)  # nothing to exit; just block new entries

    def _force_exit(self, reason):
        if not self.position or self.pending_exit:
            return
        self.pending_exit = True
        self.forced_exit_pending = True
        exit_side = "BUY" if self.position["side"] == "SHORT" else "SELL"
        asyncio.create_task(self._execute_order(exit_side, "EXIT", self.price, -1, reason))

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
        await self._load_history_warmup()
        # True position rollover: if we squared off an OPEN position at expiry, immediately
        # re-open the SAME side on the just-rolled next-month contract (carry across expiry).
        if self._rollover_armed:
            self._rollover_armed = False
            await self._rollover_enter()
            self._rollover_side = None

    async def _rollover_enter(self):
        """Open a fresh position (same side as the one squared off) on the newly-rolled
        next-month contract right after expiry square-off — independent of a brick signal."""
        if self.position or self.pending_entry or not self.broker.connected:
            return
        if not self._market_open():
            self._set_alert("Expiry rollover skipped — market closed; will resume next session.", "warning")
            return
        side = self._rollover_side or "SHORT"
        # treat like a standard 2-brick entry so the exit logic behaves normally
        if side == "SHORT":
            self.consec_red = self.down_run_reds = 2
            self.consec_green = 0
        else:
            self.consec_green = self.down_run_reds = 2
            self.consec_red = 0
        self.pending_entry = True
        self._entry_side = side
        cur = await self._cur_price()
        order_side = "SELL" if side == "SHORT" else "BUY"
        self._set_alert(f"Expiry rollover — opening new {side} on {self.broker.fut_symbol}.", "info")
        await self._execute_order(order_side, "ENTRY", cur, -1, "EXPIRY_ROLLOVER")

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
            # recovery-critical flags so a leader failover never loses in-flight exit/rollover intent
            "exit_retry_pending": self.exit_retry_pending,
            "forced_exit_pending": self.forced_exit_pending,
            "rollover_armed": self._rollover_armed, "rollover_side": self._rollover_side,
            "exit_retry_count": self._exit_retry_count,
            "pending_adoption": self.pending_adoption,
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
        self.settings["chop_filter"] = True   # chop filter is mandatory — never load it as off
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
        # In-flight orders cannot be trusted across a crash -> clear order-in-progress flags,
        # but RESTORE the intent flags (exit-retry / forced-exit / rollover / adoption) so a new
        # leader keeps trying to flatten/re-open. _reconcile_on_takeover() then verifies vs broker.
        self.pending_entry = self.pending_exit = False
        self.exit_retry_pending = bool(doc.get("exit_retry_pending"))
        self.forced_exit_pending = bool(doc.get("forced_exit_pending"))
        self._rollover_armed = bool(doc.get("rollover_armed"))
        self._rollover_side = doc.get("rollover_side")
        self._exit_retry_count = int(doc.get("exit_retry_count", 0) or 0)
        self.pending_adoption = doc.get("pending_adoption")
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
        """Pull real day P&L from Angel One's position() endpoint, but SPARINGLY. That endpoint
        is capped at 1 request/second by Angel One and is SHARED with get_net_position — if we
        poll it too often it throttles the whole session and starves the LTP price feed (bricks
        stop forming). So we only fetch: (a) during market hours, (b) at most every 20s, and
        (c) only while the bot is running or actually holding a position. Outside market hours
        the last in-hours value is FROZEN (Angel returns garbage P&L after close)."""
        if not self.broker.connected:
            return
        if not self._market_open():
            return
        if not (self.running or self.position):
            return
        now = time.time()
        if now - self._last_pnl_fetch < 20:
            return
        self._last_pnl_fetch = now
        try:
            pnl = await asyncio.to_thread(self.broker.get_day_pnl)
            if pnl.get("found"):
                self.broker_pnl = pnl
        except Exception as e:
            logger.warning("broker pnl refresh failed: %s", e)

    # -------- multi-pod leadership + command relay --------
    async def _acquire_leadership(self):
        """Atomically claim/renew the trading lease. Returns True iff THIS pod is leader."""
        now = time.time()
        try:
            doc = await self.db.leader_lock.find_one_and_update(
                {"_id": "trading_engine", "$or": [
                    {"expires_at": {"$lt": now}},
                    {"holder": INSTANCE_ID},
                ]},
                {"$set": {"holder": INSTANCE_ID, "expires_at": now + LEADER_LEASE_SEC,
                          "heartbeat": now_iso()}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
            return bool(doc and doc.get("holder") == INSTANCE_ID)
        except Exception:
            # upsert lost the race -> a valid holder already exists -> we are a follower
            return False

    async def _connect_broker(self):
        api_key = os.environ.get("ANGEL_API_KEY", "").strip()
        client_code = os.environ.get("ANGEL_CLIENT_CODE", "").strip()
        pin = os.environ.get("ANGEL_PIN", "").strip()
        totp_secret = os.environ.get("ANGEL_TOTP_SECRET", "").strip()
        if not all([api_key, client_code, pin, totp_secret]):
            return {"connected": False, "error": "Missing Angel One credentials in .env"}
        # Log in WITHOUT the slow (~35 MB, ~2 min) scrip-master download — that would block the
        # trading loop and time out the connect request. Resolve the instrument instantly from the
        # persisted cache, then refresh the scrip master in the background.
        res = await asyncio.to_thread(self.broker.login, api_key, client_code, pin, totp_secret, False)
        if res.get("connected"):
            cache = await self.db.scrip_cache.find_one({"_id": "nfo_futures"})
            if cache and cache.get("rows"):
                self.broker.restore_futures(cache["rows"])
            saved = self.settings.get("instrument_token")
            if saved and self.broker.futures:
                sel = self.broker.select_instrument(saved)
                if sel.get("ok"):
                    res["future"] = sel["symbol"]
            if self.broker.fut_token:
                await asyncio.to_thread(self.broker.start_feed)
                res["future"] = self.broker.fut_symbol
            else:
                self._set_alert("Connected to Angel One — loading the instrument list "
                                "(first-time download, may take up to ~2 min). Trading will arm "
                                "as soon as the NIFTY contract resolves.", "info")
            self.feed_mode = "LIVE"
            # Background: (re)download the scrip master to pick up new contracts + persist the cache.
            asyncio.create_task(self._refresh_scrip_master())
            logger.info("Leader connected to Angel One; LIVE feed active (contract=%s)",
                        self.broker.fut_symbol or "PENDING")
        return res

    async def _refresh_scrip_master(self):
        """Download the Angel scrip master OFF the hot path, refresh the tradable-futures cache,
        persist it to Mongo, and (if no contract was resolved from cache yet) select the default
        NIFTY future and start the feed. Never blocks connect/trading."""
        try:
            had_token = bool(self.broker.fut_token)
            ok = await asyncio.to_thread(self.broker._resolve_nifty_fut)
            if not ok or not self.broker.futures:
                return
            try:
                await self.db.scrip_cache.replace_one(
                    {"_id": "nfo_futures"},
                    {"_id": "nfo_futures", "rows": self.broker.snapshot_futures(),
                     "ts": now_iso()}, upsert=True)
            except Exception as e:
                logger.warning("scrip cache save failed: %s", e)
            # Cold start (no cache) — a contract just got resolved. Honour any saved selection,
            # then start the live feed now that we finally have a token.
            if not had_token and self.broker.fut_token:
                saved = self.settings.get("instrument_token")
                if saved:
                    self.broker.select_instrument(saved)
                await asyncio.to_thread(self.broker.start_feed)
                self._set_alert(f"Instrument list loaded — trading contract "
                                f"{self.broker.fut_symbol}. Bot is armed.", "info")
                await self._persist_state()
        except Exception as e:
            logger.warning("scrip master refresh failed: %s", e)

    async def _maybe_daily_scrip_refresh(self):
        """Once per IST day (while connected), refresh the scrip-master cache in the background so
        a newly listed month's contract is always available before market open. Never changes the
        currently selected contract (that only rolls at expiry) — it just keeps the cache current."""
        if not self.broker.connected:
            return
        today = datetime.now(IST).date().isoformat()
        if self._scrip_refresh_date == today:
            return
        self._scrip_refresh_date = today
        logger.info("Daily scrip-master refresh (contract freshness)")
        asyncio.create_task(self._refresh_scrip_master())

    async def _on_become_leader(self):
        logger.info("BECAME LEADER (%s)", INSTANCE_ID)
        await self._load_state()      # pick up the latest state persisted by the previous leader
        # Angel One allows only ONE live session per account. To avoid preview & production
        # fighting over the single session, the leader grabs the broker session ONLY when the
        # bot is actually running (started). A stopped bot (e.g. the preview/dev environment)
        # never connects, leaving the session free for the running (production) environment.
        # The user can still connect manually anytime via the "connect" command.
        if self.running:
            await self._connect_broker()  # only a RUNNING leader holds an Angel session
            await self._reconcile_on_takeover()

    async def _reconcile_on_takeover(self):
        """After a leader failover, the previous leader may have had an exit/entry in flight or an
        expiry square-off armed. Reconcile our restored `position` against Angel One's ACTUAL net
        position so we never strand or double a real trade."""
        try:
            if not (self.broker.connected and self.broker.fut_token):
                return
            np = await asyncio.to_thread(self.broker.get_net_position)
            if not np.get("found"):
                return
            broker_qty = int(np.get("netqty", 0))
            if self.position and broker_qty == 0:
                # the exit filled (or entry never did) during the handover -> we are truly flat
                logger.warning("TAKEOVER: bot had a %s but broker is flat -> clearing position",
                               self.position.get("side"))
                self.position = None
                self.pending_exit = self.exit_retry_pending = self.forced_exit_pending = False
                self._exit_retry_count = 0
            elif (not self.position) and broker_qty != 0:
                # broker holds a position we lost track of -> adopt it so the strategy manages the exit
                side = "SHORT" if broker_qty < 0 else "LONG"
                self.position = {"side": side, "qty": abs(broker_qty),
                                 "entry_price": np.get("avgprice") or self.price,
                                 "entry_time": now_iso(), "entry_order_id": "TAKEOVER_ADOPT",
                                 "reds_at_entry": self.down_run_reds or 2, "unrealized_pnl": 0.0}
                logger.warning("TAKEOVER: adopted untracked broker %s (%d qty)", side, abs(broker_qty))
            elif self.position and broker_qty != 0:
                self.position["qty"] = abs(broker_qty)   # trust the broker's size
                # if an exit/square-off was in progress, keep retrying it under the new leader
                if self.forced_exit_pending or self.exit_retry_pending or \
                        self.squared_off_date == str(datetime.now(IST).date()):
                    self.exit_retry_pending = True
                    logger.warning("TAKEOVER: %s still open with exit pending -> re-arming exit",
                                   self.position.get("side"))
            await self._persist_state()
        except Exception as e:
            logger.exception("takeover reconcile failed: %s", e)

    async def _on_lose_leadership(self):
        logger.info("LOST LEADERSHIP (%s) — disconnecting Angel", INSTANCE_ID)
        try:
            await asyncio.to_thread(self.broker.logout)
        except Exception:
            pass

    async def _publish_snapshot(self):
        """Leader writes the authoritative snapshot to Mongo so EVERY pod serves identical
        /state (kills the cross-pod flicker)."""
        try:
            await self.db.live_state.replace_one(
                {"_id": "singleton"},
                {"_id": "singleton", "snapshot": self.snapshot(), "ts": time.time(),
                 "leader": INSTANCE_ID},
                upsert=True)
        except Exception as e:
            logger.warning("publish snapshot failed: %s", e)

    async def _process_commands(self):
        """Leader drains user actions relayed from any pod, executes them sequentially
        (no order interleaving), and writes back the result."""
        pending = await self.db.commands.find({"status": "pending"}).sort("created_ts", 1).to_list(20)
        for c in pending:
            claimed = await self.db.commands.find_one_and_update(
                {"_id": c["_id"], "status": "pending"},
                {"$set": {"status": "processing", "claimed_by": INSTANCE_ID}})
            if not claimed:
                continue
            # These commands can run for many seconds (fetching lots of candles) — run them in
            # the background so they NEVER pause the live trading loop. All are read-only (no orders).
            if c["type"] in ("backtest", "load_history", "analyze_skips"):
                asyncio.create_task(self._run_command_bg(c))
                continue
            try:
                result = await self._run_command(c["type"], c.get("payload") or {})
            except Exception as e:
                logger.exception("command %s failed", c.get("type"))
                result = {"ok": False, "message": safe_err(str(e))}
            await self.db.commands.update_one(
                {"_id": c["_id"]}, {"$set": {"status": "done", "result": result, "done_at": now_iso()}})

    async def _run_command_bg(self, c):
        try:
            result = await self._run_command(c["type"], c.get("payload") or {})
        except Exception as e:
            logger.exception("bg command %s failed", c.get("type"))
            result = {"ok": False, "message": safe_err(str(e))}
        await self.db.commands.update_one(
            {"_id": c["_id"]}, {"$set": {"status": "done", "result": result, "done_at": now_iso()}})

    async def _do_select_instrument(self, token):
        res = self.broker.select_instrument(token)
        if res.get("ok"):
            self.settings["instrument_token"] = token
            self.anchor = None
            self.direction = 0
            self.bricks = []
            self.brick_seq = 0
            self.consec_red = self.consec_green = self.down_run_reds = 0
            await self._persist_state()
            # warm up the ER window for the new contract in the background (don't block the response)
            asyncio.create_task(self._load_history_warmup())
        return res

    async def _run_command(self, ctype, payload):
        if ctype == "start":
            self.running = True
            await self._persist_state()
            # Connect to Angel One on Start (the leader only holds a broker session while
            # running). If another environment currently holds the single Angel session this
            # login may fail — surfaced to the user, who can retry once the other side frees it.
            if not self.broker.connected:
                await self._connect_broker()
            await self._on_start()
            return {"running": True, "angel_connected": self.broker.connected}
        if ctype == "stop":
            squared = False
            if payload.get("square_off") and self.position and not self.pending_exit:
                self._force_exit("MANUAL_SQUAREOFF")
                squared = True
            self.running = False
            await self._persist_state()
            return {"running": False, "squared_off": squared}
        if ctype == "settings":
            self.settings.update(payload.get("data") or {})
            self.settings["chop_filter"] = True   # mandatory — cannot be disabled
            await self._persist_state()
            return {"ok": True, "settings": self.settings, **self.settings}
        if ctype == "adopt":
            return await self.adopt_position(bool(payload.get("confirm")))
        if ctype == "reconcile":
            return await self.reconcile()
        if ctype == "reconcile_resolve":
            return await self.reconcile_resolve((payload.get("action") or "").lower())
        if ctype == "manual_order":
            return await self.manual_order((payload.get("side") or "").upper(), payload.get("qty"))
        if ctype == "connect":
            return await self._connect_broker()
        if ctype == "disconnect":
            await asyncio.to_thread(self.broker.logout)
            return {"connected": False, "feed_mode": "LIVE"}
        if ctype == "load_history":
            return await self.load_history(days=int(payload.get("days", 5)),
                                           from_date=payload.get("from_date"))
        if ctype == "analyze_skips":
            return await self.analyze_skips(days=int(payload.get("days", 10)),
                                            source=payload.get("source", "future"))
        if ctype == "backtest":
            return await self.backtest(from_date=payload.get("from_date"),
                                       to_date=payload.get("to_date"),
                                       brick_size=payload.get("brick_size"),
                                       brick_sizes=payload.get("brick_sizes"),
                                       days=int(payload.get("days", 30)),
                                       source=payload.get("source", "future"),
                                       entry_bricks=int(payload.get("entry_bricks", 2)),
                                       exit_bricks=int(payload.get("exit_bricks", 1)),
                                       cost_per_trade=float(payload.get("cost_per_trade", 0) or 0),
                                       trend_ema=int(payload.get("trend_ema", 0) or 0),
                                       variants=payload.get("variants"))
        if ctype == "instruments":
            if not self.broker.connected:
                return {"ok": False, "error": "Connect Angel One first.", "items": []}
            return {"ok": True, "items": self.broker.search_futures(payload.get("q", ""))}
        if ctype == "select_instrument":
            return await self._do_select_instrument(str(payload.get("token", "")))
        if ctype == "reset":
            self.reset()
            await self.db.trades.delete_many({})
            await self.db.order_log.delete_many({})
            await self.db.engine_state.delete_one({"_id": "singleton"})
            await self.load_metrics()
            return {"ok": True}
        if ctype == "square_off":
            if not self.position:
                return {"ok": False, "message": "No open position to square off."}
            self._force_exit("MANUAL_SQUAREOFF")
            return {"ok": True, "message": "Square-off order placed."}
        if ctype == "arm":
            self.breaker_tripped = False
            self.day_key = datetime.now(IST).date().isoformat()
            await self._persist_state()
            return {"ok": True, "message": "Circuit breaker re-armed."}
        if ctype == "clear_order_log":
            res = await self.db.order_log.delete_many({})
            self.orders = []
            return {"ok": True, "cleared": res.deleted_count}
        return {"ok": False, "message": f"Unknown command: {ctype}"}

    # -------- main loop --------
    # Ticks accumulate into a bar; the Renko bricks are evaluated ONLY on bar close
    # (every bar_seconds), using that bar's close price - just like TradingView 1m Renko.
    async def run_loop(self):
        while True:
            try:
                leader = await self._acquire_leadership()
                if leader and not self.is_leader:
                    self.is_leader = True
                    await self._on_become_leader()
                elif not leader and self.is_leader:
                    self.is_leader = False
                    await self._on_lose_leadership()
                self.is_leader = leader
                if not self.is_leader:
                    await asyncio.sleep(self.settings["tick_interval"])
                    continue
                await self._process_commands()
                await self._refresh_broker_pnl()
                if self.running:
                    await self._maybe_daily_scrip_refresh()   # keep the contract list fresh each morning
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
                            self.ticks_in_bar += 1
                            self._update_unrealized()
                            if self.ticks_in_bar >= self.settings["bar_seconds"]:
                                self.ticks_in_bar = 0
                                for b in self._feed_close(self.price):   # feed the BAR CLOSE
                                    self._process_brick(b)
                            self._maybe_square_off()
                            self._check_circuit_breaker()
                            self._maybe_auto_roll()
                            # One-time daily safety reconcile at market open: adopt any
                            # untracked broker position (manual/carry-forward) so the bot
                            # never stacks a new short on top of it. Runs AFTER brick building
                            # and is fully self-guarded so it can never block the strategy.
                            await self._market_open_reconcile()
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
                                        # side depends on the OPEN position: BUY covers a short,
                                        # SELL closes a long. (Hard-coding BUY would DOUBLE a long.)
                                        retry_side = "BUY" if self.position["side"] == "SHORT" else "SELL"
                                        asyncio.create_task(self._execute_order(retry_side, "EXIT", self.price, -1, "EXIT_RETRY"))
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
                # leader publishes the authoritative snapshot every tick so all pods agree
                await self._publish_snapshot()
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
        self._entry_side = None
        self.exit_retry_pending = False
        self._exit_retry_count = 0
        self._rollover_armed = False
        self._rollover_side = None
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
            "is_leader": self.is_leader,
            "instance_id": INSTANCE_ID,
            "market_open": self._market_open(),
            "pending_adoption": self.pending_adoption,
            "feed_mode": self.feed_mode,
            "feed_error": self.feed_error,
            "alert": self.alert if (self.alert and time.time() - self._alert_ts < ALERT_TTL_SEC) else None,
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
            "chop_filter": bool(self.settings.get("chop_filter")),
            "chop_er": self._chop_ok()[1],
            "chop_threshold": float(self.settings.get("chop_threshold", 0.30) or 0.30),
            "entry_bricks": int(self.settings.get("entry_bricks", 2) or 2),
            "er_projection": self.er_projection(),
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
