"""Paper trading: simulated orders on the real broker's market data.

Same order interface as a real broker, so the engine cannot tell the
difference. MARKET fills at the reference / last price plus slippage; a
marketable LIMIT fills at the better of its price and the last price; SL /
SL-M orders wait until simulate() sees the price cross the trigger (a gap
fills at the worse price, like a real stop). The book is saved to disk so a
restart picks up where it left off.
"""

import itertools
import json
import os

from .base import BrokerError, OrderStatus

SLIPPAGE_PCT = 0.02   # each market / stop fill is this % worse than the price seen


class PaperBroker:
    paper = True

    def __init__(self, data_broker, price_fn, book_path, capital, log=None):
        self.data = data_broker           # the real broker (for its name / profile only)
        self.name = "paper(%s)" % data_broker.name
        self.price_fn = price_fn          # sym -> last price (from the live / replay feed)
        self.book_path = book_path
        self.capital = capital
        self.log = log
        self.orders = {}
        if book_path and os.path.exists(book_path):
            with open(book_path, encoding="utf-8") as fh:
                self.orders = json.load(fh)
        self._ids = itertools.count(len(self.orders) + 1)

    def _save(self):
        if self.book_path:
            with open(self.book_path, "w", encoding="utf-8") as fh:
                json.dump(self.orders, fh, indent=1)

    @staticmethod
    def _slip(price, side):
        return round(price * (1 + SLIPPAGE_PCT / 100.0) if side == "BUY" else price * (1 - SLIPPAGE_PCT / 100.0), 2)

    def profile(self):
        p = self.data.profile()
        p["funds"] = float(self.capital)
        return p

    def funds(self):
        return float(self.capital)

    def positions(self):
        net = {}
        for o in self.orders.values():
            if o["state"] == "COMPLETE":
                k = (o["symbol"], o["exchange"])
                net[k] = net.get(k, 0) + (o["qty"] if o["side"] == "BUY" else -o["qty"])
        return [{"symbol": s, "exchange": e, "qty": q} for (s, e), q in net.items() if q]

    def place_order(self, symbol, exchange, side, qty, order_type, price=None, trigger=None, tag=None,
                    ref_price=None):
        if qty <= 0:
            raise BrokerError("paper: quantity must be positive")
        oid = "P%05d" % next(self._ids)
        o = {"id": oid, "symbol": symbol, "exchange": exchange, "side": side, "qty": int(qty), "type": order_type,
             "price": price, "trigger": trigger, "tag": tag, "state": "OPEN", "avg": 0.0, "msg": ""}
        last = ref_price if ref_price is not None else self.price_fn(symbol)
        if order_type in ("MARKET", "LIMIT"):
            if last is None:
                o.update(state="REJECTED", msg="no price for %s" % symbol)
            elif order_type == "MARKET":
                o.update(state="COMPLETE", avg=self._slip(last, side))
            elif (side == "BUY" and price >= last) or (side == "SELL" and price <= last):
                o.update(state="COMPLETE", avg=round(min(price, last) if side == "BUY" else max(price, last), 2))
        elif order_type in ("SL", "SL-M"):
            if last is not None and ((side == "SELL" and last <= trigger) or (side == "BUY" and last >= trigger)):
                o.update(state="REJECTED", msg="trigger price is on the wrong side of the last price")
        self.orders[oid] = o
        self._save()
        return oid

    def simulate(self, symbol, high, low, last):
        """Fill resting orders for `symbol` that the price range [low, high] reached."""
        if last is None:
            return
        high = last if high is None else high
        low = last if low is None else low
        changed = False
        for o in self.orders.values():
            if o["symbol"] != symbol or o["state"] != "OPEN":
                continue
            if o["type"] in ("SL", "SL-M"):
                t = o["trigger"]
                if o["side"] == "SELL" and low <= t:
                    fill = min(t, high)            # gapped below the trigger: the worse price
                    o.update(state="COMPLETE", avg=self._slip(fill, "SELL"))
                    changed = True
                elif o["side"] == "BUY" and high >= t:
                    fill = max(t, low)
                    o.update(state="COMPLETE", avg=self._slip(fill, "BUY"))
                    changed = True
            elif o["type"] == "LIMIT":
                if o["side"] == "BUY" and low <= o["price"]:
                    o.update(state="COMPLETE", avg=round(min(o["price"], high), 2))
                    changed = True
                elif o["side"] == "SELL" and high >= o["price"]:
                    o.update(state="COMPLETE", avg=round(max(o["price"], low), 2))
                    changed = True
        if changed:
            self._save()

    def cancel_order(self, order_id):
        o = self.orders.get(order_id)
        if not o:
            raise BrokerError("paper: no order %s" % order_id)
        if o["state"] == "OPEN":
            o["state"] = "CANCELLED"
            self._save()

    def order_status(self, order_id):
        o = self.orders.get(order_id)
        if not o:
            raise BrokerError("paper: no order %s" % order_id)
        filled = o["qty"] if o["state"] == "COMPLETE" else 0
        return OrderStatus(o["state"], filled, float(o["avg"] or 0), o["msg"])
