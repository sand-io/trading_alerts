"""Record all profiles while only the selected profile may notify."""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from scanner import RealtimeScanner, SymbolData, MARKET_TZ
from history import HistoryStore
from strategies import StrategyStore
from strategy_runtime import profile_chart_snapshot


class AllStrategyRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'strategies').mkdir()
        for identifier, period in [('default', 10), ('second', 20)]:
            profile = {'name': identifier.title(), 'threshold': 100,
                       'bullish': [f'5-minute Close is above 5-minute EMA ({period})'], 'bearish': []}
            (self.root / 'strategies' / f'{identifier}.json').write_text(json.dumps(profile))
        with patch('scanner.KITE_DIR', self.root), patch.object(RealtimeScanner, 'load_config'):
            self.scanner = RealtimeScanner()
        self.scanner.config = {}
        self.scanner.event_callback = Mock()
        self.stamp = datetime.now(MARKET_TZ).replace(tzinfo=None, second=0, microsecond=0)
        self.stamp = self.stamp.replace(minute=self.stamp.minute // 5 * 5)
        data = SymbolData(1, 'NSE:TEST')
        for interval, minutes in [('3m', 3), ('5m', 5), ('15m', 15), ('1h', 60), ('1d', 1440)]:
            data.intervals.add(interval)
            data.candles[interval] = [dict(time=self.stamp - timedelta(minutes=minutes * (49-i)),
                open=float(i+50), close=float(i+51), high=float(i+52), low=float(i+49), volume=100+i, vwap=99.)
                for i in range(50)]
            data.last_tick_time[interval] = self.stamp
        self.data = data
        self.scanner.symbol_data = {1: data}
        self.day = self.stamp.date().isoformat()

    def evaluate(self):
        return self.scanner._evaluate_all_snapshot(self.data, 100, 99, datetime.now(MARKET_TZ).replace(tzinfo=None))

    def rollover(self):
        self.stamp += timedelta(minutes=5)
        self.data.candles['5m'].append(dict(time=self.stamp, open=100., close=100., high=101., low=99., volume=100, vwap=99.))

    def test_all_profiles_record_and_only_active_profile_alerts_and_publishes(self):
        with patch('scanner.notifier.send_alert', return_value=True) as notify:
            event = self.evaluate()
            self.assertEqual(notify.call_count, 1)
            self.assertIn('default:', notify.call_args.kwargs['cooldown_key'])
        store = self.scanner.history
        for identifier in ('default', 'second'):
            events = store.events(1, self.day, 'evaluation', identifier)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]['bullish']['triggered'])
            self.assertEqual(events[0]['alerts_enabled'], identifier == 'default')
        self.assertEqual(event['strategy_id'], 'default')
        self.assertEqual(len(store.events(1, self.day, 'alert')), 1)
        self.scanner.event_callback.assert_called_once()
        self.assertEqual(self.scanner.event_callback.call_args.args[0]['strategy_id'], 'default')

    def test_failed_nnf_profile_does_not_stop_legacy_alerts_or_other_recordings(self):
        active = self.scanner.active_strategy
        second = self.scanner.recording_profiles['second']
        broken = {**second, 'id': 'nnf', 'engine': 'nnf'}
        evaluate = self.scanner._evaluate_snapshot
        def fail_nnf(*args, **kwargs):
            if kwargs['profile']['id'] == 'nnf':
                raise RuntimeError('NNF evaluation failure')
            return evaluate(*args, **kwargs)
        with patch.object(self.scanner, '_evaluate_snapshot', side_effect=fail_nnf), \
             patch('scanner.notifier.send_alert', return_value=True) as notify, \
             self.assertLogs('kite.strategy', level='ERROR'):
            event = self.scanner._evaluate_all_snapshot(self.data, 100, 99,
                datetime.now(MARKET_TZ).replace(tzinfo=None), profiles=(active, broken, second))
        self.assertEqual(event['strategy_id'], 'default')
        notify.assert_called_once()
        for identifier in ('default', 'second'):
            self.assertEqual(len(self.scanner.history.events(1, self.day, 'evaluation', identifier)), 1)
        self.assertEqual(self.scanner.processing_error, 'NNF evaluation failure')

    def test_failed_nnf_chart_does_not_stop_legacy_chart_recordings(self):
        active = self.scanner.active_strategy
        second = self.scanner.recording_profiles['second']
        broken = {**second, 'id': 'nnf', 'engine': 'nnf'}
        def fail_nnf(data, interval, profile, **kwargs):
            if profile['id'] == 'nnf':
                raise RuntimeError('NNF chart failure')
            return profile_chart_snapshot(data, interval, profile, **kwargs)
        event = {'candle': {'time': int(self.stamp.replace(tzinfo=MARKET_TZ).timestamp())}}
        with patch('scanner.profile_chart_snapshot', side_effect=fail_nnf), self.assertLogs('kite.strategy', level='ERROR'):
            self.assertFalse(self.scanner._archive_charts(self.data, event, profiles=(active, broken, second)))
        for identifier in ('default', 'second'):
            self.assertIsNotNone(self.scanner.history.strategy_chart(1, self.day, '5m', identifier))
        self.assertEqual(self.scanner.history_error, 'NNF chart failure')

    def test_active_alert_precedes_coalesced_chart_archive(self):
        self.scanner._archive_worker = Mock()
        self.scanner._archive_worker.is_alive.return_value = True
        with patch('scanner.notifier.send_alert', return_value=True) as notify, \
             patch.object(self.scanner, '_archive_charts') as archive:
            self.evaluate()
        notify.assert_called_once()
        archive.assert_not_called()
        archive_key = (1, self.stamp.date())
        self.assertEqual(list(self.scanner._pending_archives), [archive_key])
        self.assertEqual(len(self.scanner._pending_archives[archive_key][2]), 1)
        # Another matching evaluation in the same candle does not add work.
        with patch('scanner.notifier.send_alert', return_value=False):
            self.evaluate()
        self.assertEqual(list(self.scanner._pending_archives), [archive_key])

    def test_chart_archives_wait_for_live_evaluation_and_then_drain(self):
        saved = threading.Event()
        self.scanner._evaluation_inflight = True
        self.scanner._archive_charts = Mock(side_effect=lambda *args: saved.set() or True)
        self.start_persistence()
        with patch('scanner.notifier.send_alert', return_value=True) as notify:
            self.evaluate()
            notify.assert_called_once()
        self.assertFalse(saved.wait(.1))
        self.assertTrue(self.scanner._pending_archives)
        self.scanner._evaluation_inflight = False
        with self.scanner._archive_condition:
            self.scanner._archive_condition.notify_all()
        self.assertTrue(saved.wait(2))
        self.assertTrue(self.scanner.flush_background())

    def start_persistence(self):
        self.scanner._history_worker = threading.Thread(target=self.scanner._process_history, daemon=True)
        self.scanner._archive_worker = threading.Thread(target=self.scanner._process_chart_archives, daemon=True)
        self.scanner._history_worker.start()
        self.scanner._archive_worker.start()
        self.addCleanup(self.scanner.flush_background)

    def test_blocked_disk_and_charts_do_not_block_live_alerts_or_panels(self):
        entered, release = threading.Event(), threading.Event()
        original = self.scanner.history.record_many
        def blocked(events):
            entered.set()
            release.wait(5)
            return original(events)
        with patch.object(self.scanner.history, 'record_many', side_effect=blocked), \
             patch.object(self.scanner, '_archive_charts', side_effect=lambda *args: release.wait(5) or True), \
             patch('scanner.notifier.send_alert', return_value=True) as notify:
            self.start_persistence()
            try:
                self.evaluate()
                self.assertTrue(entered.wait(2))
                self.evaluate()
                self.assertEqual(notify.call_count, 2)
                self.assertEqual(self.scanner.event_callback.call_count, 2)
                self.assertGreater(self.scanner._history_queue.unfinished_tasks, 0)
            finally:
                release.set()
                self.assertTrue(self.scanner.flush_background())
        self.assertEqual(len(self.scanner.history.events(1, self.day, 'evaluation')), 4)
        self.assertEqual(len(self.scanner.history.events(1, self.day, 'alert')), 2)

    def test_queued_archive_captures_candles_and_strategy_versions(self):
        self.scanner._archive_worker = Mock()
        self.scanner._archive_worker.is_alive.return_value = True
        with patch('scanner.notifier.send_alert', return_value=False):
            self.evaluate()
        snapshot, event, _, profiles = next(iter(self.scanner._pending_archives.values()))
        old_close = snapshot.candles['5m'][-1]['close']
        old_version = profiles[0]['version']
        self.data.candles['5m'][-1]['close'] = 9999
        self.scanner.recording_profiles['default'] = {**profiles[0], 'version': 'new'}
        self.assertEqual(snapshot.candles['5m'][-1]['close'], old_close)
        self.assertEqual(profiles[0]['version'], old_version)
        self.assertEqual(event['candle']['close'], old_close)

    def test_multi_minute_old_matches_remain_recorded_without_alerts(self):
        with patch('scanner.notifier.send_alert') as notify:
            event = self.scanner._evaluate_all_snapshot(
                self.data, 100, 99, datetime.now(MARKET_TZ).replace(tzinfo=None)-timedelta(minutes=4))
        self.assertTrue(event['bullish']['triggered'])
        notify.assert_not_called()
        self.assertEqual(len(self.scanner.history.events(1, self.day, 'evaluation')), 2)

    def test_shutdown_drains_pending_evaluations_and_recordings_without_alerts(self):
        self.scanner._queue_evaluation(self.data, 100, 99, datetime.now(MARKET_TZ).replace(tzinfo=None))
        self.rollover()
        self.scanner._queue_evaluation(self.data, 100, 99, datetime.now(MARKET_TZ).replace(tzinfo=None))
        self.start_persistence()
        self.scanner._stop_requested.set()
        with patch('scanner.notifier.send_alert') as notify:
            self.scanner._evaluation_worker = threading.Thread(target=self.scanner._process_evaluations, daemon=True)
            self.scanner._evaluation_worker.start()
            self.assertTrue(self.scanner.flush_background(5))
            notify.assert_not_called()
        self.assertEqual(self.scanner._pending_evaluations, {})
        self.assertEqual(self.scanner._pending_archives, {})
        self.assertEqual(self.scanner._history_queue.unfinished_tasks, 0)
        self.assertEqual(len(self.scanner.history.events(1, self.day, 'evaluation')), 4)

    def test_transient_database_failure_retries_ordered_events(self):
        attempts = []
        original = self.scanner.history.record_many
        def retry(events):
            attempts.append(events)
            if len(attempts) == 1:
                raise OSError('temporary database failure')
            return original(events)
        with patch.object(self.scanner.history, 'record_many', side_effect=retry), \
             patch('scanner.notifier.send_alert', return_value=False), \
             self.assertLogs('kite.strategy', level='ERROR'):
            self.start_persistence()
            self.evaluate()
            self.assertTrue(self.scanner.flush_background(5))
        self.assertGreaterEqual(len(attempts), 2)
        events = self.scanner.history.events(1, self.day, 'evaluation')
        self.assertEqual([event['strategy_id'] for event in events], ['default', 'second'])
        self.assertEqual(self.scanner._history_queue.unfinished_tasks, 0)

    def test_selected_profile_is_evaluated_first(self):
        self.scanner.activate_strategy('second')
        order = []
        original = self.scanner._evaluate_snapshot
        with patch('scanner.notifier.send_alert', return_value=False), \
             patch.object(self.scanner, '_evaluate_snapshot', side_effect=lambda *a, **kw:
                          order.append(kw['profile']['id']) or original(*a, **kw)):
            self.evaluate()
        self.assertEqual(order, ['second', 'default'])

    def test_switch_preserves_both_strategies_candle_confirmations_and_reopens_history(self):
        with patch('scanner.notifier.send_alert', return_value=True):
            self.evaluate()
            original_state = self.scanner.strategy_states['default']
            self.scanner.activate_strategy('second')
            self.rollover()
            self.evaluate()
            self.scanner.finalize_session(self.stamp.replace(hour=15, minute=31))
        reopened = HistoryStore(self.scanner.history.path)
        for identifier in ('default', 'second'):
            events = reopened.events(1, self.day, 'evaluation', identifier)
            signals = reopened.events(1, self.day, 'confirmed', identifier)
            self.assertEqual(len(events), 2)
            self.assertEqual(len(signals), 2)
            self.assertTrue(all(signal['strategy_id'] == identifier for signal in signals))
            self.assertTrue(all(signal['bullish']['triggered'] for signal in signals))
        self.scanner.activate_strategy('default')
        self.assertIs(self.scanner.strategy_states['default'], original_state)
        self.assertEqual(len(self.scanner.confirmed_signals[1]), 2)
        self.assertEqual({item['id'] for item in reopened.session_strategies(1, self.day)}, {'default', 'second'})
        self.assertEqual([event['strategy_id'] for event in reopened.events(1, self.day, 'alert')], ['default', 'second'])

    def test_queued_work_survives_switch_but_cannot_send_stale_alerts(self):
        self.scanner._queue_evaluation(self.data, 100, 99, datetime.now(MARKET_TZ).replace(tzinfo=None))
        self.scanner.activate_strategy('second')
        self.assertEqual(len(self.scanner._pending_evaluations), 1)
        with patch('scanner.notifier.send_alert') as notify:
            worker = threading.Thread(target=self.scanner._process_evaluations)
            worker.start()
            deadline = time.monotonic()+3
            while len(self.scanner.history.events(1, self.day, 'evaluation')) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.scanner._stop_requested.set()
            with self.scanner._evaluation_condition:
                self.scanner._evaluation_condition.notify_all()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            notify.assert_not_called()
        self.assertEqual(len(self.scanner.history.events(1, self.day, 'evaluation')), 2)

    def test_saved_charts_and_review_are_specific_to_recorded_strategy(self):
        import dashboard
        with patch('scanner.notifier.send_alert', return_value=False):
            self.evaluate()
        (self.root / 'strategies' / 'second.json').unlink()
        self.scanner.refresh_recording_profiles()
        self.assertNotIn('second', self.scanner.recording_profiles)
        with patch.object(dashboard, 'scanner', self.scanner):
            options = dashboard.history_strategies(1, self.day)
            self.assertEqual({item['id'] for item in options}, {'default', 'second'})
            for identifier, period in [('default', 10), ('second', 20)]:
                result = dashboard.history_day(1, self.day, strategy_id=identifier)
                self.assertEqual(result['strategy']['strategy_id'], identifier)
                self.assertEqual(result['overlays'][0]['period'], period)
                self.assertEqual(len(result['evaluations']), 1)
        self.assertEqual(self.scanner.active_strategy['id'], 'default')

    def test_new_profile_recording_begins_on_discovery_and_invalid_edit_keeps_last_working_rules(self):
        with patch('scanner.notifier.send_alert'):
            self.evaluate()
        profile = {'name': 'Third', 'threshold': 100, 'bullish': ['1-hour Close is greater than Open'], 'bearish': []}
        path = self.root / 'strategies' / 'third.json'
        path.write_text(json.dumps(profile))
        self.scanner.refresh_recording_profiles()
        self.assertIn('third', self.scanner.recording_profiles)
        with patch('scanner.notifier.send_alert'):
            self.evaluate()
        self.assertEqual(len(self.scanner.history.events(1, self.day, 'evaluation', 'third')), 1)
        path.write_text('{invalid')
        self.scanner.refresh_recording_profiles()
        self.assertIn('third', self.scanner.profile_errors)
        self.assertIn('third', self.scanner.recording_profiles)

    def test_btst_archive_contains_its_actual_indicator_parameters(self):
        profile = StrategyStore(Path(__file__).resolve().parents[1]).load('btst_setup')
        hourly = profile_chart_snapshot(self.data, '1h', profile)
        self.assertEqual([overlay['period'] for overlay in hourly['overlays']], [10])
        self.assertEqual({study['id'] for study in hourly['studies']}, {'macd12_26_9', 'obv11', 'volume3'})
        daily = profile_chart_snapshot(self.data, '1d', profile)
        self.assertEqual([overlay['period'] for overlay in daily['overlays']], [10])
        self.assertEqual({level['value'] for level in daily['studies'][0]['levels']}, {40,45,55,60})

    def test_symbol_with_missing_required_history_cannot_trigger_but_healthy_symbol_can(self):
        profile_path = self.root / 'strategies' / 'second.json'
        profile = {'name': 'Warmup', 'threshold': 50,
                   'bullish': ['5-minute Close is greater than Open', '1-hour Close is greater than Open'], 'bearish': []}
        profile_path.write_text(json.dumps(profile))
        healthy = SymbolData(2, 'NSE:HEALTHY')
        healthy.intervals = set(self.data.intervals)
        healthy.candles = {iv: [dict(row) for row in rows] for iv, rows in self.data.candles.items()}
        healthy.last_tick_time = dict(self.data.last_tick_time)
        self.data.symbol = 'NFO:ICICIGI26OCTFUT'
        self.data.candles.pop('1h')
        self.data.intervals.remove('1h')
        self.scanner.symbol_data[2] = healthy
        self.scanner.kite = Mock()
        self.scanner.kite.historical_data.return_value = []
        with self.assertLogs('kite.strategy', level='WARNING'):
            self.scanner.activate_strategy('second')
        with patch('scanner.notifier.send_alert', return_value=True) as notify, \
             patch.object(self.scanner, '_archive_charts', return_value=True):
            stamp = datetime.now(MARKET_TZ).replace(tzinfo=None)
            cold = self.scanner._evaluate_all_snapshot(self.data, 100, 99, stamp)
            ready = self.scanner._evaluate_all_snapshot(healthy, 100, 99, stamp)
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[0], 'NSE:HEALTHY')
        self.assertEqual(cold['missing_timeframes'], ['1h'])
        self.assertFalse(cold['data_ready'])
        self.assertEqual(cold['bullish']['percentage'], 50)
        self.assertFalse(cold['bullish']['triggered'])
        self.assertTrue(ready['bullish']['triggered'])
        self.assertTrue(ready['data_ready'])
        # Real candle ingestion fills the missing interval; a red/green rule
        # needs one candle, so this profile becomes eligible on the next tick.
        self.data.candles['1h'] = [dict(healthy.candles['1h'][-1])]
        with patch('scanner.notifier.send_alert', return_value=True) as notify, \
             patch.object(self.scanner, '_archive_charts', return_value=True):
            warmed = self.scanner._evaluate_all_snapshot(self.data, 100, 99, stamp)
            self.assertTrue(warmed['data_ready'])
            self.assertTrue(warmed['bullish']['triggered'])
            notify.assert_called_once()
