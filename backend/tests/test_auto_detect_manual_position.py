"""Tests for `_auto_detect_manual_position` — the RUNNING-mode broker poll that
catches an untracked manual short while the bot is live, so it never stacks.

Coverage (per review_request):
  - while running & flat & pending_adoption=None & broker SHORT -> sets pending_adoption (warning), NO order
  - THROTTLE: 2nd rapid call (within 30s) does NOT re-hit broker.get_net_position
  - NO-OP when engine.position already set
  - NO-OP when pending_adoption already set
  - NO-OP when broker disconnected
  - LONG detection -> pending_adoption side='LONG' with error alert
  - BROKER FLAT (netqty=0) -> pending_adoption stays None
  - Once pending_adoption is set, _entries_blocked() == True (no stacking)
  - adopt_position(True) on the SHORT promotes to engine.position, clears pending_adoption,
    and does NOT place an order (state-only).

All broker IO mocked. NO live calls, NO orders. /api/bot/start is NEVER invoked.
"""
import sys
import asyncio
import importlib
import pytest

sys.path.insert(0, '/app/backend')
server = importlib.import_module("server")
engine = server.engine


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Counter:
    """Wraps a callable so we can assert its invocation count."""
    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.fn(*a, **kw)


@pytest.fixture(autouse=True)
def reset_engine_state(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(engine, "_persist_state", _noop)

    # Broker: connected by default, no-op order spy
    monkeypatch.setattr(engine.broker, "connected", True, raising=False)
    monkeypatch.setattr(engine.broker, "fut_token", "FUT123", raising=False)

    def _no_real_order(*args, **kwargs):
        raise AssertionError("place_limit_order must NOT be called during auto-detect/adopt flow")
    monkeypatch.setattr(engine.broker, "place_limit_order", _no_real_order, raising=False)

    # Engine state — running & flat, throttle bypassed
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
    engine._last_pos_check = 0.0
    yield


# ---------- Main case: SHORT detected ----------

def test_auto_detect_short_sets_pending_adoption(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    run(engine._auto_detect_manual_position())

    assert spy.calls == 1
    pa = engine.pending_adoption
    assert pa is not None
    assert pa["qty"] == 65
    assert pa["side"] == "SHORT"
    assert pa["netqty"] == -65
    assert pa["avgprice"] == 24000.0
    assert pa["declined"] is False
    # No tracked position; no order placed (spy raises if called)
    assert engine.position is None
    # Warning alert surfaced
    assert engine.alert is not None
    assert engine.alert.get("level") == "warning"
    msg = engine.alert.get("msg", "")
    assert "SHORT" in msg and "65" in msg


# ---------- Throttle ----------

def test_auto_detect_is_throttled_within_30s(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    run(engine._auto_detect_manual_position())  # first call -> hits broker
    # 2nd rapid call: pending_adoption is now set AND throttle window not expired
    run(engine._auto_detect_manual_position())

    # Spy must NOT have been called twice
    assert spy.calls == 1


def test_auto_detect_throttle_blocks_even_if_pending_cleared(monkeypatch):
    """Throttle alone should block a 2nd broker hit within 30s, independent of pending_adoption."""
    spy = _Counter(lambda: {"found": True, "netqty": 0, "avgprice": 0.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    run(engine._auto_detect_manual_position())  # FLAT -> updates _last_pos_check, no pending
    assert spy.calls == 1
    assert engine.pending_adoption is None
    # Immediate 2nd call: pending_adoption still None, but throttle should block
    run(engine._auto_detect_manual_position())
    assert spy.calls == 1


# ---------- NO-OP guards ----------

def test_noop_when_already_tracking_position(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)
    engine.position = {
        "side": "SHORT", "qty": 65, "entry_price": 24000.0,
        "entry_time": "now", "entry_order_id": "X", "reds_at_entry": 2, "unrealized_pnl": 0.0,
    }

    run(engine._auto_detect_manual_position())
    assert spy.calls == 0
    assert engine.pending_adoption is None


def test_noop_when_pending_adoption_already_set(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)
    engine.pending_adoption = {"qty": 50, "avgprice": 23900.0, "netqty": -50,
                               "side": "SHORT", "declined": False}

    run(engine._auto_detect_manual_position())
    assert spy.calls == 0
    # Existing pending_adoption untouched
    assert engine.pending_adoption["qty"] == 50


def test_noop_when_broker_disconnected(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)
    monkeypatch.setattr(engine.broker, "connected", False, raising=False)

    run(engine._auto_detect_manual_position())
    assert spy.calls == 0
    assert engine.pending_adoption is None


# ---------- LONG detection ----------

def test_auto_detect_long_sets_pending_adoption_with_error(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": 50, "avgprice": 24010.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    run(engine._auto_detect_manual_position())

    assert spy.calls == 1
    pa = engine.pending_adoption
    assert pa is not None
    assert pa["side"] == "LONG"
    assert pa["qty"] == 50
    assert pa["netqty"] == 50
    assert pa["declined"] is False
    assert engine.position is None
    assert engine.alert is not None
    assert engine.alert.get("level") == "error"


# ---------- Broker FLAT ----------

def test_auto_detect_broker_flat_leaves_pending_none(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": 0, "avgprice": 0.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    run(engine._auto_detect_manual_position())

    assert spy.calls == 1
    assert engine.pending_adoption is None


def test_auto_detect_broker_not_found_leaves_pending_none(monkeypatch):
    spy = _Counter(lambda: {"found": False})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    run(engine._auto_detect_manual_position())

    assert spy.calls == 1
    assert engine.pending_adoption is None


# ---------- Stacking-prevention integration ----------

def test_pending_adoption_blocks_entries(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    assert engine._entries_blocked() is False  # flat & no pending => not blocked
    run(engine._auto_detect_manual_position())
    assert engine.pending_adoption is not None
    assert engine._entries_blocked() is True  # pending_adoption set => blocked


def test_adopt_promotes_pending_to_position_no_order(monkeypatch):
    spy = _Counter(lambda: {"found": True, "netqty": -65, "avgprice": 24000.0})
    monkeypatch.setattr(engine.broker, "get_net_position", spy, raising=False)

    run(engine._auto_detect_manual_position())
    assert engine.pending_adoption is not None

    # Confirm adoption — must NOT place an order (place_limit_order is a hard-fail spy)
    result = run(engine.adopt_position(True))
    assert result["ok"] is True
    assert engine.position is not None
    assert engine.position["side"] == "SHORT"
    assert engine.position["qty"] == 65
    assert engine.position["entry_price"] == 24000.0
    assert engine.position["entry_order_id"] == "ADOPTED_MANUAL"
    assert engine.pending_adoption is None
    # Entries no longer blocked by pending_adoption (still has the live position though)
    assert engine._entries_blocked() is False
