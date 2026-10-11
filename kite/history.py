"""Durable live observation archive. Never reconstructs signals from future data."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Kolkata')


class HistoryStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, token INTEGER, day TEXT, kind TEXT,
                    candle INTEGER, payload TEXT);
                CREATE INDEX IF NOT EXISTS events_lookup ON events(token,day,kind);
                CREATE UNIQUE INDEX IF NOT EXISTS confirmed_once
                    ON events(token,candle) WHERE kind='confirmed';
                CREATE TABLE IF NOT EXISTS charts (
                    token INTEGER, day TEXT, timeframe TEXT, payload TEXT,
                    PRIMARY KEY(token,day,timeframe));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, kind, event):
        candle = event.get('candle')
        if not candle:
            return
        day = datetime.fromtimestamp(candle['time'], TZ).date().isoformat()
        payload = {k: v for k, v in event.items() if k != 'confirmed_signal'}
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO events(token,day,kind,candle,payload) VALUES(?,?,?,?,?)',
                       (event['instrument_token'], day, kind, candle['time'], json.dumps(payload)))

    def save_charts(self, token, day, charts):
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO charts VALUES(?,?,?,?)',
                           [(token, day, tf, json.dumps(chart)) for tf, chart in charts.items()])

    def days(self, token):
        with self.connect() as db:
            return [r[0] for r in db.execute(
                'SELECT DISTINCT day FROM events WHERE token=? ORDER BY day DESC', (token,))]

    def events(self, token, day, kind):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM events WHERE token=? AND day=? AND kind=? ORDER BY candle,id',
                (token, day, kind))]

    def chart(self, token, day, timeframe):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM charts WHERE token=? AND day=? AND timeframe=?',
                             (token, day, timeframe)).fetchone()
        return json.loads(row[0]) if row else None
