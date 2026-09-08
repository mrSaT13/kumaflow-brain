from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.db import get_db
from app.services.covers import cached_content_type, get_cover
from app.services.demo import ensure_demo_server
from app.services.queue import enqueue
from app.workers.tasks import noop as _placeholder

router = APIRouter()


@router.get("/{cover_id}")
async def cover(cover_id: str):
    data = get_cover(cover_id)
    if not data:
        # 1x1 transparent png placeholder
        placeholder = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c63000100000005000100"
            "0d0a2db40000000049454e44ae426082"
        )
        return Response(content=placeholder, media_type="image/png")
    return Response(content=data, media_type=cached_content_type(cover_id))


@router.post("/prefetch")
async def prefetch(payload: dict, db=Depends(get_db)):
    from app.services.covers import prefetch_artist_covers
    artist_ids = payload.get("artist_ids", [])
    if not artist_ids:
        # если не передали — возьмём всех из БД
        from app.db.models import Artist
        server = ensure_demo_server(db)
        db.commit()
        artist_ids = [
            a.external_id for a in db.query(Artist).filter_by(server_id=server.id).all()
        ]
    n = await prefetch_artist_covers(artist_ids)
    return {"fetched": n}
