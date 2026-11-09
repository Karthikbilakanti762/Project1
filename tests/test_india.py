"""Offline tests for the India intraday core. No network, no credentials, any OS.

    python3 -m unittest discover -s tests -v
"""

import datetime
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intraday import market, settings, strategy, timemachine  # noqa: E402
from intraday.brokers import PaperBroker  # noqa: E402
from intraday.console import setup_log  # noqa: E402
from intraday.data import Bar, ReplayFeed, resample, session_bars  # noqa: E402
from intraday.engine import Engine  # noqa: E402

DAY = datetime.date(2026, 11, 6)   # a Friday, NSE open


def cfg_with(**over):
    c = settings._merge(settings.DEFAULTS, over)
    settings.validate(c)
    return c


def day_bars(day, fn):
    start = market.at(day, market.OPEN)
    return [Bar(start + datetime.timedelta(minutes=5 * i), *fn(i)) for i in range(75)]


def random_day(day, seed, base=100.0):
    rnd = random.Random(seed)
    p = [base]

    def fn(i):
        o = p[0]
        c = o * (1 + rnd.gauss(0, 0.002))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.001)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.001)))
        p[0] = c
        return (o, h, l, c, 1000)
    return day_bars(day, fn)


def breakout_day(day, drift):
    """Range 100-101 for 09:15-09:30; the 09:30 bar closes 101.5 on 3x volume, then moves `drift` a bar."""
    def fn(i):
        if i < 3:
            return (100.5, 101.0, 100.0, 100.5, 1000)
        if i == 3:
            return (100.6, 101.6, 100.5, 101.5, 3000)
        c = 101.5 + drift * (i - 3)
        o = c - drift
        return (o, max(o, c) + 0.2, min(o, c) - 0.2, c, 1000)
    return day_bars(day, fn)


class FakeBroker:
    name = "fake"

    def __init__(self, series):
        self.series = series

    def candles(self, sym, exchange, interval, d0, d1):
        return [b for b in self.series[sym] if d0 <= b.start.date() <= d1]

    def profile(self):
        return {"user": "test", "funds": 100000.0}


def build_series():
    series = {"UPCO": [], "DNCO": []}
    d = DAY - datetime.timedelta(days=45)
    k = 0
    while d < DAY:
        if market.is_trading_day(d)[0]:
            k += 1
            series["UPCO"] += random_day(d, k)
            series["DNCO"] += random_day(d, 1000 + k)
        d += datetime.timedelta(days=1)
    series["UPCO"] += breakout_day(DAY, +0.5)
    series["DNCO"] += breakout_day(DAY, -0.5)
    return series


class TestMarket(unittest.TestCase):
    def test_ist_conversions(self):
        self.assertEqual(market.ts_to_ist(market.ist_to_ts(datetime.datetime(2026, 11, 9, 9, 15))),
                         datetime.datetime(2026, 11, 9, 9, 15))
        for s in ("2026-11-09T09:15:00+0530", "2026-11-09T09:15:00+05:30", "2026-11-09T03:45:00Z",
                  "2026-11-09 09:15:00"):
            self.assertEqual(market.parse_iso_ist(s), datetime.datetime(2026, 11, 9, 9, 15), s)

    def test_calendar(self):
        self.assertFalse(market.is_trading_day(datetime.date(2026, 11, 10))[0])   # Diwali Balipratipada
        self.assertTrue(market.is_trading_day(datetime.date(2026, 11, 11))[0])
        self.assertFalse(market.is_trading_day(datetime.date(2026, 11, 14))[0])   # Saturday
        self.assertTrue(market.is_trading_day(datetime.date(2026, 11, 14),
                                              {"extra_trading_days": ["2026-11-14"]})[0])
        self.assertFalse(market.is_trading_day(DAY, {"extra_holidays": [DAY.isoformat()]})[0])
        self.assertEqual(market.next_trading_day(datetime.date(2026, 11, 9)), datetime.date(2026, 11, 11))

    def test_bar_close_after(self):
        self.assertEqual(market.bar_close_after(market.at(DAY, datetime.time(9, 30)), 5, DAY),
                         market.at(DAY, datetime.time(9, 35)))
        self.assertEqual(market.bar_close_after(market.at(DAY, datetime.time(9, 31, 10)), 5, DAY),
                         market.at(DAY, datetime.time(9, 35)))


class TestData(unittest.TestCase):
    def test_session_bars_drops_forming_bar(self):
        got = session_bars(breakout_day(DAY, 0.5), DAY, 5, market.at(DAY, datetime.time(9, 37)))
        self.assertEqual(len(got), 4)          # 09:15..09:30 complete; 09:35 still forming
        self.assertEqual(got[-1].start.time(), datetime.time(9, 30))

    def test_resample(self):
        start = market.at(DAY, market.OPEN)
        ones = [Bar(start + datetime.timedelta(minutes=i), 10 + i, 11 + i, 9 + i, 10.5 + i, 10) for i in range(10)]
        five = resample(ones, 5)
        self.assertEqual(len(five), 2)
        self.assertEqual((five[0].o, five[0].h, five[0].l, five[0].c, five[0].v), (10, 15, 9, 14.5, 50))


class TestStrategy(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_with()

    def bars(self, drift):
        return session_bars(breakout_day(DAY, drift), DAY, 5, market.at(DAY, datetime.time(9, 35)))

    def test_breakout_and_plan(self):
        bars = self.bars(0.5)
        t = strategy.find_breakout(bars, 3, self.cfg["strategy"], 5)
        self.assertEqual((t["side"], t["bar_index"], t["price"], t["or_high"], t["or_low"]),
                         ("BUY", 3, 101.5, 101.0, 100.0))
        self.assertEqual(t["at"].time(), datetime.time(9, 35))
        p = strategy.plan("UPCO", t, 101.5, bars, self.cfg["strategy"], 0.05)
        self.assertEqual((p["stop"], p["target"], p["risk_ps"]), (100.5, 103.5, 1.0))

    def test_volume_rule(self):
        s = dict(self.cfg["strategy"], min_volume_x=5)
        self.assertIsNone(strategy.find_breakout(self.bars(0.5), 3, s, 5))

    def test_short_only_when_allowed(self):
        down = [Bar(b.start, b.o, b.h, b.l, 99.0 if i == 3 else b.c, b.v) for i, b in enumerate(self.bars(0.5))]
        self.assertIsNone(strategy.find_breakout(down, 3, self.cfg["strategy"], 5))
        s = dict(self.cfg["strategy"], allow_short=True)
        t = strategy.find_breakout(down, 3, s, 5)
        p = strategy.plan("X", t, 99.0, down, s, 0.05)
        self.assertEqual((t["side"], p["stop"], p["target"]), ("SELL", 100.5, 96.0))

    def test_size_respects_max_loss_with_charges(self):
        p = {"symbol": "X", "side": "BUY", "entry": 101.5, "stop": 100.5, "target": 103.5, "risk_ps": 1.0}
        qty, why = strategy.size(self.cfg, p, 100000)
        self.assertGreater(qty, 0)
        self.assertLessEqual(strategy.loss_at_stop(self.cfg, p, qty), self.cfg["max_loss_per_trade"])
        self.assertGreater(strategy.loss_at_stop(self.cfg, p, qty + 1), self.cfg["max_loss_per_trade"])

    def test_charges_reasonable(self):
        ch = strategy.charges(self.cfg, "NSE", 100, 1000, 1010)   # Rs 2 lakh turnover
        self.assertTrue(40 < ch < 120, ch)

    def test_round_tick(self):
        self.assertEqual(strategy.round_tick(100.52, 0.05, "down"), 100.5)
        self.assertEqual(strategy.round_tick(100.52, 0.05, "up"), 100.55)
        self.assertEqual(strategy.round_tick(100.5, 0.05, "down"), 100.5)


class TestPaper(unittest.TestCase):
    def test_stop_fills_at_worse_price_on_gap(self):
        tmp = tempfile.mkdtemp()
        try:
            pb = PaperBroker(FakeBroker({}), lambda s: 100.0, os.path.join(tmp, "b.json"), 100000)
            sid = pb.place_order("X", "NSE", "SELL", 10, "SL-M", trigger=98.0)
            self.assertEqual(pb.order_status(sid).state, "OPEN")
            pb.simulate("X", 97.0, 95.0, 96.0)            # gapped through 98
            st = pb.order_status(sid)
            self.assertEqual(st.state, "COMPLETE")
            self.assertLess(st.avg_price, 97.01)
            bad = pb.place_order("X", "NSE", "SELL", 10, "SL-M", trigger=101.0)   # above the last price
            self.assertEqual(pb.order_status(bad).state, "REJECTED")
        finally:
            shutil.rmtree(tmp)


class TestSettings(unittest.TestCase):
    def test_env_roundtrip(self):
        tmp = tempfile.mkdtemp()
        old = {k: os.environ.pop(k, None) for k in settings.CRED_KEYS}
        try:
            p = os.path.join(tmp, ".env")
            with open(p, "w") as fh:
                fh.write("# c\nBROKER=Upstox\nAPI_KEY=abc  # key\nAPI_SECRET='s'\nACCESS_TOKEN=\n")
            env = settings.load_env(p)
            self.assertEqual((env["BROKER"], env["API_KEY"], env["API_SECRET"]), ("upstox", "abc", "s"))
            settings.save_env_value("ACCESS_TOKEN", "tok123", p)
            self.assertEqual(settings.load_env(p)["ACCESS_TOKEN"], "tok123")
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
            shutil.rmtree(tmp)

    def test_bad_config_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            cfg_with(exchange="NYSE")


class TestTimeMachine(unittest.TestCase):
    def test_runs_and_is_repeatable(self):
        series = build_series()["UPCO"]
        p = {"side": "BUY", "entry": 101.5, "stop": 100.5, "target": 103.5, "risk_ps": 1.0, "r_multiple": 2.0,
             "stop_rule": "or-mid"}
        a = timemachine.run(p, series, DAY, 5, 4, 71, 3, replays=300, seed=7)
        b = timemachine.run(p, series, DAY, 5, 4, 71, 3, replays=300, seed=7)
        self.assertTrue(a["ok"], a)
        self.assertEqual(a["pct"], b["pct"])
        self.assertAlmostEqual(sum(a["pct"].values()), 100.0, delta=0.2)


class TestReplayDay(unittest.TestCase):
    """The whole pipeline on a synthetic day: one winner (target), one loser (stop)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = settings.DATA_DIR
        settings.DATA_DIR = self.tmp

    def tearDown(self):
        settings.DATA_DIR = self.old
        import logging
        for h in logging.getLogger("intraday").handlers:
            h.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_day(self):
        cfg = cfg_with(poll_seconds=60, timemachine={"replays": 200}, news={"enabled": False})
        clock = market.SimClock(market.at(DAY, datetime.time(9, 0)))
        log = setup_log(clock, None)
        log.handlers[0].setLevel(40)   # quiet
        broker = FakeBroker(build_series())
        feed = ReplayFeed(clock, broker, DAY, 5, "NSE", 1, log)
        have = feed.preload(["UPCO", "DNCO"])
        self.assertEqual(sorted(have), ["DNCO", "UPCO"])
        orders = PaperBroker(broker, lambda s: feed.ltp([s]).get(s), os.path.join(self.tmp, "book.json"), 100000)
        eng = Engine(cfg, broker, orders, feed, clock, log, replay=True)
        eng.universe = have
        self.assertEqual(eng.run(), 0)
        trades = {t["symbol"]: t for t in eng.state["trades"]}
        self.assertEqual(set(trades), {"UPCO", "DNCO"})
        up, dn = trades["UPCO"], trades["DNCO"]
        self.assertEqual((up["status"], up["exit_reason"]), ("CLOSED", "target"))
        self.assertEqual((dn["status"], dn["exit_reason"]), ("CLOSED", "stop"))
        self.assertEqual((up["fill"], up["stop"], up["target"]), (101.5, 100.5, 103.5))
        self.assertGreater(up["pnl"], 0)
        self.assertLess(dn["pnl"], 0)
        self.assertLessEqual(-dn["pnl"], cfg["max_loss_per_trade"] + 20)   # + paper slippage
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "journal", "trades-replay.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "reports", DAY.isoformat() + "-replay.json")))


if __name__ == "__main__":
    unittest.main()
