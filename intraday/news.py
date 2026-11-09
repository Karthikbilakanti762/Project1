"""Headlines for a stock before entry (brokers do not offer a news API).

Source: Google News RSS, India edition, searched for "<SYMBOL> share". The
headlines are shown in the log; a headline containing one of
news.block_keywords skips the trade. Set "news": {"enabled": false} in
config.json to turn this off entirely.
"""

import datetime
import email.utils
import urllib.parse
import xml.etree.ElementTree as ET

from . import market, net

URL = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"


def headlines(symbol, hours, now):
    q = urllib.parse.quote('"%s" share OR stock when:%dh' % (symbol, max(1, int(hours))))
    try:
        xml = net.request("GET", URL % q, timeout=10, raw=True)
        root = ET.fromstring(xml)
    except (net.HttpError, ET.ParseError):
        return None
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            dt = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            ist = (dt - dt.utcoffset()).replace(tzinfo=None) + market.IST_OFFSET
        except (TypeError, ValueError):
            ist = None
        if ist and now - ist > datetime.timedelta(hours=hours):
            continue
        out.append({"title": title, "at": ist.strftime("%d %b %H:%M") if ist else "", "source":
                    (it.findtext("source") or "").strip()})
    return out


def check(symbol, ncfg, now):
    """-> (block_reason or None, [lines to log])."""
    items = headlines(symbol, ncfg.get("hours", 18), now)
    if items is None:
        return None, ["news: could not reach Google News"]
    if not items:
        return None, ["news: no headlines in the last %sh" % ncfg.get("hours", 18)]
    lines = ["news: %s  %s" % (h["at"], h["title"]) for h in items[:ncfg.get("show", 3)]]
    words = [w.lower() for w in ncfg.get("block_keywords") or []]
    for h in items:
        t = h["title"].lower()
        hit = next((w for w in words if w in t), None)
        if hit:
            return 'headline mentions "%s": %s' % (hit, h["title"]), lines
    return None, lines
