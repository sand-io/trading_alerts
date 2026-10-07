from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv, set_key
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException
from requests import RequestException


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR.parent / ".env"


def parse_request_token(value: str) -> str:
    """Accept either Kite's redirect URL or the one-time request token."""
    cleaned = value.strip()
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    values = parse_qs(parsed.query).get("request_token")
    return values[0].strip() if values else cleaned


def authenticate() -> int:
    load_dotenv(ENV_FILE, override=True)
    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print(
            "Set KITE_API_KEY and KITE_API_SECRET in trading_alerts/.env before authentication.",
            file=sys.stderr,
        )
        return 1

    kite = KiteConnect(api_key=api_key)
    print("\nOpen this Zerodha login URL in your browser:\n")
    print(kite.login_url())
    print("\nAfter login, paste the complete redirect URL or request_token below.")
    try:
        request_token = parse_request_token(input("Redirect URL/request token: "))
    except (EOFError, KeyboardInterrupt):
        print("\nAuthentication cancelled.", file=sys.stderr)
        return 130
    if not request_token:
        print("No request token was provided.", file=sys.stderr)
        return 1

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = str(session["access_token"])
    except (KiteException, RequestException, KeyError, TypeError) as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 1

    set_key(str(ENV_FILE), "KITE_ACCESS_TOKEN", access_token, quote_mode="never")
    ENV_FILE.chmod(0o600)
    user = session.get("user_name") or session.get("user_id") or "Kite user"
    print(f"Authentication successful for {user}.")
    print("The access token was saved securely to trading_alerts/.env and was not displayed.")
    return 0


def main() -> None:
    raise SystemExit(authenticate())


if __name__ == "__main__":
    main()
