"""Безопасный резолв Track/MediaUser по uuid ИЛИ external_id.

Проблема со скрина: мобила шлёт Navidrome song id (например 'xNBWsPqC0...'),
а код делал db.get(Track, s) напрямую. На Postgres с нативным UUID-типом
это даёт: psycopg2.errors.InvalidTextRepresentation: invalid input syntax
for type uuid. Фикс: сначала проверяем формат UUID, db.get только для
валидных UUID и в try/except, иначе ищем по external_id.
"""
from __future__ import annotations

import uuid as _uuid


def is_uuid(s: str | None) -> bool:
    if not s:
        return False
    try:
        _uuid.UUID(str(s))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def get_track(db, raw: str | None):
    """Вернуть Track по uuid или external_id, None если нет. Никогда не падает на PG."""
    from app.db.models import Track

    s = str(raw or "").strip()
    if not s:
        return None
    # 1) external_id — основной путь мобилы (Navidrome song id)
    try:
        t = db.query(Track).filter(Track.external_id == s).first()
        if t is not None:
            return t
    except Exception:
        pass
    # 2) uuid — только если похоже на uuid
    if is_uuid(s):
        try:
            return db.get(Track, s)
        except Exception:
            return None
    return None


def resolve_track_ids(db, ids: list[str] | None) -> list[str]:
    """Список raw id (uuid или external_id) -> список внутренних uuid. Без падений."""
    out: list[str] = []
    for raw in ids or []:
        t = get_track(db, raw)
        if t is not None:
            out.append(str(t.id))
    return out


def get_user(db, raw: str | None):
    """MediaUser по uuid или external_id."""
    from app.db.models import MediaUser

    s = str(raw or "").strip()
    if not s:
        return None
    try:
        u = db.query(MediaUser).filter(MediaUser.external_id == s).first()
        if u is not None:
            return u
    except Exception:
        pass
    if is_uuid(s):
        try:
            return db.get(MediaUser, s)
        except Exception:
            return None
    return None
