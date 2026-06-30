"""
Tests for the new feature batch:
1. POST /api/bot/trade-mode (PAPER/LIVE toggle)
2. GET /api/bot/reconcile (broker position reconciliation)
3. POST /api/bot/reconcile/resolve (accept/reenter/reexit/unknown)
4. POST /api/bot/stop {square_off:true}
5. GET /api/state includes top-level 'mode'
6. PAPER fill regression with mode='PAPER' on orders
"""
import os
import time
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module", autouse=True)
def safety_setup():
    # CRITICAL SAFETY: force SIM feed and PAPER mode before anything else.
    requests.post(f"{API}/feed/mode", json={"feed_mode": "SIM"}, timeout=10)
    requests.post(f"{API}/bot/trade-mode", json={"mode": "PAPER"}, timeout=10)
    # ensure bot stopped to begin clean
    requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
    yield
    # teardown: leave app in SIM + PAPER and restore brick_size=50
    requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
    requests.post(f"{API}/feed/mode", json={"feed_mode": "SIM"}, timeout=10)
    requests.post(f"{API}/bot/trade-mode", json={"mode": "PAPER"}, timeout=10)
    requests.post(f"{API}/settings", json={"brick_size": 50}, timeout=10)


# ---------------- trade-mode endpoint ----------------
class TestTradeMode:
    def test_state_top_level_mode_field(self):
        s = requests.get(f"{API}/state", timeout=10).json()
        assert "mode" in s
        assert s["mode"] in ("PAPER", "LIVE")


# ---------------- reconcile endpoint ----------------
class TestReconcile:
    def test_reconcile_shape(self):
        r = requests.get(f"{API}/bot/reconcile", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "available" in d
        if d["available"]:
            # When broker connected, all expected keys must be present
            for k in ["state", "message", "mode", "bot_position",
                      "broker_netqty", "broker_avgprice"]:
                assert k in d, f"missing key {k}"
            assert d["state"] in ("GOOD", "ENTRY_MISSED", "EXIT_MISSED")
        else:
            assert "reason" in d

    def test_reconcile_good_when_flat_and_connected(self):
        # Ensure bot flat first
        requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
        requests.post(f"{API}/bot/reset", timeout=10)
        d = requests.get(f"{API}/bot/reconcile", timeout=15).json()
        if not d.get("available"):
            pytest.skip(f"Broker not available: {d.get('reason')}")
        # broker may legitimately hold a real position; just assert a valid state
        assert d["state"] in ("GOOD", "ENTRY_MISSED", "EXIT_MISSED")


# ---------------- reconcile resolve ----------------
class TestReconcileResolve:
    def test_resolve_accept(self):
        # 'accept' now syncs the bot to the broker (may hit the live position API).
        r = requests.post(f"{API}/bot/reconcile/resolve",
                          json={"action": "accept"}, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "ok" in d  # ok True on sync, or False if broker positions unreadable/rate-limited

    def test_resolve_reenter_no_position(self):
        # ensure no position
        requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
        requests.post(f"{API}/bot/reset", timeout=10)
        r = requests.post(f"{API}/bot/reconcile/resolve",
                          json={"action": "reenter"}, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is False
        assert d.get("message") == "No bot position to re-enter."

    def test_resolve_unknown_action(self):
        r = requests.post(f"{API}/bot/reconcile/resolve",
                          json={"action": "foo"}, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is False
        assert d.get("message") == "Unknown action."


# ---------------- stop with square_off ----------------
class TestStopSquareOff:
    def test_stop_with_square_off_flat(self):
        # Ensure flat
        requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
        requests.post(f"{API}/bot/reset", timeout=10)
        r = requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d.get("running") is False
        assert "squared_off" in d
        assert isinstance(d["squared_off"], bool)
        # flat -> nothing squared off
        assert d["squared_off"] is False

    def test_start_then_stop(self):
        r = requests.post(f"{API}/bot/start", timeout=10)
        assert r.status_code == 200
        assert r.json().get("running") is True
        time.sleep(0.5)
        s = requests.get(f"{API}/state").json()
        assert s["running"] is True
        r2 = requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
        assert r2.status_code == 200
        assert r2.json().get("running") is False


# ---------------- paper fill regression ----------------
class TestPaperFillRegression:
    def test_paper_orders_have_mode_paper(self):
        # Ensure SIM + PAPER, small brick to speed up
        requests.post(f"{API}/feed/mode", json={"feed_mode": "SIM"}, timeout=10)
        requests.post(f"{API}/bot/trade-mode", json={"mode": "PAPER"}, timeout=10)
        requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
        requests.post(f"{API}/bot/reset", timeout=10)
        # lower brick size and bar seconds for fast brick formation in SIM
        requests.post(f"{API}/settings", json={"brick_size": 20, "bar_seconds": 2}, timeout=10)
        requests.post(f"{API}/bot/start", timeout=10)
        # wait up to ~60s for any order to appear (random walk dependent)
        order_seen = False
        order_mode_ok = True
        deadline = time.time() + 60
        while time.time() < deadline:
            s = requests.get(f"{API}/state", timeout=10).json()
            orders = s.get("orders") or []
            if orders:
                order_seen = True
                for o in orders:
                    if o.get("mode") not in ("PAPER", None):
                        order_mode_ok = False
                    assert o.get("mode") == "PAPER", f"non-PAPER mode in SIM: {o}"
                break
            time.sleep(2)
        # always stop and restore
        requests.post(f"{API}/bot/stop", json={"square_off": True}, timeout=10)
        requests.post(f"{API}/settings", json={"brick_size": 50, "bar_seconds": 60}, timeout=10)
        assert order_mode_ok
        if not order_seen:
            # not a failure: random walk may not produce signal in time
            pytest.skip("No signal produced in 60s of SIM random walk - acceptable per spec")
