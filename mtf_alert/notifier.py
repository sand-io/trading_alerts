from __future__ import annotations

import json
import logging
import platform
import subprocess
from datetime import datetime

from .models import Alert
from .state import StateStore


LOG = logging.getLogger(__name__)


class Notifier:
    def __init__(self, state: StateStore, desktop: bool) -> None:
        self.state = state
        self.desktop = desktop

    def send(self, alert: Alert, now: datetime) -> bool:
        event_key = (
            f"event|{alert.symbol}|{alert.pipeline}|{alert.candle_time}"
        )
        legacy_keys = (
            f"event|{alert.symbol}|{alert.pipeline}|{direction}|{alert.candle_time}"
            for direction in ("bullish", "bearish")
        )
        if self.state.last_alert(event_key) is not None or any(
            self.state.last_alert(key) is not None for key in legacy_keys
        ):
            return False
        payload = {
            "event": "strategy_alert",
            "symbol": alert.symbol,
            "pipeline": alert.pipeline,
            "direction": alert.direction.value,
            "price": alert.price,
            "stop_loss": alert.stop_loss,
            "target": alert.target,
            "risk_reward": "1:2",
            "candle_time": alert.candle_time,
            "reasons": alert.reasons,
        }
        # At-most-once delivery: persist the event before any side effect.
        # If the process dies immediately afterwards, the alert may be lost,
        # but cannot be sent twice after restart.
        self.state.record_alert(event_key, now)
        LOG.warning("ALERT %s", json.dumps(payload, separators=(",", ":")))
        if self.desktop:
            self._desktop(alert)
        return True

    @staticmethod
    def _desktop(alert: Alert) -> None:
        if platform.system() != "Darwin":
            return
        title = f"{alert.direction.value.upper()} {alert.symbol}"
        message = f"{alert.pipeline} signal at {alert.price}"
        script = "display notification " + json.dumps(message) + " with title " + json.dumps(title)
        try:
            subprocess.run(["osascript", "-e", script], check=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Desktop notification failed: %s", exc)
