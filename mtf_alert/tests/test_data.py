import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from mtf_alert.data import (
    closed_candles, complete_session_candles, resample_four_hour,
    validate_aggregation, validate_candles,
    validate_session_alignment,
)


IST = ZoneInfo("Asia/Kolkata")


class CandleTests(unittest.TestCase):
    def test_partial_end_of_session_candle_is_excluded(self):
        frame = closed_candles(pd.DataFrame({
            "date": [datetime(2026, 1, 1, 22, 0, tzinfo=IST),
                     datetime(2026, 1, 1, 23, 0, tzinfo=IST)],
            "open": [100, 100], "high": [101, 101], "low": [99, 99],
            "close": [100, 100], "volume": [1, 1],
        }), "1h", datetime(2026, 1, 2, 0, 0, tzinfo=IST))
        self.assertEqual(len(complete_session_candles(frame, "09:00", "23:30")), 1)

    def test_invalid_ohlc_and_duplicate_timestamp_fail_closed(self):
        frame = closed_candles(pd.DataFrame({
            "date": [datetime(2026, 1, 1, 10, 0, tzinfo=IST)],
            "open": [100], "high": [101], "low": [99],
            "close": [100], "volume": [1],
        }), "3m", datetime(2026, 1, 1, 10, 3, tzinfo=IST))
        validate_candles(frame, "3m", "TEST")
        broken = frame.copy()
        broken.loc[0, "high"] = 98
        with self.assertRaises(ValueError):
            validate_candles(broken, "3m", "TEST")
        with self.assertRaises(ValueError):
            validate_candles(pd.concat([frame, frame]), "3m", "TEST")
        validate_session_alignment(frame, "3m", "TEST", "09:00")
        shifted = frame.copy()
        shifted["time"] += pd.Timedelta(minutes=1)
        with self.assertRaisesRegex(ValueError, "session-aligned"):
            validate_session_alignment(shifted, "3m", "TEST", "09:00")

    def test_inconsistent_timeframes_fail_closed(self):
        starts = pd.date_range("2026-01-01 09:00", periods=5, freq="3min", tz=IST)
        lower = pd.DataFrame({
            "time": starts, "close_time": starts + pd.Timedelta(minutes=3),
            "open": [100] * 5, "high": [101] * 5, "low": [99] * 5,
            "close": [100] * 5, "volume": [1] * 5,
        })
        upper = pd.DataFrame({
            "time": [starts[0]], "close_time": [starts[0] + pd.Timedelta(minutes=15)],
            "open": [100], "high": [101], "low": [99], "close": [100], "volume": [5],
        })
        self.assertEqual(validate_aggregation(lower, upper, "3m", "15m", "TEST", "09:00"), 1)
        upper.loc[0, "close"] = 101
        with self.assertRaisesRegex(ValueError, "mismatch"):
            validate_aggregation(lower, upper, "3m", "15m", "TEST", "09:00")

    def test_forming_candle_is_removed(self):
        frame = pd.DataFrame({
            "date": [
                datetime(2026, 1, 1, 10, 0, tzinfo=IST),
                datetime(2026, 1, 1, 10, 5, tzinfo=IST),
            ],
            "open": [1, 2], "high": [2, 3], "low": [0, 1], "close": [1, 2], "volume": [10, 20],
        })
        result = closed_candles(frame, "5m", datetime(2026, 1, 1, 10, 7, tzinfo=IST))
        self.assertEqual(len(result), 1)

    def test_four_hour_session_alignment(self):
        times = pd.date_range("2026-01-01 09:00", periods=8, freq="1h", tz=IST)
        frame = pd.DataFrame({
            "time": times, "open": range(8), "high": range(1, 9),
            "low": range(8), "close": range(1, 9), "volume": [10] * 8,
        })
        result = resample_four_hour(
            frame, datetime(2026, 1, 1, 17, 1, tzinfo=IST), "09:00", "23:30"
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result["time"].iloc[0].hour, 9)
        self.assertEqual(result["time"].iloc[0].minute, 0)

    def test_partial_session_bucket_is_not_a_four_hour_candle(self):
        times = pd.date_range("2026-01-01 09:00", periods=15, freq="1h", tz=IST)
        frame = pd.DataFrame({
            "time": times,
            "open": range(15),
            "high": range(1, 16),
            "low": range(15),
            "close": range(1, 16),
            "volume": [10] * 15,
        })
        result = resample_four_hour(
            frame, datetime(2026, 1, 1, 23, 31, tzinfo=IST), "09:00", "23:30"
        )
        self.assertEqual(len(result), 3)
        self.assertEqual(result["time"].iloc[-1].hour, 17)

    def test_four_hour_bucket_with_missing_hour_is_excluded(self):
        times = pd.date_range("2026-01-01 09:00", periods=4, freq="1h", tz=IST).delete(2)
        frame = pd.DataFrame({
            "time": times,
            "open": range(3),
            "high": range(1, 4),
            "low": range(3),
            "close": range(1, 4),
            "volume": [10] * 3,
        })
        result = resample_four_hour(
            frame, datetime(2026, 1, 1, 13, 1, tzinfo=IST), "09:00", "23:30"
        )
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
