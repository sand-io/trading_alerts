"""Resolve the reference underlyings to Zerodha's live near-month futures."""

import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect


KITE_DIR = Path(__file__).resolve().parent
ALIASES = {"GMRINFRA": "GMRAIRPORT", "LTIM": "LTM", "TATAMOTORS": "TMPV"}


def resolve_current_futures(instruments, underlyings, today=None):
    today = today or date.today()
    futures = [row for row in instruments
               if row.get("instrument_type") == "FUT" and row.get("expiry") >= today]
    expiries = sorted({row["expiry"] for row in futures})
    if not expiries:
        raise RuntimeError("Zerodha returned no unexpired NFO futures")
    near_expiry = expiries[0]
    by_name = {row["name"]: row for row in futures if row["expiry"] == near_expiry}
    matched, missing = [], []
    for original in underlyings:
        name = ALIASES.get(original, original)
        row = by_name.get(name)
        if row:
            matched.append(f"NFO:{row['tradingsymbol']}")
        else:
            missing.append(original)
    return near_expiry, matched, missing


def main():
    load_dotenv(KITE_DIR.parent / ".env", override=True)
    kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    kite.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
    underlyings = json.loads((KITE_DIR / "nifty200_futures_underlyings.json").read_text())
    expiry, symbols, missing = resolve_current_futures(kite.instruments("NFO"), underlyings)
    (KITE_DIR / "config.json").write_text(
        json.dumps({"symbols": symbols}, indent=2) + "\n")
    print(f"Updated config.json with {len(symbols)} contracts expiring {expiry}")
    if missing:
        print(f"No exact Zerodha near-month future for {len(missing)} PDF entries:")
        print(", ".join(missing))


if __name__ == "__main__":
    main()
