from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.core.auth import require_admin, require_brain_auth
from app.services import bridge as bridge_svc

router = APIRouter(dependencies=[Depends(require_brain_auth)])


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


@router.get("/artist-meta")
def bridge_artist_meta(name: str, db: Session = Depends(get_db)):
    """Метаданные артиста из ЛОКАЛЬНОЙ базы (из моста, с датой синха).

    Без внешних вызовов: дёргать MusicBrainz на каждый запрос страницы
    нельзя (лимит 1 req/s). Обогащение — через POST /enrich или крон.
    """
    row = bridge_svc.get_cached_meta(db, name)
    if row is None:
        return {"ok": False, "meta": None}
    return {"ok": True, "meta": bridge_svc.meta_to_dict(row)}


class EnrichIn(BaseModel):
    name: str | None = None
    limit: int = 25
    kind: str = "all"  # all | artist | track | album


@router.post("/enrich", dependencies=[Depends(require_admin)])
def bridge_enrich(payload: EnrichIn | None = None, db: Session = Depends(get_db)):
    """Синхронизировать метаданные с мостом (аддитивно, best-effort).

    kind=artist — артисты (MusicBrainz + Last.fm), track — теги/настроения
    треков, album — релизы/обложки, all (по умолчанию) — всё по очереди.
    Старый мост без /track,/album просто пропускается без ошибки.
    """
    payload = payload or EnrichIn()
    kind = (payload.kind or "all").strip().lower()
    limit = max(1, min(int(payload.limit or 25), 200))
    if payload.name:
        res = bridge_svc.enrich_artist(db, payload.name, force=True)
        return {"ok": bool(res), "meta": res,
                "total_meta": bridge_svc._meta_count(db)}
    out: dict[str, object] = {"ok": True}
    if kind in ("all", "artist"):
        names = bridge_svc.library_artist_names(db, limit=max(1, min(limit, 200)))
        out.update(bridge_svc.enrich_batch(db, names, limit=limit))
    if kind in ("all", "track"):
        out["tracks"] = bridge_svc.enrich_tracks_batch(db, limit=limit)
    if kind in ("all", "album"):
        out["albums"] = bridge_svc.enrich_albums_batch(db, limit=max(5, limit // 2))
    return out


@router.get("/enrich/stats")
def bridge_enrich_stats(db: Session = Depends(get_db)):
    """Сколько артистов/треков/альбомов уже обогащено и настройки моста."""
    from sqlalchemy import func

    from app.db.models import TrackMetadataEnrich

    counts: dict[str, int] = {}
    try:
        for src, n in db.query(TrackMetadataEnrich.source,
                               func.count(TrackMetadataEnrich.track_id)).group_by(
                               TrackMetadataEnrich.source).all():
            counts[str(src)] = int(n)
    except Exception:
        pass
    return {
        "enabled": bridge_svc.get_bridge_config(db)["enabled"],
        "total_meta": bridge_svc._meta_count(db),
        "tracks_meta": counts.get(bridge_svc.TRACK_SRC, 0),
        "albums_meta": counts.get(bridge_svc.ALBUM_SRC, 0),
        "ttl_days": bridge_svc.ENRICH_TTL_DAYS,
    }
