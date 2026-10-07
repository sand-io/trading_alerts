from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any


LOG = logging.getLogger(__name__)
STATE_VERSION = 2


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = self._empty()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(loaded, dict)
                    and loaded.get("version") == STATE_VERSION
                    and isinstance(loaded.get("biases"), dict)
                    and isinstance(loaded.get("alerts"), dict)
                    and isinstance(loaded.get("macro"), dict)
                    and isinstance(loaded.get("scans", {}), dict)
                ):
                    loaded.setdefault("scans", {})
                    self.data = loaded
                else:
                    LOG.warning("Resetting incompatible runtime state in %s", path)
            except (OSError, json.JSONDecodeError) as exc:
                LOG.warning("Ignoring unreadable runtime state %s: %s", path, exc)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": STATE_VERSION, "biases": {}, "alerts": {}, "macro": {}, "scans": {}}

    def get_bias(self, key: str) -> dict[str, Any] | None:
        value = self.data["biases"].get(key)
        return value if isinstance(value, dict) else None

    def set_bias(
        self, key: str, direction: str, activated_at: datetime, expires_at: datetime
    ) -> None:
        self.data["biases"][key] = {
            "direction": direction,
            "activated_at": activated_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self.save()

    def clear_bias(self, key: str) -> None:
        if self.data["biases"].pop(key, None) is not None:
            self.save()

    def get_macro(self, key: str) -> dict[str, Any] | None:
        value = self.data["macro"].get(key)
        return value if isinstance(value, dict) else None

    def set_macro(self, key: str, value: dict[str, Any]) -> None:
        self.data["macro"][key] = value
        self.save()

    def set_macro_and_bias(
        self, key: str, macro: dict[str, Any], direction: str | None,
        activated_at: datetime, expires_at: datetime,
    ) -> None:
        """Persist a macro transition and its new bias in one atomic file swap."""
        old_macro = self.data["macro"].get(key)
        old_bias = self.data["biases"].get(key)
        self.data["macro"][key] = macro
        if direction is not None:
            self.data["biases"][key] = {
                "direction": direction,
                "activated_at": activated_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
        try:
            self.save()
        except Exception:
            if old_macro is None:
                self.data["macro"].pop(key, None)
            else:
                self.data["macro"][key] = old_macro
            if old_bias is None:
                self.data["biases"].pop(key, None)
            else:
                self.data["biases"][key] = old_bias
            raise

    def last_alert(self, key: str) -> datetime | None:
        value = self.data["alerts"].get(key)
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def record_alert(self, key: str, at: datetime) -> None:
        previous = self.data["alerts"].get(key)
        self.data["alerts"][key] = at.isoformat()
        try:
            self.save()
        except Exception:
            if previous is None:
                self.data["alerts"].pop(key, None)
            else:
                self.data["alerts"][key] = previous
            raise

    def last_scan(self, key: str) -> datetime | None:
        value = self.data["scans"].get(key)
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def record_scan(self, key: str, at: datetime) -> None:
        previous = self.data["scans"].get(key)
        self.data["scans"][key] = at.isoformat()
        try:
            self.save()
        except Exception:
            if previous is None:
                self.data["scans"].pop(key, None)
            else:
                self.data["scans"][key] = previous
            raise

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
