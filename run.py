#!/usr/bin/env python3
"""Day Trader (India): NSE / BSE intraday, the whole pipeline in one command.

    python run.py                 run today's session start to finish (paper or live per config.json)
    python run.py check           test the broker login, funds, prices and candles
    python run.py login           make today's ACCESS_TOKEN and save it to .env
    python run.py replay 2026-11-06   replay a past day bar by bar (paper, broker history)
    python run.py scan            one scan right now: which charts broke their opening range
    python run.py status          today's trades so far
    python run.py flatten         exit every open intraday position now (asks YES)

Credentials: .env (BROKER, API_KEY, API_SECRET, ACCESS_TOKEN). Settings: config.json.
Not financial advice. Most intraday traders lose money. Paper trade first.
"""

import argparse
import datetime
import json
import os
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from intraday import market, settings, strategy, universe  # noqa: E402
from intraday.brokers import CREDENTIALS_HELP, BrokerError, PaperBroker, make_broker  # noqa: E402
from intraday.console import Prompt, setup_log  # noqa: E402
from intraday.data import BrokerFeed, DataError, ReplayFeed  # noqa: E402
from intraday.engine import Engine  # noqa: E402


def load():
    if not os.path.exists(settings.CONFIG_PATH):
        import shutil
        shutil.copy(os.path.join(ROOT, "config.example.json"), settings.CONFIG_PATH)
        print("Created config.json from config.example.json (paper trading on). Edit it any time.")
    if not os.path.exists(settings.ENV_PATH):
        raise settings.SettingsError("No .env file. Copy .env.example to .env and fill in BROKER, API_KEY, "
                                     "API_SECRET and ACCESS_TOKEN.")
    env = settings.load_env()
    cfg = settings.load_config()
    return env, cfg


def _log(clock, name, verbose):
    return setup_log(clock, settings.data_path("logs", name), verbose)


# ---------------------------------------------------------------- run (the whole day)

def cmd_run(a):
    env, cfg = load()
    clock = market.LiveClock()
    day = clock.now().date()
    log = _log(clock, "%s.log" % day.isoformat(), a.verbose)
    broker = make_broker(env, cfg, log)
    feed = BrokerFeed(broker, clock, cfg["exchange"], cfg["workers"], log)
    paper = cfg["paper_trading"]
    if paper:
        orders = PaperBroker(broker, lambda s: feed.ltp([s]).get(s),
                             settings.data_path("state", "%s-paper-book.json" % day.isoformat()), cfg["capital"], log)
    else:
        orders = broker
        if not a.yes:
            print("\n  LIVE TRADING with %s: real orders, real money (MIS intraday on %s)." % (broker.name,
                                                                                            cfg["exchange"]))
            print("  Every entry will still ask for YES unless confirm_live_orders is false.")
            if input("  Type LIVE to start: ").strip().upper() != "LIVE":
                print("Not started.")
                return 1
    eng = Engine(cfg, broker, orders, feed, clock, log, prompt=Prompt())
    try:
        return eng.run()
    except KeyboardInterrupt:
        log.warning("Stopped with Ctrl+C. Open positions keep their stop orders at the broker; "
                    "run `python run.py` again to resume, or `python run.py flatten` to exit.")
        eng.save()
        return 1


# ---------------------------------------------------------------- replay

def cmd_replay(a):
    env, cfg = load()
    try:
        day = datetime.date.fromisoformat(a.date)
    except ValueError:
        print("replay wants a date like 2026-11-06")
        return 1
    clock = market.SimClock(market.at(day, datetime.time(9, 0)))
    log = _log(clock, "replay-%s.log" % day.isoformat(), a.verbose)
    ok, why = market.is_trading_day(day, cfg)
    if not ok:
        log.error("%s was not a trading day (%s).", day.isoformat(), why)
        return 1
    cfg["poll_seconds"] = 60
    broker = make_broker(env, cfg, log)
    syms = universe.load(cfg, day, log)
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    feed = ReplayFeed(clock, broker, day, cfg["strategy"]["interval"], cfg["exchange"], cfg["workers"], log)
    log.info("Loading %d symbols' history from %s (one request each, rate-limited)...", len(syms), broker.name)
    have = feed.preload(syms)
    if not have:
        log.error("No %s candles for %s from %s. Too old for the broker's history, or the token has no "
                  "data permission?", cfg["exchange"], day.isoformat(), broker.name)
        return 1
    book = settings.data_path("state", "%s-replay-book.json" % day.isoformat())
    if os.path.exists(book):
        os.remove(book)
    orders = PaperBroker(broker, lambda s: feed.ltp([s]).get(s), book, cfg["capital"], log)
    eng = Engine(cfg, broker, orders, feed, clock, log, replay=True)
    eng.universe = have
    return eng.run()


# ---------------------------------------------------------------- check / login

def cmd_check(a):
    env, cfg = load()
    clock = market.LiveClock()
    log = _log(clock, "check.log", a.verbose)
    name = env["BROKER"]
    helps = CREDENTIALS_HELP[name]
    print("Broker: %s   (paper_trading: %s)" % (name, cfg["paper_trading"]))
    for k, h in zip(("API_KEY", "API_SECRET", "ACCESS_TOKEN"), helps):
        v = env.get(k) or ""
        shown = (v[:4] + "..." + v[-3:]) if len(v) > 10 else ("(set)" if v else "(EMPTY)")
        print("  %-13s %-16s %s" % (k, shown, h))
    now = clock.now()
    ok, why = market.is_trading_day(now.date(), cfg)
    print("IST now %s: %s" % (now.strftime("%a %d %b %Y %H:%M"), "trading day" if ok else "closed (%s)" % why))
    if not ok:
        print("  next session: %s" % market.next_trading_day(now.date(), cfg).strftime("%a %d %b %Y"))
    broker = make_broker(env, cfg, log)
    fails = 0
    try:
        p = broker.profile()
        print("OK   login: %s, funds Rs %s" % (p["user"], "{:,.2f}".format(p["funds"])))
    except BrokerError as e:
        fails += 1
        print("FAIL login: %s" % e)
        print("     -> expired token? run:  python run.py login")
    syms = (cfg["symbols"] or universe.NIFTY50)[:3]
    try:
        print("OK   last prices: %s" % ", ".join("%s %.2f" % kv for kv in broker.ltp(syms, cfg["exchange"]).items()))
    except BrokerError as e:
        fails += 1
        print("FAIL last prices: %s" % e)
    d = now.date() if ok and now.time() >= market.OPEN else _prev_session(now.date(), cfg)
    try:
        iv = cfg["strategy"]["interval"]
        bars = [b for b in broker.candles(syms[0], cfg["exchange"], iv, d, d) if b.start.date() == d]
        print("OK   %d-min candles: %d bars for %s on %s%s" % (iv, len(bars), syms[0], d.isoformat(),
              (", last %s close %.2f" % (bars[-1].start.strftime("%H:%M"), bars[-1].c)) if bars else ""))
    except BrokerError as e:
        fails += 1
        print("FAIL candles: %s" % e)
    print("All good." if not fails else "%d check(s) failed." % fails)
    return 1 if fails else 0


def _prev_session(d, cfg):
    d -= datetime.timedelta(days=1)
    while not market.is_trading_day(d, cfg)[0]:
        d -= datetime.timedelta(days=1)
    return d


def cmd_login(a):
    env, cfg = load()
    clock = market.LiveClock()
    log = _log(clock, "check.log", a.verbose)
    broker = make_broker(env, cfg, log)
    name = broker.name
    if name == "dhan":
        print("Dhan: create the access token on web.dhan.co (My Profile -> DhanHQ Trading APIs),")
        print("then paste it into .env as ACCESS_TOKEN. API_KEY is your Dhan Client ID.")
        tok = input("Or paste the token here now (Enter to skip): ").strip()
        if not tok:
            return 0
    else:
        if name == "angelone":
            print("Angel One: API_SECRET in .env must be your client code.")
            code = input("Enter your PIN and the current TOTP, separated by a space: ").strip()
        else:
            url = broker.login_url()
            print("1. Log in here (opening your browser):\n   %s" % url)
            try:
                webbrowser.open(url)
            except Exception:
                pass
            print("2. After login the browser goes to your redirect URL. Copy that whole URL.")
            code = input("Paste it here: ").strip()
        tok = broker.create_access_token(code)
    settings.save_env_value("ACCESS_TOKEN", tok)
    print("Saved ACCESS_TOKEN to .env. Now run:  python run.py check")
    return 0


# ---------------------------------------------------------------- scan / status / flatten

def cmd_scan(a):
    env, cfg = load()
    clock = market.LiveClock()
    log = _log(clock, "scan.log", a.verbose)
    broker = make_broker(env, cfg, log)
    feed = BrokerFeed(broker, clock, cfg["exchange"], cfg["workers"], log)
    s = cfg["strategy"]
    n = s["or_minutes"] // s["interval"]
    syms = universe.load(cfg, clock.now().date(), log)
    log.info("Reading %d charts from %s...", len(syms), broker.name)
    data = feed.many_today(syms, s["interval"])
    hits = []
    for sym, bars in data.items():
        t = strategy.find_breakout(bars, n, s, s["interval"])
        if t:
            hits.append((t["at"], sym, t, bars[-1].c))
    hits.sort(key=lambda x: (x[0], x[1]))
    log.info("%d of %d charts closed beyond their %d-minute opening range:", len(hits), len(data), s["or_minutes"])
    for at, sym, t, last in hits:
        log.info("  %s  %-12s %-4s range %.2f-%.2f  trigger %.2f  now %.2f", at.strftime("%H:%M"), sym,
                 "UP" if t["side"] == "BUY" else "DOWN", t["or_low"], t["or_high"], t["price"], last)
    log.info("A scan is a list of charts that match a rule, not a list of stocks to buy.")
    return 0


def cmd_status(a):
    day = market.ist_now().date().isoformat()
    p = os.path.join(settings.DATA_DIR, "state", day + ".json")
    if not os.path.exists(p):
        print("No trades recorded today (%s)." % day)
        return 0
    with open(p, encoding="utf-8") as fh:
        st = json.load(fh)
    print("%s  %s  %s" % (day, st.get("broker"), "paper" if st.get("paper") else "LIVE"))
    for t in st["trades"]:
        print("  %-8s %-12s %-4s %5s  in %-9s stop %-9s target %-9s %s %s" % (
            t["status"], t["symbol"], t["side"], t["qty"], t.get("fill"), t["stop"], t["target"],
            ("out %s (%s) Rs %+.2f" % (t.get("exit_price"), t.get("exit_reason"), t.get("pnl") or 0))
            if t["status"] == "CLOSED" else "", t.get("error") or ""))
    if st.get("halted"):
        print("  halted: %s" % st["halted"])
    return 0


def cmd_flatten(a):
    env, cfg = load()
    clock = market.LiveClock()
    log = _log(clock, "%s.log" % clock.now().date().isoformat(), a.verbose)
    beat = os.path.join(settings.DATA_DIR, "state", "heartbeat")
    running = os.path.exists(beat) and time.time() - os.path.getmtime(beat) < 330   # loop sleeps <= ~5 min
    if running or cfg["paper_trading"]:
        # the running app owns its positions: let it cancel its stops and exit, so nothing is sold twice
        os.makedirs(settings.DATA_DIR, exist_ok=True)
        open(os.path.join(settings.DATA_DIR, "FLATTEN"), "w").close()
        log.info("Wrote trader/FLATTEN: the running app exits everything within a few seconds and stops "
                 "taking new trades today.%s", "" if running else " (Start `run.py` if it is not running.)")
        return 0
    broker = make_broker(env, cfg, log)
    # cancel this app's resting stop orders, then exit what the broker shows as open MIS positions
    p = os.path.join(settings.DATA_DIR, "state", clock.now().date().isoformat() + ".json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for t in json.load(fh).get("trades", []):
                if t.get("stop_order") and t["status"] in ("OPEN", "EXITING"):
                    try:
                        broker.cancel_order(t["stop_order"])
                        log.info("cancelled stop %s (%s)", t["stop_order"], t["symbol"])
                    except BrokerError as e:
                        log.warning("cancel %s: %s", t["stop_order"], e)
    pos = broker.positions()
    if not pos:
        log.info("No open intraday positions at %s.", broker.name)
        return 0
    for x in pos:
        log.info("  open: %s %s qty %d", x["exchange"], x["symbol"], x["qty"])
    if input("Type YES to exit all of these at market: ").strip().upper() != "YES":
        log.info("Nothing sent.")
        return 1
    for x in pos:
        side = "SELL" if x["qty"] > 0 else "BUY"
        try:
            oid = broker.place_order(x["symbol"], x["exchange"] or cfg["exchange"], side, abs(x["qty"]), "MARKET",
                                     tag="dtsflat")
            log.info("  %s %d %s -> order %s", side, abs(x["qty"]), x["symbol"], oid)
        except BrokerError as e:
            log.error("  %s: %s  (close it in the broker app!)", x["symbol"], e)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Day Trader (India): NSE/BSE intraday in one command.")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug lines on screen too")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="run today's session start to finish (default)")
    p.add_argument("--yes", action="store_true", help="skip the LIVE start confirmation")
    sub.add_parser("check", help="test login, funds, prices and candles")
    sub.add_parser("login", help="make today's ACCESS_TOKEN and save it to .env")
    p = sub.add_parser("replay", help="replay a past day on paper")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--symbols", help="comma-separated symbols instead of the universe")
    sub.add_parser("scan", help="one opening-range scan right now")
    sub.add_parser("status", help="today's trades")
    sub.add_parser("flatten", help="exit every open intraday position")
    a = ap.parse_args(argv)
    if not a.cmd:
        a.cmd, a.yes = "run", False
    cmds = {"run": cmd_run, "check": cmd_check, "login": cmd_login, "replay": cmd_replay, "scan": cmd_scan,
            "status": cmd_status, "flatten": cmd_flatten}
    try:
        return cmds[a.cmd](a)
    except (settings.SettingsError, BrokerError, DataError) as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
