"""Market data from YOUR BROKER's API: candles, history and last prices.

Every feed has the same small interface:

    feed.today(sym, interval)          completed bars of today's session so far
    feed.many_today(syms, interval)    the same for many symbols, in parallel
    feed.history(sym, interval, days)  completed bars of past sessions (time machine, ATR)
    feed.ltp(syms)                     {sym: last price}
    feed.range_since(sym, since)       (high, low, last) seen since `since`

BrokerFeed    live: the broker's intraday candles and LTP.
ReplayFeed    a past day bar by bar from the broker's history, for `run.py replay`.
"""

import collections
import concurrent.futures
import datetime
import threading

from . import market

Bar = collections.namedtuple("Bar", "start o h l c v")


class DataError(Exception):
    pass


def session_bars(bars, day, interval, now=None):
    """Keep bars of `day` inside 09:15-15:30, on the grid, complete by `now`. Sorted, unique."""
    out = {}
    open_dt = market.at(day, market.OPEN)
    close_dt = market.at(day, market.CLOSE)
    step = datetime.timedelta(minutes=interval)
    for b in bars:
        if b.start.date() != day or b.c is None:
            continue
        if b.start < open_dt or b.start >= close_dt:
            continue
        if (b.start - open_dt).total_seconds() % (interval * 60):
            continue  # off the grid
        if now is not None and b.start + step > now:
            continue  # still forming
        out[b.start] = b
    return [out[k] for k in sorted(out)]


def resample(bars, interval):
    """1-minute bars -> `interval`-minute bars on the 09:15 grid (for brokers without that interval)."""
    if interval == 1 or not bars:
        return bars
    groups = collections.OrderedDict()
    for b in sorted(bars, key=lambda x: x.start):
        open_dt = market.at(b.start.date(), market.OPEN)
        if b.start < open_dt:
            continue
        k = int((b.start - open_dt).total_seconds() // (interval * 60))
        groups.setdefault((b.start.date(), k), []).append(b)
    out = []
    for (d, k), g in groups.items():
        start = market.at(d, market.OPEN) + datetime.timedelta(minutes=interval * k)
        out.append(Bar(start, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g)))
    return out


def _parallel(fn, items, workers):
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(fn, items))


class BrokerFeed:
    """Live market data through the broker adapter (see intraday/brokers)."""

    def __init__(self, broker, clock, exchange="NSE", workers=3, log=None):
        self.broker = broker
        self.clock = clock
        self.exchange = exchange
        self.workers = workers
        self.log = log
        self.name = broker.name

    def today(self, sym, interval):
        now = self.clock.now()
        try:
            bars = self.broker.candles(sym, self.exchange, interval, now.date(), now.date())
        except Exception as e:  # broker errors vary: surface them as DataError
            raise DataError("%s candles for %s: %s" % (self.broker.name, sym, e))
        return session_bars(bars, now.date(), interval, now)

    def many_today(self, syms, interval):
        out = {}
        lock = threading.Lock()
        errors = []

        def one(s):
            try:
                b = self.today(s, interval)
                with lock:
                    out[s] = b
            except DataError as e:
                with lock:
                    errors.append(str(e))

        _parallel(one, syms, self.workers)
        if errors and self.log:
            self.log.debug("%d symbols had no candles, e.g. %s", len(errors), errors[0])
            if not out:
                self.log.warning("No candles at all from %s: %s", self.broker.name, errors[0])
        return out

    def history(self, sym, interval, days=60):
        now = self.clock.now()
        try:
            bars = self.broker.candles(sym, self.exchange, interval,
                                       now.date() - datetime.timedelta(days=days), now.date())
        except Exception as e:
            raise DataError("%s history for %s: %s" % (self.broker.name, sym, e))
        return [b for b in bars if b.start.date() < now.date()]

    def ltp(self, syms):
        try:
            return self.broker.ltp(list(syms), self.exchange)
        except Exception as e:
            raise DataError("%s LTP: %s" % (self.broker.name, e))

    def range_since(self, sym, since):
        p = self.ltp([sym]).get(sym)
        return (p, p, p)


class ReplayFeed:
    """A past session from the broker's history, revealed bar by bar as the SimClock moves."""

    def __init__(self, clock, broker, day, interval, exchange="NSE", workers=3, log=None, days=60):
        self.clock = clock
        self.broker = broker
        self.day = day
        self.interval = interval
        self.exchange = exchange
        self.workers = workers
        self.log = log
        self.days = days
        self.full = {}
        self.name = "replay/" + broker.name

    def preload(self, syms):
        lock = threading.Lock()
        errors = []

        def one(s):
            try:
                bars = self.broker.candles(s, self.exchange, self.interval,
                                           self.day - datetime.timedelta(days=self.days), self.day)
                with lock:
                    self.full[s] = bars
            except Exception as e:
                with lock:
                    errors.append("%s: %s" % (s, e))

        _parallel(one, syms, self.workers)
        if errors and self.log:
            self.log.warning("%d symbols could not be loaded, e.g. %s", len(errors), errors[0])
        return [s for s in syms if session_bars(self.full.get(s, []), self.day, self.interval)]

    def today(self, sym, interval):
        return session_bars(self.full.get(sym, []), self.day, interval, self.clock.now())

    def many_today(self, syms, interval):
        return {s: self.today(s, interval) for s in syms if s in self.full}

    def history(self, sym, interval, days=60):
        return [b for b in self.full.get(sym, []) if b.start.date() < self.day]

    def ltp(self, syms):
        out = {}
        for s in syms:
            bars = self.today(s, self.interval)
            if bars:
                out[s] = bars[-1].c
        return out

    def range_since(self, sym, since):
        now = self.clock.now()
        step = datetime.timedelta(minutes=self.interval)
        bars = [b for b in self.today(sym, self.interval) if since < b.start + step <= now]
        last = self.ltp([sym]).get(sym)
        if not bars:
            return (last, last, last)
        return (max(b.h for b in bars), min(b.l for b in bars), bars[-1].c)
