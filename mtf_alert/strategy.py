from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import pandas as pd

from .indicators import (
    crossed_above,
    crossed_below,
    ema,
    heikin_ashi,
    macd,
    obv,
    rsi,
    rsi_smoothing_line,
    session_vwap,
    stochastic,
)
from .models import Alert, Direction, Pipeline, TrendState
from .state import StateStore


PIPELINES = (
    Pipeline("swing", "4h", "1h", "15m", 240),
    Pipeline("fast", "15m", "5m", "3m", 15),
)

# User-confirmed interpretation of the PDF's conflicting Stochastic descriptions.
STOCHASTIC_SETTINGS = (14, 1, 3)


class MomentumStatus(str, Enum):
    CONFIRMED = "confirmed"
    NEUTRAL = "neutral"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MacroTransition:
    signal: Direction | None
    trend: TrendState
    pending: Direction | None
    pending_closes: int


class Strategy:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def evaluate(
        self, symbol: str, frames: dict[str, pd.DataFrame], now: datetime,
        pipelines: tuple[Pipeline, ...] = PIPELINES,
    ) -> list[Alert]:
        frames = completed_frames(frames, now)
        alerts: list[Alert] = []
        for pipeline in pipelines:
            self._refresh_bias(symbol, pipeline, frames[pipeline.macro_interval])
            bias = self._active_bias(symbol, pipeline, now)
            if bias is None:
                continue
            momentum = momentum_status(frames[pipeline.momentum_interval], bias)
            if momentum == MomentumStatus.NEUTRAL:
                self._reset_bias(self._key(symbol, pipeline))
                continue
            if momentum != MomentumStatus.CONFIRMED:
                continue
            # SELL mirrors the PDF's BUY execution conditions by owner choice.
            execution = frames[pipeline.execution_interval]
            if execution.empty:
                continue
            execution_close = pd.Timestamp(execution["close_time"].iloc[-1])
            # A crossover is an event on this just-closed candle. Rechecking
            # an older candle against later momentum would be lookahead.
            if execution_close != pd.Timestamp(now):
                continue
            # VWAP belongs only to FAST's 5m momentum stage. SWING must remain
            # the independent 4h -> 1h -> 15m pipeline.
            if pipeline.name == "fast" and not vwap_confirms(frames["5m"], bias):
                continue
            activated = datetime.fromisoformat(
                self.state.get_bias(self._key(symbol, pipeline))["activated_at"]
            )
            if execution_close < pd.Timestamp(activated):
                continue
            if not execution_confirms(
                execution, bias, pipeline.name == "fast"
            ):
                continue
            candle = execution.iloc[-1]
            price = float(candle["close"])
            stop_loss = float(
                candle["low"] if bias == Direction.BULLISH else candle["high"]
            )
            risk = (
                price - stop_loss if bias == Direction.BULLISH else stop_loss - price
            )
            target = (
                price + 2 * risk if bias == Direction.BULLISH else price - 2 * risk
            )
            candle_column = "close_time" if "close_time" in execution else "time"
            alerts.append(Alert(
                symbol=symbol,
                pipeline=pipeline.name,
                direction=bias,
                price=price,
                candle_time=pd.Timestamp(execution[candle_column].iloc[-1]).isoformat(),
                reasons=(
                    f"{pipeline.macro_interval} directional bias",
                    (
                        "5m EMA/RSI/VWAP confirmation"
                        if pipeline.name == "fast"
                        else "1h EMA/RSI confirmation"
                    ),
                    f"{pipeline.execution_interval} HA/EMA/MACD"
                    + ("/OBV" if pipeline.name == "fast" else ""),
                ),
                stop_loss=stop_loss,
                target=target,
            ))
        return alerts

    def _refresh_bias(
        self, symbol: str, pipeline: Pipeline, frame: pd.DataFrame
    ) -> None:
        if len(frame) < 202:
            return
        if "close_time" in frame:
            activated = pd.Timestamp(frame["close_time"].iloc[-1]).to_pydatetime()
        else:
            opened = pd.Timestamp(frame["time"].iloc[-1]).to_pydatetime()
            activated = opened + timedelta(minutes=pipeline.bias_ttl_minutes)
        key = self._key(symbol, pipeline)
        stored = self.state.get_macro(key) or {}
        if stored.get("last_candle") == activated.isoformat():
            return

        # The PDF's trend context is the preceding completed macro candle.
        # Carrying the last clean trend indefinitely through congestion would
        # suppress its explicit single-close congestion rule.
        previous_trend = trend_state(frame, -2)
        if "established_trend" in stored:
            # Legacy pending signals were armed under indefinite trend carry.
            # Do not carry them into the PDF's congestion-bypass behavior.
            pending, pending_closes = None, 0
        else:
            try:
                pending = Direction(stored["pending"]) if stored.get("pending") else None
                pending_closes = int(stored.get("pending_closes", 0))
            except (TypeError, ValueError):
                pending, pending_closes = None, 0

        transition = macro_transition(
            frame, previous_trend, pending, pending_closes
        )
        expires = activated + timedelta(minutes=pipeline.bias_ttl_minutes)
        self.state.set_macro_and_bias(key, {
            "last_candle": activated.isoformat(),
            "trend": trend_state(frame).value,
            "pending": transition.pending.value if transition.pending else None,
            "pending_closes": transition.pending_closes,
        }, transition.signal.value if transition.signal else None, activated, expires)

    def _active_bias(self, symbol: str, pipeline: Pipeline, now: datetime) -> Direction | None:
        stored = self.state.get_bias(self._key(symbol, pipeline))
        if not stored:
            return None
        try:
            activated = datetime.fromisoformat(stored["activated_at"])
            expires = datetime.fromisoformat(stored["expires_at"])
            direction = Direction(stored["direction"])
        except (KeyError, TypeError, ValueError):
            return None
        return direction if activated <= now < expires else None

    def _reset_bias(self, key: str) -> None:
        self.state.clear_bias(key)
        macro = self.state.get_macro(key)
        if macro and (macro.get("pending") is not None or macro.get("pending_closes")):
            self.state.set_macro(key, {**macro, "pending": None, "pending_closes": 0})

    @staticmethod
    def _key(symbol: str, pipeline: Pipeline) -> str:
        return f"{symbol}|{pipeline.name}"


INTERVAL_MINUTES = {"3m": 3, "5m": 5, "15m": 15, "1h": 60, "4h": 240}


def completed_frames(frames: dict[str, pd.DataFrame], now: datetime) -> dict[str, pd.DataFrame]:
    """Never expose a candle that closes after the current evaluation time."""
    result: dict[str, pd.DataFrame] = {}
    now_timestamp = pd.Timestamp(now)
    for interval, frame in frames.items():
        if frame.empty:
            result[interval] = frame
            continue
        if "close_time" in frame:
            if pd.Timestamp(frame["close_time"].iloc[-1]) <= now_timestamp:
                result[interval] = frame
                continue
            close_times = pd.to_datetime(frame["close_time"])
            result[interval] = frame.loc[close_times <= now_timestamp]
        else:
            close_times = pd.to_datetime(frame["time"]) + pd.to_timedelta(
                INTERVAL_MINUTES[interval], unit="m"
            )
            visible = frame.loc[close_times <= now_timestamp].copy()
            visible["close_time"] = close_times.loc[visible.index]
            result[interval] = visible
    return result


def trend_state(frame: pd.DataFrame, offset: int = -1) -> TrendState:
    close = frame["close"].astype(float)
    e9, e50, e200 = ema(close, 9), ema(close, 50), ema(close, 200)
    if close.iloc[offset] > e9.iloc[offset] > e50.iloc[offset] > e200.iloc[offset]:
        return TrendState.BULLISH
    if close.iloc[offset] < e9.iloc[offset] < e50.iloc[offset] < e200.iloc[offset]:
        return TrendState.BEARISH
    return TrendState.CONGESTION


def macro_transition(
    frame: pd.DataFrame,
    previous_trend: TrendState,
    pending: Direction | None = None,
    pending_closes: int = 0,
) -> MacroTransition:
    close = frame["close"].astype(float)
    e9, e200 = ema(close, 9), ema(close, 200)
    k_line, d_line = stochastic(frame, *STOCHASTIC_SETTINGS)
    current_trend = trend_state(frame, -1)
    bullish_stoch = crossed_above(k_line, d_line)
    bearish_stoch = crossed_below(k_line, d_line)
    bullish_close = close.iloc[-1] > e9.iloc[-1] or close.iloc[-1] > e200.iloc[-1]
    bearish_close = close.iloc[-1] < e9.iloc[-1] or close.iloc[-1] < e200.iloc[-1]

    # Retain the immediately preceding clean state for this transition only.
    # The caller recomputes context from the prior candle on each refresh.
    established = current_trend if current_trend != TrendState.CONGESTION else previous_trend

    # Crossovers are events. Never carry an old crossover into a later candle;
    # both counter-trend closes must already exist when the crossover occurs.
    pending = None
    pending_closes = 0

    # The preceding candle defines countertrend context. A sudden jump to a
    # clean opposite trend must not bypass the PDF's two-close requirement.
    if (
        previous_trend == TrendState.BULLISH
        and bearish_stoch
    ):
        confirmed = close.iloc[-1] < e9.iloc[-1] and close.iloc[-2] < e9.iloc[-2]
        signal = Direction.BEARISH if confirmed else None
        return MacroTransition(signal, established if signal else previous_trend, None, 0)
    if (
        previous_trend == TrendState.BEARISH
        and bullish_stoch
    ):
        confirmed = close.iloc[-1] > e9.iloc[-1] and close.iloc[-2] > e9.iloc[-2]
        signal = Direction.BULLISH if confirmed else None
        return MacroTransition(signal, established if signal else previous_trend, None, 0)

    if current_trend == TrendState.BULLISH and bullish_stoch:
        return MacroTransition(Direction.BULLISH, established, None, 0)
    if current_trend == TrendState.BEARISH and bearish_stoch:
        return MacroTransition(Direction.BEARISH, established, None, 0)

    if current_trend == TrendState.CONGESTION:
        if bullish_stoch and bullish_close:
            return MacroTransition(Direction.BULLISH, established, None, 0)
        if bearish_stoch and bearish_close:
            return MacroTransition(Direction.BEARISH, established, None, 0)
    return MacroTransition(None, established, None, 0)


def momentum_status(frame: pd.DataFrame, direction: Direction) -> MomentumStatus:
    if len(frame) < 50:
        return MomentumStatus.REJECTED
    close = frame["close"].astype(float)
    e9, e50 = ema(close, 9), ema(close, 50)
    strength = rsi(close, 14)
    # The PDF specifies an SMA(9) smoothing line, but no RSI-vs-SMA gate.
    _strength_average = rsi_smoothing_line(strength, 9)
    latest_rsi = strength.iloc[-1]
    if pd.isna(latest_rsi):
        return MomentumStatus.REJECTED
    if 40 <= latest_rsi <= 60:
        return MomentumStatus.NEUTRAL
    if direction == Direction.BULLISH:
        return (
            MomentumStatus.CONFIRMED
            if e9.iloc[-1] > e50.iloc[-1] and latest_rsi > 60
            else MomentumStatus.REJECTED
        )
    return (
        MomentumStatus.CONFIRMED
        if e9.iloc[-1] < e50.iloc[-1] and latest_rsi < 40
        else MomentumStatus.REJECTED
    )


def vwap_confirms(frame: pd.DataFrame, direction: Direction) -> bool:
    if frame.empty:
        return False
    benchmark = session_vwap(frame).iloc[-1]
    if pd.isna(benchmark):
        return False
    price = float(frame["close"].iloc[-1])
    return price > benchmark if direction == Direction.BULLISH else price < benchmark


def execution_confirms(frame: pd.DataFrame, direction: Direction, require_obv: bool) -> bool:
    if len(frame) < 40:
        return False
    ha = heikin_ashi(frame)
    e9, e18 = ema(ha["close"], 9), ema(ha["close"], 18)
    macd_line, signal, histogram = macd(ha["close"])
    if direction == Direction.BULLISH:
        base = (
            ha["close"].iloc[-1] > ha["open"].iloc[-1]
            and crossed_above(e9, e18)
            and macd_line.iloc[-1] > signal.iloc[-1]
            and histogram.iloc[-1] > histogram.iloc[-2] > 0
        )
    else:
        base = (
            ha["close"].iloc[-1] < ha["open"].iloc[-1]
            and crossed_below(e9, e18)
            and macd_line.iloc[-1] < signal.iloc[-1]
            and histogram.iloc[-1] < histogram.iloc[-2] < 0
        )
    if not base or not require_obv:
        return bool(base)
    obv_line = obv(frame)
    obv_average = ema(obv_line, 9)
    if direction == Direction.BULLISH:
        return crossed_above(obv_line, obv_average)
    return crossed_below(obv_line, obv_average)
