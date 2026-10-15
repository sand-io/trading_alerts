"""Shared strategy evaluation and chart serialization.

This module is deliberately independent of FastAPI.  The scanner, notifier and
dashboard all consume the same evaluation result, preventing rule drift.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from nnf_strategy import event_details as nnf_event_details, NNFDataView

from conditions import (
    calculate_adx,
    calculate_atr,
    calculate_ema,
    calculate_heikin_ashi,
    calculate_macd,
    calculate_obv,
    calculate_rsi,
    calculate_stoch,
)


BASE_TIMEFRAMES = ("3m", "5m", "15m", "1h", "1d")
SUPPORTED_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d")
OVERLAY_SPECS = {
    "3m": (("ema20", "EMA 20", "#3d9df5", 20, "close", True),
           ("ema50", "EMA 50", "#f2b84b", 50, "close", True)),
    "5m": (("ema20", "EMA 20", "#3d9df5", 20, "close", True),
           ("ema50", "EMA 50", "#f2b84b", 50, "close", True)),
    "15m": (("haema5", "HA EMA 5", "#3d9df5", 5, "ha_close", True),
            ("haema9", "HA EMA 9", "#f2b84b", 9, "ha_close", True)),
    "1h": (("ema9", "EMA 9", "#3d9df5", 9, "close", True),
           ("ema50", "EMA 50", "#f2b84b", 50, "close", True)),
    "1d": (("ema9", "EMA 9", "#3d9df5", 9, "close", True),
           ("ema50", "EMA 50", "#f2b84b", 50, "close", True)),
}

for _timeframe in ("1m", "30m", "4h"):
    OVERLAY_SPECS[_timeframe] = ()


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
                  symbol_data, threshold: float, as_of=None) -> SideResult:
    results: list[RuleResult] = []
    for condition in conditions:
        try:
            passed = bool(condition.evaluate_at(ltp, vwap, symbol_data, as_of)
                          if hasattr(condition, 'evaluate_at')
                          else condition.evaluate(ltp, vwap, symbol_data))
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


def resolve_direction(bullish: SideResult, bearish: SideResult) -> tuple[SideResult, SideResult]:
    """Resolve two qualifying scores into at most one directional signal."""
    if not (bullish.triggered and bearish.triggered):
        return bullish, bearish
    if bullish.percentage > bearish.percentage:
        return bullish, replace(bearish, triggered=False)
    if bearish.percentage > bullish.percentage:
        return replace(bullish, triggered=False), bearish
    return replace(bullish, triggered=False), replace(bearish, triggered=False)


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
    times = pd.DatetimeIndex(frame['time'])
    if times.tz is None:
        times = times.tz_localize('Asia/Kolkata')
    seconds = times.as_unit('s').asi8
    return [
        {"time": int(stamp), "value": float(value)}
        for stamp, value in zip(seconds, values)
        if not pd.isna(value)
    ]


def _strategy_studies(frame, timeframe: str) -> list[dict[str, Any]]:
    """Build default chart studies when no profile-specific study is available."""
    if timeframe in ("3m", "5m"):
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
    if timeframe in ("1h", "4h", "1d"):
        rsi = calculate_rsi(frame["close"], 14)
        return [{"id": "rsi", "label": "RSI (14)", "format": "number", "default_visible": True,
                 "lines": [{"id": "rsi", "label": "RSI", "color": "#a78bfa", "points": _series_points(frame, rsi)}],
                 "levels": [
                     {"value": 55, "label": "Bull 55", "color": "#24c59a"},
                     {"value": 45, "label": "Bear 45", "color": "#f15b70"},
                 ]}]
    return []


def restore_missing_chart_studies(chart, timeframe):
    """Fill old 3m/4h chart displays from saved candles, without changing records."""
    if timeframe not in ('3m', '4h') or chart.get('studies') or not chart.get('candles'):
        return chart
    frame = pd.DataFrame(chart['candles'])
    frame['time'] = pd.to_datetime(frame['time'], unit='s', utc=True)
    return {**chart, 'studies': _strategy_studies(frame, timeframe)}


def live_study_values(symbol_data, timeframe: str, profile=None) -> dict[str, Any]:
    """Return latest strategy readouts for an active dashboard client."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"timeframe": timeframe, "studies": []}
    frame = symbol_data.get_dataframe(timeframe).tail(600).copy()
    if frame.empty:
        return {"timeframe": timeframe, "studies": []}
    studies = (profile_chart_snapshot(symbol_data, timeframe, profile, limit=600)["studies"]
               if profile else _strategy_studies(frame, timeframe))
    compact = []
    for study in studies:
        lines = []
        for line in study["lines"]:
            if line["points"]:
                lines.append({"id": line["id"], **line["points"][-1]})
        compact.append({"id": study["id"], "lines": lines})
    return {"timeframe": timeframe, "studies": compact}


def missing_rule_timeframes(symbol_data, bullish_conditions, bearish_conditions):
    """No directional signals while a strategy's input timeframe is absent."""
    required = set()
    for rule in list(bullish_conditions) + list(bearish_conditions):
        if rule.interval != 'tick':
            required.add(rule.interval)
        if rule.rule_type in ('yesterday_compare', 'multi_day_close', 'opening_or_previous_day'):
            required.add('1d')
    return sorted(interval for interval in required if symbol_data.get_dataframe(interval).empty)


def historical_evaluation(symbol_data, bullish_conditions, bearish_conditions,
                          threshold: float, confirmation_timeframe: str = "5m"):
    """Read-only fallback; never emits alerts or creates confirmed signals."""
    frame = symbol_data.get_dataframe(confirmation_timeframe)
    if frame.empty:
        return None
    row = frame.iloc[-1]
    price = float(row['close'])
    raw_vwap = row.get('vwap')
    vwap = float(raw_vwap) if pd.notna(raw_vwap) else None
    as_of = datetime.now(ZoneInfo('Asia/Kolkata'))
    bullish = evaluate_side('bullish', bullish_conditions, price, vwap,
                            symbol_data, threshold, as_of=as_of)
    bearish = evaluate_side('bearish', bearish_conditions, price, vwap,
                            symbol_data, threshold, as_of=as_of)
    bullish, bearish = resolve_direction(bullish, bearish)
    missing = missing_rule_timeframes(symbol_data, bullish_conditions, bearish_conditions)
    if missing:
        bullish, bearish = replace(bullish, triggered=False), replace(bearish, triggered=False)
    event = live_event(symbol_data, price, vwap, bullish, bearish,
                       confirmation_timeframe=confirmation_timeframe)
    event.update(data_ready=not missing, missing_timeframes=missing)
    event['evaluation_source'] = 'historical'
    event['as_of'] = _epoch_seconds(row['time'])
    event.update(nnf_event_details(symbol_data, list(bullish_conditions) + list(bearish_conditions), as_of))
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
    for interval in ("5m", "15m", "1h", "1d"):
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
        bull, bear = resolve_direction(bull, bear)
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

    records = frame.to_dict('records')
    times = pd.DatetimeIndex(frame['time'])
    if times.tz is None:
        times = times.tz_localize('Asia/Kolkata')
    seconds = times.as_unit('s').asi8
    candles = [{'time': int(stamp), **{key: float(row[key]) for key in
                ('open', 'high', 'low', 'close')}, 'volume': float(row.get('volume', 0))}
               for stamp, row in zip(seconds, records)]
    ha_frame = calculate_heikin_ashi(frame) if timeframe == "15m" else None
    overlays = []
    for overlay_id, label, color, period, source, default_visible in OVERLAY_SPECS[timeframe]:
        values = ha_frame["close"] if source == "ha_close" else frame["close"]
        calculated = calculate_ema(values, period)
        points = _series_points(frame, calculated)
        overlays.append({"id": overlay_id, "label": label, "color": color,
                         "period": period, "source": source,
                         "default_visible": default_visible, "points": points})

    if timeframe in ("3m", "5m"):
        vwap_points = [
            {"time": int(stamp), "value": float(row["vwap"])}
            for stamp, row in zip(seconds, records)
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
               bearish: SideResult, market_time: datetime | None = None,
               confirmation_timeframe: str = "5m") -> dict[str, Any]:
    if market_time is not None and market_time.tzinfo is None:
        market_time = market_time.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    candles_by_timeframe = {}
    for timeframe in SUPPORTED_TIMEFRAMES:
        frame = symbol_data.get_dataframe(timeframe)
        if not frame.empty:
            candles_by_timeframe[timeframe] = _candle_dict(frame.iloc[-1])
    candle = candles_by_timeframe.get(confirmation_timeframe)
    return {
        "type": "strategy_update",
        "symbol": symbol_data.symbol,
        "instrument_token": symbol_data.instrument_token,
        "server_time": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        # Keep the exchange timestamp separate from server processing time.  A
        # delayed Kite tick must not make stale candles appear freshly updated.
        "market_time": market_time.isoformat() if market_time is not None else None,
        "price": float(ltp),
        "vwap": float(vwap) if vwap is not None else None,
        "candle": candle,
        "confirmation_timeframe": confirmation_timeframe,
        "candles_by_timeframe": candles_by_timeframe,
        "bullish": bullish.to_dict(),
        "bearish": bearish.to_dict(),
    }


def profile_chart_snapshot(symbol_data, timeframe, profile, limit=300, base=None):
    """Archive the indicators used by this profile with its shared candle history."""
    nnf = profile.get('engine') == 'nnf'
    data = NNFDataView(symbol_data) if nnf else symbol_data
    extended_interval = '1h' if timeframe == '4h' else timeframe
    uses_extended = nnf and extended_interval in getattr(symbol_data, 'nnf_candles', {})
    chart = (dict(base) if base is not None and not uses_extended else
             chart_snapshot(data, timeframe, limit))
    frame = data.get_dataframe(timeframe).copy()
    if not nnf:
        frame = frame.tail(limit)
    chart.update(strategy_id=profile['id'], strategy_version=profile['version'],
                 confirmation_timeframe=profile.get('confirmation_timeframe', '5m'))
    if frame.empty:
        return chart
    conditions = [rule for rule in profile['bullish'] + profile['bearish'] if rule.interval == timeframe]
    if not conditions:
        return chart
    overlays, studies = {}, {}
    colors = ('#3d9df5', '#f2b84b', '#a78bfa', '#38c7d9')

    def ema_overlay(period, source='close'):
        identifier = ('haema' if source == 'ha_close' else 'ema') + str(period)
        if identifier in overlays:
            return
        values = calculate_heikin_ashi(frame)['close'] if source == 'ha_close' else frame['close']
        overlays[identifier] = {'id': identifier, 'label': ('HA EMA ' if source == 'ha_close' else 'EMA ') + str(period),
            'period': period, 'source': source, 'color': colors[len(overlays) % len(colors)],
            'default_visible': True, 'points': _series_points(frame, calculate_ema(values, period))}

    def study(identifier, label, lines, levels=None, format='number'):
        studies[identifier] = {'id': identifier, 'label': label, 'format': format,
            'default_visible': True, 'lines': [{'id': line_id, 'label': name,
                'color': colors[index % len(colors)], 'points': _series_points(frame, values)}
                for index, (line_id, name, values) in enumerate(lines)], 'levels': levels or []}

    for rule in conditions:
        params, kind = rule.params, rule.rule_type
        if kind == 'nnf_bias':
            for period in (9, 50, 200):
                ema_overlay(period)
            k, d = calculate_stoch(frame, 14, 3, 1)
            study('stoch14_3_1', 'Stochastic (14,1,3) — K = D',
                  [('stoch_k', '%K (SMA 3)', k), ('stoch_d', '%D (SMA 1)', d)])
        elif kind in ('close_ema', 'prev_close_ema'):
            ema_overlay(params['ema_period'])
        elif kind in ('ema_ema', 'ha_ema_ema', 'ha_ema_cross'):
            source = 'ha_close' if kind in ('ha_ema_ema', 'ha_ema_cross') else 'close'
            for period in (params['ema1'], params['ema2']):
                ema_overlay(period, source)
        elif kind == 'close_vwap':
            overlays['vwap'] = {'id': 'vwap', 'label': 'VWAP', 'period': None, 'source': 'vwap',
                'color': '#a78bfa', 'default_visible': True,
                'points': _series_points(frame, frame.get('vwap', pd.Series(index=frame.index, dtype=float)))}
        elif kind in ('rsi', 'rsi_bands', 'rsi_bearish_bands'):
            period = params['period']
            identifier = f'rsi{period}'
            levels = {level['value']: level for level in studies.get(identifier, {}).get('levels', [])}
            for key in ('value', 'lower', 'upper', 'breakout', 'breakdown'):
                if key in params:
                    levels[params[key]] = {'value': params[key], 'label': str(params[key]), 'color': '#627287'}
            values = calculate_rsi(frame['close'], period)
            lines = [(identifier, 'RSI', values)]
            if params.get('smoothing_period'):
                smoothing = params['smoothing_period']
                lines.append((f'{identifier}_sma{smoothing}', f'RSI SMA {smoothing}', values.rolling(smoothing).mean()))
            study(identifier, f'RSI ({period})', lines, list(levels.values()))
        elif kind in ('macd', 'ha_macd', 'ha_histogram'):
            fast, slow, signal = params['fast'], params['slow'], params['signal']
            source = calculate_heikin_ashi(frame) if kind != 'macd' else frame
            values, signals = calculate_macd(source, fast, slow, signal)
            hist = values - signals
            identifier = ('ha_' if kind != 'macd' else '') + f'macd{fast}_{slow}_{signal}'
            lines = [(identifier, 'MACD', values), (identifier + '_signal', 'Signal', signals)]
            if kind != 'macd':
                lines.append((identifier + '_histogram', 'Histogram', hist))
            study(identifier, ('HA Close ' if kind != 'macd' else '') + f'MACD ({fast},{slow},{signal})', lines,
                  [{'value': 0, 'label': 'Zero', 'color': '#627287'}])
        elif kind in ('obv_ema', 'obv_cross'):
            period = params['ema_period']
            values = calculate_obv(frame)
            study(f'obv{period}', f'OBV / EMA ({period})', [('obv', 'OBV', values),
                (f'obvema{period}', f'OBV EMA {period}', calculate_ema(values, period))])
        elif kind == 'adx':
            period = params['period']
            study(f'adx{period}', f'ADX ({period})', [(f'adx{period}', 'ADX', calculate_adx(frame, period))],
                  [{'value': params['value'], 'label': str(params['value']), 'color': '#627287'}])
        elif kind == 'volume_previous_max':
            period = params['period']
            study(f'volume{period}', f'Volume / previous {period} max', [('volume', 'Volume', frame['volume']),
                  ('previous_volume_max', f'Previous {period} max', frame['volume'].shift(1).rolling(period).max())])
        elif kind == 'range_atr':
            count, period, multiplier = params['candles'], params['atr_period'], params['multiplier']
            values = frame['high'].rolling(count).max() - frame['low'].rolling(count).min()
            study('range_atr', 'Range / ATR', [('range', f'{count}-candle range', values),
                  ('atr_multiple', f'{multiplier:g} x ATR ({period})', calculate_atr(frame, period) * multiplier)], format='price')
    chart['overlays'] = list(overlays.values())
    chart['studies'] = (list(studies.values()) or
                        (_strategy_studies(frame, timeframe) if timeframe in ('3m', '4h') else []))
    if nnf:
        # Calculate on the complete lookback, then restrict only serialization.
        visible_times = {row['time'] for row in chart['candles']}
        for line in chart['overlays'] + [line for study in chart['studies'] for line in study['lines']]:
            line['points'] = [point for point in line['points'] if point['time'] in visible_times]
        chart['warnings'] = profile.get('warnings', [])
    return chart
