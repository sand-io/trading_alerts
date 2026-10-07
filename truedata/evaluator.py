"""
Rule Evaluator
Evaluates parsed Rule objects against computed indicator DataFrames.
Returns which rules passed, failed, and overall signal strength.
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd
from rule_parser import Rule


@dataclass
class RuleResult:
    rule: Rule
    passed: bool
    lhs_value: Optional[float]
    rhs_value: Optional[float]
    reason: str


def compare(lhs: float, rhs: float, condition: str) -> bool:
    if condition == "gt":  return lhs > rhs
    if condition == "gte": return lhs >= rhs
    if condition == "lt":  return lhs < rhs
    if condition == "lte": return lhs <= rhs
    return False


def get_latest(df: pd.DataFrame, col: str) -> Optional[float]:
    if df is None or col not in df.columns:
        return None
    val = df[col].iloc[-1]
    return None if pd.isna(val) else float(val)


def get_prev(df: pd.DataFrame, col: str) -> Optional[float]:
    if df is None or len(df) < 2 or col not in df.columns:
        return None
    val = df[col].iloc[-2]
    return None if pd.isna(val) else float(val)


# ─── Main Evaluator ──────────────────────────────────────────────────────────

def evaluate_rule(rule: Rule, data: dict, refs: dict) -> RuleResult:
    """
    data = {
        '5min':  DataFrame with indicators,
        '15min': DataFrame with indicators,
        '1hour': DataFrame with indicators,
        '4hour': DataFrame with indicators,
        '1day':  DataFrame with indicators,
        '1week': DataFrame with indicators,
    }
    refs = { 'yesterday_low', 'last_week_high', 'last_week_low', 'last_4_days_high_close' }
    """
    tf = rule.timeframe
    df = data.get(tf)

    lhs = None
    rhs = None

    try:
        ind = rule.indicator
        val = rule.value
        cond = rule.condition

        # ── LHS resolution ──
        if ind == "ADX":
            lhs = get_latest(df, 'adx_14')
            rhs = float(val)

        elif ind == "RSI":
            lhs = get_latest(df, 'rsi_14')
            rhs = float(val)

        elif ind == "STOCH_K":
            lhs = get_latest(df, 'stoch_k')
            rhs = get_latest(df, 'stoch_d')

        elif ind == "MACD_LINE":
            lhs = get_latest(df, 'macd_line')
            rhs = get_latest(df, 'macd_signal')

        elif ind == "OBV":
            lhs = get_latest(df, 'obv')
            rhs_col = val.lower().replace("obv_ema_", "obv_ema_")
            rhs = get_latest(df, 'obv_ema_5')

        elif ind.startswith("HA_EMA_"):
            period = ind.split("_")[-1]
            lhs = get_latest(df, f'ha_ema_{period}')
            rhs_period = val.split("_")[-1]
            rhs = get_latest(df, f'ha_ema_{rhs_period}')

        elif ind == "CLOSE" and val.startswith("EMA_"):
            lhs = get_latest(df, 'close')
            period = val.split("_")[-1]
            rhs = get_latest(df, f'ema_{period}')

        elif ind.startswith("EMA_") and val.startswith("EMA_"):
            p1 = ind.split("_")[-1]
            p2 = val.split("_")[-1]
            # Handle "previous" close check for 4hour
            if "previous" in rule.raw.lower():
                lhs = get_prev(df, f'ema_{p2}')
                rhs = get_prev(df, f'ema_{p2}')
                # Actually for "prev close above EMA(9)"
                lhs = get_prev(df, 'close')
                rhs = get_prev(df, f'ema_{p2}')
            else:
                lhs = get_latest(df, f'ema_{p1}')
                rhs = get_latest(df, f'ema_{p2}')

        elif ind == "CLOSE" and val == "VWAP":
            lhs = get_latest(df, 'close')
            rhs = get_latest(df, 'vwap')

        elif ind == "CLOSE" and val == "YESTERDAY_LOW":
            lhs = get_latest(df, 'close')
            rhs = refs.get('yesterday_low')

        elif ind == "CLOSE" and val == "LAST_WEEK_LOW":
            lhs = get_latest(df, 'close')
            rhs = refs.get('last_week_low')

        elif ind == "CLOSE" and val == "LAST_WEEK_HIGH":
            lhs = get_latest(df, 'close')
            rhs = refs.get('last_week_high')

        elif ind == "CLOSE" and val == "LAST_4_DAYS_HIGH_CLOSE":
            lhs = get_latest(df, 'close')
            rhs = refs.get('last_4_days_high_close')

        elif ind == "CLOSE" and val.startswith("LOWEST_LOW_"):
            lhs = get_latest(df, 'close')
            rhs = get_latest(df, 'lowest_low_18')

        elif ind == "CLOSE" and val.startswith("HIGHEST_HIGH_"):
            lhs = get_latest(df, 'close')
            rhs = get_latest(df, 'highest_high_18')

        elif ind == "RANGE_10" and val.startswith("ATR_x"):
            multiplier = float(val.replace("ATR_x", ""))
            lhs = get_latest(df, 'range_10')
            atr_val = get_latest(df, 'atr_14')
            rhs = multiplier * atr_val if atr_val else None

        elif ind == "BODY_PCT":
            lhs = get_latest(df, 'body_pct')
            rhs = float(val)

        elif ind == "FIRST_HIGH":
            # Current high > first candle high
            lhs = get_latest(df, 'high')
            rhs = get_latest(df, 'first_high')

        elif ind == "CURRENT_LOW":
            # Current low < first candle low
            lhs = get_latest(df, 'low')
            rhs = get_latest(df, 'first_low')

        # ── Evaluate ──
        if lhs is None or rhs is None:
            return RuleResult(rule, False, lhs, rhs, "Missing data")

        passed = compare(lhs, rhs, cond)
        return RuleResult(
            rule=rule,
            passed=passed,
            lhs_value=round(lhs, 4),
            rhs_value=round(rhs, 4),
            reason=f"{lhs:.4f} {cond} {rhs:.4f} → {'✅' if passed else '❌'}"
        )

    except Exception as e:
        return RuleResult(rule, False, lhs, rhs, f"Error: {e}")


def evaluate_all_rules(rules: list[Rule], data: dict, refs: dict) -> list[RuleResult]:
    return [evaluate_rule(r, data, refs) for r in rules]


def score_results(results: list[RuleResult]) -> dict:
    passed = [r for r in results if r.passed]
    total = len(results)
    score = len(passed) / total * 100 if total > 0 else 0
    return {
        "total": total,
        "passed": len(passed),
        "failed": total - len(passed),
        "score_pct": round(score, 1),
        "results": results
    }
