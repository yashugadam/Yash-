"""Shared configuration: logging, timezone, and app-wide constants."""
import logging
import uuid
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("renko-bot")

IST = ZoneInfo("Asia/Kolkata")

# Hard cap on any single order quantity (units) — guards against a typo/abusive
# request submitting an oversized real-money order. ~75 lots of NIFTY (lot=65).
MAX_ORDER_QTY = 5000

# Exit-retry safety: when a square-off (EXIT) order is rejected, retry — but
# THROTTLED and CAPPED so a persistent broker rejection can't hammer the API
# once per tick. After the cap, auto-retry halts and the position is held for
# manual intervention.
MAX_EXIT_RETRIES = 8
EXIT_RETRY_MIN_GAP = 15        # seconds between auto exit-retries

# ---- multi-pod leadership: Emergent runs several backend pods. Only ONE (the leader)
# may connect to Angel One, run the strategy loop and place orders — this guarantees no
# duplicate real-money orders. All other pods are read-only and relay user actions to the
# leader via a MongoDB command queue. The leader holds a short DB lease it renews each tick;
# if it dies, another pod takes over within LEADER_LEASE_SEC.
INSTANCE_ID = str(uuid.uuid4())
LEADER_LEASE_SEC = 15

# Transient alerts are served in /api/state only for this window, then dropped —
# so an old toast can't re-fire indefinitely on mobile tab-resume / remounts.
ALERT_TTL_SEC = 20

# ---- authentication (single-user JWT) ----
# The live trading API must not be open to the public internet. A single user (from .env)
# logs in; a signed JWT then gates every /api route except the public login + keepalive.
JWT_ALGORITHM = "HS256"
JWT_TTL_HOURS = 12
AUTH_PUBLIC_PATHS = {"/api/auth/login", "/api/keepalive"}
