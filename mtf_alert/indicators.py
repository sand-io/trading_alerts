from __future__ import annotations

import numpy as np
import pandas as pd


def ema(values: pd.Series, length: int) -> pd.Series:
    return values.astype(float).ewm(span=length, adjust=False, min_periods=length).mean()


def rsi(values: pd.Series, length: int = 14) -> pd.Series:
    delta = values.astype(float).diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    result = 100 - (100 / (1 + relative_strength))
    return result.mask((loss == 0) & (gain > 0), 100.0).mask((loss == 0) & (gain == 0), 50.0)


def rsi_smoothing_line(rsi_values: pd.Series, length: int = 9) -> pd.Series:
    """The PDF's display-only SMA line for RSI; it is not an entry filter."""
    return rsi_values.rolling(length, min_periods=length).mean()


def stochastic(
    frame: pd.DataFrame, length: int = 14, k_smooth: int = 1, d_smooth: int = 3
) -> tuple[pd.Series, pd.Series]:
    lowest = frame["low"].rolling(length, min_periods=length).min()
    highest = frame["high"].rolling(length, min_periods=length).max()
    spread = (highest - lowest).replace(0, np.nan)
    raw_k = 100 * (frame["close"] - lowest) / spread
    k_line = raw_k.rolling(k_smooth, min_periods=k_smooth).mean()
    d_line = k_line.rolling(d_smooth, min_periods=d_smooth).mean()
    return k_line, d_line


def macd(values: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(values, 12) - ema(values, 26)
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    return line, signal, line - signal


def heikin_ashi(frame: pd.DataFrame) -> pd.DataFrame:
    result = pd.DataFrame(index=frame.index)
    result["close"] = frame[["open", "high", "low", "close"]].mean(axis=1)
    opens = np.empty(len(frame), dtype=float)
    if len(frame):
        opens[0] = (float(frame["open"].iloc[0]) + float(frame["close"].iloc[0])) / 2
        ha_closes = result["close"].to_numpy(dtype=float)
        for index in range(1, len(frame)):
            opens[index] = (opens[index - 1] + ha_closes[index - 1]) / 2
    result["open"] = opens
    result["high"] = pd.concat([frame["high"], result["open"], result["close"]], axis=1).max(axis=1)
    result["low"] = pd.concat([frame["low"], result["open"], result["close"]], axis=1).min(axis=1)
    return result


def obv(frame: pd.DataFrame) -> pd.Series:
    direction = np.sign(frame["close"].astype(float).diff()).fillna(0)
    return (direction * frame["volume"].astype(float)).cumsum()


def session_vwap(frame: pd.DataFrame) -> pd.Series:
    dates = pd.to_datetime(frame["time"]).dt.date
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    weighted = typical * frame["volume"]
    cumulative_volume = frame["volume"].groupby(dates).cumsum().replace(0, np.nan)
    return weighted.groupby(dates).cumsum() / cumulative_volume


def crossed_above(left: pd.Series, right: pd.Series) -> bool:
    return len(left) >= 2 and left.iloc[-2] <= right.iloc[-2] and left.iloc[-1] > right.iloc[-1]


def crossed_below(left: pd.Series, right: pd.Series) -> bool:
    return len(left) >= 2 and left.iloc[-2] >= right.iloc[-2] and left.iloc[-1] < right.iloc[-1]
