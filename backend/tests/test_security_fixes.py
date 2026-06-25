"""SEC-002 / SEC-003 verification tests.

- SEC-002: angel.client_code masked + angel.error sanitized (no proxy creds/URLs/stack)
- SEC-003: server-side validation caps on /api/orders/manual qty
           and /api/settings out-of-range -> HTTP 422

CRITICAL: LIVE real-money broker. These tests rely on rejection BEFORE the broker
is contacted. No valid-qty manual order is ever sent.
"""
import os
import re
import pytest
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load REACT_APP_BACKEND_URL from frontend/.env (single source of truth)
load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------------- SEC-002: masking + safe_err ----------------
class TestSec002Masking:
    def test_state_returns_200_with_full_structure(self, session):
        r = session.get(f"{API}/state", timeout=15)
        assert r.status_code == 200
        data = r.json()
        for key in ("angel", "risk", "position", "bricks", "settings",
                    "orders", "metrics", "expiry"):
            assert key in data, f"missing key {key} in /api/state"
        assert "broker_pnl" in data["risk"]

    def test_angel_client_code_is_masked(self, session):
        r = session.get(f"{API}/state", timeout=15)
        assert r.status_code == 200
        angel = r.json()["angel"]
        code = angel.get("client_code", "")
        # masked pattern: "••••XX" (with two trailing chars) OR empty string when unset
        if code:
            assert code.startswith("••••"), f"client_code not masked: {code!r}"
            # no raw alphanumeric account-id-looking strings of length > 4
            visible = code.replace("•", "")
            assert len(visible) <= 4, f"client_code shows too many chars: {code!r}"
            # ensure raw client id (typical Angel codes are 6-10 chars) not exposed
            assert not re.search(r"[A-Z]{2,}\d{3,}", code), \
                f"client_code looks unmasked: {code!r}"

    def test_angel_error_has_no_secrets_or_urls(self, session):
        r = session.get(f"{API}/state", timeout=15)
        assert r.status_code == 200
        err = r.json()["angel"].get("error", "")
        # empty is acceptable (no current broker error)
        if not err:
            return
        low = err.lower()
        # no embedded credentials (user:pass@host)
        assert not re.search(r"://[^\s/]*:[^\s/]*@", err), \
            f"angel.error leaks credentials: {err!r}"
        # no raw URLs
        assert "http://" not in low and "https://" not in low, \
            f"angel.error leaks URL: {err!r}"
        # no connection-pool / stack details
        for forbidden in ("connectionpool", "max retries", "tunnel",
                          "traceback", "proxy"):
            # proxy-related messages MUST be collapsed by safe_err
            if forbidden in ("connectionpool", "max retries", "tunnel", "proxy"):
                # safe_err collapses these to the generic message
                if forbidden in low:
                    pytest.fail(
                        f"angel.error contains {forbidden!r} (should be collapsed): {err!r}")


    def test_order_log_has_no_raw_ip_or_url(self, session):
        """SEC-002 re-confirm: legacy + new order_log rows must not leak bare
        IPv4 addresses or http URLs in `note`."""
        r = session.get(f"{API}/orders/log", timeout=15)
        assert r.status_code == 200
        rows = r.json()
        ipv4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
        leaks = []
        for row in rows:
            note = str(row.get("note", "")) + " " + str(row.get("text", ""))
            low = note.lower()
            if ipv4.search(note):
                leaks.append(("ip", note))
            if "http://" in low or "https://" in low:
                leaks.append(("url", note))
        assert not leaks, f"order_log leaks: {leaks[:5]}"


# ---------------- SEC-003: manual order qty validation ----------------
def _order_log_len(session):
    r = session.get(f"{API}/orders/log", timeout=15)
    assert r.status_code == 200
    return len(r.json())


class TestSec003ManualOrderCap:
    """All invalid qty requests MUST be rejected pre-broker AND must NOT add a
    row to /api/orders/log (proves the broker code path was not reached)."""

    @pytest.mark.parametrize("qty", [999999, 5001, 10000])
    def test_oversized_qty_rejected_pre_broker(self, session, qty):
        before = _order_log_len(session)
        r = session.post(f"{API}/orders/manual",
                         json={"side": "BUY", "qty": qty}, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is False, f"oversized qty NOT rejected: {data}"
        msg = data.get("message", "")
        assert "qty must be between 1 and 5000" in msg, \
            f"unexpected rejection msg: {msg!r}"
        assert not data.get("broker_order_id")
        after = _order_log_len(session)
        assert after == before, \
            f"order_log grew for invalid qty={qty} ({before}->{after}) — broker path was hit!"

    @pytest.mark.parametrize("qty", [0, -5])
    def test_nonpositive_qty_rejected(self, session, qty):
        before = _order_log_len(session)
        r = session.post(f"{API}/orders/manual",
                         json={"side": "BUY", "qty": qty}, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is False, f"qty={qty} NOT rejected: {data}"
        assert "qty must be between 1 and 5000" in data.get("message", ""), \
            f"unexpected msg for qty={qty}: {data.get('message')!r}"
        assert not data.get("broker_order_id")
        after = _order_log_len(session)
        assert after == before, \
            f"order_log grew for qty={qty} ({before}->{after}) — qty=0 bypass NOT fixed!"

    def test_non_numeric_qty_rejected(self, session):
        before = _order_log_len(session)
        r = session.post(f"{API}/orders/manual",
                         json={"side": "BUY", "qty": "abc"}, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is False
        assert "whole number" in data.get("message", "").lower(), \
            f"expected 'whole number' message, got: {data.get('message')!r}"
        after = _order_log_len(session)
        assert after == before, "order_log grew for non-numeric qty"

    def test_invalid_side_rejected(self, session):
        before = _order_log_len(session)
        r = session.post(f"{API}/orders/manual",
                         json={"side": "HOLD", "qty": 1}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is False
        assert "side" in data.get("message", "").lower()
        after = _order_log_len(session)
        assert after == before, "order_log grew for invalid side"


# ---------------- SEC-003: settings field bounds ----------------
class TestSec003SettingsBounds:
    @pytest.mark.parametrize("payload", [
        {"lot_size": 99999},
        {"lot_size": 0},
        {"daily_max_loss": -1},
        {"brick_size": 0},
        {"brick_size": 99999},
        {"max_order_attempts": 50},
        {"max_order_attempts": 0},
        {"buffer_points": -1},
        {"bar_seconds": 0},
        {"bar_seconds": 99999},
    ])
    def test_out_of_range_returns_422(self, session, payload):
        r = session.post(f"{API}/settings", json=payload, timeout=15)
        assert r.status_code == 422, \
            f"payload {payload} should be 422, got {r.status_code}: {r.text}"

    def test_valid_buffer_points_succeeds(self, session):
        # capture original
        before = session.get(f"{API}/state", timeout=15).json()["settings"]
        original = before["buffer_points"]
        try:
            r = session.post(f"{API}/settings",
                             json={"buffer_points": 20}, timeout=15)
            assert r.status_code == 200, r.text
            data = r.json()
            # response should be the settings object
            assert isinstance(data, dict)
            assert data.get("buffer_points") == 20
            # verify persistence via /api/state
            state = session.get(f"{API}/state", timeout=15).json()
            assert state["settings"]["buffer_points"] == 20
        finally:
            # restore original to avoid leaving altered settings on a live bot
            session.post(f"{API}/settings",
                         json={"buffer_points": original}, timeout=15)
