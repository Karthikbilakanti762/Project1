"""Dhan API v2.

    API_KEY       your Dhan Client ID
    API_SECRET    not needed by Dhan (leave empty or anything)
    ACCESS_TOKEN  the access token generated on web.dhan.co (Profile -> DhanHQ Trading APIs)

Docs: https://dhanhq.co/docs/v2/
"""

import csv
import io

from .. import market
from .base import Broker, BrokerError, OrderStatus
from ..data import Bar

BASE = "https://api.dhan.co/v2"
MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
TYPES = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOP_LOSS", "SL-M": "STOP_LOSS_MARKET"}


class Dhan(Broker):
    name = "dhan"
    native_intervals = (1, 5, 15)
    rate_per_sec = 4

    def _h(self):
        self.need("api_key", "access_token")
        return {"access-token": self.access_token, "client-id": self.api_key, "Accept": "application/json"}

    def _call(self, method, path, **kw):
        r = self.http(method, BASE + path, headers=self._h(), **kw)
        if isinstance(r, dict) and r.get("status") == "failure":
            raise BrokerError("dhan: %s" % (r.get("remarks") or r))
        return r

    def _ids(self, exchange):
        def build():
            text = self.http("GET", MASTER, timeout=120)
            m = {}
            for r in csv.DictReader(io.StringIO(text if isinstance(text, str) else "")):
                if (r.get("SEM_EXM_EXCH_ID") == exchange and r.get("SEM_SEGMENT") == "E"
                        and r.get("SEM_INSTRUMENT_NAME") == "EQUITY"
                        and (exchange != "NSE" or r.get("SEM_SERIES") == "EQ")):
                    m[r["SEM_TRADING_SYMBOL"]] = r["SEM_SMST_SECURITY_ID"]
            return m
        return self.cached_map(exchange, build)

    def _id(self, symbol, exchange):
        i = self._ids(exchange).get(symbol)
        if not i:
            raise BrokerError("dhan: no %s instrument %s" % (exchange, symbol))
        return str(i)

    # ---- account
    def profile(self):
        d = self._call("GET", "/profile") or {}
        return {"user": str(d.get("dhanClientId") or self.api_key), "funds": self.funds()}

    def funds(self):
        d = self._call("GET", "/fundlimit") or {}
        return float(d.get("availabelBalance") or d.get("availableBalance") or 0)

    def positions(self):
        d = self._call("GET", "/positions") or []
        return [{"symbol": p.get("tradingSymbol"), "exchange": (p.get("exchangeSegment") or "NSE_EQ").split("_")[0],
                 "qty": int(p.get("netQty") or 0)}
                for p in d if p.get("productType") == "INTRADAY" and int(p.get("netQty") or 0)]

    # ---- data
    def ltp(self, symbols, exchange):
        seg = exchange + "_EQ"
        ids = {self._id(s, exchange): s for s in symbols}
        r = self._call("POST", "/marketfeed/ltp", json_body={seg: [int(i) for i in ids]})
        out = {}
        for sid, v in (((r or {}).get("data") or {}).get(seg) or {}).items():
            if str(sid) in ids:
                out[ids[str(sid)]] = float(v.get("last_price"))
        return out

    def _candles(self, symbol, exchange, interval, d0, d1):
        r = self._call("POST", "/charts/intraday", json_body={
            "securityId": self._id(symbol, exchange), "exchangeSegment": exchange + "_EQ",
            "instrument": "EQUITY", "interval": str(interval), "oi": False,
            "fromDate": "%s 09:00:00" % d0.isoformat(), "toDate": "%s 15:30:00" % d1.isoformat()}) or {}
        ts = r.get("timestamp") or []
        o, h, l, c, v = (r.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
        bars = []
        for i, t in enumerate(ts):
            try:
                bars.append(Bar(market.ts_to_ist(t), float(o[i]), float(h[i]), float(l[i]), float(c[i]),
                                int(v[i] or 0)))
            except (IndexError, TypeError, ValueError):
                continue
        bars.sort(key=lambda b: b.start)
        return bars

    # ---- orders
    def place_order(self, symbol, exchange, side, qty, order_type, price=None, trigger=None, tag=None):
        body = {"dhanClientId": self.api_key, "transactionType": side, "exchangeSegment": exchange + "_EQ",
                "productType": "INTRADAY", "orderType": TYPES[order_type], "validity": "DAY",
                "securityId": self._id(symbol, exchange), "quantity": int(qty), "disclosedQuantity": 0,
                "price": round(float(price), 2) if order_type in ("LIMIT", "SL") else 0,
                "triggerPrice": round(float(trigger), 2) if order_type in ("SL", "SL-M") else 0,
                "afterMarketOrder": False}
        if tag:
            body["correlationId"] = tag[:25]
        body.update(self.extra)
        r = self._call("POST", "/orders", json_body=body) or {}
        if not r.get("orderId"):
            raise BrokerError("dhan: order not accepted: %s" % r)
        return str(r["orderId"])

    def cancel_order(self, order_id):
        self._call("DELETE", "/orders/%s" % order_id)

    def order_status(self, order_id):
        o = self._call("GET", "/orders/%s" % order_id) or {}
        if isinstance(o, list):
            o = o[0] if o else {}
        st = (o.get("orderStatus") or "").upper()
        state = {"TRADED": "COMPLETE", "CANCELLED": "CANCELLED", "REJECTED": "REJECTED",
                 "EXPIRED": "CANCELLED"}.get(st, "OPEN")
        return OrderStatus(state, int(o.get("filledQty") or 0), float(o.get("averageTradedPrice") or 0),
                           o.get("omsErrorDescription") or "")
