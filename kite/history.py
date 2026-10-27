"""Durable live observation archive. Never reconstructs signals from future data."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Kolkata')


class HistoryStore:
    def __init__(self, path, retention_days=None):
        value = os.getenv('KITE_HISTORY_RETENTION_DAYS', '1') if retention_days is None else retention_days
        try:
            self.retention_days = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError('KITE_HISTORY_RETENTION_DAYS must be a positive integer') from None
        if self.retention_days < 1:
            raise ValueError('KITE_HISTORY_RETENTION_DAYS must be a positive integer')
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, token INTEGER, day TEXT, kind TEXT,
                    candle INTEGER, payload TEXT, strategy TEXT NOT NULL DEFAULT 'default');
                CREATE INDEX IF NOT EXISTS events_lookup ON events(token,day,kind);
                CREATE INDEX IF NOT EXISTS events_day_kind ON events(day,kind,id);

                CREATE TABLE IF NOT EXISTS strategy_charts (
                    token INTEGER, day TEXT, timeframe TEXT, strategy TEXT, payload TEXT,
                    PRIMARY KEY(token,day,timeframe,strategy));
                CREATE TABLE IF NOT EXISTS charts (
                    token INTEGER, day TEXT, timeframe TEXT, payload TEXT,
                    PRIMARY KEY(token,day,timeframe));
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(events)')}
            if 'strategy' not in columns:
                db.execute("ALTER TABLE events ADD COLUMN strategy TEXT NOT NULL DEFAULT 'default'")
            if 'confirmation_timeframe' not in columns:
                db.execute("ALTER TABLE events ADD COLUMN confirmation_timeframe TEXT NOT NULL DEFAULT '5m'")
            db.execute('CREATE INDEX IF NOT EXISTS events_strategy_lookup ON events(token,day,strategy,kind)')
            db.execute('DROP INDEX IF EXISTS confirmed_once')
            db.execute('DROP INDEX IF EXISTS confirmed_strategy_once')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS confirmed_timeframe_once ON events(token,candle,strategy,confirmation_timeframe) WHERE kind='confirmed'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, kind, event):
        self.record_many([(kind, event)])

    def record_many(self, events):
        rows = []
        for kind, event in events:
            candle = event.get('candle')
            if not candle:
                continue
            day = datetime.fromtimestamp(candle['time'], TZ).date().isoformat()
            payload = {k: v for k, v in event.items() if k != 'confirmed_signal'}
            rows.append((event['instrument_token'], day, kind, candle['time'], json.dumps(payload),
                         event.get('strategy_id', 'default'), event.get('confirmation_timeframe', '5m')))
        if not rows:
            return
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO events(token,day,kind,candle,payload,strategy,confirmation_timeframe) VALUES(?,?,?,?,?,?,?)', rows)

    def prune(self, now=None):
        """Keep the configured number of IST calendar days, including today."""
        now = now or datetime.now(TZ)
        day = now.astimezone(TZ).date() if now.tzinfo else now.date()
        cutoff = (day - timedelta(days=self.retention_days - 1)).isoformat()
        with self.connect() as db:
            for table in ('events', 'charts', 'strategy_charts'):
                db.execute(f'DELETE FROM {table} WHERE day < ?', (cutoff,))

    def save_charts(self, token, day, charts):
        rows = [(token, day, tf, json.dumps(chart)) for tf, chart in charts.items()]
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO charts VALUES(?,?,?,?)', rows)

    def days(self, token):
        with self.connect() as db:
            return [r[0] for r in db.execute(
                'SELECT DISTINCT day FROM events WHERE token=? ORDER BY day DESC', (token,))]

    def events(self, token, day, kind, strategy_id=None):
        with self.connect() as db:
            query = 'SELECT payload FROM events WHERE token=? AND day=? AND kind=?'
            args = [token, day, kind]
            if strategy_id is not None:
                query += ' AND strategy=?'
                args.append(strategy_id)
            return [json.loads(r[0]) for r in db.execute(query + ' ORDER BY id', args)]

    def chart(self, token, day, timeframe):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM charts WHERE token=? AND day=? AND timeframe=?',
                             (token, day, timeframe)).fetchone()
        return json.loads(row[0]) if row else None

    def alert_days(self):
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT DISTINCT day FROM events WHERE kind='alert' ORDER BY day DESC")]

    def daily_alerts(self, day, side=None, strategy_id=None, search='', before_id=None, limit=100):
        """Read accepted alerts across every symbol; page without loading evaluations."""
        clauses, args = ["day=?", "kind='alert'"], [day]
        if side:
            clauses.append("json_extract(payload,'$.alert_side')=?")
            args.append(side)
        if strategy_id:
            clauses.append('strategy=?')
            args.append(strategy_id)
        if search.strip():
            clauses.append("instr(lower(coalesce(json_extract(payload,'$.symbol'),'') || ' ' || "
                           "coalesce(json_extract(payload,'$.strategy_name'),strategy)),lower(?))>0")
            args.append(search.strip())
        where = ' AND '.join(clauses)
        with self.connect() as db:
            counts = dict(db.execute("SELECT coalesce(json_extract(payload,'$.alert_side'),'unknown'),count(*) "
                                     f'FROM events WHERE {where} GROUP BY 1', args))
            strategies = [{'id': row[0], 'name': row[1], 'alert_count': row[2]} for row in db.execute(
                "SELECT strategy,coalesce(json_extract(payload,'$.strategy_name'),strategy),count(*) "
                "FROM events WHERE day=? AND kind='alert' GROUP BY strategy ORDER BY strategy", (day,))]
            page_where, page_args = where, list(args)
            if before_id is not None:
                page_where += ' AND id<?'
                page_args.append(before_id)
            rows = list(db.execute(f'SELECT id,payload FROM events WHERE {page_where} ORDER BY id DESC LIMIT ?',
                                   [*page_args, limit + 1]))
        alerts = [{'id': row[0], 'day': day, 'event': json.loads(row[1])} for row in rows[:limit]]
        return {'day': day, 'alerts': alerts, 'total': sum(counts.values()),
                'bullish': counts.get('bullish', 0), 'bearish': counts.get('bearish', 0),
                'strategies': strategies, 'retention_days': self.retention_days,
                'next_cursor': alerts[-1]['id'] if len(rows) > limit else None}

    def alert(self, alert_id):
        with self.connect() as db:
            row = db.execute("SELECT day,payload FROM events WHERE id=? AND kind='alert'", (alert_id,)).fetchone()
        return {'id': alert_id, 'day': row[0], 'event': json.loads(row[1])} if row else None

    def save_strategy_charts(self, token, day, strategy_id, charts):
        rows = [(token, day, tf, strategy_id, json.dumps(chart)) for tf, chart in charts.items()]
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO strategy_charts VALUES(?,?,?,?,?)', rows)

    def strategy_chart(self, token, day, timeframe, strategy_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM strategy_charts WHERE token=? AND day=? AND timeframe=? AND strategy=?',
                             (token, day, timeframe, strategy_id)).fetchone()
        return json.loads(row[0]) if row else self.chart(token, day, timeframe)

    def session_strategies(self, token, day):
        with self.connect() as db:
            rows = db.execute("SELECT strategy,payload FROM events WHERE token=? AND day=? AND kind='evaluation' ORDER BY id", (token, day))
            result = {}
            for strategy_id, payload in rows:
                event = json.loads(payload)
                entry = result.setdefault(strategy_id, {'id': strategy_id, 'name': event.get('strategy_name', strategy_id),
                                                        'evaluations': 0, 'versions': [], 'confirmation_timeframes': []})
                entry['name'] = event.get('strategy_name', entry['name'])
                entry['evaluations'] += 1
                timeframe = event.get('confirmation_timeframe', '5m')
                if timeframe not in entry['confirmation_timeframes']:
                    entry['confirmation_timeframes'].append(timeframe)
                version = event.get('strategy_version')
                if version and version not in entry['versions']:
                    entry['versions'].append(version)
        return list(result.values())
