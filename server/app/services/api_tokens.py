"""API-токены плеер <-> мозг: создание в веб-UI, без compose.

Храним только sha256-хэш; plaintext показывается один раз при создании.
Скоупы: wave / sync / covers / playlists / admin.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid as _uuid

SCOPES: dict[str, str] = {
    "wave": "Волна и сейчас играет",
    "sync": "Синк вкусов и событий",
    "covers": "Обложки",
    "playlists": "Плейлисты и открытия",
    "admin": "Полный доступ",
}

# Готовые наборы для UI
PRESETS: dict[str, dict] = {
    "mobile": {
        "label": "Мобила",
        "desc": "Плеер: волна + синк + обложки + плейлисты",
        "scopes": ["wave", "sync", "covers", "playlists"],
    },
    "wave": {
        "label": "Только волна",
        "desc": "Волна и сейчас играет, без синка",
        "scopes": ["wave", "covers"],
    },
    "admin": {
        "label": "Админ",
        "desc": "Всё, включая этот браузер",
        "scopes": ["admin"],
    },
}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def normalize_scopes(scopes: list[str] | None) -> list[str]:
    out: list[str] = []
    for s in scopes or []:
        s = str(s or "").strip().lower()
        if s in SCOPES and s not in out:
            out.append(s)
    if "admin" in out:
        return ["admin"]
    return out or ["wave"]


def create_token(db, owner_user_id: str | None, name: str, scopes: list[str] | None) -> dict:
    """Создать токен. Возвращает dict с plaintext (показать один раз!)."""
    from app.db.models import ApiToken, MediaUser

    scopes = normalize_scopes(scopes)
    owner = None
    if owner_user_id:
        owner = db.get(MediaUser, str(owner_user_id))
        if owner is None:
            raise ValueError("user not found")
    raw = secrets.token_urlsafe(32)
    row = ApiToken(
        id=str(_uuid.uuid4()),
        owner_user_id=str(owner.id) if owner else None,
        name=(name or "mobile").strip()[:128] or "mobile",
        token_hash=_hash(raw),
        prefix=raw[:8],
        scopes=scopes,
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "prefix": row.prefix,
        "scopes": list(row.scopes or []),
        "owner_user_id": str(row.owner_user_id) if row.owner_user_id else None,
        "token": raw,  # только сейчас!
    }


def list_tokens(db, owner_user_id: str | None = None) -> list[dict]:
    from app.db.models import ApiToken

    q = db.query(ApiToken).order_by(ApiToken.created_at.desc())
    if owner_user_id:
        q = q.filter(ApiToken.owner_user_id == str(owner_user_id))
    return [to_dict(r) for r in q.all()]


def to_dict(r) -> dict:
    try:
        created = r.created_at.isoformat() if r.created_at else None
    except Exception:
        created = None
    try:
        used = r.last_used_at.isoformat() if r.last_used_at else None
    except Exception:
        used = None
    return {
        "id": str(r.id),
        "name": r.name,
        "prefix": r.prefix or "",
        "scopes": list(r.scopes or []),
        "owner_user_id": str(r.owner_user_id) if r.owner_user_id else None,
        "enabled": bool(r.enabled),
        "created_at": created,
        "last_used_at": used,
    }


def delete_token(db, token_id: str) -> bool:
    from app.db.models import ApiToken

    r = db.get(ApiToken, str(token_id))
    if r is None:
        return False
    db.delete(r)
    db.commit()
    return True


def set_enabled(db, token_id: str, enabled: bool) -> dict | None:
    from app.db.models import ApiToken

    r = db.get(ApiToken, str(token_id))
    if r is None:
        return None
    r.enabled = bool(enabled)
    db.commit()
    return to_dict(r)


def count_tokens(db) -> int:
    from app.db.models import ApiToken

    try:
        return db.query(ApiToken).count()
    except Exception:
        return 0


def verify(db, raw: str) -> dict | None:
    """Проверить plaintext. Возвращает {scopes, owner_user_id, is_admin} или None."""
    from app.core.time import utcnow as _utcnow
    from app.db.models import ApiToken

    raw = (raw or "").strip()
    if not raw:
        return None
    r = db.query(ApiToken).filter(ApiToken.token_hash == _hash(raw)).first()
    if r is None or not r.enabled:
        return None
    try:
        r.last_used_at = _utcnow()
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    scopes = list(r.scopes or [])
    return {
        "token_id": str(r.id),
        "scopes": scopes,
        "owner_user_id": str(r.owner_user_id) if r.owner_user_id else None,
        "is_admin": "admin" in scopes,
    }
