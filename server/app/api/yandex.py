from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import Track
from app.services.yandex_music.client import get_yandex_config, save_yandex_config, fetch_metadata_sync
from app.services.queue import enqueue

router = APIRouter()


@router.get("/status")
def status(db: Session = Depends(get_db)):
    cfg = get_yandex_config(db)
    return {"enabled": cfg["enabled"] and bool(cfg["token"]), "has_token": bool(cfg["token"]), "throttle_sec": 1.2}


@router.get("/config")
def get_config(db: Session = Depends(get_db)):
    cfg = get_yandex_config(db)
    return {"token": "***" if cfg["token"] else "", "enabled": cfg["enabled"], "has_token": bool(cfg["token"])}


@router.post("/config")
def save_config(payload: dict, db: Session = Depends(get_db)):
    val = save_yandex_config(db, payload or {})
    return {"ok": True, "saved": {"enabled": val["enabled"], "has_token": bool(val["token"])}}


@router.post("/test")
def test(payload: dict | None = None, db: Session = Depends(get_db)):
    artist = (payload or {}).get("artist") or "Земфира"
    title = (payload or {}).get("title") or "Искала"
    res = fetch_metadata_sync(db, artist, title)
    if res is None:
        cfg = get_yandex_config(db)
        if not cfg["enabled"] or not cfg["token"]:
            return {"ok": False, "error": "yandex disabled or token empty (set YANDEX_MUSIC_TOKEN + enabled)"}
        return {"ok": False, "error": "not found or yandex throttled (429) — retry in 2s"}
    return {"ok": True, "result": res}


@router.post("/enrich")
def enrich(limit: int = 20, db: Session = Depends(get_db)):
    """Обогатить до limit треков без yandex-метаданных (фоновая задача)."""
    from app.db.models import TrackMetadataEnrich

    cfg = get_yandex_config(db)
    if not cfg["enabled"] or not cfg["token"]:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="yandex disabled or token empty")
    # выбрать треки без yandex enrich
    have = db.query(TrackMetadataEnrich.track_id).filter(TrackMetadataEnrich.source == "yandex").subquery()
    q = db.query(Track).filter(~Track.id.in_(db.query(have.c.track_id))).limit(limit).all()
    if not q:
        return {"queued": False, "reason": "all enriched"}
    from app.workers.tasks import yandex_enrich

    # создаём ScanRun
    import uuid
    from datetime import datetime

    from app.db.models import ScanLog, ScanRun
    from app.services.media_server import resolve_active_server

    server = resolve_active_server(db)
    db.commit()
    run = ScanRun(id=str(uuid.uuid4()), server_id=server.id, phase="yandex", status="running", total_items=len(q), processed_items=0, started_at=datetime.utcnow())
    db.add(run)
    db.flush()
    job_id = enqueue(yandex_enrich, str(run.id), job_timeout=3600)
    db.add(ScanLog(id=str(uuid.uuid4()), run_id=run.id, level="info", message=f"Yandex enrich queued {job_id} for {len(q)} tracks"))
    db.commit()
    return {"queued": True, "run_id": str(run.id), "job_id": job_id, "tracks": len(q)}


@router.get("/track/{track_id}")
def track_meta(track_id: str, db: Session = Depends(get_db)):
    from app.db.models import TrackMetadataEnrich

    rows = db.query(TrackMetadataEnrich).filter(TrackMetadataEnrich.track_id == track_id, TrackMetadataEnrich.source == "yandex").all()
    return {"items": [{"source": r.source, "data": r.data, "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None} for r in rows]}
