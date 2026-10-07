"""
Strategy Evaluator
Evaluates user-created strategies (from DB) against live indicator DataFrames.
Supports all indicators: EMA, RSI, MACD, ADX, VWAP, OBV, Stochastic, ATR, Bollinger Bands, Heikin-Ashi.
"""

import pandas as pd
import numpy as np
from indicators import ema, sma


# ─── Available Indicators Catalog (sent to UI for dropdowns) ─────────────────

INDICATORS = {
    # Timeframe-agnostic — works on any TF
    "EMA":         {"label": "EMA",              "params": ["period"], "compare_to": ["VALUE", "EMA"]},
    "RSI":         {"label": "RSI",              "params": ["period"], "compare_to": ["VALUE"]},
    "ADX":         {"label": "ADX",              "params": ["period"], "compare_to": ["VALUE"]},
    "MACD_LINE":   {"label": "MACD Line",        "params": ["fast","slow","signal"], "compare_to": ["MACD_SIGNAL", "VALUE"]},
    "STOCH_K":     {"label": "Stochastic %K",    "params": ["k_period","d_period"], "compare_to": ["STOCH_D", "VALUE"]},
    "BB_UPPER":    {"label": "Bollinger Upper",  "params": ["period","std"], "compare_to": ["CLOSE", "VALUE"]},
    "BB_LOWER":    {"label": "Bollinger Lower",  "params": ["period","std"], "compare_to": ["CLOSE", "VALUE"]},
    "BB_MID":      {"label": "Bollinger Mid",    "params": ["period","std"], "compare_to": ["CLOSE", "VALUE"]},
    "ATR":         {"label": "ATR",              "params": ["period"], "compare_to": ["VALUE"]},
    "VWAP":        {"label": "VWAP",             "params": [],         "compare_to": ["CLOSE"]},
    "OBV":         {"label": "OBV",              "params": [],         "compare_to": ["OBV_EMA"]},
    "HA_EMA":      {"label": "Heikin-Ashi EMA",  "params": ["period"], "compare_to": ["HA_EMA"]},
    "CLOSE":       {"label": "Close Price",      "params": [],         "compare_to": ["EMA", "VALUE", "VWAP"]},
    "VOLUME":      {"label": "Volume",           "params": [],         "compare_to": ["VOLUME_MA"]},
    "BODY_PCT":    {"label": "Candle Body %",    "params": [],         "compare_to": ["VALUE"]},
}

TIMEFRAMES = ["5min", "15min", "1hour", "4hour", "1day", "1week"]

CONDITIONS = [
    {"value": "gt",  "label": "is greater than (>)"},
    {"value": "gte", "label": "is greater than or equal to (≥)"},
    {"value": "lt",  "label": "is less than (<)"},
    {"value": "lte", "label": "is less than or equal to (≤)"},
]


# ─── Bollinger Bands ──────────────────────────────────────────────────────────

def bollinger(series: pd.Series, period: int = 20, std: float = 2.0):
    mid   = sma(series, period)
    upper = mid + std * series.rolling(period).std()
    lower = mid - std * series.rolling(period).std()
    return upper, mid, lower


# ─── Single Condition Evaluator ───────────────────────────────────────────────

def evaluate_condition(cond: dict, data: dict) -> dict:
    """
    cond = {
        "timeframe": "5min",
        "indicator": "RSI",
        "indicator_param": "14",
        "condition": "gt",
        "compare_to": "VALUE",
        "compare_param": "60"
    }
    Returns: { passed: bool, lhs: float, rhs: float, reason: str }
    """
    tf      = cond.get('timeframe', '5min')
    ind     = cond.get('indicator')
    ind_p   = cond.get('indicator_param', '')
    op      = cond.get('condition', 'gt')
    cmp_to  = cond.get('compare_to', 'VALUE')
    cmp_p   = cond.get('compare_param', '')

    df = data.get(tf)
    if df is None or df.empty:
        return {"passed": False, "lhs": None, "rhs": None, "reason": f"No {tf} data"}

    try:
        lhs = _get_lhs(ind, ind_p, df)
        rhs = _get_rhs(cmp_to, cmp_p, ind_p, df)

        if lhs is None or rhs is None:
            return {"passed": False, "lhs": lhs, "rhs": rhs, "reason": "Missing data"}

        passed = _compare(lhs, rhs, op)
        label  = f"{ind}({ind_p})={lhs:.4f} {op} {cmp_to}({cmp_p})={rhs:.4f}"
        return {"passed": passed, "lhs": round(lhs, 4), "rhs": round(rhs, 4),
                "reason": label + (" ✅" if passed else " ❌")}

    except Exception as e:
        return {"passed": False, "lhs": None, "rhs": None, "reason": f"Error: {e}"}


def _latest(df, col):
    if col not in df.columns:
        return None
    v = df[col].iloc[-1]
    return None if pd.isna(v) else float(v)


def _get_lhs(ind: str, param: str, df: pd.DataFrame):
    c = df['close']
    if ind == "RSI":
        period = int(param or 14)
        col = f'rsi_{period}'
        if col not in df.columns:
            from indicators import rsi
            df[col] = rsi(c, period)
        return _latest(df, col)

    elif ind == "EMA":
        period = int(param or 9)
        col = f'ema_{period}'
        if col not in df.columns:
            df[col] = ema(c, period)
        return _latest(df, col)

    elif ind == "ADX":
        period = int(param or 14)
        return _latest(df, 'adx_14')

    elif ind == "MACD_LINE":
        return _latest(df, 'macd_line')

    elif ind == "STOCH_K":
        return _latest(df, 'stoch_k')

    elif ind == "BB_UPPER":
        p, s = _bb_params(param)
        col = f'bb_upper_{p}_{s}'
        if col not in df.columns:
            df[f'bb_upper_{p}_{s}'], df[f'bb_mid_{p}_{s}'], df[f'bb_lower_{p}_{s}'] = bollinger(c, p, s)
        return _latest(df, col)

    elif ind == "BB_LOWER":
        p, s = _bb_params(param)
        col = f'bb_lower_{p}_{s}'
        if col not in df.columns:
            df[f'bb_upper_{p}_{s}'], df[f'bb_mid_{p}_{s}'], df[f'bb_lower_{p}_{s}'] = bollinger(c, p, s)
        return _latest(df, col)

    elif ind == "BB_MID":
        p, s = _bb_params(param)
        col = f'bb_mid_{p}_{s}'
        if col not in df.columns:
            df[f'bb_upper_{p}_{s}'], df[f'bb_mid_{p}_{s}'], df[f'bb_lower_{p}_{s}'] = bollinger(c, p, s)
        return _latest(df, col)

    elif ind == "ATR":
        return _latest(df, 'atr_14')

    elif ind == "VWAP":
        return _latest(df, 'vwap')

    elif ind == "OBV":
        return _latest(df, 'obv')

    elif ind == "HA_EMA":
        period = int(param or 9)
        col = f'ha_ema_{period}'
        if col not in df.columns:
            df[col] = ema(df['ha_close'], period)
        return _latest(df, col)

    elif ind == "CLOSE":
        return _latest(df, 'close')

    elif ind == "VOLUME":
        return _latest(df, 'volume')

    elif ind == "BODY_PCT":
        return _latest(df, 'body_pct')

    return None


def _get_rhs(cmp_to: str, cmp_p: str, ind_p: str, df: pd.DataFrame):
    c = df['close']

    if cmp_to == "VALUE":
        return float(cmp_p)

    elif cmp_to == "EMA":
        period = int(cmp_p or ind_p or 9)
        col = f'ema_{period}'
        if col not in df.columns:
            df[col] = ema(c, period)
        return _latest(df, col)

    elif cmp_to == "MACD_SIGNAL":
        return _latest(df, 'macd_signal')

    elif cmp_to == "STOCH_D":
        return _latest(df, 'stoch_d')

    elif cmp_to == "VWAP":
        return _latest(df, 'vwap')

    elif cmp_to == "CLOSE":
        return _latest(df, 'close')

    elif cmp_to == "OBV_EMA":
        period = int(cmp_p or 5)
        return _latest(df, 'obv_ema_5')

    elif cmp_to == "HA_EMA":
        period = int(cmp_p or 9)
        col = f'ha_ema_{period}'
        if col not in df.columns:
            df[col] = ema(df['ha_close'], period)
        return _latest(df, col)

    elif cmp_to == "VOLUME_MA":
        return _latest(df, 'volume_ma_20')

    elif cmp_to == "BB_UPPER":
        p, s = _bb_params(cmp_p)
        col = f'bb_upper_{p}_{s}'
        if col not in df.columns:
            df[f'bb_upper_{p}_{s}'], df[f'bb_mid_{p}_{s}'], df[f'bb_lower_{p}_{s}'] = bollinger(c, p, s)
        return _latest(df, col)

    elif cmp_to == "BB_LOWER":
        p, s = _bb_params(cmp_p)
        col = f'bb_lower_{p}_{s}'
        if col not in df.columns:
            df[f'bb_upper_{p}_{s}'], df[f'bb_mid_{p}_{s}'], df[f'bb_lower_{p}_{s}'] = bollinger(c, p, s)
        return _latest(df, col)

    return None


def _compare(lhs, rhs, op):
    if op == "gt":  return lhs > rhs
    if op == "gte": return lhs >= rhs
    if op == "lt":  return lhs < rhs
    if op == "lte": return lhs <= rhs
    return False


def _bb_params(param: str):
    parts = str(param).split(',')
    period = int(parts[0]) if parts else 20
    std    = float(parts[1]) if len(parts) > 1 else 2.0
    return period, std


# ─── Full Strategy Evaluator ─────────────────────────────────────────────────

def evaluate_strategy(strategy: dict, data: dict, refs: dict) -> dict:
    """
    Evaluate all conditions in a strategy against live data.
    Returns full result with passed/failed breakdown and score.
    """
    conditions  = strategy['conditions']
    match_type  = strategy.get('match_type', 'ALL')
    threshold   = strategy.get('threshold_pct', 100.0)

    results = []
    for cond in conditions:
        res = evaluate_condition(cond, data)
        res['condition'] = cond
        results.append(res)

    passed  = [r for r in results if r['passed']]
    failed  = [r for r in results if not r['passed']]
    total   = len(results)
    score   = len(passed) / total * 100 if total > 0 else 0

    if match_type == 'ALL':
        triggered = len(failed) == 0
    elif match_type == 'ANY':
        triggered = len(passed) > 0
    else:  # THRESHOLD
        triggered = score >= threshold

    return {
        "strategy_id":   strategy['id'],
        "strategy_name": strategy['name'],
        "signal_type":   strategy['signal_type'],
        "total":         total,
        "passed_count":  len(passed),
        "failed_count":  len(failed),
        "score_pct":     round(score, 1),
        "triggered":     triggered,
        "match_type":    match_type,
        "passed":        passed,
        "failed":        failed,
    }
