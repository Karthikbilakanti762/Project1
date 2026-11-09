"""Small HTTP helpers (urllib only) shared by the data feed and the brokers."""

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class HttpError(Exception):
    def __init__(self, msg, status=None, body=""):
        Exception.__init__(self, msg)
        self.status = status
        self.body = body


def request(method, url, headers=None, params=None, data=None, json_body=None, timeout=20, raw=False):
    """-> parsed JSON (or text / bytes with raw=True). Raises HttpError with the body on 4xx / 5xx."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        h["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            if r.headers.get("Content-Encoding") == "gzip" or payload[:2] == b"\x1f\x8b":
                payload = gzip.decompress(payload)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read()
            if txt[:2] == b"\x1f\x8b":
                txt = gzip.decompress(txt)
            txt = txt.decode("utf-8", "replace")
        except Exception:
            txt = ""
        raise HttpError("HTTP %d from %s: %s" % (e.code, url.split("?")[0], txt[:300]), e.code, txt)
    except (urllib.error.URLError, OSError) as e:
        raise HttpError("could not reach %s: %s" % (url.split("?")[0], getattr(e, "reason", e)))
    if raw:
        return payload
    text = payload.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


class RateLimiter:
    """At most `per_sec` calls a second (thread-safe enough for our pool sizes)."""

    def __init__(self, per_sec):
        import threading
        self.gap = 1.0 / per_sec if per_sec else 0
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        if not self.gap:
            return
        with self.lock:
            now = time.time()
            delay = self.last + self.gap - now
            if delay > 0:
                time.sleep(delay)
            self.last = time.time()
