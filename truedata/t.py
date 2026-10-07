"""
Dynamic Rule Parser
Parses bullish.txt / bearish.txt into executable condition functions.
Rules can be updated anytime — just edit the .txt files.
"""

import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Rule:
    raw: str           # Original text from file
    timeframe: str     # e.g. "5min", "1hour", "4hour", "1day", "1week"
    indicator: str     # e.g. "ADX", "EMA", "RSI", "MACD", "OBV", "Close"
    condition: str     # e.g. "greater_than", "less_than", "above", "below"
    value: str         # e.g. "30", "EMA(9)", "VWAP"
    params: dict       # Extra parsed params like period, fast, slow


# ─── Normalizers ────────────────────────────────────────────────────────────

TIMEFRAME_MAP = {
    "5-minute": "5min", "5 min": "5min", "5min": "5min",
    "15-minute": "15min", "15 min": "15min",
    "1-hour": "1hour", "1 hour": "1hour",
    "4-hour": "4hour", "4 hour": "4hour",
    "1-day": "1day", "1 day": "1day", "daily": "1day",
    "1-week": "1week", "1 week": "1week", "weekly": "1week",
}

CONDITION_MAP = {
    "greater than": "gt", "greater than or equal to": "gte",
    "less than": "lt", "less than or equal to": "lte",
    "above": "gt", "below": "lt",
    "is above": "gt", "is below": "lt",
    "is greater than": "gt", "is less than": "lt",
    "is greater than or equal to": "gte", "is less than or equal to": "lte",
}


def normalize_timeframe(text: str) -> str:
    text_lower = text.lower().strip()
    for k, v in TIMEFRAME_MAP.items():
        if k in text_lower:
            return v
    return "5min"


def normalize_condition(text: str) -> str:
    text_lower = text.lower().strip()
    for k, v in sorted(CONDITION_MAP.items(), key=lambda x: -len(x[0])):
        if k in text_lower:
            return v
    return "gt"


def extract_params(text: str) -> dict:
    params = {}
    matches = re.findall(r'\((\d+(?:,\d+)*)\)', text)
    if matches:
        all_nums = []
        for m in matches:
            all_nums.extend([int(x) for x in m.split(',')])
        params['periods'] = all_nums
    return params


# ─── Rule Parser ────────────────────────────────────────────────────────────

def parse_rules(filepath: str) -> list[Rule]:
    rules = []
    with open(filepath, 'r') as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if not line or line.lower().startswith("bullish") or line.lower().startswith("bearish"):
            continue

        rule = parse_single_rule(line)
        if rule:
            rules.append(rule)

    return rules


def parse_single_rule(line: str) -> Optional[Rule]:
    line_lower = line.lower()
    params = extract_params(line)

    # Detect timeframe
    timeframe = "5min"
    for k, v in sorted(TIMEFRAME_MAP.items(), key=lambda x: -len(x[0])):
        if k in line_lower:
            timeframe = v
            break

    # Detect condition
    condition = normalize_condition(line_lower)

    indicator = "unknown"
    value = "unknown"

    if "adx" in line_lower:
        indicator = "ADX"
        m = re.search(r'greater than\s+(\d+)', line_lower)
        value = m.group(1) if m else "30"

    elif "stochastic" in line_lower or "%k" in line_lower:
        indicator = "STOCH_K"
        value = "STOCH_D"

    elif "macd line" in line_lower or ("macd" in line_lower and "signal" in line_lower):
        indicator = "MACD_LINE"
        value = "MACD_SIGNAL"

    elif "obv" in line_lower:
        indicator = "OBV"
        m = re.search(r'ema\s*\((\d+)\)', line_lower)
        value = f"OBV_EMA_{m.group(1)}" if m else "OBV_EMA_5"

    elif "heikin-ashi" in line_lower or "heikin ashi" in line_lower:
        emas = re.findall(r'ema\s*\((\d+)\)', line_lower)
        if len(emas) >= 2:
            indicator = f"HA_EMA_{emas[0]}"
            value = f"HA_EMA_{emas[1]}"
        else:
            indicator = "HA_EMA_5"
            value = "HA_EMA_9"

    elif "vwap" in line_lower:
        indicator = "CLOSE"
        value = "VWAP"

    elif "rsi" in line_lower:
        indicator = "RSI"
        m = re.search(r'(?:greater than|less than|>|<)\s+(\d+)', line_lower)
        value = m.group(1) if m else ("60" if "greater" in line_lower else "40")

    elif "ema" in line_lower:
        emas = re.findall(r'ema\s*\((\d+)\)', line_lower)
        if "close" in line_lower and len(emas) == 1:
            indicator = "CLOSE"
            value = f"EMA_{emas[0]}"
        elif len(emas) >= 2:
            indicator = f"EMA_{emas[0]}"
            value = f"EMA_{emas[1]}"
        elif len(emas) == 1:
            indicator = "CLOSE"
            value = f"EMA_{emas[0]}"

    elif "atr" in line_lower and "range" in line_lower:
        indicator = "RANGE_10"
        m = re.search(r'(\d+)\s*[×x\*]\s*atr', line_lower)
        multiplier = m.group(1) if m else "5"
        value = f"ATR_x{multiplier}"

    elif "body size" in line_lower or "candle body" in line_lower:
        indicator = "BODY_PCT"
        m = re.search(r'(\d+)%', line_lower)
        value = m.group(1) if m else "55"

    elif "last week low" in line_lower:
        indicator = "CLOSE"
        value = "LAST_WEEK_LOW"

    elif "last week high" in line_lower:
        indicator = "CLOSE"
        value = "LAST_WEEK_HIGH"

    elif "yesterday's low" in line_lower or "yesterday low" in line_lower:
        indicator = "CLOSE"
        value = "YESTERDAY_LOW"

    elif "yesterday's high" in line_lower or "yesterday high" in line_lower:
        indicator = "CLOSE"
        value = "YESTERDAY_HIGH"

    elif "last 4 days close" in line_lower:
        indicator = "CLOSE"
        value = "LAST_4_DAYS_HIGH_CLOSE"

    elif "lowest low of the last" in line_lower or "lowest low" in line_lower:
        m = re.search(r'last\s+(\d+)\s+candles', line_lower)
        n = m.group(1) if m else "18"
        indicator = "CLOSE"
        value = f"LOWEST_LOW_{n}"

    elif "highest high of last" in line_lower or "highest high" in line_lower:
        m = re.search(r'last\s+(\d+)\s+candles', line_lower)
        n = m.group(1) if m else "18"
        indicator = "CLOSE"
        value = f"HIGHEST_HIGH_{n}"

    elif "current" in line_lower and "high" in line_lower and "first" in line_lower:
        indicator = "FIRST_HIGH"
        value = "CURRENT_HIGH"

    elif "current" in line_lower and "low" in line_lower and "first" in line_lower:
        indicator = "CURRENT_LOW"
        value = "FIRST_LOW"

    elif "volume" in line_lower:               # ← VOLUME — must be before "close"
        indicator = "VOLUME"
        value = "VOLUME_MA_20"

    elif "close" in line_lower:
        indicator = "CLOSE"
        value = "CLOSE_REF"

    if indicator == "unknown":
        print(f"  [WARN] Could not parse rule: {line}")
        return None

    return Rule(
        raw=line,
        timeframe=timeframe,
        indicator=indicator,
        condition=condition,
        value=value,
        params=params
    )


def delete_from_sqlite(db_path: str, query: str, params: tuple = ()) -> int:
    """
    Executes a DELETE query on a SQLite database and returns the number of deleted rows.
    Example: delete_from_sqlite('strategies.db', 'DELETE FROM strategies WHERE id=?', (1,))
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(query, params)
            conn.commit()
            return cursor.rowcount
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
        return 0
# import sqlite3, json
# conn = sqlite3.connect("strategies.db")
# rows = conn.execute("SELECT id, name, mode, is_active, symbols FROM strategies").fetchall()
# for r in rows:
#     print(f"id={r[0]} name={r[1]} mode={r[2]} active={r[3]} symbols={r[4]}")
# delete_from_sqlite('strategies.db', 'DELETE FROM strategy_alerts')