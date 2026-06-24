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

TICK_SIZE = 0.05   # NSE/NFO price tick — all order prices must be a multiple of this


def round_to_tick(price, tick=TICK_SIZE):
    """Round a price to the nearest valid exchange tick (0.05). Angel One rejects
    LIMIT orders whose price is not a multiple of the tick size."""
    return round(round(float(price) / tick) * tick, 2)


class AngelBroker:
    def __init__(self):
        self.smart = None
        self.connected = False
        self.api_key = ""
        self.client_code = ""
        self._pin = ""
        self._totp = ""
        self.profile_name = ""
        self.error = ""
        # resolved NIFTY current-month future
        self.fut_symbol = ""
        self.fut_token = ""
        self.fut_expiry = ""
        self.fut_lotsize = None
        self.fut_name = ""
        self.fut_type = ""
        self.futures = []

    def login(self, api_key, client_code, pin, totp_secret):
        self.error = ""
        self.api_key, self.client_code, self._pin, self._totp = api_key, client_code, pin, totp_secret
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
        self.futures = []          # cache of tradable NFO futures (NIFTY, BANKNIFTY, stocks…)
        best = None
        best_exp = None
        for r in rows:
            if r.get("exch_seg") != "NFO":
                continue
            if r.get("instrumenttype") not in ("FUTIDX", "FUTSTK"):
                continue
            exp_raw = r.get("expiry", "")
            try:
                exp = datetime.strptime(exp_raw, "%d%b%Y").date()
            except Exception:
                continue
            if exp < today:
                continue
            try:
                lot = int(r.get("lotsize"))
            except Exception:
                lot = None
            self.futures.append({
                "symbol": r.get("symbol", ""), "token": str(r.get("token", "")),
                "name": r.get("name", ""), "expiry": exp, "lotsize": lot,
                "type": r.get("instrumenttype"),
            })
            # default selection = nearest-expiry NIFTY index future
            if r.get("instrumenttype") == "FUTIDX" and r.get("name") == "NIFTY":
                if best_exp is None or exp < best_exp:
                    best_exp, best = exp, r
        if best and not self.fut_token:
            self.fut_symbol = best.get("symbol", "")
            self.fut_token = str(best.get("token", ""))
            self.fut_expiry = best_exp.isoformat()
            self.fut_name = best.get("name", "NIFTY")
            self.fut_type = best.get("instrumenttype", "FUTIDX")
            try:
                self.fut_lotsize = int(best.get("lotsize"))
            except Exception:
                self.fut_lotsize = None

    def search_futures(self, query="", limit=40):
        q = (query or "").upper().strip()
        items = self.futures
        if q:
            items = [f for f in items if q in f["symbol"].upper() or q in f["name"].upper()]
        # index futures first, then by name + expiry
        items = sorted(items, key=lambda f: (f["type"] != "FUTIDX", f["name"], f["expiry"]))[:limit]
        return [{"symbol": f["symbol"], "token": f["token"], "name": f["name"],
                 "expiry": f["expiry"].isoformat(), "lotsize": f["lotsize"], "type": f["type"]}
                for f in items]

    def select_instrument(self, token):
        for f in self.futures:
            if f["token"] == str(token):
                self.fut_symbol = f["symbol"]
                self.fut_token = f["token"]
                self.fut_expiry = f["expiry"].isoformat()
                self.fut_lotsize = f["lotsize"]
                self.fut_name = f["name"]
                self.fut_type = f["type"]
                logger.info("Instrument selected: %s (%s)", self.fut_symbol, self.fut_token)
                return {"ok": True, "symbol": self.fut_symbol, "token": self.fut_token,
                        "expiry": self.fut_expiry, "lotsize": self.fut_lotsize}
        return {"ok": False, "error": "Instrument not found"}

    def roll_to_next(self):
        """Switch to the nearest non-expired contract of the same underlying (auto-roll)."""
        if not self.futures:
            return {"ok": False, "error": "no futures cache"}
        today = date.today()
        cands = sorted(
            [f for f in self.futures if f["name"] == self.fut_name
             and f["type"] == self.fut_type and f["expiry"] >= today],
            key=lambda f: f["expiry"])
        if not cands:
            return {"ok": False, "error": "no next contract found"}
        nxt = cands[0]
        if nxt["token"] == self.fut_token:
            return {"ok": False, "error": "already on current contract"}
        return self.select_instrument(nxt["token"])

    def relogin(self):
        """Re-establish the session (e.g. after token expiry) using stored creds."""
        if not (self.api_key and self.client_code and self._pin and self._totp):
            return False
        res = self.login(self.api_key, self.client_code, self._pin, self._totp)
        return bool(res.get("connected"))

    def get_ltp(self):
        if not self.connected or not self.fut_token:
            return None
        for attempt in range(2):
            try:
                res = self.smart.ltpData("NFO", self.fut_symbol, self.fut_token)
                if res.get("status"):
                    return float(res["data"]["ltp"])
                self.error = str(res.get("message") or res)
            except Exception as e:
                self.error = str(e)
                logger.warning("get_ltp failed (attempt %d): %s", attempt + 1, e)
            # try one auto-relogin then retry (handles session/token expiry)
            if attempt == 0:
                logger.info("Attempting Angel auto-relogin…")
                if not self.relogin():
                    break
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

    # -------- real order placement / positions (LIVE trading) --------
    def place_limit_order(self, transactiontype, price, quantity):
        """Place a LIMIT, CARRYFORWARD (NRML) order on the selected future.
        transactiontype = 'BUY' | 'SELL'. Returns {ok, orderid, error}."""
        if not self.connected or not self.fut_token:
            return {"ok": False, "error": "broker not connected"}
        params = {
            "variety": "NORMAL",
            "tradingsymbol": self.fut_symbol,
            "symboltoken": str(self.fut_token),
            "transactiontype": transactiontype,
            "exchange": "NFO",
            "ordertype": "LIMIT",
            "producttype": "CARRYFORWARD",   # overnight carry-forward (NRML)
            "duration": "DAY",
            "price": str(round_to_tick(price)),
            "quantity": str(int(quantity)),
            "squareoff": "0",
            "stoploss": "0",
        }
        for attempt in range(2):
            try:
                resp = self.smart.placeOrderFullResponse(params)
                if resp.get("status"):
                    d = resp.get("data") or {}
                    oid = d.get("orderid") or d.get("orderId")
                    logger.info("LIVE order placed: %s %s @ %s qty=%s id=%s",
                                transactiontype, self.fut_symbol, price, quantity, oid)
                    return {"ok": True, "orderid": oid,
                            "uniqueorderid": d.get("uniqueorderid")}
                self.error = str(resp.get("message") or resp)
            except Exception as e:
                self.error = str(e)
                logger.warning("placeOrder failed (attempt %d): %s", attempt + 1, e)
            if attempt == 0 and not self.relogin():
                break
        return {"ok": False, "error": self.error}

    def modify_order_price(self, orderid, price, quantity, transactiontype):
        """Re-price a working LIMIT order (used to escalate the buffer on retries)."""
        params = {
            "variety": "NORMAL",
            "orderid": str(orderid),
            "tradingsymbol": self.fut_symbol,
            "symboltoken": str(self.fut_token),
            "transactiontype": transactiontype,
            "exchange": "NFO",
            "ordertype": "LIMIT",
            "producttype": "CARRYFORWARD",
            "duration": "DAY",
            "price": str(round_to_tick(price)),
            "quantity": str(int(quantity)),
        }
        try:
            resp = self.smart.modifyOrder(params)
            return {"ok": bool(resp.get("status")), "error": str(resp.get("message") or "")}
        except Exception as e:
            self.error = str(e)
            logger.warning("modifyOrder failed: %s", e)
            return {"ok": False, "error": self.error}

    def get_order_status(self, orderid):
        """Look up an order in the order book. Returns
        {found, status, avgprice, filledqty, text}."""
        try:
            ob = self.smart.orderBook()
            if ob.get("status"):
                for o in ob.get("data") or []:
                    if str(o.get("orderid")) == str(orderid):
                        return {
                            "found": True,
                            "status": o.get("orderstatus") or o.get("status") or "",
                            "avgprice": float(o.get("averageprice") or 0) or None,
                            "filledqty": int(float(o.get("filledshares") or 0)),
                            "text": o.get("text", ""),
                        }
            else:
                self.error = str(ob.get("message") or ob)
        except Exception as e:
            self.error = str(e)
            logger.warning("orderBook failed: %s", e)
        return {"found": False}

    def cancel_order(self, orderid):
        try:
            resp = self.smart.cancelOrder(str(orderid), "NORMAL")
            return {"ok": bool(resp.get("status")), "error": str(resp.get("message") or "")}
        except Exception as e:
            self.error = str(e)
            logger.warning("cancelOrder failed: %s", e)
            return {"ok": False, "error": self.error}

    def get_net_position(self):
        """Net quantity for the selected future token. Negative = short, positive = long.
        Returns {found, netqty, avgprice}. found=True with netqty=0 means flat."""
        try:
            res = self.smart.position()
            if res.get("status"):
                for p in res.get("data") or []:
                    tok = str(p.get("symboltoken") or p.get("scripttoken") or "")
                    if tok == str(self.fut_token):
                        return {"found": True, "netqty": int(float(p.get("netqty") or 0)),
                                "avgprice": float(p.get("avgnetprice") or p.get("netprice")
                                                  or p.get("openprice") or 0) or None}
                return {"found": True, "netqty": 0, "avgprice": None}  # flat for this token
            self.error = str(res.get("message") or res)
        except Exception as e:
            self.error = str(e)
            logger.warning("position() failed: %s", e)
        return {"found": False, "error": self.error}

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
