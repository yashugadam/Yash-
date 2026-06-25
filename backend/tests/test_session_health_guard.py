"""SESSION HEALTH GUARD tests for the LIVE real-money NIFTY Renko engine.

Bug-fix being verified:
At market open with engine.running=True but Angel One disconnected, the old
code retried EXIT every tick -> burst of 'Angel One not connected' rejected
orders. The new guard PAUSES all order activity while disconnected:
  * no _process_brick,
  * no _execute_order (no entries, no exit retries),
  * ticks_in_bar reset to 0,
  * _disc_flagged=True, alert containing 'DISCONNECTED' and 'orders paused',
  * _next_price() is still called -> auto-reconnect runs,
  * open position is HELD untouched.
On reconnect:
  * _disc_flagged flips back to False,
  * a single "Angel One reconnected — strategy resumed." alert is set,
  * normal processing resumes (ticks_in_bar increments, _update_unrealized
    runs).
While staying disconnected, only ONE DISCONNECTED alert is raised (gated by
_disc_flagged).

CRITICAL LIVE-MONEY: ALL broker methods mocked. Bot is NEVER started via
/api/bot/start. No real order is placed.
"""
import os
import sys
import asyncio
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")

import server  # noqa: E402
from server import engine  # noqa: E402

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


# ---------------------------------------------------------------- snapshot ---
def _snapshot_engine():
    return {
        "running": engine.running,
        "position": engine.position,
        "pending_entry": engine.pending_entry,
        "pending_exit": engine.pending_exit,
        "exit_retry_pending": engine.exit_retry_pending,
        "exit_retry_count": engine._exit_retry_count,
        "alert": engine.alert,
        "mkt_paused": engine._mkt_paused,
        "disc_flagged": engine._disc_flagged,
        "ticks_in_bar": engine.ticks_in_bar,
        "tick_interval": engine.settings.get("tick_interval", 1.0),
        "bar_seconds": engine.settings.get("bar_seconds", 60),
        "connected": engine.broker.connected,
        "price": engine.price,
        "prev_price": engine.prev_price,
    }


def _restore_engine(snap):
    engine.running = snap["running"]
    engine.position = snap["position"]
    engine.pending_entry = snap["pending_entry"]
    engine.pending_exit = snap["pending_exit"]
    engine.exit_retry_pending = snap["exit_retry_pending"]
    engine._exit_retry_count = snap["exit_retry_count"]
    engine.alert = snap["alert"]
    engine._mkt_paused = snap["mkt_paused"]
    engine._disc_flagged = snap["disc_flagged"]
    engine.ticks_in_bar = snap["ticks_in_bar"]
    engine.settings["tick_interval"] = snap["tick_interval"]
    engine.settings["bar_seconds"] = snap["bar_seconds"]
    engine.broker.connected = snap["connected"]
    engine.price = snap["price"]
    engine.prev_price = snap["prev_price"]


def _dummy_short(qty=75, entry=24000.0):
    return {
        "side": "SHORT", "qty": qty, "entry_price": entry,
        "entry_time": "2026-01-05T10:00:00+05:30",
        "entry_order_id": "TEST_entry", "reds_at_entry": 2,
        "unrealized_pnl": 0.0,
    }


# ================================================================= 0. init
class TestDiscFlagInit:
    def test_disc_flagged_is_false_at_init(self):
        # The attribute exists on the singleton engine and starts False (only
        # flips True when run_loop sees broker disconnected during market hrs)
        assert hasattr(engine, "_disc_flagged")
        assert isinstance(engine._disc_flagged, bool)


# ============================================== 1. PAUSE while disconnected
class TestDisconnectedPause:
    """run_loop with market open + broker.connected=False:
       - NO _process_brick
       - NO _execute_order
       - ticks_in_bar reset to 0
       - _disc_flagged=True
       - alert mentions DISCONNECTED + 'orders paused'
       - _next_price() IS called (auto-reconnect path)
       - position HELD untouched
    """

    @pytest.mark.asyncio
    async def test_pause_no_orders_no_bricks_position_held(self):
        snap = _snapshot_engine()

        next_price_calls = {"n": 0}
        orig_next_price = engine._next_price

        async def counting_next_price():
            next_price_calls["n"] += 1
            # do NOT call real broker; just no-op (broker stays disconnected)
            return None

        process_brick_calls = []
        execute_order_calls = []

        def fake_process_brick(b):
            process_brick_calls.append(b)

        async def fake_execute(side, kind, ref_price, brick_index, reason="SIGNAL"):
            execute_order_calls.append({
                "side": side, "kind": kind, "reason": reason, "t": time.time(),
            })

        try:
            engine.running = True
            held_position = _dummy_short()
            engine.position = dict(held_position)  # copy
            engine.pending_entry = False
            engine.pending_exit = False
            engine.exit_retry_pending = False
            engine._exit_retry_count = 0
            engine._disc_flagged = False
            engine._mkt_paused = False
            engine.ticks_in_bar = 17        # non-zero -> must be reset to 0
            engine.alert = None
            engine.broker.connected = False
            engine.settings["tick_interval"] = 0.05
            engine.settings["bar_seconds"] = 9999

            with patch.object(engine, "_market_open", return_value=True), \
                 patch.object(engine, "_maybe_square_off"), \
                 patch.object(engine, "_check_circuit_breaker"), \
                 patch.object(engine, "_maybe_auto_roll"), \
                 patch.object(engine, "_next_price", counting_next_price), \
                 patch.object(engine, "_process_brick", side_effect=fake_process_brick), \
                 patch.object(engine, "_execute_order", side_effect=fake_execute), \
                 patch.object(engine.broker, "get_day_pnl",
                              return_value={"found": False, "realised": 0,
                                            "unrealised": 0, "total": 0}):

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.8)        # ~16 ticks
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # _next_price MUST still be called (so auto-reconnect runs)
            assert next_price_calls["n"] >= 5, (
                f"_next_price should still run while disconnected, got "
                f"{next_price_calls['n']} calls"
            )
            # NO bricks processed.
            assert len(process_brick_calls) == 0, (
                f"_process_brick must NOT be called while disconnected, got "
                f"{len(process_brick_calls)} calls"
            )
            # NO orders placed (no entry, no exit retry).
            assert len(execute_order_calls) == 0, (
                f"_execute_order must NOT be called while disconnected, got "
                f"{len(execute_order_calls)} calls"
            )
            # ticks_in_bar reset.
            assert engine.ticks_in_bar == 0, (
                f"ticks_in_bar must be 0 while disconnected, got {engine.ticks_in_bar}"
            )
            # _disc_flagged set.
            assert engine._disc_flagged is True, "_disc_flagged must be True"
            # Alert content.
            assert engine.alert is not None, "DISCONNECTED alert must be set"
            msg = (engine.alert.get("msg") or "")
            assert "DISCONNECTED" in msg.upper(), \
                f"alert must contain DISCONNECTED, got: {msg!r}"
            assert "orders paused" in msg.lower(), \
                f"alert must contain 'orders paused', got: {msg!r}"
            assert engine.alert.get("level") in ("error", "critical"), \
                f"alert level should be error, got {engine.alert.get('level')!r}"
            # Position HELD unchanged.
            assert engine.position is not None, "position must be HELD"
            assert engine.position["side"] == held_position["side"]
            assert engine.position["qty"] == held_position["qty"]
            assert engine.position["entry_price"] == held_position["entry_price"]
        finally:
            _restore_engine(snap)


# ====================================== 2. NO DUPLICATE DISCONNECTED ALERTS
class TestNoDuplicateDisconnectedAlerts:
    """While staying disconnected, only ONE DISCONNECTED alert is set on the
    transition (subsequent ticks see _disc_flagged=True and skip the alert)."""

    @pytest.mark.asyncio
    async def test_only_one_disconnected_alert_set(self):
        snap = _snapshot_engine()
        alerts_seen = []
        orig_set_alert = engine._set_alert

        def recording_set_alert(msg, level="info"):
            alerts_seen.append({"msg": msg, "level": level})
            orig_set_alert(msg, level)

        try:
            engine.running = True
            engine.position = _dummy_short()
            engine.pending_entry = False
            engine.pending_exit = False
            engine.exit_retry_pending = False
            engine._disc_flagged = False
            engine._mkt_paused = False
            engine.ticks_in_bar = 0
            engine.alert = None
            engine.broker.connected = False
            engine.settings["tick_interval"] = 0.05
            engine.settings["bar_seconds"] = 9999

            async def noop_next_price():
                return None

            with patch.object(engine, "_market_open", return_value=True), \
                 patch.object(engine, "_maybe_square_off"), \
                 patch.object(engine, "_check_circuit_breaker"), \
                 patch.object(engine, "_maybe_auto_roll"), \
                 patch.object(engine, "_next_price", noop_next_price), \
                 patch.object(engine, "_set_alert", side_effect=recording_set_alert), \
                 patch.object(engine.broker, "get_day_pnl",
                              return_value={"found": False, "realised": 0,
                                            "unrealised": 0, "total": 0}):

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.8)        # many ticks staying disconnected
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            disc_alerts = [a for a in alerts_seen if "DISCONNECTED" in a["msg"].upper()]
            assert len(disc_alerts) == 1, (
                f"Exactly ONE DISCONNECTED alert expected over many ticks, "
                f"got {len(disc_alerts)}: {disc_alerts}"
            )
            assert engine._disc_flagged is True
        finally:
            _restore_engine(snap)


# ============================================== 3. RECONNECT RESUME
class TestReconnectResume:
    """When broker.connected flips back to True:
       - _disc_flagged -> False
       - 'Angel One reconnected — strategy resumed.' alert is set
       - normal processing resumes: ticks_in_bar increments, _update_unrealized
         runs (position.unrealized_pnl gets recomputed from current price)
    """

    @pytest.mark.asyncio
    async def test_reconnect_clears_flag_and_resumes(self):
        snap = _snapshot_engine()

        # State at reconnect: bot already in DISCONNECTED state.
        try:
            engine.running = True
            engine.position = _dummy_short(qty=75, entry=24000.0)
            engine.position["unrealized_pnl"] = 999.99   # will be overwritten
            engine.pending_entry = False
            engine.pending_exit = False
            engine.exit_retry_pending = False
            engine._exit_retry_count = 0
            engine._disc_flagged = True       # was disconnected
            engine._mkt_paused = False
            engine.ticks_in_bar = 0
            engine.alert = {"id": "old", "msg": "Angel One DISCONNECTED",
                            "level": "error", "time": "old"}
            # Reconnected:
            engine.broker.connected = True
            engine.price = 24050.0
            engine.prev_price = 24045.0
            engine.settings["tick_interval"] = 0.05
            engine.settings["bar_seconds"] = 9999  # never close a bar in test

            async def noop_next_price():
                # broker.connected stays True; price already set above
                return None

            with patch.object(engine, "_market_open", return_value=True), \
                 patch.object(engine, "_maybe_square_off"), \
                 patch.object(engine, "_check_circuit_breaker"), \
                 patch.object(engine, "_maybe_auto_roll"), \
                 patch.object(engine, "_next_price", noop_next_price), \
                 patch.object(engine.broker, "get_day_pnl",
                              return_value={"found": False, "realised": 0,
                                            "unrealised": 0, "total": 0}):

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.4)       # ~8 ticks
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # Flag cleared
            assert engine._disc_flagged is False, \
                "_disc_flagged must clear when broker reconnects"
            # ticks_in_bar incremented (>=1)
            assert engine.ticks_in_bar >= 1, (
                f"ticks_in_bar must increment after reconnect, got {engine.ticks_in_bar}"
            )
            # _update_unrealized ran: pnl = (entry - price) * qty = (24000-24050)*75 = -3750
            assert engine.position is not None
            expected_pnl = round((24000.0 - 24050.0) * 75, 2)
            assert engine.position["unrealized_pnl"] == expected_pnl, (
                f"_update_unrealized should recompute pnl to {expected_pnl}, "
                f"got {engine.position['unrealized_pnl']}"
            )
            # Reconnected alert set
            assert engine.alert is not None
            msg = (engine.alert.get("msg") or "")
            assert "reconnected" in msg.lower(), \
                f"reconnected alert expected, got {msg!r}"
            assert "resumed" in msg.lower() or "strategy resumed" in msg.lower(), \
                f"alert should mention 'resumed', got {msg!r}"
        finally:
            _restore_engine(snap)

    @pytest.mark.asyncio
    async def test_single_reconnect_alert_on_transition(self):
        """Multiple ticks after reconnect must not spam reconnect alerts."""
        snap = _snapshot_engine()
        alerts_seen = []
        orig_set_alert = engine._set_alert

        def rec_set_alert(msg, level="info"):
            alerts_seen.append({"msg": msg, "level": level})
            orig_set_alert(msg, level)

        try:
            engine.running = True
            engine.position = _dummy_short()
            engine.pending_entry = False
            engine.pending_exit = False
            engine.exit_retry_pending = False
            engine._disc_flagged = True
            engine._mkt_paused = False
            engine.ticks_in_bar = 0
            engine.alert = None
            engine.broker.connected = True
            engine.settings["tick_interval"] = 0.05
            engine.settings["bar_seconds"] = 9999

            async def noop_np():
                return None

            with patch.object(engine, "_market_open", return_value=True), \
                 patch.object(engine, "_maybe_square_off"), \
                 patch.object(engine, "_check_circuit_breaker"), \
                 patch.object(engine, "_maybe_auto_roll"), \
                 patch.object(engine, "_next_price", noop_np), \
                 patch.object(engine, "_set_alert", side_effect=rec_set_alert), \
                 patch.object(engine.broker, "get_day_pnl",
                              return_value={"found": False, "realised": 0,
                                            "unrealised": 0, "total": 0}):

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.6)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            recon = [a for a in alerts_seen if "reconnect" in a["msg"].lower()]
            assert len(recon) == 1, (
                f"Exactly ONE reconnect alert expected, got {len(recon)}: {recon}"
            )
        finally:
            _restore_engine(snap)


# ============================== 4. REGRESSION: market closed -> freeze still works
class TestMarketClosedRegression:
    """Disconnected guard must NOT interfere with the market-closed freeze.
    When _market_open()=False, the else branch runs and ticks_in_bar=0,
    _mkt_paused=True, position held. _disc_flagged is NOT touched (no spam)."""

    @pytest.mark.asyncio
    async def test_market_closed_freeze_unaffected(self):
        snap = _snapshot_engine()
        execute_calls = []

        async def fake_exec(*a, **kw):
            execute_calls.append(a)

        try:
            engine.running = True
            engine.position = _dummy_short()
            engine.pending_entry = False
            engine.pending_exit = False
            engine.exit_retry_pending = False
            engine._mkt_paused = False
            engine._disc_flagged = False
            engine.ticks_in_bar = 5
            engine.alert = None
            engine.broker.connected = False    # disconnected AND market closed
            engine.settings["tick_interval"] = 0.05
            engine.settings["bar_seconds"] = 9999

            with patch.object(engine, "_market_open", return_value=False), \
                 patch.object(engine, "_maybe_square_off"), \
                 patch.object(engine, "_check_circuit_breaker"), \
                 patch.object(engine, "_maybe_auto_roll"), \
                 patch.object(engine, "_execute_order", side_effect=fake_exec), \
                 patch.object(engine.broker, "get_day_pnl",
                              return_value={"found": False, "realised": 0,
                                            "unrealised": 0, "total": 0}):

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.3)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert engine._mkt_paused is True
            assert engine.ticks_in_bar == 0
            assert engine.position is not None     # held
            assert len(execute_calls) == 0         # no orders


            assert engine.alert is not None
            assert "market closed" in (engine.alert.get("msg") or "").lower()
        finally:
            _restore_engine(snap)


# ============================== 5. REGRESSION: /api/state endpoint
class TestStateEndpointRegression:
    def test_state_returns_200_with_legacy_keys(self):
        r = requests.get(f"{API}/state", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "market_open" in d and isinstance(d["market_open"], bool)
        for k in ("position", "bricks", "settings", "risk"):
            assert k in d, f"legacy key {k!r} missing from /api/state"
        assert "broker_pnl" in d["risk"]
