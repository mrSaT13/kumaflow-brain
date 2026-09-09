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
    if real_url:
        srv = db.query(MediaServer).filter(MediaServer.url == real_url).first()
        if srv:
            return {
                "tracks": db.query(Track).filter(Track.server_id == srv.id).count(),
                "albums": db.query(Album).filter(Album.server_id == srv.id).count(),
                "artists": db.query(Artist).filter(Artist.server_id == srv.id).count(),
                "users": db.query(MediaUser).count(),
                "servers": db.query(MediaServer).count(),
                "active_server": srv.url,
            }
    return {
        "tracks": db.query(Track).count(),
        "albums": db.query(Album).count(),
        "artists": db.query(Artist).count(),
        "users": db.query(MediaUser).count(),
        "servers": db.query(MediaServer).count(),
        "active_server": None,
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
def list_genres(db: Session = Depends(get_db)):
    from app.services.media_server import get_media_server_config

    cfg = get_media_server_config(db)
    real_url = cfg.get("url") if cfg.get("url") not in ("", "http://localhost", "https://localhost") else None
    q = db.query(Track.genre).filter(Track.genre.isnot(None))
    if real_url:
        srv = db.query(MediaServer).filter(MediaServer.url == real_url).first()
        if srv:
            q = q.filter(Track.server_id == srv.id)
    rows = q.distinct().all()
    return {"genres": sorted([r[0] for r in rows if r[0]])}
