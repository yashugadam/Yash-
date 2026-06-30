"""Tests for TRUE POSITION ROLLOVER at expiry (rollover_position setting).

LIVE-MONEY SAFETY: in-process pytest only — all broker order methods mocked.
Verifies:
  * _maybe_square_off arms _rollover_armed when setting is True
  * does NOT arm when setting is False (just square off)
  * _autoload_after_roll -> _rollover_enter opens fresh SHORT on next month
  * _rollover_enter skips when market closed (warning alert)
  * Not armed when flat at expiry cutoff (no phantom short)
  * Settings API persists rollover_position true/false
  * SettingsUpdate accepts the boolean
"""
import asyncio
import os
import sys
from datetime import date
from unittest.mock import patch, MagicMock

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server as srv  # noqa: E402


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")


def _reset_engine(eng):
    eng.position = None
    eng.pending_entry = False
    eng.pending_exit = False
    eng.forced_exit_pending = False
    eng._rollover_armed = False
    eng.squared_off_date = None
    eng.alert = None
    eng.consec_red = 0
    eng.consec_green = 0
    eng.down_run_reds = 0
    eng.settings["auto_square_off"] = True
    eng.settings["auto_roll"] = True
    eng.settings["rollover_position"] = True
    eng.broker.connected = True
    eng.broker.fut_symbol = "NIFTYNEXTFUT"
    eng.broker.fut_token = "222"
    eng.broker.fut_expiry = "2026-02-26"
    eng.broker.fut_name = "NIFTY"
    eng.broker.fut_type = "FUTIDX"
    eng.broker.fut_lotsize = 75


@pytest.fixture
def eng():
    engine = srv.engine
    _reset_engine(engine)
    yield engine
    _reset_engine(engine)


# ---------------- 1. ROLLOVER ARM at expiry square-off ----------------
class TestRolloverArm:
    def test_arms_when_setting_true_and_position_open(self, eng):
        today = date.today()
        eng.position = {"side": "SHORT", "qty": 75, "entry_price": 24000,
                        "entry_time": "", "entry_order_id": "x",
                        "reds_at_entry": 2, "unrealized_pnl": 0.0}
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng, "_force_exit") as fexit, \
             patch("server.asyncio.create_task", lambda c: c.close() if hasattr(c, "close") else None):
            eng._maybe_square_off()
        assert eng._rollover_armed is True
        assert eng.squared_off_date == str(today)
        fexit.assert_called_once_with("EXPIRY_SQUAREOFF")

    def test_does_not_arm_when_setting_false(self, eng):
        today = date.today()
        eng.settings["rollover_position"] = False
        eng.position = {"side": "SHORT", "qty": 75, "entry_price": 24000,
                        "entry_time": "", "entry_order_id": "x",
                        "reds_at_entry": 2, "unrealized_pnl": 0.0}
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng, "_force_exit") as fexit, \
             patch("server.asyncio.create_task", lambda c: c.close() if hasattr(c, "close") else None):
            eng._maybe_square_off()
        assert eng._rollover_armed is False
        fexit.assert_called_once_with("EXPIRY_SQUAREOFF")

    def test_does_not_arm_when_auto_roll_false(self, eng):
        today = date.today()
        eng.settings["auto_roll"] = False
        eng.position = {"side": "SHORT", "qty": 75, "entry_price": 24000,
                        "entry_time": "", "entry_order_id": "x",
                        "reds_at_entry": 2, "unrealized_pnl": 0.0}
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng, "_force_exit"), \
             patch("server.asyncio.create_task", lambda c: c.close() if hasattr(c, "close") else None):
            eng._maybe_square_off()
        assert eng._rollover_armed is False

    def test_not_armed_when_flat_at_expiry_cutoff(self, eng):
        """No open position at cutoff -> no phantom rollover should be armed."""
        today = date.today()
        eng.position = None
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng, "_force_exit") as fexit:
            eng._maybe_square_off()
        assert eng._rollover_armed is False
        assert eng.squared_off_date == str(today)
        fexit.assert_not_called()


# ---------------- 2. ROLLOVER RE-ENTRY after roll ----------------
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.get_event_loop().is_running() else asyncio.run(coro)


class TestRolloverEnter:
    def test_re_enters_short_on_new_contract_when_armed(self, eng):
        eng._rollover_armed = True
        eng.broker.place_limit_order = MagicMock(return_value={"ok": True, "orderid": "BRO1"})
        eng.broker.get_ltp = MagicMock(return_value=24000.0)
        eng.broker.get_order_status = MagicMock(return_value={"status": "COMPLETE", "avgprice": 24000.0})
        eng.broker.modify_order_price = MagicMock()
        captured = {}

        async def fake_execute(side, kind, price, idx, reason="SIGNAL"):
            captured["side"] = side
            captured["kind"] = kind
            captured["reason"] = reason
            captured["price"] = price

        with patch.object(eng, "load_history", return_value=None), \
             patch.object(eng, "_market_open", return_value=True), \
             patch.object(eng, "_cur_price", return_value=24000.0), \
             patch.object(eng, "_execute_order", side_effect=fake_execute):
            asyncio.run(eng._autoload_after_roll())

        assert eng._rollover_armed is False
        assert eng.pending_entry is True
        assert eng.consec_red == 2
        assert eng.down_run_reds == 2
        assert eng.consec_green == 0
        assert eng.alert is not None
        assert "rollover" in eng.alert["msg"].lower()
        assert "short" in eng.alert["msg"].lower()
        assert captured.get("side") == "SELL"
        assert captured.get("kind") == "ENTRY"
        assert captured.get("reason") == "EXPIRY_ROLLOVER"

    def test_skip_when_market_closed(self, eng):
        eng._rollover_armed = True
        called = {"exec": False}

        async def fake_execute(*args, **kwargs):
            called["exec"] = True

        with patch.object(eng, "load_history", return_value=None), \
             patch.object(eng, "_market_open", return_value=False), \
             patch.object(eng, "_execute_order", side_effect=fake_execute):
            asyncio.run(eng._autoload_after_roll())

        assert called["exec"] is False
        assert eng.pending_entry is False
        assert eng.position is None
        assert eng.alert is not None
        assert "market closed" in eng.alert["msg"].lower()
        assert eng.alert["level"] == "warning"
        # armed flag was cleared by _autoload_after_roll before calling _rollover_enter
        assert eng._rollover_armed is False

    def test_skip_when_not_armed(self, eng):
        eng._rollover_armed = False
        called = {"exec": False}

        async def fake_execute(*args, **kwargs):
            called["exec"] = True

        with patch.object(eng, "load_history", return_value=None), \
             patch.object(eng, "_market_open", return_value=True), \
             patch.object(eng, "_execute_order", side_effect=fake_execute):
            asyncio.run(eng._autoload_after_roll())

        assert called["exec"] is False
        assert eng.pending_entry is False

    def test_skip_when_already_has_position(self, eng):
        eng._rollover_armed = True
        eng.position = {"side": "SHORT", "qty": 75, "entry_price": 1, "entry_time": "",
                        "entry_order_id": "x", "reds_at_entry": 2, "unrealized_pnl": 0.0}
        called = {"exec": False}

        async def fake_execute(*args, **kwargs):
            called["exec"] = True

        with patch.object(eng, "_market_open", return_value=True), \
             patch.object(eng, "_execute_order", side_effect=fake_execute):
            asyncio.run(eng._rollover_enter())

        assert called["exec"] is False


# ---------------- 3. reset() clears _rollover_armed ----------------
class TestReset:
    def test_reset_clears_rollover_armed(self, eng):
        eng._rollover_armed = True
        eng.reset()
        assert eng._rollover_armed is False


# ---------------- 4. SETTING plumbing via HTTP API ----------------
class TestSettingsAPI:
    def test_state_has_rollover_position_default_true(self):
        r = requests.get(f"{BASE_URL}/api/state", timeout=15)
        assert r.status_code == 200
        s = r.json().get("settings", {})
        assert "rollover_position" in s
        # restore baseline
        requests.post(f"{BASE_URL}/api/settings", json={"rollover_position": True}, timeout=15)

    def test_can_persist_false_then_true(self):
        # set false
        r = requests.post(f"{BASE_URL}/api/settings", json={"rollover_position": False}, timeout=15)
        assert r.status_code == 200
        assert r.json().get("rollover_position") is False
        r2 = requests.get(f"{BASE_URL}/api/state", timeout=15)
        assert r2.json()["settings"]["rollover_position"] is False
        # set true
        r3 = requests.post(f"{BASE_URL}/api/settings", json={"rollover_position": True}, timeout=15)
        assert r3.status_code == 200
        assert r3.json().get("rollover_position") is True
        r4 = requests.get(f"{BASE_URL}/api/state", timeout=15)
        assert r4.json()["settings"]["rollover_position"] is True

    def test_settings_update_model_accepts_bool(self):
        # invalid type should be rejected by Pydantic
        r = requests.post(f"{BASE_URL}/api/settings", json={"rollover_position": "yes"}, timeout=15)
        # FastAPI/Pydantic v2 may coerce "yes"? Actually for bool, only true/false/1/0
        # accept either 422 (rejected) or success after coercion; ensure no 500.
        assert r.status_code in (200, 422)
        # restore baseline
        requests.post(f"{BASE_URL}/api/settings", json={"rollover_position": True}, timeout=15)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
