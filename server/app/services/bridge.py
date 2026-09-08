from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import AppSetting

logger = get_logger("bridge")


def _norm_url(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip().rstrip("/")
    if not u.startswith("http"):
        u = "http://" + u
    return u


def get_bridge_config(db: Session) -> dict[str, Any]:
    """Сначала AppSetting.bridge (сохранено из веб-UI), иначе fallback на env."""
    row = db.get(AppSetting, "bridge")
    if row and isinstance(row.value, dict):
        v = row.value
        return {
            "url": _norm_url(v.get("url")),
            "enabled": bool(v.get("enabled", False)),
        }
    s = get_settings()
    return {"url": _norm_url(s.bridge_url), "enabled": bool(s.bridge_enabled)}


def save_bridge_config(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    value = {
        "url": _norm_url(payload.get("url")),
        "enabled": bool(payload.get("enabled", False)),
    }
    row = db.get(AppSetting, "bridge")
    if row is None:
        row = AppSetting(key="bridge", value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return value


async def bridge_health(url: str) -> dict[str, Any]:
    """Пинг моста. Бросает исключение при недоступности — ловит вызывающий код."""
    target = f"{url.rstrip('/')}/api/bridge/health"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(target)
        r.raise_for_status()
        return r.json()


async def fetch_artist(db: Session, name: str) -> dict[str, Any] | None:
    """Метаданные артиста через мост. None = мост выключен/недоступен (не ошибка)."""
    cfg = get_bridge_config(db)
    if not cfg["enabled"] or not cfg["url"]:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{cfg['url']}/api/bridge/artist", params={"name": name})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001 — мост опционален, backend не должен падать
        logger.warning("bridge unavailable ({}), skipping enrichment", e)
        return None
