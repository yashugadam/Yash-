"""Time + NIFTY expiry helper functions."""
import calendar
from datetime import datetime, timezone, date

from config import IST  # noqa: F401  (kept importable for callers/tests)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def last_thursday(year, month):
    # NIFTY futures expire on the last Thursday of the month.
    weeks = calendar.monthcalendar(year, month)
    thursdays = [w[calendar.THURSDAY] for w in weeks if w[calendar.THURSDAY] != 0]
    return date(year, month, thursdays[-1])


def next_expiry(d):
    exp = last_thursday(d.year, d.month)
    if d <= exp:
        return exp
    y = d.year + (1 if d.month == 12 else 0)
    m = 1 if d.month == 12 else d.month + 1
    return last_thursday(y, m)
