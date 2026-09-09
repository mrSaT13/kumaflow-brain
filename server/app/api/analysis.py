from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.demo import ensure_demo_server as _ensure_demo  # noqa: F401 (реэкспорт для совместимости)
from app.services.media_server import resolve_active_server
from app.services.ml import (
    build_clusters,
    cold_start_playlist,
    recommend_by_track,
    search_by_embedding,
)
from app.services.queue import enqueue

router = APIRouter()


@router.post("/clusters/build")
async def clusters_build(db: Session = Depends(get_db)):
    server = resolve_active_server(db)
    db.commit()
    res = build_clusters(str(server.id))
    return res


@router.get("/recommend/by-track/{track_id}")
async def recommend_by_track_endpoint(track_id: str):
    return {"items": recommend_by_track(track_id, top_k=20)}


@router.get("/cold-start")
async def cold_start(db: Session = Depends(get_db), n: int = 30):
    server = resolve_active_server(db)
    db.commit()
    return cold_start_playlist(str(server.id), n=n)


@router.post("/search-by-text")
async def search_by_text(payload: dict):
    """Заглушка для будущего CLAP-эмбеддинга от текста.
    Сейчас работает через keyword → embedding (если есть)."""
    return {"items": []}


@router.post("/lyrics/analyze-all")
async def analyze_all(db: Session = Depends(get_db)):
    server = resolve_active_server(db)
    db.commit()
    from app.db.models import ScanRun, ScanLog, Track
    import uuid
    from app.workers.tasks import lyrics_fetch

    total = db.query(Track).filter_by(server_id=server.id).count()
    run = ScanRun(
        id=str(uuid.uuid4()),
        server_id=server.id,
        phase="lyrics",
        status="running",
        total_items=total,
        processed_items=0,
    )
    db.add(run)
    db.flush()
    job_id = enqueue(lyrics_fetch, str(run.id))
    db.add(
        ScanLog(
            id=str(uuid.uuid4()),
            run_id=run.id,
            level="info",
            message=f"Запущен AI-анализ текстов (job {job_id})",
        )
    )
    db.commit()
    return {"queued": True, "run_id": str(run.id)}
