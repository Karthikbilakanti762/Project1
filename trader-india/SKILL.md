---
name: trader-india
description: Run, check, replay or explain the NSE/BSE intraday trading app (opening-range breakouts, broker API data, paper by default). Use when the user wants to start the trading day, test their broker login, generate the daily access token, replay a past day, scan for breakouts, see today's trades, or exit all positions.
---

# Trader India: NSE / BSE intraday in one command

The app lives in the folder that contains this skill's parent: `run.py`, `intraday/`,
`.env`, `config.json`. Run every command from that folder. Use `python3` on
macOS/Linux and `python` on Windows.

| user wants | command |
| --- | --- |
| start today's session (scan -> news -> plan -> time machine -> size -> risk -> order + stop -> monitor -> square-off -> report) | `python3 run.py` |
| test broker keys, funds, prices, candles | `python3 run.py check` |
| make today's access token | `python3 run.py login` (interactive: the user pastes the redirect URL) |
| replay a past day on paper | `python3 run.py replay YYYY-MM-DD` |
| list breakouts right now | `python3 run.py scan` |
| today's trades | `python3 run.py status` |
| exit everything | `python3 run.py flatten` (asks YES), or create the file `trader/FLATTEN` |
| block new entries | create the file `trader/STOP` |

Rules for Claude:

- Never put API keys or tokens in the chat. They go in `.env`, which the user edits.
- `paper_trading` in `config.json` is `true` by default. Only the user switches it to
  `false`; never change it for them. In live mode every entry asks for YES in the
  terminal: that YES must come from the user, never from Claude.
- `run.py` (no arguments) runs until the square-off time (15:10 IST by default). Start it
  in the background and read `trader/logs/<date>.log` for progress.
- Switching broker = edit `BROKER`, `API_KEY`, `API_SECRET`, `ACCESS_TOKEN` in `.env`.
  Supported: zerodha, upstox, fyers, dhan, angelone. `python3 run.py check` shows what each
  value means for the chosen broker.
- Not financial advice. A stop is a plan, not a guarantee. Most intraday traders lose money.
