from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


CONFIG_KEYS = {
    "symbols",
    "poll_seconds",
    "scan_delay_seconds",
    "history_days",
    "session_open",
    "session_close",
    "desktop_notifications",
    "log_level",
    "state_file",
}


@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...]
    poll_seconds: int = 20
    scan_delay_seconds: int = 5
    history_days: int = 180
    session_open: str = "09:00"
    session_close: str = "23:30"
    desktop_notifications: bool = False
    log_level: str = "INFO"
    state_file: Path = Path(".state.json")

    @classmethod
    def load(cls, base_dir: Path) -> "Settings":
        # The shared token is refreshed by mtf_alert.auth and must win over a
        # stale value inherited from the parent shell or another application.
        load_dotenv(base_dir.parent / ".env", override=True)
        config_path = base_dir / "config.json"
        if not config_path.exists():
            raise FileNotFoundError(f"Missing required configuration: {config_path}")

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {config_path}: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise ValueError("config.json must contain a JSON object")
        unknown = set(raw) - CONFIG_KEYS
        if unknown:
            raise ValueError(f"Unknown config fields: {', '.join(sorted(unknown))}")

        symbol_values = raw.get("symbols", [])
        if not isinstance(symbol_values, list) or not all(
            isinstance(item, str) for item in symbol_values
        ):
            raise ValueError("symbols must be an array of exchange:tradingsymbol strings")
        symbols = tuple(item.strip().upper() for item in symbol_values if item.strip())
        if not symbols:
            raise ValueError("config.json must contain at least one exchange:symbol entry")
        if len(set(symbols)) != len(symbols) or any(":" not in symbol for symbol in symbols):
            raise ValueError("symbols must be unique exchange:tradingsymbol values")

        poll = _integer(raw, "poll_seconds", 20, minimum=5, maximum=180)
        scan_delay = _integer(raw, "scan_delay_seconds", 5, minimum=0, maximum=60)
        history = _integer(raw, "history_days", 180, minimum=120, maximum=400)
        session_open = _string(raw, "session_open", "09:00")
        session_close = _string(raw, "session_close", "23:30")
        for value in (session_open, session_close):
            try:
                hour, minute = (int(part) for part in value.split(":"))
            except (TypeError, ValueError) as exc:
                raise ValueError("session_open and session_close must use HH:MM") from exc
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError("session_open and session_close must use valid HH:MM values")
        if _minutes(session_open) >= _minutes(session_close):
            raise ValueError("session_open must be earlier than session_close")
        log_level = _string(raw, "log_level", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        desktop = raw.get("desktop_notifications", False)
        if not isinstance(desktop, bool):
            raise ValueError("desktop_notifications must be true or false")

        state_file = Path(_string(raw, "state_file", ".state.json"))
        state_file = (base_dir / state_file).resolve()
        if not state_file.is_relative_to(base_dir.resolve()):
            raise ValueError("state_file must stay inside the mtf_alert directory")
        return cls(
            symbols=symbols,
            poll_seconds=poll,
            scan_delay_seconds=scan_delay,
            history_days=history,
            session_open=session_open,
            session_close=session_close,
            desktop_notifications=desktop,
            log_level=log_level,
            state_file=state_file,
        )


def require_credentials() -> tuple[str, str]:
    api_key = os.getenv("KITE_API_KEY", "").strip()
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if not api_key or not access_token:
        raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN must be set in trading_alerts/.env")
    return api_key, access_token


def _integer(
    values: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int
) -> int:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _string(values: dict[str, Any], key: str, default: str) -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute
