"""Backend API tests for Renko Algo Trading Bot."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback for local pytest run when env not exported
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.strip().split("=", 1)[1].rstrip("/")
                    break
    except Exception:
        pass
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module", autouse=True)
def reset_before(session):
    """Reset bot state before tests; stop after."""
    session.post(f"{API}/bot/stop")
    session.post(f"{API}/bot/reset")
    yield
    session.post(f"{API}/bot/stop")


# ------------------ /api/state snapshot ------------------
class TestState:
    def test_state_keys(self, session):
        r = session.get(f"{API}/state")
        assert r.status_code == 200
        d = r.json()
        for k in ["running", "mode", "price", "settings", "bricks", "position", "orders", "metrics"]:
            assert k in d, f"missing key {k}"
        assert d["mode"] == "DEMO"
        assert isinstance(d["bricks"], list)
        assert isinstance(d["orders"], list)
        assert d["settings"]["lot_size"] == 65
        assert d["settings"]["brick_size"] == 50


# ------------------ Settings ------------------
class TestSettings:
    def test_update_settings(self, session):
        payload = {"brick_size": 20, "lot_size": 65, "buffer_points": 5,
                   "max_red_single_green": 4, "greens_to_exit_extended": 2}
        r = session.post(f"{API}/settings", json=payload)
        assert r.status_code == 200
        s = r.json()
        assert s["brick_size"] == 20
        assert s["buffer_points"] == 5

        # verify via /state
        r2 = session.get(f"{API}/state")
        assert r2.json()["settings"]["brick_size"] == 20


# ------------------ Start: price changes ------------------
class TestBotStartPriceMoves:
    def test_start_and_price_change(self, session):
        r = session.post(f"{API}/bot/start")
        assert r.status_code == 200
        assert r.json()["running"] is True

        s1 = session.get(f"{API}/state").json()
        p1 = s1["price"]
        assert s1["running"] is True
        time.sleep(5)
        s2 = session.get(f"{API}/state").json()
        p2 = s2["price"]
        assert p1 != p2, f"price did not change after 5s ({p1} -> {p2})"


# ------------------ Bricks build & color/structure ------------------
class TestBricksBuild:
    def test_bricks_grow(self, session):
        # Reset and start with small brick_size for speed
        session.post(f"{API}/bot/stop")
        session.post(f"{API}/bot/reset")
        session.post(f"{API}/settings", json={"brick_size": 20})
        session.post(f"{API}/bot/start")
        bs = session.get(f"{API}/state").json()["settings"]["brick_size"]

        # Poll for up to 90s waiting for bricks
        end = time.time() + 90
        bricks = []
        while time.time() < end:
            bricks = session.get(f"{API}/state").json()["bricks"]
            if len(bricks) >= 3:
                break
            time.sleep(2)
        assert len(bricks) >= 1, "no bricks formed within 90s"
        for b in bricks:
            assert b["color"] in ("green", "red")
            assert abs(abs(b["close"] - b["open"]) - bs) < 1e-6, f"brick size mismatch: {b}"


# ------------------ Strategy entry/exit/orders/trades ------------------
class TestStrategyFlow:
    def test_short_entry_buffer_and_exit(self, session):
        """Wait for engine to produce at least one SHORT entry. Validate order buffer & types.
        Then wait for a COVER (exit) and verify trade record + pnl formula."""
        # ensure running with small brick size to speed things up
        session.post(f"{API}/bot/start")
        sets = session.get(f"{API}/state").json()["settings"]
        buf = sets["buffer_points"]
        lot = sets["lot_size"]

        # Wait up to 3 minutes for a SELL order to appear
        sell_order = None
        end = time.time() + 180
        while time.time() < end:
            st = session.get(f"{API}/state").json()
            orders = st["orders"]
            for o in orders:
                if o["side"] == "SELL" and o["kind"] == "ENTRY":
                    sell_order = o
                    break
            if sell_order:
                break
            time.sleep(2)
        assert sell_order is not None, "no SHORT entry SELL order in 180s"

        # Validate SELL limit order
        assert sell_order["order_type"] == "LIMIT"
        assert sell_order["qty"] == lot == 65
        expected = round(sell_order["ref_price"] - buf, 2)
        assert abs(sell_order["limit_price"] - expected) < 0.01, \
            f"SELL limit_price {sell_order['limit_price']} != ref - buffer ({expected})"

        # Wait for sell order to COMPLETE (max 15s)
        end = time.time() + 15
        while time.time() < end:
            st = session.get(f"{API}/state").json()
            found = next((x for x in st["orders"] if x["id"] == sell_order["id"]), None)
            if found and found["status"] == "COMPLETE":
                sell_order = found
                break
            time.sleep(1)
        assert sell_order["status"] == "COMPLETE", f"entry order didn't complete: {sell_order['status']}"
        assert sell_order["fill_price"] is not None

        # Position must be SHORT
        st = session.get(f"{API}/state").json()
        # The position might have already closed by quick cover; check more leniently
        if st["position"]:
            assert st["position"]["side"] == "SHORT"
            assert st["position"]["qty"] == 65

        # Wait for a BUY EXIT order and complete
        buy_order = None
        end = time.time() + 240
        while time.time() < end:
            st = session.get(f"{API}/state").json()
            for o in st["orders"]:
                if o["side"] == "BUY" and o["kind"] == "EXIT" and o["status"] == "COMPLETE":
                    buy_order = o
                    break
            if buy_order:
                break
            time.sleep(2)
        assert buy_order is not None, "no BUY exit order completed in 240s"
        assert buy_order["order_type"] == "LIMIT"
        # BUY limit = ref + buffer (initial attempt). If retried, attempts=2 and wider buffer used.
        if buy_order["attempts"] == 1:
            expected_b = round(buy_order["ref_price"] + buf, 2)
        else:
            expected_b = round(buy_order["ref_price"] + 2 * buf, 2)
        assert abs(buy_order["limit_price"] - expected_b) < 0.01, \
            f"BUY limit_price {buy_order['limit_price']} != ref+buffer ({expected_b})"

        # Validate trade recorded with correct pnl formula
        time.sleep(2)
        trades = session.get(f"{API}/trades").json()
        assert isinstance(trades, list) and len(trades) >= 1
        t = trades[0]  # latest
        assert t["side"] == "SHORT"
        assert t["qty"] == 65
        expected_pnl = round((t["entry_price"] - t["exit_price"]) * 65, 2)
        assert abs(t["pnl"] - expected_pnl) < 0.01, f"pnl {t['pnl']} != {expected_pnl}"


# ------------------ Order retry simulation ------------------
class TestOrderRetry:
    def test_all_orders_eventually_complete(self, session):
        st = session.get(f"{API}/state").json()
        # Wait a bit so any RETRYING ones complete (max retry = 5s)
        time.sleep(7)
        st = session.get(f"{API}/state").json()
        for o in st["orders"]:
            assert o["status"] == "COMPLETE", f"order {o['id']} stuck at {o['status']}"
            assert o["fill_price"] is not None


# ------------------ Angel config: stays DEMO ------------------
class TestAngelConfig:
    def test_angel_remains_demo(self, session):
        r = session.post(f"{API}/angel/config", json={"api_key": "DUMMY_KEY", "client_id": "DUMMY_CID"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["mode"] == "DEMO"
        st = session.get(f"{API}/state").json()
        assert st["mode"] == "DEMO"


# ------------------ Reset clears state ------------------
class TestReset:
    def test_reset_clears(self, session):
        r = session.post(f"{API}/bot/reset")
        assert r.status_code == 200
        st = session.get(f"{API}/state").json()
        assert st["running"] is False
        assert st["bricks"] == []
        assert st["position"] is None
        assert st["metrics"]["trades"] == 0
        assert st["metrics"]["realized_pnl"] == 0.0
        trades = session.get(f"{API}/trades").json()
        assert trades == []
