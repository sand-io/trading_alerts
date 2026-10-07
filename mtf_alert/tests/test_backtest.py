from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from mtf_alert.backtest import (
    BacktestResult, HistoricalDownloader, MemoryState, build_parser, classify_outcome, replay,
    save_frames, write_csv,
)
from mtf_alert.data import resample_four_hour
from mtf_alert.models import Alert, Direction


IST = ZoneInfo("Asia/Kolkata")


def frame(interval: str, periods: int = 3) -> pd.DataFrame:
    minutes = {"3m": 3, "5m": 5, "15m": 15, "1h": 60}[interval]
    opened = pd.date_range("2026-01-05 09:00", periods=periods, freq=f"{minutes}min", tz=IST)
    return pd.DataFrame({
        "time": opened,
        "open": [100] * periods,
        "high": [101] * periods,
        "low": [99] * periods,
        "close": [100] * periods,
        "volume": [0] * periods,
        "close_time": opened + pd.to_timedelta(minutes, unit="m"),
    })


class BacktestTests(unittest.TestCase):
    def test_outcome_uses_only_later_candles_and_marks_same_bar_conflict(self) -> None:
        start = datetime(2026, 1, 5, 9, 3, tzinfo=IST)
        alert = Alert("MCX:TEST", "fast", Direction.BULLISH, 100.0,
                      start.isoformat(), ("test",), 95.0, 110.0)
        bars = pd.DataFrame({
            "time": [start - timedelta(minutes=3), start, start + timedelta(minutes=3)],
            "close_time": [start, start + timedelta(minutes=3), start + timedelta(minutes=6)],
            "low": [94, 99, 94], "high": [111, 101, 111],
        })
        outcome = classify_outcome(alert, bars, start + timedelta(minutes=6))
        self.assertEqual(outcome.status, "ambiguous")
        bars.loc[2, "high"] = 105
        self.assertEqual(classify_outcome(alert, bars, start + timedelta(minutes=6)).status,
                         "stop")
        self.assertEqual(classify_outcome(alert, bars, start).status, "open")

    def test_download_through_future_end_rejects_forming_candle(self) -> None:
        now = datetime.now(IST)
        records = [
            {"date": now - timedelta(minutes=10), "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 1},
            {"date": now, "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 1},
        ]
        downloader = HistoricalDownloader(None)  # type: ignore[arg-type]
        with patch.object(downloader, "_history", return_value=records):
            frames = downloader.frames(123, now - timedelta(minutes=15),
                                       now + timedelta(hours=1))
        self.assertEqual(len(frames["3m"]), 1)
        self.assertLessEqual(frames["3m"]["close_time"].iloc[-1], pd.Timestamp(now))

    def test_default_warmup_is_long_enough_to_attempt_four_hour_ema(self) -> None:
        args = build_parser().parse_args(["--from", "2026-09-01", "--to", "2026-09-30"])
        self.assertEqual(args.warmup_days, 180)

    def test_memory_state_does_not_write_to_disk(self) -> None:
        state = MemoryState()
        at = datetime(2026, 1, 1, tzinfo=IST)
        state.set_bias("x", "bullish", at, at)
        state.set_macro("x", {"trend": "congestion"})
        self.assertEqual(state.get_bias("x")["direction"], "bullish")
        state.clear_bias("x")
        self.assertIsNone(state.get_bias("x"))

    def test_replay_does_not_report_warmup_period(self) -> None:
        frames = {name: frame(name) for name in ("3m", "5m", "15m", "1h")}
        result = replay(
            "MCX:TEST", frames,
            datetime(2026, 1, 5, 9, 0, tzinfo=IST),
            datetime(2026, 1, 6, 9, 0, tzinfo=IST),
            datetime(2026, 1, 6, 23, 59, tzinfo=IST),
            "09:00", "23:30",
        )
        self.assertEqual(result.alerts, ())
        self.assertEqual(result.scanned_slots, 6)

    def test_swing_replay_does_not_depend_on_matching_three_minute_bar(self) -> None:
        frames = {name: frame(name) for name in ("3m", "5m", "15m", "1h")}
        frames["3m"] = frames["3m"].iloc[:2]
        at = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
        alert = Alert("MCX:TEST", "swing", Direction.BULLISH, 100.0,
                      at.isoformat(), ("test",), 95.0, 110.0)
        with patch("mtf_alert.backtest.Strategy.evaluate",
                   side_effect=lambda _symbol, _frames, now: [alert] if now == at else []):
            result = replay(
                "MCX:TEST", frames,
                datetime(2026, 1, 5, 9, 0, tzinfo=IST),
                datetime(2026, 1, 5, 9, 0, tzinfo=IST),
                datetime(2026, 1, 5, 10, 0, tzinfo=IST),
                "09:00", "23:30",
            )
        self.assertEqual(result.alerts, (alert,))

    def test_replay_never_exposes_future_candles(self) -> None:
        frames = {name: frame(name) for name in ("3m", "5m", "15m", "1h")}
        with patch("mtf_alert.backtest.Strategy.evaluate", return_value=[]) as evaluate:
            replay(
                "MCX:TEST", frames,
                datetime(2026, 1, 5, 9, 0, tzinfo=IST),
                datetime(2026, 1, 5, 9, 0, tzinfo=IST),
                datetime(2026, 1, 5, 10, 0, tzinfo=IST),
                "09:00", "23:30",
            )
        self.assertTrue(evaluate.call_args_list)
        for call in evaluate.call_args_list:
            visible, now = call.args[1:]
            for candle_frame in visible.values():
                if not candle_frame.empty:
                    self.assertLessEqual(candle_frame["close_time"].iloc[-1], pd.Timestamp(now))

    def test_cached_four_hour_buckets_match_asof_resampling(self) -> None:
        hourly = frame("1h", 8)
        end = datetime(2026, 1, 5, 17, 0, tzinfo=IST)
        cached = resample_four_hour(hourly, end, "09:00", "23:30")
        for hour, minute in ((12, 57), (13, 0), (15, 0), (17, 0)):
            now = datetime(2026, 1, 5, hour, minute, tzinfo=IST)
            visible = cached.loc[cached["close_time"] <= pd.Timestamp(now)].reset_index(drop=True)
            source = hourly.loc[hourly["close_time"] <= pd.Timestamp(now)]
            expected = resample_four_hour(source, now, "09:00", "23:30")
            pd.testing.assert_frame_equal(visible, expected)

    def test_replay_deduplicates_same_candle_across_directions(self) -> None:
        frames = {name: frame(name) for name in ("3m", "5m", "15m", "1h")}
        candle_time = "2026-01-05T09:03:00+05:30"
        buy = Alert("MCX:TEST", "fast", Direction.BULLISH, 100.0, candle_time,
                    ("test",), 95.0, 110.0)
        sell = Alert("MCX:TEST", "fast", Direction.BEARISH, 100.0, candle_time,
                     ("test",), 105.0, 90.0)
        with patch("mtf_alert.backtest.Strategy.evaluate", return_value=[buy, sell]):
            result = replay(
                "MCX:TEST", frames,
                datetime(2026, 1, 5, 9, 0, tzinfo=IST),
                datetime(2026, 1, 5, 9, 0, tzinfo=IST),
                datetime(2026, 1, 5, 10, 0, tzinfo=IST),
                "09:00", "23:30",
            )
        self.assertEqual(result.alerts, (buy,))

    def test_csv_contains_alert(self) -> None:
        alert = Alert("MCX:TEST", "fast", Direction.BULLISH, 123.0, "2026-01-01T10:00:00+05:30", ("why",), 120.0, 129.0)
        result = BacktestResult(
            "MCX:TEST", datetime.now(IST), datetime.now(IST), datetime.now(IST), 1, (alert,)
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "alerts.csv"
            write_csv([result], output)
            content = output.read_text(encoding="utf-8")
        self.assertIn("MCX:TEST,fast,bullish,123.0", content)
        self.assertIn("123.0,120.0,129.0", content)

    def test_save_frames_preserves_auditable_candles(self) -> None:
        frames = {name: frame(name) for name in ("3m", "5m", "15m", "1h")}
        start = datetime(2026, 1, 5, tzinfo=IST)
        end = datetime(2026, 1, 6, tzinfo=IST)
        with tempfile.TemporaryDirectory() as directory:
            save_frames("MCX:TEST", 123, frames, Path(directory), start, end)
            symbol_dir = Path(directory) / "MCX_TEST"
            saved = pd.read_csv(symbol_dir / "3m.csv")
            metadata = (symbol_dir / "metadata.json").read_text(encoding="utf-8")
        self.assertEqual(len(saved), 3)
        self.assertIn('\"instrument_token\": 123', metadata)


if __name__ == "__main__":
    unittest.main()
