import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from kiteconnect import KiteConnect, KiteTicker

from conditions import load_conditions_from_file
from notifier import notifier
from strategy_runtime import evaluate_side, live_event

# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)

KITE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = KITE_DIR / "config.json"
BULLISH_FILE = KITE_DIR / "bullish.txt"
BEARISH_FILE = KITE_DIR / "bearish.txt"
MARKET_TZ = ZoneInfo("Asia/Kolkata")


def market_datetime(value):
    """Store market wall time; convert aware timestamps before dropping tzinfo."""
    if value.tzinfo is not None:
        value = value.astimezone(MARKET_TZ)
    return value.replace(tzinfo=None)


def normalize_kite_tick(tick):
    """Recover the instant from KiteTicker's host-local naive datetimes.

    The SDK decodes Unix seconds using datetime.fromtimestamp(). Historical
    timestamps and internal market-wall-time values use a different contract;
    normalize only at the SDK boundary, without changing prices or buckets.
    """
    normalized = dict(tick)
    for key in ("exchange_timestamp", "last_trade_time"):
        value = normalized.get(key)
        if isinstance(value, datetime):
            normalized[key] = datetime.fromtimestamp(value.timestamp(), MARKET_TZ)
    return normalized


def regular_session_tick(value):
    """NSE equity normal-session window, excluding pre/post-market updates."""
    local = market_datetime(value)
    start = local.replace(hour=9, minute=15, second=0, microsecond=0)
    end = local.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= local < end

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
        self._lock = threading.RLock()

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
            to_date = market_datetime(datetime.now(MARKET_TZ))
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
            session_value = 0.0
            session_volume = 0.0
            session_date = None
            for r in records:
                dt = market_datetime(r['date'])
                
                h, l, c = r['high'], r['low'], r['close']
                if session_date != dt.date():
                    session_date = dt.date()
                    session_value = 0.0
                    session_volume = 0.0
                typical_price = (h + l + c) / 3.0
                candle_volume = float(r['volume'])
                session_value += typical_price * candle_volume
                session_volume += candle_volume
                # Kite historical candles do not expose exchange VWAP. This is
                # the standard volume-weighted reconstruction for chart history;
                # live strategy checks use Kite's exact average_price tick field.
                historical_vwap = session_value / session_volume if session_volume else typical_price
                
                candles_list.append({
                    'time': dt,
                    'open': r['open'],
                    'high': h,
                    'low': l,
                    'close': c,
                    'volume': r['volume'],
                    'cumulative_volume_start': 0,
                    'cumulative_volume_end': 0,
                    'vwap': historical_vwap
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
        with self._lock:
            return self._add_tick_unlocked(tick)

    @staticmethod
    def _is_valid_price(value):
        try:
            return math.isfinite(float(value)) and float(value) > 0
        except (TypeError, ValueError):
            return False

    def _add_tick_unlocked(self, tick):
        ltp = tick.get("last_price")
        if not self._is_valid_price(ltp):
            return set()
        tick_vwap = tick.get("average_price")
        tick_time = tick.get("exchange_timestamp") or tick.get("timestamp") or datetime.now(MARKET_TZ)
        volume = tick.get("volume", 0)

        # Strip timezone if present
        tick_time = market_datetime(tick_time)
        if not regular_session_tick(tick_time):
            return set()
        previous_tick = getattr(self, '_latest_exchange_time', None)
        if previous_tick is not None and tick_time < previous_tick:
            return set()
        self._latest_exchange_time = tick_time

        rolled_intervals = set()
        for iv in self.intervals:
            bucket_time = self._get_bucket_time(tick_time, iv)
            if self.last_tick_time.get(iv) is not None and bucket_time < self.last_tick_time[iv]:
                continue
            previous_candle = self.candles[iv][-1] if self.candles[iv] else None
            same_session = bool(
                previous_candle
                and pd.Timestamp(previous_candle['time']).date() == tick_time.date()
            )
            previous_vwap = previous_candle.get('vwap') if same_session else None
            if self._is_valid_price(tick_vwap):
                vwap = float(tick_vwap)
            elif self._is_valid_price(previous_vwap):
                vwap = float(previous_vwap)
            else:
                # Never inject zero into the shared price scale or strategy.
                # Before Kite publishes average_price, LTP is the safest finite
                # fallback for a newly observed instrument.
                vwap = float(ltp)
            
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
                rolled_intervals.add(iv)
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
        return rolled_intervals

    def get_latest_vwap(self, interval='5m', fallback=None):
        with self._lock:
            candles = self.candles.get(interval, [])
            value = candles[-1].get('vwap') if candles else None
            if self._is_valid_price(value):
                return float(value)
            return float(fallback) if self._is_valid_price(fallback) else None

    def get_dataframe(self, iv):
        with self._lock:
            if iv == '4h':
                # Resample 4-hour candles from 1-hour candles in real-time
                df_1h = self.get_dataframe('1h')
                return resample_candles(df_1h, '4h')

            if iv not in self.candles or not self.candles[iv]:
                return pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume', 'vwap'])
            return pd.DataFrame(list(self.candles[iv]))

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
            # NSE hourly candles are anchored to the 09:15 session open.
            session_open = dt.replace(hour=9, minute=15, second=0, microsecond=0)
            elapsed_minutes = max(0, int((dt - session_open).total_seconds() // 60))
            return session_open + timedelta(minutes=(elapsed_minutes // 60) * 60)
        elif interval == '1d':
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return dt

class RealtimeScanner:
    def __init__(self, event_callback=None, status_callback=None):
        # Load environment variables
        load_dotenv(ENV_FILE, override=True)
        self.alert_threshold = float(os.getenv("ALERT_THRESHOLD", "100.0"))
        print(f"[SYSTEM] Alert threshold set to {self.alert_threshold}%")
        
        self.config = {}
        self.kite = None
        self.kws = None
        self._stop_requested = threading.Event()
        self._ticker_closed = threading.Event()
        
        self.bullish_conditions = []
        self.bearish_conditions = []
        self.bullish_mtime = 0
        self.bearish_mtime = 0
        
        self.last_reload_check = 0
        self.reload_cooldown = 5  # reload config/conditions files every 5s if modified
        
        self.token_to_symbol = {}
        self.symbol_data = {}  # token -> SymbolData
        self.event_callback = event_callback
        self.status_callback = status_callback
        self.latest_results = {}
        self.confirmed_signals = {}
        self._process_lock = threading.RLock()
        self._finalized_candles = {}
        self.connection_status = "initializing"
        self.connection_error = None
        
        self.load_config()
        self.load_conditions()

    def _set_connection_status(self, status, error=None):
        self.connection_status = status
        self.connection_error = str(error) if error else None
        if self.status_callback:
            self.status_callback(status, self.connection_error)

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            print(f"[ERROR] Configuration file '{CONFIG_FILE}' not found.")
            sys.exit(1)
        with open(CONFIG_FILE, "r") as f:
            self.config = json.load(f)
        # Preserve configured order while preventing duplicate subscriptions.
        self.config["symbols"] = list(dict.fromkeys(self.config.get("symbols", [])))

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
        with self._process_lock:
            return self._process_tick_locked(tick)

    def _process_tick_locked(self, tick):
        token = tick.get("instrument_token")
        if token not in self.symbol_data:
            return
        tick_time = market_datetime(tick.get('exchange_timestamp') or
                                    tick.get('timestamp') or datetime.now(MARKET_TZ))
        if not regular_session_tick(tick_time) or not SymbolData._is_valid_price(tick.get('last_price')):
            return
        latest = getattr(self.symbol_data[token], '_latest_exchange_time', None)
        if latest is not None and tick_time < latest:
            return

        # Check and reload files if they changed
        self.check_reloads()

        sd = self.symbol_data[token]
        
        # Ensure intervals required by currently active conditions are initialized
        required_intervals = self.get_required_intervals()
        sd.check_and_add_intervals(self.kite, required_intervals)
        
        # Add tick to compile OHLC candles
        rolled_intervals = sd.add_tick(tick)

        ltp = tick.get("last_price")
        if ltp is None:
            return None
        # In Kite, average_price is the running daily VWAP
        vwap = sd.get_latest_vwap('5m', ltp)

        bullish = evaluate_side("bullish", self.bullish_conditions, ltp, vwap, sd, self.alert_threshold)
        bearish = evaluate_side("bearish", self.bearish_conditions, ltp, vwap, sd, self.alert_threshold)
        event = live_event(sd, ltp, vwap, bullish, bearish)
        self.latest_results[token] = event

        # Freeze the last live state as the signal for the candle that just closed.
        confirmed_signal = None
        if "5m" in rolled_intervals and token in self.latest_results:
            prior = getattr(self, "_prior_results", {}).get(token)
            confirmed_signal = self._confirm_once(token, prior)
        event["confirmed_signal"] = confirmed_signal

        if not hasattr(self, "_prior_results"):
            self._prior_results = {}
        self._prior_results[token] = event

        for result, is_bullish, key in (
            (bullish, True, "bullish_block"),
            (bearish, False, "bearish_block"),
        ):
            if result.triggered:
                passed_text = "\n     • ".join(rule.text for rule in result.rules if rule.passed)
                condition_str = (
                    f"{result.percentage:.1f}% conditions passed "
                    f"({result.passed}/{result.total}):\n     • {passed_text}"
                )
                notifier.send_alert(sd.symbol, is_bullish=is_bullish,
                                    condition_str=condition_str, price=ltp,
                                    cooldown_key=key)

        if self.event_callback:
            self.event_callback(event)
        return event

    def _confirm_once(self, token, prior):
        if not prior or not prior.get('candle'):
            return None
        candle_time = prior['candle']['time']
        if candle_time <= self._finalized_candles.get(token, -1):
            return None
        self._finalized_candles[token] = candle_time
        # Retain neutral states too: absence of a signal is not missing data.
        # Store an immutable-in-practice shallow copy without nested signal history.
        signal = {key: value for key, value in prior.items() if key != 'confirmed_signal'}
        previous = self.confirmed_signals.setdefault(token, [])
        previous.append(signal)
        del previous[:-200]
        return signal

    def finalize_session(self, now=None):
        """Close the last observed candle at session end even if no new tick arrives."""
        now = market_datetime(now or datetime.now(MARKET_TZ))
        with self._process_lock:
            for token, prior in getattr(self, '_prior_results', {}).items():
                if not prior.get('candle'):
                    continue
                start = datetime.fromtimestamp(prior['candle']['time'], MARKET_TZ)
                end = market_datetime(start).replace(hour=15, minute=30, second=0, microsecond=0)
                if now < end:
                    continue
                signal = self._confirm_once(token, prior)
                if signal and self.event_callback:
                    self.event_callback({**prior, 'confirmed_signal': signal})

    def request_stop(self):
        """Prevent reconnects/startup and close Kite on its owning reactor thread."""
        self._stop_requested.set()
        ticker = self.kws
        if ticker is None:
            self._ticker_closed.set()
            return self._ticker_closed
        from twisted.internet import reactor
        def close_ticker():
            connected = ticker.is_connected()
            ticker.close(code=1000, reason="Application shutdown")
            # Cancel pending connector work when the SDK exposes its connector.
            if not connected:
                connector = getattr(getattr(ticker, 'factory', None), 'connector', None)
                if connector:
                    connector.disconnect()
                self._ticker_closed.set()
        if reactor.running:
            reactor.callFromThread(close_ticker)
        else:
            close_ticker()
        return self._ticker_closed

    def run(self, threaded=False):
        if self._stop_requested.is_set():
            return
        # 1. Resolve Instrument Tokens
        self.resolve_tokens()

        # 2. Pre-fetch historical candles for symbols for required intervals
        required_intervals = self.get_required_intervals()
        for token, sd in self.symbol_data.items():
            if self._stop_requested.is_set():
                return
            sd.check_and_add_intervals(self.kite, required_intervals)

        # 3. Setup WebSocket connection
        api_key = os.getenv("KITE_API_KEY")
        access_token = os.getenv("KITE_ACCESS_TOKEN")
        
        if self._stop_requested.is_set():
            return
        self._ticker_closed.clear()
        self.kws = KiteTicker(api_key, access_token)
        tokens = list(self.token_to_symbol.keys())
        self._set_connection_status("connecting")

        def on_ticks(ws, ticks):
            if self._stop_requested.is_set():
                return
            for tick in ticks:
                self.process_tick(normalize_kite_tick(tick))

        def on_connect(ws, response):
            self._ticker_closed.clear()
            if self._stop_requested.is_set():
                ws.close(code=1000, reason="Application shutdown")
                return
            self._set_connection_status("connected")
            print(f"[WEBSOCKET] Connected! Subscribing to: {list(self.token_to_symbol.values())}")
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        def on_close(ws, code, reason):
            self._ticker_closed.set()
            if self._stop_requested.is_set():
                return
            self._set_connection_status("disconnected", reason)
            print(f"[WEBSOCKET] Connection closed: Code {code} | Reason: {reason}")

        def on_error(ws, code, reason):
            if self._stop_requested.is_set():
                return
            self._set_connection_status("error", reason)
            print(f"[WEBSOCKET ERROR] Code {code} | Reason: {reason}")
            if "403" in str(reason):
                print("[AUTH ERROR] Kite rejected the WebSocket credentials. Run auth.py to refresh KITE_ACCESS_TOKEN.")

        def on_reconnect(ws, attempt_count):
            if self._stop_requested.is_set():
                ws.stop_retry()
                return
            self._set_connection_status("reconnecting")
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
        
        # Dashboard mode already runs the scanner outside the main interpreter
        # thread. KiteTicker/Twisted must then disable OS signal handlers by
        # using its threaded connection mode.
        self.kws.connect(threaded=threaded)
        if self._stop_requested.is_set():
            self.request_stop()

if __name__ == "__main__":
    scanner = RealtimeScanner()
    try:
        scanner.run()
    except KeyboardInterrupt:
        print("\n[SYSTEM] Exiting scanner.")
        sys.exit(0)
