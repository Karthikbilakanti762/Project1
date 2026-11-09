"""Upstox API v2 / v3.

    API_KEY       the app's API key (client_id)
    API_SECRET    the app's API secret (used only by `run.py login`)
    ACCESS_TOKEN  today's access token (expires ~3:30 AM next day; `run.py login` makes one)
    REDIRECT_URI  optional, only for `run.py login`: the redirect URL registered on the app

Docs: https://upstox.com/developer/api-documentation/
"""

import datetime
import urllib.parse

from .. import market
from .base import Broker, BrokerError, OrderStatus, bars_from_rows

V2 = "https://api.upstox.com/v2"
V3 = "https://api.upstox.com/v3"
HFT = "https://api-hft.upstox.com/v2"
MASTER = "https://assets.upstox.com/market-quote/instruments/exchange/%s.json.gz"
ORDER_TYPES = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "SL", "SL-M": "SL-M"}


class Upstox(Broker):
    name = "upstox"
    native_intervals = (1, 3, 5, 15)
    rate_per_sec = 10

    def _h(self):
        self.need("access_token")
        return {"Authorization": "Bearer " + self.access_token, "Accept": "application/json"}

    def _call(self, method, url, **kw):
        r = self.http(method, url, headers=self._h(), **kw)
        if not isinstance(r, dict) or r.get("status") != "success":
            raise BrokerError("upstox: %s" % (r.get("errors") if isinstance(r, dict) else str(r)[:200]))
        return r.get("data")

    # ---- instruments: SYMBOL -> "NSE_EQ|INE002A01018"
    def _keys(self, exchange):
        seg = exchange + "_EQ"

        def build():
            rows = self.http("GET", MASTER % exchange, timeout=60)
            m = {}
            for r in rows if isinstance(rows, list) else []:
                if r.get("segment") != seg:
                    continue
                sym = r.get("trading_symbol")
                if sym and (sym not in m or r.get("instrument_type") == "EQ"):
                    m[sym] = r["instrument_key"]
            return m
        return self.cached_map(exchange, build)

    def _key(self, symbol, exchange):
        k = self._keys(exchange).get(symbol)
        if not k:
            raise BrokerError("upstox: no %s instrument %s" % (exchange, symbol))
        return k

    # ---- account
    def profile(self):
        d = self._call("GET", V2 + "/user/profile")
        return {"user": "%s (%s)" % (d.get("user_name"), d.get("user_id")), "funds": self.funds()}

    def funds(self):
        d = self._call("GET", V2 + "/user/get-funds-and-margin", params={"segment": "SEC"})
        return float(((d or {}).get("equity") or {}).get("available_margin") or 0)

    def positions(self):
        d = self._call("GET", V2 + "/portfolio/short-term-positions") or []
        return [{"symbol": p.get("trading_symbol") or p.get("tradingsymbol"), "exchange": p.get("exchange"),
                 "qty": int(p.get("quantity") or 0)}
                for p in d if p.get("product") == "I" and int(p.get("quantity") or 0)]

    # ---- data
    def ltp(self, symbols, exchange):
        keys = {self._key(s, exchange): s for s in symbols}
        out = {}
        items = list(keys)
        for i in range(0, len(items), 500):
            d = self._call("GET", V2 + "/market-quote/ltp", params={"instrument_key": ",".join(items[i:i + 500])})
            for k, v in (d or {}).items():
                key = v.get("instrument_token") or ""
                sym = keys.get(key) or k.split(":", 1)[-1]
                out[sym] = float(v["last_price"])
        return out

    def _candles(self, symbol, exchange, interval, d0, d1):
        key = urllib.parse.quote(self._key(symbol, exchange), safe="")
        today = market.ist_now().date()
        rows = []
        if d1 >= today:
            d = self._call("GET", V3 + "/historical-candle/intraday/%s/minutes/%d" % (key, interval))
            rows += (d or {}).get("candles") or []
        end = min(d1, today - datetime.timedelta(days=1))
        while end >= d0:   # history: at most ~1 month per request for minute candles
            start = max(d0, end - datetime.timedelta(days=27))
            d = self._call("GET", V3 + "/historical-candle/%s/minutes/%d/%s/%s"
                           % (key, interval, end.isoformat(), start.isoformat()))
            rows += (d or {}).get("candles") or []
            end = start - datetime.timedelta(days=1)
        return bars_from_rows(rows, market.parse_iso_ist)

    # ---- orders
    def place_order(self, symbol, exchange, side, qty, order_type, price=None, trigger=None, tag=None):
        body = {"quantity": int(qty), "product": "I", "validity": "DAY",
                "price": round(float(price), 2) if order_type in ("LIMIT", "SL") else 0,
                "instrument_token": self._key(symbol, exchange), "order_type": ORDER_TYPES[order_type],
                "transaction_type": side, "disclosed_quantity": 0,
                "trigger_price": round(float(trigger), 2) if order_type in ("SL", "SL-M") else 0,
                "is_amo": False}
        if tag:
            body["tag"] = tag[:20]
        body.update(self.extra)
        d = self._call("POST", HFT + "/order/place", json_body=body)
        return str(d["order_id"])

    def cancel_order(self, order_id):
        self._call("DELETE", HFT + "/order/cancel", params={"order_id": order_id})

    def order_status(self, order_id):
        o = self._call("GET", V2 + "/order/details", params={"order_id": order_id}) or {}
        st = (o.get("status") or "").lower()
        state = {"complete": "COMPLETE", "cancelled": "CANCELLED", "rejected": "REJECTED"}.get(st, "OPEN")
        return OrderStatus(state, int(o.get("filled_quantity") or 0), float(o.get("average_price") or 0),
                           o.get("status_message") or "")

    # ---- login
    def login_url(self):
        self.need("api_key")
        if not self.redirect_uri:
            raise BrokerError("upstox login needs REDIRECT_URI in .env (the redirect URL of your Upstox app)")
        return ("https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id=%s"
                "&redirect_uri=%s" % (self.api_key, urllib.parse.quote(self.redirect_uri, safe="")))

    def create_access_token(self, code):
        self.need("api_key", "api_secret")
        if "code=" in code:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["code"][0]
        r = self.http("POST", V2 + "/login/authorization/token", headers={"Accept": "application/json"},
                      data={"code": code, "client_id": self.api_key, "client_secret": self.api_secret,
                            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"})
        if not isinstance(r, dict) or not r.get("access_token"):
            raise BrokerError("upstox login failed: %s" % r)
        return r["access_token"]
