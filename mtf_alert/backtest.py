from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from .config import Settings, require_credentials
from .data import (
    API_INTERVALS, INTERVAL_MINUTES, closed_candles, complete_session_candles,
    resample_four_hour, validate_aggregation, validate_candles,
    validate_session_alignment,
)
from .models import Alert
from .strategy import Strategy


IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger(__name__)
DEFAULT_CHUNK_DAYS = 55


class MemoryState:
    """StateStore-compatible state that never touches the live state file."""

    def __init__(self) -> None:
        self.biases: dict[str, dict[str, Any]] = {}
        self.macro: dict[str, dict[str, Any]] = {}

    def get_bias(self, key: str) -> dict[str, Any] | None:
        return self.biases.get(key)

    def set_bias(
        self, key: str, direction: str, activated_at: datetime, expires_at: datetime
    ) -> None:
        self.biases[key] = {
            "direction": direction,
            "activated_at": activated_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    def clear_bias(self, key: str) -> None:
        self.biases.pop(key, None)

    def get_macro(self, key: str) -> dict[str, Any] | None:
        return self.macro.get(key)

    def set_macro(self, key: str, value: dict[str, Any]) -> None:
        self.macro[key] = value

    def set_macro_and_bias(
        self, key: str, macro: dict[str, Any], direction: str | None,
        activated_at: datetime, expires_at: datetime,
    ) -> None:
        self.macro[key] = macro
        if direction is not None:
            self.set_bias(key, direction, activated_at, expires_at)


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    replay_start: datetime
    report_start: datetime
    end: datetime
    scanned_slots: int
    alerts: tuple[Alert, ...]
    outcomes: tuple[SignalOutcome, ...] = ()


@dataclass(frozen=True)
class SignalOutcome:
    status: str
    first_touch_time: str | None


def classify_outcome(alert: Alert, three_minute: pd.DataFrame, end: datetime) -> SignalOutcome:
    """Classify later stop/target touches; never pretend a fill was observed."""
    start = pd.Timestamp(alert.candle_time)
    later = three_minute.loc[
        (three_minute["time"] >= start)
        & (three_minute["close_time"] <= pd.Timestamp(end))
    ]
    if later.empty:
        return SignalOutcome("open", None)
    if alert.direction.value == "bullish":
        stop_hits = later["low"] <= alert.stop_loss
        target_hits = later["high"] >= alert.target
    else:
        stop_hits = later["high"] >= alert.stop_loss
        target_hits = later["low"] <= alert.target
    touches = later.loc[stop_hits | target_hits]
    if touches.empty:
        return SignalOutcome("open", None)
    first = touches.iloc[0]
    stop_hit = bool(stop_hits.loc[first.name])
    target_hit = bool(target_hits.loc[first.name])
    status = "ambiguous" if stop_hit and target_hit else "stop" if stop_hit else "target"
    return SignalOutcome(status, pd.Timestamp(first["close_time"]).isoformat())


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from exc


def day_start(value: date) -> datetime:
    return datetime.combine(value, clock_time.min, tzinfo=IST)


def day_end(value: date) -> datetime:
    return datetime.combine(value, clock_time.max, tzinfo=IST)


class HistoricalDownloader:
    def __init__(self, client: KiteConnect, chunk_days: int = DEFAULT_CHUNK_DAYS) -> None:
        self.client = client
        self.chunk_days = chunk_days

    def resolve(self, symbols: Iterable[str]) -> dict[str, int]:
        wanted = tuple(symbols)
        exchanges = {symbol.split(":", 1)[0] for symbol in wanted}
        lookup: dict[str, int] = {}
        for exchange in exchanges:
            for item in self.client.instruments(exchange):
                lookup[f"{item['exchange']}:{item['tradingsymbol']}"] = int(
                    item["instrument_token"]
                )
        missing = [symbol for symbol in wanted if symbol not in lookup]
        if missing:
            raise ValueError(f"Unknown or expired Kite instruments: {', '.join(missing)}")
        return {symbol: lookup[symbol] for symbol in wanted}

    def frames(
        self, token: int, start: datetime, end: datetime
    ) -> dict[str, pd.DataFrame]:
        # A request through today's end-of-day must not admit Kite's current
        # still-forming candle. Freeze the cutoff once for every interval.
        end = min(end, datetime.now(IST))
        result: dict[str, pd.DataFrame] = {}
        for name, api_interval in API_INTERVALS.items():
            records: list[dict[str, Any]] = []
            cursor = start
            while cursor < end:
                chunk_end = min(cursor + timedelta(days=self.chunk_days), end)
                records.extend(self._history(token, cursor, chunk_end, api_interval))
                cursor = chunk_end
            frame = closed_candles(pd.DataFrame(records), name, end)
            if not frame.empty:
                duplicates = frame.loc[frame.duplicated(subset="time", keep=False)]
                if not duplicates.empty:
                    for _, group in duplicates.groupby("time", sort=False):
                        if len(group.drop_duplicates(
                            subset=["open", "high", "low", "close", "volume", "close_time"]
                        )) != 1:
                            raise ValueError(
                                f"Historical API returned conflicting {name} candles "
                                f"for token {token} at {group['time'].iloc[0]}"
                            )
                frame = (
                    frame.drop_duplicates(subset="time", keep="last")
                    .sort_values("time")
                    .reset_index(drop=True)
                )
            result[name] = frame
            LOG.info("Downloaded %s %s candles", len(frame), name)
        return result

    def _history(
        self, token: int, start: datetime, end: datetime, interval: str
    ) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                values = self.client.historical_data(
                    token, start, end, interval, continuous=False, oi=False
                )
                time.sleep(0.35)
                return values
            except Exception:
                if attempt == 2:
                    raise
                delay = 2**attempt
                LOG.warning("Historical request failed; retrying in %ss", delay)
                time.sleep(delay)
        return []


def save_frames(
    symbol: str,
    token: int,
    frames: dict[str, pd.DataFrame],
    directory: Path,
    start: datetime,
    end: datetime,
) -> None:
    """Persist the exact normalized candles used by a replay for later auditing."""
    symbol_dir = directory / symbol.replace(":", "_")
    symbol_dir.mkdir(parents=True, exist_ok=True)
    for interval, frame in frames.items():
        frame.to_csv(symbol_dir / f"{interval}.csv", index=False)
    metadata = {
        "symbol": symbol,
        "instrument_token": token,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "candles": {interval: len(frame) for interval, frame in frames.items()},
    }
    (symbol_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )
    LOG.info("Saved raw candles to %s", symbol_dir.resolve())


def replay(
    symbol: str,
    raw_frames: dict[str, pd.DataFrame],
    replay_start: datetime,
    report_start: datetime,
    end: datetime,
    session_open: str,
    session_close: str,
) -> BacktestResult:
    missing = set(API_INTERVALS) - set(raw_frames)
    if missing:
        raise ValueError(f"{symbol}: missing intervals {sorted(missing)}")
    for interval in API_INTERVALS:
        validate_candles(raw_frames[interval], interval, symbol)
    source_frames = {
        interval: complete_session_candles(
            raw_frames[interval], session_open, session_close
        )
        for interval in API_INTERVALS
    }
    for interval in API_INTERVALS:
        validate_session_alignment(source_frames[interval], interval, symbol, session_open)
    if source_frames["3m"].empty and source_frames["15m"].empty:
        raise ValueError(f"{symbol}: no complete execution candles in the requested session")
    for lower, upper in (("3m", "15m"), ("5m", "15m"), ("15m", "1h")):
        count = validate_aggregation(
            source_frames[lower], source_frames[upper],
            lower, upper, symbol, session_open,
        )
        LOG.info("%s: verified %d %s bars against %s", symbol, count, upper, lower)
    state = MemoryState()
    strategy = Strategy(state)  # type: ignore[arg-type]
    # A complete 4h bucket contains only source hours that closed by its own
    # close_time. Precompute once, then reveal each bucket only at that time.
    frames_by_interval = {
        **source_frames,
        "4h": resample_four_hour(source_frames["1h"], end, session_open, session_close),
    }
    times = {
        name: pd.DatetimeIndex(frame["close_time"])
        for name, frame in frames_by_interval.items()
    }
    # Swing executes on 15m, independently of whether a 3m bar exists at the
    # same timestamp. Fast executes on 3m. Replay the union of both clocks.
    slots = times["3m"].union(times["15m"]).sort_values()
    selected_slots = slots[(slots >= replay_start) & (slots <= end)]
    alerts: list[Alert] = []
    seen: set[tuple[str, str, str]] = set()

    for stamp in selected_slots:
        now = stamp.to_pydatetime()
        frames: dict[str, pd.DataFrame] = {}
        for name, frame in frames_by_interval.items():
            cursor = int(times[name].searchsorted(stamp, side="right"))
            frames[name] = frame.iloc[:cursor]
        for alert in strategy.evaluate(symbol, frames, now):
            key = (alert.symbol, alert.pipeline, alert.candle_time)
            if key not in seen and pd.Timestamp(alert.candle_time) >= report_start:
                seen.add(key)
                alerts.append(alert)

    return BacktestResult(
        symbol=symbol,
        replay_start=replay_start,
        report_start=report_start,
        end=end,
        scanned_slots=len(selected_slots),
        alerts=tuple(alerts),
        outcomes=tuple(classify_outcome(alert, source_frames["3m"], end) for alert in alerts),
    )


def write_csv(results: Iterable[BacktestResult], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["symbol", "pipeline", "direction", "price", "stop_loss", "target", "candle_time", "reasons", "theoretical_outcome", "first_touch_time"],
        )
        writer.writeheader()
        for result in results:
            for index, alert in enumerate(result.alerts):
                outcome = result.outcomes[index] if index < len(result.outcomes) else SignalOutcome("unknown", None)
                writer.writerow({
                    "symbol": alert.symbol,
                    "pipeline": alert.pipeline,
                    "direction": alert.direction.value,
                    "price": alert.price,
                    "stop_loss": alert.stop_loss,
                    "target": alert.target,
                    "candle_time": alert.candle_time,
                    "reasons": " | ".join(alert.reasons),
                    "theoretical_outcome": outcome.status,
                    "first_touch_time": outcome.first_touch_time,
                })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Causally replay MTF alerts on Kite history")
    parser.add_argument("--from", dest="start", required=True, type=parse_day)
    parser.add_argument("--to", dest="end", required=True, type=parse_day)
    parser.add_argument(
        "--warmup-days", type=int, default=180,
        help="calendar days replayed before --from to initialize EMA/state (default: 180)",
    )
    parser.add_argument(
        "--symbol", action="append", dest="symbols",
        help="exchange:symbol; repeat for multiple symbols (default: config symbols)",
    )
    parser.add_argument("--output", type=Path, default=Path("mtf_alert/backtest_alerts.csv"))
    parser.add_argument(
        "--data-dir", type=Path,
        help="save the exact normalized historical candles used by the backtest",
    )
    parser.add_argument(
        "--input-data-dir", type=Path,
        help="replay previously saved Kite candles without downloading again",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.start > args.end:
        raise SystemExit("--from must be on or before --to")
    if args.warmup_days < 60:
        raise SystemExit("--warmup-days must be at least 60")

    base_dir = Path(__file__).resolve().parent
    settings = Settings.load(base_dir)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s: %(message)s")
    symbols = tuple(value.strip().upper() for value in (args.symbols or settings.symbols))
    report_start = day_start(args.start)
    replay_start = report_start - timedelta(days=args.warmup_days)
    session_end = datetime.combine(
        args.end, clock_time.fromisoformat(settings.session_close), tzinfo=IST
    )
    end = min(session_end, datetime.now(IST))

    try:
        if args.input_data_dir is None:
            api_key, access_token = require_credentials()
            client = KiteConnect(api_key=api_key)
            client.set_access_token(access_token)
            downloader = HistoricalDownloader(client)
            tokens = downloader.resolve(symbols)
        else:
            tokens = {symbol: None for symbol in symbols}
        results = []
        for symbol, token in tokens.items():
            LOG.info("Backtesting %s from %s through %s", symbol, args.start, args.end)
            symbol_end = end
            if args.input_data_dir is None:
                raw = downloader.frames(token, replay_start, end)
            else:
                symbol_dir = args.input_data_dir / symbol.replace(":", "_")
                metadata = json.loads((symbol_dir / "metadata.json").read_text(encoding="utf-8"))
                if metadata.get("symbol") != symbol:
                    raise ValueError(f"{symbol}: saved-data symbol mismatch")
                archived_end = datetime.fromisoformat(metadata["end"])
                if archived_end < end:
                    LOG.warning(
                        "%s archive ends at %s, before requested end %s; results are truncated",
                        symbol, archived_end, end,
                    )
                    symbol_end = archived_end
                if symbol_end < report_start:
                    raise ValueError(f"{symbol}: archive ends before requested report start")
                raw = {
                    interval: pd.read_csv(symbol_dir / f"{interval}.csv",
                                          parse_dates=["time", "close_time"])
                    for interval in API_INTERVALS
                }
                token = int(metadata["instrument_token"])
            complete_four_hour = resample_four_hour(
                raw["1h"], end, settings.session_open, settings.session_close
            )
            if len(complete_four_hour) < 202:
                LOG.warning(
                    "%s has only %d completed 4h candles; Swing cannot initialize EMA-200",
                    symbol, len(complete_four_hour),
                )
            if len(raw["15m"]) < 202:
                LOG.warning(
                    "%s has only %d completed 15m candles; Fast cannot initialize EMA-200",
                    symbol, len(raw["15m"]),
                )
            if args.data_dir is not None and args.input_data_dir is None:
                save_frames(symbol, token, raw, args.data_dir, replay_start, end)
            result = replay(
                symbol, raw, replay_start, report_start, symbol_end,
                settings.session_open, settings.session_close,
            )
            results.append(result)
            LOG.info(
                "%s: scanned %d slots and found %d alert(s)",
                symbol, result.scanned_slots, len(result.alerts),
            )
        write_csv(results, args.output)
    except KiteException as exc:
        raise SystemExit(f"Kite request failed: {exc}. Run `python -m mtf_alert.auth`.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    total = sum(len(result.alerts) for result in results)
    print(f"Backtest complete: {total} alert(s); CSV: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
