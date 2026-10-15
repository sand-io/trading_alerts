"""Market wall-time session boundaries; stock defaults remain unchanged."""
from datetime import timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
US_EASTERN = ZoneInfo('America/New_York')


def session_bounds(value, exchange='NSE'):
    if value.tzinfo is not None:
        value = value.astimezone(IST).replace(tzinfo=None)
    if exchange == 'MCX':
        # Non-agricultural futures follow US daylight saving time.
        summer = bool(value.replace(hour=12, tzinfo=US_EASTERN).dst())
        opening = value.replace(hour=9, minute=0, second=0, microsecond=0)
        closing = value.replace(hour=23, minute=30 if summer else 55, second=0, microsecond=0)
    else:
        opening = value.replace(hour=9, minute=15, second=0, microsecond=0)
        closing = value.replace(hour=15, minute=30, second=0, microsecond=0)
    return opening, closing


def session_is_open(value, exchange='NSE'):
    if value.tzinfo is not None:
        value = value.astimezone(IST).replace(tzinfo=None)
    opening, closing = session_bounds(value, exchange)
    return value.weekday() < 5 and opening <= value < closing


def cached_history_end(value, exchange='NSE'):
    if value.tzinfo is not None:
        value = value.astimezone(IST).replace(tzinfo=None)
    requested = value
    if value < session_bounds(value, exchange)[0]:
        value -= timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return min(requested, session_bounds(value, exchange)[1])
