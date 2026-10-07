import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from mtf_alert.main import (
    completed_source_frames_ready, due_execution_candles, market_session_active,
    source_frames_readiness,
)


IST = ZoneInfo("Asia/Kolkata")


class SessionTests(unittest.TestCase):
    def test_weekday_inside_session(self):
        value = datetime(2026, 9, 28, 10, 0, tzinfo=IST)  # Monday
        self.assertTrue(market_session_active(value, "09:00", "23:30"))

    def test_weekend_and_after_close_are_inactive(self):
        saturday = datetime(2026, 9, 26, 10, 0, tzinfo=IST)
        late = datetime(2026, 9, 28, 23, 31, tzinfo=IST)
        self.assertFalse(market_session_active(saturday, "09:00", "23:30"))
        self.assertFalse(market_session_active(late, "09:00", "23:30"))

    def test_scan_waits_for_latest_due_candles(self):
        now = datetime(2026, 9, 28, 9, 15, tzinfo=IST)
        frames = {
            name: pd.DataFrame({"close_time": [pd.Timestamp(now)]})
            for name in ("3m", "5m", "15m", "1h")
        }
        self.assertTrue(completed_source_frames_ready(frames, now, "09:00"))
        frames["15m"]["close_time"] = [pd.Timestamp("2026-09-26 23:30", tz=IST)]
        self.assertFalse(completed_source_frames_ready(frames, now, "09:00"))
        frames["15m"]["close_time"] = [pd.Timestamp("2026-09-28 09:30", tz=IST)]
        self.assertFalse(completed_source_frames_ready(frames, now, "09:00"))

    def test_scan_requires_a_new_3m_candle_after_open(self):
        now = datetime(2026, 9, 28, 9, 3, tzinfo=IST)
        old = pd.DataFrame({"close_time": [pd.Timestamp("2026-09-25 23:30", tz=IST)]})
        frames = {name: old for name in ("3m", "5m", "15m", "1h")}
        self.assertFalse(completed_source_frames_ready(frames, now, "09:00"))
        frames["3m"] = pd.DataFrame({"close_time": [pd.Timestamp(now)]})
        self.assertTrue(completed_source_frames_ready(frames, now, "09:00"))

    def test_delayed_3m_candles_are_processed_in_order(self):
        times = pd.date_range("2026-09-28 09:03", periods=3, freq="3min", tz=IST)
        execution = pd.DataFrame({"close_time": times})
        due = due_execution_candles(
            execution, times[0], datetime(2026, 9, 28, 9, 9, tzinfo=IST)
        )
        self.assertEqual(list(due), [times[1], times[2]])

    def test_missing_higher_candle_is_skipped_after_later_one_arrives(self):
        now = datetime(2026, 9, 28, 9, 15, tzinfo=IST)
        frames = {
            name: pd.DataFrame({"close_time": [pd.Timestamp(now)]})
            for name in ("3m", "5m", "15m", "1h")
        }
        frames["15m"] = pd.DataFrame({
            "close_time": [pd.Timestamp(now + pd.Timedelta(minutes=15))]
        })
        self.assertEqual(source_frames_readiness(frames, now, "09:00"), "missing")

    def test_fast_readiness_does_not_wait_for_swing_one_hour_data(self):
        now = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
        frames = {
            name: pd.DataFrame({"close_time": [pd.Timestamp(now)]})
            for name in ("3m", "5m", "15m", "1h")
        }
        frames["1h"] = pd.DataFrame({
            "close_time": [pd.Timestamp(now - pd.Timedelta(hours=1))]
        })
        self.assertEqual(source_frames_readiness(
            frames, now, "09:00", ("3m", "5m", "15m")
        ), "ready")
        self.assertEqual(source_frames_readiness(
            frames, now, "09:00", ("5m", "15m", "1h")
        ), "wait")


if __name__ == "__main__":
    unittest.main()
