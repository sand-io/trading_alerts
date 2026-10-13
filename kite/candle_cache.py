"""Reusable broker candle history, independent of saved strategy evaluations."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Kolkata')


def wall_time(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp.astimezone(TZ).replace(tzinfo=None) if stamp.tzinfo else stamp


def history_end(value):
    """Avoid redownloading an unchanged regular session overnight or at weekends."""
    value = wall_time(value)
    requested = value
    opening = value.replace(hour=9, minute=15, second=0, microsecond=0)
    if value < opening:
        value -= timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    close = value.replace(hour=15, minute=30, second=0, microsecond=0)
    return min(requested, close)


class CandleCache:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS candle_cache (
                token INTEGER, interval TEXT, start TEXT, end TEXT, refreshed TEXT,
                payload TEXT, PRIMARY KEY(token, interval))''')

    def prune(self, now):
        cutoff = (wall_time(now) - timedelta(days=7)).isoformat()
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute('DELETE FROM candle_cache WHERE refreshed <= ?', (cutoff,))

    def load(self, token, interval, start, now):
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            row = db.execute('SELECT start,end,refreshed,payload FROM candle_cache WHERE token=? AND interval=?',
                             (token, interval)).fetchone()
        if not row or wall_time(row[0]) > start or wall_time(row[2]) <= now - timedelta(days=7):
            return None
        records = json.loads(row[3])
        for record in records:
            record['date'] = wall_time(record['date'])
        return wall_time(row[1]), records

    def save(self, token, interval, start, end, records, now):
        payload = json.dumps([{**r, 'date': wall_time(r['date']).isoformat()} for r in records])
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute('INSERT OR REPLACE INTO candle_cache VALUES(?,?,?,?,?,?)',
                       (token, interval, start.isoformat(), end.isoformat(), now.isoformat(), payload))
