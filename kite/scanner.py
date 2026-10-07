import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from kiteconnect import KiteConnect, KiteTicker

from conditions import load_conditions_from_file
from notifier import notifier

# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)

CONFIG_FILE = "config.json"
BULLISH_FILE = "bullish.txt"
BEARISH_FILE = "bearish.txt"

def resample_candles(df_1h, target_interval='4h'):
    """
    Kite Connect Historical API does not natively support 4-hour candles.
    This helper aggregates 1-hour candles into 4-hour blocks on the fly.
    """
    if df_1h.empty:
        return df_1h
    
    df = df_1h.copy()
    df['time'] = pd.to_datetime(df['time'])
    df = df.set_index('time')
    
    # Resample candles in 4-hour intervals starting from the market open (9:15 AM)
    resampled = df.resample('4h', closed='left', label='left', origin='9:15').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
        'vwap': 'mean'
    }).dropna().reset_index()
    
    return resampled

class SymbolData:
    def __init__(self, instrument_token, symbol):
        self.instrument_token = instrument_token
        self.symbol = symbol
        self.intervals = set()  # set of active intervals (e.g. {'5m', '15m'})
        self.candles = {}       # interval -> list of candle dicts
        self.last_tick_time = {} # interval -> last bucket datetime

    def check_and_add_intervals(self, kite, required_intervals):
        """
        Dynamically initializes candle tracking for new intervals on-the-fly.
        """
        # If 4h is in required, make sure 1h is also added so we can compile 4h candles
        resolved_intervals = set(required_intervals)
        if '4h' in resolved_intervals:
            resolved_intervals.add('1h')

        for iv in resolved_intervals:
            if iv in ['tick', '4h']:
                continue
            if iv not in self.intervals:
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Initializing candle tracking for {self.symbol} on {iv} interval...")
                self.candles[iv] = []
                self.last_tick_time[iv] = None
                self.intervals.add(iv)
                self.initialize_historical_candles(kite, iv)

    def initialize_historical_candles(self, kite, iv):
        # Map our interval strings to Kite's historical interval strings
        interval_map = {
            '1m': 'minute',
            '3m': '3minute',
            '5m': '5minute',
            '15m': '15minute',
            '30m': '30minute',
            '1h': '60minute',
            '1d': 'day'
        }
        
        kite_interval = interval_map.get(iv)
        if not kite_interval or not kite:
            return
            
        try:
            to_date = datetime.now()
            # Fetch enough historical candles to compute standard technical indicators
            if iv == '1d':
                from_date = to_date - timedelta(days=150)
            elif iv in ['1h', '30m']:
                # EMA(50) needs at least 50 trading candles; use enough calendar
                # history to comfortably cover weekends and exchange holidays.
                from_date = to_date - timedelta(days=30)
            else:
                from_date = to_date - timedelta(days=4)
                
            if os.getenv("VERBOSE", "false").lower() == "true":
                print(f"[HISTORICAL] Fetching {iv} data for {self.symbol}...")
            records = kite.historical_data(self.instrument_token, from_date, to_date, kite_interval)
            
            candles_list = []
            for r in records:
                dt = r['date']
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                
                h, l, c = r['high'], r['low'], r['close']
                approx_vwap = (h + l + c) / 3.0
                
                candles_list.append({
                    'time': dt,
                    'open': r['open'],
                    'high': h,
                    'low': l,
                    'close': c,
                    'volume': r['volume'],
                    'cumulative_volume_start': 0,
                    'cumulative_volume_end': 0,
                    'vwap': approx_vwap
                })
            
            self.candles[iv] = candles_list
            if candles_list:
                self.last_tick_time[iv] = candles_list[-1]['time']
            if os.getenv("VERBOSE", "false").lower() == "true":
                print(f"[HISTORICAL] Successfully loaded {len(candles_list)} historical candles for {self.symbol} ({iv})")
        except Exception as e:
            if os.getenv("VERBOSE", "false").lower() == "true":
                print(f"[HISTORICAL WARNING] Could not fetch history for {self.symbol} ({iv}): {e}")
                print(f"                     Will build candles in real-time starting from first tick.")

    def add_tick(self, tick):
        ltp = tick.get("last_price")
        vwap = tick.get("average_price")
        tick_time = tick.get("timestamp") or datetime.now()
        volume = tick.get("volume", 0)

        # Strip timezone if present
        if tick_time.tzinfo:
            tick_time = tick_time.replace(tzinfo=None)

        for iv in self.intervals:
            bucket_time = self._get_bucket_time(tick_time, iv)
            
            # Check if we should start a new candle or update current one
            if not self.candles[iv]:
                # First candle
                new_candle = {
                    'time': bucket_time,
                    'open': ltp,
                    'high': ltp,
                    'low': ltp,
                    'close': ltp,
                    'volume': 0,
                    'cumulative_volume_start': volume,
                    'cumulative_volume_end': volume,
                    'vwap': vwap
                }
                self.candles[iv].append(new_candle)
                self.last_tick_time[iv] = bucket_time
            elif bucket_time > self.last_tick_time[iv]:
                # Close the current candle and open a new one
                start_volume = self.candles[iv][-1]['cumulative_volume_end']
                
                new_candle = {
                    'time': bucket_time,
                    'open': ltp,
                    'high': ltp,
                    'low': ltp,
                    'close': ltp,
                    'volume': max(0, volume - start_volume),
                    'cumulative_volume_start': start_volume,
                    'cumulative_volume_end': volume,
                    'vwap': vwap
                }
                self.candles[iv].append(new_candle)
                self.last_tick_time[iv] = bucket_time
                
                # Sane buffer size limit
                if len(self.candles[iv]) > 500:
                    self.candles[iv].pop(0)
            else:
                # Update current candle
                curr_candle = self.candles[iv][-1]
                curr_candle['close'] = ltp
                if ltp > curr_candle['high']:
                    curr_candle['high'] = ltp
                if ltp < curr_candle['low']:
                    curr_candle['low'] = ltp
                
                start_vol = curr_candle.get('cumulative_volume_start', volume)
                curr_candle['volume'] = max(0, volume - start_vol)
                curr_candle['cumulative_volume_end'] = volume
                curr_candle['vwap'] = vwap

    def get_dataframe(self, iv):
        if iv == '4h':
            # Resample 4-hour candles from 1-hour candles in real-time
            df_1h = self.get_dataframe('1h')
            return resample_candles(df_1h, '4h')

        if iv not in self.candles or not self.candles[iv]:
            return pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume', 'vwap'])
        return pd.DataFrame(self.candles[iv])

    def _get_bucket_time(self, dt, interval):
        if interval == '1m':
            return dt.replace(second=0, microsecond=0)
        elif interval == '3m':
            minute = (dt.minute // 3) * 3
            return dt.replace(minute=minute, second=0, microsecond=0)
        elif interval == '5m':
            minute = (dt.minute // 5) * 5
            return dt.replace(minute=minute, second=0, microsecond=0)
        elif interval == '15m':
            minute = (dt.minute // 15) * 15
            return dt.replace(minute=minute, second=0, microsecond=0)
        elif interval == '30m':
            minute = (dt.minute // 30) * 30
            return dt.replace(minute=minute, second=0, microsecond=0)
        elif interval == '1h':
            return dt.replace(minute=0, second=0, microsecond=0)
        elif interval == '1d':
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return dt

class RealtimeScanner:
    def __init__(self):
        # Load environment variables
        load_dotenv(ENV_FILE, override=True)
        self.alert_threshold = float(os.getenv("ALERT_THRESHOLD", "100.0"))
        print(f"[SYSTEM] Alert threshold set to {self.alert_threshold}%")
        
        self.config = {}
        self.kite = None
        self.kws = None
        
        self.bullish_conditions = []
        self.bearish_conditions = []
        self.bullish_mtime = 0
        self.bearish_mtime = 0
        
        self.last_reload_check = 0
        self.reload_cooldown = 5  # reload config/conditions files every 5s if modified
        
        self.token_to_symbol = {}
        self.symbol_data = {}  # token -> SymbolData
        
        self.load_config()
        self.load_conditions()

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            print(f"[ERROR] Configuration file '{CONFIG_FILE}' not found.")
            sys.exit(1)
        with open(CONFIG_FILE, "r") as f:
            self.config = json.load(f)

    def load_conditions(self):
        # Bullish
        if os.path.exists(BULLISH_FILE):
            mtime = os.path.getmtime(BULLISH_FILE)
            if mtime != self.bullish_mtime:
                self.bullish_mtime = mtime
                self.bullish_conditions = load_conditions_from_file(BULLISH_FILE)
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Loaded {len(self.bullish_conditions)} bullish conditions.")
        
        # Bearish
        if os.path.exists(BEARISH_FILE):
            mtime = os.path.getmtime(BEARISH_FILE)
            if mtime != self.bearish_mtime:
                self.bearish_mtime = mtime
                self.bearish_conditions = load_conditions_from_file(BEARISH_FILE)
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Loaded {len(self.bearish_conditions)} bearish conditions.")

    def check_reloads(self):
        now = time.time()
        if now - self.last_reload_check > self.reload_cooldown:
            self.last_reload_check = now
            
            # Reload environment variables to check for threshold updates
            load_dotenv(ENV_FILE, override=True)
            new_threshold = float(os.getenv("ALERT_THRESHOLD", "100.0"))
            if new_threshold != self.alert_threshold:
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Alert threshold updated from {self.alert_threshold}% to {new_threshold}%")
                self.alert_threshold = new_threshold
            
            self.load_conditions()
            
            # Dynamically update requirements for intervals across symbols
            required_intervals = self.get_required_intervals()
            for sd in self.symbol_data.values():
                sd.check_and_add_intervals(self.kite, required_intervals)

    def get_required_intervals(self):
        intervals = set()
        for cond in self.bullish_conditions + self.bearish_conditions:
            intervals.add(cond.interval)
            # If the condition references yesterday or daily closes, we require daily '1d' data
            if cond.rule_type in ['yesterday_compare', 'multi_day_close', 'opening_or_previous_day']:
                intervals.add('1d')
        return intervals

    def resolve_tokens(self):
        api_key = os.getenv("KITE_API_KEY")
        access_token = os.getenv("KITE_ACCESS_TOKEN")
        symbols = self.config.get("symbols", [])

        if not api_key or not access_token:
            print("[ERROR] Credentials missing in .env file. Please run auth.py first.")
            sys.exit(1)

        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

        # Retrieve instruments from exchange to resolve tokens
        exchanges = set(sym.split(":")[0] for sym in symbols if ":" in sym)
        if not exchanges:
            exchanges = {"NSE"} # default

        all_instruments = []
        for ex in exchanges:
            try:
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Fetching symbols metadata for exchange: {ex}...")
                insts = self.kite.instruments(ex)
                all_instruments.extend(insts)
            except Exception as e:
                print(f"[SYSTEM WARNING] Failed to fetch instruments for {ex}: {e}")

        if not all_instruments:
            try:
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print("[SYSTEM] Fetching all instruments from Kite Connect...")
                all_instruments = self.kite.instruments()
            except Exception as e:
                print(f"[SYSTEM ERROR] Could not fetch instruments list: {e}")
                sys.exit(1)

        # Build mapping (exchange:tradingsymbol) -> token
        mapping = {}
        for inst in all_instruments:
            ex = inst.get("exchange")
            sym = inst.get("tradingsymbol")
            token = inst.get("instrument_token")
            if ex and sym and token:
                mapping[f"{ex}:{sym}"] = token
                mapping[sym] = token

        # Resolve requested symbols
        for sym in symbols:
            token = mapping.get(sym)
            if not token:
                clean_sym = sym.split(":")[-1] if ":" in sym else sym
                token = mapping.get(clean_sym)

            if token:
                self.token_to_symbol[token] = sym
                self.symbol_data[token] = SymbolData(token, sym)
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Resolved {sym} -> Instrument Token: {token}")
            else:
                print(f"[SYSTEM WARNING] Could not resolve token for symbol: {sym}")

        if not self.token_to_symbol:
            print("[SYSTEM ERROR] No symbols could be resolved. Exiting.")
            sys.exit(1)

    def process_tick(self, tick):
        token = tick.get("instrument_token")
        if token not in self.symbol_data:
            return

        # Check and reload files if they changed
        self.check_reloads()

        sd = self.symbol_data[token]
        
        # Ensure intervals required by currently active conditions are initialized
        required_intervals = self.get_required_intervals()
        sd.check_and_add_intervals(self.kite, required_intervals)
        
        # Add tick to compile OHLC candles
        sd.add_tick(tick)

        ltp = tick.get("last_price")
        # In Kite, average_price is the running daily VWAP
        vwap = tick.get("average_price", 0.0) 

        # Evaluate Bullish conditions
        if self.bullish_conditions:
            passed_bullish = []
            for cond in self.bullish_conditions:
                try:
                    if cond.evaluate(ltp, vwap, sd):
                        passed_bullish.append(cond)
                except Exception:
                    pass
            
            total_bullish = len(self.bullish_conditions)
            pct_bullish = (len(passed_bullish) / total_bullish) * 100.0 if total_bullish > 0 else 0.0
            if pct_bullish >= self.alert_threshold:
                cond_list_str = "\n     • ".join([c.raw_text for c in passed_bullish])
                condition_str = f"{pct_bullish:.1f}% conditions passed ({len(passed_bullish)}/{total_bullish}):\n     • {cond_list_str}"
                notifier.send_alert(sd.symbol, is_bullish=True, condition_str=condition_str, price=ltp, cooldown_key="bullish_block")

        # Evaluate Bearish conditions
        if self.bearish_conditions:
            passed_bearish = []
            for cond in self.bearish_conditions:
                try:
                    if cond.evaluate(ltp, vwap, sd):
                        passed_bearish.append(cond)
                except Exception:
                    pass
            
            total_bearish = len(self.bearish_conditions)
            pct_bearish = (len(passed_bearish) / total_bearish) * 100.0 if total_bearish > 0 else 0.0
            if pct_bearish >= self.alert_threshold:
                cond_list_str = "\n     • ".join([c.raw_text for c in passed_bearish])
                condition_str = f"{pct_bearish:.1f}% conditions passed ({len(passed_bearish)}/{total_bearish}):\n     • {cond_list_str}"
                notifier.send_alert(sd.symbol, is_bullish=False, condition_str=condition_str, price=ltp, cooldown_key="bearish_block")

    def run(self):
        # 1. Resolve Instrument Tokens
        self.resolve_tokens()

        # 2. Pre-fetch historical candles for symbols for required intervals
        required_intervals = self.get_required_intervals()
        for token, sd in self.symbol_data.items():
            sd.check_and_add_intervals(self.kite, required_intervals)

        # 3. Setup WebSocket connection
        api_key = os.getenv("KITE_API_KEY")
        access_token = os.getenv("KITE_ACCESS_TOKEN")
        
        self.kws = KiteTicker(api_key, access_token)
        tokens = list(self.token_to_symbol.keys())

        def on_ticks(ws, ticks):
            for tick in ticks:
                self.process_tick(tick)

        def on_connect(ws, response):
            print(f"[WEBSOCKET] Connected! Subscribing to: {list(self.token_to_symbol.values())}")
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        def on_close(ws, code, reason):
            print(f"[WEBSOCKET] Connection closed: Code {code} | Reason: {reason}")

        def on_error(ws, code, reason):
            print(f"[WEBSOCKET ERROR] Code {code} | Reason: {reason}")

        def on_reconnect(ws, attempt_count):
            print(f"[WEBSOCKET] Reconnecting... Attempt #{attempt_count}")

        # Register callback handlers
        self.kws.on_ticks = on_ticks
        self.kws.on_connect = on_connect
        self.kws.on_close = on_close
        self.kws.on_error = on_error
        self.kws.on_reconnect = on_reconnect

        # Connect and keep main thread alive
        if os.getenv("VERBOSE", "false").lower() == "true":
            print("\n" + "=" * 80)
            print("STARTING WEBSOCKET SCANNER DAEMON")
            print("=" * 80)
            print("Active Symbols:", list(self.token_to_symbol.values()))
            print("Cooldown Period:", notifier.cooldown_seconds, "seconds")
            print("Bullish Conditions loaded:", len(self.bullish_conditions))
            print("Bearish Conditions loaded:", len(self.bearish_conditions))
            print("Press Ctrl+C to exit.\n")
        else:
            print(f"[SYSTEM] Starting scanner daemon for symbols: {list(self.token_to_symbol.values())}")
        
        self.kws.connect()

if __name__ == "__main__":
    scanner = RealtimeScanner()
    try:
        scanner.run()
    except KeyboardInterrupt:
        print("\n[SYSTEM] Exiting scanner.")
        sys.exit(0)
