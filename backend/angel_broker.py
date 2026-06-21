"""Angel One SmartAPI broker wrapper.

LIVE DATA + PAPER orders: this module logs in and streams the real NIFTY
futures LTP. Order placement is intentionally NOT wired to real placeOrder yet
(the bot stays in paper/demo mode). All calls are synchronous (requests-based)
and must be invoked from the engine via asyncio.to_thread.
"""
import logging
from datetime import datetime, date

import pyotp
import requests
from SmartApi import SmartConnect

logger = logging.getLogger("renko-bot.angel")

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)


class AngelBroker:
    def __init__(self):
        self.smart = None
        self.connected = False
        self.client_code = ""
        self.profile_name = ""
        self.error = ""
        # resolved NIFTY current-month future
        self.fut_symbol = ""
        self.fut_token = ""
        self.fut_expiry = ""
        self.fut_lotsize = None

    def login(self, api_key, client_code, pin, totp_secret):
        self.error = ""
        try:
            self.smart = SmartConnect(api_key=api_key)
            otp = pyotp.TOTP(totp_secret).now()
            data = self.smart.generateSession(client_code, pin, otp)
            if not data.get("status"):
                self.connected = False
                self.error = str(data.get("message") or data)
                logger.warning("Angel login failed: %s", self.error)
                return {"connected": False, "error": self.error}
            self.client_code = client_code
            try:
                prof = self.smart.getProfile(data["data"].get("refreshToken", ""))
                self.profile_name = prof.get("data", {}).get("name", "") if prof else ""
            except Exception:
                self.profile_name = ""
            self.connected = True
            self._resolve_nifty_fut()
            logger.info("Angel connected (%s), future=%s token=%s", client_code,
                        self.fut_symbol, self.fut_token)
            return {"connected": True, "future": self.fut_symbol, "token": self.fut_token,
                    "expiry": self.fut_expiry, "name": self.profile_name}
        except Exception as e:
            self.connected = False
            self.error = str(e)
            logger.exception("Angel login exception")
            return {"connected": False, "error": self.error}

    def _resolve_nifty_fut(self):
        """Find the nearest-expiry NIFTY index future from the scrip master."""
        try:
            rows = requests.get(SCRIP_MASTER_URL, timeout=30).json()
        except Exception as e:
            logger.warning("scrip master download failed: %s", e)
            return
        today = date.today()
        best = None
        best_exp = None
        for r in rows:
            if r.get("exch_seg") != "NFO" or r.get("instrumenttype") != "FUTIDX":
                continue
            if r.get("name") != "NIFTY":
                continue
            exp_raw = r.get("expiry", "")
            try:
                exp = datetime.strptime(exp_raw, "%d%b%Y").date()
            except Exception:
                continue
            if exp < today:
                continue
            if best_exp is None or exp < best_exp:
                best_exp, best = exp, r
        if best:
            self.fut_symbol = best.get("symbol", "")
            self.fut_token = str(best.get("token", ""))
            self.fut_expiry = best_exp.isoformat()
            try:
                self.fut_lotsize = int(best.get("lotsize"))
            except Exception:
                self.fut_lotsize = None

    def get_ltp(self):
        if not self.connected or not self.fut_token:
            return None
        try:
            res = self.smart.ltpData("NFO", self.fut_symbol, self.fut_token)
            if res.get("status"):
                return float(res["data"]["ltp"])
            self.error = str(res.get("message") or res)
        except Exception as e:
            self.error = str(e)
            logger.warning("get_ltp failed: %s", e)
        return None

    def get_history(self, interval, from_dt, to_dt):
        """Fetch historical candles. from_dt/to_dt format: 'YYYY-MM-DD HH:MM'.
        Returns list of [timestamp, open, high, low, close, volume]."""
        if not self.connected or not self.fut_token:
            return None
        try:
            params = {
                "exchange": "NFO",
                "symboltoken": self.fut_token,
                "interval": interval,
                "fromdate": from_dt,
                "todate": to_dt,
            }
            res = self.smart.getCandleData(params)
            if res.get("status"):
                return res.get("data") or []
            self.error = str(res.get("message") or res)
        except Exception as e:
            self.error = str(e)
            logger.warning("get_history failed: %s", e)
        return None

    def logout(self):
        try:
            if self.smart and self.client_code:
                self.smart.terminateSession(self.client_code)
        except Exception:
            pass
        self.connected = False

    def status(self):
        return {
            "connected": self.connected,
            "client_code": self.client_code,
            "name": self.profile_name,
            "future": self.fut_symbol,
            "token": self.fut_token,
            "expiry": self.fut_expiry,
            "lotsize": self.fut_lotsize,
            "error": self.error,
        }
