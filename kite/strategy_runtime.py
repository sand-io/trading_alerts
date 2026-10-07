"""Shared strategy evaluation and chart serialization.

This module is deliberately independent of FastAPI.  The scanner, notifier and
dashboard all consume the same evaluation result, preventing rule drift.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from conditions import (
    calculate_adx,
    calculate_atr,
    calculate_ema,
    calculate_heikin_ashi,
    calculate_macd,
    calculate_obv,
    calculate_rsi,
)


SUPPORTED_TIMEFRAMES = ("5m", "15m", "1h", "1d")
OVERLAY_SPECS = {
    "5m": (("ema20", "EMA 20", "#3d9df5", 20, "close", True),
           ("ema50", "EMA 50", "#f2b84b", 50, "close", True)),
    "15m": (("haema5", "HA EMA 5", "#3d9df5", 5, "ha_close", True),
            ("haema9", "HA EMA 9", "#f2b84b", 9, "ha_close", True)),
    "1h": (("ema9", "EMA 9", "#3d9df5", 9, "close", True),
           ("ema50", "EMA 50", "#f2b84b", 50, "close", True)),
    "1d": (("ema9", "EMA 9", "#3d9df5", 9, "close", True),
           ("ema50", "EMA 50", "#f2b84b", 50, "close", True)),
}


@dataclass(frozen=True)
class RuleResult:
    text: str
    passed: bool
    error: str | None = None


@dataclass(frozen=True)
class SideResult:
    side: str
    passed: int
    total: int
    percentage: float
    triggered: bool
    rules: tuple[RuleResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "passed": self.passed,
            "total": self.total,
            "percentage": self.percentage,
            "triggered": self.triggered,
            "rules": [asdict(rule) for rule in self.rules],
        }


def evaluate_side(side: str, conditions: Iterable, ltp: float, vwap: float,
                  symbol_data, threshold: float) -> SideResult:
    results: list[RuleResult] = []
    for condition in conditions:
        try:
            passed = bool(condition.evaluate(ltp, vwap, symbol_data))
            results.append(RuleResult(condition.raw_text, passed))
        except Exception as exc:  # preserve visibility without killing the feed
            results.append(RuleResult(condition.raw_text, False, str(exc)))

    total = len(results)
    passed = sum(result.passed for result in results)
    percentage = (passed / total * 100.0) if total else 0.0
    return SideResult(
        side=side,
        passed=passed,
        total=total,
        percentage=percentage,
        triggered=bool(total and percentage >= threshold),
        rules=tuple(results),
    )


def _epoch_seconds(value) -> int:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("Asia/Kolkata")
    return int(timestamp.timestamp())


def _candle_dict(row) -> dict[str, Any]:
    return {
        "time": _epoch_seconds(row["time"]),
        "open": float(row["open"]), "high": float(row["high"]),
        "low": float(row["low"]), "close": float(row["close"]),
        "volume": float(row.get("volume", 0)),
    }


def _series_points(frame, values) -> list[dict[str, Any]]:
    return [
        {"time": _epoch_seconds(frame.iloc[position]["time"]), "value": float(value)}
        for position, value in enumerate(values)
        if not pd.isna(value)
    ]


def _strategy_studies(frame, timeframe: str) -> list[dict[str, Any]]:
    """Build the lower-pane studies actually referenced by the strategy."""
    if timeframe == "5m":
        adx = calculate_adx(frame, 14)
        obv = calculate_obv(frame)
        obv_ema = calculate_ema(obv, 5)
        atr5 = calculate_atr(frame, 14) * 5.0
        range10 = frame["high"].rolling(10).max() - frame["low"].rolling(10).min()
        return [
            {"id": "adx", "label": "ADX (14)", "format": "number", "default_visible": True,
             "lines": [{"id": "adx", "label": "ADX", "color": "#38c7d9", "points": _series_points(frame, adx)}],
             "levels": [{"value": 22, "label": "Threshold 22", "color": "#f2b84b"}]},
            {"id": "obv", "label": "OBV / EMA (5)", "format": "volume", "default_visible": False,
             "lines": [
                 {"id": "obv", "label": "OBV", "color": "#3d9df5", "points": _series_points(frame, obv)},
                 {"id": "obvema5", "label": "OBV EMA 5", "color": "#f2b84b", "points": _series_points(frame, obv_ema)},
             ], "levels": []},
            {"id": "range_atr", "label": "Range / 5×ATR", "format": "price", "default_visible": False,
             "lines": [
                 {"id": "range10", "label": "10-candle range", "color": "#f15b70", "points": _series_points(frame, range10)},
                 {"id": "atr5", "label": "5 × ATR (14)", "color": "#38c7d9", "points": _series_points(frame, atr5)},
             ], "levels": []},
        ]
    if timeframe == "15m":
        macd, signal = calculate_macd(frame, 12, 26, 9)
        return [{"id": "macd", "label": "MACD (12,26,9)", "format": "number", "default_visible": True,
                 "lines": [
                     {"id": "macd", "label": "MACD", "color": "#3d9df5", "points": _series_points(frame, macd)},
                     {"id": "signal", "label": "Signal", "color": "#f2b84b", "points": _series_points(frame, signal)},
                 ], "levels": [{"value": 0, "label": "Zero", "color": "#627287"}]}]
    if timeframe in ("1h", "1d"):
        rsi = calculate_rsi(frame["close"], 14)
        return [{"id": "rsi", "label": "RSI (14)", "format": "number", "default_visible": True,
                 "lines": [{"id": "rsi", "label": "RSI", "color": "#a78bfa", "points": _series_points(frame, rsi)}],
                 "levels": [
                     {"value": 55, "label": "Bull 55", "color": "#24c59a"},
                     {"value": 45, "label": "Bear 45", "color": "#f15b70"},
                 ]}]
    return []


def live_study_values(symbol_data, timeframe: str) -> dict[str, Any]:
    """Return latest strategy readouts for an active dashboard client."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"timeframe": timeframe, "studies": []}
    frame = symbol_data.get_dataframe(timeframe).tail(300).copy()
    if frame.empty:
        return {"timeframe": timeframe, "studies": []}
    studies = _strategy_studies(frame, timeframe)
    compact = []
    for study in studies:
        lines = []
        for line in study["lines"]:
            if line["points"]:
                lines.append({"id": line["id"], **line["points"][-1]})
        compact.append({"id": study["id"], "lines": lines})
    return {"timeframe": timeframe, "studies": compact}


def historical_evaluation(symbol_data, bullish_conditions, bearish_conditions,
                          threshold: float):
    """Read-only fallback; never emits alerts or creates confirmed signals."""
    frame = symbol_data.get_dataframe('5m')
    if frame.empty:
        return None
    row = frame.iloc[-1]
    price = float(row['close'])
    raw_vwap = row.get('vwap')
    vwap = float(raw_vwap) if pd.notna(raw_vwap) else None
    bullish = evaluate_side('bullish', bullish_conditions, price, vwap,
                            symbol_data, threshold)
    bearish = evaluate_side('bearish', bearish_conditions, price, vwap,
                            symbol_data, threshold)
    event = live_event(symbol_data, price, vwap, bullish, bearish)
    event['evaluation_source'] = 'historical'
    event['as_of'] = _epoch_seconds(row['time'])
    return event


def historical_strategy_states(symbol_data, bullish_conditions, bearish_conditions,
                               threshold, now=None, limit=300):
    """Replay completed 5m closes with as-of, partial higher-timeframe candles.

    Current 15m/hour/day OHLC is reconstructed from available 5m bars, never
    from that higher timeframe's future final OHLC. Results are reconstructed
    history, not live alerts. Historical VWAP remains candle-based.
    """
    now = pd.Timestamp(now or datetime.now(ZoneInfo('Asia/Kolkata')))
    if now.tzinfo is not None:
        now = now.tz_convert('Asia/Kolkata').tz_localize(None)
    frames = {}
    for interval in SUPPORTED_TIMEFRAMES:
        frame = symbol_data.get_dataframe(interval).copy()
        if frame.empty:
            return []
        frame['time'] = pd.to_datetime(frame['time'])
        if frame['time'].dt.tz is not None:
            frame['time'] = frame['time'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
        frames[interval] = frame.sort_values('time').reset_index(drop=True)
    base = frames['5m']
    base = base[(base.time.dt.hour * 60 + base.time.dt.minute >= 555) &
                (base.time.dt.hour * 60 + base.time.dt.minute < 930)]

    class AsOfData:
        def __init__(self, values):
            self.values = values

        def get_dataframe(self, interval):
            return self.values.get(interval, pd.DataFrame()).copy()

    output = []
    for _, row in base.tail(limit).iterrows():
        start = row['time'].to_pydatetime()
        cutoff = start + timedelta(minutes=5)
        if cutoff > now:
            continue
        known = base[base.time <= start]
        values = {'5m': known}
        complete = True
        for interval, minutes in (('15m', 15), ('1h', 60), ('1d', None)):
            midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
            session_open = midnight + timedelta(hours=9, minutes=15)
            bucket = (midnight if minutes is None else
                      session_open + timedelta(minutes=int((start-session_open).total_seconds()//60//minutes)*minutes))
            previous = frames[interval][frames[interval].time < bucket]
            partial = known[known.time >= bucket]
            expected_start = session_open if minutes is None else bucket
            expected_count = int((cutoff - expected_start).total_seconds() // 300)
            if partial.empty or partial.iloc[0]['time'] != expected_start or len(partial) != expected_count:
                complete = False
                break
            aggregate = {'time': bucket, 'open': partial.iloc[0]['open'],
                         'high': partial.high.max(), 'low': partial.low.min(),
                         'close': row['close'], 'volume': partial.volume.sum(),
                         'vwap': row.get('vwap')}
            current = pd.DataFrame([aggregate])
            values[interval] = (current if previous.empty else
                                pd.concat([previous, current], ignore_index=True))
        if not complete:
            continue
        view = AsOfData(values)
        price, vwap = float(row['close']), row.get('vwap')
        bull = evaluate_side('bullish', bullish_conditions, price, vwap, view, threshold)
        bear = evaluate_side('bearish', bearish_conditions, price, vwap, view, threshold)
        summary = lambda result: {key: value for key, value in result.to_dict().items() if key != 'rules'}
        output.append({'candle': _candle_dict(row), 'bullish': summary(bull),
                       'bearish': summary(bear), 'evaluation_source': 'reconstructed'})
    return output


def chart_snapshot(symbol_data, timeframe: str = "5m", limit: int = 300) -> dict[str, Any]:
    """Return candles and only the overlays used by this strategy timeframe."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    frame = symbol_data.get_dataframe(timeframe).tail(limit).copy()
    if frame.empty:
        return {"timeframe": timeframe, "candles": [], "overlays": [], "studies": [], "levels": {}}

    candles = [_candle_dict(row) for _, row in frame.iterrows()]
    ha_frame = calculate_heikin_ashi(frame) if timeframe == "15m" else None
    overlays = []
    for overlay_id, label, color, period, source, default_visible in OVERLAY_SPECS[timeframe]:
        values = ha_frame["close"] if source == "ha_close" else frame["close"]
        calculated = calculate_ema(values, period)
        points = [
            {"time": _epoch_seconds(frame.iloc[position]["time"]), "value": float(value)}
            for position, value in enumerate(calculated)
            if not pd.isna(value)
        ]
        overlays.append({"id": overlay_id, "label": label, "color": color,
                         "period": period, "source": source,
                         "default_visible": default_visible, "points": points})

    if timeframe == "5m":
        vwap_points = [
            {"time": _epoch_seconds(row["time"]), "value": float(row["vwap"])}
            for _, row in frame.iterrows()
            if not pd.isna(row.get("vwap")) and float(row["vwap"]) > 0
        ]
        overlays.append({"id": "vwap", "label": "VWAP", "color": "#a78bfa",
                         "period": None, "source": "vwap", "default_visible": False,
                         "points": vwap_points})

    levels: dict[str, float] = {}
    five_minute = symbol_data.get_dataframe("5m")
    current_date = pd.to_datetime(frame["time"]).iloc[-1].date()
    if not five_minute.empty:
        times = pd.to_datetime(five_minute["time"])
        current_date = times.iloc[-1].date()
        today = five_minute[times.dt.date == current_date].sort_values("time")
        if len(today) >= 3:
            levels["opening_high"] = float(today.iloc[:3]["high"].max())
            levels["opening_low"] = float(today.iloc[:3]["low"].min())
    daily = symbol_data.get_dataframe("1d")
    if not daily.empty and "time" in daily.columns:
        daily_times = pd.to_datetime(daily["time"])
        previous = daily[daily_times.dt.date < current_date]
        if not previous.empty:
            levels["previous_high"] = float(previous.iloc[-1]["high"])
            levels["previous_low"] = float(previous.iloc[-1]["low"])

    return {
        "timeframe": timeframe,
        "candles": candles,
        "overlays": overlays,
        "studies": _strategy_studies(frame, timeframe),
        "levels": levels,
    }


def live_event(symbol_data, ltp: float, vwap: float, bullish: SideResult,
               bearish: SideResult) -> dict[str, Any]:
    candles_by_timeframe = {}
    for timeframe in SUPPORTED_TIMEFRAMES:
        frame = symbol_data.get_dataframe(timeframe)
        if not frame.empty:
            candles_by_timeframe[timeframe] = _candle_dict(frame.iloc[-1])
    candle = candles_by_timeframe.get("5m")
    return {
        "type": "strategy_update",
        "symbol": symbol_data.symbol,
        "instrument_token": symbol_data.instrument_token,
        "server_time": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        "price": float(ltp),
        "vwap": float(vwap) if vwap is not None else None,
        "candle": candle,
        "candles_by_timeframe": candles_by_timeframe,
        "bullish": bullish.to_dict(),
        "bearish": bearish.to_dict(),
    }
