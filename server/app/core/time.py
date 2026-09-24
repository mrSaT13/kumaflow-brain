"""Единое время для всего бэкенда.

Проблема: везде использовался datetime.utcnow() (naive, deprecated в 3.12+,
без таймзоны — хаос при сравнении с timestamptz и кроном).
Правило:
- в БД храним naive UTC (совместимо с существующими DateTime-колонками);
- новый код использует utcnow() отсюда, а не datetime.utcnow() напрямую;
- сравнение крона делает через aware-UTC и приводит к naive;
- миграция на timestamptz — отдельным alembic-шагом (см. docs/time-tz.md).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache


def utcnow() -> datetime:
    """Текущее UTC как naive datetime (совместимо с DateTime-колонками)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_aware() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime | None = None) -> str:
    d = dt or utcnow()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat().replace("+00:00", "Z")


@lru_cache(maxsize=4)
def server_tz(name: str = ""):
    """Часовой пояс сервера (APP_TIMEZONE): IANA-имя (нужна tzdata в образе)
    либо фиксированный сдвиг вида +4 / -5:30. Неизвестное → UTC."""
    from app.core.config import get_settings

    raw = (name or get_settings().app_timezone or "UTC").strip()
    if raw.upper() == "UTC":
        return timezone.utc
    import re

    m = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", raw)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        mins = int(m.group(2)) * 60 + int(m.group(3) or 0)
        return timezone(sign * timedelta(minutes=mins), name=raw)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(raw)
    except Exception:
        return timezone.utc


def server_now() -> datetime:
    """Текущее время в поясе сервера (aware)."""
    return datetime.now(timezone.utc).astimezone(server_tz())


def local_today():
    """Дата «сегодня» в поясе сервера — граница суток для daily-плейлистов и крона."""
    from datetime import date as _date

    return _date.fromtimestamp(datetime.now(timezone.utc).timestamp(),
                               tz=server_tz())


def local_hour() -> int:
    """Час суток 0-23 в поясе сервера (контекст «утро/вечер» для волны)."""
    return server_now().hour
