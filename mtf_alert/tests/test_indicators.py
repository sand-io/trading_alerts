import unittest

import pandas as pd

from mtf_alert.indicators import (
    crossed_above, crossed_below, heikin_ashi, rsi_smoothing_line, session_vwap, stochastic,
)


class IndicatorTests(unittest.TestCase):
    def test_crosses_are_events(self):
        self.assertTrue(crossed_above(pd.Series([1.0, 3.0]), pd.Series([2.0, 2.0])))
        self.assertTrue(crossed_below(pd.Series([3.0, 1.0]), pd.Series([2.0, 2.0])))
        self.assertFalse(crossed_above(pd.Series([3.0, 4.0]), pd.Series([2.0, 2.0])))

    def test_heikin_ashi(self):
        frame = pd.DataFrame({
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
        })
        result = heikin_ashi(frame)
        self.assertEqual(result["close"].iloc[0], 10.5)
        self.assertEqual(result["open"].iloc[0], 10.5)

    def test_confirmed_stochastic_uses_raw_k_and_three_candle_d(self):
        frame = pd.DataFrame({
            "low": [0.0] * 16,
            "high": [30.0] * 16,
            "close": [15.0] * 13 + [6.0, 15.0, 24.0],
        })
        k_line, d_line = stochastic(frame, 14, 1, 3)
        self.assertEqual(k_line.iloc[-1], 80.0)
        self.assertEqual(d_line.iloc[-1], 50.0)

    def test_rsi_smoothing_line_is_nine_period_sma(self):
        values = pd.Series([float(value) for value in range(1, 11)])
        smoothed = rsi_smoothing_line(values)
        self.assertTrue(pd.isna(smoothed.iloc[7]))
        self.assertEqual(smoothed.iloc[8], 5.0)
        self.assertEqual(smoothed.iloc[9], 6.0)

    def test_vwap_resets_each_session(self):
        frame = pd.DataFrame({
            "time": pd.to_datetime(["2026-01-01 09:15", "2026-01-01 09:20", "2026-01-02 09:15"]),
            "high": [11.0, 13.0, 21.0], "low": [9.0, 11.0, 19.0],
            "close": [10.0, 12.0, 20.0], "volume": [10.0, 10.0, 10.0],
        })
        result = session_vwap(frame)
        self.assertEqual(result.iloc[-1], 20.0)


if __name__ == "__main__":
    unittest.main()
