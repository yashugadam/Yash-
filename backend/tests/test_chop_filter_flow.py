"""Test the new chop filter + entry_bricks flow via the live backend API.
Focus: /api/auth/login, /api/state fields, /api/settings persistence + validation.
DO NOT invoke bot/start, bot/stop, orders/manual, or any real-money endpoints.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fall back to frontend/.env
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass

USERNAME = "Yashgadam"
PASSWORD = "Yashbgadam@1994"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": USERNAME, "password": PASSWORD}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "token" in data and isinstance(data["token"], str) and len(data["token"]) > 20
    assert data.get("username") == USERNAME
    return data["token"]


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---- 1. Login ----
class TestLogin:
    def test_login_returns_jwt(self, token):
        # JWT is header.payload.sig
        parts = token.split(".")
        assert len(parts) == 3, "token is not a JWT"

    def test_login_bad_password(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": USERNAME, "password": "wrong"}, timeout=10)
        assert r.status_code == 401


# ---- 2. /api/state has new fields ----
class TestStateFields:
    def test_state_exposes_chop_and_entry_fields(self, auth):
        r = requests.get(f"{BASE_URL}/api/state", headers=auth, timeout=10)
        assert r.status_code == 200
        s = r.json()
        # top-level fields per engine.snapshot()
        assert "chop_filter" in s
        assert isinstance(s["chop_filter"], bool)
        assert "chop_er" in s   # may be None (warming up)
        assert s["chop_er"] is None or isinstance(s["chop_er"], (int, float))
        assert "chop_threshold" in s
        assert isinstance(s["chop_threshold"], (int, float))
        assert "entry_bricks" in s
        assert isinstance(s["entry_bricks"], int)
        # settings dict
        st = s.get("settings") or {}
        assert "chop_filter" in st
        assert "chop_lookback" in st
        assert "chop_threshold" in st
        assert "entry_bricks" in st


# ---- 3. Settings persist ----
class TestSettingsPersist:
    def _get_settings(self, auth):
        r = requests.get(f"{BASE_URL}/api/state", headers=auth, timeout=10)
        assert r.status_code == 200
        return r.json().get("settings") or {}, r.json()

    def test_update_and_reset_chop_settings(self, auth):
        # ---- change to non-default values ----
        payload = {"chop_threshold": 0.4, "chop_lookback": 30, "entry_bricks": 3,
                   "chop_filter": True}
        r = requests.post(f"{BASE_URL}/api/settings", headers=auth, json=payload, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        # relay returns whatever leader returned; usually {ok:True, settings:{...}}
        # We don't strictly assert shape here — verify via GET /api/state below.
        assert body.get("ok") is not False, f"settings update returned not-ok: {body}"

        # small delay for leader relay
        time.sleep(1.5)
        settings, snap = self._get_settings(auth)
        assert abs(float(settings.get("chop_threshold", -1)) - 0.4) < 1e-6, settings
        assert int(settings.get("chop_lookback", -1)) == 30, settings
        assert int(settings.get("entry_bricks", -1)) == 3, settings
        assert bool(settings.get("chop_filter")) is True, settings
        # top-level snapshot fields reflect too
        assert int(snap.get("entry_bricks")) == 3
        assert abs(float(snap.get("chop_threshold")) - 0.4) < 1e-6

        # ---- reset back to chosen defaults ----
        reset = {"chop_threshold": 0.30, "chop_lookback": 50, "entry_bricks": 2,
                 "chop_filter": True}
        r2 = requests.post(f"{BASE_URL}/api/settings", headers=auth, json=reset, timeout=15)
        assert r2.status_code == 200
        assert r2.json().get("ok") is not False

        time.sleep(1.5)
        settings2, snap2 = self._get_settings(auth)
        assert abs(float(settings2.get("chop_threshold")) - 0.30) < 1e-6
        assert int(settings2.get("chop_lookback")) == 50
        assert int(settings2.get("entry_bricks")) == 2


# ---- 4. Validation ----
class TestSettingsValidation:
    def test_reject_out_of_range(self, auth):
        # Capture pre-values
        pre = requests.get(f"{BASE_URL}/api/state", headers=auth, timeout=10).json().get("settings") or {}

        bad = {"chop_threshold": 2, "entry_bricks": 99, "chop_lookback": 1}
        r = requests.post(f"{BASE_URL}/api/settings", headers=auth, json=bad, timeout=10)
        # Pydantic Field(ge/le) => 422
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

        # verify none of the bad values were persisted
        time.sleep(0.5)
        post = requests.get(f"{BASE_URL}/api/state", headers=auth, timeout=10).json().get("settings") or {}
        assert post.get("chop_threshold") == pre.get("chop_threshold")
        assert post.get("entry_bricks") == pre.get("entry_bricks")
        assert post.get("chop_lookback") == pre.get("chop_lookback")

    def test_reject_each_individually(self, auth):
        for bad in ({"chop_threshold": 1.5}, {"chop_threshold": -0.1},
                    {"entry_bricks": 0}, {"entry_bricks": 11},
                    {"chop_lookback": 1}, {"chop_lookback": 501}):
            r = requests.post(f"{BASE_URL}/api/settings", headers=auth, json=bad, timeout=10)
            assert r.status_code == 422, f"expected 422 for {bad}, got {r.status_code}"
