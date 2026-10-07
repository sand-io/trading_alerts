"""
Strategy Database
SQLite storage for user-created strategies.
"""

import json
import sqlite3
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional


DB_PATH = "strategies.db"


@dataclass
class Condition:
    timeframe: str      # 5min, 15min, 1hour, 4hour, 1day, 1week
    indicator: str      # RSI, EMA, MACD_LINE, ADX, VWAP, OBV, STOCH_K, ATR, BB_UPPER etc.
    indicator_param: str  # e.g. "14" for RSI(14), "9" for EMA(9), "26,12,9" for MACD
    condition: str      # gt, gte, lt, lte
    compare_to: str     # VALUE, EMA, MACD_SIGNAL, STOCH_D, VWAP, BB_LOWER, BB_UPPER etc.
    compare_param: str  # e.g. "60" for RSI > 60, "50" for EMA(9) > EMA(50)


@dataclass
class Strategy:
    id: Optional[int]
    name: str
    description: str
    signal_type: str        # BULLISH, BEARISH, CUSTOM
    conditions: list        # list of Condition dicts
    match_type: str         # ALL (and), ANY (or)
    symbols: list           # list of symbol strings
    mode: str               # LIVE, ONETIME
    threshold_pct: float    # % of conditions that must pass
    is_active: bool
    created_at: str
    updated_at: str


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT,
                signal_type TEXT NOT NULL DEFAULT 'CUSTOM',
                conditions  TEXT NOT NULL,
                match_type  TEXT NOT NULL DEFAULT 'ALL',
                symbols     TEXT NOT NULL,
                mode        TEXT NOT NULL DEFAULT 'LIVE',
                threshold_pct REAL NOT NULL DEFAULT 100.0,
                is_active   INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                score_pct   REAL NOT NULL,
                passed_conditions TEXT NOT NULL,
                failed_conditions TEXT NOT NULL,
                triggered_at TEXT NOT NULL,
                FOREIGN KEY (strategy_id) REFERENCES strategies(id)
            )
        """)
        conn.commit()


# ─── CRUD ────────────────────────────────────────────────────────────────────

def create_strategy(data: dict) -> int:
    now = datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO strategies
            (name, description, signal_type, conditions, match_type, symbols, mode, threshold_pct, is_active, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data['name'],
            data.get('description', ''),
            data.get('signal_type', 'CUSTOM'),
            json.dumps(data['conditions']),
            data.get('match_type', 'ALL'),
            json.dumps(data['symbols']),
            data.get('mode', 'LIVE'),
            data.get('threshold_pct', 100.0),
            1,
            now, now
        ))
        conn.commit()
        return cur.lastrowid


def get_strategy(strategy_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_all_strategies(active_only: bool = False) -> list:
    with get_conn() as conn:
        query = "SELECT * FROM strategies"
        if active_only:
            query += " WHERE is_active=1"
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_strategy(strategy_id: int, data: dict) -> bool:
    now = datetime.now().isoformat()
    fields = []
    values = []
    for key in ['name', 'description', 'signal_type', 'match_type', 'mode', 'threshold_pct', 'is_active']:
        if key in data:
            fields.append(f"{key}=?")
            values.append(data[key])
    if 'conditions' in data:
        fields.append("conditions=?")
        values.append(json.dumps(data['conditions']))
    if 'symbols' in data:
        fields.append("symbols=?")
        values.append(json.dumps(data['symbols']))
    fields.append("updated_at=?")
    values.append(now)
    values.append(strategy_id)

    with get_conn() as conn:
        conn.execute(f"UPDATE strategies SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    return True


def delete_strategy(strategy_id: int) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM strategies WHERE id=?", (strategy_id,))
        conn.commit()
    return True


def save_alert(strategy_id: int, symbol: str, signal_type: str,
               score_pct: float, passed: list, failed: list):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO strategy_alerts
            (strategy_id, symbol, signal_type, score_pct, passed_conditions, failed_conditions, triggered_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            strategy_id, symbol, signal_type, score_pct,
            json.dumps(passed), json.dumps(failed),
            datetime.now().isoformat()
        ))
        conn.commit()


def get_alerts(strategy_id: int = None, limit: int = 50) -> list:
    with get_conn() as conn:
        if strategy_id:
            rows = conn.execute(
                "SELECT * FROM strategy_alerts WHERE strategy_id=? ORDER BY triggered_at DESC LIMIT ?",
                (strategy_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strategy_alerts ORDER BY triggered_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row)
    d['conditions'] = json.loads(d['conditions'])
    d['symbols']    = json.loads(d['symbols'])
    d['is_active']  = bool(d['is_active'])
    return d
