from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.db import get_db
from app.core.auth import require_scope
from app.services.covers import cached_content_type, get_cover, get_disk_cover
from app.services.demo import ensure_demo_server as _ensure_demo  # noqa: F401 (реэкспорт для совместимости)
from app.services.media_server import get_media_server_config, resolve_active_server
from app.services.queue import enqueue
from app.workers.tasks import noop as _placeholder

router = APIRouter(dependencies=[Depends(require_scope("covers"))])

import hashlib


def _etag_for(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


@router.get("/{cover_id}")
def cover(cover_id: str, request: Request, db=Depends(get_db), size: int = 300):
    # size из query ?size=300
    data = get_cover(cover_id, size=size, cfg=dict(get_media_server_config(db)))
    if not data:
        return _placeholder_png()
    etag = _etag_for(data)
    if request.headers.get("if-none-match", "").strip('\" ') == etag:
        return Response(status_code=304)
    headers = {
        "Cache-Control": "public, max-age=604800, immutable",
        "ETag": f'"{etag}"',
        "X-Cache": "HIT",
    }
    return Response(content=data, media_type=cached_content_type(cover_id, size), headers=headers)


@router.get("/track/{track_id}")
def track_cover(track_id: str, request: Request, db=Depends(get_db), size: int = 300):
    """Обложка трека: встроенная в файл (диск) → своя → альбома → плейсхолдер.

    Принимает uuid ИЛИ external_id (Navidrome song id) — мобила шлёт external_id.
    """
    from app.db.models import Track
    from app.services.covers import resolve_track_cover_id
    from app.services.track_resolve import get_track as _gt

    t = _gt(db, track_id)
    if not t:
        raise HTTPException(404, "track not found")
    # 1) встроенная обложка из файла
    data = get_disk_cover(t.path)
    if data:
        ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        etag = _etag_for(data)
        if request.headers.get("if-none-match", "").strip('" ') == etag:
            return Response(status_code=304)
        headers = {"Cache-Control": "public, max-age=604800, immutable", "ETag": f'"{etag}"'}
        return Response(content=data, media_type=ctype, headers=headers)
    # 2) своя coverArt либо альбома (song-level Navidrome обычно не отдаёт)
    cid = resolve_track_cover_id(db, t)
    if cid:
        data = get_cover(cid, size=size, cfg=dict(get_media_server_config(db)))
        if data:
            etag = _etag_for(data)
            if request.headers.get("if-none-match", "").strip('" ') == etag:
                return Response(status_code=304)
            headers = {"Cache-Control": "public, max-age=604800, immutable", "ETag": f'"{etag}"'}
            return Response(content=data, media_type=cached_content_type(cid, size), headers=headers)
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
