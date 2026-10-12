"""PDF buy conditions and the user-requested bearish mirror."""
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from conditions import Condition
from strategies import StrategyStore
from strategy_runtime import evaluate_side


class Data:
    def __init__(self):
        self.frames = {}
        for timeframe, freq in [('1d', 'D'), ('1h', 'h')]:
            self.frames[timeframe] = pd.DataFrame({
                'time': pd.date_range('2026-01-01', periods=50, freq=freq),
                'open': 99., 'high': 101., 'low': 98., 'close': 100.,
                'volume': [100.] * 49 + [101.],
            })

    def get_dataframe(self, timeframe):
        return self.frames.get(timeframe, pd.DataFrame())


class BTSTTests(unittest.TestCase):
    def setUp(self):
        self.profile = StrategyStore(Path(__file__).resolve().parent).load('btst_setup')
        self.rules = self.profile['bullish']
        self.data = Data()

    def test_pdf_rule_mapping(self):
        self.assertEqual(self.profile['threshold'], 100)
        expected = [
            ('close_ema', '1d', {'op': '>', 'ema_period': 10}),
            ('rsi_bands', '1d', {'period': 14, 'lower': 40., 'upper': 45., 'breakout': 60.}),
            ('close_ema', '1h', {'op': '>', 'ema_period': 10}),
            ('macd', '1h', {'fast': 12, 'slow': 26, 'signal': 9, 'op': '>'}),
            ('obv_ema', '1h', {'ema_period': 11, 'op': '>'}),
            ('volume_previous_max', '1h', {'period': 3}),
            ('close_open', '1h', {'op': '>'}),
        ]
        self.assertEqual([(rule.rule_type, rule.interval, rule.params) for rule in self.rules], expected)

    def test_rsi_formula_boundaries_and_missing_data(self):
        for value, passes in [(39.99, False), (40, True), (44.99, True),
                              (45, False), (59.99, False), (60, True),
                              (80, True), (np.nan, False)]:
            with self.subTest(value=value), patch('conditions.calculate_rsi', return_value=pd.Series([value])):
                self.assertEqual(bool(self.rules[1].evaluate(100, 100, self.data)), passes)
        self.data.frames['1d'] = pd.DataFrame()
        self.assertFalse(self.rules[1].evaluate(100, 100, self.data))

    def test_volume_strictly_exceeds_each_prior_candle(self):
        frame = self.data.frames['1h']
        # A very large older candle is outside the required three-bar window.
        frame.loc[45, 'volume'] = 10000
        frame.loc[46:49, 'volume'] = [100, 200, 300, 301]
        self.assertTrue(self.rules[5].evaluate(100, 100, self.data))
        for volume in (300, 250, np.nan):
            frame.loc[49, 'volume'] = volume
            self.assertFalse(self.rules[5].evaluate(100, 100, self.data))
        frame.loc[49, 'volume'] = 301
        frame.loc[47, 'volume'] = np.nan
        self.assertFalse(self.rules[5].evaluate(100, 100, self.data))
        self.data.frames['1h'] = frame.tail(3)
        self.assertFalse(self.rules[5].evaluate(100, 100, self.data))

    def test_green_candle_is_strict(self):
        for price, passes in [(100, True), (99, False), (98, False)]:
            self.assertEqual(bool(self.rules[6].evaluate(price, 100, self.data)), passes)

    def test_all_seven_required_and_each_failed_gate_blocks_buy(self):
        # Exercise actual rule evaluation with deterministic indicator outputs.
        def ema(series, period):
            return pd.Series([0 if series.name == 'obv' else 99] * len(series))

        def obv(frame):
            return pd.Series([1.] * len(frame), name='obv')

        with patch('conditions.calculate_ema', side_effect=ema), \
             patch('conditions.calculate_rsi', return_value=pd.Series([60.])), \
             patch('conditions.calculate_macd', return_value=(pd.Series([2.]), pd.Series([1.]))), \
             patch('conditions.calculate_obv', side_effect=obv):
            result = evaluate_side('bullish', self.rules, 100, 100, self.data, 100)
            self.assertTrue(result.triggered)
            self.assertEqual(result.passed, 7)
            for index in range(7):
                with self.subTest(failed_gate=index), patch.object(self.rules[index], 'evaluate', return_value=False):
                    result = evaluate_side('bullish', self.rules, 100, 100, self.data, 100)
                    self.assertFalse(result.triggered)
                    self.assertEqual(result.passed, 6)

    def test_new_patterns_reject_invalid_counts_and_bounds(self):
        self.assertIsNone(Condition.parse('1-hour Volume is greater than each of the previous 0 candles'))
        self.assertIsNone(Condition.parse('1 day RSI (14) is greater than or equal to 45 and less than 40, or greater than or equal to 60'))

    def test_daily_and_hourly_ema_comparisons_are_strict(self):
        with patch('conditions.calculate_ema', return_value=pd.Series([100.])), \
             patch('conditions.calculate_rsi', return_value=pd.Series([60.])):
            for index in (0, 2):
                for price, expected in [(100, False), (99, False), (101, True)]:
                    with self.subTest(rule=index, price=price):
                        self.assertEqual(bool(self.rules[index].evaluate(price, 100, self.data)), expected)

    def test_macd_and_obv_accept_held_above_without_new_crossover(self):
        with patch('conditions.calculate_macd', return_value=(pd.Series([2., 2.]), pd.Series([1., 1.]))):
            self.assertTrue(self.rules[3].evaluate(100, 100, self.data))
        with patch('conditions.calculate_macd', return_value=(pd.Series([2., 1.]), pd.Series([1., 1.]))):
            self.assertFalse(self.rules[3].evaluate(100, 100, self.data))
        with patch('conditions.calculate_obv', return_value=pd.Series([2., 2.])), \
             patch('conditions.calculate_ema', return_value=pd.Series([1., 1.])):
            self.assertTrue(self.rules[4].evaluate(100, 100, self.data))
        with patch('conditions.calculate_obv', return_value=pd.Series([2., 1.])), \
             patch('conditions.calculate_ema', return_value=pd.Series([1., 1.])):
            self.assertFalse(self.rules[4].evaluate(100, 100, self.data))

    def test_bearish_rule_mapping(self):
        expected = [
            ('close_ema', '1d', {'op': '<', 'ema_period': 10}),
            ('rsi_bearish_bands', '1d', {'period': 14, 'lower': 55., 'upper': 60., 'breakdown': 40.}),
            ('close_ema', '1h', {'op': '<', 'ema_period': 10}),
            ('macd', '1h', {'fast': 12, 'slow': 26, 'signal': 9, 'op': '<'}),
            ('obv_ema', '1h', {'ema_period': 11, 'op': '<'}),
            ('volume_previous_max', '1h', {'period': 3}),
            ('close_open', '1h', {'op': '<'}),
        ]
        self.assertEqual([(rule.rule_type, rule.interval, rule.params)
                          for rule in self.profile['bearish']], expected)

    def test_bearish_rsi_boundaries_and_exact_reflection(self):
        bearish = self.profile['bearish'][1]
        for value, expected in [(0, True), (40, True), (40.01, False),
                                (55, False), (55.01, True), (60, True),
                                (60.01, False), (100, False),
                                (np.nan, False), (np.inf, False)]:
            with self.subTest(value=value), patch('conditions.calculate_rsi', return_value=pd.Series([value])):
                self.assertEqual(bool(bearish.evaluate(100, 100, self.data)), expected)
        for value in [0, 39.99, 40, 44.99, 45, 50, 55, 60, 100]:
            with patch('conditions.calculate_rsi', return_value=pd.Series([value])):
                bullish_passed = self.rules[1].evaluate(100, 100, self.data)
            with patch('conditions.calculate_rsi', return_value=pd.Series([100 - value])):
                self.assertEqual(bool(bearish.evaluate(100, 100, self.data)), bool(bullish_passed))
        self.data.frames['1d'] = pd.DataFrame()
        self.assertFalse(bearish.evaluate(100, 100, self.data))

    def test_all_seven_bearish_required_and_equality_fails(self):
        rules = self.profile['bearish']
        def ema(series, period):
            return pd.Series([0 if series.name == 'obv' else 101] * len(series))
        with patch('conditions.calculate_ema', side_effect=ema), \
             patch('conditions.calculate_rsi', return_value=pd.Series([40.])), \
             patch('conditions.calculate_macd', return_value=(pd.Series([-2., -2.]), pd.Series([-1., -1.]))), \
             patch('conditions.calculate_obv', return_value=pd.Series([-2., -2.], name='obv')):
            result = evaluate_side('bearish', rules, 98, 100, self.data, 100)
            self.assertTrue(result.triggered)
            self.assertEqual(result.passed, 7)
            for index in range(7):
                with self.subTest(failed_gate=index), patch.object(rules[index], 'evaluate', return_value=False):
                    result = evaluate_side('bearish', rules, 98, 100, self.data, 100)
                    self.assertFalse(result.triggered)
                    self.assertEqual(result.passed, 6)
        for price, expected in [(98, True), (99, False), (100, False)]:
            self.assertEqual(bool(rules[6].evaluate(price, 100, self.data)), expected)
        with patch('conditions.calculate_ema', return_value=pd.Series([100.])):
            for index in (0, 2):
                self.assertFalse(rules[index].evaluate(100, 100, self.data))
        with patch('conditions.calculate_macd', return_value=(pd.Series([1.]), pd.Series([1.]))):
            self.assertFalse(rules[3].evaluate(98, 100, self.data))
        with patch('conditions.calculate_obv', return_value=pd.Series([1.])), \
             patch('conditions.calculate_ema', return_value=pd.Series([1.])):
            self.assertFalse(rules[4].evaluate(98, 100, self.data))
