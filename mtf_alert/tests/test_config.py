import json
import tempfile
import unittest
from pathlib import Path

from mtf_alert.config import Settings


def valid_config() -> dict:
    return {
        "symbols": ["MCX:GOLD26DECFUT"],
        "poll_seconds": 20,
        "scan_delay_seconds": 5,
        "history_days": 180,
        "session_open": "09:00",
        "session_close": "23:30",
        "desktop_notifications": False,
        "log_level": "INFO",
        "state_file": ".state.json",
    }


class ConfigurationTests(unittest.TestCase):
    def test_loads_valid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "config.json").write_text(json.dumps(valid_config()), encoding="utf-8")
            settings = Settings.load(base)
            self.assertEqual(settings.symbols, ("MCX:GOLD26DECFUT",))
            self.assertEqual(settings.scan_delay_seconds, 5)

    def test_rejects_string_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = valid_config()
            config["desktop_notifications"] = "false"
            (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                Settings.load(base)

    def test_rejects_state_path_outside_application(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = valid_config()
            config["state_file"] = "../outside.json"
            (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must stay inside"):
                Settings.load(base)

    def test_rejects_unknown_field(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = valid_config()
            config["unexpected"] = True
            (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown config fields"):
                Settings.load(base)


if __name__ == "__main__":
    unittest.main()
