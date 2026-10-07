from __future__ import annotations

import sys
from pathlib import Path

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException
from requests import RequestException


BASE_DIR = Path(__file__).resolve().parent

# Support both `python -m mtf_alert.symbol_check` from the repository root and
# `python symbol_check.py` from inside mtf_alert.
if __package__ in {None, ""}:
    sys.path.insert(0, str(BASE_DIR.parent))

from mtf_alert.config import Settings, require_credentials  # noqa: E402


def check_symbols() -> int:
    try:
        settings = Settings.load(BASE_DIR)
        api_key, access_token = require_credentials()
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        print("Connecting to Kite...")
        quotes = kite.quote(list(settings.symbols))

        missing = [symbol for symbol in settings.symbols if symbol not in quotes]
        if missing:
            raise RuntimeError("Kite returned no quote for: " + ", ".join(missing))

        print("\nConfigured contract quotes")
        print("=" * 72)
        for symbol in settings.symbols:
            quote = quotes[symbol]
            ohlc = quote.get("ohlc") or {}
            depth = quote.get("depth") or {}
            buys = depth.get("buy") or []
            sells = depth.get("sell") or []
            best_bid = buys[0].get("price") if buys else None
            best_offer = sells[0].get("price") if sells else None

            print(f"\nSymbol          : {symbol}")
            print(f"Last price      : {quote.get('last_price')}")
            print(f"Day open        : {ohlc.get('open')}")
            print(f"Day high        : {ohlc.get('high')}")
            print(f"Day low         : {ohlc.get('low')}")
            print(f"Last trade time : {quote.get('last_trade_time')}")
            print(f"Exchange time   : {quote.get('timestamp')}")
            print(f"Volume          : {quote.get('volume')}")
            print(f"Best bid/offer  : {best_bid} / {best_offer}")
        print("\n" + "=" * 72)
        return 0
    except (FileNotFoundError, ValueError, KiteException, RequestException, RuntimeError) as exc:
        print(f"Symbol check failed: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(check_symbols())


if __name__ == "__main__":
    main()
