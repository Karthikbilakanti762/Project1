"""The broker interface every adapter implements, plus shared helpers.

An adapter is built from three credentials: api_key, api_secret, access_token.
Everything the app needs from a broker:

    profile()                                   -> {"user": str, "funds": float}
    funds()                                     -> available cash (INR)
    ltp(symbols, exchange)                      -> {SYMBOL: last price}
    candles(symbol, exchange, interval, d0, d1) -> [Bar] (IST), interval in minutes
    place_order(symbol, exchange, side, qty, order_type, price=None, trigger=None, tag=None) -> order id
    cancel_order(order_id)
    order_status(order_id)                      -> OrderStatus
    positions()                                 -> [{"symbol", "exchange", "qty"}] open intraday (MIS) positions
    login_url() / create_access_token(code)     -> daily login helpers, where the broker has them

side: BUY / SELL.  order_type: MARKET / LIMIT / SL / SL-M.  product: intraday (MIS) always.
"""

import collections
import json
import os

from .. import market, net, settings
from ..data import Bar, resample

OrderStatus = collections.namedtuple("OrderStatus", "state filled_qty avg_price message")
# state: OPEN (incl. trigger pending), COMPLETE, CANCELLED, REJECTED


class BrokerError(Exception):
    pass


def error_text(e):
    """A readable message out of an HttpError body (JSON or text)."""
    body = getattr(e, "body", "") or ""
    try:
        doc = json.loads(body)
        if isinstance(doc, dict):
            for k in ("message", "errorMessage", "errorcode", "error", "errors", "remarks"):
                if doc.get(k):
                    v = doc[k]
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        v = v[0].get("message") or v[0]
                    return "%s (HTTP %s)" % (v, getattr(e, "status", "?"))
    except ValueError:
        pass
    return str(e)


class Broker:
    name = "base"
    native_intervals = (1, 3, 5, 15)
    rate_per_sec = 3

    def __init__(self, api_key, api_secret, access_token, cfg=None, log=None, redirect_uri=""):
        self.redirect_uri = redirect_uri or ""
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.access_token = (access_token or "").strip()
        self.cfg = cfg or {}
        self.log = log
        self.limiter = net.RateLimiter(self.rate_per_sec)
        self.extra = dict((self.cfg.get("broker_params") or {}))

    # ---- helpers
    def need(self, *fields):
        missing = [f for f in fields if not getattr(self, f)]
        if missing:
            raise BrokerError("%s needs %s in .env" % (self.name, ", ".join(f.upper() for f in missing)))

    def http(self, method, url, **kw):
        self.limiter.wait()
        try:
            return net.request(method, url, **kw)
        except net.HttpError as e:
            raise BrokerError("%s: %s" % (self.name, error_text(e)))

    def candles(self, symbol, exchange, interval, d0, d1):
        if interval in self.native_intervals:
            return self._candles(symbol, exchange, interval, d0, d1)
        return resample(self._candles(symbol, exchange, 1, d0, d1), interval)

    def _candles(self, symbol, exchange, interval, d0, d1):
        raise NotImplementedError

    def login_url(self):
        return None

    def create_access_token(self, code):
        raise BrokerError("%s: generate the access token on the broker's website and paste it into "
                          ".env as ACCESS_TOKEN" % self.name)

    # ---- instrument master cache (symbol -> broker token), refreshed daily
    def cached_map(self, key, builder):
        today = market.ist_now().date().isoformat()
        p = settings.data_path("cache", "%s-%s-%s.json" % (self.name, key, today))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        if self.log:
            self.log.info("Downloading %s instrument list (%s), once a day...", self.name, key)
        m = builder()
        if not m:
            raise BrokerError("%s: empty instrument list for %s" % (self.name, key))
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        return m


def fmt_dt(d, end=False):
    return "%s %s" % (d.isoformat(), "15:30:00" if end else "09:00:00")


def bars_from_rows(rows, ts_parser):
    """[[ts, o, h, l, c, v, ...]] -> [Bar] sorted oldest first."""
    out = []
    for r in rows or []:
        try:
            out.append(Bar(ts_parser(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                           int(float(r[5] or 0)) if len(r) > 5 else 0))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda b: b.start)
    return out
