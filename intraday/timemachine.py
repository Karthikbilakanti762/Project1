"""Time Machine: replay a planned intraday trade before risking a rupee.

1. Past FULL sessions from the broker's history (every bar 09:15-15:30 present).
2. Pool: for each bar slot of the day, every past session's bar as ratios to
   the previous bar's close (open, high, low, close) / prev close.
3. One replay starts at the entry and walks to flat_by: bar i draws a slot
   j = i + randint(-2, 2) (kept inside the day), then a random past bar from
   that slot, scaled to the current price. The stop is checked before the
   target in every bar (a bar touching both counts as the stop).
4. History: the same opening-range rule re-run on every past session.

Verdict: target / (target + stop) among replays that hit a side, compared
with the share that breaks even at the plan's R: 1 / (1 + R).
A simulation built from recent bars is not a prediction.
"""

import datetime
import random

from . import market

WINDOW = 2
MIN_SESSIONS = 15


def full_sessions(bars, interval, before_day):
    n = int((market.at(before_day, market.CLOSE) - market.at(before_day, market.OPEN)).total_seconds()
            // (interval * 60))
    by_day = {}
    for b in bars:
        if b.start.date() < before_day:
            by_day.setdefault(b.start.date(), {})[b.start] = b
    out = []
    for d in sorted(by_day):
        day_bars = [by_day[d][k] for k in sorted(by_day[d])]
        start = market.at(d, market.OPEN)
        grid = [start + datetime.timedelta(minutes=interval * i) for i in range(n)]
        if len(day_bars) >= n and [b.start for b in day_bars[:n]] == grid:
            out.append(day_bars[:n])
    return out, n


def _pool(sessions, n):
    pool = [[] for _ in range(n)]
    for s in sessions:
        for j in range(1, n):
            pc = s[j - 1].c
            if pc:
                b = s[j]
                pool[j].append((b.o / pc, b.h / pc, b.l / pc, b.c / pc))
    return pool


def _walk(price, side, stop, target, bars_iter):
    """bars_iter yields (high, low, close) -> ('stop'|'target'|'neither', last price)."""
    for h, l, c in bars_iter:
        if side == "BUY":
            if l <= stop:
                return "stop", stop
            if h >= target:
                return "target", target
        else:
            if h >= stop:
                return "stop", stop
            if l <= target:
                return "target", target
        price = c
    return "neither", price


def run(plan, bars_history, day, interval, entry_slot, end_slot, n_range, replays=3000, seed=19):
    sessions, n = full_sessions(bars_history, interval, day)
    if len(sessions) < MIN_SESSIONS:
        return {"ok": False, "why": "only %d full past sessions (need %d)" % (len(sessions), MIN_SESSIONS)}
    end_slot = min(end_slot, n)
    pool = _pool(sessions, n)
    rnd = random.Random(seed)
    side, entry, stop, target = plan["side"], plan["entry"], plan["stop"], plan["target"]
    risk = plan["risk_ps"]
    counts = {"target": 0, "stop": 0, "neither": 0}
    rs = []

    for _ in range(replays):
        def gen(price=entry):
            for i in range(entry_slot, end_slot):
                j = min(n - 1, max(1, i + rnd.randint(-WINDOW, WINDOW)))
                o, h, l, c = rnd.choice(pool[j])
                yield price * h, price * l, price * c
                price = price * c
        out, last = _walk(entry, side, stop, target, gen())
        counts[out] += 1
        r = plan["r_multiple"] if out == "target" else -1.0 if out == "stop" else (
            (last - entry) / risk if side == "BUY" else (entry - last) / risk)
        rs.append(r)

    # history: the same rule on every past session
    hist = {"fired": 0, "target": 0, "stop": 0, "neither": 0}
    for s in sessions:
        rng = s[:n_range]
        oh, ol = max(b.h for b in rng), min(b.l for b in rng)
        for k in range(n_range, end_slot):
            b = s[k]
            fired = b.c > oh if side == "BUY" else b.c < ol
            if not fired:
                continue
            e = b.c
            if plan["stop_rule"] == "or-mid":
                st = (oh + ol) / 2.0
            elif plan["stop_rule"] == "or-low":
                st = ol if side == "BUY" else oh
            else:  # pct / atr: keep the plan's risk as a fraction of price
                f = risk / entry
                st = e * (1 - f) if side == "BUY" else e * (1 + f)
            rk = e - st if side == "BUY" else st - e
            if rk <= 0:
                break
            tg = e + plan["r_multiple"] * rk if side == "BUY" else e - plan["r_multiple"] * rk
            out, _ = _walk(e, side, st, tg, ((x.h, x.l, x.c) for x in s[k + 1:end_slot]))
            hist["fired"] += 1
            hist[out] += 1
            break

    sides = counts["target"] + counts["stop"]
    share = counts["target"] / float(sides) if sides else 0.0
    even = 1.0 / (1.0 + plan["r_multiple"])
    gap = (share - even) * 100
    if abs(gap) < 5:
        verdict = "close to break-even"
    elif gap > 0:
        verdict = "leans toward the target" if gap < 15 else "strongly toward the target"
    else:
        verdict = "leans toward the stop" if gap > -15 else "strongly toward the stop"
    pct = {k: round(100.0 * v / replays, 1) for k, v in counts.items()}
    return {"ok": True, "sessions": len(sessions), "replays": replays, "seed": seed, "pct": pct,
            "avg_r": round(sum(rs) / len(rs), 3), "target_share": round(share * 100, 1),
            "break_even_share": round(even * 100, 1), "verdict": verdict, "history": hist,
            "leans_stop": gap <= -5}


def summary(tm):
    if not tm.get("ok"):
        return "time machine skipped: " + tm.get("why", "")
    p, h = tm["pct"], tm["history"]
    return ("time machine (%d replays, %d past sessions): target first %.1f%%, stop first %.1f%%, open at "
            "flat-by %.1f%%, avg %.2fR -> %s. History: fired %d, target %d, stop %d, neither %d."
            % (tm["replays"], tm["sessions"], p["target"], p["stop"], p["neither"], tm["avg_r"], tm["verdict"],
               h["fired"], h["target"], h["stop"], h["neither"]))
