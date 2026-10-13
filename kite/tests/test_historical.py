import threading
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from historical import HistoricalRequests, HistoryCancelled
from scanner import SymbolData, RealtimeScanner


class Clock:
    def __init__(self):
        self.now = 0.
    def __call__(self):
        return self.now
    def sleep(self, duration):
        self.now += duration


class HistoricalRequestTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.manager = HistoricalRequests(clock=self.clock, sleep=self.clock.sleep, log=Mock())
        self.start = datetime(2026, 10, 8)
        self.end = datetime(2026, 10, 12, 10, 15)
        self.broker = Mock()
        self.broker.historical_data.return_value = [{'close': 100}]

    def fetch(self, token=1, interval='5minute', **kwargs):
        return self.manager.fetch(self.broker, token, self.start, self.end, interval, **kwargs)

    def test_shared_limit_covers_all_symbols_and_timeframes(self):
        times = []
        self.broker.historical_data.side_effect = lambda *args: times.append(self.clock()) or [{'close': 100}]
        for token in range(5):
            for interval in ('3minute', '5minute', '60minute', 'day'):
                self.fetch(token, interval)
        self.assertEqual(len(times), 20)
        self.assertTrue(all(later - earlier >= .5 for earlier, later in zip(times, times[1:])))

    def test_concurrent_identical_loads_use_one_request_and_private_cached_data(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.fetch())) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.broker.historical_data.assert_called_once()
        self.assertEqual(self.manager.cache_hits, 5)
        results[0][0]['close'] = 0
        self.assertEqual(self.fetch()[0]['close'], 100)
        self.clock.now += 61
        self.fetch()
        self.assertEqual(self.broker.historical_data.call_count, 2)

    def test_429_retries_back_off_and_following_request_is_also_limited(self):
        times = []
        def request(*args):
            times.append(self.clock())
            if len(times) < 3:
                raise RuntimeError('Too many requests')
            return [{'close': 100}]
        self.broker.historical_data.side_effect = request
        self.fetch()
        self.fetch(2)
        self.assertEqual(times, [0, 2, 6, 6.5])
        self.assertEqual(self.manager.retries, 2)

    def test_persistent_429_is_bounded_and_does_not_cache_failure(self):
        self.broker.historical_data.side_effect = RuntimeError('Too many requests')
        with self.assertRaisesRegex(RuntimeError, 'Too many requests'):
            self.fetch()
        self.assertEqual(self.broker.historical_data.call_count, 3)
        self.assertEqual(len(self.manager.cache), 0)
        self.broker.historical_data.side_effect = None
        self.fetch(2)
        self.assertEqual(self.clock(), 14)

    def test_non_rate_errors_not_retried_and_shutdown_interrupts_backoff(self):
        self.broker.historical_data.side_effect = RuntimeError('Invalid token')
        with self.assertRaisesRegex(RuntimeError, 'Invalid token'):
            self.fetch()
        self.broker.historical_data.assert_called_once()
        stop = threading.Event()
        def limited(*args):
            stop.set()
            raise RuntimeError('Too many requests')
        self.broker.historical_data.side_effect = limited
        with self.assertRaises(HistoryCancelled):
            self.fetch(stop=stop)
        self.assertEqual(self.broker.historical_data.call_count, 2)

    def test_startup_and_activation_do_not_initialize_same_interval_twice(self):
        entered, release = threading.Event(), threading.Event()
        data = SymbolData(1, 'TEST')
        broker = Mock()
        def initialize(*args):
            entered.set()
            release.wait(2)
            data.candles['5m'] = [{'close': 100}]
        scanner = RealtimeScanner.__new__(RealtimeScanner)
        scanner.symbol_data = {1: data}
        scanner.kite = broker
        scanner._stop_requested = threading.Event()
        prepared = []
        with patch.object(data, 'initialize_historical_candles', side_effect=initialize) as load, \
             patch.object(scanner, '_profile_intervals', return_value={'5m'}):
            startup = threading.Thread(target=data.check_and_add_intervals, args=(broker, {'5m'}))
            startup.start()
            self.assertTrue(entered.wait(1))
            activate = threading.Thread(target=lambda: prepared.append(scanner._prepare_profile_history({'id': 'test'})))
            activate.start()
            release.set()
            startup.join(2)
            activate.join(2)
            self.assertFalse(startup.is_alive())
            self.assertFalse(activate.is_alive())
            load.assert_called_once()
        self.assertEqual(prepared[0][1].intervals, set())
        # If startup/live ingestion fills a prepared interval before merge, keep it.
        candidate = SymbolData(1, 'TEST')
        candidate.intervals = {'5m'}
        candidate.candles['5m'] = [{'close': 90}]
        candidate.last_tick_time['5m'] = None
        scanner._merge_profile_history({1: candidate})
        self.assertEqual(data.candles['5m'], [{'close': 100}])
