from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AppSetting, MediaServer


def _norm_url(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip().rstrip("/")
    if not u.startswith("http"):
        u = "http://" + u
    return u


def get_media_server_config(db: Session) -> dict[str, str]:
    """Сначала пробуем AppSetting.media_server, иначе fallback на .env."""
    row = db.get(AppSetting, "media_server")
    if row and isinstance(row.value, dict) and row.value.get("url"):
        v = row.value
        return {
            "type": (v.get("type") or "navidrome").strip().lower(),
            "url": _norm_url(v.get("url")),
            "user": (v.get("user") or "").strip(),
            "password": v.get("password") or "",
            "token": (v.get("token") or "").strip(),
        }
    s = get_settings()
    return {
        "type": (s.media_server_type or "navidrome").strip().lower(),
        "url": _norm_url(s.navidrome_url),
        "user": (s.navidrome_user or "").strip(),
        "password": s.navidrome_password or "",
        "token": "",
    }


def save_media_server_config(db: Session, payload: dict[str, Any]) -> dict[str, str]:
    value = {
        "type": (payload.get("type") or "navidrome").strip().lower(),
        "url": _norm_url(payload.get("url")),
        "user": (payload.get("user") or "").strip(),
        "password": payload.get("password") or "",
        "token": (payload.get("token") or "").strip(),
    }
    row = db.get(AppSetting, "media_server")
    if row is None:
        row = AppSetting(key="media_server", value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()
    # также заводим/обновляем строку в media_servers
    ensure_media_server_row(db, value)
    db.commit()
    return value


def ensure_media_server_row(db: Session, cfg: dict[str, str] | None = None) -> MediaServer:
    if cfg is None:
        cfg = get_media_server_config(db)
    url = cfg.get("url") or ""
    # ищем по url
    existing = None
    if url:
        existing = db.query(MediaServer).filter(MediaServer.url == url).first()
    if existing is None:
        # пробуем по типу demo если url пустой
        existing = db.query(MediaServer).filter(MediaServer.type == cfg.get("type", "navidrome")).first()
    if existing:
        existing.type = cfg.get("type") or existing.type
        existing.url = url or existing.url
        existing.name = cfg.get("type", "navidrome").title()
        existing.enabled = True
        db.flush()
        return existing
    row = MediaServer(
        id=str(uuid.uuid4()),
        type=cfg.get("type") or "navidrome",
        name=(cfg.get("type") or "navidrome").title(),
        url=url or "http://localhost",
        enabled=True,
    )
    db.add(row)
    db.flush()
    return row
