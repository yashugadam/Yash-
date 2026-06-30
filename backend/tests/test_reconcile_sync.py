"""Test: reconcile_resolve('accept') (a.k.a. 'Sync to broker') actually syncs
bot state to Angel One's reality so the warning clears and does not reappear.

Covers all variants:
  - broker flat + bot has stale short  -> clear bot.position to None
  - broker holds short + bot is flat   -> adopt as engine.position
  - get_net_position not found          -> ok:False, no state change
  - reconcile() state mapping (GOOD / ENTRY_MISSED / EXIT_MISSED)
  - reenter requires bot position; reexit requires broker netqty != 0
  - default square_off_time = '15:20'

In-process (imports server.engine) and mocks broker calls so NO real orders
are placed.
"""
import os
import sys
import asyncio
import importlib
import pytest

sys.path.insert(0, '/app/backend')

# Import fresh engine
server = importlib.import_module("server")
engine = server.engine


def run(coro):
    try:
        loop = asyncio.new_event_loop()
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def reset_engine_state(monkeypatch):
    """Reset relevant engine state between tests and stub persistence."""
    # No DB writes during unit tests
    async def _noop():
        return None
    monkeypatch.setattr(engine, "_persist_state", _noop)

    # Reset state
    engine.position = None
    engine.pending_entry = False
    engine.pending_exit = False
    engine.exit_retry_pending = False
    engine._exit_retry_count = 0
    engine.down_run_reds = 0
    engine.price = 24000.0

    # Pretend broker connected (reconcile() checks this)
    class _BrokerStub:
        connected = True
        fut_token = "FUT123"
        fut_symbol = "NIFTY-FUT"
        fut_expiry = "2026-01-29"
        def status(self):
            return {"connected": True}
    # Only attributes needed; keep underlying place_limit_order untouched (we won't call it)
    monkeypatch.setattr(engine.broker, "connected", True, raising=False)
    monkeypatch.setattr(engine.broker, "fut_token", "FUT123", raising=False)

    yield


# ---------- reconcile() state mapping ----------

def test_reconcile_good_when_bot_and_broker_match_flat(monkeypatch):
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": 0, "avgprice": 0.0})
    res = run(engine.reconcile())
    assert res["available"] is True
    assert res["state"] == "GOOD"


def test_reconcile_entry_missed(monkeypatch):
    engine.position = {"side": "SHORT", "qty": 65, "entry_price": 24000,
                       "entry_time": "x", "entry_order_id": "x",
                       "reds_at_entry": 2, "unrealized_pnl": 0.0}
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": 0, "avgprice": 0.0})
    res = run(engine.reconcile())
    assert res["state"] == "ENTRY_MISSED"


def test_reconcile_exit_missed(monkeypatch):
    engine.position = None
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    res = run(engine.reconcile())
    assert res["state"] == "EXIT_MISSED"
    assert res["broker_netqty"] == -65


# ---------- reconcile_resolve('accept') ----------

def test_accept_when_broker_flat_clears_stale_position(monkeypatch):
    # Bot has a stale short, broker is flat -> bot must be cleared to FLAT.
    engine.position = {"side": "SHORT", "qty": 65, "entry_price": 24000.0,
                       "entry_time": "x", "entry_order_id": "x",
                       "reds_at_entry": 2, "unrealized_pnl": 0.0}
    engine.pending_exit = True
    engine.exit_retry_pending = True
    engine._exit_retry_count = 3

    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": 0, "avgprice": 0.0})

    res = run(engine.reconcile_resolve("accept"))
    assert res["ok"] is True
    assert "flat" in res["message"].lower()
    assert engine.position is None
    assert engine.pending_entry is False
    assert engine.pending_exit is False
    assert engine.exit_retry_pending is False
    assert engine._exit_retry_count == 0

    # Subsequent reconcile -> GOOD
    res2 = run(engine.reconcile())
    assert res2["state"] == "GOOD"


def test_accept_when_broker_has_position_adopts_it(monkeypatch):
    # Bot is flat (EXIT_MISSED) but broker still holds -65 short.
    engine.position = None
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})

    res = run(engine.reconcile_resolve("accept"))
    assert res["ok"] is True
    assert "adopted" in res["message"].lower()
    assert engine.position is not None
    assert engine.position["side"] == "SHORT"
    assert engine.position["qty"] == 65
    assert engine.position["entry_price"] == 24000.0
    assert engine.position["entry_order_id"] == "RECONCILE_ADOPT"

    # Subsequent reconcile -> GOOD (bot short 65, broker -65)
    res2 = run(engine.reconcile())
    assert res2["state"] == "GOOD"


def test_accept_when_get_net_position_not_found(monkeypatch):
    engine.position = {"side": "SHORT", "qty": 65, "entry_price": 24000.0,
                       "entry_time": "x", "entry_order_id": "x",
                       "reds_at_entry": 2, "unrealized_pnl": 0.0}
    snapshot = dict(engine.position)
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": False, "error": "broker timeout"})

    res = run(engine.reconcile_resolve("accept"))
    assert res["ok"] is False
    assert "timeout" in res["message"].lower() or "could not" in res["message"].lower()
    # No state change
    assert engine.position == snapshot


# ---------- reenter / reexit guards ----------

def test_reenter_requires_bot_position(monkeypatch):
    engine.position = None
    res = run(engine.reconcile_resolve("reenter"))
    assert res["ok"] is False


def test_reexit_requires_broker_position(monkeypatch):
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": 0})
    res = run(engine.reconcile_resolve("reexit"))
    assert res["ok"] is False
    assert "no open position" in res["message"].lower()


def test_unknown_action_returns_error():
    res = run(engine.reconcile_resolve("bogus"))
    assert res["ok"] is False


# ---------- defaults / regression ----------

def test_default_square_off_time_is_15_20():
    # Construct a brand-new engine; default constant lives in __init__
    fresh = server.TradingEngine(server.db)
    assert fresh.settings["square_off_time"] == "15:20"


def test_state_endpoint_has_expected_keys():
    """Regression: GET /api/state-like snapshot has all critical keys."""
    snap = engine.snapshot()
    for key in ["running", "mode", "market_open", "price", "settings",
                "position", "expiry", "risk", "metrics"]:
        assert key in snap
    # expiry.square_off_time exposed
    assert "square_off_time" in snap["expiry"]
