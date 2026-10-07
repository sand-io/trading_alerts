from __future__ import annotations

import logging
import fcntl
import signal
import time
from collections.abc import Mapping, Sized
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect.exceptions import KiteException

from .config import Settings, require_credentials
from .data import INTERVAL_MINUTES, KiteData
from .notifier import Notifier
from .state import StateStore
from .strategy import PIPELINES, Strategy


IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    try:
        settings = Settings.load(base_dir)
        configure_logging(settings.log_level)
        api_key, access_token = require_credentials()
        data = KiteData(
            api_key, access_token, settings.history_days,
            settings.session_open, settings.session_close,
        )
        tokens = data.resolve(settings.symbols)
    except KiteException as exc:
        raise SystemExit(
            f"Kite authentication failed: {exc}. Run `python -m mtf_alert.auth`."
        ) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"Startup failed: {exc}") from exc

    # One writer per state file: two monitor processes could otherwise emit
    # the same candle before either has observed the other's dedupe record.
    lock_path = settings.state_file.with_suffix(settings.state_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise SystemExit(f"Another mtf_alert monitor owns {lock_path}") from exc
    store = StateStore(settings.state_file)
    strategy = Strategy(store)
    notifier = Notifier(store, settings.desktop_notifications)
    stop_event = Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    LOG.info("Started alert-only strategy for %d instrument(s)", len(tokens))
    last_scan_candle: dict[tuple[str, str], pd.Timestamp] = {}
    readiness_logged: set[str] = set()

    while not stop_event.is_set():
        cycle_started = time.monotonic()
        for symbol, token in tokens.items():
            if stop_event.is_set():
                break
            wall_clock = datetime.now(IST)
            now = wall_clock - timedelta(seconds=settings.scan_delay_seconds)
            if not market_session_active(now, settings.session_open, settings.session_close):
                continue
            try:
                frames = data.frames(token, now)
                if symbol not in readiness_logged:
                    log_readiness(symbol, frames)
                    readiness_logged.add(symbol)
                for pipeline in PIPELINES:
                    execution = frames[pipeline.execution_interval]
                    if execution.empty or "close_time" not in execution:
                        continue
                    scan_key = (symbol, pipeline.name)
                    persisted_key = f"{symbol}|{pipeline.name}"
                    minutes = INTERVAL_MINUTES[pipeline.execution_interval]
                    if scan_key not in last_scan_candle:
                        # Resume today's unfinished scans after a restart.
                        seconds = minutes * 60
                        latest_due = datetime.fromtimestamp(
                            (int(now.timestamp()) // seconds) * seconds, IST
                        )
                        saved = store.last_scan(persisted_key)
                        if saved is not None and saved.date() == now.date() and saved <= now:
                            last_scan_candle[scan_key] = pd.Timestamp(saved)
                        else:
                            last_scan_candle[scan_key] = pd.Timestamp(
                                latest_due - timedelta(minutes=minutes)
                            )
                    required = (
                        ("5m", "15m", "1h") if pipeline.name == "swing"
                        else ("3m", "5m", "15m")
                    )
                    for stamp in due_execution_candles(
                        execution, last_scan_candle[scan_key], now
                    ):
                        candle_now = stamp.to_pydatetime()
                        readiness = source_frames_readiness(
                            frames, candle_now, settings.session_open, required
                        )
                        if readiness == "wait":
                            # Retry this pipeline's candle on the next poll.
                            break
                        if readiness == "missing":
                            LOG.error(
                                "Skipping %s %s %s: a due source candle is missing",
                                symbol, pipeline.name, stamp,
                            )
                            store.record_scan(persisted_key, candle_now)
                            last_scan_candle[scan_key] = stamp
                            continue
                        for alert in strategy.evaluate(
                            symbol, frames, candle_now, pipelines=(pipeline,)
                        ):
                            notifier.send(alert, wall_clock)
                        store.record_scan(persisted_key, candle_now)
                        last_scan_candle[scan_key] = stamp
            except Exception:
                # One instrument or temporary API failure must not stop all monitoring.
                LOG.exception("Scan failed for %s", symbol)
        remaining = settings.poll_seconds - (time.monotonic() - cycle_started)
        if remaining > 0:
            stop_event.wait(remaining)
    LOG.info("Stopped cleanly")


def market_session_active(now: datetime, open_value: str, close_value: str) -> bool:
    if now.weekday() >= 5:
        return False
    open_hour, open_minute = (int(part) for part in open_value.split(":"))
    close_hour, close_minute = (int(part) for part in close_value.split(":"))
    current = now.hour * 60 + now.minute
    return open_hour * 60 + open_minute <= current <= close_hour * 60 + close_minute


def completed_source_frames_ready(
    frames: Mapping[str, pd.DataFrame], now: datetime, session_open: str
) -> bool:
    return source_frames_readiness(frames, now, session_open) == "ready"


def source_frames_readiness(
    frames: Mapping[str, pd.DataFrame], now: datetime, session_open: str,
    required: tuple[str, ...] = ("3m", "5m", "15m", "1h"),
) -> str:
    """Wait for every raw timeframe candle due by this scan timestamp."""
    open_hour, open_minute = (int(part) for part in session_open.split(":"))
    opened = now.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
    elapsed_seconds = int((now - opened).total_seconds())
    if elapsed_seconds < 0:
        return "wait"
    for name in required:
        minutes = INTERVAL_MINUTES[name]
        periods = elapsed_seconds // (minutes * 60)
        if periods == 0:
            if name == "3m":
                return "wait"
            continue
        expected = pd.Timestamp(opened + timedelta(minutes=periods * minutes))
        frame = frames.get(name)
        if frame is None or frame.empty or "close_time" not in frame:
            return "wait"
        available = pd.DatetimeIndex(frame["close_time"])
        if expected not in available:
            return "missing" if available.max() > expected else "wait"
    return "ready"


def due_execution_candles(
    execution: pd.DataFrame, last_processed: pd.Timestamp, now: datetime
) -> pd.DatetimeIndex:
    """Include late-arriving closed 3m candles without replaying earlier history."""
    times = pd.DatetimeIndex(execution["close_time"])
    return times[(times > last_processed) & (times <= pd.Timestamp(now))]


def log_readiness(symbol: str, frames: Mapping[str, Sized]) -> None:
    counts = {name: len(frame) for name, frame in frames.items()}
    if counts.get("4h", 0) < 202 or counts.get("15m", 0) < 202:
        LOG.warning(
            "%s has insufficient macro history for EMA-200: %s; signals wait for more data",
            symbol,
            counts,
        )
    else:
        LOG.info("%s data ready: %s", symbol, counts)
