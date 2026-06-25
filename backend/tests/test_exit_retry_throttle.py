"""EXIT-retry throttle + cap tests for the LIVE real-money NIFTY Renko engine.

Bug-fix being verified:
A persistently REJECTED square-off (BUY EXIT) order used to be retried once
per tick (~1/sec) with no throttle and no cap, producing dozens of rejected
orders in seconds. The fix adds:

  * THROTTLE: auto-retries must be spaced at least EXIT_RETRY_MIN_GAP (15s)
    apart (or settings.retry_seconds if larger).
  * CAP: after MAX_EXIT_RETRIES (8) consecutive rejected auto-retries, the
    engine HALTS auto-retry (exit_retry_pending=False), sets a clear alert,
    and HOLDS the open position (engine.position unchanged).
  * COUNTER RESET: _exit_retry_count resets to 0 when (a) an EXIT fills,
    (b) a fresh ENTRY fills, (c) a fresh non-retry EXIT signal arrives.

CRITICAL LIVE-MONEY: All broker methods (place_limit_order, get_ltp,
get_day_pnl, get_order_status, modify_order_price, cancel_order) are mocked.
The bot is NEVER started via /api/bot/start. No real order is placed.
"""
import os
import sys
import asyncio
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import requests
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")

import server  # noqa: E402
from server import engine, MAX_EXIT_RETRIES, EXIT_RETRY_MIN_GAP  # noqa: E402

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


# ---------------------------------------------------------------- snapshot ---
def _snapshot_engine():
    return {
        "running": engine.running,
        "position": engine.position,
        "pending_exit": engine.pending_exit,
        "exit_retry_pending": engine.exit_retry_pending,
        "exit_retry_count": engine._exit_retry_count,
        "last_exit_retry": engine._last_exit_retry,
        "last_reject_note": engine._last_reject_note,
        "alert": engine.alert,
        "mkt_paused": engine._mkt_paused,
        "ticks_in_bar": engine.ticks_in_bar,
        "tick_interval": engine.settings.get("tick_interval", 1.0),
        "bar_seconds": engine.settings.get("bar_seconds", 60),
        "retry_seconds": engine.settings.get("retry_seconds", 5),
        "connected": engine.broker.connected,
        "price": engine.price,
        "prev_price": engine.prev_price,
    }


def _restore_engine(snap):
    engine.running = snap["running"]
    engine.position = snap["position"]
    engine.pending_exit = snap["pending_exit"]
    engine.exit_retry_pending = snap["exit_retry_pending"]
    engine._exit_retry_count = snap["exit_retry_count"]
    engine._last_exit_retry = snap["last_exit_retry"]
    engine._last_reject_note = snap["last_reject_note"]
    engine.alert = snap["alert"]
    engine._mkt_paused = snap["mkt_paused"]
    engine.ticks_in_bar = snap["ticks_in_bar"]
    engine.settings["tick_interval"] = snap["tick_interval"]
    engine.settings["bar_seconds"] = snap["bar_seconds"]
    engine.settings["retry_seconds"] = snap["retry_seconds"]
    engine.broker.connected = snap["connected"]
    engine.price = snap["price"]
    engine.prev_price = snap["prev_price"]


def _dummy_short(qty=75, entry=24000.0, tag="TEST"):
    return {
        "side": "SHORT", "qty": qty, "entry_price": entry,
        "entry_time": "2026-01-05T10:00:00+05:30",
        "entry_order_id": f"{tag}_entry", "reds_at_entry": 2,
        "unrealized_pnl": 0.0,
    }


# ================================================================= 0. const
class TestConstants:
    def test_constants_have_safe_values(self):
        assert MAX_EXIT_RETRIES == 8, "MAX_EXIT_RETRIES must be 8 (account safety)"
        assert EXIT_RETRY_MIN_GAP == 15, "EXIT_RETRY_MIN_GAP must be 15s (throttle)"


# ============================================================ 1. THROTTLE
class TestExitRetryThrottle:
    """With broker rejecting every EXIT, run_loop must NOT fire one EXIT per tick.
    Auto-retries are spaced at least EXIT_RETRY_MIN_GAP (15s) apart."""

    @pytest.mark.asyncio
    async def test_throttle_spacing_far_smaller_than_ticks(self):
        snap = _snapshot_engine()

        # Count run_loop iterations by patching _refresh_broker_pnl (called at top).
        tick_counter = {"n": 0}
        orig_refresh = engine._refresh_broker_pnl

        async def counting_refresh():
            tick_counter["n"] += 1
            return await orig_refresh() if False else None  # cheap no-op

        # Track EXIT auto-retry attempts via _execute_order mock.
        # The retry block sets pending_exit=True right before scheduling
        # _execute_order. The mock must clear pending_exit (and re-flag
        # exit_retry_pending=True) so the next retry can be considered again.
        exit_calls = []

        async def fake_exec(side, kind, ref_price, brick_index, reason="SIGNAL"):
            exit_calls.append({
                "side": side, "kind": kind, "reason": reason, "t": time.time(),
            })
            # simulate the rejection branch of _execute_order
            engine.pending_exit = False
            engine.exit_retry_pending = True
            engine._last_reject_note = "RMS rejected"

        mock_pnl = MagicMock(return_value={"found": False, "realised": 0,
                                            "unrealised": 0, "total": 0})

        try:
            engine.running = True
            engine.position = _dummy_short()
            engine.pending_exit = False
            engine.exit_retry_pending = True
            engine._exit_retry_count = 0
            engine._last_exit_retry = 0.0     # never retried yet -> first will fire
            engine._last_reject_note = ""
            engine.alert = None
            engine.broker.connected = True
            engine.settings["tick_interval"] = 0.05    # ~20 ticks/sec
            engine.settings["bar_seconds"] = 9999
            engine.settings["retry_seconds"] = 5       # gap = max(5, 15) = 15

            with patch.object(engine, "_market_open", return_value=True), \
                 patch.object(engine, "_maybe_square_off"), \
                 patch.object(engine, "_check_circuit_breaker"), \
                 patch.object(engine, "_maybe_auto_roll"), \
                 patch.object(engine, "_refresh_broker_pnl", counting_refresh), \
                 patch.object(engine, "_execute_order", side_effect=fake_exec), \
                 patch.object(engine.broker, "get_ltp", return_value=24050.0), \
                 patch.object(engine.broker, "place_limit_order",
                              return_value={"ok": False, "error": "RMS rejected"}), \
                 patch.object(engine.broker, "get_day_pnl", mock_pnl):

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(1.2)              # ~24 ticks
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert tick_counter["n"] >= 10, \
                f"run_loop did not iterate enough: {tick_counter['n']}"
            # Throttle: with gap=15s and a ~1.2s window, only the FIRST retry
            # should fire. Allow <=2 to tolerate any startup edge.
            assert len(exit_calls) <= 2, (
                f"EXIT auto-retries not throttled: {len(exit_calls)} calls "
                f"in {tick_counter['n']} ticks (expected <=2 with 15s gap)"
            )
            assert len(exit_calls) >= 1, \
                "first EXIT auto-retry should fire immediately"
            assert exit_calls[0]["side"] == "BUY"
            assert exit_calls[0]["kind"] == "EXIT"
            assert exit_calls[0]["reason"] == "EXIT_RETRY"
            # Counter incremented by exactly the number of calls.
            assert engine._exit_retry_count == len(exit_calls), (
                f"_exit_retry_count={engine._exit_retry_count} but "
                f"{len(exit_calls)} calls were made"
            )
            # Position still held.
            assert engine.position is not None
            assert engine.position["qty"] == 75
        finally:
            _restore_engine(snap)


# ============================================================ 2. CAP + HALT
class TestExitRetryCapAndHalt:
    """After MAX_EXIT_RETRIES consecutive rejected auto-retries, auto-retry
    must HALT (exit_retry_pending=False), set an alert, and HOLD the position."""

    @pytest.mark.asyncio
    async def test_cap_halts_with_alert_and_holds_position(self):
        snap = _snapshot_engine()

        exit_calls = []

        async def fake_exec(side, kind, ref_price, brick_index, reason="SIGNAL"):
            exit_calls.append({"side": side, "kind": kind, "reason": reason})
            engine.pending_exit = False
            engine.exit_retry_pending = True
            engine._last_reject_note = "RMS rejected"

        try:
            engine.running = True
            engine.position = _dummy_short()
            engine.pending_exit = False
            engine.exit_retry_pending = True
            # Pre-seed the counter AT the cap so the very next tick should halt.
            engine._exit_retry_count = MAX_EXIT_RETRIES
            engine._last_exit_retry = 0.0
            engine._last_reject_note = "RMS rejected"
            engine.alert = None
            engine.broker.connected = True
            engine.settings["tick_interval"] = 0.05
            engine.settings["bar_seconds"] = 9999

            with patch.object(engine, "_market_open", return_value=True), \
                 patch.object(engine, "_maybe_square_off"), \
                 patch.object(engine, "_check_circuit_breaker"), \
                 patch.object(engine, "_maybe_auto_roll"), \
                 patch.object(engine, "_execute_order", side_effect=fake_exec), \
                 patch.object(engine.broker, "get_ltp", return_value=24050.0), \
                 patch.object(engine.broker, "place_limit_order",
                              return_value={"ok": False, "error": "RMS rejected"}), \
                 patch.object(engine.broker, "get_day_pnl",
                              return_value={"found": False, "realised": 0,
                                            "unrealised": 0, "total": 0}):

                task = asyncio.create_task(engine.run_loop())
                await asyncio.sleep(0.6)              # ~12 ticks
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # HALT: no further EXIT auto-retries scheduled.
            assert len(exit_calls) == 0, (
                f"After cap, no further EXIT auto-retries must fire, "
                f"got {len(exit_calls)}"
            )
            # Flag flipped off so the retry block is no longer entered.
            assert engine.exit_retry_pending is False, \
                "exit_retry_pending must be False after halt"
            # Position STILL held (carry-forward) — NOT closed automatically.
            assert engine.position is not None, \
                "position must be HELD (not closed) after halt"
            assert engine.position["qty"] == 75
            assert engine.position["entry_price"] == 24000.0
            # Alert was raised and clearly mentions HALTED.
            assert engine.alert is not None
            msg = (engine.alert.get("msg") or engine.alert.get("message")
                   or engine.alert.get("text") or "")
            assert "HALTED" in msg or "halted" in msg.lower(), \
                f"alert must mention auto-retry HALTED, got: {msg!r}"
            assert "EXIT" in msg.upper(), f"alert must mention EXIT, got {msg!r}"
            # Severity should be 'error' (loud)
            assert engine.alert.get("level") in ("error", "critical"), \
                f"alert level should be error, got: {engine.alert.get('level')!r}"
        finally:
            _restore_engine(snap)


# ============================================================ 3. RESETS
class TestExitRetryCounterReset:
    """_exit_retry_count must reset to 0 on:
       (a) fresh non-retry EXIT signal,
       (b) ENTRY fill (_apply_fill ENTRY),
       (c) EXIT fill (success branch in _execute_order).
    """

    @pytest.mark.asyncio
    async def test_reset_on_fresh_non_retry_exit_signal(self):
        """The reset happens at the top of _execute_order BEFORE any broker call.
        We force the disconnected-broker early-return so no real call is made."""
        snap = _snapshot_engine()
        try:
            engine.position = _dummy_short()
            engine.pending_exit = False
            engine.exit_retry_pending = True
            engine._exit_retry_count = 5     # pretend we had several rejects
            engine.broker.connected = False  # forces early-return after reset

            await engine._execute_order("BUY", "EXIT", 24050.0, -1, reason="SIGNAL")

            assert engine._exit_retry_count == 0, (
                "fresh non-retry EXIT must reset _exit_retry_count to 0 "
                f"(got {engine._exit_retry_count})"
            )
        finally:
            _restore_engine(snap)

    @pytest.mark.asyncio
    async def test_no_reset_on_exit_retry_reason(self):
        """An EXIT call with reason='EXIT_RETRY' must NOT reset the counter."""
        snap = _snapshot_engine()
        try:
            engine.position = _dummy_short()
            engine.pending_exit = False
            engine.exit_retry_pending = True
            engine._exit_retry_count = 4
            engine.broker.connected = False  # early-return after the gate

            await engine._execute_order("BUY", "EXIT", 24050.0, -1,
                                         reason="EXIT_RETRY")

            assert engine._exit_retry_count == 4, (
                "EXIT_RETRY must NOT reset _exit_retry_count "
                f"(got {engine._exit_retry_count})"
            )
        finally:
            _restore_engine(snap)

    def test_reset_on_entry_fill(self):
        """`_apply_fill` of an ENTRY order resets _exit_retry_count and clears
        exit_retry_pending (so a brand-new trade starts with a full budget)."""
        snap = _snapshot_engine()
        try:
            engine.position = None  # ENTRY opens a position
            engine.down_run_reds = 2
            engine.pending_entry = True
            engine.exit_retry_pending = True
            engine._exit_retry_count = 6

            entry_order = {
                "id": "TEST_entry_fill",
                "kind": "ENTRY", "side": "SELL", "qty": 75,
                "fill_price": 24000.0, "fill_time": "2026-01-05T10:00:00+05:30",
                "reason": "SIGNAL",
            }
            engine._apply_fill(entry_order)

            assert engine._exit_retry_count == 0, \
                "ENTRY fill must reset _exit_retry_count to 0"
            assert engine.exit_retry_pending is False, \
                "ENTRY fill must clear exit_retry_pending"
            assert engine.position is not None
            assert engine.position["side"] == "SHORT"
            assert engine.pending_entry is False
        finally:
            _restore_engine(snap)

    @pytest.mark.asyncio
    async def test_reset_on_exit_fill_success(self):
        """When the EXIT actually fills (_live_fill returns True), the success
        branch in _execute_order must set exit_retry_pending=False AND
        _exit_retry_count=0 BEFORE _apply_fill is called."""
        snap = _snapshot_engine()
        try:
            engine.position = _dummy_short()
            engine.pending_exit = False
            engine.exit_retry_pending = True
            # Reason is EXIT_RETRY -> counter should NOT be reset by the top
            # branch; the SUCCESS branch is the one we are testing.
            engine._exit_retry_count = 7
            engine.broker.connected = True

            async def fake_live_fill(order, side, base, cap, max_attempts,
                                      retry_secs, floor, ceil):
                # Simulate broker filling at ref_price
                order["fill_price"] = order["ref_price"]
                return True

            with patch.object(engine, "_live_fill", side_effect=fake_live_fill):
                await engine._execute_order("BUY", "EXIT", 24050.0, -1,
                                             reason="EXIT_RETRY")

            assert engine._exit_retry_count == 0, (
                "EXIT fill success must reset _exit_retry_count to 0 "
                f"(got {engine._exit_retry_count})"
            )
            assert engine.exit_retry_pending is False, \
                "EXIT fill success must clear exit_retry_pending"
            # Position closed by _apply_fill EXIT.
            assert engine.position is None
        finally:
            _restore_engine(snap)


# ============================================================ 4. /api/state
class TestStateRegression:
    def test_state_endpoint_still_returns_200_with_market_open(self):
        r = requests.get(f"{API}/state", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "market_open" in data
        assert isinstance(data["market_open"], bool)
        for key in ("position", "bricks", "settings", "risk"):
            assert key in data
        assert "broker_pnl" in data["risk"]


# ============================================================ 5. LTP guard regression
class TestLtpGuardRegression:
    """_next_price + _cur_price must still ignore None/0/negative LTPs."""

    @pytest.mark.asyncio
    async def test_ltp_garbage_ignored(self):
        snap = _snapshot_engine()
        try:
            engine.broker.connected = True
            engine.price = 24000.0
            engine.prev_price = 24000.0

            with patch.object(engine.broker, "get_ltp", return_value=None):
                await engine._next_price()
            assert engine.price == 24000.0

            with patch.object(engine.broker, "get_ltp", return_value=0):
                await engine._next_price()
            assert engine.price == 24000.0

            with patch.object(engine.broker, "get_ltp", return_value=-3):
                await engine._next_price()
            assert engine.price == 24000.0

            # _cur_price falls back to self.price on 0/None
            with patch.object(engine.broker, "get_ltp", return_value=0):
                cp = await engine._cur_price()
            assert cp == 24000.0
        finally:
            _restore_engine(snap)
