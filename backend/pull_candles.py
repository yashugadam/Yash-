"""One-time, standalone Angel history pull (REST only, NO websocket feed) so we can
re-run the corrected backtest locally without touching the live engine's broker session.
Caches candles to /app/memory so we never need to reconnect again."""
import os, json, time
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import pyotp
from SmartApi import SmartConnect

load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.environ["ANGEL_API_KEY"]
CLIENT = os.environ["ANGEL_CLIENT_CODE"]
PIN = os.environ["ANGEL_PIN"]
TOTP = os.environ["ANGEL_TOTP_SECRET"]

TOKEN = os.environ.get("PULL_TOKEN", "68407")   # production active NIFTY future
EXCH = os.environ.get("PULL_EXCH", "NFO")
DAYS = int(os.environ.get("PULL_DAYS", "70"))
OUT = Path("/app/memory/candles_cache.json")

smart = SmartConnect(api_key=API_KEY)
otp = pyotp.TOTP(TOTP).now()
sess = smart.generateSession(CLIENT, PIN, otp)
if not sess.get("status"):
    raise SystemExit(f"LOGIN FAILED: {sess.get('message') or sess}")
print("Login OK (REST only, no feed started).")

try:
    now = datetime.now()
    start = now - timedelta(days=DAYS)
    all_c, seen, cur, calls = [], set(), start, 0
    while cur < now and calls < 12:
        chunk_end = min(cur + timedelta(days=20), now)
        params = {"exchange": EXCH, "symboltoken": str(TOKEN), "interval": "ONE_MINUTE",
                  "fromdate": cur.strftime("%Y-%m-%d %H:%M"),
                  "todate": chunk_end.strftime("%Y-%m-%d %H:%M")}
        res = smart.getCandleData(params)
        calls += 1
        data = res.get("data") or [] if isinstance(res, dict) else []
        for c in data:
            if c[0] not in seen:
                seen.add(c[0]); all_c.append(c)
        print(f"chunk {calls}: {cur.date()}->{chunk_end.date()} got {len(data)} (total {len(all_c)})")
        cur = chunk_end + timedelta(minutes=1)
        time.sleep(0.4)
    all_c.sort(key=lambda c: c[0])
    OUT.write_text(json.dumps({"token": TOKEN, "exch": EXCH, "pulled_at": now.isoformat(),
                               "candles": all_c}))
    print(f"SAVED {len(all_c)} candles -> {OUT}")
    if all_c:
        print("first:", all_c[0][0], "last:", all_c[-1][0])
finally:
    try:
        smart.terminateSession(CLIENT)
        print("Session terminated cleanly.")
    except Exception as e:
        print("terminate note:", e)
