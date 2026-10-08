import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from scanner import RealtimeScanner, SymbolData


class TestProcessingPipeline(unittest.TestCase):
    def scanner(self):
        scanner = RealtimeScanner.__new__(RealtimeScanner)
        scanner._evaluation_condition = threading.Condition()
        scanner._pending_evaluations = {}
        scanner._last_chart_publish = {}
        scanner.latest_market_results = {}
        scanner._last_evaluation_at = {}
        scanner._evaluation_interval = 5
        scanner.event_callback = Mock()
        scanner.check_reloads = Mock()
        scanner.get_required_intervals = Mock(return_value={'5m'})
        scanner.kite = None
        scanner.bullish_conditions = []
        scanner.bearish_conditions = []
        scanner.alert_threshold = 90
        scanner.symbol_data = {}
        for token in range(167):
            data = SymbolData(token, f'NFO:TEST{token}')
            data.intervals = {'5m'}
            data.candles = {'5m': []}
            data.last_tick_time = {'5m': None}
            scanner.symbol_data[token] = data
        return scanner

    def test_burst_updates_all_candles_without_running_evaluator(self):
        scanner = self.scanner()
        scanner._evaluate_snapshot = Mock(side_effect=AssertionError('ingestion must not evaluate'))
        start = time.monotonic()
        for step in range(20):
            for token in range(167):
                scanner._process_tick_locked({
                    'instrument_token': token, 'last_price': 100 + step,
                    'volume': step * 10, 'timestamp': datetime(2026, 10, 8, 13, 35) + timedelta(seconds=step),
                }, defer_evaluation=True)
        elapsed = time.monotonic() - start
        self.assertEqual(len(scanner._pending_evaluations), 167)
        for data in scanner.symbol_data.values():
            self.assertEqual(data.candles['5m'][-1]['close'], 119)
            self.assertEqual(data.candles['5m'][-1]['high'], 119)
        scanner._evaluate_snapshot.assert_not_called()
        print(f'Processed 3,340 ticks across 167 symbols in {elapsed:.3f}s')

    def test_rollover_retains_final_previous_snapshot(self):
        scanner = self.scanner()
        for minute, price in [(39, 100), (39, 110), (40, 120)]:
            scanner._process_tick_locked({
                'instrument_token': 0, 'last_price': price,
                'timestamp': datetime(2026, 10, 8, 13, minute),
            }, defer_evaluation=True)
        tasks = list(scanner._pending_evaluations.values())
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0][0].candles['5m'][-1]['close'], 110)
        self.assertEqual(tasks[1][0].candles['5m'][-1]['close'], 120)
        self.assertEqual(tasks[0][0].get_dataframe('5m').iloc[-1]['close'], 110)


if __name__ == '__main__':
    unittest.main()
