import threading
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from scanner import RealtimeScanner


class HistoryMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.scanner = RealtimeScanner.__new__(RealtimeScanner)
        self.scanner._retention_lock = threading.Lock()
        self.scanner.history = Mock()
        self.now = datetime(2026, 11, 3, 10)

    def test_dashboard_skips_cleanup_while_startup_is_pruning(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_prune(now):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release startup cleanup')

        self.scanner.history.prune.side_effect = slow_prune
        with patch('scanner.history_requests.persistent_cache') as cache:
            worker = threading.Thread(target=self.scanner.maintain_history, args=(self.now,))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.scanner.maintain_history(self.now)
                self.scanner.history.prune.assert_called_once_with(self.now)
                cache.prune.assert_not_called()
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.scanner.maintain_history(self.now)
            self.scanner.history.prune.assert_called_once_with(self.now)
            cache.prune.assert_called_once_with(self.now)
            self.assertEqual(self.scanner._retention_day, self.now.date())

    def test_failed_cleanup_releases_lock_and_retries(self):
        self.scanner.history.prune.side_effect = [RuntimeError('database is locked'), None]
        with patch('scanner.history_requests.persistent_cache') as cache, \
                patch('scanner.strategy_log.exception') as log:
            self.scanner.maintain_history(self.now)
            self.assertFalse(hasattr(self.scanner, '_retention_day'))
            cache.prune.assert_not_called()
            log.assert_called_once()
            self.scanner.maintain_history(self.now)
            self.assertEqual(self.scanner.history.prune.call_count, 2)
            cache.prune.assert_called_once_with(self.now)
            self.assertEqual(self.scanner._retention_day, self.now.date())
