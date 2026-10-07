"""
Indicator Calculator
Computes all indicators required by the rule engine from OHLCV DataFrames.
Each timeframe has its own DataFrame with pre-computed indicators.
"""

import pandas as pd
import numpy as np


# ─── Core Indicator Functions ────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    dm_plus = ((high - high.shift()) > (low.shift() - low)).astype(float) * (high - high.shift()).clip(lower=0)
    dm_minus = ((low.shift() - low) > (high - high.shift())).astype(float) * (low.shift() - low).clip(lower=0)

    atr_val = tr.ewm(span=period, adjust=False).mean()
    di_plus = 100 * dm_plus.ewm(span=period, adjust=False).mean() / atr_val
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr_val
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def macd(series: pd.Series, fast=12, slow=26, signal=9):
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k_period=14, d_period=3):
    lowest_low = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    typical_price = (high + low + close) / 3
    return (typical_price * volume).cumsum() / volume.cumsum()


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def heikin_ashi(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series):
    ha_close = (open_ + high + low + close) / 4
    ha_open = pd.Series(index=open_.index, dtype=float)
    ha_open.iloc[0] = (open_.iloc[0] + close.iloc[0]) / 2
    for i in range(1, len(open_)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2
    ha_high = pd.concat([high, ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([low, ha_open, ha_close], axis=1).min(axis=1)
    return ha_open, ha_high, ha_low, ha_close


# ─── Full Indicator Builder ──────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Input df must have columns: open, high, low, close, volume
    Returns df with all indicators added.
    """
    df = df.copy()
    o, h, l, c, v = df['open'], df['high'], df['low'], df['close'], df['volume']

    # EMAs
    for period in [5, 9, 20, 50]:
        df[f'ema_{period}'] = ema(c, period)

    # RSI
    df['rsi_14'] = rsi(c, 14)

    # ADX
    df['adx_14'] = adx(h, l, c, 14)

    # ATR
    df['atr_14'] = atr(h, l, c, 14)

    # MACD
    df['macd_line'], df['macd_signal'] = macd(c, 12, 26, 9)

    # Stochastic
    df['stoch_k'], df['stoch_d'] = stochastic(h, l, c, 14, 3)

    # VWAP (meaningful only intraday — reset daily)
    df['vwap'] = vwap(h, l, c, v)

    # OBV
    df['obv'] = obv(c, v)
    df['obv_ema_5'] = ema(df['obv'], 5)

    # Heikin-Ashi EMAs
    _, _, _, ha_close = heikin_ashi(o, h, l, c)
    df['ha_close'] = ha_close
    df['ha_ema_5'] = ema(ha_close, 5)
    df['ha_ema_9'] = ema(ha_close, 9)

    # Volume MA
    df['volume_ma_20'] = sma(v, 20)

    # Rolling stats
    df['highest_high_18'] = h.rolling(18).max()
    df['lowest_low_18'] = l.rolling(18).min()
    df['range_10'] = (h.rolling(10).max() - l.rolling(10).min())

    # Body size %
    df['body_pct'] = (c - o).abs() / (h - l).replace(0, np.nan) * 100

    # First candle of day reference (filled forward)
    df['first_high'] = h.groupby(df.index.date).transform('first')
    df['first_low'] = l.groupby(df.index.date).transform('first')

    return df


def get_reference_values(daily_df: pd.DataFrame, weekly_df: pd.DataFrame) -> dict:
    """
    Extract reference values needed for cross-timeframe comparisons.
    """
    refs = {}

    if daily_df is not None and len(daily_df) >= 5:
        refs['yesterday_low'] = daily_df['low'].iloc[-2] if len(daily_df) >= 2 else None
        refs['last_4_days_high_close'] = daily_df['close'].iloc[-5:-1].max() if len(daily_df) >= 5 else None

    if weekly_df is not None and len(weekly_df) >= 2:
        prev_week = weekly_df.iloc[-2]
        refs['last_week_high'] = prev_week['high']
        refs['last_week_low'] = prev_week['low']

    return refs