import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from history import HistoryStore


class HistoryTests(unittest.TestCase):
    def test_restart_retains_evaluations_alerts_and_unique_confirmations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            store = HistoryStore(path)
            stamp = int(datetime(2026, 10, 8, 9, 15, tzinfo=ZoneInfo('Asia/Kolkata')).timestamp())
            event = {'instrument_token': 1, 'candle': {'time': stamp},
                     'threshold': 70, 'bullish': {'rules': [{'text': 'old rule', 'passed': True}]}}
            store.record('evaluation', event)
            store.record('evaluation', {**event, 'threshold': 80})
            store.record('alert', {**event, 'alert_side': 'bullish'})
            store.record('confirmed', event)
            store.record('confirmed', {**event, 'threshold': 100})
            store.save_charts(1, '2026-10-08', {'5m': {'candles': [event['candle']]}})
            reopened = HistoryStore(path)
            self.assertEqual(reopened.days(1), ['2026-10-08'])
            self.assertEqual(len(reopened.events(1, '2026-10-08', 'evaluation')), 2)
            self.assertEqual(reopened.events(1, '2026-10-08', 'confirmed'), [event])
            self.assertEqual(reopened.events(1, '2026-10-08', 'alert')[0]['alert_side'], 'bullish')
            self.assertEqual(reopened.chart(1, '2026-10-08', '5m')['candles'], [event['candle']])
            self.assertEqual(reopened.events(2, '2026-10-08', 'evaluation'), [])
            self.assertIsNone(reopened.chart(1, '2026-10-09', '5m'))

    def test_review_endpoint_returns_saved_rules_and_chart_after_restart(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import dashboard
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            store = HistoryStore(path)
            stamp = int(datetime(2026, 10, 8, 15, 25, tzinfo=ZoneInfo('Asia/Kolkata')).timestamp())
            event = {'instrument_token': 1, 'candle': {'time': stamp}, 'threshold': 75,
                     'bullish': {'triggered': True, 'rules': [{'text': 'original rule', 'passed': True}]}}
            store.record('evaluation', event)
            store.record('confirmed', event)
            store.record('alert', {**event, 'alert_side': 'bullish'})
            store.save_charts(1, '2026-10-08', {'5m': {'candles': [event['candle']], 'studies': []}})
            with patch.object(dashboard, 'scanner', SimpleNamespace(history=HistoryStore(path))):
                result = dashboard.history_day(1, '2026-10-08')
                self.assertEqual(result['strategy'], event)
                self.assertEqual(result['threshold'], 75)
                self.assertEqual(result['signals'], [event])
                self.assertEqual(len(result['alerts']), 1)
                self.assertEqual(dashboard.history_dates(1), ['2026-10-08'])
                with self.assertRaises(dashboard.HTTPException) as error:
                    dashboard.history_day(1, '2026-10-09')
                self.assertEqual(error.exception.status_code, 404)

    def test_scanner_records_alert_only_when_notifier_accepts_it(self):
        from unittest.mock import Mock, patch
        from scanner import RealtimeScanner, SymbolData, MARKET_TZ
        import threading
        with tempfile.TemporaryDirectory() as directory:
            scanner = RealtimeScanner.__new__(RealtimeScanner)
            scanner.history = HistoryStore(Path(directory) / 'history.sqlite3')
            scanner._archived_chart_candles = {}
            scanner.latest_results = {}
            scanner.confirmed_signals = {}
            scanner._finalized_candles = {}
            scanner._evaluation_lock = threading.RLock()
            scanner._process_lock = threading.RLock()
            scanner.event_callback = None
            condition = Mock(raw_text='saved matching rule')
            condition.evaluate.return_value = True
            scanner.bullish_conditions = [condition]
            scanner.bearish_conditions = []
            scanner.alert_threshold = 70
            data = SymbolData(1, 'NSE:TEST')
            stamp = datetime.now(MARKET_TZ).replace(tzinfo=None)
            # Use a valid session candle independently of the test execution time.
            candle_time = stamp.replace(hour=15, minute=25, second=0, microsecond=0)
            data.candles['5m'] = [{'time': candle_time, 'open': 100, 'high': 101,
                                  'low': 99, 'close': 100, 'volume': 10, 'vwap': 100}]
            scanner.symbol_data = {1: data}
            with patch('scanner.notifier.send_alert', side_effect=[True, False]):
                scanner._evaluate_snapshot(data, 100, 100, stamp)
                scanner._evaluate_snapshot(data, 100, 100, stamp)
            scanner.finalize_session(candle_time.replace(hour=15, minute=31))
            day = candle_time.date().isoformat()
            self.assertEqual(len(scanner.history.events(1, day, 'evaluation')), 2)
            self.assertEqual(len(scanner.history.events(1, day, 'alert')), 1)
            self.assertEqual(len(scanner.history.events(1, day, 'confirmed')), 1)
            self.assertTrue(scanner.history.chart(1, day, '5m')['candles'])
