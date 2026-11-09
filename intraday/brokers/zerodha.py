"""Zerodha Kite Connect v3.

    API_KEY       the Kite Connect app's api_key
    API_SECRET    the app's api_secret (used only by `run.py login`)
    ACCESS_TOKEN  today's access token (expires every morning; `run.py login` makes one)

Candles need Kite's historical-data add-on on the app.
Docs: https://kite.trade/docs/connect/v3/
"""

import csv
import hashlib
import io
import urllib.parse

from .. import market
from .base import Broker, BrokerError, OrderStatus, bars_from_rows

BASE = "https://api.kite.trade"
INTERVALS = {1: "minute", 3: "3minute", 5: "5minute", 15: "15minute"}


class Zerodha(Broker):
    name = "zerodha"
    native_intervals = (1, 3, 5, 15)
    rate_per_sec = 3   # historical API limit

    def _h(self):
        self.need("api_key", "access_token")
        return {"X-Kite-Version": "3", "Authorization": "token %s:%s" % (self.api_key, self.access_token)}

    def _call(self, method, path, **kw):
        r = self.http(method, BASE + path, headers=self._h(), **kw)
        if not isinstance(r, dict) or r.get("status") != "success":
            raise BrokerError("zerodha: %s" % (r.get("message") if isinstance(r, dict) else str(r)[:200]))
        return r.get("data")

    # ---- account
    def profile(self):
        d = self._call("GET", "/user/profile") or {}
        return {"user": "%s (%s)" % (d.get("user_name"), d.get("user_id")), "funds": self.funds()}

    def funds(self):
        d = self._call("GET", "/user/margins/equity") or {}
        return float(d.get("net") or (d.get("available") or {}).get("live_balance") or 0)

    def positions(self):
        d = self._call("GET", "/portfolio/positions") or {}
        return [{"symbol": p["tradingsymbol"], "exchange": p["exchange"], "qty": int(p["quantity"])}
                for p in d.get("net", []) if p.get("product") == "MIS" and int(p.get("quantity") or 0)]

    # ---- data
    def ltp(self, symbols, exchange):
        out = {}
        for i in range(0, len(symbols), 500):
            chunk = symbols[i:i + 500]
            d = self._call("GET", "/quote/ltp", params=[("i", "%s:%s" % (exchange, s)) for s in chunk]) or {}
            for k, v in d.items():
                out[k.split(":", 1)[1]] = float(v["last_price"])
        return out

    def _tokens(self, exchange):
        def build():
            text = self.http("GET", BASE + "/instruments/" + exchange, headers=self._h(), timeout=60)
            m = {}
            for r in csv.DictReader(io.StringIO(text if isinstance(text, str) else "")):
                if r.get("segment") == exchange and r.get("instrument_type") == "EQ":
                    m[r["tradingsymbol"]] = r["instrument_token"]
            return m
        return self.cached_map(exchange, build)

    def _candles(self, symbol, exchange, interval, d0, d1):
        tok = self._tokens(exchange).get(symbol)
        if not tok:
            raise BrokerError("zerodha: no %s instrument %s" % (exchange, symbol))
        d = self._call("GET", "/instruments/historical/%s/%s" % (tok, INTERVALS[interval]),
                       params={"from": "%s 09:00:00" % d0.isoformat(), "to": "%s 15:30:00" % d1.isoformat()})
        return bars_from_rows((d or {}).get("candles"), market.parse_iso_ist)

    # ---- orders
    def place_order(self, symbol, exchange, side, qty, order_type, price=None, trigger=None, tag=None):
        body = {"tradingsymbol": symbol, "exchange": exchange, "transaction_type": side,
                "order_type": order_type, "quantity": int(qty), "product": "MIS", "validity": "DAY"}
        if order_type in ("LIMIT", "SL"):
            body["price"] = "%.2f" % price
        if order_type in ("SL", "SL-M"):
            body["trigger_price"] = "%.2f" % trigger
        if tag:
            body["tag"] = tag[:20]
        body.update(self.extra)
        return str(self._call("POST", "/orders/regular", data=body)["order_id"])

    def cancel_order(self, order_id):
        self._call("DELETE", "/orders/regular/%s" % order_id)

    def order_status(self, order_id):
        hist = self._call("GET", "/orders/%s" % order_id) or []
        if not hist:
            return OrderStatus("OPEN", 0, 0.0, "")
        o = hist[-1]
        st = (o.get("status") or "").upper()
        state = st if st in ("COMPLETE", "CANCELLED", "REJECTED") else "OPEN"
        return OrderStatus(state, int(o.get("filled_quantity") or 0), float(o.get("average_price") or 0),
                           o.get("status_message") or "")

    # ---- daily login
    def login_url(self):
        self.need("api_key")
        return "https://kite.zerodha.com/connect/login?v=3&api_key=" + self.api_key

    def create_access_token(self, code):
        """`code` = the request_token from the redirect URL (or the whole URL)."""
        self.need("api_key", "api_secret")
        if "request_token=" in code:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["request_token"][0]
        checksum = hashlib.sha256((self.api_key + code + self.api_secret).encode()).hexdigest()
        r = self.http("POST", BASE + "/session/token", headers={"X-Kite-Version": "3"},
                      data={"api_key": self.api_key, "request_token": code, "checksum": checksum})
        if not isinstance(r, dict) or r.get("status") != "success":
            raise BrokerError("zerodha login failed: %s" % r)
        return r["data"]["access_token"]
