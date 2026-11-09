# Day Trader (India): NSE / BSE intraday, one command, any broker

Scans the Nifty for opening-range breakouts, checks the news, replays the trade
3,000 times before risking a rupee, sizes it to your max loss (charges
included), checks it against your risk rules, places the order with a
protective stop at your broker, watches the target, and squares everything
off before the close. The whole day runs from one command.

Market data (candles, history, live prices) and orders all go through **your
broker's API**. Switch broker by changing four lines in `.env`. Paper trading
is on by default.

Plain Python 3.8+, standard library only: nothing to `pip install`. Works on
Windows, macOS and Linux.

## Setup (once)

1. Install Python 3.8 or newer from python.org.
2. Copy `.env.example` to `.env` and fill it in:

   ```
   BROKER=zerodha          # zerodha | upstox | fyers | dhan | angelone
   API_KEY=...
   API_SECRET=...
   ACCESS_TOKEN=...
   ```

   | BROKER | API_KEY | API_SECRET | ACCESS_TOKEN |
   | --- | --- | --- | --- |
   | zerodha | Kite Connect api_key | api_secret | daily: `run.py login` |
   | upstox | app API key | API secret | daily: `run.py login` (set `REDIRECT_URI`) |
   | fyers | App ID (`ABCD1234-100`) | app secret | daily: `run.py login` (set `REDIRECT_URI`) |
   | dhan | Client ID | (empty) | token from web.dhan.co |
   | angelone | SmartAPI key | your client code | daily: `run.py login` (PIN + TOTP) |

3. `config.json` is created from `config.example.json` the first time you run
   anything. Edit capital, max loss, universe and times there.

## Every trading day

macOS / Linux use `python3` (or `./run.sh`); Windows uses `python` (or double-click `run.bat`).

```bash
python3 run.py login     # brokers whose token expires daily (not Dhan)
python3 run.py check     # login OK? funds? prices? candles?
python3 run.py           # runs the whole day; leave it open until it prints the summary
```

Start it any time before 09:30 IST. It waits for the opening range, scans at
every 5-minute bar close, and stops on its own after square-off (15:10) or once
the entry window closes with nothing open.

| command | what it does |
| --- | --- |
| `run.py` | the whole day: scan, news, plan, time machine, size, risk, order + stop, monitor, square-off, report |
| `run.py check` | tests the credentials, funds, last prices and candles |
| `run.py login` | creates today's `ACCESS_TOKEN` and saves it to `.env` |
| `run.py replay 2026-11-06` | replays a past day bar by bar on paper, from the broker's history |
| `run.py scan` | one scan now: which charts closed beyond their opening range |
| `run.py status` | today's trades |
| `run.py flatten` | exits every open intraday position (asks YES) |

While it runs: create an empty file `trader/STOP` to block new entries, or
`trader/FLATTEN` to exit everything. Ctrl+C stops the app but leaves the stop
orders at the broker. Run it again and it resumes the day from `trader/state/`.

## How a trade happens

1. **Scanner**: the opening range is 09:15-09:30 (`or_minutes`). A trigger is
   the first completed 5-minute bar that *closes* above the range high (or below
   the low, with `allow_short`), on at least `min_volume_x` times the range's
   average volume. Only fresh triggers are taken; a breakout the app missed is
   not chased, and neither is one that already ran `max_chase_pct` past the breakout close.
2. **News**: recent headlines (Google News, India) are logged. A headline with a
   `block_keywords` word skips the trade. Brokers have no news API, so this is
   the one outside source; turn it off with `"news": {"enabled": false}`.
3. **Exit plan**: stop at the range midpoint (`or-mid`), the far side (`or-low`),
   `pct:X` or `atr:K`, rounded to the tick away from the entry. Target `2R` (or
   `pct:X`). Flat by 15:10 either way.
4. **Time machine**: 3,000 replays of the rest of the session built from the
   stock's own past bars, plus the same rule on every past session. Logged;
   `block_if_leans_stop` turns it into a filter.
5. **Budget**: shares = the smallest of max loss / risk per share, the position
   cap and your cash x leverage, then trimmed until the loss at the stop
   *including charges* (brokerage, STT, exchange, SEBI, stamp, GST) fits.
6. **Risk**: entry window, trading day, trades per day, open positions, one trade
   per symbol, blocked list, price band, reward:risk after charges, the daily
   loss budget (losses so far + open risk + this trade), margin, kill switch.
7. **Order**: in live mode you type `YES` in the terminal (60 s, then it skips).
   Entry is a marketable LIMIT (MIS). The moment it fills, an SL-M stop goes to
   the broker, so a crash never leaves a position unprotected.
8. **Monitor**: every 5 s: stop filled? target reached (cancel the stop, exit at
   market)? daily loss limit hit (exit all, stop for the day)?
9. **Square-off** at `flat_by`, then the day summary, `trader/reports/<date>.json`
   and a row per trade in `trader/journal/trades.csv`.

## Going live

Run paper for a while, compare `trader/journal/trades.csv` with what you
expected, then set `"paper_trading": false`. The app asks you to type `LIVE`
at start and `YES` for each entry (`confirm_live_orders`).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Offline, with a fake broker: the IST clock and NSE calendar, bar handling, the
breakout rule, stops and targets, charges, sizing, the paper broker, the time
machine and a full replayed day with one target and one stop.

## Files

```
run.py, run.sh, run.bat   the one entry point (+ launchers)
.env.example              credentials template  -> .env
config.example.json       settings template     -> config.json
intraday/                 the core: market (IST, holidays), data, strategy, timemachine,
                          news, engine, brokers/ (zerodha, upstox, fyers, dhan, angelone, paper)
trader-india/SKILL.md     Claude skill: lets Claude run and explain the app
tests/                    offline tests
trader/                   created at run time: logs, state, journal, reports, cache
_us_legacy/               the original US (NYSE / Alpaca / SEC) skills, untouched
```

To add a broker, write one file in `intraday/brokers/` with the methods listed
in `intraday/brokers/base.py` and add it to `ADAPTERS` in
`intraday/brokers/__init__.py`.

## Fine print

**Not financial advice.** Software that reads market data and does
arithmetic. SEBI's studies show most individual intraday traders lose money.

**Untested against live accounts.** The broker adapters follow each broker's
published API, but check every broker in paper mode with `run.py check` and
`run.py replay` before trusting it with real orders. APIs change.

**A stop is a plan, not a guarantee.** A gap, a circuit limit or a fast
market can fill past it. The max loss shown is at the stop price.

**Holidays.** The NSE 2026 list is built in (`intraday/market.py`). Add later
years or surprise closures with `extra_holidays` in `config.json`, and special
sessions with `extra_trading_days`.

**Brokers square off MIS positions themselves** around 15:20 with a fee; the
app's `flat_by` (15:10) is earlier on purpose.

**Taxes.** Intraday profit is speculative business income in India. Nothing
here is tax advice.

MIT. Original US skills by [Jake Schincariol](https://opusjake.ai).
