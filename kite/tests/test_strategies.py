import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from strategies import StrategyStore
from history import HistoryStore
from scanner import RealtimeScanner, SymbolData


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'strategies').mkdir()
        self.profile = {'name': 'Trend', 'threshold': 80, 'cooldown_seconds': 20,
                        'bullish': ['5-minute Close is above VWAP.'], 'bearish': []}
        (self.root / 'strategies' / 'trend.json').write_text(json.dumps(self.profile))
        (self.root / 'strategies' / 'default.json').write_text(json.dumps({**self.profile, 'name': 'Default', 'threshold': 70}))
        self.store = StrategyStore(self.root)

    def scanner(self):
        with patch('scanner.KITE_DIR', self.root), patch.object(RealtimeScanner, 'load_config'):
            scanner = RealtimeScanner()
        scanner.strategy_store = self.store
        return scanner

    def test_profile_validation_and_persistence(self):
        self.assertEqual(self.store.load('default')['threshold'], 70)
        self.assertEqual(self.store.load('trend')['bullish'][0].interval, '5m')
        self.store.save_selection('trend')
        self.assertEqual(StrategyStore(self.root).selected(), 'trend')
        for identifier in ('../trend', 'unknown'):
            with self.assertRaises((ValueError, OSError)):
                self.store.load(identifier)
        self.profile['bullish'] = ['invalid rule']
        (self.root / 'strategies' / 'trend.json').write_text(json.dumps(self.profile))
        with self.assertRaises(ValueError):
            self.store.load('trend')

    def test_switch_preserves_recording_work_and_failed_switch_preserves_active(self):
        scanner = self.scanner()
        scanner.latest_results[1] = {'old': True}
        scanner._pending_evaluations[1] = ('old',)
        scanner._prior_results = {1: {'old': True}}
        scanner.activate_strategy('trend')
        self.assertEqual(scanner.alert_threshold, 80)
        self.assertEqual(scanner.active_strategy['id'], 'trend')
        self.assertEqual(scanner._pending_evaluations, {1: ('old',)})
        self.assertEqual(scanner.latest_results, {})
        self.assertEqual(scanner._prior_results, {})
        generation = scanner._strategy_generation
        with self.assertRaises(OSError):
            scanner.activate_strategy('missing')
        self.assertEqual(scanner._strategy_generation, generation)
        self.assertEqual(self.store.selected(), 'trend')

    def test_nnf_view_preserves_stock_alert_strategy_across_restart(self):
        source = Path(__file__).resolve().parents[1] / 'strategies' / 'nnf.json'
        (self.root / 'strategies' / 'nnf.json').write_text(source.read_text())
        scanner = self.scanner()
        scanner.activate_strategy('trend')
        scanner.activate_strategy('nnf')
        self.assertEqual(set(scanner.strategy_info()['alert_strategies']), {'trend', 'nnf'})
        self.assertEqual(self.store.stock_alert_strategy(), 'trend')
        restored = self.scanner()
        self.assertEqual(restored.active_strategy['id'], 'nnf')
        self.assertEqual(set(restored.strategy_info()['alert_strategies']), {'trend', 'nnf'})
        restored.activate_strategy('default')
        self.assertEqual(set(restored.strategy_info()['alert_strategies']), {'default', 'nnf'})

    def test_legacy_nnf_selection_retains_default_stock_alerts(self):
        self.store.save_selection('nnf')
        self.assertEqual(self.store.stock_alert_strategy(), 'default')

    def test_missing_history_allows_activation_and_preserves_existing_candles(self):
        scanner = self.scanner()
        data = SymbolData(1, 'NFO:ICICIGI26OCTFUT')
        original = [{'close': 100}]
        data.intervals = {'5m'}
        data.candles['5m'] = original
        scanner.symbol_data[1] = data
        scanner.kite = object()
        with patch.object(SymbolData, 'initialize_historical_candles'), self.assertLogs('kite.strategy', level='WARNING') as logs:
            scanner.activate_strategy('trend')
        self.assertIs(data.candles['5m'], original)
        self.assertIn('1h', data.intervals)
        self.assertEqual(data.candles['1h'], [])
        self.assertEqual(scanner.active_strategy['id'], 'trend')
        self.assertEqual(self.store.selected(), 'trend')
        self.assertTrue(any('ICICIGI26OCTFUT' in message and '[1h]' in message for message in logs.output))

    def test_history_fetch_failures_capture_reason_and_do_not_block_activation(self):
        from unittest.mock import Mock
        scanner = self.scanner()
        data = SymbolData(1, 'NFO:ICICIGI26OCTFUT')
        scanner.symbol_data[1] = data
        scanner.kite = Mock()
        scanner.kite.historical_data.side_effect = RuntimeError('Temporary history request failure')
        with self.assertLogs('kite.strategy', level='WARNING') as logs:
            scanner.activate_strategy('trend')
        self.assertEqual(scanner.active_strategy['id'], 'trend')
        self.assertIn('Temporary history request failure', data.history_errors['1h'])
        self.assertTrue(any('Temporary history request failure' in message for message in logs.output))

    def test_history_migration_and_strategy_confirmation_isolation(self):
        import sqlite3
        path = self.root / 'old.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, token INTEGER, day TEXT, kind TEXT, candle INTEGER, payload TEXT)')
            db.execute("CREATE UNIQUE INDEX confirmed_once ON events(token,candle) WHERE kind='confirmed'")
        store = HistoryStore(path)
        event = {'instrument_token': 1, 'candle': {'time': 1791431100}}
        for strategy in ('default', 'trend', 'trend'):
            store.record('confirmed', {**event, 'strategy_id': strategy})
        day = store.days(1)[0]
        self.assertEqual(len(store.events(1, day, 'confirmed')), 2)
        self.assertEqual(len(store.events(1, day, 'confirmed', 'trend')), 1)

    def test_worker_discards_evaluation_popped_before_activation(self):
        scanner = self.scanner()
        scanner.activate_strategy('trend')
        scanner._pending_evaluations[1] = (None, 100, 100, None, (), (), 90, 0)
        with patch.object(scanner, '_evaluate_snapshot') as evaluate:
            worker = threading.Thread(target=scanner._process_evaluations)
            worker.start()
            # An evaluation scheduled under the previous generation is discarded.
            import time
            deadline = time.monotonic() + 1
            while scanner._pending_evaluations and time.monotonic() < deadline:
                time.sleep(.001)
            scanner._stop_requested.set()
            with scanner._evaluation_condition:
                scanner._evaluation_condition.notify_all()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            evaluate.assert_not_called()

    def test_invalid_live_edit_keeps_working_profile(self):
        scanner = self.scanner()
        scanner.activate_strategy('trend')
        (self.root / 'strategies' / 'trend.json').write_text('{broken')
        scanner.last_reload_check = 0
        scanner.check_reloads()
        self.assertEqual(scanner.active_strategy['id'], 'trend')
        self.assertEqual(scanner.alert_threshold, 80)
        self.assertIsNotNone(scanner.strategy_error)

    def test_default_json_preserves_every_original_parsed_rule(self):
        import hashlib
        root = Path(__file__).resolve().parents[1]
        profile = StrategyStore(root).load('default')
        self.assertEqual(profile['threshold'], 90)
        for side in ('bullish', 'bearish'):
            self.assertEqual(len(profile[side]), 14)
        # Frozen before removing the original files: detects changes to every
        # original rule's text, parsed type, timeframe and parameters.
        original_fingerprints = {
            'bullish': 'fa569fe8d42d27a436877c52ee0ebef4626267e6bc2fd59390271017f0aea7a3',
            'bearish': '0bf539a5c50405e02ba102533b980c2eff76adf1dc24523d77fdcb221e03d6ee',
        }
        for side, expected in original_fingerprints.items():
            parsed = json.dumps([vars(rule) for rule in profile[side]], sort_keys=True)
            self.assertEqual(hashlib.sha256(parsed.encode()).hexdigest(), expected)

    def test_default_uses_only_json_and_ignores_environment_threshold(self):
        with patch.dict('os.environ', {'ALERT_THRESHOLD': 'invalid'}):
            scanner = self.scanner()
        self.assertEqual(scanner.alert_threshold, 70)
        self.assertEqual(scanner.active_strategy['id'], 'default')
        self.assertEqual(self.store.profiles()[0]['name'], 'Default')

    def test_default_json_edits_reload_rules_and_threshold(self):
        scanner = self.scanner()
        path = self.root / 'strategies' / 'default.json'
        profile = json.loads(path.read_text())
        profile['threshold'] = 95
        profile['bullish'] = ['1-hour RSI (14) is greater than 55']
        path.write_text(json.dumps(profile))
        scanner.check_reloads()
        self.assertEqual(scanner.alert_threshold, 95)
        self.assertEqual(scanner.bullish_conditions[0].rule_type, 'rsi')
        self.assertEqual(scanner.bullish_conditions[0].interval, '1h')

    def test_activation_emits_global_event_and_logs_change_once(self):
        from unittest.mock import Mock
        scanner = self.scanner()
        scanner.event_callback = Mock()
        with self.assertLogs('kite.strategy', level='INFO') as logs:
            scanner.activate_strategy('trend')
        event = scanner.event_callback.call_args.args[0]
        self.assertEqual(event['type'], 'strategy_changed')
        self.assertEqual(event['active']['id'], 'trend')
        self.assertEqual(event['source'], 'dashboard activation')
        self.assertIn('threshold=80%', logs.output[0])
        self.assertIn('Default [default] -> Trend [trend]', logs.output[0])
        generation = scanner._strategy_generation
        scanner.event_callback.reset_mock()
        scanner.activate_strategy('trend')
        scanner.event_callback.assert_not_called()
        self.assertEqual(scanner._strategy_generation, generation)

    def test_file_reload_emits_event_and_invalid_edit_logs_once(self):
        from unittest.mock import Mock
        scanner = self.scanner()
        scanner.event_callback = Mock()
        path = self.root / 'strategies' / 'default.json'
        profile = json.loads(path.read_text())
        profile['threshold'] = 95
        path.write_text(json.dumps(profile))
        with self.assertLogs('kite.strategy', level='INFO') as logs:
            scanner.check_reloads()
        self.assertEqual(scanner.event_callback.call_args.args[0]['source'], 'rule file reload')
        self.assertIn('rule file reload', logs.output[0])
        path.write_text('{invalid')
        with self.assertLogs('kite.strategy', level='WARNING') as logs:
            for _ in range(3):
                scanner.last_reload_check = 0
                scanner.check_reloads()
        self.assertEqual(len(logs.output), 1)
