import unittest
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from conditions import Condition
from scanner import SymbolData, market_datetime, normalize_kite_tick
from strategy_runtime import chart_snapshot, evaluate_side, live_event, resolve_direction


class TestLiveDashboard(unittest.TestCase):
    def test_three_minute_chart_history_overlays_and_live_rollover(self):
        from scanner import RealtimeScanner
        from unittest.mock import Mock
        scanner = RealtimeScanner.__new__(RealtimeScanner)
        scanner.bullish_conditions = []
        scanner.bearish_conditions = []
        self.assertIn('3m', scanner.get_required_intervals())
        data = SymbolData(1, 'NSE:TEST')
        broker = Mock()
        broker.historical_data.return_value = [
            {'date': datetime(2026, 10, 7, 9, 15) + timedelta(minutes=3*i),
             'open': 100+i, 'high': 102+i, 'low': 99+i, 'close': 101+i, 'volume': 10}
            for i in range(55)]
        data.check_and_add_intervals(broker, {'3m'})
        self.assertEqual(broker.historical_data.call_args.args[-1], '3minute')
        snapshot = chart_snapshot(data, '3m')
        overlays = {item['id']: item for item in snapshot['overlays']}
        self.assertEqual(set(overlays), {'ema20', 'ema50', 'vwap'})
        self.assertTrue(overlays['ema20']['points'])
        self.assertTrue(overlays['ema50']['points'])
        self.assertEqual(snapshot['studies'], [])
        for minute, price, volume in ((15, 100, 100), (17, 102, 120), (18, 101, 140)):
            data.add_tick({'timestamp': datetime(2026, 10, 8, 9, minute),
                           'last_price': price, 'volume': volume, 'average_price': 101})
        snapshot = chart_snapshot(data, '3m')
        self.assertEqual(snapshot['candles'][-2]['open'], 100)
        self.assertEqual(snapshot['candles'][-2]['close'], 102)
        self.assertEqual(data.candles['3m'][-1]['time'], datetime(2026, 10, 8, 9, 18))
        side = evaluate_side('bullish', [], 101, 101, data, 100)
        event = live_event(data, 101, 101, side, side)
        self.assertEqual(event['candles_by_timeframe']['3m'], snapshot['candles'][-1])

    def test_conflicting_direction_uses_stronger_score_and_tie_is_neutral(self):
        from strategy_runtime import RuleResult, SideResult
        rules = (RuleResult('test', True),)
        bull = SideResult('bullish', 8, 14, 57.1, True, rules)
        bear = SideResult('bearish', 5, 14, 35.7, True, rules)
        resolved_bull, resolved_bear = resolve_direction(bull, bear)
        self.assertTrue(resolved_bull.triggered)
        self.assertFalse(resolved_bear.triggered)
        self.assertEqual(resolved_bear.percentage, 35.7)

        tied_bull, tied_bear = resolve_direction(
            bull, SideResult('bearish', 8, 14, 57.1, True, rules))
        self.assertFalse(tied_bull.triggered)
        self.assertFalse(tied_bear.triggered)

    def test_live_event_reports_exchange_time_separately_from_server_time(self):
        data = SymbolData(1, 'NSE:TEST')
        market_time = datetime(2026, 10, 8, 11, 18, 7)
        side = evaluate_side('bullish', [], 100, 100, data, 100)
        event = live_event(data, 100, 100, side, side, market_time=market_time)
        self.assertEqual(event['market_time'], '2026-10-08T11:18:07+05:30')
        self.assertIn('server_time', event)

    def test_ribbon_replay_excludes_future_higher_timeframe_ohlc(self):
        from strategy_runtime import historical_strategy_states
        rows = [{'time': datetime(2026, 10, 7, 9, minute), 'open': 100,
                 'high': 101 if minute < 25 else 999, 'low': 99,
                 'close': 100, 'volume': 10, 'vwap': 100}
                for minute in (15, 20, 25)]
        frames = {'5m': pd.DataFrame(rows)}
        for interval in ('15m', '1h', '1d'):
            frames[interval] = pd.DataFrame([{**rows[0], 'high': 999,
                'time': datetime(2026, 10, 7) if interval == '1d' else rows[0]['time']}])
        class Data:
            def get_dataframe(self, interval):
                return frames[interval].copy()
        class Rule:
            raw_text = 'No future high'
            def evaluate(self, ltp, vwap, data):
                return all(data.get_dataframe(iv).iloc[-1]['high'] < 500
                           for iv in ('15m', '1h', '1d'))
        original = {iv: frame.copy() for iv, frame in frames.items()}
        states = historical_strategy_states(Data(), [Rule()], [], 100,
                                             now=datetime(2026, 10, 7, 9, 25))
        self.assertEqual(len(states), 2)
        self.assertTrue(all(s['bullish']['triggered'] for s in states))
        self.assertTrue(all(s['evaluation_source'] == 'reconstructed' for s in states))
        for iv in frames:
            pd.testing.assert_frame_equal(frames[iv], original[iv])
        frames['5m'] = frames['5m'].iloc[1:]
        self.assertEqual(historical_strategy_states(Data(), [Rule()], [], 100,
                        now=datetime(2026, 10, 7, 9, 30)), [])

    def test_historical_fallback_is_labelled_and_does_not_mutate_candles(self):
        from strategy_runtime import historical_evaluation
        data = SymbolData(1, 'NSE:TEST')
        self.assertIsNone(historical_evaluation(data, [], [], 30))
        data.intervals.add('5m')
        row = {'time': datetime(2026, 10, 7, 15, 25), 'open': 100,
               'high': 102, 'low': 99, 'close': 101, 'volume': 100, 'vwap': 100.5}
        data.candles['5m'] = [dict(row)]
        rule = Condition.parse('5-minute Close is above VWAP')
        result = historical_evaluation(data, [rule], [], 30)
        self.assertEqual(result['evaluation_source'], 'historical')
        self.assertEqual(result['price'], 101)
        self.assertEqual(result['bullish']['passed'], 1)
        self.assertNotIn('confirmed_signal', result)
        self.assertEqual(data.candles['5m'], [row])

    def test_outside_session_and_stale_ticks_do_not_change_candles(self):
        data = SymbolData(1, 'NSE:TEST')
        data.intervals.add('5m')
        data.candles['5m'] = []
        data.last_tick_time['5m'] = None
        def tick(hour, minute, price=100):
            return {'timestamp': datetime(2026, 10, 7, hour, minute),
                    'last_price': price, 'volume': 100, 'average_price': 100}
        data.add_tick(tick(9, 14))
        self.assertEqual(data.candles['5m'], [])
        data.add_tick(tick(15, 29))
        before = [dict(c) for c in data.candles['5m']]
        for update in (tick(15, 30, 999), tick(15, 50, 999), tick(15, 28, 999)):
            self.assertEqual(data.add_tick(update), set())
            self.assertEqual(data.candles['5m'], before)

    def test_session_finalization_is_once_without_synthetic_candle(self):
        import threading
        from scanner import RealtimeScanner
        scanner = RealtimeScanner.__new__(RealtimeScanner)
        scanner._process_lock = threading.RLock()
        scanner._finalized_candles = {}
        scanner.confirmed_signals = {}
        events = []
        scanner.event_callback = events.append
        instant = int(datetime(2026, 10, 7, 9, 55, tzinfo=timezone.utc).timestamp())
        prior = {'candle': {'time': instant, 'close': 100},
                 'bullish': {'triggered': False}, 'bearish': {'triggered': True}}
        scanner._prior_results = {1: prior}
        scanner.finalize_session(datetime(2026, 10, 7, 15, 29, 59))
        self.assertEqual(events, [])
        scanner.finalize_session(datetime(2026, 10, 7, 15, 30))
        scanner.finalize_session(datetime(2026, 10, 7, 15, 50))
        self.assertEqual(len(events), 1)
        self.assertEqual(len(scanner.confirmed_signals[1]), 1)
        self.assertEqual(scanner.confirmed_signals[1][0]['candle'], prior['candle'])
        self.assertIsNone(scanner._confirm_once(1, prior))
        neutral = {**prior, 'candle': {**prior['candle'], 'time': instant + 300},
                   'bearish': {'triggered': False}}
        result = scanner._confirm_once(1, neutral)
        self.assertIsNotNone(result)
        self.assertFalse(result['bullish']['triggered'])
        self.assertFalse(result['bearish']['triggered'])
        self.assertEqual(len(scanner.confirmed_signals[1]), 2)

    @unittest.skipUnless(hasattr(time, 'tzset'), 'Requires POSIX timezone support')
    def test_sdk_timestamp_normalization_is_host_timezone_independent(self):
        previous = os.environ.get('TZ')
        instant = datetime(2026, 10, 7, 3, 45, tzinfo=timezone.utc).timestamp()
        try:
            for zone in ('UTC', 'Asia/Kolkata', 'America/New_York'):
                os.environ['TZ'] = zone
                time.tzset()
                tick = {'exchange_timestamp': datetime.fromtimestamp(instant),
                        'last_price': 123.45}
                normalized = normalize_kite_tick(tick)
                self.assertEqual(market_datetime(normalized['exchange_timestamp']),
                                 datetime(2026, 10, 7, 9, 15))
                self.assertEqual(normalized['last_price'], tick['last_price'])
                self.assertEqual(normalized['exchange_timestamp'].timestamp(), instant)
                self.assertIsNone(tick['exchange_timestamp'].tzinfo)
        finally:
            if previous is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = previous
            time.tzset()

    def test_serialized_candle_keeps_start_instant_and_ohlc(self):
        from strategy_runtime import _candle_dict
        row = {'time': datetime(2026, 10, 7, 9, 15),
               'open': 100, 'high': 102, 'low': 99, 'close': 101, 'volume': 50}
        result = _candle_dict(row)
        self.assertEqual(result['time'], int(datetime(2026, 10, 7, 3, 45,
                                                    tzinfo=timezone.utc).timestamp()))
        for key in ('open', 'high', 'low', 'close', 'volume'):
            self.assertEqual(result[key], row[key])

    def test_last_regular_session_candle_start_labels(self):
        data = SymbolData(1, 'NSE:TEST')
        last_instant = datetime(2026, 10, 7, 15, 29, 59)
        for interval, minute in (('5m', 25), ('15m', 15), ('1h', 15)):
            self.assertEqual(data._get_bucket_time(last_instant, interval),
                             datetime(2026, 10, 7, 15, minute))
        self.assertEqual(data._get_bucket_time(last_instant, '1d'),
                         datetime(2026, 10, 7))

    def test_utc_timestamp_converts_to_ist_before_bucketing(self):
        utc = datetime(2026, 10, 7, 3, 45, tzinfo=timezone.utc)
        local = market_datetime(utc)
        self.assertEqual(local, datetime(2026, 10, 7, 9, 15))
        self.assertEqual(SymbolData(1, 'NSE:TEST')._get_bucket_time(local, '1h'), local)

    def test_dashboard_uses_thread_safe_ticker_mode(self):
        import inspect
        from scanner import RealtimeScanner

        signature = inspect.signature(RealtimeScanner.run)
        self.assertIn("threaded", signature.parameters)
        self.assertFalse(signature.parameters["threaded"].default)

    def test_nse_hourly_buckets_are_session_aligned(self):
        data = SymbolData(1, "NSE:TEST")
        self.assertEqual(
            data._get_bucket_time(datetime(2026, 10, 7, 9, 45), "1h"),
            datetime(2026, 10, 7, 9, 15),
        )
        self.assertEqual(
            data._get_bucket_time(datetime(2026, 10, 7, 10, 15), "1h"),
            datetime(2026, 10, 7, 10, 15),
        )

    def test_add_tick_reports_rollover_without_duplicate_candles(self):
        data = SymbolData(1, "NSE:TEST")
        data.intervals.add("5m")
        data.candles["5m"] = []
        data.last_tick_time["5m"] = None
        first = {"last_price": 100, "average_price": 99, "volume": 1000,
                 "timestamp": datetime(2026, 10, 7, 9, 15)}
        second = {"last_price": 101, "average_price": 100, "volume": 1020,
                  "timestamp": datetime(2026, 10, 7, 9, 20)}
        self.assertEqual(data.add_tick(first), set())
        self.assertEqual(data.add_tick(second), {"5m"})
        self.assertEqual(len(data.candles["5m"]), 2)
        self.assertEqual(data.candles["5m"][0]["close"], 100)

    def test_zero_kite_average_price_never_flattens_chart_or_vwap_rule(self):
        data = SymbolData(1, "NSE:TEST")
        data.intervals.add("5m")
        data.candles["5m"] = [{
            "time": datetime(2026, 10, 7, 9, 15), "open": 700,
            "high": 710, "low": 698, "close": 705, "volume": 1000,
            "vwap": 704.5, "cumulative_volume_start": 0,
            "cumulative_volume_end": 1000,
        }]
        data.last_tick_time["5m"] = datetime(2026, 10, 7, 9, 15)
        data.add_tick({"last_price": 706, "average_price": 0, "volume": 1010,
                       "timestamp": datetime(2026, 10, 7, 9, 16)})
        self.assertEqual(data.get_latest_vwap("5m"), 704.5)
        self.assertEqual(data.candles["5m"][-1]["vwap"], 704.5)

    def test_vwap_fallback_does_not_leak_across_sessions(self):
        data = SymbolData(1, "NSE:TEST")
        data.intervals.add("5m")
        data.candles["5m"] = [{
            "time": datetime(2026, 10, 6, 15, 25), "open": 700,
            "high": 705, "low": 699, "close": 704, "volume": 1000,
            "vwap": 702, "cumulative_volume_start": 0,
            "cumulative_volume_end": 1000,
        }]
        data.last_tick_time["5m"] = datetime(2026, 10, 6, 15, 25)
        data.add_tick({"last_price": 710, "average_price": 0, "volume": 5,
                       "timestamp": datetime(2026, 10, 7, 9, 15)})
        self.assertEqual(data.candles["5m"][-1]["vwap"], 710)

    def test_shared_evaluator_reports_each_rule_once(self):
        data = SymbolData(1, "NSE:TEST")
        data.intervals.add("5m")
        data.candles["5m"] = [{
            "time": datetime(2026, 10, 7, 9, 15), "open": 100,
            "high": 102, "low": 99, "close": 101, "volume": 1000,
            "vwap": 100,
        }]
        result = evaluate_side(
            "bullish", [Condition.parse("5-minute Close is above VWAP")],
            101, 100, data, 100,
        )
        self.assertTrue(result.triggered)
        self.assertEqual(result.passed, 1)
        self.assertEqual(result.total, 1)
        self.assertEqual(len(result.rules), 1)

    def test_chart_snapshot_has_strategy_overlays_and_levels(self):
        data = SymbolData(1, "NSE:TEST")
        times = pd.date_range("2026-10-07 09:15", periods=60, freq="5min")
        data.intervals.update({"5m", "1d"})
        data.candles["5m"] = [{
            "time": t.to_pydatetime(), "open": 100 + i, "high": 102 + i,
            "low": 99 + i, "close": 101 + i, "volume": 1000,
            "vwap": 100.5 + i,
        } for i, t in enumerate(times)]
        data.candles["1d"] = [{
            "time": datetime(2026, 10, 6), "open": 90, "high": 110,
            "low": 80, "close": 105, "volume": 10000, "vwap": 99,
        }]
        snapshot = chart_snapshot(data)
        self.assertEqual(len(snapshot["candles"]), 60)
        self.assertEqual(snapshot["timeframe"], "5m")
        self.assertEqual([item["id"] for item in snapshot["overlays"]],
                         ["ema20", "ema50", "vwap"])
        self.assertTrue(snapshot["overlays"][0]["points"])
        self.assertFalse(snapshot["overlays"][2]["default_visible"])
        self.assertEqual([item["id"] for item in snapshot["studies"]],
                         ["adx", "obv", "range_atr"])
        self.assertEqual(snapshot["studies"][0]["levels"][0]["value"], 22)
        self.assertEqual(snapshot["levels"]["opening_high"], 104)
        self.assertEqual(snapshot["levels"]["previous_high"], 110)

    def test_each_strategy_timeframe_has_matching_overlays(self):
        data = SymbolData(1, "NSE:TEST")
        times = pd.date_range("2026-07-01", periods=80, freq="5min")
        candles = [{
            "time": t.to_pydatetime(), "open": 100 + i / 10,
            "high": 102 + i / 10, "low": 99 + i / 10,
            "close": 101 + i / 10, "volume": 1000, "vwap": 100 + i / 10,
        } for i, t in enumerate(times)]
        data.intervals.update({"5m", "15m", "1h", "1d"})
        data.candles = {interval: list(candles) for interval in data.intervals}
        self.assertEqual([x["id"] for x in chart_snapshot(data, "15m")["overlays"]],
                         ["haema5", "haema9"])
        self.assertEqual([x["id"] for x in chart_snapshot(data, "15m")["studies"]],
                         ["macd"])
        self.assertEqual([x["id"] for x in chart_snapshot(data, "1h")["overlays"]],
                         ["ema9", "ema50"])
        self.assertEqual([x["id"] for x in chart_snapshot(data, "1h")["studies"]],
                         ["rsi"])
        self.assertEqual([x["id"] for x in chart_snapshot(data, "1d")["overlays"]],
                         ["ema9", "ema50"])


if __name__ == "__main__":
    unittest.main()
