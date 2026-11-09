"""The whole trading day in one loop: scan -> news -> plan -> time machine ->
size -> risk -> (your YES) -> entry + protective stop -> monitor -> exit -> report.

    before 09:15   load the universe, check the broker login and funds, wait
    09:30 onward   at every bar close: scan for opening-range breakouts and
                   take the fresh ones that pass every check
    all day        every poll_seconds: stop filled? target hit? daily loss limit?
                   kill switch? (trader/STOP blocks new entries, trader/FLATTEN
                   exits everything)
    flat_by        cancel stops and square off everything, then write the report

Every entry gets a real stop order at the broker (SL-M by default) the moment
it fills, so a crash or a lost connection never leaves a position unprotected.
The target is watched by the app and taken with a market exit (no OCO needed).
State is saved after every change: restart the app and it resumes the day.
"""

import csv
import datetime
import json
import os
import time

from . import market, news, settings, strategy, timemachine, universe
from .brokers import BrokerError
from .data import DataError

TS = "%Y-%m-%d %H:%M:%S"


def _opp(side):
    return "SELL" if side == "BUY" else "BUY"


class Engine:
    def __init__(self, cfg, broker, orders, feed, clock, log, prompt=None, replay=False):
        self.cfg = cfg
        self.broker = broker          # the real broker (account, data)
        self.orders = orders          # where orders go: the same broker, or PaperBroker
        self.feed = feed
        self.clock = clock
        self.log = log
        self.prompt = prompt
        self.replay = replay
        self.paper = bool(getattr(orders, "paper", False))
        s = cfg["strategy"]
        self.scfg = s
        self.interval = s["interval"]
        self.n_range = s["or_minutes"] // s["interval"]
        self.tick = cfg["orders"]["tick_size"]
        self.ex = cfg["exchange"]
        self.day = clock.now().date()
        suffix = "-replay" if replay else ""
        self.state_path = settings.data_path("state", "%s%s.json" % (self.day.isoformat(), suffix))
        self.journal_path = settings.data_path("journal", "trades%s.csv" % suffix)
        self.report_path = settings.data_path("reports", "%s%s.json" % (self.day.isoformat(), suffix))
        if replay and os.path.exists(self.state_path):
            os.remove(self.state_path)
        self.state = self._load()
        self.cash = float(cfg["capital"])
        self.universe = []
        self._flatten_seen = False

    # ------------------------------------------------------------ state
    def _load(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            self.log.info("Resuming today's state: %d trade(s) on record.", len(st.get("trades", [])))
            return st
        return {"date": self.day.isoformat(), "broker": self.orders.name, "paper": self.paper,
                "trades": [], "seen": [], "skipped": [], "halted": None}

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1, default=str)
        os.replace(tmp, self.state_path)

    def open_trades(self):
        return [t for t in self.state["trades"] if t["status"] in ("OPEN", "EXITING")]

    def at(self, key):
        return market.at(self.day, market.hhmm(self.cfg["session"][key]))

    # ------------------------------------------------------------ the day
    def run(self):
        ok, why = market.is_trading_day(self.day, self.cfg)
        if not ok:
            nxt = market.next_trading_day(self.day, self.cfg)
            self.log.info("No session today (%s, %s). Next session: %s.", self.day.isoformat(), why,
                          nxt.strftime("%a %d %b %Y"))
            return 0
        note = market.calendar_note(self.day)
        if note:
            self.log.warning(note)
        flat = self.at("flat_by")
        last_entry = self.at("no_entries_after")
        grace = datetime.timedelta(seconds=self.cfg["scan_grace_sec"])
        open_dt = market.at(self.day, market.OPEN)
        first_scan = open_dt + datetime.timedelta(minutes=self.scfg["or_minutes"] + self.interval) + grace

        flag = os.path.join(settings.DATA_DIR, "FLATTEN")
        if os.path.exists(flag) and time.time() - os.path.getmtime(flag) > 12 * 3600:
            os.remove(flag)   # left over from an earlier day
        if not self.prepare():
            return 1
        now = self.clock.now()
        if now >= flat and not self.open_trades():
            self.log.info("It is past flat-by (%s): nothing to do today.", flat.strftime("%H:%M"))
            self.report()
            return 0
        self._wait_until(first_scan - datetime.timedelta(minutes=self.interval), "the opening range")
        self.log.info("Opening range: %s to %s. First scan at %s.", open_dt.strftime("%H:%M"),
                      (open_dt + datetime.timedelta(minutes=self.scfg["or_minutes"])).strftime("%H:%M"),
                      first_scan.strftime("%H:%M:%S"))
        next_scan = max(first_scan, market.bar_close_after(self.clock.now() - grace, self.interval, self.day) + grace)

        while True:
            now = self.clock.now()
            self._heartbeat()
            if now >= flat:
                self.log.info("Flat-by %s: squaring off.", flat.strftime("%H:%M"))
                self.square_off("flat-by")
                break
            if self.open_trades():
                self.monitor(now)
            self._kill_switches()
            if now >= next_scan:
                if self.entries_allowed(now):
                    self.scan(now)
                next_scan = market.bar_close_after(self.clock.now() - grace, self.interval, self.day) + grace
            now = self.clock.now()
            if now > last_entry and not self.open_trades():
                self.log.info("Entry window closed (%s) and no open positions: done for the day.",
                              last_entry.strftime("%H:%M"))
                break
            wait = (next_scan - now).total_seconds()
            if self.open_trades():
                wait = min(wait, self.cfg["poll_seconds"])
            wait = min(wait, (flat - now).total_seconds())
            self.clock.sleep(max(1, wait))
        self.report()
        return 0

    def _heartbeat(self):
        """`run.py flatten` reads this to know a live app is running (and lets the app do the exits)."""
        if not self.replay:
            with open(settings.data_path("state", "heartbeat"), "w") as fh:
                fh.write(self.clock.now().strftime(TS))

    def _wait_until(self, when, what):
        last_note = None
        while self.clock.now() < when:
            self._heartbeat()
            now = self.clock.now()
            left = (when - now).total_seconds()
            if last_note is None or (now - last_note).total_seconds() >= 900:
                self.log.info("Waiting for %s (%s, in %d min)...", what, when.strftime("%H:%M"), int(left // 60) + 1)
                last_note = now
            self.clock.sleep(min(60, left))

    def prepare(self):
        self.log.info("Mode: %s | broker: %s | exchange: %s | %s", "PAPER" if self.paper else "LIVE",
                      self.broker.name, self.ex, "replay " + self.day.isoformat() if self.replay else "live clock")
        if not self.replay:
            try:
                prof = self.broker.profile()
                self.log.info("Logged in: %s | broker funds Rs %s", prof.get("user"), _rs(prof.get("funds") or 0))
                if not self.paper:
                    self.cash = min(float(self.cfg["capital"]), float(prof.get("funds") or 0))
            except BrokerError as e:
                self.log.error("Broker login check failed: %s", e)
                self.log.error("If the access token expired, run:  python run.py login")
                return False
        if not self.universe:
            self.universe = universe.load(self.cfg, self.day, self.log)
        c = self.cfg
        self.log.info("Universe: %d symbols (%s). Capital Rs %s, leverage %sx, max loss/trade Rs %s, "
                      "max daily loss Rs %s, max %d trades.", len(self.universe), c["universe"], _rs(c["capital"]),
                      c["intraday_leverage"], c["max_loss_per_trade"], c["max_daily_loss"], c["max_trades_per_day"])
        self.log.info("Setup: %d-min ORB on %d-min bars, stop %s, target %s, shorts %s. Entries %s-%s, flat by %s.",
                      self.scfg["or_minutes"], self.interval, self.scfg["stop"], self.scfg["target"],
                      "on" if self.scfg.get("allow_short") else "off", c["session"]["no_entries_before"],
                      c["session"]["no_entries_after"], c["session"]["flat_by"])
        return True

    def entries_allowed(self, now):
        if self.state.get("halted"):
            return False
        if now > self.at("no_entries_after") or now < self.at("no_entries_before"):
            return False
        trades = [t for t in self.state["trades"] if t["status"] != "FAILED"]
        if len(trades) >= self.cfg["max_trades_per_day"]:
            return False
        return len(self.open_trades()) < self.cfg["max_open_positions"]

    # ------------------------------------------------------------ scan + entry
    def scan(self, now):
        seen = set(self.state["seen"])
        cands = [s for s in self.universe if s not in seen]
        if not cands:
            return
        t0 = time.time()
        data = self.feed.many_today(cands, self.interval)
        fresh = []
        for sym, bars in data.items():
            if len(bars) <= self.n_range:
                continue
            trig = strategy.find_breakout(bars, self.n_range, self.scfg, self.interval)
            if not trig:
                continue
            age = len(bars) - 1 - trig["bar_index"]
            if age > 1:
                self.state["seen"].append(sym)   # broke out earlier (stale): not chased
                continue
            fresh.append((sum(b.c * b.v for b in bars), sym, trig, bars))
        self.log.info("Scan %s: %d of %d charts read in %.0fs, %d fresh breakout(s)%s.",
                      now.strftime("%H:%M"), len(data), len(cands), time.time() - t0, len(fresh),
                      (": " + ", ".join("%s %s" % (s, "UP" if t["side"] == "BUY" else "DOWN")
                                        for _, s, t, _ in sorted(fresh, reverse=True, key=lambda x: x[0])))
                      if fresh else "")
        if not data:
            self.log.warning("No candles came back: check the access token and the broker's data permissions.")
        fresh.sort(key=lambda x: -x[0])  # most traded first
        for _, sym, trig, bars in fresh:
            self.state["seen"].append(sym)
            if not self.entries_allowed(self.clock.now()):
                break
            try:
                self.try_trade(sym, trig, bars)
            except (BrokerError, DataError) as e:
                self.log.error("%s: %s", sym, e)
        self.save()

    def _skip(self, sym, why):
        self.log.info("  SKIP %s: %s", sym, why)
        self.state["skipped"].append({"symbol": sym, "at": self.clock.now().strftime(TS), "why": why})

    def free_cash(self):
        used = sum(t["qty"] * t["fill"] / float(self.cfg["intraday_leverage"]) for t in self.open_trades())
        return max(0.0, self.cash - used)

    def try_trade(self, sym, trig, bars):
        now = self.clock.now()
        word = "above" if trig["side"] == "BUY" else "below"
        self.log.info("%s: %s-bar closed %s the range (%.2f-%.2f) at %.2f%s.", sym, trig["bar_start"].strftime("%H:%M"),
                      word, trig["or_low"], trig["or_high"], trig["price"],
                      (", volume %.1fx" % trig["volume_x"]) if trig.get("volume_x") else "")
        ltp = self.feed.ltp([sym]).get(sym)
        if ltp is None:
            return self._skip(sym, "no last price")
        chase = self.scfg["max_chase_pct"] / 100.0
        if trig["side"] == "BUY":
            if ltp > trig["price"] * (1 + chase):
                return self._skip(sym, "already ran to %.2f (> %.1f%% past the breakout)" % (ltp, chase * 100))
            if ltp <= trig["or_high"]:
                return self._skip(sym, "back inside the range (%.2f)" % ltp)
        else:
            if ltp < trig["price"] * (1 - chase):
                return self._skip(sym, "already fell to %.2f (> %.1f%% past the breakdown)" % (ltp, chase * 100))
            if ltp >= trig["or_low"]:
                return self._skip(sym, "back inside the range (%.2f)" % ltp)
        try:
            p = strategy.plan(sym, trig, ltp, bars, self.scfg, self.tick)
        except strategy.PlanError as e:
            return self._skip(sym, str(e))
        self.log.info("  plan: %s at ~%.2f, stop %.2f (%s), target %.2f (%s), risk %.2f/share.", p["side"],
                      p["entry"], p["stop"], p["stop_rule"], p["target"], p["target_rule"], p["risk_ps"])

        if self.cfg["news"].get("enabled") and not self.replay:
            block, lines = news.check(sym, self.cfg["news"], now)
            for ln in lines:
                self.log.info("  %s", ln)
            if block:
                return self._skip(sym, block)

        tmc = self.cfg["timemachine"]
        if tmc.get("enabled"):
            try:
                hist = self.feed.history(sym, self.interval)
                end_slot = int((self.at("flat_by") - market.at(self.day, market.OPEN)).total_seconds()
                               // (self.interval * 60))
                tm = timemachine.run(p, hist, self.day, self.interval, trig["bar_index"] + 1, end_slot,
                                     self.n_range, tmc.get("replays", 3000), tmc.get("seed", 19))
                self.log.info("  %s", timemachine.summary(tm))
                if tm.get("ok") and tm.get("leans_stop") and tmc.get("block_if_leans_stop"):
                    return self._skip(sym, "time machine leans toward the stop")
            except DataError as e:
                self.log.info("  time machine skipped: %s", e)

        cash = self.free_cash()
        qty, binding = strategy.size(self.cfg, p, cash)
        blocks = strategy.check(self.cfg, p, qty, self.state, now, cash)
        if blocks:
            return self._skip(sym, "BLOCK - " + "; ".join(blocks))
        loss = strategy.loss_at_stop(self.cfg, p, qty)
        gain = strategy.gain_at_target(self.cfg, p, qty)
        self.log.info("  PASS: %s %d %s (~Rs %s, sized by %s). Loss at stop Rs %.0f, gain at target Rs %.0f "
                      "(after charges).", p["side"], qty, sym, _rs(qty * p["entry"]), binding, loss, gain)

        if not self.paper and self.cfg["orders"].get("confirm_live_orders") and self.prompt:
            ans = self.prompt.ask("\n  >>> LIVE ORDER on %s: %s %d %s (%s, MIS) ~%.2f, stop %.2f, target %.2f, "
                                  "max loss Rs %.0f.\n  >>> Type YES within %ds to place it: "
                                  % (self.broker.name, p["side"], qty, sym, self.ex, p["entry"], p["stop"], p["target"],
                                     loss, self.cfg["orders"]["confirm_timeout_sec"]),
                                  self.cfg["orders"]["confirm_timeout_sec"])
            if (ans or "").strip().upper() != "YES":
                return self._skip(sym, "not confirmed (%s)" % ("no answer" if ans is None else repr(ans)))
        self.enter(p, qty)

    def _place(self, sym, side, qty, otype, price=None, trigger=None, ref=None, tag="dts"):
        kw = {"price": price, "trigger": trigger, "tag": tag}
        if self.paper:
            kw["ref_price"] = ref
        return self.orders.place_order(sym, self.ex, side, int(qty), otype, **kw)

    def _wait_fill(self, oid, timeout):
        t0 = time.time()
        while True:
            st = self.orders.order_status(oid)
            if st.state != "OPEN" or not self.clock.live or time.time() - t0 >= timeout:
                return st
            time.sleep(1)

    def enter(self, p, qty):
        sym, side = p["symbol"], p["side"]
        oc = self.cfg["orders"]
        trade = {"symbol": sym, "exchange": self.ex, "side": side, "qty": int(qty), "planned_entry": p["entry"],
                 "stop": p["stop"], "target": p["target"], "fill": None, "status": "PENDING",
                 "opened_at": self.clock.now().strftime(TS), "entry_order": None, "stop_order": None,
                 "exit_order": None, "last_check": self.clock.now().strftime(TS), "plan": p}
        self.state["trades"].append(trade)
        price = None
        if oc["entry_type"] == "LIMIT":
            buf = oc["limit_buffer_pct"] / 100.0
            price = strategy.round_tick(p["entry"] * (1 + buf) if side == "BUY" else p["entry"] * (1 - buf),
                                        self.tick, "up" if side == "BUY" else "down")
        try:
            oid = self._place(sym, side, qty, oc["entry_type"], price=price, ref=p["entry"], tag="dtsentry")
        except BrokerError as e:
            trade["status"] = "FAILED"
            trade["error"] = str(e)
            self.log.error("  ENTRY REJECTED %s: %s", sym, e)
            self.save()
            return
        trade["entry_order"] = oid
        st = self._wait_fill(oid, oc["fill_timeout_sec"])
        if st.state == "OPEN":
            try:
                self.orders.cancel_order(oid)
            except BrokerError as e:
                self.log.warning("  cancel of unfilled entry %s failed: %s", oid, e)
            st = self.orders.order_status(oid)
        if st.state != "COMPLETE" and st.filled_qty <= 0:
            trade["status"] = "FAILED"
            trade["error"] = "entry %s: %s" % (st.state.lower(), st.message)
            self.log.warning("  NOT FILLED %s: %s %s", sym, st.state, st.message)
            self.save()
            return
        if st.filled_qty and st.filled_qty != qty:
            self.log.warning("  partial fill: %d of %d", st.filled_qty, qty)
            trade["qty"] = int(st.filled_qty)
        trade["fill"] = round(st.avg_price or p["entry"], 2)
        trade["status"] = "OPEN"
        self.log.info("  ENTERED %s %d %s at %.2f (order %s).", side, trade["qty"], sym, trade["fill"], oid)
        self._protect(trade)
        b, s_ = (trade["fill"], trade["stop"]) if side == "BUY" else (trade["stop"], trade["fill"])
        trade["risk_amount"] = round(trade["qty"] * abs(trade["fill"] - trade["stop"]) +
                                     strategy.charges(self.cfg, self.ex, trade["qty"], b, s_), 2)
        self.save()

    def _protect(self, trade):
        """Place the stop at the broker right after the fill; if that fails, get out now."""
        oc = self.cfg["orders"]
        sym, xside, stop = trade["symbol"], _opp(trade["side"]), trade["stop"]
        try:
            if oc["stop_type"] == "SL-M":
                sid = self._place(sym, xside, trade["qty"], "SL-M", trigger=stop, tag="dtsstop")
            else:
                buf = oc["sl_limit_buffer_pct"] / 100.0
                lim = strategy.round_tick(stop * (1 - buf) if xside == "SELL" else stop * (1 + buf), self.tick,
                                          "down" if xside == "SELL" else "up")
                sid = self._place(sym, xside, trade["qty"], "SL", price=lim, trigger=stop, tag="dtsstop")
            st = self.orders.order_status(sid)
            if st.state == "REJECTED":
                raise BrokerError("stop order rejected: %s" % st.message)
            trade["stop_order"] = sid
            self.log.info("  STOP placed: %s %d at %.2f (%s, order %s). Target %.2f watched by the app.",
                          xside, trade["qty"], stop, oc["stop_type"], sid, trade["target"])
        except BrokerError as e:
            self.log.error("  STOP ORDER FAILED for %s (%s): exiting now for safety.", sym, e)
            self.exit_trade(trade, "stop order failed")

    # ------------------------------------------------------------ monitor + exit
    def monitor(self, now):
        for tr in self.open_trades():
            sym = tr["symbol"]
            try:
                since = datetime.datetime.strptime(tr["last_check"], TS)
                hi, lo, last = self.feed.range_since(sym, since)
            except DataError as e:
                self.log.debug("monitor %s: %s", sym, e)
                continue
            tr["last_check"] = now.strftime(TS)
            if last is not None:
                tr["last"] = last
            if tr["status"] == "EXITING":
                self.exit_trade(tr, tr.get("exit_reason") or "exit retry")
                continue
            if self.paper:
                self.orders.simulate(sym, hi, lo, last)
            if tr.get("stop_order"):
                try:
                    st = self.orders.order_status(tr["stop_order"])
                except BrokerError as e:
                    self.log.warning("stop status %s: %s", sym, e)
                    continue
                if st.state == "COMPLETE":
                    self.close(tr, st.avg_price or tr["stop"], "stop")
                    continue
                if st.state in ("CANCELLED", "REJECTED"):
                    self.log.warning("%s: the stop order is %s at the broker; exiting.", sym, st.state.lower())
                    tr["stop_order"] = None
                    self.exit_trade(tr, "stop order %s" % st.state.lower())
                    continue
            if hi is not None and lo is not None:
                hit = hi >= tr["target"] if tr["side"] == "BUY" else lo <= tr["target"]
                if hit:
                    self.exit_trade(tr, "target", ref=tr["target"])
        self._loss_guard()
        self.save()

    def pnl(self):
        realised = sum(t.get("pnl") or 0 for t in self.state["trades"] if t["status"] == "CLOSED")
        unreal = 0.0
        for t in self.open_trades():
            if t.get("last") is not None and t.get("fill"):
                d = t["last"] - t["fill"] if t["side"] == "BUY" else t["fill"] - t["last"]
                unreal += d * t["qty"]
        return realised, unreal

    def _loss_guard(self):
        realised, unreal = self.pnl()
        if realised + unreal <= -self.cfg["max_daily_loss"] and not self.state.get("halted"):
            self.state["halted"] = "daily loss Rs %.0f hit max_daily_loss" % -(realised + unreal)
            self.log.error("DAILY LOSS LIMIT: Rs %.0f. Exiting everything, no more entries today.",
                           -(realised + unreal))
            self.square_off("daily loss limit")

    def _kill_switches(self):
        flag = os.path.join(settings.DATA_DIR, "FLATTEN")
        if os.path.exists(flag) and not self._flatten_seen:
            self._flatten_seen = True
            self.state["halted"] = "trader/FLATTEN"
            self.log.error("trader/FLATTEN found: exiting everything, no more entries today.")
            self.square_off("manual flatten")
            self.save()
            try:
                os.remove(flag)   # one-shot: tomorrow starts clean
            except OSError:
                pass

    def exit_trade(self, tr, reason, ref=None):
        sym = tr["symbol"]
        tr["exit_reason"] = reason
        if tr.get("stop_order") and not tr.get("stop_cancelled"):
            try:
                st = self.orders.order_status(tr["stop_order"])
                if st.state == "OPEN":
                    self.orders.cancel_order(tr["stop_order"])
                    st = self.orders.order_status(tr["stop_order"])
                if st.state == "COMPLETE":   # the stop filled first
                    self.close(tr, st.avg_price or tr["stop"], "stop")
                    return
                if st.state == "OPEN":
                    raise BrokerError("stop order still open after cancel")
                tr["stop_cancelled"] = True
            except BrokerError as e:
                tr["status"] = "EXITING"
                self.log.error("%s: could not cancel the stop before exiting (%s); retrying.", sym, e)
                self.save()
                return
        if not tr.get("exit_order") and not self.paper:
            # never exit twice: if the position was closed outside the app, a market exit would open a new one
            try:
                held = sum(x["qty"] for x in self.orders.positions() if x["symbol"] == sym)
            except BrokerError as e:
                held = None
                self.log.warning("%s: could not read positions (%s); exiting anyway.", sym, e)
            if held == 0:
                self.log.warning("%s: the broker shows no open position (closed outside the app?).", sym)
                self.close(tr, tr.get("last") or tr["fill"], reason + " / closed outside the app")
                return
        if not tr.get("exit_order"):
            try:
                tr["exit_order"] = self._place(sym, _opp(tr["side"]), tr["qty"], "MARKET", ref=ref, tag="dtsexit")
            except BrokerError as e:
                tr["status"] = "EXITING"
                self.log.error("%s: EXIT ORDER FAILED (%s). Retrying; check the broker app!", sym, e)
                self.save()
                return
        st = self._wait_fill(tr["exit_order"], 15)
        if st.state == "COMPLETE":
            self.close(tr, st.avg_price or ref or tr.get("last") or tr["fill"], reason)
        else:
            if st.state in ("CANCELLED", "REJECTED"):
                self.log.error("%s: exit order %s (%s); retrying.", sym, st.state.lower(), st.message)
                tr["exit_order"] = None
            tr["status"] = "EXITING"
            self.save()

    def close(self, tr, price, reason):
        price = round(float(price), 2)
        b, s = (tr["fill"], price) if tr["side"] == "BUY" else (price, tr["fill"])
        gross = (s - b) * tr["qty"]
        ch = strategy.charges(self.cfg, self.ex, tr["qty"], b, s)
        tr.update(status="CLOSED", exit_price=price, exit_reason=reason, closed_at=self.clock.now().strftime(TS),
                  gross=round(gross, 2), charges=ch, pnl=round(gross - ch, 2))
        self.log.info("EXIT %s %s %d at %.2f (%s): P&L Rs %+.2f after Rs %.2f charges.", tr["symbol"],
                      _opp(tr["side"]), tr["qty"], price, reason, tr["pnl"], ch)
        self._journal(tr)
        self.save()

    def square_off(self, reason):
        for tr in self.open_trades():
            self.exit_trade(tr, reason, ref=tr.get("last"))
        tries = 0
        while self.open_trades() and tries < 12 and self.clock.live:
            tries += 1
            time.sleep(5)
            for tr in self.open_trades():
                self.exit_trade(tr, tr.get("exit_reason") or reason, ref=tr.get("last"))
        for tr in self.open_trades():
            self.log.error("STILL OPEN: %s %d %s. Close it in the broker app NOW.", tr["side"], tr["qty"], tr["symbol"])

    # ------------------------------------------------------------ report
    def _journal(self, tr):
        new = not os.path.exists(self.journal_path)
        with open(self.journal_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["date", "broker", "mode", "symbol", "side", "qty", "entry", "stop", "target", "exit",
                            "reason", "gross", "charges", "pnl", "opened", "closed"])
            w.writerow([self.day.isoformat(), self.broker.name, "paper" if self.paper else "live", tr["symbol"],
                        tr["side"], tr["qty"], tr["fill"], tr["stop"], tr["target"], tr["exit_price"],
                        tr["exit_reason"], tr["gross"], tr["charges"], tr["pnl"], tr["opened_at"], tr["closed_at"]])

    def report(self):
        closed = [t for t in self.state["trades"] if t["status"] == "CLOSED"]
        wins = [t for t in closed if t["pnl"] > 0]
        net = sum(t["pnl"] for t in closed)
        ch = sum(t["charges"] for t in closed)
        doc = {"date": self.day.isoformat(), "broker": self.broker.name, "paper": self.paper,
               "trades": len(closed), "wins": len(wins), "net_pnl": round(net, 2), "charges": round(ch, 2),
               "still_open": len(self.open_trades()), "halted": self.state.get("halted"),
               "detail": self.state["trades"], "skipped": self.state["skipped"]}
        with open(self.report_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, default=str)
        self.save()
        self.log.info("=" * 70)
        self.log.info("DAY SUMMARY %s (%s, %s)", self.day.isoformat(), self.broker.name,
                      "paper" if self.paper else "LIVE")
        for t in closed:
            self.log.info("  %-12s %-4s %5d  in %9.2f  out %9.2f  %-18s Rs %+9.2f", t["symbol"], t["side"], t["qty"],
                          t["fill"], t["exit_price"], t["exit_reason"], t["pnl"])
        if not closed:
            self.log.info("  no trades")
        self.log.info("  trades %d, winners %d, charges Rs %.2f, NET P&L Rs %+.2f", len(closed), len(wins), ch, net)
        self.log.info("  report: %s", os.path.relpath(self.report_path, settings.ROOT))
        self.log.info("=" * 70)


def _rs(x):
    return "{:,.0f}".format(float(x))
