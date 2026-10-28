import json
import logging
import math
import os
import queue
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from kiteconnect import KiteConnect, KiteTicker

from notifier import notifier, format_alert
from history import HistoryStore
from historical import HistoricalRequests
from strategies import StrategyStore
from instrument_scope import applies, nearest_futures
from market_sessions import session_bounds, session_is_open
from nnf_strategy import event_details as nnf_event_details, history_days as nnf_history_days, MINUTES as NNF_MINUTES
from strategy_runtime import evaluate_side, live_event, resolve_direction, missing_rule_timeframes
from strategy_runtime import _candle_dict, SUPPORTED_TIMEFRAMES, BASE_TIMEFRAMES, chart_snapshot, profile_chart_snapshot

# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)

KITE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = KITE_DIR / "config.json"
MARKET_TZ = ZoneInfo("Asia/Kolkata")
strategy_log = logging.getLogger('kite.strategy')
if not strategy_log.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s IST [STRATEGY] %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S')
    formatter.converter = lambda *_: datetime.now(MARKET_TZ).timetuple()
    handler.setFormatter(formatter)
    strategy_log.addHandler(handler)
strategy_log.setLevel(logging.INFO)
strategy_log.propagate = False
history_requests = HistoricalRequests(log=strategy_log)


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


def regular_session_tick(value, exchange='NSE'):
    """Normal exchange session; preserve existing stock tick filtering."""
    local = market_datetime(value)
    start, end = session_bounds(local, exchange)
    return start <= local < end and (exchange != 'MCX' or local.weekday() < 5)

def resample_candles(df_1h, target_interval='4h', exchange='NSE'):
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
    resampled = df.resample('4h', closed='left', label='left', origin='start_day', offset=timedelta(hours=9, minutes=0 if exchange == 'MCX' else 15)).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
        'vwap': 'mean'
    }).dropna().reset_index()
    
    return resampled

class SymbolData:
    def __init__(self, instrument_token, symbol, instrument=None):
        self.instrument_token = instrument_token
        self.symbol = symbol
        self.instrument = dict(instrument or {})
        self.exchange = self.instrument.get('exchange') or (symbol.split(':')[0] if ':' in symbol else 'NSE')
        self.instrument_type = self.instrument.get('instrument_type', 'FUT')
        self.intervals = set()  # set of active intervals (e.g. {'5m', '15m'})
        self.candles = {}       # interval -> list of candle dicts
        self.nnf_candles = {}   # extended NNF lookback; never changes legacy inputs
        self.last_tick_time = {} # interval -> last bucket datetime
        self._lock = threading.RLock()
        self.history_errors = {}
        self.history_stop = None
        self.history_days = {}
        self.loaded_history_days = {}
        self._nnf_evaluation_cache = {}

    def check_and_add_intervals(self, kite, required_intervals, history_days=None):
        """
        Dynamically initializes candle tracking for new intervals on-the-fly.
        """
        with self._lock:
            upgrades = set()
            for iv, days in (history_days or {}).items():
                if days > self.loaded_history_days.get(iv, 0):
                    self.history_days[iv] = days
                    upgrades.add(iv)
            # If 4h is in required, make sure 1h is also added so we can compile 4h candles
            resolved_intervals = set(required_intervals)
            if '4h' in resolved_intervals:
                resolved_intervals.add('1h')

            for iv in resolved_intervals:
                if iv in ['tick', '4h']:
                    continue
                if iv not in self.intervals or iv in upgrades:
                    if os.getenv("VERBOSE", "false").lower() == "true":
                        print(f"[SYSTEM] Initializing candle tracking for {self.symbol} on {iv} interval...")
                    was_initialized = iv in self.intervals
                    existing = self.candles.get(iv, [])
                    self.candles.setdefault(iv, [])
                    self.last_tick_time.setdefault(iv, None)
                    self.intervals.add(iv)
                    self.initialize_historical_candles(kite, iv)
                    if self.candles[iv] and iv not in self.history_errors:
                        self.loaded_history_days[iv] = self.history_days.get(iv, 0)
                    self._nnf_evaluation_cache.clear()
                    if was_initialized:
                        # An NNF upgrade must preserve the existing strategies'
                        # exact input range and live candle objects.
                        self.candles[iv] = existing
                        if existing:
                            self.last_tick_time[iv] = existing[-1]['time']
                    if iv in self.nnf_candles:
                        merged = {row['time']: row for row in self.nnf_candles[iv]}
                        merged.update({row['time']: dict(row) for row in self.candles[iv]})
                        self.nnf_candles[iv] = [merged[stamp] for stamp in sorted(merged)]

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
            legacy_start = from_date
            if self.history_days.get(iv):
                from_date = min(from_date, to_date - timedelta(days=self.history_days[iv]))
                
            if os.getenv("VERBOSE", "false").lower() == "true":
                print(f"[HISTORICAL] Fetching {iv} data for {self.symbol}...")
            options = {'exchange': 'MCX'} if self.exchange == 'MCX' else {}
            records = history_requests.fetch(kite, self.instrument_token, from_date, to_date, kite_interval, stop=self.history_stop, **options)
            
            if from_date < legacy_start:
                self.nnf_candles[iv] = self._history_candles(records)
                records = [record for record in records if market_datetime(record['date']) >= legacy_start]
            candles_list = self._history_candles(records)
            
            self.candles[iv] = candles_list
            if candles_list:
                self.history_errors.pop(iv, None)
            else:
                self.history_errors[iv] = 'Historical API returned no candles'
            if candles_list:
                self.last_tick_time[iv] = candles_list[-1]['time']
            if os.getenv("VERBOSE", "false").lower() == "true":
                print(f"[HISTORICAL] Successfully loaded {len(candles_list)} historical candles for {self.symbol} ({iv})")
        except Exception as e:
            self.history_errors[iv] = str(e)
            if os.getenv("VERBOSE", "false").lower() == "true":
                print(f"[HISTORICAL WARNING] Could not fetch history for {self.symbol} ({iv}): {e}")
                print(f"                     Will build candles in real-time starting from first tick.")

    @staticmethod
    def _history_candles(records):
        """Reconstruct each history range independently, including session VWAP."""
        candles, session_date, session_value, session_volume = [], None, 0., 0.
        for record in records:
            stamp = market_datetime(record['date'])
            if session_date != stamp.date():
                session_date, session_value, session_volume = stamp.date(), 0., 0.
            typical = (record['high'] + record['low'] + record['close']) / 3.
            volume = float(record['volume'])
            session_value += typical * volume
            cumulative_start = session_volume
            session_volume += volume
            candles.append({'time': stamp,
                **{field: record[field] for field in ('open', 'high', 'low', 'close', 'volume')},
                'cumulative_volume_start': cumulative_start, 'cumulative_volume_end': session_volume,
                'vwap': session_value / session_volume if session_volume else typical})
        return candles

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
        # KiteTicker's native names differ from REST quote names. Keep legacy
        # aliases for callers/replays, but prefer the actual SDK fields.
        tick_vwap = tick.get("average_traded_price", tick.get("average_price"))
        tick_time = tick.get("exchange_timestamp") or tick.get("timestamp") or datetime.now(MARKET_TZ)
        volume = tick.get("volume_traded", tick.get("volume", 0))

        # Strip timezone if present
        tick_time = market_datetime(tick_time)
        if not regular_session_tick(tick_time, self.exchange):
            return set()
        previous_tick = getattr(self, '_latest_exchange_time', None)
        if previous_tick is not None and tick_time < previous_tick:
            return set()
        self._latest_exchange_time = tick_time
        session_open = session_bounds(tick_time, self.exchange)[0]

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
                # Before Kite publishes average_traded_price, LTP is the safest finite
                # fallback for a newly observed instrument.
                vwap = float(ltp)
            
            # Check if we should start a new candle or update current one
            if not self.candles[iv]:
                # First candle
                start_volume = 0 if iv == '1d' or bucket_time == session_open else volume
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
            elif bucket_time > self.last_tick_time[iv]:
                rolled_intervals.add(iv)
                # Close the current candle and open a new one
                if same_session:
                    start_volume = self.candles[iv][-1]['cumulative_volume_end']
                else:
                    # Session cumulative volume starts at zero each day. With
                    # no current-day history mid-session, count observed deltas
                    # instead of inventing a full opening-to-now candle.
                    start_volume = 0 if iv == '1d' or bucket_time == session_open else volume
                
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
            if iv in self.nnf_candles:
                rows = self.nnf_candles[iv]
                current = dict(self.candles[iv][-1])
                if rows and rows[-1]['time'] == current['time']:
                    rows[-1] = current
                else:
                    rows.append(current)
                limit = 1600 if iv == '1h' else 500
                del rows[:-limit]
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
                return resample_candles(df_1h, '4h', self.exchange)

            if iv not in self.candles or not self.candles[iv]:
                return pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume', 'vwap'])
            return pd.DataFrame(list(self.candles[iv]))

    def get_nnf_dataframe(self, iv):
        with self._lock:
            if iv == '4h':
                return resample_candles(self.get_nnf_dataframe('1h'), '4h', self.exchange)
            if iv in self.nnf_candles:
                return pd.DataFrame(self.nnf_candles[iv])
            return self.get_dataframe(iv)

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
        elif interval in ('30m', '1h', '4h'):
            # NSE hourly candles are anchored to the 09:15 session open.
            session_open = session_bounds(dt, self.exchange)[0]
            elapsed_minutes = max(0, int((dt - session_open).total_seconds() // 60))
            duration = {'30m': 30, '1h': 60, '4h': 240}[interval]
            return session_open + timedelta(minutes=(elapsed_minutes // duration) * duration)
        elif interval == '1d':
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return dt

class EvaluationSnapshot(SymbolData):
    """Private candle copies with one DataFrame construction per timeframe."""
    def __init__(self, source):
        super().__init__(source.instrument_token, source.symbol, source.instrument)
        with source._lock:
            self.candles = {iv: [dict(row) for row in rows]
                            for iv, rows in source.candles.items()}
            self.nnf_candles = {iv: [dict(row) for row in rows]
                                for iv, rows in source.nnf_candles.items()}
            self._nnf_evaluation_cache = source._nnf_evaluation_cache
        self._frames = {}
        self._nnf_frames = {}
        self._evaluation_frames = {}
        self._indicator_values = {}

    def get_dataframe(self, interval):
        if interval not in self._frames:
            self._frames[interval] = super().get_dataframe(interval)
        return self._frames[interval]

    def get_evaluation_dataframe(self, interval, ltp, vwap):
        """Prepare each live timeframe once for every rule in this snapshot."""
        vwap_key = None if vwap is None or pd.isna(vwap) else float(vwap)
        key = (interval, float(ltp), vwap_key)
        if key not in self._evaluation_frames:
            frame = self.get_dataframe(interval).copy()
            if not frame.empty:
                latest = frame.index[-1]
                frame.loc[latest, 'close'] = ltp
                frame.loc[latest, 'high'] = max(frame.loc[latest, 'high'], ltp)
                frame.loc[latest, 'low'] = min(frame.loc[latest, 'low'], ltp)
                frame.loc[latest, 'vwap'] = vwap
            self._evaluation_frames[key] = frame
        return self._evaluation_frames[key]

    def get_nnf_dataframe(self, interval):
        if interval not in self._nnf_frames:
            self._nnf_frames[interval] = super().get_nnf_dataframe(interval)
        return self._nnf_frames[interval]

    def cached_indicator(self, key, factory):
        if key not in self._indicator_values:
            self._indicator_values[key] = factory()
        return self._indicator_values[key]


class RealtimeScanner:
    def __init__(self, event_callback=None, status_callback=None):
        # Load environment variables
        load_dotenv(ENV_FILE, override=True)
        
        self.config = {}
        self.kite = None
        self.kws = None
        self._stop_requested = threading.Event()
        self._ticker_closed = threading.Event()
        
        self.bullish_conditions = []
        self.bearish_conditions = []
        
        self.last_reload_check = 0
        self.reload_cooldown = 5  # reload config/conditions files every 5s if modified
        
        self.token_to_symbol = {}
        self.symbol_data = {}  # token -> SymbolData
        self.event_callback = event_callback
        self.status_callback = status_callback
        self.history = HistoryStore(os.getenv("KITE_HISTORY_DB", str(KITE_DIR / "data" / "history.sqlite3")))
        self._archived_chart_candles = {}
        self.history_error = None
        self.latest_results = {}
        self.confirmed_signals = {}
        self._process_lock = threading.RLock()
        self._finalized_candles = {}
        self.connection_status = "initializing"
        self.connection_error = None
        self._last_tick_received = None
        self._last_reconnect_request = 0.0
        self._connected_at = None
        self._latest_wire_exchange_time = None
        self._tick_queue = queue.Queue(maxsize=1000)
        self._tick_worker = None
        self._last_evaluation_at = {}
        self._evaluation_interval = 5.0
        self.processing_error = None
        self._evaluation_condition = threading.Condition()
        self._evaluation_lock = threading.RLock()
        self._pending_evaluations = {}
        self._evaluation_worker = None
        self._archive_condition = threading.Condition()
        self._pending_archives = {}
        self._archive_worker = None
        self._archive_inflight = False
        self._history_queue = queue.Queue(maxsize=10000)
        self._history_worker = None
        self._persistence_stopping = threading.Event()
        self._last_chart_publish = {}
        self._last_processed_received = None
        self.latest_market_results = {}
        
        self.load_config()
        self.strategy_store = StrategyStore(KITE_DIR)
        self.active_strategy = self.strategy_store.load('default')
        self.bullish_conditions = self.active_strategy['bullish']
        self.bearish_conditions = self.active_strategy['bearish']
        self.alert_threshold = self.active_strategy['threshold']
        self.recording_profiles = {self.active_strategy['id']: self.active_strategy}
        self.strategy_states = {}
        self.profile_errors = {}
        self._bind_active_state()
        self._strategy_generation = 0
        self.strategy_error = None
        self._stock_alert_strategy_id = 'default'
        try:
            self._stock_alert_strategy_id = self.strategy_store.stock_alert_strategy()
            selected = self.strategy_store.selected()
            if selected != 'default':
                self.activate_strategy(selected, persist=False, source='startup restore')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.strategy_error = str(exc)
            strategy_log.warning('Saved strategy could not be restored; using %s: %s', self.active_strategy['name'], exc)
        self.refresh_recording_profiles()
        strategy_log.info('Active: %s [%s] | threshold=%g%% | bullish=%d bearish=%d | version=%s',
                          self.active_strategy['name'], self.active_strategy['id'], self.alert_threshold,
                          len(self.bullish_conditions), len(self.bearish_conditions), self.active_strategy['version'])

    def strategy_info(self):
        profile = self.active_strategy
        info = {key: profile[key] for key in ('id', 'name', 'version', 'threshold', 'cooldown_seconds', 'confirmation_timeframe', 'engine', 'warnings', 'instrument_scope') if key in profile}
        info['alert_strategies'] = [item['id'] for item in self.recording_profiles.values()
                                    if self._profile_alerts_enabled(item)]
        return info

    def _profile_alerts_enabled(self, profile):
        current = self.recording_profiles.get(profile['id'])
        if current is None or current['version'] != profile['version']:
            return False
        stock_id = (getattr(self, '_stock_alert_strategy_id', 'default')
                    if self.active_strategy.get('engine') == 'nnf' else self.active_strategy['id'])
        return profile.get('engine') == 'nnf' or profile['id'] == stock_id

    def profile_applies(self, profile, data):
        if profile.get('instrument_scope') and hasattr(self, '_commodity_tokens'):
            if data.instrument_token not in self._commodity_tokens:
                return False
        return applies(profile, data, getattr(self, 'config', {}).get('symbols'))

    def market_is_open(self, now=None):
        now = now or datetime.now(MARKET_TZ)
        exchanges = {getattr(data, 'exchange', data.symbol.split(':')[0])
                     for data in getattr(self, 'symbol_data', {}).values()} or {'NSE'}
        return any(session_is_open(now, exchange) for exchange in exchanges)

    def _profile_state(self, profile):
        return self.strategy_states.setdefault(profile['id'], {
            'latest': {}, 'prior': {}, 'confirmed': {}, 'finalized': {}})

    def _bind_active_state(self):
        state = self._profile_state(self.active_strategy)
        self.latest_results = state['latest']
        self._prior_results = state['prior']
        self.confirmed_signals = state['confirmed']
        self._finalized_candles = state['finalized']

    def refresh_recording_profiles(self):
        """Load all valid profiles; retain last working rules on invalid edits."""
        errors = {}
        available = self.strategy_store.profiles()
        present = {item['id'] for item in available}
        for item in available:
            identifier = item['id']
            try:
                if item.get('error'):
                    raise ValueError(item['error'])
                profile = self.strategy_store.load(identifier)
                previous = self.recording_profiles.get(identifier)
                if previous and previous['version'] == profile['version']:
                    continue
                if identifier == self.active_strategy['id']:
                    self.activate_strategy(identifier, persist=False)
                else:
                    self._merge_profile_history(self._prepare_profile_history(profile))
                    self.recording_profiles[identifier] = profile
                    strategy_log.info('Recording enabled: %s [%s] | version=%s',
                                      profile['name'], identifier, profile['version'])
            except (ValueError, OSError, TypeError, KeyError) as exc:
                errors[identifier] = str(exc)
                if self.profile_errors.get(identifier) != str(exc):
                    strategy_log.warning('Recording profile [%s] rejected; keeping last working rules if available: %s', identifier, exc)
        for identifier in list(self.recording_profiles):
            if identifier not in present and identifier != self.active_strategy['id']:
                del self.recording_profiles[identifier]
        self.profile_errors = errors
        self.strategy_error = errors.get(self.active_strategy['id'])

    @staticmethod
    def _profile_intervals(profile):
        intervals = set(BASE_TIMEFRAMES)
        intervals.add(profile.get('confirmation_timeframe', '5m'))
        for condition in profile['bullish'] + profile['bearish']:
            intervals.add(condition.interval)
            if condition.rule_type in ('yesterday_compare', 'multi_day_close', 'opening_or_previous_day'):
                intervals.add('1d')
        if '4h' in intervals:
            intervals.add('1h')
        return intervals - {'4h', 'tick'}

    def _prepare_profile_history(self, profile):
        prepared = {}
        for token, data in self.symbol_data.items():
            if not self.profile_applies(profile, data):
                continue
            # Startup and activation share SymbolData. Check under its lock so
            # a switch cannot refetch intervals currently being initialized.
            with data._lock:
                candidate = SymbolData(token, data.symbol, data.instrument)
                candidate.history_stop = self._stop_requested
                lookback = nnf_history_days(profile.get('bullish', []) + profile.get('bearish', []))
                upgrades = {iv: days for iv, days in lookback.items() if days > data.loaded_history_days.get(iv, 0)}
                intervals = (self._profile_intervals(profile) - data.intervals) | set(upgrades)
                candidate.check_and_add_intervals(self.kite, intervals, history_days=upgrades)
            for interval, rows in candidate.candles.items():
                if not rows:
                    detail = candidate.history_errors.get(interval, 'No historical candles available')
                    strategy_log.warning('History unavailable for %s [%s], strategy=%s: %s; activation continues, waiting for candle data',
                                         data.symbol, interval, profile['id'], detail)
            prepared[token] = candidate
        return prepared

    def _merge_profile_history(self, prepared):
        for token, candidate in prepared.items():
            data = self.symbol_data[token]
            with data._lock:
                data.history_days.update(candidate.history_days)
                data.loaded_history_days.update(candidate.loaded_history_days)
                if candidate.intervals:
                    data._nnf_evaluation_cache.clear()
                for interval, rows in candidate.nnf_candles.items():
                    merged = {row['time']: row for row in rows}
                    merged.update({row['time']: row for row in data.nnf_candles.get(interval, [])})
                    merged.update({row['time']: dict(row) for row in data.candles.get(interval, [])})
                    data.nnf_candles[interval] = [merged[stamp] for stamp in sorted(merged)]
                # Startup may have populated an interval while other symbols
                # were prepared. Never replace that history or live candle state.
                for interval in candidate.intervals - data.intervals:
                    data.intervals.add(interval)
                    data.candles[interval] = candidate.candles[interval]
                    data.last_tick_time[interval] = candidate.last_tick_time[interval]
                    if interval in candidate.history_errors:
                        data.history_errors[interval] = candidate.history_errors[interval]

    def activate_strategy(self, strategy_id, persist=True, source=None):
        # Consistent lock order: ingestion, evaluation, then pending queue.
        with self._process_lock, self._evaluation_lock:
            profile = self.strategy_store.load(strategy_id)
            previous = self.active_strategy
            stock_id = (getattr(self, '_stock_alert_strategy_id', 'default')
                        if profile.get('engine') == 'nnf' else profile['id'])
            if profile['id'] == previous['id'] and profile['version'] == previous['version']:
                if persist:
                    self.strategy_store.save_selection(strategy_id, stock_alert_strategy=stock_id)
                return self.strategy_info()
            prepared = self._prepare_profile_history(profile)
            if persist:
                self.strategy_store.save_selection(strategy_id, stock_alert_strategy=stock_id)
            self._merge_profile_history(prepared)
            self.active_strategy = profile
            self._stock_alert_strategy_id = stock_id
            self.recording_profiles[profile['id']] = profile
            # An NNF-only reload must not discard pending stock alerts. Old
            # NNF versions are independently rejected by _profile_alerts_enabled.
            if not (profile.get('engine') == previous.get('engine') == 'nnf'
                    and profile['id'] == previous['id']):
                self._strategy_generation += 1
            self.bullish_conditions = profile['bullish']
            self.bearish_conditions = profile['bearish']
            self.alert_threshold = profile['threshold']
            self._bind_active_state()
            self._last_evaluation_at.clear()
            self.strategy_error = None
            source = source or ('dashboard activation' if persist else 'rule file reload')
            strategy_log.info('%s: %s [%s] -> %s [%s] | threshold=%g%% | bullish=%d bearish=%d | version=%s',
                              source, previous['name'], previous['id'], profile['name'], profile['id'],
                              self.alert_threshold, len(self.bullish_conditions), len(self.bearish_conditions), profile['version'])
            if self.event_callback:
                self.event_callback({'type': 'strategy_changed', 'active': self.strategy_info(), 'source': source})
            return self.strategy_info()

    def _set_connection_status(self, status, error=None):
        if status == self.connection_status and (str(error) if error else None) == self.connection_error:
            return
        self.connection_status = status
        self.connection_error = str(error) if error else None
        if self.status_callback:
            self.status_callback(status, self.connection_error)

    def _set_connection_failure(self, code, reason):
        """Authentication is fatal; transport failures are automatically retried."""
        detail = f"{code}: {reason}" if code is not None else str(reason)
        if "403" in detail:
            self._set_connection_status("error", reason)
        else:
            self._set_connection_status("reconnecting", reason)

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            print(f"[ERROR] Configuration file '{CONFIG_FILE}' not found.")
            sys.exit(1)
        with open(CONFIG_FILE, "r") as f:
            self.config = json.load(f)
        # Preserve configured order while preventing duplicate subscriptions.
        self.config["symbols"] = list(dict.fromkeys(self.config.get("symbols", [])))

    def check_reloads(self):
        now = time.time()
        if now - self.last_reload_check > self.reload_cooldown:
            self.last_reload_check = now
            with self._process_lock, self._evaluation_lock:
                self.refresh_recording_profiles()

    def get_required_intervals(self, data=None):
        intervals = set(BASE_TIMEFRAMES)
        profiles = getattr(self, 'recording_profiles', None)
        if profiles is not None and data is not None:
            profiles = {key: profile for key, profile in profiles.items() if self.profile_applies(profile, data)}
        if profiles:
            intervals.update(profile.get('confirmation_timeframe', '5m') for profile in profiles.values())
        conditions = ([condition for profile in profiles.values()
                       for condition in profile['bullish'] + profile['bearish']]
                      if profiles is not None else self.bullish_conditions + self.bearish_conditions)
        for cond in conditions:
            intervals.add(cond.interval)
            if cond.rule_type in ('yesterday_compare', 'multi_day_close', 'opening_or_previous_day'):
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
        scopes = [profile['instrument_scope'] for profile in getattr(self, 'recording_profiles', {}).values()
                  if profile.get('instrument_scope')]
        exchanges.update(scope['exchange'] for scope in scopes)
        self.instrument_errors = {}

        all_instruments = []
        for ex in exchanges:
            try:
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Fetching symbols metadata for exchange: {ex}...")
                insts = self.kite.instruments(ex)
                all_instruments.extend(insts)
            except Exception as e:
                self.instrument_errors[ex] = str(e)
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
        metadata = {}
        for inst in all_instruments:
            ex = inst.get("exchange")
            sym = inst.get("tradingsymbol")
            token = inst.get("instrument_token")
            if ex and sym and token:
                mapping[f"{ex}:{sym}"] = token
                mapping[sym] = token
                metadata[token] = inst

        # Resolve requested symbols
        for sym in symbols:
            token = mapping.get(sym)
            if not token:
                clean_sym = sym.split(":")[-1] if ":" in sym else sym
                token = mapping.get(clean_sym)

            if token:
                self.token_to_symbol[token] = sym
                self.symbol_data[token] = SymbolData(token, sym, metadata.get(token))
                self.symbol_data[token].history_stop = self._stop_requested
                if os.getenv("VERBOSE", "false").lower() == "true":
                    print(f"[SYSTEM] Resolved {sym} -> Instrument Token: {token}")
            else:
                print(f"[SYSTEM WARNING] Could not resolve token for symbol: {sym}")

        if scopes:
            self._commodity_tokens = set()
            contracts = nearest_futures(all_instruments, datetime.now(MARKET_TZ).date())
            requested = {base for scope in scopes for base in scope['underlyings']}
            for base in sorted(requested):
                instrument = contracts.get(base)
                if instrument is None:
                    self.instrument_errors[base] = 'No unexpired standard MCX future found in Kite instruments'
                    strategy_log.warning('%s: %s; other subscriptions continue', base, self.instrument_errors[base])
                    continue
                token = instrument['instrument_token']
                self._commodity_tokens.add(token)
                symbol = 'MCX:' + instrument['tradingsymbol']
                if token not in self.symbol_data:
                    self.token_to_symbol[token] = symbol
                    self.symbol_data[token] = SymbolData(token, symbol, instrument)
                    self.symbol_data[token].history_stop = self._stop_requested
                strategy_log.info('NNF commodity: %s | expiry=%s', symbol, instrument['expiry'])

        if not self.token_to_symbol:
            print("[SYSTEM ERROR] No symbols could be resolved. Exiting.")
            sys.exit(1)

    def process_tick(self, tick):
        with self._process_lock:
            return self._process_tick_locked(tick)

    def _process_tick_locked(self, tick, defer_evaluation=False):
        token = tick.get("instrument_token")
        if token not in self.symbol_data:
            return
        tick_time = market_datetime(tick.get('exchange_timestamp') or
                                    tick.get('timestamp') or datetime.now(MARKET_TZ))
        if not regular_session_tick(tick_time, self.symbol_data[token].exchange) or not SymbolData._is_valid_price(tick.get('last_price')):
            return
        latest = getattr(self.symbol_data[token], '_latest_exchange_time', None)
        if latest is not None and tick_time < latest:
            return

        # Check and reload files if they changed
        self.check_reloads()

        sd = self.symbol_data[token]
        
        # Ensure intervals required by currently active conditions are initialized
        required_intervals = self.get_required_intervals(sd)
        sd.check_and_add_intervals(self.kite, required_intervals)
        
        # Add tick to compile OHLC candles
        if defer_evaluation and latest is not None:
            # Preserve the final pre-rollover snapshot for every profile, including
            # boundaries (such as 09:18 on 3m) that do not coincide with 5m.
            rolling = any(rows and sd._get_bucket_time(tick_time, iv) > rows[-1]['time']
                          for iv, rows in sd.candles.items())
            if rolling:
                previous = sd.candles.get('5m', [])
                price = previous[-1]['close'] if previous else tick['last_price']
                self._queue_evaluation(sd, price, sd.get_latest_vwap('5m', price),
                                       getattr(sd, '_latest_exchange_time', tick_time))
        rolled_intervals = sd.add_tick(tick)
        if defer_evaluation:
            now = time.monotonic()
            if self.event_callback and (rolled_intervals or
                    now - self._last_chart_publish.get(token, 0) >= 1):
                candles = {iv: _candle_dict(rows[-1])
                           for iv, rows in sd.candles.items()
                           if iv in SUPPORTED_TIMEFRAMES and rows}
                market_event = {
                    'type': 'candle_update', 'symbol': sd.symbol,
                    'instrument_token': token, 'candles_by_timeframe': candles,
                    'price': tick['last_price'],
                    'vwap': sd.get_latest_vwap('5m', tick['last_price']),
                    'market_time': tick_time.replace(tzinfo=MARKET_TZ).isoformat(),
                }
                self.latest_market_results[token] = market_event
                self.event_callback(market_event)
                self._last_chart_publish[token] = now

        # All ticks update OHLC/volume, but full indicator calculations need
        # not repeat for every price packet across hundreds of contracts.
        # Always evaluate a rollover so closed-candle confirmation is retained.
        evaluation_times = getattr(self, '_last_evaluation_at', {})
        evaluation_now = time.monotonic()
        if (not rolled_intervals and
                evaluation_now - evaluation_times.get(token, -float('inf')) <
                getattr(self, '_evaluation_interval', 5.0)):
            return
        evaluation_times[token] = evaluation_now
        self._last_evaluation_at = evaluation_times

        ltp = tick.get("last_price")
        if ltp is None:
            return None
        # In KiteTicker, average_traded_price is the running daily VWAP
        vwap = sd.get_latest_vwap('5m', ltp)

        if defer_evaluation:
            self._queue_evaluation(sd, ltp, vwap, tick_time)
            return
        return self._evaluate_all_snapshot(sd, ltp, vwap, tick_time)

    def _queue_evaluation(self, sd, ltp, vwap, tick_time):
        snapshot = EvaluationSnapshot(sd)
        candle = snapshot.candles.get('5m', [])
        if not candle:
            return
        key = (sd.instrument_token, tuple((iv, rows[-1]['time'])
                   for iv, rows in sorted(snapshot.candles.items()) if rows))
        with self._evaluation_condition:
            # Replace provisional evaluations for the same candle while
            # retaining the final snapshot of every earlier candle.
            self._pending_evaluations[key] = (
                snapshot, ltp, vwap, tick_time,
                tuple(self.bullish_conditions), tuple(self.bearish_conditions),
                self.alert_threshold, getattr(self, '_strategy_generation', 0),
                tuple(getattr(self, 'recording_profiles', {}).values()))
            self._evaluation_condition.notify()

    def _evaluate_all_snapshot(self, sd, ltp, vwap, tick_time, profiles=None, allow_alerts=True):
        profiles = tuple(self.recording_profiles.values()) if profiles is None and hasattr(self, 'recording_profiles') else profiles
        if not profiles:
            return self._evaluate_snapshot(sd, ltp, vwap, tick_time, alert=allow_alerts)
        # The selected strategy owns user-facing alerts, so evaluate it before
        # profiles that are recorded only for later review.
        profiles = tuple(sorted(profiles, key=lambda profile: not (
            profile['id'] == self.active_strategy['id'] and
            profile['version'] == self.active_strategy['version'])))
        active_event = None
        for profile in profiles:
            if not self.profile_applies(profile, sd):
                continue
            active = (profile['id'] == self.active_strategy['id'] and
                      profile['version'] == self.active_strategy['version'])
            try:
                event = self._evaluate_snapshot(sd, ltp, vwap, tick_time, profile=profile,
                                                alert=self._profile_alerts_enabled(profile) and allow_alerts, publish=active)
            except Exception as exc:
                self.processing_error = str(exc)
                strategy_log.exception('Strategy evaluation failed for %s [%s]; continuing other profiles',
                                       sd.symbol, profile['id'])
                continue
            if active:
                active_event = event
        return active_event

    def _evaluate_snapshot(self, sd, ltp, vwap, tick_time, rules=None, profile=None, alert=True, publish=True):
        token = sd.instrument_token
        profile = profile or getattr(self, 'active_strategy', None)
        if profile and not self.profile_applies(profile, sd):
            return None
        if profile and rules is None:
            rules = (profile['bullish'], profile['bearish'], profile['threshold'])
        bull_rules, bear_rules, threshold = rules or (
            self.bullish_conditions, self.bearish_conditions, self.alert_threshold)

        as_of = getattr(sd, '_nnf_as_of', tick_time)
        bullish = evaluate_side("bullish", bull_rules, ltp, vwap, sd, threshold, as_of=as_of)
        bearish = evaluate_side("bearish", bear_rules, ltp, vwap, sd, threshold, as_of=as_of)
        bullish, bearish = resolve_direction(bullish, bearish)
        missing = missing_rule_timeframes(sd, bull_rules, bear_rules) if profile else []
        confirmation = profile.get('confirmation_timeframe', '5m') if profile else '5m'
        if sd.get_dataframe(confirmation).empty and confirmation not in missing:
            missing.append(confirmation)
        if missing:
            bullish, bearish = replace(bullish, triggered=False), replace(bearish, triggered=False)
        if profile and hasattr(self, 'strategy_states'):
            state = self._profile_state(profile)
            latest, priors = state['latest'], state['prior']
        else:
            state = None
            latest = self.latest_results
            if not hasattr(self, '_prior_results'):
                self._prior_results = {}
            priors = self._prior_results
        event = live_event(sd, ltp, vwap, bullish, bearish, market_time=tick_time,
                           confirmation_timeframe=confirmation)
        details = nnf_event_details(sd, list(bull_rules) + list(bear_rules), as_of)
        event.update(details)
        missing = sorted(set(missing) | set(details.get('missing_timeframes', [])))
        stale_closed_signal = False
        if event.get('evaluation_mode') == 'closed_candle':
            cutoff = event.get('as_of')
            evaluation_time = market_datetime(as_of).replace(tzinfo=MARKET_TZ).timestamp()
            signal_age = evaluation_time - cutoff if cutoff is not None else float('inf')
            stale_closed_signal = not 0 <= signal_age < NNF_MINUTES[confirmation] * 60
            event['signal_stale'] = stale_closed_signal
        if missing or stale_closed_signal:
            bullish, bearish = replace(bullish, triggered=False), replace(bearish, triggered=False)
            event.update(bullish=bullish.to_dict(), bearish=bearish.to_dict())
        event['threshold'] = threshold
        event['data_ready'] = not missing
        event['missing_timeframes'] = missing
        if profile:
            event.update(strategy_id=profile['id'], strategy_name=profile['name'], strategy_version=profile['version'])
        event['alerts_enabled'] = bool(alert)
        latest[token] = event
        history = getattr(self, 'history', None)

        # Notify before any SQLite/chart checkpoint work. With hundreds of
        # symbols, persistence latency must never make a fresh match go stale.
        evaluation_lag = max(0., (datetime.now(MARKET_TZ).replace(tzinfo=None) - tick_time).total_seconds())
        if alert:
            self._last_evaluation_lag_seconds = evaluation_lag
        max_alert_age = float(getattr(self, 'config', {}).get('alert_max_age_seconds', 30))
        for result, is_bullish, key in (
            (bullish, True, "bullish_block"),
            (bearish, False, "bearish_block"),
        ):
            if alert and result.triggered and evaluation_lag <= max_alert_age:
                passed_text = "\n     • ".join(rule.text for rule in result.rules if rule.passed)
                condition_str = (
                    f"{result.percentage:.1f}% conditions passed "
                    f"({result.passed}/{result.total}):\n     • {passed_text}"
                )
                if profile:
                    key = f"{profile['id']}:{profile['version']}:{key}"
                    cooldown = profile.get('cooldown_seconds')
                    notifier.cooldown_seconds = cooldown if cooldown is not None else self.config.get('cooldown_seconds', 300)
                    condition_str = f"[{profile['name']}] {condition_str}"
                notification = format_alert(sd.symbol, is_bullish, condition_str, ltp, event)
                sent = notifier.send_alert(sd.symbol, is_bullish=is_bullish,
                                    condition_str=condition_str, price=ltp,
                                    cooldown_key=key, notification=notification)
                if sent and history:
                    self._record_history("alert", {**event, "alert_side": result.side,
                                                  "notification": notification})

        if history:
            self._record_history('evaluation', event)
            # Chart building and large JSON writes are coalesced by symbol on a
            # dedicated worker. Event rows remain exact and ordered here.
            archive_key = (token, confirmation)
            if event.get('candle') and self._archived_chart_candles.get(archive_key) != event['candle']['time']:
                self._schedule_chart_archive(sd, event, archive_key)

        # Freeze the last live state as the signal for the candle that just closed.
        confirmed_signal = None
        prior = priors.get(token)
        if event.get('evaluation_mode') == 'closed_candle':
            candle = event.get('candle')
            if candle and datetime.fromtimestamp(candle['time'], MARKET_TZ).date() == tick_time.date():
                confirmed_signal = self._confirm_once(token, event, state)
        elif (prior and prior.get('candle') and event.get('candle') and
                prior.get('confirmation_timeframe', '5m') == confirmation and
                prior.get('strategy_version') == event.get('strategy_version') and
                prior['candle']['time'] < event['candle']['time']):
            confirmed_signal = self._confirm_once(token, prior, state)
        event["confirmed_signal"] = confirmed_signal

        priors[token] = event

        if publish and self.event_callback:
            self.event_callback(event)
        return event

    def _process_evaluations(self):
        while True:
            with self._evaluation_condition:
                self._evaluation_condition.wait_for(
                    lambda: self._pending_evaluations or self._stop_requested.is_set(), timeout=0.25)
                if not self._pending_evaluations:
                    if self._stop_requested.is_set() and not self._producer_alive('_tick_worker'):
                        return
                    if self._stop_requested.is_set():
                        self._evaluation_condition.wait(.05)
                    continue
                key = next(iter(self._pending_evaluations))
                task = self._pending_evaluations.pop(key)
                self._evaluation_inflight = True
                sd, ltp, vwap, stamp, bull, bear, threshold, generation = task[:8]
            try:
                with self._evaluation_lock:
                    current = generation == getattr(self, '_strategy_generation', 0)
                    if len(task) > 8 and task[8]:
                        self._evaluate_all_snapshot(sd, ltp, vwap, stamp, task[8],
                                                    allow_alerts=current and not self._stop_requested.is_set())
                    elif current:
                        self._evaluate_snapshot(sd, ltp, vwap, stamp, (bull, bear, threshold),
                                                alert=not self._stop_requested.is_set())
            except Exception as exc:
                self.processing_error = str(exc)
                print(f'[EVALUATION ERROR] {exc}')
            finally:
                with self._evaluation_condition:
                    self._evaluation_inflight = False

    def _record_history(self, kind, event):
        worker = getattr(self, '_history_worker', None)
        if worker is not None and worker.is_alive():
            # Immutable event payloads stay in order; no evaluations or alerts
            # are coalesced or silently dropped when disk work is slower.
            self._history_queue.put((kind, {k: v for k, v in event.items() if k != 'confirmed_signal'}))
            return
        try:
            self.history.record(kind, event)
        except Exception as exc:
            self.history_error = str(exc)
            print(f'[HISTORY ERROR] {exc}')

    def _process_history(self):
        while not self._persistence_stopping.is_set() or not self._history_queue.empty():
            try:
                batch = [self._history_queue.get(timeout=.1)]
            except queue.Empty:
                continue
            while len(batch) < 100:
                try:
                    batch.append(self._history_queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self.history.record_many(batch)
            except Exception as exc:
                self.history_error = str(exc)
                strategy_log.exception('History batch write failed; retrying without losing events')
                # Retain this batch in order and apply backpressure to producers.
                while True:
                    try:
                        self.history.record_many(batch)
                        break
                    except Exception:
                        time.sleep(.25)
            finally:
                for _ in batch:
                    self._history_queue.task_done()

    def _archive_charts(self, sd, event, profiles=None):
        try:
            day = datetime.fromtimestamp(event['candle']['time'], MARKET_TZ).date().isoformat()
            base_charts = {tf: chart_snapshot(sd, tf, limit=600) for tf in SUPPORTED_TIMEFRAMES}
            self.history.save_charts(sd.instrument_token, day, base_charts)
            saved = True
            for profile in (tuple(getattr(self, 'recording_profiles', {}).values()) if profiles is None else profiles):
                if not self.profile_applies(profile, sd):
                    continue
                try:
                    self.history.save_strategy_charts(sd.instrument_token, day, profile['id'], {
                        tf: profile_chart_snapshot(sd, tf, profile, limit=600, base=base_charts[tf]) for tf in SUPPORTED_TIMEFRAMES})
                except Exception as exc:
                    saved = False
                    self.history_error = str(exc)
                    strategy_log.exception('Chart archive failed for %s [%s]; continuing other profiles', sd.symbol, profile['id'])
            return saved
        except Exception as exc:
            self.history_error = str(exc)
            print(f'[HISTORY ERROR] {exc}')
            return False

    def _schedule_chart_archive(self, sd, event, archive_key):
        worker = getattr(self, '_archive_worker', None)
        if worker is None or not worker.is_alive():
            if self._archive_charts(sd, event):
                self._archived_chart_candles[archive_key] = event['candle']['time']
            return
        with self._archive_condition:
            day = datetime.fromtimestamp(event['candle']['time'], MARKET_TZ).date()
            pending_key = (sd.instrument_token, day)
            pending = self._pending_archives.get(pending_key)
            keys = dict(pending[2]) if pending else {}
            keys[archive_key] = event['candle']['time']
            # A checkpoint must never read future candles from live SymbolData
            # or apply a strategy version loaded after the checkpoint was queued.
            snapshot = sd if isinstance(sd, EvaluationSnapshot) else EvaluationSnapshot(sd)
            profiles = tuple(getattr(self, 'recording_profiles', {}).values())
            self._pending_archives[pending_key] = (snapshot, dict(event), keys, profiles)
            # Mark the candle when queued so repeated evaluations coalesce.
            self._archived_chart_candles[archive_key] = event['candle']['time']
            self._archive_condition.notify()

    def _process_chart_archives(self):
        while True:
            with self._archive_condition:
                self._archive_condition.wait_for(
                    lambda: self._pending_archives or self._persistence_stopping.is_set(), timeout=.25)
                if not self._pending_archives:
                    if self._persistence_stopping.is_set():
                        return
                    continue
                if (not self._persistence_stopping.is_set() and
                        (getattr(self, '_evaluation_inflight', False) or
                         getattr(self, '_pending_evaluations', {}))):
                    # Large chart builds contend with indicator evaluation for
                    # Python CPU time. Let live alerts catch up before starting
                    # the next coalesced checkpoint; recordings stay queued.
                    self._archive_condition.wait(timeout=.05)
                    continue
                token = next(iter(self._pending_archives))
                sd, event, keys, profiles = self._pending_archives.pop(token)
                self._archive_inflight = True
            try:
                saved = self._archive_charts(sd, event, profiles)
            except Exception:
                saved = False
                strategy_log.exception('Chart archive worker failed')
            with self._archive_condition:
                if not saved:
                    for key, candle_time in keys.items():
                        if self._archived_chart_candles.get(key) == candle_time:
                            self._archived_chart_candles.pop(key, None)
                self._archive_inflight = False
                self._archive_condition.notify_all()

    def _confirm_once(self, token, prior, state=None):
        if not prior or not prior.get('candle'):
            return None
        finalized = state['finalized'] if state is not None else self._finalized_candles
        confirmed = state['confirmed'] if state is not None else self.confirmed_signals
        candle_time = prior['candle']['time']
        timeframe = prior.get('confirmation_timeframe', '5m')
        key = token if timeframe == '5m' else (token, timeframe)
        if candle_time <= finalized.get(key, -1):
            return None
        finalized[key] = candle_time
        # Retain neutral states too: absence of a signal is not missing data.
        # Store an immutable-in-practice shallow copy without nested signal history.
        signal = {key: value for key, value in prior.items() if key != 'confirmed_signal'}
        previous = confirmed.setdefault(token, [])
        if getattr(self, 'history', None):
            self._record_history('confirmed', signal)
        previous.append(signal)
        del previous[:-500]
        return signal

    def _refresh_session_close(self, now):
        """Evaluate the last ingested tick, including ticks skipped by throttling."""
        if not hasattr(self, '_session_close_checked'):
            self._session_close_checked = {}
        for token, data in getattr(self, 'symbol_data', {}).items():
            stamp = getattr(data, '_latest_exchange_time', None)
            if stamp is None or now < session_bounds(stamp, data.exchange)[1]:
                continue
            if self._session_close_checked.get(token) == stamp:
                continue
            profiles = tuple(profile for profile in getattr(self, 'recording_profiles', {}).values()
                             if self.profile_applies(profile, data))
            expected_time = stamp.replace(tzinfo=MARKET_TZ).isoformat()
            if profiles:
                stale_profiles = tuple(profile for profile in profiles
                    if (self._profile_state(profile)['prior'].get(token, {}).get('market_time') != expected_time
                        or profile.get('engine') == 'nnf'))
                if not stale_profiles:
                    self._session_close_checked[token] = stamp
                    continue
            else:
                stale_profiles = None
                if getattr(self, '_prior_results', {}).get(token, {}).get('market_time') == expected_time:
                    self._session_close_checked[token] = stamp
                    continue
            snapshot = EvaluationSnapshot(data)
            snapshot._nnf_as_of = session_bounds(stamp, data.exchange)[1]
            rows = snapshot.candles.get('5m', [])
            if not rows:
                continue
            price = rows[-1]['close']
            vwap = snapshot.get_latest_vwap('5m', price)
            if stale_profiles:
                self._evaluate_all_snapshot(snapshot, price, vwap, stamp,
                                            profiles=stale_profiles, allow_alerts=False)
            else:
                self._evaluate_snapshot(snapshot, price, vwap, stamp, alert=False)
            self._session_close_checked[token] = stamp

    def finalize_session(self, now=None):
        """Close the last observed candle at session end even if no new tick arrives."""
        now = market_datetime(now or datetime.now(MARKET_TZ))
        self.maintain_history(now)
        with self._process_lock, getattr(self, '_evaluation_lock', self._process_lock):
            # Never freeze a stale result while an already-received tick or
            # a queued/popped evaluation is still being processed.
            if (getattr(getattr(self, '_tick_queue', None), 'unfinished_tasks', 0) or
                    getattr(self, '_pending_evaluations', {}) or
                    getattr(self, '_evaluation_inflight', False)):
                return
            self._refresh_session_close(now)
            entries = ([(state, token, prior) for state in self.strategy_states.values()
                        for token, prior in state['prior'].items()] if hasattr(self, 'strategy_states')
                       else [(None, token, prior) for token, prior in getattr(self, '_prior_results', {}).items()])
            archived = set()
            for state, token, prior in entries:
                if not prior.get('candle'):
                    continue
                start = datetime.fromtimestamp(prior['candle']['time'], MARKET_TZ)
                data = getattr(self, 'symbol_data', {}).get(token)
                end = session_bounds(market_datetime(start), getattr(data, 'exchange', 'NSE'))[1]
                if now < end:
                    continue
                signal = self._confirm_once(token, prior, state)
                if signal and token not in archived and getattr(self, 'history', None):
                    confirmation = prior.get('confirmation_timeframe', '5m')
                    self._schedule_chart_archive(self.symbol_data[token], prior, (token, confirmation))
                    archived.add(token)
                active = (not hasattr(self, 'active_strategy') or
                          (prior.get('strategy_id') == self.active_strategy['id'] and
                           prior.get('strategy_version') == self.active_strategy['version']))
                if signal and active and self.event_callback:
                    self.event_callback({**prior, 'confirmed_signal': signal})

    def reconnect_if_stale(self, max_age=60.0, now=None):
        """Recycle a connected Kite socket when the whole live feed goes quiet."""
        now = market_datetime(now or datetime.now(MARKET_TZ))
        if not self.market_is_open(now):
            return False
        ticker = self.kws
        last_tick = self._last_tick_received
        monotonic_now = time.monotonic()
        latest_exchange = getattr(self, '_latest_wire_exchange_time', None)
        arrivals_stale = last_tick is not None and monotonic_now - last_tick > max_age
        connected_at = getattr(self, '_connected_at', None)
        connection_started = connected_at if connected_at is not None else last_tick
        connection_age = monotonic_now - connection_started if connection_started is not None else 0.0
        exchange_stale = (latest_exchange is not None and
                          (now - latest_exchange).total_seconds() > max_age and
                          connection_age > max_age)
        if (self.connection_status != "connected" or ticker is None or
                last_tick is None or not (arrivals_stale or exchange_stale) or
                monotonic_now - self._last_reconnect_request <= max_age):
            return False
        self._last_reconnect_request = monotonic_now
        self._set_connection_status("reconnecting", "Kite feed stale; reconnecting")
        from twisted.internet import reactor
        def recycle_connection():
            # Do not call KiteTicker.close(): it disables the SDK retry loop.
            if ticker.is_connected():
                # Autobahn accepts normal closure (1000) or application-defined
                # codes in 3000-4999. Keep this non-normal so Kite's retry
                # factory treats the stale socket as a lost connection.
                ticker._close(code=4001, reason="Stale market data")
        if reactor.running:
            reactor.callFromThread(recycle_connection)
        else:
            recycle_connection()
        return True

    def feed_age_seconds(self):
        """Age of the last batch received from Kite, independent of one symbol."""
        last_tick = self._last_tick_received
        return max(0.0, time.monotonic() - last_tick) if last_tick is not None else None

    def _process_tick_queue(self):
        """Process ticks away from Twisted so indicator work cannot block pings."""
        while True:
            try:
                ticks = self._tick_queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop_requested.is_set():
                    return
                continue
            if ticks is None:
                self._tick_queue.task_done()
                break
            try:
                for tick in ticks:
                    try:
                        with self._process_lock:
                            self._process_tick_locked(tick, defer_evaluation=True)
                        self._last_processed_received = time.monotonic()
                    except Exception as exc:
                        self.processing_error = str(exc)
                        print(f"[PROCESSING ERROR] {exc}")
            finally:
                self._tick_queue.task_done()

    def request_stop(self):
        """Prevent reconnects/startup and close Kite on its owning reactor thread."""
        self._stop_requested.set()
        condition = getattr(self, '_evaluation_condition', None)
        if condition is not None:
            with condition:
                condition.notify_all()
        archive_condition = getattr(self, '_archive_condition', None)
        if archive_condition is not None:
            with archive_condition:
                archive_condition.notify_all()
        try:
            self._tick_queue.put_nowait(None)
        except (AttributeError, queue.Full):
            pass
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

    def _producer_alive(self, name):
        worker = getattr(self, name, None)
        return isinstance(worker, threading.Thread) and worker.is_alive()

    def flush_background(self, timeout=60):
        """Drain producers before closing their persistence consumers; bound waits."""
        deadline = time.monotonic() + timeout
        for name in ('_tick_worker', '_evaluation_worker'):
            worker = getattr(self, name, None)
            if isinstance(worker, threading.Thread):
                worker.join(max(0, deadline-time.monotonic()))
        if self._producer_alive('_tick_worker') or self._producer_alive('_evaluation_worker'):
            strategy_log.warning('Shutdown timeout: tick/evaluation work has not finished')
            return False
        stopping = getattr(self, '_persistence_stopping', None)
        if stopping is not None:
            stopping.set()
        condition = getattr(self, '_archive_condition', None)
        if condition is not None:
            with condition:
                condition.notify_all()
        for name in ('_history_worker', '_archive_worker'):
            worker = getattr(self, name, None)
            if isinstance(worker, threading.Thread):
                worker.join(max(0, deadline-time.monotonic()))
        notifier.stop_background(max(0, deadline-time.monotonic()))
        complete = not any(self._producer_alive(name) for name in ('_history_worker', '_archive_worker'))
        if not complete:
            strategy_log.warning('Shutdown timeout: queued recordings/checkpoints remain unsaved')
        return complete

    def maintain_history(self, now=None):
        now = market_datetime(now or datetime.now(MARKET_TZ))
        if getattr(self, '_retention_day', None) == now.date():
            return
        try:
            if getattr(self, 'history', None):
                self.history.prune(now)
            if history_requests.persistent_cache:
                history_requests.persistent_cache.prune(now)
        except Exception:
            strategy_log.exception('History retention cleanup failed; will retry')
            return
        self._retention_day = now.date()

    def run(self, threaded=False):
        if self._stop_requested.is_set():
            return
        self.maintain_history()
        try:
            history_requests.enable_persistence(self.history.path)
        except Exception:
            strategy_log.exception('Persistent candle cache unavailable; using broker history')
        # 1. Resolve Instrument Tokens
        self.resolve_tokens()

        # 2. Pre-fetch historical candles for symbols for required intervals
        required_intervals = self.get_required_intervals()
        total = len(self.symbol_data)
        strategy_log.info('History warmup: %d symbols, %d intervals, at most 2 requests/second; persistent cache reused, missing candles refreshed', total, len(required_intervals))
        for index, (token, sd) in enumerate(self.symbol_data.items(), 1):
            if self._stop_requested.is_set():
                return
            required_intervals = self.get_required_intervals(sd)
            lookback = nnf_history_days([rule for profile in self.recording_profiles.values() if self.profile_applies(profile, sd)
                                         for rule in profile['bullish'] + profile['bearish']])
            sd.check_and_add_intervals(self.kite, required_intervals, history_days=lookback)
            if index % 25 == 0 or index == total:
                strategy_log.info('History warmup progress: %d/%d symbols | requests=%d cache_hits=%d disk_hits=%d incremental=%d retries=%d',
                                  index, total, history_requests.requests, history_requests.cache_hits,
                                  history_requests.disk_hits, history_requests.incremental_requests, history_requests.retries)

        # 3. Setup WebSocket connection
        api_key = os.getenv("KITE_API_KEY")
        access_token = os.getenv("KITE_ACCESS_TOKEN")
        
        if self._stop_requested.is_set():
            return
        self._ticker_closed.clear()
        self._persistence_stopping.clear()
        self._history_worker = threading.Thread(target=self._process_history, name='kite-history-writer', daemon=True)
        self._history_worker.start()
        self._archive_worker = threading.Thread(
            target=self._process_chart_archives, name='kite-chart-archive-worker', daemon=True)
        self._archive_worker.start()
        notifier.start_background()
        self._tick_worker = threading.Thread(
            target=self._process_tick_queue, name="kite-tick-worker", daemon=True)
        self._tick_worker.start()
        self._evaluation_worker = threading.Thread(
            target=self._process_evaluations, name='kite-evaluation-worker', daemon=True)
        self._evaluation_worker.start()
        self.kws = KiteTicker(api_key, access_token)
        tokens = list(self.token_to_symbol.keys())
        self._set_connection_status("connecting")

        def on_ticks(ws, ticks):
            if self._stop_requested.is_set():
                return
            if ticks:
                self._last_tick_received = time.monotonic()
                # Fresh wire traffic is stronger evidence than a delayed
                # transport callback left over from a reconnect attempt.
                if self.connection_status != "connected":
                    self._set_connection_status("connected")
            normalized = [normalize_kite_tick(tick) for tick in ticks]
            exchange_times = [market_datetime(tick.get('exchange_timestamp') or
                                              tick.get('timestamp'))
                              for tick in normalized
                              if tick.get('exchange_timestamp') or tick.get('timestamp')]
            if exchange_times:
                newest = max(exchange_times)
                if (self._latest_wire_exchange_time is None or
                        newest > self._latest_wire_exchange_time):
                    self._latest_wire_exchange_time = newest
            try:
                self._tick_queue.put_nowait(normalized)
            except queue.Full:
                self.processing_error = "Tick processor overloaded: incoming batch could not be retained"
                print(f"[PROCESSING ERROR] {self.processing_error}")

        def on_connect(ws, response):
            self._ticker_closed.clear()
            if self._stop_requested.is_set():
                ws.close(code=1000, reason="Application shutdown")
                return
            self._set_connection_status("connected")
            self._connected_at = self._last_tick_received = time.monotonic()
            print(f"[WEBSOCKET] Connected! Subscribing to: {list(self.token_to_symbol.values())}")
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        def on_close(ws, code, reason):
            self._ticker_closed.set()
            if self._stop_requested.is_set():
                return
            self._set_connection_failure(code, reason)
            print(f"[WEBSOCKET] Connection closed: Code {code} | Reason: {reason}")

        def on_error(ws, code, reason):
            if self._stop_requested.is_set():
                return
            self._set_connection_failure(code, reason)
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
    finally:
        scanner.request_stop()
        scanner.flush_background()
