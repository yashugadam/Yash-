"""Market-hours freeze tests for the LIVE real-money NIFTY Renko engine.

Verifies:
1. `_market_open()` returns True ONLY Mon–Fri between 09:15:00 and 15:30:00 IST.
2. MARKET-CLOSED FREEZE: with engine.running=True and market_open() forced False,
   the run_loop must NOT form new bricks, NOT change engine.position, must reset
   ticks_in_bar to 0, set _mkt_paused=True, and emit a 'Market closed' alert.
3. MARKET-OPEN path still works WITHOUT placing real orders (position pre-set
   to a dummy SHORT so no ENTRY is triggered, broker.get_ltp mocked).
4. `_next_price()` ignores ltp <= 0 (and None) — engine.price must not become 0.
5. Regression: GET /api/state -> 200 and `market_open` is a boolean.

CRITICAL LIVE-MONEY: All broker calls (get_ltp / place_limit_order / get_day_pnl)
are mocked. Position is pre-set so the market-open path never tries to ENTER.
The engine is NEVER started via /api/bot/start.
"""
import os
import sys
import asyncio
from pathlib import Path
from datetime import datetime, time as dtime, timezone
from unittest.mock import patch, MagicMock

import pytest
import requests
from dotenv import load_dotenv

# Make /app/backend importable
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# Load REACT_APP_BACKEND_URL from frontend/.env
load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")

import server  # noqa: E402
from server import IST, engine  # noqa: E402

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


# ------------------------------------------------------------------ helpers ---
def _make_fake_datetime(fixed_ist):
    """Return a stand-in for `datetime` whose .now(tz) yields `fixed_ist`.
    Other attributes (date, time, fromisoformat, timezone) are passed through to
    the real datetime so the rest of server.py keeps working."""
    real_dt = datetime

    class _FakeDT(datetime):  # subclass to inherit class methods we don't override
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_ist.replace(tzinfo=None)
            return fixed_ist.astimezone(tz)
    return _FakeDT


def _ist(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


# ============================================================ 1. _market_open
class TestMarketOpenWindow:
    """Mon–Fri 09:15:00–15:30:00 IST -> True. Everything else -> False."""

    @pytest.mark.parametrize("label,ist_dt,expected", [
        # Mon 2026-01-05
        ("mon-09:14:59",  _ist(2026, 1, 5,  9, 14, 59), False),
        ("mon-09:15:00",  _ist(2026, 1, 5,  9, 15,  0), True),
        ("mon-12:00:00",  _ist(2026, 1, 5, 12,  0,  0), True),
        ("mon-15:30:00",  _ist(2026, 1, 5, 15, 30,  0), True),
        ("mon-15:30:01",  _ist(2026, 1, 5, 15, 30,  1), False),
        ("mon-15:31:00",  _ist(2026, 1, 5, 15, 31,  0), False),
        ("mon-00:30:00",  _ist(2026, 1, 5,  0, 30,  0), False),
        # Fri 2026-01-09
        ("fri-10:00:00",  _ist(2026, 1, 9, 10,  0,  0), True),
        # Sat / Sun (even inside the time window)
        ("sat-10:00:00",  _ist(2026, 1, 10, 10,  0, 0), False),
        ("sun-12:00:00",  _ist(2026, 1, 11, 12,  0, 0), False),
    ])
    def test_market_open_truth_table(self, label, ist_dt, expected):
        fake = _make_fake_datetime(ist_dt)
        with patch.object(server, "datetime", fake):
            assert engine._market_open() is expected, f"{label} -> expected {expected}"


# ============================================================ 2. FREEZE path
class TestMarketClosedFreeze:
    """The core safety fix: outside market hours the engine MUST hold."""

    @pytest.mark.asyncio
    async def test_freeze_holds_position_no_bricks_no_orders(self):
        # snapshot pre-test state
        prev_running = engine.running
        prev_position = engine.position
        prev_bricks_len = len(engine.bricks)
        prev_alert = engine.alert
        prev_paused = engine._mkt_paused
        prev_ticks = engine.ticks_in_bar
        prev_tick_interval = engine.settings.get("tick_interval", 1)
        prev_connected = engine.broker.connected

        # dummy SHORT position (carry-forward) — must remain UNCHANGED after freeze
        dummy_short = {
            "side": "SELL",
            "qty": 75,
            "entry_price": 24000.0,
            "unrealized_pnl": 0.0,
            "id": "TEST_dummy_short",
        }

        # Mocks: any broker call is a fail-safe (should NOT happen during freeze)
        mock_get_ltp = MagicMock(return_value=24050.0)
        mock_place = MagicMock(return_value={"ok": True, "order_id": "SHOULD_NOT_BE_CALLED"})
        mock_pnl = MagicMock(return_value={"found": False, "realised": 0, "unrealised": 0, "total": 0})

        # Patch broker methods + engine time gate + tick_interval for speed
        with patch.object(engine.broker, "get_ltp", mock_get_ltp), \
             patch.object(engine.broker, "place_limit_order", mock_place), \
             patch.object(engine.broker, "get_day_pnl", mock_pnl), \
             patch.object(engine, "_market_open", return_value=False), \
             patch.object(engine, "_process_brick") as mock_process_brick:
            try:
                engine.running = True
                engine.position = dummy_short
                engine.ticks_in_bar = 5            # should be RESET to 0 by freeze
                engine._mkt_paused = False
                engine.settings["tick_interval"] = 0.05
                engine.broker.connected = True

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.4)           # ~6-8 freeze iterations
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                # ---- assertions ----
                assert engine._mkt_paused is True, "freeze must set _mkt_paused=True"
                assert engine.position is dummy_short, "position must be untouched"
                assert engine.position["qty"] == 75
                assert engine.position["entry_price"] == 24000.0
                assert len(engine.bricks) == prev_bricks_len, "no new bricks during freeze"
                assert engine.ticks_in_bar == 0, "ticks_in_bar must be reset to 0"
                mock_process_brick.assert_not_called()
                mock_place.assert_not_called()
                mock_get_ltp.assert_not_called()
                assert engine.alert is not None, "freeze must emit a 'Market closed' alert"
                alert_text = (engine.alert.get("text") or engine.alert.get("message")
                              or str(engine.alert)).lower()
                assert "market closed" in alert_text or "paused" in alert_text, \
                    f"alert should mention market-closed, got: {engine.alert!r}"
            finally:
                # restore engine
                engine.running = prev_running
                engine.position = prev_position
                engine.ticks_in_bar = prev_ticks
                engine._mkt_paused = prev_paused
                engine.alert = prev_alert
                engine.settings["tick_interval"] = prev_tick_interval
                engine.broker.connected = prev_connected


# ============================================================ 3. OPEN path
class TestMarketOpenPathNoEntry:
    """Market open -> ticks accumulate & unrealized updates, but NO order is placed
    because a dummy SHORT position is already held."""

    @pytest.mark.asyncio
    async def test_open_path_updates_ticks_without_placing_orders(self):
        prev_running = engine.running
        prev_position = engine.position
        prev_paused = engine._mkt_paused
        prev_price = engine.price
        prev_ticks = engine.ticks_in_bar
        prev_tick_interval = engine.settings.get("tick_interval", 1)
        prev_bar_seconds = engine.settings.get("bar_seconds", 60)
        prev_connected = engine.broker.connected

        dummy_short = {
            "side": "SELL",
            "qty": 75,
            "entry_price": 24000.0,
            "unrealized_pnl": 0.0,
            "id": "TEST_dummy_short_open",
        }

        mock_get_ltp = MagicMock(return_value=24050.0)
        mock_place = MagicMock(return_value={"ok": True, "order_id": "SHOULD_NOT_BE_CALLED"})
        mock_pnl = MagicMock(return_value={"found": True, "realised": 0,
                                            "unrealised": -10, "total": -10})

        with patch.object(engine.broker, "get_ltp", mock_get_ltp), \
             patch.object(engine.broker, "place_limit_order", mock_place), \
             patch.object(engine.broker, "get_day_pnl", mock_pnl), \
             patch.object(engine, "_market_open", return_value=True), \
             patch.object(engine, "_maybe_square_off"), \
             patch.object(engine, "_check_circuit_breaker"), \
             patch.object(engine, "_maybe_auto_roll"):
            try:
                engine.running = True
                engine.position = dummy_short
                engine._mkt_paused = True               # should be cleared on open
                engine.ticks_in_bar = 0
                engine.settings["tick_interval"] = 0.05
                engine.settings["bar_seconds"] = 9999   # prevent a bar close in this short window
                engine.broker.connected = True

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.3)                # ~5-6 ticks
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                assert engine._mkt_paused is False, "open path must clear _mkt_paused"
                assert engine.ticks_in_bar > 0, "ticks_in_bar must have incremented"
                assert mock_get_ltp.called, "broker.get_ltp must have been called on open path"
                mock_place.assert_not_called()
                # Unrealized recomputed for SHORT: (entry - price) * qty = (24000 - 24050)*75 = -3750
                assert engine.position["unrealized_pnl"] == round(
                    (24000.0 - 24050.0) * 75, 2)
                assert engine.position is dummy_short, "position not replaced"
                assert engine.price == 24050.0
            finally:
                engine.running = prev_running
                engine.position = prev_position
                engine._mkt_paused = prev_paused
                engine.price = prev_price
                engine.ticks_in_bar = prev_ticks
                engine.settings["tick_interval"] = prev_tick_interval
                engine.settings["bar_seconds"] = prev_bar_seconds
                engine.broker.connected = prev_connected


# ============================================================ 4. LTP guard
class TestLtpGarbageGuard:
    """ `_next_price()` must ignore ltp <= 0 and None."""

    @pytest.mark.asyncio
    async def test_zero_and_none_ltp_do_not_update_price(self):
        prev_price = engine.price
        prev_prev = engine.prev_price
        prev_connected = engine.broker.connected
        prev_feed_err = engine.feed_error
        try:
            engine.broker.connected = True
            engine.price = 24000.0
            engine.prev_price = 24000.0

            # 1) None -> price unchanged
            with patch.object(engine.broker, "get_ltp", return_value=None):
                await engine._next_price()
            assert engine.price == 24000.0, "None LTP must not update price"

            # 2) Zero -> price MUST NOT become 0
            with patch.object(engine.broker, "get_ltp", return_value=0):
                await engine._next_price()
            assert engine.price != 0, "ltp=0 must be ignored (garbage guard)"
            assert engine.price == 24000.0, "ltp=0 must leave price unchanged"

            # 3) Negative -> ignored
            with patch.object(engine.broker, "get_ltp", return_value=-5):
                await engine._next_price()
            assert engine.price == 24000.0, "negative LTP must be ignored"

            # 4) Valid positive -> updates
            with patch.object(engine.broker, "get_ltp", return_value=24123.5):
                await engine._next_price()
            assert engine.price == 24123.5, "valid positive LTP must update price"
        finally:
            engine.price = prev_price
            engine.prev_price = prev_prev
            engine.broker.connected = prev_connected
            engine.feed_error = prev_feed_err


# ============================================================ 5. /api/state
class TestStateMarketOpenField:
    def test_state_has_market_open_boolean(self):
        r = requests.get(f"{API}/state", timeout=15)
        assert r.status_code == 200
        data = r.json()
        # new field
        assert "market_open" in data, "/api/state must expose top-level `market_open`"
        assert isinstance(data["market_open"], bool), \
            f"market_open must be bool, got {type(data['market_open']).__name__}"
        # existing keys remain
        for key in ("position", "bricks", "settings", "risk"):
            assert key in data, f"/api/state missing existing key: {key}"
        assert "broker_pnl" in data["risk"], "risk.broker_pnl missing"
