"""Broker plug-ins. Pick one with BROKER= in .env; nothing else in the app changes.

To add a broker: write a class with the methods listed in base.py, then add
it to ADAPTERS below.
"""

from .angelone import AngelOne
from .base import Broker, BrokerError, OrderStatus
from .dhan import Dhan
from .fyers import Fyers
from .paper import PaperBroker
from .upstox import Upstox
from .zerodha import Zerodha

ADAPTERS = {
    "zerodha": Zerodha,
    "upstox": Upstox,
    "fyers": Fyers,
    "dhan": Dhan,
    "angelone": AngelOne,
}

# What each of the three .env values means, per broker (shown by `run.py check`).
CREDENTIALS_HELP = {
    "zerodha": ("Kite Connect api_key", "Kite Connect api_secret", "daily access token (run.py login)"),
    "upstox": ("app API key", "app API secret", "daily access token (run.py login, needs REDIRECT_URI)"),
    "fyers": ("App ID, e.g. ABCD1234-100", "app secret", "daily access token (run.py login, needs REDIRECT_URI)"),
    "dhan": ("Dhan Client ID", "(not used)", "access token from web.dhan.co"),
    "angelone": ("SmartAPI key", "your client code", "session JWT (run.py login asks PIN + TOTP)"),
}


def make_broker(env, cfg=None, log=None):
    name = env["BROKER"]
    cls = ADAPTERS.get(name)
    if cls is None:
        raise BrokerError("unknown BROKER=%s (use one of %s)" % (name, ", ".join(ADAPTERS)))
    return cls(env.get("API_KEY"), env.get("API_SECRET"), env.get("ACCESS_TOKEN"), cfg=cfg, log=log,
               redirect_uri=env.get("REDIRECT_URI", ""))


__all__ = ["ADAPTERS", "Broker", "BrokerError", "CREDENTIALS_HELP", "OrderStatus", "PaperBroker", "make_broker"]
