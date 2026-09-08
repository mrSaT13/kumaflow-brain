from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import bridge as bridge_svc

router = APIRouter()


@router.get("/status")
async def bridge_status(db: Session = Depends(get_db)):
    """Что настроено + жив ли мост прямо сейчас."""
    cfg = bridge_svc.get_bridge_config(db)
    if not cfg["enabled"] or not cfg["url"]:
        return {"enabled": False, "url": "", "reachable": False}
    try:
        health = await bridge_svc.bridge_health(cfg["url"])
        return {"enabled": True, "url": cfg["url"], "reachable": True, "bridge": health}
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "url": cfg["url"], "reachable": False, "error": str(e)}


@router.get("/artist")
async def bridge_artist(name: str, db: Session = Depends(get_db)):
    """Прокси к мосту: нормализованные метаданные артиста. 404 = нет данных/мост выключен."""
    data = await bridge_svc.fetch_artist(db, name)
    if data is None:
        return {"ok": False, "error": "bridge disabled, unreachable or artist not found"}
    return {"ok": True, "artist": data}
