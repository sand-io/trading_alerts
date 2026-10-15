"""NNF history must not alter existing rule inputs, charts, or live candles."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd

from . import KITE_DIR
from .test_nnf_strategy import candles
from scanner import SymbolData, EvaluationSnapshot, RealtimeScanner, MARKET_TZ
from strategies import StrategyStore
from strategy_runtime import evaluate_side, profile_chart_snapshot
from conditions import calculate_heikin_ashi


class NNFCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, 13, 33, tzinfo=MARKET_TZ)
        self.records = {}
        for interval, days in [('1h', 150), ('15m', 30)]:
            rows = candles(interval, days).rename(columns={'time': 'date'}).to_dict('records')
            self.records[{'1h': '60minute', '15m': '15minute'}[interval]] = rows
        self.store = StrategyStore(KITE_DIR)

    def fetch(self, broker, token, start, end, interval, **kwargs):
        return [dict(row) for row in self.records[interval] if start <= row['date'] <= end]

    def initialize(self, data, extra=None):
        with patch('scanner.datetime', wraps=datetime) as clock, \
             patch('scanner.history_requests.fetch', side_effect=self.fetch):
            clock.now.return_value = self.now
            data.check_and_add_intervals(Mock(), {'1h', '15m'}, history_days=extra)

    def test_initial_warmup_matches_legacy_inputs_including_vwap(self):
        baseline, extended = SymbolData(1, 'NFO:TEST'), SymbolData(1, 'NFO:TEST')
        self.initialize(baseline)
        self.initialize(extended, {'1h': 240, '15m': 21})
        for interval in ('1h', '15m'):
            pd.testing.assert_frame_equal(baseline.get_dataframe(interval), extended.get_dataframe(interval))
            self.assertGreater(len(extended.get_nnf_dataframe(interval)), len(extended.get_dataframe(interval)))

    def test_upgrade_preserves_default_btst_results_charts_and_live_updates(self):
        baseline, extended = SymbolData(1, 'NFO:TEST'), SymbolData(1, 'NFO:TEST')
        self.initialize(baseline)
        self.initialize(extended)
        # Include the other inputs used by existing strategies.
        for data in (baseline, extended):
            for interval in ('3m', '5m'):
                rows = candles(interval, 4).loc[lambda frame: frame.time <= self.now.replace(tzinfo=None)]
                data.candles[interval] = rows.to_dict('records')
                data.intervals.add(interval)
                data.last_tick_time[interval] = data.candles[interval][-1]['time']
                for row in data.candles[interval]:
                    row.update(cumulative_volume_start=0, cumulative_volume_end=0)
            daily = data.get_dataframe('1h').groupby(pd.to_datetime(data.get_dataframe('1h')['time']).dt.date).last().reset_index(drop=True)
            data.candles['1d'] = daily.to_dict('records')
        self.initialize(extended, {'1h': 240, '15m': 21})
        for stamp in (self.now.replace(tzinfo=None), self.now.replace(tzinfo=None)+timedelta(minutes=12)):
            for data in (baseline, extended):
                data.add_tick({'timestamp': stamp, 'last_price': 170, 'volume': 500, 'average_price': 165})
            for interval in baseline.candles:
                pd.testing.assert_frame_equal(baseline.get_dataframe(interval), extended.get_dataframe(interval))
            for identifier in ('default', 'btst_setup'):
                profile = self.store.load(identifier)
                for side in ('bullish', 'bearish'):
                    self.assertEqual(evaluate_side(side, profile[side], 170, 165, EvaluationSnapshot(baseline), profile['threshold']),
                                     evaluate_side(side, profile[side], 170, 165, EvaluationSnapshot(extended), profile['threshold']))
                for interval in ('15m', '1h'):
                    self.assertEqual(profile_chart_snapshot(baseline, interval, profile),
                                     profile_chart_snapshot(extended, interval, profile))
        profile = self.store.load('nnf')
        chart = profile_chart_snapshot(extended, '4h', profile, limit=600)
        self.assertGreater(len(chart['candles']), len(extended.get_dataframe('4h')))
        ema200 = next(overlay for overlay in chart['overlays'] if overlay['id'] == 'ema200')
        self.assertTrue(ema200['points'])

    def test_extended_snapshot_does_not_share_mutable_candles(self):
        data = SymbolData(1, 'NFO:TEST')
        self.initialize(data, {'1h': 240, '15m': 21})
        snapshot = EvaluationSnapshot(data)
        before = snapshot.get_nnf_dataframe('1h').copy()
        data.nnf_candles['1h'][-1]['close'] = 99999
        pd.testing.assert_frame_equal(snapshot.get_nnf_dataframe('1h'), before)

    def test_activation_merge_does_not_replace_existing_history(self):
        data, candidate = SymbolData(1, 'NFO:TEST'), SymbolData(1, 'NFO:TEST')
        self.initialize(data)
        original = data.candles['1h']
        before = data.get_dataframe('1h').copy()
        self.initialize(candidate, {'1h': 240, '15m': 21})
        scanner = RealtimeScanner.__new__(RealtimeScanner)
        scanner.symbol_data = {1: data}
        scanner._merge_profile_history({1: candidate})
        self.assertIs(data.candles['1h'], original)
        pd.testing.assert_frame_equal(data.get_dataframe('1h'), before)
        self.assertGreater(len(data.get_nnf_dataframe('1h')), len(before))

    def test_faster_heikin_ashi_matches_original_recurrence_exactly(self):
        frame = candles('15m', 10)
        close = (frame['open'] + frame['high'] + frame['low'] + frame['close']) / 4.
        reference = [(frame['open'].iloc[0] + frame['close'].iloc[0]) / 2.]
        for index in range(1, len(frame)):
            reference.append((reference[-1] + close.iloc[index-1]) / 2.)
        pd.testing.assert_series_equal(calculate_heikin_ashi(frame)['open'],
            pd.Series(reference, index=frame.index, name='open'), check_exact=True)
