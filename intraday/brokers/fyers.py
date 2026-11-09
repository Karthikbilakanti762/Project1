"""Fyers API v3.

    API_KEY       the App ID, e.g. ABCD1234-100
    API_SECRET    the app's secret (used only by `run.py login`)
    ACCESS_TOKEN  today's access token (`run.py login` makes one)
    REDIRECT_URI  optional, only for `run.py login`: the redirect URL registered on the app

Symbols: NSE:RELIANCE-EQ; BSE symbols come from Fyers' BSE_CM symbol master.
Docs: https://myapi.fyers.in/docsv3
"""

import csv
import hashlib
import io
import urllib.parse

from .. import market
from .base import Broker, BrokerError, OrderStatus, bars_from_rows

API = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"
BSE_MASTER = "https://public.fyers.in/sym_details/BSE_CM.csv"
TYPES = {"LIMIT": 1, "MARKET": 2, "SL-M": 3, "SL": 4}


class Fyers(Broker):
    name = "fyers"
    native_intervals = (1, 3, 5, 15)
    rate_per_sec = 8

    def _h(self):
        self.need("api_key", "access_token")
        return {"Authorization": "%s:%s" % (self.api_key, self.access_token)}

    def _call(self, method, url, **kw):
        r = self.http(method, url, headers=self._h(), **kw)
        if not isinstance(r, dict) or r.get("s") != "ok":
            raise BrokerError("fyers: %s" % (r.get("message") if isinstance(r, dict) else str(r)[:200]))
        return r

    def _sym(self, symbol, exchange):
        if exchange == "NSE":
            return "NSE:%s-EQ" % symbol
        m = self.cached_map("BSE", self._bse_map)
        if symbol not in m:
            raise BrokerError("fyers: no BSE instrument %s" % symbol)
        return m[symbol]

    def _bse_map(self):
        text = self.http("GET", BSE_MASTER, timeout=60)
        m = {}
        for row in csv.reader(io.StringIO(text if isinstance(text, str) else "")):
            for cell in row:   # the column holding "BSE:NAME-GROUP"
                if cell.startswith("BSE:") and "-" in cell:
                    name = cell[4:].rsplit("-", 1)[0]
                    m.setdefault(name, cell)
                    break
        return m

    @staticmethod
    def _plain(fsym):
        return fsym.split(":", 1)[-1].rsplit("-", 1)[0]

    # ---- account
    def profile(self):
        d = self._call("GET", API + "/profile").get("data") or {}
        return {"user": "%s (%s)" % (d.get("name"), d.get("fy_id")), "funds": self.funds()}

    def funds(self):
        for f in self._call("GET", API + "/funds").get("fund_limit") or []:
            if (f.get("title") or "").lower() == "available balance":
                return float(f.get("equityAmount") or 0)
        return 0.0

    def positions(self):
        out = []
        for p in self._call("GET", API + "/positions").get("netPositions") or []:
            if p.get("productType") == "INTRADAY" and int(p.get("netQty") or 0):
                out.append({"symbol": self._plain(p["symbol"]), "exchange": p["symbol"].split(":")[0],
                            "qty": int(p["netQty"])})
        return out

    # ---- data
    def ltp(self, symbols, exchange):
        names = {self._sym(s, exchange): s for s in symbols}
        out = {}
        items = list(names)
        for i in range(0, len(items), 50):
            r = self._call("GET", DATA + "/quotes", params={"symbols": ",".join(items[i:i + 50])})
            for q in r.get("d") or []:
                if q.get("s") == "ok" and q.get("n") in names:
                    out[names[q["n"]]] = float((q.get("v") or {}).get("lp"))
        return out

    def _candles(self, symbol, exchange, interval, d0, d1):
        r = self._call("GET", DATA + "/history", params={
            "symbol": self._sym(symbol, exchange), "resolution": str(interval), "date_format": "1",
            "range_from": d0.isoformat(), "range_to": d1.isoformat(), "cont_flag": "1"})
        return bars_from_rows(r.get("candles"), market.ts_to_ist)

    # ---- orders
    def place_order(self, symbol, exchange, side, qty, order_type, price=None, trigger=None, tag=None):
        body = {"symbol": self._sym(symbol, exchange), "qty": int(qty), "type": TYPES[order_type],
                "side": 1 if side == "BUY" else -1, "productType": "INTRADAY",
                "limitPrice": round(float(price), 2) if order_type in ("LIMIT", "SL") else 0,
                "stopPrice": round(float(trigger), 2) if order_type in ("SL", "SL-M") else 0,
                "validity": "DAY", "disclosedQty": 0, "offlineOrder": False, "stopLoss": 0, "takeProfit": 0}
        if tag:
            body["orderTag"] = "".join(ch for ch in tag if ch.isalnum())[:20]
        body.update(self.extra)
        return str(self._call("POST", API + "/orders/sync", json_body=body)["id"])

    def cancel_order(self, order_id):
        self._call("DELETE", API + "/orders/sync", json_body={"id": order_id})

    def order_status(self, order_id):
        book = self._call("GET", API + "/orders", params={"id": order_id}).get("orderBook") or []
        if not book:
            return OrderStatus("OPEN", 0, 0.0, "")
        o = book[0]
        state = {1: "CANCELLED", 2: "COMPLETE", 5: "REJECTED", 7: "CANCELLED"}.get(int(o.get("status") or 0), "OPEN")
        return OrderStatus(state, int(o.get("filledQty") or 0), float(o.get("tradedPrice") or 0),
                           o.get("message") or "")

    # ---- daily login
    def login_url(self):
        self.need("api_key")
        if not self.redirect_uri:
            raise BrokerError("fyers login needs REDIRECT_URI in .env (the redirect URL of your Fyers app)")
        return ("%s/generate-authcode?client_id=%s&redirect_uri=%s&response_type=code&state=intraday"
                % (API, self.api_key, urllib.parse.quote(self.redirect_uri, safe="")))

    def create_access_token(self, code):
        self.need("api_key", "api_secret")
        if "auth_code=" in code:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["auth_code"][0]
        app_hash = hashlib.sha256(("%s:%s" % (self.api_key, self.api_secret)).encode()).hexdigest()
        r = self.http("POST", API + "/validate-authcode",
                      json_body={"grant_type": "authorization_code", "appIdHash": app_hash, "code": code})
        if not isinstance(r, dict) or not r.get("access_token"):
            raise BrokerError("fyers login failed: %s" % r)
        return r["access_token"]
