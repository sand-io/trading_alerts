"""Daily alert archive is independent of the current watchlist and selection."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from history import HistoryStore


class DailyAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.store = HistoryStore(self.path)
        self.day = '2026-10-27'
        stamp = int(datetime(2026, 10, 27, 10, tzinfo=ZoneInfo('Asia/Kolkata')).timestamp())
        self.event = {'instrument_token': 1, 'symbol': 'NFO:TEST26OCTFUT',
                      'strategy_id': 'default', 'strategy_name': 'Default strategy',
                      'candle': {'time': stamp}, 'alert_side': 'bullish',
                      'notification': {'title': 'Original title', 'message': 'Original content'}}
        self.store.record('evaluation', self.event)
        self.store.record('confirmed', self.event)
        self.store.record('alert', self.event)
        self.store.record('alert', {**self.event, 'instrument_token': 2,
                                   'symbol': 'MCX:GOLD26DECFUT', 'strategy_id': 'nnf',
                                   'strategy_name': 'NNF Multi-Timeframe', 'alert_side': 'bearish'})
        self.store.record('alert', {**self.event, 'instrument_token': 3,
                                   'strategy_id': 'btst_setup', 'strategy_name': 'BTST'})
        self.store.record('alert', {**self.event, 'candle': {'time': stamp - 86400}})

    def test_all_symbols_directions_and_strategies_survive_restart(self):
        store = HistoryStore(self.path)
        page = store.daily_alerts(self.day)
        self.assertEqual((page['total'], page['bullish'], page['bearish']), (3, 2, 1))
        self.assertEqual([item['event']['instrument_token'] for item in page['alerts']], [3, 2, 1])
        self.assertEqual({item['id'] for item in page['strategies']}, {'default', 'nnf', 'btst_setup'})
        self.assertEqual(store.alert_days(), ['2026-10-27', '2026-10-26'])
        saved = page['alerts'][-1]
        self.assertEqual(store.alert(saved['id']), saved)
        self.assertEqual(saved['event']['notification'], self.event['notification'])
        self.assertIsNone(store.alert(1))  # an evaluation is never an alert

    def test_filters_and_cursor_pagination_do_not_duplicate_or_omit_alerts(self):
        page = self.store.daily_alerts(self.day, limit=1)
        ids = []
        while page['alerts']:
            ids.extend(item['id'] for item in page['alerts'])
            if page['next_cursor'] is None:
                break
            page = self.store.daily_alerts(self.day, limit=1, before_id=page['next_cursor'])
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(self.store.daily_alerts(self.day, side='bearish')['total'], 1)
        self.assertEqual(self.store.daily_alerts(self.day, strategy_id='default')['total'], 1)
        self.assertEqual(self.store.daily_alerts(self.day, search='gold')['total'], 1)
        self.assertEqual(self.store.daily_alerts(self.day, search='BTST')['total'], 1)
        self.assertEqual(self.store.daily_alerts(self.day, search="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.store.daily_alerts('2026-10-28')['alerts'], [])

    def test_routes_validate_input_and_do_not_require_subscribed_symbols(self):
        import dashboard
        scanner = SimpleNamespace(history=self.store, symbol_data={}, token_to_symbol={})
        with patch.object(dashboard, 'scanner', scanner):
            page = dashboard.daily_alerts(self.day)
            self.assertEqual(page['total'], 3)
            self.assertEqual(dashboard.saved_alert(page['alerts'][0]['id']), page['alerts'][0])
            self.assertEqual(dashboard.alert_dates(), ['2026-10-27', '2026-10-26'])
            for kwargs in ({'day': 'bad'}, {'day': self.day, 'side': 'invalid'},
                           {'day': self.day, 'limit': 0}, {'day': self.day, 'limit': 201},
                           {'day': self.day, 'before_id': -1}):
                with self.subTest(kwargs=kwargs), self.assertRaises(dashboard.HTTPException) as error:
                    dashboard.daily_alerts(**kwargs)
                self.assertEqual(error.exception.status_code, 400)
            with self.assertRaises(dashboard.HTTPException) as error:
                dashboard.saved_alert(99999)
            self.assertEqual(error.exception.status_code, 404)

    def test_daily_archive_obeys_existing_retention(self):
        self.store.prune(datetime(2026, 10, 28, tzinfo=ZoneInfo('Asia/Kolkata')))
        self.assertEqual(self.store.alert_days(), [])
        self.assertEqual(self.store.daily_alerts(self.day)['total'], 0)

    def test_strategy_picker_includes_zero_alert_profiles_and_deleted_history(self):
        import dashboard
        store = SimpleNamespace(profiles=lambda: [
            {'id': 'default', 'name': 'Default'}, {'id': 'nnf', 'name': 'NNF'},
            {'id': 'btst_setup', 'name': 'BTST'}])
        scanner = SimpleNamespace(history=self.store, strategy_store=store)
        # This date has only Default alerts. All three must remain selectable.
        with patch.object(dashboard, 'scanner', scanner):
            result = dashboard.daily_alerts('2026-10-26')
            counts = {item['id']: item['alert_count'] for item in result['strategies']}
            self.assertEqual(counts, {'default': 1, 'nnf': 0, 'btst_setup': 0})
            empty = dashboard.daily_alerts('2026-10-26', strategy_id='nnf')
            self.assertEqual(empty['total'], 0)
            self.assertEqual(len(empty['strategies']), 3)
            # Removed profiles with saved alerts still belong in the picker.
            store.profiles = lambda: [{'id': 'default', 'name': 'Default'}]
            result = dashboard.daily_alerts(self.day)
            self.assertEqual({item['id'] for item in result['strategies']}, {'default', 'nnf', 'btst_setup'})
