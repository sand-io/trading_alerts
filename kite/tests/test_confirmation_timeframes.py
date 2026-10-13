"""Strategy timeframes must govern confirmation, persisted history and chart studies."""
import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from history import HistoryStore
from scanner import RealtimeScanner, SymbolData, MARKET_TZ
from strategies import StrategyStore, CONFIRMATION_TIMEFRAMES
from strategy_runtime import live_study_values, historical_evaluation, profile_chart_snapshot, restore_missing_chart_studies


class ConfirmationTimeframeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'strategies').mkdir()
        self.store = StrategyStore(self.root)

    def profile(self, interval='3m', **extra):
        labels = {'1m': '1-minute', '3m': '3-minute', '5m': '5-minute', '15m': '15-minute',
                  '30m': '30-minute', '1h': '1-hour', '4h': '4-hour', '1d': '1 day'}
        raw = dict(name='Test', threshold=100, bullish=[labels[interval] + ' Close is greater than Open'], bearish=[])
        raw.update(extra)
        (self.root / 'strategies' / 'default.json').write_text(json.dumps(raw))
        return self.store.load('default')

    def scanner(self, profile):
        with patch('scanner.KITE_DIR', self.root), patch.object(RealtimeScanner, 'load_config'):
            scanner = RealtimeScanner()
        scanner.config = {}
        scanner.check_reloads = Mock()
        scanner._archive_charts = Mock(return_value=True)
        return scanner

    def data(self, stamp, intervals):
        data = SymbolData(1, 'NSE:TEST')
        durations = {'1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '1d': 1440}
        for interval in intervals:
            if interval == '4h':
                continue
            bucket = data._get_bucket_time(stamp, interval)
            data.intervals.add(interval)
            data.candles[interval] = [dict(time=bucket-timedelta(minutes=durations[interval]*(79-i)),
                open=100.+i, high=102.+i, low=99.+i, close=101.+i, volume=100.+i,
                vwap=100., cumulative_volume_start=0, cumulative_volume_end=200)
                for i in range(80)]
            data.last_tick_time[interval] = bucket
        return data

    def test_inference_override_and_validation(self):
        for interval in CONFIRMATION_TIMEFRAMES:
            with self.subTest(interval=interval):
                self.assertEqual(self.profile(interval)['confirmation_timeframe'], interval)
        mixed = ['3-minute Close is greater than Open', '1-hour Close is greater than Open']
        self.assertEqual(self.profile(bullish=mixed)['confirmation_timeframe'], '5m')
        self.assertEqual(self.profile(bullish=mixed, confirmation_timeframe='15m')['confirmation_timeframe'], '15m')
        for invalid in ['tick', '2m', '', None, []]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.profile(confirmation_timeframe=invalid)
        self.assertEqual(self.profile(bullish=['3-minute ADX (14) is greater than 22 and rising'])['confirmation_timeframe'], '3m')

    def test_confirmation_uses_each_interval_and_session_close(self):
        for interval in CONFIRMATION_TIMEFRAMES:
            with self.subTest(interval=interval):
                profile = self.profile(interval)
                scanner = self.scanner(profile)
                start = datetime(2026, 10, 12, 9, 15)
                data = self.data(start, {'5m', '1h', interval})
                scanner.symbol_data = {1: data}
                event = scanner._evaluate_snapshot(data, 180, 100, start, alert=False)
                self.assertEqual(event['confirmation_timeframe'], interval)
                self.assertEqual(event['candle']['time'], int(data.get_dataframe(interval).iloc[-1]['time'].replace(tzinfo=MARKET_TZ).timestamp()))
                scanner.finalize_session(start.replace(hour=15, minute=30))
                scanner.finalize_session(start.replace(hour=15, minute=31))
                records = [event for event in scanner.history.events(1, '2026-10-12', 'confirmed', 'default')
                           if event.get('confirmation_timeframe') == interval]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['confirmation_timeframe'], interval)
                fallback = historical_evaluation(data, profile['bullish'], [], 100, interval)
                self.assertEqual(fallback['candle'], event['candle'])

    def test_three_minute_boundary_retains_final_snapshot_before_five_minute_rollover(self):
        profile = self.profile()
        scanner = self.scanner(profile)
        data = self.data(datetime(2026, 10, 12, 9, 15), {'3m', '5m'})
        scanner.symbol_data = {1: data}
        scanner.get_required_intervals = Mock(return_value={'3m', '5m'})
        for minute, price in [(16, 180), (17, 190), (18, 200)]:
            scanner._process_tick_locked({'instrument_token': 1, 'last_price': price,
                'timestamp': datetime(2026, 10, 12, 9, minute), 'volume': 200}, defer_evaluation=True)
        tasks = list(scanner._pending_evaluations.values())
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0][0].candles['3m'][-1]['close'], 190)
        for task in tasks:
            scanner._evaluate_all_snapshot(*task[:4], profiles=task[8], allow_alerts=False)
        confirmed = scanner.history.events(1, '2026-10-12', 'confirmed', 'default')
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]['candle']['close'], 190)
        self.assertEqual(confirmed[0]['confirmation_timeframe'], '3m')
        self.assertEqual(scanner.latest_results[1]['candle']['close'], 200)

    def test_live_studies_follow_three_minute_profile(self):
        profile = self.profile(bullish=['3-minute ADX (14) is greater than 22 and rising'])
        data = self.data(datetime(2026, 10, 12, 9, 15), {'3m', '5m'})
        chart = profile_chart_snapshot(data, '3m', profile)
        live = live_study_values(data, '3m', profile)
        self.assertEqual(chart['studies'][0]['id'], 'adx14')
        self.assertEqual(live['studies'][0]['lines'][0]['value'], chart['studies'][0]['lines'][0]['points'][-1]['value'])

    def test_three_minute_and_four_hour_fallback_studies_use_chart_candles(self):
        data = self.data(datetime(2026, 10, 12, 9, 15), {'3m', '1h'})
        for timeframe, expected in [('3m', ['adx', 'obv', 'range_atr']), ('4h', ['rsi'])]:
            # Both unrelated rules and price-only rules need a lower-panel fallback.
            for rule_interval in ('5m', timeframe):
                with self.subTest(timeframe=timeframe, rule_interval=rule_interval):
                    profile = self.profile(rule_interval)
                    chart = profile_chart_snapshot(data, timeframe, profile)
                    self.assertEqual([s['id'] for s in chart['studies']], expected)
                    live = live_study_values(data, timeframe, profile)
                    candle_times = {c['time'] for c in chart['candles']}
                    for study, update in zip(chart['studies'], live['studies']):
                        for line, latest in zip(study['lines'], update['lines']):
                            self.assertTrue(line['points'])
                            self.assertTrue(all(p['time'] in candle_times for p in line['points']))
                            self.assertEqual(latest['value'], line['points'][-1]['value'])
                    old_chart = {**chart, 'studies': []}
                    restored = restore_missing_chart_studies(old_chart, timeframe)
                    self.assertEqual(restored['studies'], chart['studies'])
                    self.assertEqual(old_chart['studies'], [])
                    self.assertEqual(restored['candles'], chart['candles'])
                    self.assertIs(restore_missing_chart_studies(chart, timeframe), chart)

    def test_four_hour_profile_study_overrides_fallback(self):
        profile = self.profile('4h', bullish=['4-hour RSI (9) is greater than 60'])
        data = self.data(datetime(2026, 10, 12, 9, 15), {'1h'})
        chart = profile_chart_snapshot(data, '4h', profile)
        self.assertEqual([s['id'] for s in chart['studies']], ['rsi9'])
        self.assertEqual(chart['studies'][0]['levels'][0]['value'], 60)
        self.assertEqual(live_study_values(data, '4h', profile)['studies'][0]['id'], 'rsi9')
        self.assertIs(restore_missing_chart_studies(chart, '4h'), chart)

    def test_migration_retains_legacy_five_minute_confirmation_and_new_interval(self):
        path = self.root / 'legacy.sqlite3'
        event = {'instrument_token': 1, 'candle': {'time': 1791776700}, 'strategy_id': 'default'}
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, token INTEGER, day TEXT, kind TEXT, candle INTEGER, payload TEXT, strategy TEXT)')
            db.execute("CREATE UNIQUE INDEX confirmed_strategy_once ON events(token,candle,strategy) WHERE kind='confirmed'")
            db.execute('INSERT INTO events VALUES(1,1,?,\'confirmed\',?,?,\'default\')',
                       ('2026-10-12', event['candle']['time'], json.dumps(event)))
        store = HistoryStore(path)
        store.record('confirmed', {**event, 'confirmation_timeframe': '3m'})
        store.record('confirmed', {**event, 'confirmation_timeframe': '3m'})
        reopened = HistoryStore(path)
        records = reopened.events(1, '2026-10-12', 'confirmed', 'default')
        self.assertEqual([e.get('confirmation_timeframe', '5m') for e in records], ['5m', '3m'])

    def test_explicit_fifteen_minute_strategy_ignores_five_minute_rollover(self):
        profile = self.profile(bullish=['3-minute Close is greater than Open',
                                       '1-hour Close is greater than Open'], confirmation_timeframe='15m')
        scanner = self.scanner(profile)
        data = self.data(datetime(2026, 10, 12, 9, 15), {'3m', '5m', '15m', '1h'})
        scanner.symbol_data = {1: data}
        scanner.get_required_intervals = Mock(return_value=data.intervals)
        for minute in (19, 20):
            scanner._process_tick_locked({'instrument_token': 1, 'last_price': 200+minute,
                'timestamp': datetime(2026, 10, 12, 9, minute), 'volume': 200})
        self.assertEqual(scanner.history.events(1, '2026-10-12', 'confirmed'), [])
        scanner._process_tick_locked({'instrument_token': 1, 'last_price': 240,
            'timestamp': datetime(2026, 10, 12, 9, 30), 'volume': 200})
        confirmed = scanner.history.events(1, '2026-10-12', 'confirmed')
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]['confirmation_timeframe'], '15m')
        self.assertEqual(confirmed[0]['candle']['time'], int(datetime(2026, 10, 12, 9, 15, tzinfo=MARKET_TZ).timestamp()))

    def test_dashboard_snapshot_uses_native_candle_and_profile_indicators(self):
        import dashboard
        profile = self.profile(bullish=['3-minute ADX (14) is greater than 22 and rising'])
        scanner = self.scanner(profile)
        data = self.data(datetime(2026, 10, 12, 9, 15), {'3m', '5m'})
        scanner.symbol_data = {1: data}
        with patch.object(dashboard, 'scanner', scanner):
            result = dashboard.snapshot(1, '3m')
        self.assertEqual(result['confirmation_timeframe'], '3m')
        self.assertEqual(result['strategy']['confirmation_timeframe'], '3m')
        self.assertEqual(result['strategy']['candle'], result['candles'][-1])
        self.assertEqual(result['studies'][0]['id'], 'adx14')

    def test_first_live_tick_does_not_record_historical_candles_as_live(self):
        profile = self.profile()
        scanner = self.scanner(profile)
        data = self.data(datetime(2026, 10, 12, 15, 27), {'3m', '5m'})
        scanner.symbol_data = {1: data}
        scanner.get_required_intervals = Mock(return_value=data.intervals)
        scanner._process_tick_locked({'instrument_token': 1, 'last_price': 210,
            'timestamp': datetime(2026, 10, 13, 9, 16), 'volume': 200}, defer_evaluation=True)
        tasks = list(scanner._pending_evaluations.values())
        self.assertEqual(len(tasks), 1)
        for task in tasks:
            scanner._evaluate_all_snapshot(*task[:4], profiles=task[8], allow_alerts=False)
        self.assertEqual(scanner.history.events(1, '2026-10-12', 'evaluation'), [])

    def test_close_uses_last_ingested_tick_even_when_evaluation_was_throttled(self):
        scanner = self.scanner(self.profile())
        data = self.data(datetime(2026, 10, 12, 15, 27), {'3m', '5m'})
        scanner.symbol_data = {1: data}
        scanner.get_required_intervals = Mock(return_value=data.intervals)
        with patch('scanner.time.monotonic', return_value=100):
            scanner._process_tick_locked({'instrument_token': 1, 'last_price': 180,
                'timestamp': datetime(2026, 10, 12, 15, 29, 56), 'volume': 200}, defer_evaluation=True)
            task = next(iter(scanner._pending_evaluations.values()))
            scanner._pending_evaluations.clear()
            scanner._evaluate_all_snapshot(*task[:4], profiles=task[8], allow_alerts=False)
            scanner._process_tick_locked({'instrument_token': 1, 'last_price': 210,
                'timestamp': datetime(2026, 10, 12, 15, 29, 59), 'volume': 250}, defer_evaluation=True)
        self.assertEqual(scanner.latest_results[1]['candle']['close'], 180)
        self.assertEqual(scanner._pending_evaluations, {})
        with patch('scanner.notifier.send_alert') as notify:
            scanner.finalize_session(datetime(2026, 10, 12, 15, 30))
            scanner.finalize_session(datetime(2026, 10, 12, 15, 31))
            notify.assert_not_called()
        saved = scanner.history.events(1, '2026-10-12', 'confirmed')
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['candle']['close'], 210)
        self.assertEqual(saved[0]['market_time'], '2026-10-12T15:29:59+05:30')

    def test_close_waits_for_pending_and_inflight_evaluations(self):
        scanner = self.scanner(self.profile())
        data = self.data(datetime(2026, 10, 12, 15, 27), {'3m', '5m'})
        scanner.symbol_data = {1: data}
        scanner._evaluate_all_snapshot(data, 180, 100, datetime(2026, 10, 12, 15, 29), allow_alerts=False)
        scanner._tick_queue.put([])
        scanner.finalize_session(datetime(2026, 10, 12, 15, 30))
        self.assertEqual(scanner.history.events(1, '2026-10-12', 'confirmed'), [])
        scanner._tick_queue.get_nowait()
        scanner._tick_queue.task_done()
        scanner._pending_evaluations['pending'] = ()
        scanner.finalize_session(datetime(2026, 10, 12, 15, 30))
        self.assertEqual(scanner.history.events(1, '2026-10-12', 'confirmed'), [])
        scanner._pending_evaluations.clear()
        scanner._evaluation_inflight = True
        scanner.finalize_session(datetime(2026, 10, 12, 15, 30))
        self.assertEqual(scanner.history.events(1, '2026-10-12', 'confirmed'), [])
        scanner._evaluation_inflight = False
        scanner.finalize_session(datetime(2026, 10, 12, 15, 30))
        self.assertEqual(len(scanner.history.events(1, '2026-10-12', 'confirmed')), 1)

    def test_history_keeps_evaluation_order_when_confirmation_changes_to_daily(self):
        store = HistoryStore(self.root / 'ordering.sqlite3')
        stamp = datetime(2026, 10, 12, 10, 0, tzinfo=MARKET_TZ)
        intraday = {'instrument_token': 1, 'confirmation_timeframe': '5m',
                    'candle': {'time': int(stamp.timestamp())}, 'market_time': stamp.isoformat()}
        daily = {**intraday, 'confirmation_timeframe': '1d',
                 'candle': {'time': int(stamp.replace(hour=0).timestamp())},
                 'market_time': (stamp+timedelta(minutes=1)).isoformat()}
        store.record('evaluation', intraday)
        store.record('evaluation', daily)
        self.assertEqual(store.events(1, '2026-10-12', 'evaluation'), [intraday, daily])
