import os
import urllib.parse as urlparse
from pathlib import Path
from kiteconnect import KiteConnect

from dotenv import load_dotenv, set_key
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)

def parse_request_token(input_str):
    # Try parsing as a URL
    parsed = urlparse.urlparse(input_str)
    queries = urlparse.parse_qs(parsed.query)
    if "request_token" in queries:
        return queries["request_token"][0]
    # Otherwise, assume the input itself is the request token
    return input_str.strip()

def authenticate():
    
    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")

    if not api_key or not api_secret or api_key.startswith("YOUR_") or api_secret.startswith("YOUR_"):
        print("\n[WARNING] Please fill in 'KITE_API_KEY' and 'KITE_API_SECRET' in your .env file first.")
        return

    # Initialize kite connect
    kite = KiteConnect(api_key=api_key)

    # Print login URL
    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    print("\n" + "=" * 80)
    print("KITE CONNECT AUTHENTICATION HELPER")
    print("=" * 80)
    print(f"1. Open the following URL in your web browser:\n\n   {login_url}\n")
    print("2. Log in with your Zerodha credentials.")
    print("3. After successful login, you will be redirected to your Redirect URL.")
    print("4. Copy the entire Redirect URL from your browser's address bar or extract the 'request_token'.\n")

    try:
        user_input = input("Paste the Redirect URL or request_token here: ")
    except KeyboardInterrupt:
        print("\nAuthentication cancelled.")
        return

    request_token = parse_request_token(user_input)
    if not request_token:
        print("Error: Could not extract request_token.")
        return

    print(f"Extracted request_token: {request_token}")
    print("Exchanging request token for access token...")

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]
        user_name = session.get("user_name", "User")
        
        print(f"\nSuccessfully authenticated as {user_name}!")
        print(f"Access Token: {access_token}")
        
        # Save to .env
        # set_key(DOTENV_FILE, "KITE_ACCESS_TOKEN", access_token)
        # print(f"Updated {DOTENV_FILE} with new access token.")
        
    except Exception as e:
        print(f"\nAuthentication failed: {e}")
        print("Please verify your API key, API secret, and ensure the request token is fresh.")

if __name__ == "__main__":
    authenticate()
