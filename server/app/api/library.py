from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.db.models import MediaServer, Track, Album, Artist, MediaUser

router = APIRouter()


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    from app.services.media_server import get_media_server_config

    cfg = get_media_server_config(db)
    real_url = cfg.get("url") if cfg.get("url") not in ("", "http://localhost", "https://localhost") else None
    active = None
    if real_url:
        srv = db.query(MediaServer).filter(MediaServer.url == real_url).first()
        if srv:
            active = srv.url
    # Итоги — по ВСЕЙ базе (Navidrome + диск + демо), иначе файлы с диска
    # на другой строке media_servers пропадали из счётчиков.
    total_tracks = db.query(Track).count()
    disk_tracks = db.query(Track).filter(Track.external_id.like("disk:%")).count()
    navidrome_tracks = db.query(Track).filter(
        ~Track.external_id.like("disk:%"), ~Track.external_id.like("demo-%")
    ).count()
    return {
        "tracks": total_tracks,
        "albums": db.query(Album).count(),
        "artists": db.query(Artist).count(),
        "users": db.query(MediaUser).count(),
        "servers": db.query(MediaServer).count(),
        "active_server": active,
        "disk_tracks": disk_tracks,
        "navidrome_tracks": navidrome_tracks,
    }


@router.get("/servers")
def list_servers(db: Session = Depends(get_db)):
    rows = db.query(MediaServer).all()
    return {
        "servers": [
            {
                "id": str(s.id),
                "type": s.type,
                "name": s.name,
                "url": s.url,
                "enabled": s.enabled,
            }
            for s in rows
        ]
    }


@router.get("/genres")
def list_genres(source: str | None = None, db: Session = Depends(get_db)):
    # Жанры по всей базе (опционально source=disk/navidrome/demo).
    q = db.query(Track.genre).filter(Track.genre.isnot(None))
    if source == "disk":
        q = q.filter(Track.external_id.like("disk:%"))
    elif source == "navidrome":
        q = q.filter(~Track.external_id.like("disk:%"), ~Track.external_id.like("demo-%"))
    elif source == "demo":
        q = q.filter(Track.external_id.like("demo-%"))
    rows = q.distinct().all()
    return {"genres": sorted([r[0] for r in rows if r[0]])}
