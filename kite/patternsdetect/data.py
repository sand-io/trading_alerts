from kiteconnect import KiteConnect
import os
import pandas as pd
from pathlib import Path
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
import datetime
# pyrefly: ignore [missing-import]
import talib

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ENV_FILE)
import sys


from patterns import _find_peaks, _find_troughs, detect_double_top, detect_double_bottom, detect_head_shoulders, find_pivot_highs


ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN")
API_KEY = os.getenv("KITE_API_KEY")

if not API_KEY:
    raise ValueError("KITE_API_KEY not found in environment variables. Please set it in your .env file.")


kite = KiteConnect(api_key=API_KEY)

try:
    kite.set_access_token(ACCESS_TOKEN)
except Exception as e:
    print(f"Authentication failed: {e}")
    exit()


# user = kite.profile()
# print(user["user_name"]) 

# Fetch LTP for RELIANCE for testing

def verify_kite_connection(symbol: str = "NSE:RELIANCE"):
    try:    
        ltp_data = kite.ltp(symbol)
        last_price = ltp_data[symbol]['last_price']
        print(f"LTP for {symbol}: {last_price}")
        return True
    except Exception as e:
        print(f"Failed to fetch LTP for {symbol}: {e}")
        return False
    

def get_historical_data(kite, instrument_token: str, from_date: str, to_date: str, interval: str = "15minute"):
    """
    Fetch historical data for a given instrument token and date range.
    """
    try:
        data = kite.historical_data(instrument_token, from_date, to_date, interval)
        df = pd.DataFrame(data)
        return df
    except Exception as e:
        print(f"Error fetching historical data: {e}")
        return pd.DataFrame()  # Return an empty DataFrame on error
    
    
def get_instrument_token(symbol: str) -> str:
    """
    Fetch the instrument token for a given symbol.
    """
    try:
        instruments = kite.instruments("NSE")
        for instrument in instruments:
            if instrument['tradingsymbol'] == symbol:
                return instrument['instrument_token']
        raise ValueError(f"Instrument token for {symbol} not found.")
    except Exception as e:
        print(f"Error fetching instrument token: {e}")
        # pyrefly: ignore [bad-return]
        return None

def get_candlestick_data(symbol: str, days=90, interval: str = "15minute") -> pd.DataFrame:
    """
    Fetch candlestick data for a given symbol and date range.
    """
    token = get_instrument_token(symbol)
        

    # to_date = datetime.datetime.now().strftime("%Y-%m-%d")
    to_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")  # 180 days back
    from_date = (datetime.datetime.now() - datetime.timedelta(days=180)).strftime("%Y-%m-%d")

    df = get_historical_data(kite, token, "2025-12-01", "2026-06-30", interval)
    return pd.DataFrame(df)



if __name__ == "__main__":
    # verify_kite_connection()
    instrument_token = get_instrument_token("TOLINS")
    print(f"Instrument Token for TOLINS: {instrument_token}")
    # df = get_historical_data(kite, instrument_token, "2025-08-01", "2025-12-31")
    # print(df.head())
    df = get_candlestick_data("SBIN", days=90, interval="day")
    print(f"Got {len(df)} rows of candlestick data for TOLINS.")


    result_double_top = detect_double_top(df)
    print(f"Double Top Detection Result: {result_double_top}")

    # peaks = find_pivot_highs(df)
    # print("Detected trough points:")
    # for i, peak in enumerate(peaks):
    #     print(f"Trough {i}: Index {peak}, Date {df.iloc[peak]['date'].isoformat()}, high {df.iloc[peak]['high']}")
