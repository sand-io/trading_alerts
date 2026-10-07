import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mtf_alert.models import Alert, Direction
from mtf_alert.notifier import Notifier
from mtf_alert.state import StateStore


IST = ZoneInfo("Asia/Kolkata")


class NotifierTests(unittest.TestCase):
    def test_same_pipeline_and_candle_alerts_once_even_if_direction_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            notifier = Notifier(store, desktop=False)
            now = datetime(2026, 10, 5, 13, 0, tzinfo=IST)
            first = Alert("MCX:TEST", "fast", Direction.BULLISH, 100.0,
                          now.isoformat(), ("test",), 95.0, 110.0)
            second = Alert("MCX:TEST", "fast", Direction.BEARISH, 100.0,
                           now.isoformat(), ("test",), 105.0, 90.0)
            with patch("mtf_alert.notifier.LOG.warning") as warning:
                self.assertTrue(notifier.send(first, now))
                self.assertFalse(notifier.send(second, now))
            warning.assert_called_once()

    def test_legacy_event_key_still_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            notifier = Notifier(store, desktop=False)
            now = datetime(2026, 10, 5, 13, 0, tzinfo=IST)
            alert = Alert("MCX:TEST", "fast", Direction.BULLISH, 100.0,
                          now.isoformat(), ("test",), 95.0, 110.0)
            store.record_alert(f"event|MCX:TEST|fast|bullish|{now.isoformat()}", now)
            with patch("mtf_alert.notifier.LOG.warning") as warning:
                self.assertFalse(notifier.send(alert, now))
            warning.assert_not_called()

    def test_event_is_deduped_after_log_failure_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            now = datetime(2026, 10, 5, 13, 0, tzinfo=IST)
            alert = Alert("MCX:TEST", "fast", Direction.BULLISH, 100.0,
                          now.isoformat(), ("test",), 95.0, 110.0)
            with patch("mtf_alert.notifier.LOG.warning", side_effect=RuntimeError("log failed")):
                with self.assertRaises(RuntimeError):
                    Notifier(StateStore(path), False).send(alert, now)
            with patch("mtf_alert.notifier.LOG.warning") as warning:
                self.assertFalse(Notifier(StateStore(path), False).send(alert, now))
            warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
