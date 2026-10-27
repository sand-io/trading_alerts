"""NNF PDF semantics, timing, chart parameters and scanner/history integration."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from . import KITE_DIR
from conditions import calculate_stoch, calculate_heikin_ashi, calculate_macd
from nnf_strategy import (NNFSettings, NNFEvaluation, build_rules, completed_frame,
                          close_times, crossed, evaluation, history_days, trend_states)
from scanner import RealtimeScanner, SymbolData, EvaluationSnapshot, MARKET_TZ, resample_candles
from strategies import StrategyStore
from strategy_runtime import evaluate_side, profile_chart_snapshot


class Frames:
    def __init__(self, frames):
        self.frames = frames

    def get_dataframe(self, interval):
        if interval == '4h' and '1h' in self.frames:
            return resample_candles(self.frames['1h'])
        return self.frames.get(interval, pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume', 'vwap']))


def candles(interval, days=28):
    minutes = {'3m': 3, '5m': 5, '15m': 15, '1h': 60, '4h': 240}[interval]
    times = []
    for day in pd.bdate_range(end='2026-10-14', periods=days):
        day = day.to_pydatetime()
        times.extend(pd.date_range(day + timedelta(hours=9, minutes=15),
                                  day + timedelta(hours=15, minutes=29), freq=f'{minutes}min'))
    n = np.arange(len(times))
    close = 100 + n * .05 + np.sin(n * .3) * 2
    return pd.DataFrame({'time': times, 'open': close - .2, 'high': close + 1,
                         'low': close - 1, 'close': close, 'volume': 100 + n % 20, 'vwap': close - .1})


class NNFStrategyTests(unittest.TestCase):
    def setUp(self):
        self.store = StrategyStore(KITE_DIR)
        self.fast = NNFSettings('15m', '5m', '3m')
        self.slow = NNFSettings('4h', '1h', '15m')
        self.data = Frames({iv: candles(iv) for iv in ('3m', '5m', '15m', '1h')})
        self.data.frames['1h'] = candles('1h', 130)
        self.as_of = datetime(2026, 10, 14, 13, 33)

    def test_one_profile_requires_both_groups_and_counts_shared_filters_once(self):
        profile = self.store.load('nnf')
        nnf_profiles = [item for item in self.store.profiles() if item['id'].startswith('nnf')]
        self.assertEqual([item['id'] for item in nnf_profiles], ['nnf'])
        self.assertEqual(profile['confirmation_timeframe'], '3m')
        self.assertEqual(profile['threshold'], 100)
        self.assertTrue(profile['warnings'])
        for side in ('bullish', 'bearish'):
            rules = profile[side]
            self.assertEqual(len(rules), 16)
            self.assertEqual({rule.settings for rule in rules}, {self.slow, self.fast})
            self.assertEqual(sum(rule.gate == 'obv_cross' for rule in rules), 1)
            self.assertEqual(sum(rule.gate == 'vwap' for rule in rules), 1)
            # Prove that either group failing blocks the single combined signal.
            with patch('nnf_strategy.NNFCondition.evaluate_at', return_value=True):
                self.assertTrue(evaluate_side(side, rules, 200, 100, self.data, 100, as_of=self.as_of).triggered)
                for rule in rules:
                    with self.subTest(side=side, gate=rule.raw_text), patch.object(rule, 'evaluate_at', return_value=False):
                        self.assertFalse(evaluate_side(side, rules, 200, 100, self.data, 100, as_of=self.as_of).triggered)

    def test_literal_stochastic_is_identical_and_never_activates(self):
        frame = self.data.frames['15m']
        k, d = calculate_stoch(frame, 14, 3, 1)
        pd.testing.assert_series_equal(k, d)
        states = trend_states(frame, self.fast)
        self.assertTrue(any(state['ready'] for state in states))
        self.assertTrue(all(state['bias'] is None for state in states))
        view = NNFEvaluation(self.data, self.fast, self.as_of)
        self.assertEqual(view.missing, [])
        for side in ('bullish', 'bearish'):
            result = evaluate_side(side, build_rules(self.fast)[side], 200, 100, self.data, 100, as_of=self.as_of)
            self.assertFalse(result.triggered)
            self.assertFalse(result.rules[0].passed)

    def test_opposite_directions_in_the_groups_do_not_create_a_combined_alert(self):
        profile = self.store.load('nnf')
        def group_result(data, settings, as_of):
            side = 'bullish' if settings == self.slow else 'bearish'
            gates = {direction: {rule.gate: direction == side for rule in rules}
                     for direction, rules in build_rules(settings).items()}
            return SimpleNamespace(gates=gates)
        with patch('nnf_strategy.evaluation', side_effect=group_result):
            for side in ('bullish', 'bearish'):
                result = evaluate_side(side, profile[side], 200, 100, self.data, 100, as_of=self.as_of)
                self.assertFalse(result.triggered)
                self.assertFalse(any(rule.error for rule in result.rules))

    def test_split_profile_selection_resolves_to_the_combined_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'strategies').mkdir()
            (root / 'strategies/nnf.json').write_text((KITE_DIR / 'strategies/nnf.json').read_text())
            store = StrategyStore(root)
            for previous in ('nnf_4h_1h_15m', 'nnf_15m_5m_3m'):
                store.save_selection(previous)
                self.assertEqual(store.selected(), 'nnf')
                self.assertEqual(store.load(store.selected())['confirmation_timeframe'], '3m')

    def test_invalid_formula_threshold_timeframes_and_confirmation_are_rejected(self):
        raw = json.loads((KITE_DIR / 'strategies/nnf.json').read_text())
        variants = [{'threshold': 90}, {'confirmation_timeframe': '5m'},
                    {'stochastic': {'lookback': 14, 'k_smoothing': 3, 'd_smoothing': 3}},
                    {'timeframe_groups': [{'trend': '15m', 'momentum': '5m', 'execution': '3m'}]},
                    {'combination': 'any'}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'strategies').mkdir()
            for variant in variants:
                with self.subTest(variant=variant):
                    (root / 'strategies/nnf.json').write_text(json.dumps({**raw, **variant}))
                    with self.assertRaises(ValueError):
                        StrategyStore(root).load('nnf')

    def test_crossover_requires_a_transition_and_equality_is_not_a_signal(self):
        self.assertTrue(crossed(pd.Series([1., 3.]), pd.Series([2., 2.]), 'bullish'))
        self.assertTrue(crossed(pd.Series([3., 1.]), pd.Series([2., 2.]), 'bearish'))
        self.assertFalse(crossed(pd.Series([3., 4.]), pd.Series([2., 2.]), 'bullish'))
        self.assertFalse(crossed(pd.Series([1., 2.]), pd.Series([2., 2.]), 'bullish'))
        self.assertFalse(crossed(pd.Series([np.nan, 3.]), pd.Series([2., 2.]), 'bullish'))

    def test_counter_trend_remembers_context_and_waits_for_two_closes(self):
        # Inject distinct K/D solely to exercise the otherwise unreachable filter.
        frame = pd.DataFrame({'close': [12., 8., 7.]})
        ema_values = {9: pd.Series([10., 10., 10.]), 50: pd.Series([9., 9., 9.]), 200: pd.Series([6., 6., 6.])}
        with patch('nnf_strategy.calculate_ema', side_effect=lambda values, period: ema_values[period]), \
             patch('nnf_strategy.calculate_stoch', return_value=(pd.Series([2., 0., -1.]), pd.Series([1., 1., -2.]))):
            states = trend_states(frame, self.fast)
        self.assertEqual([state['context'] for state in states], ['bullish'] * 3)
        self.assertIsNone(states[1]['bias'])  # first opposite close + bearish crossover
        # A fresh crossover on the second opposite close can activate.
        with patch('nnf_strategy.calculate_ema', side_effect=lambda values, period: ema_values[period]), \
             patch('nnf_strategy.calculate_stoch', return_value=(pd.Series([2., 2., 0.]), pd.Series([1., 1., 1.]))):
            states = trend_states(frame, self.fast)
        self.assertEqual(states[-1]['bias'], 'bearish')
        # Mirror: established bearish stack, two closes above EMA9.
        frame['close'] = [4., 8., 9.]
        ema_values = {9: pd.Series([6.] * 3), 50: pd.Series([7.] * 3), 200: pd.Series([10.] * 3)}
        with patch('nnf_strategy.calculate_ema', side_effect=lambda values, period: ema_values[period]), \
             patch('nnf_strategy.calculate_stoch', return_value=(pd.Series([0., 0., 2.]), pd.Series([1.] * 3))):
            self.assertEqual(trend_states(frame, self.fast)[-1]['bias'], 'bullish')

    def test_counter_trend_crossover_waits_for_second_close_without_recross(self):
        for side, closes, oscillator in (
                ('bearish', [12., 8., 7., 7.], [2., 0., 0., 0.]),
                ('bullish', [4., 8., 9., 9.], [0., 2., 2., 2.])):
            emas = {9: pd.Series([10.] * 4), 50: pd.Series([9.] * 4),
                    200: pd.Series([6.] * 4)} if side == 'bearish' else {
                        9: pd.Series([6.] * 4), 50: pd.Series([7.] * 4),
                        200: pd.Series([10.] * 4)}
            with self.subTest(side=side), \
                    patch('nnf_strategy.calculate_ema', side_effect=lambda values, period: emas[period]), \
                    patch('nnf_strategy.calculate_stoch', return_value=(pd.Series(oscillator), pd.Series([1.] * 4))):
                states = trend_states(pd.DataFrame({'close': closes}), self.fast)
            self.assertEqual([state['bias'] for state in states], [None, None, side, None])

    def test_pending_counter_trend_cancels_on_recovery_or_stochastic_reversal(self):
        emas = {9: pd.Series([10.] * 4), 50: pd.Series([9.] * 4),
                200: pd.Series([6.] * 4)}
        for closes, oscillator in (([12., 8., 11., 7.], [2., 0., 0., 0.]),
                                   ([12., 8., 7., 7.], [2., 0., 1., 1.])):
            with self.subTest(closes=closes, oscillator=oscillator), \
                    patch('nnf_strategy.calculate_ema', side_effect=lambda values, period: emas[period]), \
                    patch('nnf_strategy.calculate_stoch', return_value=(pd.Series(oscillator), pd.Series([1.] * 4))):
                states = trend_states(pd.DataFrame({'close': closes}), self.fast)
            self.assertTrue(all(state['bias'] is None for state in states))

    def test_tangled_ema_context_bypasses_two_candle_rule(self):
        frame = pd.DataFrame({'close': [12., 8.]})
        ema_values = {9: pd.Series([10., 10.]), 50: pd.Series([9., 11.]), 200: pd.Series([6., 6.])}
        with patch('nnf_strategy.calculate_ema', side_effect=lambda values, period: ema_values[period]), \
             patch('nnf_strategy.calculate_stoch', return_value=(pd.Series([2., 0.]), pd.Series([1., 1.]))):
            state = trend_states(frame, self.fast)[-1]
        self.assertIsNone(state['context'])
        self.assertEqual(state['bias'], 'bearish')

    def test_completed_candles_and_filters_are_synced_without_future_data(self):
        original = NNFEvaluation(self.data, self.fast, self.as_of)
        self.assertEqual(original.cutoff, pd.Timestamp(self.as_of))
        self.assertEqual(original.candle['time'], pd.Timestamp('2026-10-14 13:30'))
        self.assertEqual(completed_frame(self.data, '15m', self.as_of).iloc[-1]['time'], pd.Timestamp('2026-10-14 13:15'))
        self.assertEqual(completed_frame(self.data, '5m', self.as_of).iloc[-1]['time'], pd.Timestamp('2026-10-14 13:25'))
        for iv, frame in self.data.frames.items():
            mask = close_times(frame, iv) > self.as_of
            frame.loc[mask, ['open', 'high', 'low', 'close', 'volume']] = 999999
        changed = NNFEvaluation(self.data, self.fast, self.as_of)
        self.assertEqual(original.gates, changed.gates)
        # The last observation in a still-forming execution candle is excluded.
        early = NNFEvaluation(self.data, self.fast, datetime(2026, 10, 14, 13, 32, 59))
        self.assertEqual(early.candle['time'], pd.Timestamp('2026-10-14 13:27'))

    def test_combined_rules_anchor_to_last_available_three_minute_close(self):
        profile = self.store.load('nnf')
        baseline = Frames({interval: frame.copy() for interval, frame in self.data.frames.items()})
        baseline.frames['3m'] = baseline.frames['3m'].loc[lambda frame: frame.time <= '2026-10-14 13:21']
        changed = Frames({interval: frame.copy() for interval, frame in baseline.frames.items()})
        cutoff = datetime(2026, 10, 14, 13, 24)
        # Even when the feed has stopped, later higher bars cannot leak into
        # the result for the last available 3m execution candle.
        for interval, frame in changed.frames.items():
            frame.loc[close_times(frame, interval) > cutoff, ['open', 'high', 'low', 'close', 'volume']] = 999999
        for side in ('bullish', 'bearish'):
            before = evaluate_side(side, profile[side], 200, 100, baseline, 100, as_of=datetime(2026, 10, 14, 20))
            after = evaluate_side(side, profile[side], 200, 100, changed, 100, as_of=datetime(2026, 10, 14, 20))
            self.assertEqual(before, after)

    def test_neutral_momentum_resets_bias_until_next_trend_close_and_bias_expires(self):
        def active_states(frame, settings):
            return [{'context': 'bullish', 'bias': 'bullish', 'ready': True}] * len(frame)
        def neutral_then_high(values, period):
            result = pd.Series(70., index=values.index)
            times = self.data.frames['5m'].iloc[:len(values)]['time']
            result.loc[times == pd.Timestamp('2026-10-14 13:25')] = 50.
            return result
        with patch('nnf_strategy.trend_states', side_effect=active_states), \
             patch('nnf_strategy.calculate_rsi', side_effect=neutral_then_high):
            view = NNFEvaluation(self.data, self.fast, self.as_of)
            self.assertIsNone(view.bias)  # neutral at 13:30 resets the new 13:30 bias
        with patch('nnf_strategy.trend_states', side_effect=active_states), \
             patch('nnf_strategy.calculate_rsi', side_effect=lambda values, period: pd.Series(70., index=values.index)):
            view = NNFEvaluation(self.data, self.fast, self.as_of)
            self.assertEqual(view.bias, 'bullish')
            self.data.frames['15m'] = self.data.frames['15m'].loc[self.data.frames['15m']['time'] < '2026-10-14 13:15']
            view = NNFEvaluation(self.data, self.fast, self.as_of)
            self.assertIsNone(view.bias)  # 13:15 bias expired at 13:30

    def test_execution_uses_ha_macd_and_strict_mirrored_histograms(self):
        execution = completed_frame(self.data, '3m', self.as_of)
        ha = calculate_heikin_ashi(execution)
        with patch('nnf_strategy.calculate_macd', wraps=calculate_macd) as macd:
            NNFEvaluation(self.data, self.fast, self.as_of)
        pd.testing.assert_frame_equal(macd.call_args.args[0], ha)
        for hist, side in [([1., 2.], 'bullish'), ([-1., -2.], 'bearish')]:
            with self.subTest(side=side), patch('nnf_strategy.calculate_macd', return_value=(pd.Series(hist), pd.Series([0., 0.]))):
                view = NNFEvaluation(self.data, self.fast, self.as_of)
                self.assertTrue(view.gates[side]['ha_macd'])
                self.assertTrue(view.gates[side]['ha_histogram'])
                opposite = 'bearish' if side == 'bullish' else 'bullish'
                self.assertFalse(view.gates[opposite]['ha_histogram'])
        with patch('nnf_strategy.calculate_macd', return_value=(pd.Series([0., 1.]), pd.Series([0., 0.]))):
            self.assertFalse(NNFEvaluation(self.data, self.fast, self.as_of).gates['bullish']['ha_histogram'])

    def test_chart_displays_literal_stochastic_ema200_ha_macd_and_rsi_smoothing(self):
        profile = self.store.load('nnf')
        trend = profile_chart_snapshot(self.data, '15m', profile, limit=20)
        self.assertEqual({item['id'] for item in trend['overlays']}, {'ema9', 'ema50', 'ema200', 'haema5', 'haema9'})
        self.assertTrue(all(overlay['points'] for overlay in trend['overlays']))
        k, d = next(study for study in trend['studies'] if study['id'] == 'stoch14_3_1')['lines']
        self.assertEqual(k['points'], d['points'])
        momentum = profile_chart_snapshot(self.data, '5m', profile)
        self.assertEqual(len(momentum['studies'][0]['lines']), 2)
        execution = profile_chart_snapshot(self.data, '3m', profile)
        self.assertEqual({item['id'] for item in execution['overlays']}, {'haema5', 'haema9'})
        self.assertEqual([item['id'] for item in execution['studies']], ['ha_macd12_26_9', 'obv9'])
        self.assertEqual(len(execution['studies'][0]['lines']), 3)
        visible = {candle['time'] for candle in trend['candles']}
        self.assertTrue(all(point['time'] in visible for overlay in trend['overlays'] for point in overlay['points']))

    def test_scanner_records_closed_candle_immediately_once_and_never_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('scanner.KITE_DIR', Path(directory)), patch.object(RealtimeScanner, 'load_config'), \
                 patch.object(StrategyStore, 'load', return_value=self.store.load('nnf')):
                scanner = RealtimeScanner()
            scanner.config = {}
            scanner._archive_charts = Mock(return_value=True)
            data = SymbolData(1, 'MCX:GOLD26DECFUT')
            data.candles = {iv: frame.to_dict('records') for iv, frame in self.data.frames.items()}
            scanner.symbol_data = {1: data}
            with patch('scanner.notifier.send_alert') as notify:
                for second in (0, 1):
                    event = scanner._evaluate_snapshot(EvaluationSnapshot(data), 200, 100,
                              self.as_of + timedelta(seconds=second))
                notify.assert_not_called()
            self.assertEqual(event['evaluation_mode'], 'closed_candle')
            self.assertTrue(event['data_ready'])
            expected = int(datetime(2026, 10, 14, 13, 30, tzinfo=MARKET_TZ).timestamp())
            self.assertEqual(event['candle']['time'], expected)
            saved = scanner.history.events(1, '2026-10-14', 'confirmed', 'nnf')
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]['candle']['time'], expected)
            self.assertFalse(saved[0]['bullish']['triggered'])
            self.assertEqual(len(saved[0]['nnf']['groups']), 2)
            self.assertEqual(saved[0]['bullish']['total'], 16)

    def test_missing_lookback_is_visible_and_extended_hourly_buffer_is_retained(self):
        self.assertEqual(history_days(build_rules(self.slow)['bullish']), {'1h': 240})
        short = Frames({'3m': candles('3m', 1)})
        view = NNFEvaluation(short, self.fast, self.as_of)
        self.assertIn('15m', view.missing)
        data = SymbolData(1, 'NFO:TEST')
        data.intervals = {'1h'}
        data.history_days = {'1h': 240}
        frame = candles('1h', 150)
        frame = frame.loc[frame['time'] < '2026-10-14 13:15']
        data.nnf_candles['1h'] = frame.to_dict('records')
        data.candles['1h'] = [dict(row) for row in data.nnf_candles['1h'][-200:]]
        for row in data.candles['1h']:
            row['cumulative_volume_end'] = 0
        data.last_tick_time['1h'] = data.candles['1h'][-1]['time']
        data.add_tick({'timestamp': datetime(2026, 10, 14, 13, 15), 'last_price': 200, 'volume': 500})
        self.assertGreater(len(data.nnf_candles['1h']), 1000)
        self.assertEqual(len(data.candles['1h']), 201)

    def test_history_upgrade_merges_older_bars_and_preserves_live_candle(self):
        data = SymbolData(1, 'NFO:TEST')
        stamp = datetime(2026, 10, 14, 13, 15)
        live = dict(time=stamp, open=100, high=105, low=99, close=104, volume=100)
        data.candles['1h'] = [live]
        data.intervals = {'1h'}
        data.last_tick_time['1h'] = stamp
        def initialize(broker, iv):
            data.nnf_candles[iv] = [dict(live, time=stamp-timedelta(hours=1), close=98), dict(live, close=90)]
            data.candles[iv] = [dict(live, close=90)]
        with patch.object(data, 'initialize_historical_candles', side_effect=initialize) as fetch:
            data.check_and_add_intervals(object(), {'1h'}, history_days={'1h': 240})
            data.check_and_add_intervals(object(), {'1h'}, history_days={'1h': 240})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([row['close'] for row in data.candles['1h']], [104])
        self.assertEqual([row['close'] for row in data.nnf_candles['1h']], [98, 104])
        self.assertIs(data.candles['1h'][-1], live)
        self.assertEqual(data.loaded_history_days['1h'], 240)

    def test_failed_lookback_upgrade_is_retryable_and_keeps_live_rows(self):
        data = SymbolData(1, 'NFO:TEST')
        live = dict(time=datetime(2026, 10, 14, 13, 15), close=104)
        data.candles['1h'], data.intervals = [live], {'1h'}
        def unavailable(broker, iv):
            data.history_errors[iv] = 'Temporary API failure'
        with patch.object(data, 'initialize_historical_candles', side_effect=unavailable) as fetch:
            data.check_and_add_intervals(object(), {'1h'}, history_days={'1h': 240})
            data.check_and_add_intervals(object(), {'1h'}, history_days={'1h': 240})
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(data.candles['1h'], [live])
        self.assertNotIn('1h', data.loaded_history_days)

    def test_completed_bar_calculations_are_reused_across_snapshots(self):
        data = SymbolData(1, 'NFO:TEST')
        data.candles = {iv: frame.to_dict('records') for iv, frame in self.data.frames.items()}
        with patch('nnf_strategy.NNFEvaluation', wraps=NNFEvaluation) as compute:
            first = evaluation(EvaluationSnapshot(data), self.fast, self.as_of)
            second = evaluation(EvaluationSnapshot(data), self.fast, self.as_of+timedelta(seconds=1))
        self.assertIs(first, second)
        self.assertEqual(compute.call_count, 1)

    def test_session_finalizer_includes_last_completed_execution_bar_once(self):
        profile = self.store.load('nnf')
        with tempfile.TemporaryDirectory() as directory:
            with patch('scanner.KITE_DIR', Path(directory)), patch.object(RealtimeScanner, 'load_config'), \
                 patch.object(StrategyStore, 'load', return_value=profile):
                scanner = RealtimeScanner()
            scanner._archive_charts = Mock(return_value=True)
            data = SymbolData(1, 'MCX:GOLD26DECFUT')
            data.candles = {iv: frame.to_dict('records') for iv, frame in self.data.frames.items()}
            data._latest_exchange_time = datetime(2026, 10, 14, 15, 29, 59)
            scanner.symbol_data = {1: data}
            with patch('scanner.notifier.send_alert') as notify:
                scanner._evaluate_snapshot(EvaluationSnapshot(data), 200, 100, data._latest_exchange_time, alert=False)
                scanner.finalize_session(datetime(2026, 10, 14, 15, 30))
                self.assertNotIn(1, getattr(scanner, '_session_close_checked', {}))
                scanner.finalize_session(datetime(2026, 10, 14, 23, 30))
                scanner.finalize_session(datetime(2026, 10, 14, 23, 31))
                notify.assert_not_called()
            saved = scanner.history.events(1, '2026-10-14', 'confirmed', profile['id'])
            last = saved[-1]
            expected = int(datetime(2026, 10, 14, 15, 27, tzinfo=MARKET_TZ).timestamp())
            self.assertEqual(last['candle']['time'], expected)
            self.assertEqual(len([event for event in saved if event['candle']['time'] == expected]), 1)


if __name__ == '__main__':
    unittest.main()
