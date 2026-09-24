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
def _parse_tz_cached(raw: str):
    return parse_tz(raw)


def parse_tz(raw: str):
    """Строго распарсить зону: IANA-имя или сдвиг +4 / -5:30. Ошибка → ValueError."""
    from zoneinfo import ZoneInfo

    v = (raw or "").strip()
    if not v or v.upper() == "UTC":
        return timezone.utc
    import re

    m = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", v)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        mins = int(m.group(2)) * 60 + int(m.group(3) or 0)
        if mins > 14 * 60:
            raise ValueError(f"bad offset: {raw}")
        return timezone(sign * timedelta(minutes=mins), name=v)
    try:
        return ZoneInfo(v)
    except Exception as e:
        raise ValueError(f"unknown timezone: {raw}") from e


_TZ_TTL_SEC = 60
_tz_cache: dict = {"name": None, "ts": 0.0}


def db_tz_name() -> str | None:
    """Зона из веб-настроек (AppSetting.app_timezone). None — не задана/БД недоступна."""
    import time as _t

    now = _t.time()
    if _tz_cache["name"] is not None and now - _tz_cache["ts"] < _TZ_TTL_SEC:
        return _tz_cache["name"]
    try:
        from app.db.database import session_scope as _scope

        from app.db.models import AppSetting as _AS

        with _scope() as db:
            row = db.get(_AS, "app_timezone")
            v = (row.value or {}).get("timezone", "") if row and isinstance(row.value, dict) else ""
            v = str(v or "").strip()
            _tz_cache.update(name=v or None, ts=now)
            return v or None
    except Exception:
        return _tz_cache["name"]


def resolve_tz_name() -> str:
    """Приоритет: веб-настройка → APP_TIMEZONE → UTC."""
    from app.core.config import get_settings

    return db_tz_name() or (get_settings().app_timezone or "UTC")


def server_tz(name: str = ""):
    """Часовой пояс сервера. Пустое имя → резолв (веб-настройка → env → UTC)."""
    raw = (name or "").strip() or resolve_tz_name()
    try:
        return _parse_tz_cached(raw)
    except ValueError:
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
