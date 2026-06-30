"""Tests for expiry-day auto-roll and roll_to_next() correctness.

LIVE-MONEY SAFETY: in-process pytest only, no real Angel API calls.
broker.futures is seeded with plain dicts; broker methods are mocked.
"""
import os
import sys
from datetime import date, timedelta
from unittest.mock import patch

import pytest

# Make backend importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from angel_broker import AngelBroker  # noqa: E402
import server as srv  # noqa: E402


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _make_fut(expiry_date, symbol, token, name="NIFTY", typ="FUTIDX", lot=75):
    return {"symbol": symbol, "token": str(token), "name": name,
            "expiry": expiry_date, "lotsize": lot, "type": typ}


def _seed_broker(broker, today):
    """Seed broker.futures with 3 NIFTY FUTIDX contracts + irrelevant ones."""
    next_month = today + timedelta(days=30)
    month_after = today + timedelta(days=60)
    broker.futures = [
        _make_fut(today, "NIFTY28JAN26FUT", "111"),
        _make_fut(next_month, "NIFTY26FEB26FUT", "222"),
        _make_fut(month_after, "NIFTY26MAR26FUT", "333"),
        # noise: a stock future and another underlying — must NOT be picked
        _make_fut(next_month, "RELIANCE26FEB26FUT", "444", name="RELIANCE", typ="FUTSTK"),
        _make_fut(next_month, "BANKNIFTY26FEB26FUT", "555", name="BANKNIFTY"),
    ]
    broker.fut_symbol = "NIFTY28JAN26FUT"
    broker.fut_token = "111"
    broker.fut_expiry = today.isoformat()
    broker.fut_name = "NIFTY"
    broker.fut_type = "FUTIDX"
    broker.fut_lotsize = 75


# ------------------------------------------------------------------ #
# 1. roll_to_next() correctness
# ------------------------------------------------------------------ #
class TestRollToNext:
    def test_roll_picks_strictly_later_expiry(self):
        b = AngelBroker()
        today = date.today()
        _seed_broker(b, today)
        res = b.roll_to_next()
        assert res["ok"] is True
        assert res["token"] == "222"
        assert res["symbol"] == "NIFTY26FEB26FUT"
        # broker state should have moved to next contract
        assert b.fut_token == "222"
        assert b.fut_symbol == "NIFTY26FEB26FUT"
        assert b.fut_expiry == (today + timedelta(days=30)).isoformat()

    def test_roll_does_not_pick_current_or_earlier(self):
        b = AngelBroker()
        today = date.today()
        _seed_broker(b, today)
        # add a stale, already-expired contract (must be ignored — only > cur_exp picked)
        b.futures.append(_make_fut(today - timedelta(days=30), "NIFTYSTALE", "999"))
        res = b.roll_to_next()
        assert res["ok"] is True
        # picks the soonest STRICTLY later expiry (next month, token 222)
        assert res["token"] == "222"

    def test_roll_no_next_available(self):
        b = AngelBroker()
        today = date.today()
        # only the current expiring contract — no later one
        b.futures = [_make_fut(today, "NIFTY28JAN26FUT", "111")]
        b.fut_symbol = "NIFTY28JAN26FUT"
        b.fut_token = "111"
        b.fut_expiry = today.isoformat()
        b.fut_name = "NIFTY"
        b.fut_type = "FUTIDX"
        res = b.roll_to_next()
        assert res["ok"] is False
        assert "no next" in res["error"].lower()

    def test_roll_ignores_other_underlyings(self):
        """A BANKNIFTY contract with later expiry must NOT be chosen when current is NIFTY."""
        b = AngelBroker()
        today = date.today()
        b.futures = [
            _make_fut(today, "NIFTY28JAN26FUT", "111"),
            _make_fut(today + timedelta(days=30), "BANKNIFTY26FEB26FUT", "555", name="BANKNIFTY"),
        ]
        b.fut_symbol = "NIFTY28JAN26FUT"
        b.fut_token = "111"
        b.fut_expiry = today.isoformat()
        b.fut_name = "NIFTY"
        b.fut_type = "FUTIDX"
        res = b.roll_to_next()
        assert res["ok"] is False  # no NIFTY FUTIDX with later expiry

    def test_roll_empty_futures_cache(self):
        b = AngelBroker()
        b.futures = []
        res = b.roll_to_next()
        assert res["ok"] is False
        assert "cache" in res["error"].lower()


# ------------------------------------------------------------------ #
# 2. _maybe_auto_roll on EXPIRY DAY
# ------------------------------------------------------------------ #
class TestMaybeAutoRoll:
    def _engine_ready_for_expiry_roll(self, today):
        eng = srv.engine
        # reset relevant flags
        eng.broker.connected = True
        eng.broker.fut_expiry = today.isoformat()
        eng.broker.fut_token = "111"
        eng.broker.fut_symbol = "NIFTY28JAN26FUT"
        eng.broker.fut_name = "NIFTY"
        eng.broker.fut_type = "FUTIDX"
        eng.position = None
        eng.pending_entry = False
        eng.pending_exit = False
        eng.settings["auto_roll"] = True
        eng.bricks = [{"index": 1, "color": "red", "open": 1, "close": 2, "time": "", "signal": None}]
        eng.brick_seq = 1
        eng.anchor = 24000
        eng.direction = -1
        eng.consec_red = 2
        eng.consec_green = 0
        eng.down_run_reds = 2
        eng.squared_off_date = str(today)
        eng.alert = None
        return eng

    def test_rolls_on_expiry_day_after_cutoff_when_flat(self):
        today = date.today()
        eng = self._engine_ready_for_expiry_roll(today)
        fake_roll = {"ok": True, "token": "X", "symbol": "NIFTYNEXTFUT", "expiry": "2026-02-26"}
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng.broker, "roll_to_next", return_value=fake_roll), \
             patch.object(eng, "_autoload_after_roll", return_value=None), \
             patch("server.asyncio.create_task", lambda c: c.close() if hasattr(c, "close") else None):
            eng._maybe_auto_roll()
        # Verify side effects of a successful roll
        assert eng.settings.get("instrument_token") == "X"
        assert eng.anchor is None
        assert eng.direction == 0
        assert eng.bricks == []
        assert eng.brick_seq == 0
        assert eng.consec_red == 0
        assert eng.consec_green == 0
        assert eng.down_run_reds == 0
        assert eng.squared_off_date is None
        assert eng.alert is not None
        assert "Auto-rolled" in eng.alert["msg"]

    def test_does_not_roll_when_position_open(self):
        """Mid-trade guard: must never roll while a position is open."""
        today = date.today()
        eng = self._engine_ready_for_expiry_roll(today)
        eng.position = {"side": "SHORT", "qty": 75, "entry_price": 24000,
                        "entry_time": "", "entry_order_id": "x",
                        "reds_at_entry": 2, "unrealized_pnl": 0.0}
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng.broker, "roll_to_next") as roll_mock:
            eng._maybe_auto_roll()
            roll_mock.assert_not_called()
        # Position is still on the original expiring contract
        assert eng.position is not None
        eng.position = None  # cleanup

    def test_does_not_roll_when_pending_exit(self):
        today = date.today()
        eng = self._engine_ready_for_expiry_roll(today)
        eng.pending_exit = True
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng.broker, "roll_to_next") as roll_mock:
            eng._maybe_auto_roll()
            roll_mock.assert_not_called()
        eng.pending_exit = False

    def test_does_not_roll_when_pending_entry(self):
        today = date.today()
        eng = self._engine_ready_for_expiry_roll(today)
        eng.pending_entry = True
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng.broker, "roll_to_next") as roll_mock:
            eng._maybe_auto_roll()
            roll_mock.assert_not_called()
        eng.pending_entry = False

    def test_does_not_roll_on_expiry_day_before_cutoff(self):
        """On expiry day but BEFORE square-off cutoff -> must NOT roll (still trading)."""
        today = date.today()
        eng = self._engine_ready_for_expiry_roll(today)
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, False)), \
             patch.object(eng.broker, "roll_to_next") as roll_mock:
            eng._maybe_auto_roll()
            roll_mock.assert_not_called()

    def test_rolls_day_after_expiry(self):
        """If expiry < today (e.g. ran next morning), it must still roll."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        eng = self._engine_ready_for_expiry_roll(yesterday)  # exp=yesterday
        fake_roll = {"ok": True, "token": "Y", "symbol": "NIFTYNEXT2", "expiry": "2026-02-26"}
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, yesterday, False, True)), \
             patch.object(eng.broker, "roll_to_next", return_value=fake_roll), \
             patch.object(eng, "_autoload_after_roll", return_value=None), \
             patch("server.asyncio.create_task", lambda c: c.close() if hasattr(c, "close") else None):
            eng._maybe_auto_roll()
        assert eng.settings.get("instrument_token") == "Y"

    def test_does_not_roll_when_auto_roll_disabled(self):
        today = date.today()
        eng = self._engine_ready_for_expiry_roll(today)
        eng.settings["auto_roll"] = False
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)), \
             patch.object(eng.broker, "roll_to_next") as roll_mock:
            eng._maybe_auto_roll()
            roll_mock.assert_not_called()
        eng.settings["auto_roll"] = True


# ------------------------------------------------------------------ #
# 3. _entries_blocked() before vs after roll
# ------------------------------------------------------------------ #
class TestEntriesBlocked:
    def test_blocked_on_expiry_day_after_cutoff(self):
        eng = srv.engine
        eng.breaker_tripped = False
        today = date.today()
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, today, True, True)):
            assert eng._entries_blocked() is True

    def test_not_blocked_after_roll_next_month_contract(self):
        eng = srv.engine
        eng.breaker_tripped = False
        today = date.today()
        next_exp = today + timedelta(days=30)
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, next_exp, False, True)):
            assert eng._entries_blocked() is False

    def test_blocked_when_breaker_tripped(self):
        eng = srv.engine
        eng.breaker_tripped = True
        today = date.today()
        next_exp = today + timedelta(days=30)
        with patch.object(eng, "_expiry_status",
                          return_value=(None, today, next_exp, False, False)):
            assert eng._entries_blocked() is True
        eng.breaker_tripped = False


# ------------------------------------------------------------------ #
# 4. Regression: /api/state still returns legacy keys
# ------------------------------------------------------------------ #
class TestStateRegression:
    def test_get_state_returns_legacy_keys(self):
        import requests
        base = os.environ.get("REACT_APP_BACKEND_URL")
        if not base:
            # fallback to localhost backend for in-cluster run
            base = "http://localhost:8001"
        url = base.rstrip("/") + "/api/state"
        r = requests.get(url, timeout=15)
        assert r.status_code == 200
        data = r.json()
        # legacy + new keys
        for key in ("running", "price", "settings", "bricks", "metrics",
                    "risk", "expiry", "angel", "orders", "feed_mode", "market_open"):
            assert key in data, f"missing key: {key}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
