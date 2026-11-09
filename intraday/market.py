"""IST clock and the NSE / BSE equity calendar.

India has no daylight saving: IST = UTC + 5:30, always.
Normal session 09:15 - 15:30 IST (pre-open 09:00 - 09:08).
All times inside the app are naive datetimes in IST.
"""

import datetime
import time

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)

# NSE equity / equity-derivatives trading holidays (BSE follows the same list).
# Weekday holidays only. Add or remove days with "extra_holidays" /
# "extra_trading_days" in config.json when the exchange publishes changes.
HOLIDAYS = {
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
KNOWN_YEARS = (2026,)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def ist_now():
    return (utc_now() + IST_OFFSET).replace(microsecond=0)


def ts_to_ist(ts):
    """Unix seconds -> naive IST. Never uses the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts)) + IST_OFFSET


def ist_to_ts(dt):
    return int((dt - IST_OFFSET - EPOCH).total_seconds())


def parse_iso_ist(text):
    """'2026-11-09T09:15:00+0530' / '+05:30' / 'Z' / naive -> naive IST."""
    t = str(text).strip().replace("Z", "+00:00")
    if len(t) >= 5 and t[-5] in "+-" and t[-4:].isdigit():
        t = t[:-2] + ":" + t[-2:]  # +0530 -> +05:30 (Python < 3.11)
    t = t.replace(" ", "T", 1) if "T" not in t and len(t) > 10 else t
    dt = datetime.datetime.fromisoformat(t)
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + IST_OFFSET


def hhmm(text):
    h, m = str(text).strip().split(":")
    return datetime.time(int(h), int(m))


def is_trading_day(d, cfg=None):
    """(bool, reason)."""
    iso = d.isoformat()
    cfg = cfg or {}
    if iso in (cfg.get("extra_trading_days") or []):
        return True, "special session"
    if iso in (cfg.get("extra_holidays") or []):
        return False, "holiday (config.json)"
    if d.weekday() >= 5:
        return False, "weekend"
    if iso in HOLIDAYS:
        return False, "NSE holiday: " + HOLIDAYS[iso]
    return True, ""


def calendar_note(d):
    if d.year not in KNOWN_YEARS:
        return ("The built-in holiday list covers %s only: add %d holidays to extra_holidays in config.json."
                % (", ".join(map(str, KNOWN_YEARS)), d.year))
    return ""


def at(d, t):
    return datetime.datetime.combine(d, t)


def next_trading_day(d, cfg=None):
    d = d + datetime.timedelta(days=1)
    for _ in range(15):
        if is_trading_day(d, cfg)[0]:
            return d
        d += datetime.timedelta(days=1)
    return d


def bar_close_after(now, interval_min, day):
    """The first bar-close time (IST) strictly after `now`, on the grid from 09:15."""
    start = at(day, OPEN)
    if now < start:
        return start + datetime.timedelta(minutes=interval_min)
    k = int((now - start).total_seconds() // (interval_min * 60)) + 1
    return start + datetime.timedelta(minutes=interval_min * k)


def label(dt):
    return dt.strftime("%H:%M:%S") if isinstance(dt, datetime.datetime) else dt.strftime("%H:%M")


class LiveClock:
    """Wall clock in IST."""
    live = True

    def now(self):
        return ist_now()

    def sleep(self, seconds):
        if seconds > 0:
            time.sleep(seconds)


class SimClock:
    """A replay clock: sleep() just moves time forward."""
    live = False

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(seconds)))
