"""Angel One SmartAPI.

    API_KEY       the SmartAPI app's API key
    API_SECRET    your Angel One client code (used only by `run.py login`)
    ACCESS_TOKEN  the session JWT (`run.py login` asks for your PIN + TOTP and makes one)

Docs: https://smartapi.angelone.in/docs
"""

from .. import market
from .base import Broker, BrokerError, OrderStatus, bars_from_rows

BASE = "https://apiconnect.angelone.in"
MASTER = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
INTERVALS = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 15: "FIFTEEN_MINUTE"}
TYPES = {"MARKET": ("NORMAL", "MARKET"), "LIMIT": ("NORMAL", "LIMIT"),
         "SL": ("STOPLOSS", "STOPLOSS_LIMIT"), "SL-M": ("STOPLOSS", "STOPLOSS_MARKET")}


class AngelOne(Broker):
    name = "angelone"
    native_intervals = (1, 3, 5, 15)
    rate_per_sec = 3

    def __init__(self, *a, **kw):
        Broker.__init__(self, *a, **kw)
        self._variety = {}

    def _h(self, auth=True):
        self.need("api_key")
        h = {"Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
             "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.api_key}
        if auth:
            self.need("access_token")
            tok = self.access_token
            h["Authorization"] = tok if tok.lower().startswith("bearer ") else "Bearer " + tok
        return h

    def _call(self, method, path, **kw):
        r = self.http(method, BASE + path, headers=self._h(), **kw)
        if not isinstance(r, dict) or not r.get("status"):
            raise BrokerError("angelone: %s" % ((r.get("message") or r.get("errorcode")) if isinstance(r, dict)
                                                else str(r)[:200]))
        return r.get("data")

    # ---- instruments: SYMBOL -> [token, tradingsymbol]
    def _tokens(self, exchange):
        def build():
            rows = self.http("GET", MASTER, timeout=180)
            m = {}
            for r in rows if isinstance(rows, list) else []:
                if r.get("exch_seg") != exchange:
                    continue
                ts = r.get("symbol") or ""
                if exchange == "NSE":
                    if ts.endswith("-EQ"):
                        m[ts[:-3]] = [r["token"], ts]
                elif not r.get("instrumenttype"):
                    m.setdefault(ts, [r["token"], ts])
            return m
        return self.cached_map(exchange, build)

    def _tok(self, symbol, exchange):
        t = self._tokens(exchange).get(symbol)
        if not t:
            raise BrokerError("angelone: no %s instrument %s" % (exchange, symbol))
        return t

    # ---- account
    def profile(self):
        d = self._call("GET", "/rest/secure/angelbroking/user/v1/getProfile") or {}
        return {"user": "%s (%s)" % (d.get("name"), d.get("clientcode")), "funds": self.funds()}

    def funds(self):
        d = self._call("GET", "/rest/secure/angelbroking/user/v1/getRMS") or {}
        return float(d.get("availablecash") or d.get("net") or 0)

    def positions(self):
        d = self._call("GET", "/rest/secure/angelbroking/order/v1/getPosition") or []
        out = []
        for p in d:
            if p.get("producttype") == "INTRADAY" and int(p.get("netqty") or 0):
                sym = p.get("tradingsymbol") or ""
                out.append({"symbol": sym[:-3] if sym.endswith("-EQ") else sym, "exchange": p.get("exchange"),
                            "qty": int(p["netqty"])})
        return out

    # ---- data
    def ltp(self, symbols, exchange):
        toks = {self._tok(s, exchange)[0]: s for s in symbols}
        items = list(toks)
        out = {}
        for i in range(0, len(items), 50):
            d = self._call("POST", "/rest/secure/angelbroking/market/v1/quote/",
                           json_body={"mode": "LTP", "exchangeTokens": {exchange: items[i:i + 50]}}) or {}
            for q in d.get("fetched") or []:
                s = toks.get(str(q.get("symbolToken")))
                if s:
                    out[s] = float(q.get("ltp"))
        return out

    def _candles(self, symbol, exchange, interval, d0, d1):
        d = self._call("POST", "/rest/secure/angelbroking/historical/v1/getCandleData", json_body={
            "exchange": exchange, "symboltoken": self._tok(symbol, exchange)[0], "interval": INTERVALS[interval],
            "fromdate": "%s 09:00" % d0.isoformat(), "todate": "%s 15:30" % d1.isoformat()})
        return bars_from_rows(d, market.parse_iso_ist)

    # ---- orders
    def place_order(self, symbol, exchange, side, qty, order_type, price=None, trigger=None, tag=None):
        token, tsym = self._tok(symbol, exchange)
        variety, otype = TYPES[order_type]
        body = {"variety": variety, "tradingsymbol": tsym, "symboltoken": token, "transactiontype": side,
                "exchange": exchange, "ordertype": otype, "producttype": "INTRADAY", "duration": "DAY",
                "price": "%.2f" % (price if order_type in ("LIMIT", "SL") else 0),
                "triggerprice": "%.2f" % (trigger if order_type in ("SL", "SL-M") else 0),
                "quantity": str(int(qty))}
        if tag:
            body["ordertag"] = tag[:20]
        body.update(self.extra)
        d = self._call("POST", "/rest/secure/angelbroking/order/v1/placeOrder", json_body=body) or {}
        oid = str(d.get("orderid") or "")
        if not oid:
            raise BrokerError("angelone: order not accepted: %s" % d)
        self._variety[oid] = variety
        return oid

    def cancel_order(self, order_id):
        self._call("POST", "/rest/secure/angelbroking/order/v1/cancelOrder",
                   json_body={"variety": self._variety.get(order_id, "STOPLOSS"), "orderid": order_id})

    def order_status(self, order_id):
        for o in self._call("GET", "/rest/secure/angelbroking/order/v1/getOrderBook") or []:
            if str(o.get("orderid")) == str(order_id):
                self._variety[str(order_id)] = o.get("variety") or self._variety.get(str(order_id))
                st = (o.get("status") or "").lower()
                state = {"complete": "COMPLETE", "cancelled": "CANCELLED", "rejected": "REJECTED"}.get(st, "OPEN")
                return OrderStatus(state, int(float(o.get("filledshares") or 0)),
                                   float(o.get("averageprice") or 0), o.get("text") or "")
        return OrderStatus("OPEN", 0, 0.0, "not in the order book yet")

    # ---- daily login (client code + PIN + TOTP)
    def login_url(self):
        return None

    def create_access_token(self, code):
        """`code` = "PIN TOTP" (the 4-digit PIN and the 6-digit authenticator code)."""
        self.need("api_key", "api_secret")
        parts = code.split()
        if len(parts) != 2:
            raise BrokerError("angelone login wants your PIN and TOTP separated by a space")
        r = self.http("POST", BASE + "/rest/auth/angelbroking/user/v1/loginByPassword",
                      headers=self._h(auth=False),
                      json_body={"clientcode": self.api_secret, "password": parts[0], "totp": parts[1]})
        if not isinstance(r, dict) or not r.get("status"):
            raise BrokerError("angelone login failed: %s" % (r.get("message") if isinstance(r, dict) else r))
        return r["data"]["jwtToken"]
