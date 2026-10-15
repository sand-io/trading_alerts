"""Commodity subscriptions must not change the existing stock strategies."""
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from . import KITE_DIR
from candle_cache import history_end
from historical import HistoricalRequests
from instrument_scope import applies, nearest_futures
from market_sessions import session_bounds, session_is_open
from nnf_strategy import close_times, completed_frame
from scanner import RealtimeScanner, SymbolData, EvaluationSnapshot, MARKET_TZ
from strategies import StrategyStore


def future(token, symbol, expiry, **extra):
    return {'instrument_token': token, 'exchange': 'MCX', 'tradingsymbol': symbol,
            'instrument_type': 'FUT', 'expiry': expiry, **extra}


class CommodityScopeTests(unittest.TestCase):
    def setUp(self):
        store = StrategyStore(KITE_DIR)
        self.profiles = {identifier: store.load(identifier)
                         for identifier in ('default', 'btst_setup', 'nnf')}
        self.stock = SymbolData(1, 'NFO:HCLTECH26OCTFUT')
        self.gold = SymbolData(2, 'MCX:GOLD26DECFUT')
        self.crude = SymbolData(3, 'MCX:CRUDEOIL26OCTFUT')
        self.scanner = RealtimeScanner.__new__(RealtimeScanner)
        self.scanner.config = {'symbols': [self.stock.symbol]}
        self.scanner.recording_profiles = self.profiles
        self.scanner.active_strategy = self.profiles['default']
        self.scanner.symbol_data = {data.instrument_token: data for data in (self.stock, self.gold, self.crude)}
        self.scanner._stop_requested = threading.Event()
        self.scanner.kite = Mock()

    def test_only_standard_gold_and_crude_are_eligible_and_legacy_universe_is_preserved(self):
        for data in (self.gold, self.crude):
            self.assertTrue(self.scanner.profile_applies(self.profiles['nnf'], data))
            for identifier in ('default', 'btst_setup'):
                self.assertFalse(self.scanner.profile_applies(self.profiles[identifier], data))
        self.assertFalse(self.scanner.profile_applies(self.profiles['nnf'], self.stock))
        for identifier in ('default', 'btst_setup'):
            self.assertTrue(self.scanner.profile_applies(self.profiles[identifier], self.stock))
        for symbol in ('MCX:GOLDM26DECFUT', 'MCX:CRUDEOILM26OCTFUT', 'MCX:SILVER26DECFUT',
                       'MCX:GOLD26DEC100000CE', 'NFO:GOLD26DECFUT'):
            self.assertFalse(applies(self.profiles['nnf'], SymbolData(9, symbol)))
        # Explicit existing watchlist entries retain their old eligibility.
        self.scanner.config['symbols'].append(self.gold.symbol)
        self.assertTrue(self.scanner.profile_applies(self.profiles['default'], self.gold))

    def test_evaluation_routes_and_active_alert_ownership_are_isolated(self):
        self.scanner._evaluate_snapshot = Mock(return_value={'ok': True})
        for active, data, expected in (
                ('default', self.stock, ['default', 'btst_setup']),
                ('default', self.gold, ['nnf']),
                ('nnf', self.stock, ['default', 'btst_setup']),
                ('nnf', self.crude, ['nnf'])):
            self.scanner.active_strategy = self.profiles[active]
            self.scanner._evaluate_snapshot.reset_mock()
            self.scanner._evaluate_all_snapshot(data, 100, 99, datetime(2026, 10, 14, 12))
            calls = self.scanner._evaluate_snapshot.call_args_list
            self.assertEqual([call.kwargs['profile']['id'] for call in calls], expected)
            for call in calls:
                self.assertEqual(call.kwargs['alert'], call.kwargs['profile']['id'] == active)
                self.assertEqual(call.kwargs['publish'], call.kwargs['alert'])

    def test_extra_lookback_warms_only_commodities_and_copies_metadata(self):
        with patch.object(SymbolData, 'check_and_add_intervals', autospec=True) as warm:
            prepared = self.scanner._prepare_profile_history(self.profiles['nnf'])
        self.assertEqual(set(prepared), {2, 3})
        self.assertEqual(warm.call_count, 2)
        for call in warm.call_args_list:
            self.assertEqual(call.args[0].exchange, 'MCX')
            self.assertEqual(call.kwargs['history_days'], {'1h': 240, '15m': 21})
        self.assertEqual(self.stock.nnf_candles, {})

    def test_nearest_contract_resolution_excludes_expired_mini_options_and_wrong_exchange(self):
        today = date(2026, 10, 14)
        instruments = [future(2, 'GOLD26DECFUT', date(2026, 12, 4)),
                       future(3, 'GOLD27FEBFUT', date(2027, 2, 5)),
                       future(4, 'GOLD26OCTFUT', date(2026, 10, 5)),
                       future(5, 'GOLDM26OCTFUT', today),
                       future(6, 'GOLD26OCT100000CE', today, instrument_type='CE'),
                       future(7, 'CRUDEOIL26OCTFUT', today.isoformat()),
                       future(8, 'CRUDEOIL26NOVFUT', date(2026, 11, 19)),
                       future(9, 'GOLD26OCTFUT', today, exchange='NFO'),
                       future(10, 'GOLD26OCTFUT', '')]
        selected = nearest_futures(instruments, today)
        self.assertEqual({key: item['instrument_token'] for key, item in selected.items()},
                         {'GOLD': 2, 'CRUDEOIL': 7})

    def test_startup_adds_both_dashboard_symbols_without_editing_config(self):
        gold = future(2, 'GOLD26DECFUT', date(2026, 12, 4))
        crude = future(3, 'CRUDEOIL26OCTFUT', date(2026, 10, 20))
        stock = {'instrument_token': 1, 'exchange': 'NFO', 'tradingsymbol': self.stock.symbol.split(':')[1]}
        broker = Mock()
        broker.instruments.side_effect = lambda exchange: [stock] if exchange == 'NFO' else [gold, crude]
        self.scanner.symbol_data, self.scanner.token_to_symbol = {}, {}
        with patch('scanner.KiteConnect', return_value=broker), \
             patch.dict('os.environ', {'KITE_API_KEY': 'test', 'KITE_ACCESS_TOKEN': 'test'}), \
             patch('scanner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 14, tzinfo=MARKET_TZ)
            self.scanner.resolve_tokens()
        self.assertEqual(self.scanner.config['symbols'], [self.stock.symbol])
        self.assertEqual(set(self.scanner.token_to_symbol.values()), {self.stock.symbol, self.gold.symbol, self.crude.symbol})
        self.assertEqual(self.scanner.symbol_data[2].exchange, 'MCX')
        self.assertEqual(EvaluationSnapshot(self.scanner.symbol_data[2]).instrument, gold)
        later = SymbolData(9, 'MCX:GOLD27FEBFUT')
        self.assertFalse(self.scanner.profile_applies(self.profiles['nnf'], later))
        self.assertTrue(self.scanner.profile_applies(self.profiles['nnf'], self.scanner.symbol_data[2]))

    def test_strategy_chart_archives_follow_scope_and_base_charts_remain_available(self):
        self.scanner.history = Mock()
        event = {'candle': {'time': int(datetime(2026, 10, 14, 12, tzinfo=MARKET_TZ).timestamp())}}
        for data, expected in [(self.stock, ['default', 'btst_setup']), (self.gold, ['nnf'])]:
            self.scanner.history.reset_mock()
            with patch('scanner.chart_snapshot', return_value={'candles': []}), \
                 patch('scanner.profile_chart_snapshot', return_value={'candles': []}):
                self.assertTrue(self.scanner._archive_charts(data, event))
            self.scanner.history.save_charts.assert_called_once()
            self.assertEqual([call.args[2] for call in self.scanner.history.save_strategy_charts.call_args_list], expected)

    def test_missing_mcx_master_keeps_existing_subscriptions_running(self):
        stock = {'instrument_token': 1, 'exchange': 'NFO', 'tradingsymbol': self.stock.symbol.split(':')[1]}
        broker = Mock()
        def instruments(exchange):
            if exchange == 'MCX':
                raise RuntimeError('MCX unavailable')
            return [stock]
        broker.instruments.side_effect = instruments
        self.scanner.symbol_data, self.scanner.token_to_symbol = {}, {}
        with patch('scanner.KiteConnect', return_value=broker), \
             patch.dict('os.environ', {'KITE_API_KEY': 'test', 'KITE_ACCESS_TOKEN': 'test'}):
            self.scanner.resolve_tokens()
        self.assertEqual(self.scanner.token_to_symbol, {1: self.stock.symbol})
        self.assertEqual(set(self.scanner.instrument_errors), {'MCX', 'GOLD', 'CRUDEOIL'})

    def test_dashboard_clears_ineligible_rules_but_keeps_charts_and_eligible_studies(self):
        import dashboard
        for data in self.scanner.symbol_data.values():
            data.candles['5m'] = [dict(time=datetime(2026, 10, 14, 12),
                open=100, high=102, low=99, close=101, volume=10, vwap=100)]
        self.scanner.latest_results = {}
        self.scanner.history = Mock()
        self.scanner.history.events.return_value = []
        self.scanner.alert_threshold = 100
        for active, token, eligible in [('nnf', 1, False), ('default', 2, False), ('nnf', 2, True)]:
            profile = self.profiles[active]
            self.scanner.active_strategy = profile
            self.scanner.bullish_conditions, self.scanner.bearish_conditions = profile['bullish'], profile['bearish']
            with patch.object(dashboard, 'scanner', self.scanner):
                result = dashboard.snapshot(token)
            self.assertTrue(result['candles'])
            self.assertEqual(result['strategy_applicable'], eligible)
            if not eligible:
                self.assertIsNone(result['strategy'])
                self.assertEqual(result['signals'], [])
            else:
                self.assertEqual(result['strategy_id'], 'nnf')


class CommoditySessionTests(unittest.TestCase):
    def test_dst_weekends_and_stock_boundaries(self):
        summer, winter = datetime(2026, 10, 14, 20), datetime(2026, 12, 14, 20)
        self.assertEqual(session_bounds(summer, 'MCX')[1].time().isoformat(), '23:30:00')
        self.assertEqual(session_bounds(winter, 'MCX')[1].time().isoformat(), '23:55:00')
        self.assertTrue(session_is_open(summer, 'MCX'))
        self.assertFalse(session_is_open(summer, 'NFO'))
        self.assertFalse(session_is_open(datetime(2026, 10, 17, 20), 'MCX'))
        self.assertEqual(history_end(summer, 'MCX'), summer)
        self.assertEqual(history_end(summer), summer.replace(hour=15, minute=30))
        self.assertEqual(history_end(datetime(2026, 10, 15, 1), 'MCX'), summer.replace(hour=23, minute=30))

    def test_evening_ticks_and_hourly_four_hour_buckets_do_not_change_stocks(self):
        gold, stock = SymbolData(2, 'MCX:GOLD26DECFUT'), SymbolData(1, 'NFO:HCLTECH26OCTFUT')
        for data in (gold, stock):
            data.intervals = {'5m', '1h'}
            data.candles = {'5m': [], '1h': []}
            data.last_tick_time = {'5m': None, '1h': None}
            data.add_tick({'timestamp': datetime(2026, 10, 14, 20, 17), 'last_price': 100, 'volume': 10})
        self.assertEqual(stock.candles['5m'], [])
        self.assertEqual(gold.candles['1h'][-1]['time'], datetime(2026, 10, 14, 20))
        self.assertEqual(gold.get_dataframe('4h').iloc[-1]['time'], pd.Timestamp('2026-10-14 17:00'))
        self.assertEqual(gold._get_bucket_time(datetime(2026, 10, 14, 9, 14), '1h'), datetime(2026, 10, 14, 9))
        self.assertEqual(stock._get_bucket_time(datetime(2026, 10, 14, 10, 14), '1h'), datetime(2026, 10, 14, 9, 15))

    def test_completed_bars_respect_evening_and_shortened_final_bar(self):
        data = SymbolData(2, 'MCX:GOLD26DECFUT')
        data.candles['4h'] = []
        data.candles['1h'] = [dict(time=datetime(2026, 10, 14, 21), open=100, high=102, low=99,
                                  close=101, volume=10, vwap=100)]
        frame = data.get_dataframe('4h')
        self.assertEqual(close_times(frame, '4h', 'MCX')[0], pd.Timestamp('2026-10-14 23:30'))
        self.assertTrue(completed_frame(data, '4h', datetime(2026, 10, 14, 23, 29)).empty)
        self.assertEqual(len(completed_frame(data, '4h', datetime(2026, 10, 14, 23, 30))), 1)
        winter = pd.DataFrame({'time': [datetime(2026, 12, 14, 23, 54)]})
        self.assertEqual(close_times(winter, '3m', 'MCX')[0], pd.Timestamp('2026-12-14 23:55'))

    def test_persistent_cache_retains_evening_candles_and_reuses_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            broker = Mock()
            broker.historical_data.return_value = [dict(date=datetime(2026, 10, 14, 20),
                open=100, high=102, low=99, close=101, volume=10)]
            start, end = datetime(2026, 10, 13), datetime(2026, 10, 14, 23, 40)
            for cutoff in (end, end + timedelta(hours=1)):
                manager = HistoricalRequests(spacing=0, cache_size=0)
                manager.enable_persistence(path)
                result = manager.fetch(broker, 2, start, cutoff, '5minute', exchange='MCX')
                self.assertEqual(result, broker.historical_data.return_value)
            self.assertEqual(broker.historical_data.call_count, 1)
            self.assertEqual(broker.historical_data.call_args.args[2], datetime(2026, 10, 14, 23, 30))
