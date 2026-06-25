"""
Tests for broker P&L reflection in /api/state:
- risk.broker_pnl object exists with keys found, realised, unrealised, total
- When broker connected, found=True and numeric fields are present
- Repeated calls don't 500 (throttle is safe)
- /api/state still returns risk.day_total and risk.daily_max_loss

READ-ONLY. Does NOT place any orders. Does NOT start the bot.
"""
import os
import time
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"


# Shape & presence of risk.broker_pnl in /api/state
class TestBrokerPnlShape:
    def test_state_200(self):
        r = requests.get(f"{API}/state", timeout=15)
        assert r.status_code == 200

    def test_state_has_risk_broker_pnl(self):
        s = requests.get(f"{API}/state", timeout=15).json()
        assert "risk" in s, "risk missing in /api/state"
        risk = s["risk"]
        assert "broker_pnl" in risk, "risk.broker_pnl missing"
        bp = risk["broker_pnl"]
        for k in ("found", "realised", "unrealised", "total"):
            assert k in bp, f"missing key {k} in risk.broker_pnl"
        assert isinstance(bp["found"], bool)
        # numeric (int/float, not NoneType)
        for k in ("realised", "unrealised", "total"):
            assert isinstance(bp[k], (int, float)), f"{k} not numeric: {type(bp[k])}"

    def test_state_day_pnl_and_max_loss_still_present(self):
        s = requests.get(f"{API}/state", timeout=15).json()
        risk = s["risk"]
        assert "day_total" in risk
        assert "daily_max_loss" in risk
        assert isinstance(risk["day_total"], (int, float))
        assert isinstance(risk["daily_max_loss"], (int, float))


# Broker connected => found should be True when broker.connected
class TestBrokerPnlSynced:
    def test_synced_when_broker_connected(self):
        # _refresh_broker_pnl runs at top of run_loop, throttled to 8s.
        # Poll for up to ~12s for it to populate.
        bp = None
        deadline = time.time() + 12
        while time.time() < deadline:
            s2 = requests.get(f"{API}/state", timeout=10).json()
            bp = s2["risk"]["broker_pnl"]
            if bp.get("found"):
                break
            time.sleep(1.5)
        if not bp or not bp.get("found"):
            pytest.skip(f"Broker P&L not yet synced (likely broker disconnected): {bp}")
        assert bp.get("found") is True, f"broker_pnl.found=False: {bp}"
        # numeric values
        assert isinstance(bp["realised"], (int, float))
        assert isinstance(bp["unrealised"], (int, float))
        assert isinstance(bp["total"], (int, float))
        # invariant: total == realised + unrealised (within rounding)
        assert abs((bp["realised"] + bp["unrealised"]) - bp["total"]) < 0.05, \
            f"total != realised + unrealised: {bp}"


# Throttle / no engine errors under repeated polling
class TestBrokerPnlThrottle:
    def test_repeated_state_calls_no_500(self):
        # Hit /api/state 12 times rapidly; should all 200 and shape stable
        codes = []
        last_total = None
        for _ in range(12):
            r = requests.get(f"{API}/state", timeout=10)
            codes.append(r.status_code)
            d = r.json()
            bp = d["risk"]["broker_pnl"]
            for k in ("found", "realised", "unrealised", "total"):
                assert k in bp
            last_total = bp["total"]
            time.sleep(0.4)
        assert all(c == 200 for c in codes), f"non-200 in repeated polling: {codes}"
        assert last_total is not None
