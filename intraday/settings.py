"""Credentials (.env) and strategy settings (config.json).

Credentials live in .env. Switching broker = change these values:

    BROKER=zerodha          # zerodha | upstox | fyers | dhan | angelone
    API_KEY=...
    API_SECRET=...
    ACCESS_TOKEN=...

All market data (candles, history, last price) AND orders go through this
broker's API. "paper_trading": true in config.json simulates the orders on
that real data instead of sending them.

Real environment variables win over the file, so a server can inject them.
"""

import copy
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "trader")

CRED_KEYS = ("BROKER", "API_KEY", "API_SECRET", "ACCESS_TOKEN")
BROKERS = ("zerodha", "upstox", "fyers", "dhan", "angelone")

DEFAULTS = {
    # ---- paper first: simulated orders on the broker's real data. false = real orders.
    "paper_trading": True,

    # ---- money (INR)
    "capital": 100000,               # what this app may use today
    "intraday_leverage": 1,          # 1 = own cash only; MIS leverage (e.g. 5) multiplies buying power
    "max_position_pct": 50,          # one position <= this % of capital x leverage
    "max_loss_per_trade": 500,       # rupees lost if the stop fills, charges included
    "max_daily_loss": 1500,          # stop trading for the day past this (realised + open)
    "max_trades_per_day": 3,
    "max_open_positions": 2,
    "min_reward_risk": 1.5,          # net of estimated charges

    # ---- what to scan
    "exchange": "NSE",               # NSE or BSE
    "universe": "nifty50",           # nifty50 | nifty100 | nifty200 | nifty500 | custom (bigger = slower scans)
    "symbols": [],                   # used when universe = custom, e.g. ["RELIANCE", "TCS"]
    "blocked": [],                   # never trade these
    "min_price": 50,
    "max_price": 20000,

    # ---- the setup: opening-range breakout
    "strategy": {
        "or_minutes": 15,            # 09:15-09:30 range
        "interval": 5,               # minutes per bar
        "min_volume_x": 1.0,         # breakout bar volume >= X x average opening-range bar volume
        "stop": "or-mid",            # or-mid | or-low | pct:0.8 | atr:1.5
        "target": "2R",              # 1R, 1.5R, 2R ... or pct:1.5
        "max_chase_pct": 0.5,        # skip if the price already ran this % past the breakout close
        "allow_short": False,        # also trade breakdowns below the range low (MIS short)
    },

    # ---- the clock (IST)
    "session": {
        "no_entries_before": "09:30",
        "no_entries_after": "14:30",
        "flat_by": "15:10",          # square off everything; brokers auto-square MIS ~15:20 with a fee
    },

    # ---- orders
    "orders": {
        "product": "MIS",            # intraday
        "entry_type": "LIMIT",       # LIMIT (marketable, safer) or MARKET
        "limit_buffer_pct": 0.1,     # marketable limit = LTP +/- this %
        "stop_type": "SL-M",         # SL-M or SL
        "sl_limit_buffer_pct": 0.5,  # SL only: limit price this % beyond the trigger
        "fill_timeout_sec": 30,      # cancel an unfilled entry after this
        "confirm_live_orders": True, # type YES in the terminal before each LIVE entry
        "confirm_timeout_sec": 60,
        "tick_size": 0.05,
    },

    # ---- extra checks before an entry
    "news": {"enabled": True, "hours": 18, "show": 3,
             "block_keywords": ["fraud", "raid", "sebi bans", "insolvency", "resigns"]},
    "timemachine": {"enabled": True, "replays": 3000, "seed": 19, "block_if_leans_stop": False},

    # ---- plumbing
    "poll_seconds": 5,               # how often open positions are checked
    "scan_grace_sec": 5,             # wait this long after a bar closes before scanning it
    "workers": 3,                    # parallel candle requests (each broker's rate limit still applies)
    "extra_holidays": [],            # "YYYY-MM-DD" days to treat as closed
    "extra_trading_days": [],        # "YYYY-MM-DD" days to force open (special sessions)
    "broker_params": {},             # extra fields merged into every order sent to the broker

    # ---- charges model (Indian intraday equity, discount-broker style)
    "charges": {
        "brokerage_flat": 20, "brokerage_pct": 0.03,
        "stt_sell_pct": 0.025, "txn_pct_nse": 0.00297, "txn_pct_bse": 0.00375,
        "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18,
    },
}


class SettingsError(Exception):
    pass


def load_env(path=None):
    path = path or ENV_PATH
    vals = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split(" #", 1)[0].strip().strip('"').strip("'")
                vals[k.strip().upper()] = v
    for k in list(CRED_KEYS) + ["REDIRECT_URI"]:
        if os.environ.get(k):
            vals[k] = os.environ[k]
    vals["BROKER"] = (vals.get("BROKER") or "").strip().lower()
    for k in CRED_KEYS[1:]:
        vals.setdefault(k, "")
    if not vals["BROKER"]:
        raise SettingsError("No BROKER in .env. Copy .env.example to .env and fill in BROKER, API_KEY, "
                            "API_SECRET and ACCESS_TOKEN.")
    if vals["BROKER"] not in BROKERS:
        raise SettingsError("BROKER=%s is not supported. Use one of: %s" % (vals["BROKER"], ", ".join(BROKERS)))
    return vals


def save_env_value(key, value, path=None):
    """Replace (or append) KEY=value in .env, keeping every other line."""
    path = path or ENV_PATH
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    rx = re.compile(r"^\s*%s\s*=" % re.escape(key), re.I)
    for i, ln in enumerate(lines):
        if rx.match(ln):
            lines[i] = "%s=%s" % (key, value)
            break
    else:
        lines.append("%s=%s" % (key, value))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue  # "_comment" keys
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    path = path or CONFIG_PATH
    user = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                user = json.load(fh)
        except ValueError as e:
            raise SettingsError("%s is not valid JSON: %s" % (path, e))
    cfg = _merge(DEFAULTS, user)
    cfg["exchange"] = str(cfg["exchange"]).upper()
    cfg["blocked"] = [str(s).upper() for s in cfg["blocked"]]
    cfg["symbols"] = [str(s).upper() for s in cfg["symbols"]]
    validate(cfg)
    return cfg


def validate(cfg):
    errs = []
    if cfg["exchange"] not in ("NSE", "BSE"):
        errs.append('exchange must be "NSE" or "BSE"')
    for k in ("capital", "max_loss_per_trade", "max_daily_loss", "intraday_leverage"):
        if not isinstance(cfg[k], (int, float)) or cfg[k] <= 0:
            errs.append("%s must be a positive number" % k)
    s = cfg["strategy"]
    if s["interval"] not in (1, 3, 5, 15):
        errs.append("strategy.interval must be 1, 3, 5 or 15")
    elif s["or_minutes"] <= 0 or s["or_minutes"] % s["interval"]:
        errs.append("strategy.or_minutes must be a multiple of strategy.interval")
    if not re.match(r"^(or-mid|or-low|pct:[\d.]+|atr:[\d.]+)$", str(s["stop"])):
        errs.append("strategy.stop must be or-mid, or-low, pct:X or atr:K")
    if not re.match(r"^([\d.]+R|pct:[\d.]+)$", str(s["target"])):
        errs.append("strategy.target must be like 2R or pct:1.5")
    for k in ("no_entries_before", "no_entries_after", "flat_by"):
        if not re.match(r"^\d{1,2}:\d{2}$", str(cfg["session"][k])):
            errs.append("session.%s must look like 14:30" % k)
    if cfg["orders"]["entry_type"] not in ("LIMIT", "MARKET"):
        errs.append("orders.entry_type must be LIMIT or MARKET")
    if cfg["orders"]["stop_type"] not in ("SL-M", "SL"):
        errs.append("orders.stop_type must be SL-M or SL")
    if cfg["universe"] not in ("nifty50", "nifty100", "nifty200", "nifty500", "custom"):
        errs.append("universe must be nifty50, nifty100, nifty200, nifty500 or custom")
    if cfg["universe"] == "custom" and not cfg["symbols"]:
        errs.append('universe "custom" needs a "symbols" list')
    if errs:
        raise SettingsError("config.json: " + "; ".join(errs))


def data_path(*parts):
    p = os.path.join(DATA_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p
