from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect


LOG = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
API_INTERVALS = {"3m": "3minute", "5m": "5minute", "15m": "15minute", "1h": "60minute"}
INTERVAL_MINUTES = {"3m": 3, "5m": 5, "15m": 15, "1h": 60}


class KiteData:
    def __init__(
        self, api_key: str, access_token: str, history_days: int,
        session_open: str, session_close: str,
    ) -> None:
        self.client = KiteConnect(api_key=api_key)
        self.client.set_access_token(access_token)
        self.history_days = history_days
        self.session_open = session_open
        self.session_close = session_close
        self._cache: dict[tuple[int, str], pd.DataFrame] = {}

    def resolve(self, symbols: tuple[str, ...]) -> dict[str, int]:
        exchanges = {symbol.split(":", 1)[0] for symbol in symbols if ":" in symbol}
        instruments: list[dict] = []
        for exchange in exchanges:
            instruments.extend(self.client.instruments(exchange))
        lookup = {
            f"{item['exchange']}:{item['tradingsymbol']}": int(item["instrument_token"])
            for item in instruments
        }
        missing = [symbol for symbol in symbols if symbol not in lookup]
        if missing:
            raise ValueError(f"Unknown or expired Kite instruments: {', '.join(missing)}")
        return {symbol: lookup[symbol] for symbol in symbols}

    def frames(self, token: int, now: datetime | None = None) -> dict[str, pd.DataFrame]:
        now = now or datetime.now(IST)
        result: dict[str, pd.DataFrame] = {}
        for name, api_interval in API_INTERVALS.items():
            result[name] = self._load_interval(token, name, api_interval, now)
        result["4h"] = resample_four_hour(
            result["1h"], now, self.session_open, self.session_close
        )
        return result

    def _load_interval(
        self, token: int, name: str, api_interval: str, now: datetime,
    ) -> pd.DataFrame:
        key = (token, name)
        cached = self._cache.get(key)
        if cached is None or cached.empty:
            days = self.history_days if name == "1h" else min(self.history_days, 30)
            start = now - timedelta(days=days)
        else:
            overlap = timedelta(minutes=INTERVAL_MINUTES[name] * 3)
            start = pd.Timestamp(cached["time"].iloc[-1]).to_pydatetime() - overlap

        records = self._history(token, start, now, api_interval)
        fresh = closed_candles(pd.DataFrame(records), name, now)
        fresh = complete_session_candles(
            fresh, self.session_open, self.session_close
        )
        if cached is not None and not cached.empty:
            fresh = pd.concat([cached, fresh], ignore_index=True)
            duplicated = fresh.loc[fresh.duplicated(subset="time", keep=False)]
            for _, group in duplicated.groupby("time", sort=False):
                if len(group.drop_duplicates(
                    subset=["open", "high", "low", "close", "volume", "close_time"]
                )) != 1:
                    raise ValueError(
                        f"Kite revised {name} candle for token {token} at "
                        f"{group['time'].iloc[0]}; refusing an inconsistent live scan"
                    )
            fresh = fresh.drop_duplicates(subset="time", keep="last")
            fresh = fresh.sort_values("time").tail(5000).reset_index(drop=True)
        validate_candles(fresh, name, f"token {token}")
        validate_session_alignment(fresh, name, f"token {token}", self.session_open)
        self._cache[key] = fresh
        return fresh.copy()

    def _history(
        self, token: int, start: datetime, end: datetime, interval: str
    ) -> list[dict]:
        for attempt in range(3):
            try:
                records = self.client.historical_data(
                    token, start, end, interval, continuous=False, oi=False
                )
                time.sleep(0.35)  # stay below Kite's historical-data request rate
                return records
            except Exception:
                if attempt == 2:
                    raise
                delay = 2 ** attempt
                LOG.warning("Historical request failed; retrying in %ss", delay)
                time.sleep(delay)
        return []


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    result = frame.rename(columns={"date": "time"}).copy()
    result["time"] = pd.to_datetime(result["time"])
    if result["time"].dt.tz is None:
        result["time"] = result["time"].dt.tz_localize(IST)
    else:
        result["time"] = result["time"].dt.tz_convert(IST)
    columns = ["time", "open", "high", "low", "close", "volume"]
    return result[columns].sort_values("time").reset_index(drop=True)


def closed_candles(frame: pd.DataFrame, interval: str, now: datetime) -> pd.DataFrame:
    result = _normalise(frame)
    if result.empty:
        result["close_time"] = pd.Series(dtype=f"datetime64[ns, {IST.key}]")
        return result
    now_value = pd.Timestamp(now)
    now_ts = now_value.tz_convert(IST) if now_value.tzinfo else now_value.tz_localize(IST)
    close_times = result["time"] + pd.to_timedelta(INTERVAL_MINUTES[interval], unit="m")
    result["close_time"] = close_times
    return result.loc[result["close_time"] <= now_ts].reset_index(drop=True)


def complete_session_candles(
    frame: pd.DataFrame, session_open: str, session_close: str
) -> pd.DataFrame:
    """Exclude off-session and shortened final bars from indicator history."""
    if frame.empty:
        return frame.copy()
    local = pd.to_datetime(frame["time"]).dt.tz_convert(IST)
    opened = local.dt.normalize() + pd.to_timedelta(session_open + ":00")
    closed = local.dt.normalize() + pd.to_timedelta(session_close + ":00")
    result = frame.loc[
        (local >= opened) & (pd.to_datetime(frame["close_time"]) <= closed)
    ]
    return result.reset_index(drop=True)


def validate_candles(frame: pd.DataFrame, interval: str, symbol: str) -> None:
    """Fail closed on malformed historical bars instead of inventing signals."""
    columns = {"time", "close_time", "open", "high", "low", "close", "volume"}
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{symbol} {interval}: missing columns {sorted(missing)}")
    if frame.empty:
        return
    times = pd.DatetimeIndex(frame["time"])
    closes = pd.DatetimeIndex(frame["close_time"])
    if times.tz is None or closes.tz is None:
        raise ValueError(f"{symbol} {interval}: timestamps must have timezones")
    if not times.is_monotonic_increasing or not times.is_unique:
        raise ValueError(f"{symbol} {interval}: timestamps must be sorted and unique")
    expected_close = times + timedelta(minutes=INTERVAL_MINUTES[interval])
    if not closes.equals(expected_close):
        raise ValueError(f"{symbol} {interval}: invalid candle close times")
    prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    volume = frame["volume"].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or not np.isfinite(volume).all():
        raise ValueError(f"{symbol} {interval}: non-finite price or volume")
    opened_price, high, low, closed_price = prices.T
    if (
        (opened_price <= 0).any() or (closed_price <= 0).any()
        or (low <= 0).any() or (volume < 0).any()
        or (low > np.minimum(opened_price, closed_price)).any()
        or (high < np.maximum(opened_price, closed_price)).any()
        or (high < low).any()
    ):
        raise ValueError(f"{symbol} {interval}: invalid OHLC or volume")


def validate_session_alignment(
    frame: pd.DataFrame, interval: str, symbol: str, session_open: str
) -> None:
    """Reject source timestamps that could put future trades in an earlier bar."""
    if frame.empty:
        return
    hour, minute = (int(part) for part in session_open.split(":"))
    local = pd.to_datetime(frame["time"]).dt.tz_convert(IST)
    opened = local.dt.normalize() + timedelta(hours=hour, minutes=minute)
    offset = (local - opened).dt.total_seconds()
    if (offset % (INTERVAL_MINUTES[interval] * 60) != 0).any():
        raise ValueError(f"{symbol} {interval}: candles are not session-aligned")


def validate_aggregation(
    lower: pd.DataFrame, upper: pd.DataFrame,
    lower_interval: str, upper_interval: str,
    symbol: str, session_open: str,
) -> int:
    """Check every fully observed upper bar against independent lower bars."""
    if lower.empty or upper.empty:
        return 0
    lower_minutes = INTERVAL_MINUTES[lower_interval]
    upper_minutes = INTERVAL_MINUTES[upper_interval]
    if upper_minutes % lower_minutes:
        raise ValueError("aggregation intervals must divide evenly")
    ratio = upper_minutes // lower_minutes
    local = pd.to_datetime(lower["time"]).dt.tz_convert(IST)
    opened = local.dt.normalize() + pd.to_timedelta(session_open + ":00")
    elapsed = ((local - opened).dt.total_seconds() // 60).astype(int)
    grouped = lower.assign(
        bucket=opened + pd.to_timedelta((elapsed // upper_minutes) * upper_minutes, unit="m")
    ).groupby("bucket", sort=True).agg(
        count=("time", "size"),
        first_time=("time", "first"), last_time=("time", "last"),
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"),
    )
    full = grouped.loc[
        (grouped["count"] == ratio)
        & (grouped["first_time"] == grouped.index)
        & (grouped["last_time"] == grouped.index + timedelta(minutes=upper_minutes - lower_minutes))
    ]
    if full.empty:
        return 0
    matched = full.merge(upper.set_index("time"), left_index=True, right_index=True,
                         how="left", suffixes=("_lower", "_upper"), indicator=True)
    if (matched["_merge"] != "both").any():
        raise ValueError(
            f"{symbol}: complete {lower_interval} bars lack matching {upper_interval} bars"
        )
    for field in ("open", "high", "low", "close", "volume"):
        if not np.allclose(
            matched[f"{field}_lower"], matched[f"{field}_upper"],
            rtol=0, atol=1e-8,
        ):
            raise ValueError(
                f"{symbol}: {lower_interval}/{upper_interval} {field} mismatch"
            )
    return len(matched)


def resample_four_hour(
    hourly: pd.DataFrame, now: datetime,
    session_open_value: str = "09:00", session_close_value: str = "23:30",
) -> pd.DataFrame:
    if hourly.empty:
        return hourly.copy()
    frame = hourly.copy()
    local = frame["time"].dt.tz_convert(IST)
    open_hour, open_minute = (int(part) for part in session_open_value.split(":"))
    close_hour, close_minute = (int(part) for part in session_close_value.split(":"))
    session_open = local.dt.normalize() + timedelta(minutes=open_hour * 60 + open_minute)
    elapsed = ((local - session_open).dt.total_seconds() // 60).astype(int)
    frame["bucket"] = session_open + pd.to_timedelta((elapsed // 240) * 240, unit="m")
    result = frame.groupby("bucket", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
        source_candles=("time", "count"),
    ).reset_index().rename(columns={"bucket": "time"})

    now_value = pd.Timestamp(now)
    now_ts = now_value.tz_convert(IST) if now_value.tzinfo else now_value.tz_localize(IST)
    close_offset = timedelta(minutes=close_hour * 60 + close_minute)
    session_close = result["time"].dt.normalize() + close_offset
    result["close_time"] = result["time"] + timedelta(hours=4)
    # A shortened end-of-session bucket is not a four-hour candle and must
    # not influence the 4h strategy.
    result = result.loc[
        (result["source_candles"] == 4)
        & (result["close_time"] <= session_close)
        & (result["close_time"] <= now_ts)
    ]
    return result.drop(columns="source_candles").reset_index(drop=True)
