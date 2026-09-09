from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.db import get_db
from app.services.covers import cached_content_type, get_cover, get_disk_cover
from app.services.demo import ensure_demo_server as _ensure_demo  # noqa: F401 (реэкспорт для совместимости)
from app.services.media_server import get_media_server_config, resolve_active_server
from app.services.queue import enqueue
from app.workers.tasks import noop as _placeholder

router = APIRouter()


@router.get("/{cover_id}")
async def cover(cover_id: str, db=Depends(get_db)):
    data = get_cover(cover_id, cfg=dict(get_media_server_config(db)))
    if not data:
        return _placeholder_png()
    return Response(content=data, media_type=cached_content_type(cover_id))


@router.get("/track/{track_id}")
async def track_cover(track_id: str, db=Depends(get_db)):
    """Обложка трека: встроенная в файл (диск) либо coverArt из Navidrome."""
    import uuid as _uuid

    try:
        _uuid.UUID(track_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    from app.db.models import Track

    t = db.get(Track, track_id)
    if not t:
        raise HTTPException(404, "track not found")
    # 1) встроенная обложка из файла
    data = get_disk_cover(t.path)
    if data:
        ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return Response(content=data, media_type=ctype)
    # 2) coverArt из Navidrome
    if t.cover_art_id:
        data = get_cover(t.cover_art_id, cfg=dict(get_media_server_config(db)))
        if data:
            return Response(content=data, media_type=cached_content_type(t.cover_art_id))
    return _placeholder_png()


def _placeholder_png() -> Response:
    # 1x1 transparent png placeholder
    placeholder = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000100"
        "0d0a2db40000000049454e44ae426082"
    )
    return Response(content=placeholder, media_type="image/png")


@router.post("/prefetch")
async def prefetch(payload: dict, db=Depends(get_db)):
    from app.services.covers import prefetch_artist_covers
    artist_ids = payload.get("artist_ids", [])
    if not artist_ids:
        # если не передали — возьмём всех из БД
        from app.db.models import Artist
        server = resolve_active_server(db)
        db.commit()
        artist_ids = [
            a.external_id for a in db.query(Artist).filter_by(server_id=server.id).all()
        ]
    n = await prefetch_artist_covers(artist_ids)
    return {"fetched": n}
