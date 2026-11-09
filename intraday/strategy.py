"""The trade: opening-range breakout -> exit plan -> position size -> risk check.

Scanner / entry   The opening range is the high and low of the first
                  `or_minutes` of the session (09:15-09:30 by default). A trigger
                  is the first COMPLETED bar after the range that CLOSES above
                  the range high (long) or, with allow_short, below the range low
                  (short). A wick through the level is not a trigger. With
                  min_volume_x the trigger bar also needs that multiple of the
                  average opening-range bar volume.
Exit plan         Stop: or-mid (range midpoint), or-low (the far side of the
                  range), pct:X or atr:K. Rounded to the tick AWAY from the entry.
                  Target: R multiple of the risk (2R) or pct:X. Out by flat_by
                  either way.
Budget            qty = floor(min(max loss / risk per share, position cap / entry,
                  cash x leverage / entry)), then trimmed until the loss at the
                  stop INCLUDING charges fits max_loss_per_trade.
Risk              every rule in check(); one failing rule blocks the order.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
"""

import datetime
import math
import os

from . import market, settings


# ---------------------------------------------------------------- prices

def round_tick(x, tick, mode="near"):
    n = x / tick
    n = math.floor(n + 1e-9) if mode == "down" else math.ceil(n - 1e-9) if mode == "up" else round(n)
    return round(n * tick, 2)


def atr(bars, n=14):
    if len(bars) < 2:
        return None
    trs = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in zip(bars, bars[1:])]
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else None


# ---------------------------------------------------------------- setup

def find_breakout(bars, n_range, scfg, interval):
    """First completed bar after the range that closes beyond it (with the volume rule). None = no trigger."""
    if len(bars) <= n_range:
        return None
    rng = bars[:n_range]
    oh, ol = max(b.h for b in rng), min(b.l for b in rng)
    avg_vol = sum(b.v for b in rng) / float(n_range)
    vx = float(scfg.get("min_volume_x") or 0)
    for k in range(n_range, len(bars)):
        b = bars[k]
        side = "BUY" if b.c > oh else "SELL" if (scfg.get("allow_short") and b.c < ol) else None
        if not side:
            continue
        if vx and avg_vol and b.v < vx * avg_vol:
            continue
        return {"side": side, "bar_index": k, "price": round(b.c, 2), "or_high": round(oh, 2),
                "or_low": round(ol, 2), "volume_x": round(b.v / avg_vol, 2) if avg_vol else None,
                "at": b.start + datetime.timedelta(minutes=interval), "bar_start": b.start}
    return None


# ---------------------------------------------------------------- exit plan

class PlanError(Exception):
    pass


def plan(symbol, trig, entry, bars, scfg, tick):
    """-> {side, entry, stop, target, risk_ps, ...} or raise PlanError."""
    side = trig["side"]
    long_ = side == "BUY"
    oh, ol = trig["or_high"], trig["or_low"]
    rule = str(scfg["stop"])
    if rule == "or-mid":
        stop = (oh + ol) / 2.0
    elif rule == "or-low":
        stop = ol if long_ else oh
    elif rule.startswith("pct:"):
        p = float(rule[4:]) / 100.0
        stop = entry * (1 - p) if long_ else entry * (1 + p)
    else:  # atr:K on the session's bars up to the trigger
        k = float(rule[4:])
        a = atr(bars[:trig["bar_index"] + 1])
        if not a:
            raise PlanError("not enough bars for ATR")
        stop = entry - k * a if long_ else entry + k * a
    stop = round_tick(stop, tick, "down" if long_ else "up")
    risk_ps = round(entry - stop if long_ else stop - entry, 2)
    if risk_ps <= 0:
        raise PlanError("the stop %.2f is on the wrong side of the entry %.2f" % (stop, entry))
    if risk_ps < entry * 0.0005:
        raise PlanError("risk %.2f a share is under 0.05%% of the price: too tight to survive the spread" % risk_ps)
    tr = str(scfg["target"])
    if tr.endswith("R"):
        r = float(tr[:-1])
        target = entry + r * risk_ps if long_ else entry - r * risk_ps
    else:
        p = float(tr[4:]) / 100.0
        target = entry * (1 + p) if long_ else entry * (1 - p)
    target = round_tick(target, tick, "near")
    if (long_ and target <= entry) or (not long_ and target >= entry):
        raise PlanError("the target is not beyond the entry")
    return {"symbol": symbol, "side": side, "entry": round(entry, 2), "stop": stop, "target": target,
            "risk_ps": risk_ps, "r_multiple": round(abs(target - entry) / risk_ps, 2),
            "or_high": oh, "or_low": ol, "trigger_price": trig["price"], "trigger_at": trig["at"].strftime("%H:%M"),
            "stop_rule": rule, "target_rule": tr}


# ---------------------------------------------------------------- charges

def charges(cfg, exchange, qty, buy_price, sell_price):
    """Estimated statutory + brokerage charges for one round trip (rupees)."""
    c = cfg["charges"]
    buy, sell = qty * buy_price, qty * sell_price
    brokerage = sum(min(c["brokerage_flat"], t * c["brokerage_pct"] / 100.0) for t in (buy, sell))
    stt = sell * c["stt_sell_pct"] / 100.0
    txn = (buy + sell) * (c["txn_pct_nse"] if exchange == "NSE" else c["txn_pct_bse"]) / 100.0
    sebi = (buy + sell) * c["sebi_per_crore"] / 1e7
    stamp = buy * c["stamp_buy_pct"] / 100.0
    gst = (brokerage + txn + sebi) * c["gst_pct"] / 100.0
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)


def _round_trip(p, qty, exit_price):
    return (p["entry"], exit_price) if p["side"] == "BUY" else (exit_price, p["entry"])


def loss_at_stop(cfg, p, qty):
    b, s = _round_trip(p, qty, p["stop"])
    return qty * p["risk_ps"] + charges(cfg, cfg["exchange"], qty, b, s)


def gain_at_target(cfg, p, qty):
    b, s = _round_trip(p, qty, p["target"])
    return qty * abs(p["target"] - p["entry"]) - charges(cfg, cfg["exchange"], qty, b, s)


# ---------------------------------------------------------------- budget

def size(cfg, p, cash):
    """-> (qty, why) where why names the binding limit."""
    entry = p["entry"]
    lev = float(cfg["intraday_leverage"])
    caps = {
        "max loss per trade": cfg["max_loss_per_trade"] / p["risk_ps"],
        "position cap": cfg["capital"] * lev * cfg["max_position_pct"] / 100.0 / entry,
        "cash": min(cfg["capital"], cash) * lev / entry,
    }
    why = min(caps, key=caps.get)
    qty = int(math.floor(caps[why] + 1e-9))
    while qty > 0 and loss_at_stop(cfg, p, qty) > cfg["max_loss_per_trade"]:
        qty -= 1
        why = "max loss incl. charges"
    return qty, why


# ---------------------------------------------------------------- risk

def check(cfg, p, qty, state, now, cash):
    """-> list of reasons to BLOCK (empty = PASS)."""
    s = cfg["session"]
    t = now.time()
    why = []
    ok, reason = market.is_trading_day(now.date(), cfg)
    if not ok:
        why.append("market closed: " + reason)
    if t < market.hhmm(s["no_entries_before"]) or t > market.hhmm(s["no_entries_after"]):
        why.append("outside the entry window %s-%s" % (s["no_entries_before"], s["no_entries_after"]))
    if os.path.exists(os.path.join(settings.DATA_DIR, "STOP")):
        why.append("kill switch: trader/STOP exists")
    if state.get("halted"):
        why.append("halted: " + state["halted"])
    trades = [x for x in state["trades"] if x["status"] != "FAILED"]
    if len(trades) >= cfg["max_trades_per_day"]:
        why.append("max_trades_per_day (%d) reached" % cfg["max_trades_per_day"])
    open_ = [x for x in trades if x["status"] in ("OPEN", "EXITING")]
    if len(open_) >= cfg["max_open_positions"]:
        why.append("max_open_positions (%d) reached" % cfg["max_open_positions"])
    if any(x["symbol"] == p["symbol"] for x in trades):
        why.append("already traded %s today" % p["symbol"])
    if p["symbol"] in cfg["blocked"]:
        why.append("%s is on the blocked list" % p["symbol"])
    if p["side"] == "SELL" and not cfg["strategy"].get("allow_short"):
        why.append("shorts are off (strategy.allow_short)")
    if not (cfg["min_price"] <= p["entry"] <= cfg["max_price"]):
        why.append("price %.2f outside min_price / max_price" % p["entry"])
    if qty < 1:
        why.append("0 shares: the stop is too far for max_loss_per_trade Rs %s" % cfg["max_loss_per_trade"])
        return why
    loss = loss_at_stop(cfg, p, qty)
    gain = gain_at_target(cfg, p, qty)
    if loss > cfg["max_loss_per_trade"] + 0.01:
        why.append("loss at stop Rs %.2f > max_loss_per_trade" % loss)
    rr = gain / loss if loss > 0 else 0
    if rr < cfg["min_reward_risk"]:
        why.append("reward:risk %.2f after charges < min_reward_risk %.2f" % (rr, cfg["min_reward_risk"]))
    realised = sum(x.get("pnl") or 0 for x in trades if x["status"] == "CLOSED")
    open_risk = sum(x.get("risk_amount") or 0 for x in open_)
    if -min(realised, 0) + open_risk + loss > cfg["max_daily_loss"] + 0.01:
        why.append("daily loss budget: lost Rs %.0f + open risk Rs %.0f + this Rs %.0f > max_daily_loss Rs %s"
                   % (-min(realised, 0), open_risk, loss, cfg["max_daily_loss"]))
    need = qty * p["entry"] / float(cfg["intraday_leverage"])
    if need > cash + 0.01:
        why.append("needs Rs %.0f margin, Rs %.0f available" % (need, cash))
    return why
