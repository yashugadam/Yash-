"""Tests for the 'Adopt manual trade on Start' feature.

Coverage:
  - _on_start with broker SHORT  -> pending_adoption set, no order, no entry
  - _on_start with broker LONG   -> pending_adoption(side=LONG), blocks trading
  - _on_start with broker FLAT   -> pending_adoption stays None, proceeds normally
  - _entries_blocked() respects pending_adoption
  - adopt_position(True) on SHORT  -> adopts as engine.position, clears pending, NO order
  - adopt_position(False)          -> sets declined=True, keeps blocking entries
  - adopt_position(True) on LONG   -> ok:False (long-only block)
  - reset() clears pending_adoption
  - API: POST /api/bot/adopt routes to engine.adopt_position
  - API: GET  /api/state includes 'pending_adoption' key
  - Regression: snapshot has all legacy keys (running, position, settings, etc.)

In-process pytest only. All broker IO is monkey-patched. NO real orders placed.
"""
import sys
import asyncio
import importlib
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, '/app/backend')
server = importlib.import_module("server")
engine = server.engine


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def reset_engine_state(monkeypatch):
    """Reset relevant engine state between tests and stub all IO."""
    async def _noop():
        return None
    monkeypatch.setattr(engine, "_persist_state", _noop)

    # Stub broker so no live calls are made
    monkeypatch.setattr(engine.broker, "connected", True, raising=False)
    monkeypatch.setattr(engine.broker, "fut_token", "FUT123", raising=False)

    # By default, return broker FLAT - individual tests will override
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": 0, "avgprice": 0.0},
                        raising=False)

    # Make place_limit_order a hard-fail spy: must NEVER be called by adoption logic
    def _no_real_order(*args, **kwargs):
        raise AssertionError("place_limit_order must NOT be called during adoption flow")
    monkeypatch.setattr(engine.broker, "place_limit_order", _no_real_order, raising=False)

    # Engine state
    engine.position = None
    engine.pending_entry = False
    engine.pending_exit = False
    engine.exit_retry_pending = False
    engine._exit_retry_count = 0
    engine.pending_adoption = None
    engine.alert = None
    engine.breaker_tripped = False
    engine.bricks = []
    engine.down_run_reds = 0
    engine.price = 24000.0
    engine.running = True
    yield


# ---------- _on_start: BROKER SHORT (the main case) ----------

def test_on_start_with_broker_short_sets_pending_adoption(monkeypatch):
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    # If _maybe_enter_on_start were called, it would inspect bricks - patch to fail loudly
    enter_called = {"n": 0}
    orig = engine._maybe_enter_on_start
    async def _spy():
        enter_called["n"] += 1
        return await orig()
    monkeypatch.setattr(engine, "_maybe_enter_on_start", _spy)

    run(engine._on_start())

    assert engine.pending_adoption is not None
    assert engine.pending_adoption["qty"] == 65
    assert engine.pending_adoption["netqty"] == -65
    assert engine.pending_adoption["avgprice"] == 24000.0
    assert engine.pending_adoption["side"] == "SHORT"
    assert engine.pending_adoption["declined"] is False
    # No entry attempted while pending
    assert enter_called["n"] == 0
    # Position must remain None
    assert engine.position is None
    # Alert surfaced
    assert engine.alert is not None
    assert "SHORT" in (engine.alert.get("msg") or "")


# ---------- _entries_blocked respects pending_adoption ----------

def test_entries_blocked_true_when_pending_adoption_set():
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    assert engine._entries_blocked() is True


def test_entries_blocked_false_when_no_pending_adoption():
    engine.pending_adoption = None
    engine.breaker_tripped = False
    # Default expiry isn't 'today and past cut'
    assert engine._entries_blocked() is False


def test_entries_blocked_still_true_when_pending_adoption_declined():
    # declined still keeps the block (no stacking)
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": True}
    assert engine._entries_blocked() is True


# ---------- adopt_position(True) on SHORT ----------

def test_adopt_confirm_short_promotes_to_engine_position():
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    res = run(engine.adopt_position(True))
    assert res["ok"] is True
    assert "adopted" in res["message"].lower() or "managing" in res["message"].lower()

    assert engine.position is not None
    assert engine.position["side"] == "SHORT"
    assert engine.position["qty"] == 65
    assert engine.position["entry_price"] == 24000.0
    assert engine.position["entry_order_id"] == "ADOPTED_MANUAL"
    assert engine.pending_adoption is None
    # Entries unblocked once adoption clears (assuming no expiry/breaker)
    assert engine._entries_blocked() is False
    # Alert says adopted
    assert "dopt" in (engine.alert.get("msg") or "")


# ---------- adopt_position(False) ----------

def test_adopt_decline_keeps_block_and_no_position():
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    res = run(engine.adopt_position(False))
    assert res["ok"] is True
    assert "blocked" in res["message"].lower() or "stacking" in res["message"].lower()
    assert engine.position is None
    # pending_adoption still present but declined=True
    assert engine.pending_adoption is not None
    assert engine.pending_adoption["declined"] is True
    # Entries stay blocked to avoid stacking
    assert engine._entries_blocked() is True


# ---------- LONG handling ----------

def test_on_start_with_broker_long_blocks_trading(monkeypatch):
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": 50, "avgprice": 24000.0})
    run(engine._on_start())
    assert engine.pending_adoption is not None
    assert engine.pending_adoption["side"] == "LONG"
    assert engine.pending_adoption["netqty"] == 50
    assert engine.pending_adoption["qty"] == 50
    assert engine.position is None
    # Alert is error severity
    assert engine.alert is not None
    assert engine.alert.get("level") == "error"
    # Entries blocked
    assert engine._entries_blocked() is True


def test_adopt_confirm_long_rejected():
    engine.pending_adoption = {"qty": 50, "avgprice": 24000.0, "netqty": 50,
                                "side": "LONG", "declined": False}
    res = run(engine.adopt_position(True))
    assert res["ok"] is False
    assert "long" in res["message"].lower()
    assert engine.position is None
    # Still blocking entries (declined flag is set on rejection)
    assert engine._entries_blocked() is True


# ---------- BROKER FLAT ----------

def test_on_start_with_broker_flat_proceeds_normally(monkeypatch):
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": 0, "avgprice": 0.0})
    enter_called = {"n": 0}
    async def _spy():
        enter_called["n"] += 1
    monkeypatch.setattr(engine, "_maybe_enter_on_start", _spy)

    run(engine._on_start())

    assert engine.pending_adoption is None
    assert enter_called["n"] == 1   # normal path proceeds


# ---------- adopt_position when no pending ----------

def test_adopt_with_no_pending_returns_error():
    engine.pending_adoption = None
    res = run(engine.adopt_position(True))
    assert res["ok"] is False


# ---------- reset() clears pending_adoption ----------

def test_reset_clears_pending_adoption():
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    engine.reset()
    assert engine.pending_adoption is None


# ---------- snapshot includes pending_adoption ----------

def test_snapshot_includes_pending_adoption_key():
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    snap = engine.snapshot()
    assert "pending_adoption" in snap
    assert snap["pending_adoption"]["qty"] == 65
    assert snap["pending_adoption"]["side"] == "SHORT"


def test_snapshot_pending_adoption_is_none_by_default():
    engine.pending_adoption = None
    snap = engine.snapshot()
    assert "pending_adoption" in snap
    assert snap["pending_adoption"] is None


# ---------- REGRESSION: legacy snapshot keys ----------

def test_snapshot_has_all_legacy_keys():
    snap = engine.snapshot()
    for key in ["running", "mode", "market_open", "feed_mode", "alert",
                "angel", "price", "settings", "bricks", "position",
                "pending_entry", "pending_exit", "orders", "metrics",
                "expiry", "risk", "pending_adoption"]:
        assert key in snap, f"missing snapshot key: {key}"


# ---------- API ROUTES (in-process TestClient, no live broker) ----------

@pytest.fixture
def client(monkeypatch):
    # Keep broker stubbed for API tests too
    monkeypatch.setattr(engine.broker, "connected", True, raising=False)
    monkeypatch.setattr(engine.broker, "get_net_position",
                        lambda: {"found": True, "netqty": -65, "avgprice": 24000.0},
                        raising=False)
    def _no_real_order(*args, **kwargs):
        raise AssertionError("place_limit_order must NOT be called by API")
    monkeypatch.setattr(engine.broker, "place_limit_order", _no_real_order, raising=False)
    return TestClient(server.app)


def test_api_state_includes_pending_adoption(client):
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert "pending_adoption" in body
    assert body["pending_adoption"]["side"] == "SHORT"
    assert body["pending_adoption"]["qty"] == 65


def test_api_bot_adopt_routes_to_engine(client):
    # Seed a pending SHORT and confirm via API
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    r = client.post("/api/bot/adopt", json={"confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert engine.position is not None
    assert engine.position["side"] == "SHORT"
    assert engine.position["entry_order_id"] == "ADOPTED_MANUAL"
    assert engine.pending_adoption is None


def test_api_bot_adopt_decline(client):
    engine.pending_adoption = {"qty": 65, "avgprice": 24000.0, "netqty": -65,
                                "side": "SHORT", "declined": False}
    r = client.post("/api/bot/adopt", json={"confirm": False})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert engine.position is None
    assert engine.pending_adoption is not None
    assert engine.pending_adoption["declined"] is True


def test_api_bot_adopt_no_pending(client):
    engine.pending_adoption = None
    r = client.post("/api/bot/adopt", json={"confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
