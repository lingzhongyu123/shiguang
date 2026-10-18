"""User-facing dates are always Asia/Shanghai, independent of the host."""
from datetime import date, datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def today():
    return datetime.now(SHANGHAI).date().isoformat()


def valid_date(value):
    if not isinstance(value, str):
        return ''
    try:
        return date.fromisoformat(value).isoformat() if len(value) == 10 else ''
    except ValueError:
        return ''


def period_window(mode, end):
    days = 7 if mode == '/周总结' else 30
    return ((date.fromisoformat(end) - timedelta(days=days - 1)).isoformat(), end)
