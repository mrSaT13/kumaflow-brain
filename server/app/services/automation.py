"""Флаги автоматизации (БД, без перезапуска и правок compose).

Хранятся в AppSetting.automation dict. Дефолты — в DEFAULTS.
Читают и backend, и worker (одна postgres).
"""
from __future__ import annotations

from typing import Any

KEY = "automation"

DEFAULTS: dict[str, Any] = {
    # тянуть текст + AI-настроение сразу после sonic-анализа трека
    "analysis_fetch_lyrics": True,
}


def get_flags(db=None) -> dict[str, Any]:
    flags = dict(DEFAULTS)
    try:
        if db is None:
            from app.db.database import session_scope

            with session_scope() as _db:
                return get_flags(_db)
        from app.db.models import AppSetting

        row = db.get(AppSetting, KEY)
        if row is not None and isinstance(row.value, dict):
            for k, v in row.value.items():
                if k in DEFAULTS:
                    flags[k] = v
    except Exception:
        pass
    return flags


def analysis_fetch_lyrics_enabled(db=None) -> bool:
    try:
        return bool(get_flags(db).get("analysis_fetch_lyrics", True))
    except Exception:
        return True


def set_flags(patch: dict[str, Any], db=None) -> dict[str, Any]:
    clean = {k: patch[k] for k in DEFAULTS if k in patch}
    if db is None:
        from app.db.database import session_scope

        with session_scope() as _db:
            return set_flags(clean, _db)
    from app.db.models import AppSetting

    row = db.get(AppSetting, KEY)
    merged = get_flags(db)
    merged.update(clean)
    if row is None:
        db.add(AppSetting(key=KEY, value=merged))
    else:
        row.value = merged
    db.commit()
    return merged
