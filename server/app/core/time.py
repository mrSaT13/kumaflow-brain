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

from datetime import datetime, timezone


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
