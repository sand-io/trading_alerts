"""Shared Kite login for both scanners; credentials stay in the root .env."""
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import load_dotenv, set_key
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

ENV_FILE = Path(__file__).resolve().parents[1] / '.env'
CALLBACK_URL = 'http://127.0.0.1:8765/callback'


def parse_request_token(value: str) -> str:
    cleaned = value.strip()
    values = parse_qs(urlparse(cleaned).query).get('request_token')
    return values[0].strip() if values else cleaned


def capture_request_token(kite, timeout=300):
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not log credential-bearing redirect URLs.

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != '/callback':
                code, message = 404, 'Unknown page.'
            elif query.get('state') != [state]:
                code, message = 400, 'Invalid login state. Start login from the terminal.'
            elif not query.get('request_token'):
                code, message = 400, 'Login did not return a request token. Try login again.'
            else:
                result['token'] = query['request_token'][0]
                code, message = 200, 'Login received. Return to the terminal while authentication completes. You can close this tab.'
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(message.encode())

    with HTTPServer(('127.0.0.1', 8765), Callback) as server:
        server.timeout = 1
        url = kite.login_url() + '&' + urlencode({'redirect_params': urlencode({'state': state})})
        print(f'Kite developer app Redirect URL must be: {CALLBACK_URL}')
        print('Complete Zerodha login and the app-code step in your browser.')
        print(f'If the browser does not open, open:\n{url}', flush=True)
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
        deadline = time.monotonic() + timeout
        while 'token' not in result and time.monotonic() < deadline:
            server.handle_request()
        if 'token' not in result:
            raise RuntimeError('Login timed out. Check the developer app Redirect URL and run again. For a different redirect URL use --manual-auth.')
    return result['token']


def ensure_session(*, manual=False, force=False):
    load_dotenv(ENV_FILE, override=True)
    api_key = os.getenv('KITE_API_KEY', '').strip()
    api_secret = os.getenv('KITE_API_SECRET', '').strip()
    if not api_key or not api_secret or any(v.startswith('YOUR_') for v in (api_key, api_secret)):
        raise RuntimeError('Set KITE_API_KEY and KITE_API_SECRET in trading_alerts/.env first.')
    kite = KiteConnect(api_key=api_key, timeout=15)
    token = os.getenv('KITE_ACCESS_TOKEN', '').strip()
    if token and not force:
        kite.set_access_token(token)
        try:
            kite.profile()
        except TokenException:
            print('Saved Kite session expired. Starting login.')
        else:
            print('Using the existing valid Kite session.')
            return token
    if manual:
        print(f'Open this Zerodha login URL:\n{kite.login_url()}')
        token = parse_request_token(input('Redirect URL/request token: '))
    else:
        token = capture_request_token(kite)
    if not token:
        raise RuntimeError('No request token was provided.')
    session = kite.generate_session(token, api_secret=api_secret)
    access_token = session['access_token']
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError('Kite returned an invalid access token.')
    set_key(str(ENV_FILE), 'KITE_ACCESS_TOKEN', access_token, quote_mode='never')
    ENV_FILE.chmod(0o600)
    os.environ['KITE_ACCESS_TOKEN'] = access_token
    print('Authentication successful. Shared access token saved to .env.')
    return access_token


def authenticate():
    """Keep old auth commands usable with their manual redirect workflow."""
    try:
        ensure_session(manual=True, force=True)
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\nAuthentication cancelled.', file=sys.stderr)
        return 130
    except Exception as exc:
        # Upstream exception messages can contain credentials.
        print(f'Authentication failed ({type(exc).__name__}). Check credentials and retry with a fresh login.', file=sys.stderr)
        return 1


def main():
    raise SystemExit(authenticate())
