"""Logging with the app's own clock (IST, or the replay clock) and a yes/no prompt with a timeout."""

import logging
import queue
import sys
import threading


class _ClockFormatter(logging.Formatter):
    def __init__(self, clock, fmt):
        logging.Formatter.__init__(self, fmt)
        self.clock = clock

    def formatTime(self, record, datefmt=None):
        return self.clock.now().strftime("%H:%M:%S")


def setup_log(clock, path=None, verbose=False):
    log = logging.getLogger("intraday")
    log.handlers[:] = []
    log.setLevel(logging.DEBUG)
    log.propagate = False
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(_ClockFormatter(clock, "%(asctime)s  %(message)s"))
    log.addHandler(sh)
    if path:
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_ClockFormatter(clock, "%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(fh)
    return log


class Prompt:
    """ask() waits for one line from the keyboard, at most `timeout` seconds."""

    def __init__(self):
        self.q = queue.Queue()
        self.t = None

    def _reader(self):
        for line in sys.stdin:
            self.q.put(line)

    def ask(self, text, timeout):
        if self.t is None:
            self.t = threading.Thread(target=self._reader, daemon=True)
            self.t.start()
        while not self.q.empty():
            self.q.get_nowait()   # drop anything typed earlier
        print(text, end="", flush=True)
        try:
            return self.q.get(timeout=timeout).strip()
        except queue.Empty:
            print("")
            return None
