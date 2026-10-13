import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from candle_cache import CandleCache, history_end
from historical import HistoricalRequests
from history import HistoryStore


class PersistentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.start = datetime(2026, 10, 9)
        self.end = datetime(2026, 10, 13, 16)
        self.broker = Mock()
        self.records = [dict(date=datetime(2026, 10, 13, 15, minute),
                             open=100, high=102, low=99, close=101, volume=10)
                        for minute in (20, 25)]
        self.broker.historical_data.return_value = self.records

    def manager(self):
        manager = HistoricalRequests(spacing=0, cache_size=0, log=Mock())
        manager.enable_persistence(self.path)
        return manager

    def test_restart_reuses_closed_session_without_broker_request(self):
        first = self.manager().fetch(self.broker, 1, self.start, self.end, '5minute')
        self.assertEqual(first, self.records)
        manager = self.manager()
        second = manager.fetch(self.broker, 1, self.start, self.end + timedelta(hours=2), '5minute')
        self.assertEqual(first, second)
        self.assertEqual(self.broker.historical_data.call_count, 1)
        self.assertEqual(manager.disk_hits, 1)

    def test_incremental_refresh_replaces_partial_candle_and_appends(self):
        manager = self.manager()
        manager.fetch(self.broker, 1, self.start, self.end.replace(hour=15, minute=26), '5minute')
        corrected = {**self.records[-1], 'close': 102}
        self.broker.historical_data.return_value = [corrected]
        result = manager.fetch(self.broker, 1, self.start, self.end, '5minute')
        self.assertEqual(self.broker.historical_data.call_args.args[1], self.records[-1]['date'])
        self.assertEqual(result, [self.records[0], corrected])
        self.assertEqual(manager.incremental_requests, 1)

    def test_failed_refresh_does_not_claim_cache_is_fresh(self):
        manager = self.manager()
        manager.fetch(self.broker, 1, self.start, self.end.replace(hour=15, minute=26), '5minute')
        self.broker.historical_data.side_effect = RuntimeError('network unavailable')
        with self.assertRaisesRegex(RuntimeError, 'network unavailable'):
            manager.fetch(self.broker, 1, self.start, self.end, '5minute')
        self.broker.historical_data.side_effect = None
        manager.fetch(self.broker, 1, self.start, self.end, '5minute')
        self.assertEqual(self.broker.historical_data.call_count, 3)

    def test_cache_expiry_and_indicator_lookback(self):
        cache = CandleCache(self.path)
        now = self.end
        older = {**self.records[0], 'date': now - timedelta(days=100)}
        start = now - timedelta(days=150)
        cache.save(1, 'day', start, now, [older], now)
        self.assertEqual(cache.load(1, 'day', start, now)[1], [older])
        self.assertIsNone(cache.load(1, 'day', start-timedelta(days=1), now))
        self.assertIsNone(cache.load(1, 'day', start, now+timedelta(days=7)))
        cache.prune(now+timedelta(days=7))
        self.assertIsNone(cache.load(1, 'day', start, now))

    def test_session_retention_keeps_today_and_six_previous_days(self):
        store = HistoryStore(self.path, retention_days=7)
        for days_ago in (0, 6, 7, 12):
            day = (self.end-timedelta(days=days_ago)).date().isoformat()
            with store.connect() as db:
                db.execute('INSERT INTO events(token,day,kind,payload) VALUES(1,?,\'evaluation\',\'{}\')', (day,))
            store.save_charts(1, day, {'5m': {'candles': []}})
            store.save_strategy_charts(1, day, 'default', {'5m': {'candles': []}})
        store.prune(self.end)
        self.assertEqual(store.days(1), ['2026-10-13', '2026-10-07'])
        for table in ('events', 'charts', 'strategy_charts'):
            with store.connect() as db:
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 2)

    def test_retention_defaults_to_today_and_accepts_env_setting(self):
        with patch.dict('os.environ', {}, clear=True):
            store = HistoryStore(self.path)
        self.assertEqual(store.retention_days, 1)
        for day in ('2026-10-12', '2026-10-13'):
            with store.connect() as db:
                db.execute('INSERT INTO events(token,day,kind,payload) VALUES(1,?,\'evaluation\',\'{}\')', (day,))
            store.save_charts(1, day, {'5m': {'candles': []}})
            store.save_strategy_charts(1, day, 'default', {'5m': {'candles': []}})
        store.prune(self.end)
        self.assertEqual(store.days(1), ['2026-10-13'])
        with store.connect() as db:
            for table in ('charts', 'strategy_charts'):
                self.assertEqual(db.execute(f'SELECT day FROM {table}').fetchall(), [('2026-10-13',)])
        with patch.dict('os.environ', {'KITE_HISTORY_RETENTION_DAYS': '3'}):
            self.assertEqual(HistoryStore(self.path).retention_days, 3)
        for invalid in ('0', '-1', 'abc', '', '1.5'):
            with self.subTest(value=invalid), patch.dict('os.environ', {'KITE_HISTORY_RETENTION_DAYS': invalid}):
                with self.assertRaisesRegex(ValueError, 'positive integer'):
                    HistoryStore(self.path)

    def test_session_end_is_stable_overnight_and_weekend(self):
        friday_close = datetime(2026, 10, 9, 15, 30)
        for now in (datetime(2026, 10, 9, 22), datetime(2026, 10, 10, 11),
                    datetime(2026, 10, 11, 20), datetime(2026, 10, 12, 8)):
            self.assertEqual(history_end(now), friday_close)
        now = datetime(2026, 10, 12, 11, 12)
        self.assertEqual(history_end(now), now)
