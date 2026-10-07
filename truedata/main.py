"""
Main Entry Point — Nifty 200 Scanner
Whenever LTP changes for any symbol → scanner triggers immediately for that symbol.
"""
import os
import time
import threading
import pandas as pd
from datetime import datetime
from pathlib import Path
from truedata_ws.websocket.TD import TD

from rule_parser import parse_rules
from indicators import compute_indicators, get_reference_values
from alerts import AlertManager
from nifty200 import NIFTY200_SYMBOLS
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)


# ─── Config ──────────────────────────────────────────────────────────────────

USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
PORT      = 8084
URL       = "push.truedata.in"

BULLISH_RULES_FILE  = "bullish.txt"
BEARISH_RULES_FILE  = "bearish.txt"
ALERT_THRESHOLD_PCT = 70    # % of rules that must pass to fire alert
COOLDOWN_SECONDS    = 300   # Don't re-alert same symbol within 5 mins
MIN_TICKS           = 3     # Min ticks before a symbol is evaluated
EVAL_DEBOUNCE_SECONDS = 5     # Max one evaluation per symbol per 5 seconds

# ─── Candle Store ────────────────────────────────────────────────────────────

TIMEFRAMES = {
    '5min':  '5min',
    '15min': '15min',
    '1hour': '1h',
    '4hour': '4h',
    '1day':  '1D',
    '1week': '1W',
}

class CandleStore:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.ticks: list[dict] = []
        self._lock  = threading.Lock()

    def add_tick(self, price, volume, timestamp, day_open, day_high, day_low):
        with self._lock:
            self.ticks.append({
                'timestamp': timestamp,
                'price':     price,
                'volume':    volume,
                'day_open':  day_open,
                'day_high':  day_high,
                'day_low':   day_low,
            })

    def tick_count(self):
        return len(self.ticks)

    def get_all_timeframes(self) -> dict:
        with self._lock:
            if not self.ticks:
                return {}
            df = pd.DataFrame(self.ticks).set_index('timestamp')
            df.index = pd.to_datetime(df.index)

        result = {}
        for label, tf_str in TIMEFRAMES.items():
            try:
                if label == '1day':
                    ohlcv = df.resample('1D').agg(
                        open=('day_open', 'first'),
                        high=('day_high', 'max'),
                        low=('day_low', 'min'),
                        close=('price', 'last'),
                        volume=('volume', 'last'),
                    ).dropna()
                else:
                    ohlcv = df['price'].resample(tf_str).ohlc()
                    ohlcv.columns = ['open', 'high', 'low', 'close']
                    vol = df['volume'].resample(tf_str)
                    ohlcv['volume'] = (vol.last() - vol.first()).clip(lower=0)
                    ohlcv.dropna(inplace=True)

                if len(ohlcv) >= 1:
                    result[label] = compute_indicators(ohlcv)
            except Exception:
                pass

        return result


# ─── Scanner ─────────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self):
        self.alert_mgr   = AlertManager(ALERT_THRESHOLD_PCT, COOLDOWN_SECONDS)
        self.stores: dict[str, CandleStore] = {}
        self.req_to_sym: dict[int, str] = {}
        self._last_eval: dict[str, float] = {}   # symbol -> last eval timestamp
        self._rules_lock = threading.Lock()
        self._bull_rules = []
        self._bear_rules = []
        self._reload_rules()

    def _reload_rules(self):
        """Re-parse rule files — picks up any edits without restart."""
        with self._rules_lock:
            self._bull_rules = parse_rules(BULLISH_RULES_FILE)
            self._bear_rules = parse_rules(BEARISH_RULES_FILE)

    def evaluate(self, symbol: str):
        """
        Called whenever LTP changes for a symbol.
        Builds indicators and checks all rules immediately.
        """
        store = self.stores.get(symbol)
        if not store or store.tick_count() < MIN_TICKS:
            return

        # Debounce — max one evaluation per symbol per EVAL_DEBOUNCE_SECONDS
        now = time.time()
        if now - self._last_eval.get(symbol, 0) < EVAL_DEBOUNCE_SECONDS:
            return
        self._last_eval[symbol] = now

        data = store.get_all_timeframes()
        if not data:
            return

        refs = get_reference_values(
            daily_df=data.get('1day'),
            weekly_df=data.get('1week'),
        )

        self._reload_rules()
        with self._rules_lock:
            bull = list(self._bull_rules)
            bear = list(self._bear_rules)

        self.alert_mgr.check_and_alert(symbol, bull, bear, data, refs)

    def start(self):
        print("Connecting to TrueData...")
        td = TD(USERNAME, PASSWORD, live_port=PORT, url=URL)
        time.sleep(2)

        symbols = NIFTY200_SYMBOLS
        print(f"Subscribing to {len(symbols)} symbols...")

        req_ids = []
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i+100]
            ids = td.start_live_data(chunk)
            if ids:
                req_ids.extend(ids)
            time.sleep(1)

        for req_id, symbol in zip(req_ids, symbols):
            self.stores[symbol]     = CandleStore(symbol)
            self.req_to_sym[req_id] = symbol

        print(f"Subscribed to {len(self.stores)} symbols.")
        print(f"Scanning on every LTP change. Threshold: {ALERT_THRESHOLD_PCT}%\n")

        last_ltp: dict[int, float] = {}

        while True:
            try:
                for req_id, symbol in self.req_to_sym.items():
                    try:
                        tick = td.live_data[req_id]
                        if not tick or not tick.ltp:
                            continue

                        ltp = float(tick.ltp)

                        # Only act if LTP actually changed
                        if last_ltp.get(req_id) == ltp:
                            continue

                        last_ltp[req_id] = ltp

                        # Store the tick
                        day_low = float(tick.day_low) if tick.day_low and tick.day_low > 0 else ltp
                        self.stores[symbol].add_tick(
                            price     = ltp,
                            volume    = float(tick.ttq or 0),
                            timestamp = datetime.now(),
                            day_open  = float(tick.day_open or ltp),
                            day_high  = float(tick.day_high or ltp),
                            day_low   = day_low,
                        )

                        # Trigger scanner immediately in background thread
                        threading.Thread(
                            target=self.evaluate,
                            args=(symbol,),
                            daemon=True
                        ).start()

                    except Exception:
                        pass

                time.sleep(0.1)  # 100ms poll — just to read live_data dict

            except KeyboardInterrupt:
                print("\nScanner stopped.")
                break



# ─── Run with API Server ─────────────────────────────────────────────────────

def run_with_api():
    """
    Starts both the scanner and the API server together.
    Run this instead of Scanner().start() if you want the strategy builder UI.
    """
    import uvicorn
    import api_server

    scanner = Scanner()

    # Give scanner reference to API server so /run endpoint can access live data
    api_server.scanner_ref = scanner

    # Start scanner in background thread
    scanner_thread = threading.Thread(target=scanner.start, daemon=True)
    scanner_thread.start()

    print("API server starting at http://localhost:8000")
    print("Strategy Builder UI: http://localhost:8000\n")

    # Run FastAPI in main thread
    uvicorn.run(api_server.app, host="0.0.0.0", port=8000)


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print("=" * 60)
    print("  Nifty 200 Strategy Scanner")
    print(f"  Trigger   : every LTP change")
    print(f"  Threshold : {ALERT_THRESHOLD_PCT}% rules must pass")
    print(f"  Cooldown  : {COOLDOWN_SECONDS}s between same-symbol alerts")
    print("=" * 60 + "\n")

    # Pass --api flag to also start the strategy builder API
    if "--api" in sys.argv:
        run_with_api()
    else:
        Scanner().start()
