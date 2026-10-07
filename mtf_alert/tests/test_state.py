import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mtf_alert.state import StateStore


class StateTests(unittest.TestCase):
    def test_macro_and_bias_commit_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            at = datetime(2026, 10, 5, 13, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
            store.set_macro_and_bias("key", {"last_candle": at.isoformat()},
                                     "bullish", at, at + timedelta(minutes=15))
            reloaded = StateStore(path)
            self.assertEqual(reloaded.get_macro("key")["last_candle"], at.isoformat())
            self.assertEqual(reloaded.get_bias("key")["direction"], "bullish")

    def test_failed_state_write_does_not_poison_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            at = datetime(2026, 10, 5, 13, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
            with patch.object(store, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.set_macro_and_bias("key", {"last_candle": at.isoformat()},
                                             "bullish", at, at + timedelta(minutes=15))
                with self.assertRaises(OSError):
                    store.record_alert("event", at)
                with self.assertRaises(OSError):
                    store.record_scan("key", at)
            self.assertIsNone(store.get_macro("key"))
            self.assertIsNone(store.get_bias("key"))
            self.assertIsNone(store.last_alert("event"))
            self.assertIsNone(store.last_scan("key"))

    def test_scan_cursor_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            at = datetime(2026, 10, 5, 13, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
            StateStore(path).record_scan("MCX:TEST|fast", at)
            self.assertEqual(StateStore(path).last_scan("MCX:TEST|fast"), at)
