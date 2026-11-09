"""Which stocks to scan: a Nifty index list (downloaded once a day) or your own list.

The index lists come from NSE's public CSVs (niftyindices.com, then the NSE
archive). If both are unreachable, the built-in Nifty 50 below is used.
"""

import csv
import io
import os

from . import net, settings

INDEX_FILES = {
    "nifty50": "ind_nifty50list.csv",
    "nifty100": "ind_nifty100list.csv",
    "nifty200": "ind_nifty200list.csv",
    "nifty500": "ind_nifty500list.csv",
}
SOURCES = ("https://www.niftyindices.com/IndexConstituent/",
           "https://archives.nseindia.com/content/indices/")

# Fallback only. Index membership changes twice a year: the download is preferred.
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
    "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO",
    "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND",
    "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUM", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]


def parse_index_csv(text):
    rows = csv.DictReader(io.StringIO(text))
    out = []
    for r in rows:
        sym = (r.get("Symbol") or r.get("SYMBOL") or "").strip().upper()
        series = (r.get("Series") or "EQ").strip().upper()
        if sym and series == "EQ":
            out.append(sym)
    return out


def load(cfg, today, log=None):
    """-> list of symbols (upper case), blocked ones removed."""
    name = cfg["universe"]
    if name == "custom":
        syms = list(cfg["symbols"])
    else:
        syms = _index(name, today, log)
    blocked = set(cfg["blocked"])
    seen, out = set(), []
    for s in syms:
        if s not in blocked and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _index(name, today, log):
    cache = settings.data_path("cache", "%s-%s.txt" % (name, today.isoformat()))
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            syms = [ln.strip() for ln in fh if ln.strip()]
        if syms:
            return syms
    fname = INDEX_FILES[name]
    for base in SOURCES:
        try:
            text = net.request("GET", base + fname, timeout=20)
            syms = parse_index_csv(text if isinstance(text, str) else "")
            if len(syms) >= 40:
                with open(cache, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(syms) + "\n")
                return syms
        except net.HttpError as e:
            if log:
                log.debug("universe: %s", e)
    if log:
        log.warning("Could not download the %s list; using the built-in Nifty 50.", name)
    return list(NIFTY50)
