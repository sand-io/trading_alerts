import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from mtf_alert.models import Direction, TrendState
from mtf_alert.state import StateStore
from mtf_alert.strategy import (
    PIPELINES,
    MacroTransition,
    MomentumStatus,
    Strategy,
    completed_frames,
    execution_confirms,
    macro_transition,
    momentum_status,
    trend_state,
    vwap_confirms,
)


IST = ZoneInfo("Asia/Kolkata")


def frame(interval: str, periods: int = 220) -> pd.DataFrame:
    frequency = {
        "3m": "3min", "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"
    }[interval]
    times = pd.date_range("2026-01-01 09:00", periods=periods, freq=frequency, tz=IST)
    return pd.DataFrame({
        "time": times, "open": range(periods), "high": range(1, periods + 1),
        "low": range(periods), "close": range(1, periods + 1),
        "volume": [100] * periods,
    })


class StrategyFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary.name) / "state.json")
        self.strategy = Strategy(self.store)
        self.now = datetime(2026, 3, 2, 13, 0, tzinfo=IST)
        self.frames = {
            name: frame(name) for name in ("3m", "5m", "15m", "1h", "4h")
        }
        intervals = {"3m": 3, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
        for name, values in self.frames.items():
            values["time"] = pd.date_range(
                end=self.now - timedelta(minutes=intervals[name]),
                periods=len(values), freq=f"{intervals[name]}min", tz=IST,
            )
        for pipeline in PIPELINES:
            self.store.set_bias(
                f"MCX:GOLD|{pipeline.name}", Direction.BULLISH.value,
                self.now - timedelta(minutes=1),
                self.now + timedelta(minutes=pipeline.bias_ttl_minutes),
            )

    def tearDown(self):
        self.temporary.cleanup()

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_both_pipelines_can_alert(self, *_mocks):
        alerts = self.strategy.evaluate("MCX:GOLD", self.frames, self.now)
        self.assertEqual({alert.pipeline for alert in alerts}, {"swing", "fast"})
        for alert in alerts:
            self.assertEqual(alert.price - alert.stop_loss, 1.0)
            self.assertEqual(alert.target - alert.price, 2.0)

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_live_scan_can_evaluate_fast_independently(self, *_mocks):
        fast = next(pipeline for pipeline in PIPELINES if pipeline.name == "fast")
        alerts = self.strategy.evaluate(
            "MCX:GOLD", self.frames, self.now, pipelines=(fast,)
        )
        self.assertEqual([alert.pipeline for alert in alerts], ["fast"])

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_swing_signal_does_not_require_a_three_minute_candle(self, *_mocks):
        self.frames["3m"] = self.frames["3m"].iloc[:0]
        swing = next(pipeline for pipeline in PIPELINES if pipeline.name == "swing")
        alerts = self.strategy.evaluate(
            "MCX:GOLD", self.frames, self.now, pipelines=(swing,)
        )
        self.assertEqual([alert.pipeline for alert in alerts], ["swing"])

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_owner_approved_mirrored_sell_alerts_and_two_r_targets(self, *_mocks):
        for pipeline in PIPELINES:
            self.store.set_bias(
                f"MCX:GOLD|{pipeline.name}", Direction.BEARISH.value,
                self.now - timedelta(minutes=1),
                self.now + timedelta(minutes=pipeline.bias_ttl_minutes),
            )
        for pipeline in PIPELINES:
            self.frames[pipeline.execution_interval].loc[219, "high"] += 2
        alerts = self.strategy.evaluate("MCX:GOLD", self.frames, self.now)
        self.assertEqual({alert.pipeline for alert in alerts}, {"swing", "fast"})
        for alert in alerts:
            self.assertEqual(alert.direction, Direction.BEARISH)
            self.assertEqual(alert.stop_loss - alert.price, 2.0)
            self.assertEqual(alert.price - alert.target, 4.0)

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.REJECTED)
    def test_failed_momentum_blocks_alert(self, *_mocks):
        self.assertEqual(self.strategy.evaluate("MCX:GOLD", self.frames, self.now), [])

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.NEUTRAL)
    def test_neutral_rsi_resets_bias(self, *_mocks):
        for pipeline in PIPELINES:
            self.store.set_macro(
                f"MCX:GOLD|{pipeline.name}",
                {"pending": Direction.BEARISH.value, "pending_closes": 1},
            )
        self.strategy.evaluate("MCX:GOLD", self.frames, self.now)
        self.assertIsNone(self.store.get_bias("MCX:GOLD|fast"))
        self.assertIsNone(self.store.get_bias("MCX:GOLD|swing"))
        self.assertIsNone(self.store.get_macro("MCX:GOLD|fast")["pending"])
        self.assertEqual(self.store.get_macro("MCX:GOLD|swing")["pending_closes"], 0)

    def test_expired_bias_is_inactive(self):
        activated = datetime(2026, 1, 1, 9, 0, tzinfo=IST)
        self.store.set_bias(
            "MCX:GOLD|fast", "bullish", activated, activated + timedelta(minutes=15)
        )
        fast = next(pipeline for pipeline in PIPELINES if pipeline.name == "fast")
        self.assertIsNone(
            self.strategy._active_bias("MCX:GOLD", fast, activated + timedelta(minutes=16))
        )
        self.assertIsNone(
            self.strategy._active_bias("MCX:GOLD", fast, activated + timedelta(minutes=15))
        )

    @patch("mtf_alert.strategy.macro_transition")
    def test_rsi_reset_waits_for_next_macro_candle(self, mock_transition):
        fast = next(pipeline for pipeline in PIPELINES if pipeline.name == "fast")
        key = "MCX:OTHER|fast"
        mock_transition.return_value = MacroTransition(
            Direction.BULLISH, TrendState.BULLISH, None, 0
        )
        self.strategy._refresh_bias("MCX:OTHER", fast, self.frames["15m"])
        self.assertIsNotNone(self.store.get_bias(key))
        self.store.clear_bias(key)
        self.strategy._refresh_bias("MCX:OTHER", fast, self.frames["15m"])
        self.assertIsNone(self.store.get_bias(key))
        mock_transition.assert_called_once()

    @patch("mtf_alert.strategy.macro_transition")
    def test_only_immediately_preceding_trend_controls_countertrend(self, mock_transition):
        fast = next(pipeline for pipeline in PIPELINES if pipeline.name == "fast")
        macro = frame("15m", 202)
        key = "MCX:OTHER|fast"
        self.store.set_macro(key, {
            "established_trend": TrendState.BULLISH.value,
            "pending": Direction.BEARISH.value,
            "pending_closes": 1,
        })
        mock_transition.return_value = MacroTransition(None, TrendState.BULLISH, None, 0)
        with patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION) as trend:
            self.strategy._refresh_bias("MCX:OTHER", fast, macro)
        self.assertEqual(mock_transition.call_args.args[1], TrendState.CONGESTION)
        self.assertEqual(mock_transition.call_args.args[2:], (None, 0))
        self.assertEqual(trend.call_args_list[0].args[1], -2)
        self.assertNotIn("established_trend", self.store.get_macro(key))

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_future_candles_are_invisible(self, *_mocks):
        previous_price = float(self.frames["3m"]["close"].iloc[-1])
        future = self.frames["3m"].iloc[-1].copy()
        future["time"] = self.now
        future["close"] = previous_price + 1000
        future["high"] = future["close"]
        self.frames["3m"] = pd.concat(
            [self.frames["3m"], pd.DataFrame([future])], ignore_index=True
        )
        alerts = self.strategy.evaluate("MCX:GOLD", self.frames, self.now)
        fast = next(alert for alert in alerts if alert.pipeline == "fast")
        self.assertEqual(fast.price, previous_price)
        self.assertEqual(pd.Timestamp(fast.candle_time), pd.Timestamp(self.now))

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_execution_candle_must_not_precede_bias(self, *_mocks):
        for name in ("3m", "15m"):
            self.frames[name]["time"] -= timedelta(minutes=15)
        self.assertEqual(self.strategy.evaluate("MCX:GOLD", self.frames, self.now), [])

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_old_swing_execution_candle_cannot_alert_after_later_momentum(
        self, *_mocks
    ):
        self.frames["15m"]["time"] -= timedelta(minutes=15)
        alerts = self.strategy.evaluate("MCX:GOLD", self.frames, self.now)
        self.assertEqual([alert.pipeline for alert in alerts], ["fast"])

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_zero_volume_is_not_an_undocumented_signal_filter(self, *_mocks):
        self.frames["3m"].loc[219, "volume"] = 0
        self.frames["15m"].loc[219, "volume"] = 0
        self.assertEqual(
            {alert.pipeline for alert in self.strategy.evaluate(
                "MCX:GOLD", self.frames, self.now
            )},
            {"swing", "fast"},
        )

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=True)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_zero_distance_stop_is_not_an_undocumented_signal_filter(self, *_mocks):
        self.frames["3m"].loc[219, "low"] = self.frames["3m"].loc[219, "close"]
        fast = next(pipeline for pipeline in PIPELINES if pipeline.name == "fast")
        alerts = self.strategy.evaluate(
            "MCX:GOLD", self.frames, self.now, pipelines=(fast,)
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].stop_loss, alerts[0].price)

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=False)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_swing_does_not_use_fast_vwap_filter(
        self, _momentum, mock_vwap, _execution, _refresh
    ):
        swing = next(pipeline for pipeline in PIPELINES if pipeline.name == "swing")
        alerts = self.strategy.evaluate(
            "MCX:GOLD", self.frames, self.now, pipelines=(swing,)
        )
        self.assertEqual([alert.pipeline for alert in alerts], ["swing"])
        mock_vwap.assert_not_called()

    @patch.object(Strategy, "_refresh_bias")
    @patch("mtf_alert.strategy.execution_confirms", return_value=True)
    @patch("mtf_alert.strategy.vwap_confirms", return_value=False)
    @patch("mtf_alert.strategy.momentum_status", return_value=MomentumStatus.CONFIRMED)
    def test_fast_requires_five_minute_vwap(
        self, _momentum, mock_vwap, _execution, _refresh
    ):
        fast = next(pipeline for pipeline in PIPELINES if pipeline.name == "fast")
        self.assertEqual(
            self.strategy.evaluate("MCX:GOLD", self.frames, self.now, pipelines=(fast,)),
            [],
        )
        mock_vwap.assert_called_once()
        called_frame, called_direction = mock_vwap.call_args.args
        self.assertEqual(called_direction, Direction.BULLISH)
        pd.testing.assert_frame_equal(called_frame, completed_frames(self.frames, self.now)["5m"])

    def test_completed_frames_excludes_future_macro_and_momentum(self):
        future = self.frames["15m"].iloc[-1].copy()
        future["time"] = self.now
        self.frames["15m"] = pd.concat(
            [self.frames["15m"], pd.DataFrame([future])], ignore_index=True
        )
        visible = completed_frames(self.frames, self.now)
        self.assertEqual(len(visible["15m"]), 220)
        self.assertTrue((visible["15m"]["close_time"] <= pd.Timestamp(self.now)).all())


class MacroTransitionTests(unittest.TestCase):
    def setUp(self):
        self.frame = frame("15m", 202)
        self.close = self.frame["close"].astype(float)

    @staticmethod
    def stochastic_cross(bullish: bool) -> tuple[pd.Series, pd.Series]:
        if bullish:
            return pd.Series([0.0] * 200 + [1.0, 3.0]), pd.Series([0.0] * 200 + [2.0, 2.0])
        return pd.Series([0.0] * 200 + [3.0, 1.0]), pd.Series([0.0] * 200 + [2.0, 2.0])

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_congestion_cross_activates_immediately(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close - 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(True)
        result = macro_transition(self.frame, TrendState.CONGESTION)
        self.assertEqual(result.signal, Direction.BULLISH)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_congestion_bearish_close_needs_no_fresh_price_cross(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close + 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(False)
        result = macro_transition(self.frame, TrendState.CONGESTION)
        self.assertEqual(result.signal, Direction.BEARISH)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_countertrend_crossover_is_not_carried_to_a_later_close(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        def average(values, length):
            line = values.astype(float).copy()
            if len(values) == 203:
                line.iloc[-2:] = [values.iloc[-2] - 1, values.iloc[-1] + 1]
            elif len(values) == 204:
                line.iloc[-2:] = [values.iloc[-2] + 1, values.iloc[-1] + 1]
            else:
                line += 1
            return line

        def stochastic_lines(values, *_args):
            if len(values) == 203:
                k_line = pd.Series([1.0] * 203)
                k_line.iloc[-2:] = [3.0, 1.0]
                return k_line, pd.Series([2.0] * 203)
            return pd.Series([1.0] * len(values)), pd.Series([2.0] * len(values))

        mock_ema.side_effect = average
        mock_stochastic.side_effect = stochastic_lines
        first = macro_transition(frame("15m", 202), TrendState.BULLISH)
        self.assertEqual(first.trend, TrendState.BULLISH)
        second = macro_transition(frame("15m", 203), first.trend)
        self.assertEqual((second.signal, second.pending, second.pending_closes),
                         (None, None, 0))
        third = macro_transition(frame("15m", 204), TrendState.CONGESTION,
                                 second.pending, second.pending_closes)
        self.assertIsNone(third.signal)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_counter_crossover_requires_both_closes_on_event_candle(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close.copy()
        e9.iloc[-2:] = [self.close.iloc[-2] - 1, self.close.iloc[-1] + 1]
        mock_ema.side_effect = [e9, e9, e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(False)
        first = macro_transition(self.frame, TrendState.BULLISH)
        self.assertIsNone(first.signal)
        self.assertEqual((first.pending, first.pending_closes), (None, 0))

        second = macro_transition(
            self.frame, first.trend, first.pending, first.pending_closes
        )
        self.assertIsNone(second.signal)
        self.assertIsNone(second.pending)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.BULLISH)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_counter_crossover_without_first_close_is_discarded(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close - 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(False)
        result = macro_transition(self.frame, TrendState.BULLISH)
        self.assertEqual(
            (result.signal, result.pending, result.pending_closes),
            (None, None, 0),
        )

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_legacy_zero_close_pending_does_not_block_a_new_crossover(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close - 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(True)
        result = macro_transition(
            self.frame, TrendState.CONGESTION, Direction.BEARISH, 0
        )
        self.assertEqual((result.signal, result.pending, result.pending_closes),
                         (Direction.BULLISH, None, 0))

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.BULLISH)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_trend_aligned_signal_does_not_require_price_cross(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        below_price = self.close - 10
        mock_ema.side_effect = [below_price, below_price]
        mock_stochastic.return_value = self.stochastic_cross(True)
        result = macro_transition(self.frame, TrendState.BULLISH)
        self.assertEqual(result.signal, Direction.BULLISH)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.BEARISH)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_new_clean_opposite_trend_still_needs_two_closes(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close.copy()
        e9.iloc[-2:] = [self.close.iloc[-2] - 1, self.close.iloc[-1] + 1]
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(False)
        result = macro_transition(self.frame, TrendState.BULLISH)
        self.assertIsNone(result.signal)
        self.assertIsNone(result.pending)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_bullish_counter_signal_also_waits_for_two_closes(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close.copy()
        e9.iloc[-2:] = [self.close.iloc[-2] + 1, self.close.iloc[-1] - 1]
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(True)
        result = macro_transition(self.frame, TrendState.BEARISH)
        self.assertEqual((result.signal, result.pending_closes), (None, 0))
        self.assertIsNone(result.pending)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_two_completed_bearish_closes_unlock_countertrend_sell(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close + 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(False)
        result = macro_transition(self.frame, TrendState.BULLISH)
        self.assertEqual(result.signal, Direction.BEARISH)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_two_completed_bullish_closes_unlock_countertrend_buy(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close - 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(True)
        result = macro_transition(self.frame, TrendState.BEARISH)
        self.assertEqual(result.signal, Direction.BULLISH)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.CONGESTION)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_broken_confirmation_cancels_pending_signal(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close - 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(False)
        result = macro_transition(
            self.frame, TrendState.CONGESTION, Direction.BEARISH, 1
        )
        self.assertEqual(result.pending_closes, 0)
        self.assertIsNone(result.pending)

    @patch("mtf_alert.strategy.trend_state", return_value=TrendState.BEARISH)
    @patch("mtf_alert.strategy.stochastic")
    @patch("mtf_alert.strategy.ema")
    def test_broken_pending_does_not_hide_new_aligned_crossover(
        self, mock_ema, mock_stochastic, _mock_state
    ):
        e9 = self.close + 1
        mock_ema.side_effect = [e9, e9]
        mock_stochastic.return_value = self.stochastic_cross(False)
        result = macro_transition(
            self.frame, TrendState.CONGESTION, Direction.BULLISH, 1
        )
        self.assertEqual(result.signal, Direction.BEARISH)
        self.assertIsNone(result.pending)


class MomentumTests(unittest.TestCase):
    @patch("mtf_alert.strategy.rsi")
    @patch("mtf_alert.strategy.ema")
    def test_rsi_neutral_zone_requests_reset(self, mock_ema, mock_rsi):
        values = pd.Series(range(60), dtype=float)
        mock_ema.side_effect = [values + 1, values]
        mock_rsi.return_value = pd.Series([50.0] * 60)
        self.assertEqual(
            momentum_status(frame("5m", 60), Direction.BULLISH), MomentumStatus.NEUTRAL
        )

    @patch("mtf_alert.strategy.rsi")
    @patch("mtf_alert.strategy.ema")
    def test_bullish_momentum_uses_documented_thresholds(self, mock_ema, mock_rsi):
        values = pd.Series(range(60), dtype=float)
        mock_ema.side_effect = [values + 1, values]
        mock_rsi.return_value = pd.Series([60.0] * 59 + [61.0])
        self.assertEqual(
            momentum_status(frame("1h", 60), Direction.BULLISH),
            MomentumStatus.CONFIRMED,
        )

    @patch("mtf_alert.strategy.rsi")
    @patch("mtf_alert.strategy.ema")
    def test_rsi_sma_does_not_gate_momentum(self, mock_ema, mock_rsi):
        values = pd.Series(range(60), dtype=float)
        mock_ema.side_effect = [values + 1, values, values - 1, values]
        mock_rsi.side_effect = [
            pd.Series([70.0] * 59 + [61.0]),
            pd.Series([30.0] * 59 + [39.0]),
        ]
        self.assertEqual(momentum_status(frame("1h", 60), Direction.BULLISH), MomentumStatus.CONFIRMED)
        self.assertEqual(momentum_status(frame("1h", 60), Direction.BEARISH), MomentumStatus.CONFIRMED)

    @patch("mtf_alert.strategy.rsi")
    @patch("mtf_alert.strategy.ema")
    def test_bearish_momentum_uses_documented_thresholds(self, mock_ema, mock_rsi):
        values = pd.Series(range(60), dtype=float)
        mock_ema.side_effect = [values - 1, values]
        mock_rsi.return_value = pd.Series([40.0] * 59 + [39.0])
        self.assertEqual(
            momentum_status(frame("5m", 60), Direction.BEARISH),
            MomentumStatus.CONFIRMED,
        )


class DirectionAndVwapTests(unittest.TestCase):
    @patch("mtf_alert.strategy.ema")
    def test_clean_bullish_and_bearish_trends(self, mock_ema):
        data = frame("4h", 220)
        close = data["close"].astype(float)
        mock_ema.side_effect = [close - 1, close - 2, close - 3]
        self.assertEqual(trend_state(data), TrendState.BULLISH)
        mock_ema.side_effect = [close + 1, close + 2, close + 3]
        self.assertEqual(trend_state(data), TrendState.BEARISH)

    @patch("mtf_alert.strategy.ema")
    def test_in_between_ema_order_is_congestion(self, mock_ema):
        data = frame("15m", 220)
        close = data["close"].astype(float)
        mock_ema.side_effect = [close - 1, close - 3, close - 2]
        self.assertEqual(trend_state(data), TrendState.CONGESTION)

    @patch("mtf_alert.strategy.session_vwap")
    def test_vwap_direction_is_exact(self, mock_vwap):
        data = frame("5m", 60)
        close = float(data["close"].iloc[-1])
        mock_vwap.return_value = pd.Series([close - 1] * len(data))
        self.assertTrue(vwap_confirms(data, Direction.BULLISH))
        self.assertFalse(vwap_confirms(data, Direction.BEARISH))
        mock_vwap.return_value = pd.Series([close + 1] * len(data))
        self.assertTrue(vwap_confirms(data, Direction.BEARISH))
        self.assertFalse(vwap_confirms(data, Direction.BULLISH))


class ExecutionTests(unittest.TestCase):
    @patch("mtf_alert.strategy.obv")
    @patch("mtf_alert.strategy.macd")
    @patch("mtf_alert.strategy.ema")
    @patch("mtf_alert.strategy.heikin_ashi")
    def test_bearish_execution_is_symmetric(
        self, mock_ha, mock_ema, mock_macd, mock_obv
    ):
        data = frame("3m", 40)
        ha = pd.DataFrame({
            "open": [10.0] * 40,
            "close": [9.0] * 40,
            "high": [10.0] * 40,
            "low": [8.5] * 40,
        })
        fast = pd.Series([10.0] * 38 + [11.0, 9.0])
        slow = pd.Series([10.0] * 40)
        obv_line = pd.Series([10.0] * 38 + [11.0, 9.0])
        histogram = pd.Series([0.0] * 38 + [-1.0, -2.0])
        mock_ha.return_value = ha
        mock_ema.side_effect = [fast, slow, slow]
        mock_macd.return_value = (slow - 1, slow, histogram)
        mock_obv.return_value = obv_line
        self.assertTrue(execution_confirms(data, Direction.BEARISH, require_obv=True))

    @patch("mtf_alert.strategy.heikin_ashi")
    @patch("mtf_alert.strategy.macd")
    @patch("mtf_alert.strategy.ema")
    def test_small_green_body_does_not_block_valid_execution(
        self, mock_ema, mock_macd, mock_ha
    ):
        data = frame("15m", 40)
        mock_ha.return_value = pd.DataFrame({
            "open": [0.0] * 40,
            "close": [1.0] * 40,
            "high": [5.0] * 40,
            "low": [0.0] * 40,
        })
        fast = pd.Series([10.0] * 38 + [9.0, 11.0])
        slow = pd.Series([10.0] * 40)
        histogram = pd.Series([0.0] * 38 + [1.0, 2.0])
        mock_ema.side_effect = [fast, slow]
        mock_macd.return_value = (slow + 1, slow, histogram)
        self.assertTrue(execution_confirms(data, Direction.BULLISH, require_obv=False))

    @patch("mtf_alert.strategy.obv")
    @patch("mtf_alert.strategy.macd")
    @patch("mtf_alert.strategy.ema")
    @patch("mtf_alert.strategy.heikin_ashi")
    def test_fast_execution_rejects_missing_obv_crossover(
        self, mock_ha, mock_ema, mock_macd, mock_obv
    ):
        data = frame("3m", 40)
        mock_ha.return_value = pd.DataFrame({
            "open": [9.0] * 40, "close": [10.0] * 40,
            "high": [10.0] * 40, "low": [9.0] * 40,
        })
        fast = pd.Series([10.0] * 38 + [9.0, 11.0])
        slow = pd.Series([10.0] * 40)
        histogram = pd.Series([0.0] * 38 + [1.0, 2.0])
        mock_ema.side_effect = [fast, slow, slow]
        mock_macd.return_value = (slow + 1, slow, histogram)
        mock_obv.return_value = pd.Series([9.0] * 40)
        self.assertFalse(execution_confirms(data, Direction.BULLISH, require_obv=True))

    @patch("mtf_alert.strategy.macd")
    @patch("mtf_alert.strategy.ema")
    @patch("mtf_alert.strategy.heikin_ashi")
    def test_execution_uses_ema_9_and_18_and_macd_12_26_9(
        self, mock_ha, mock_ema, mock_macd
    ):
        data = frame("15m", 40)
        ha = pd.DataFrame({
            "open": [9.0] * 40, "close": [10.0] * 40,
            "high": [10.0] * 40, "low": [9.0] * 40,
        })
        mock_ha.return_value = ha
        fast = pd.Series([10.0] * 38 + [9.0, 11.0])
        slow = pd.Series([10.0] * 40)
        mock_ema.side_effect = [fast, slow]
        histogram = pd.Series([0.0] * 38 + [1.0, 2.0])
        mock_macd.return_value = (slow + 1, slow, histogram)
        self.assertTrue(execution_confirms(data, Direction.BULLISH, require_obv=False))
        self.assertEqual([call.args[1] for call in mock_ema.call_args_list], [9, 18])
        mock_macd.assert_called_once_with(ha["close"])


if __name__ == "__main__":
    unittest.main()
