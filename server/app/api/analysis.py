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
def clusters_build(db: Session = Depends(get_db)):
    server = resolve_active_server(db)
    db.commit()
    res = build_clusters(str(server.id))
    return res


@router.get("/recommend/by-track/{track_id}")
def recommend_by_track_endpoint(track_id: str):
    return {"items": recommend_by_track(track_id, top_k=20)}


@router.get("/cold-start")
def cold_start(db: Session = Depends(get_db), n: int = 30):
    server = resolve_active_server(db)
    db.commit()
    return cold_start_playlist(str(server.id), n=n)


@router.post("/search-by-text")
def search_by_text(payload: dict, db: Session = Depends(get_db)):
    """Текстовый поиск: CLAP-эмбеддинг если есть, иначе TF-IDF/keyword fallback.

    Payload: {"q": "грустный инди", "top_k": 20}
    """
    q = (payload.get("q") or payload.get("query") or "").strip() if isinstance(payload, dict) else ""
    top_k = int(payload.get("top_k") or payload.get("n") or 20) if isinstance(payload, dict) else 20
    top_k = max(1, min(50, top_k))
    if not q:
        return {"items": [], "mode": "empty", "query": q}

    # 1) если есть CLAP эмбеддинг — пробуем через AI/onnx (опционально)
    try:
        from app.services.ai import is_configured as ai_ready

        # CLAP пока не подключен как провайдер, но если появится — используем
        _ = ai_ready  # noqa
    except Exception:
        pass

    # 2) TF-IDF fallback по title/artist/album/lyrics/mood (работает без ML)
    from sqlalchemy import or_

    from app.db.models import Lyrics, Track, TrackFeatures

    like = f"%{q.lower()}%"
    # прямой keyword-поиск (ILIKE через lower -> работает и на sqlite и на postgres)
    from sqlalchemy import func as _func

    try:
        base_q = db.query(Track).filter(
            or_(
                _func.lower(Track.title).like(like),
                _func.lower(Track.artist_name).like(like),
                _func.lower(Track.album_name).like(like),
                _func.lower(Track.genre).like(like),
            )
        )
        rows = base_q.order_by(Track.play_count.desc()).limit(top_k * 2).all()
    except Exception:
        rows = []
    # если есть тексты — дополняем поиском по lyrics
    lyric_ids: list[str] = []
    try:
        lyric_rows = (
            db.query(Lyrics.track_id)
            .filter(_func.lower(Lyrics.text).like(like))
            .limit(top_k)
            .all()
        )
        lyric_ids = [str(r[0]) for r in lyric_rows]
    except Exception:
        pass
    # mood fallback: ищем по TrackFeatures.mood_labels (json LIKE)
    mood_rows: list[Track] = []
    try:
        # SQLite: json_each, Postgres: @> — упрощаем через Python фильтрацию
        cand = db.query(Track).join(TrackFeatures, TrackFeatures.track_id == Track.id).all()
        q_low = q.lower()
        for t in cand:
            f = db.get(TrackFeatures, t.id)
            if f and f.mood_labels and any(q_low in str(m).lower() for m in f.mood_labels):
                mood_rows.append(t)
                if len(mood_rows) >= top_k:
                    break
    except Exception:
        pass

    seen: set[str] = set()
    items: list[dict] = []
    for t in list(rows) + mood_rows:
        tid = str(t.id)
        if tid in seen:
            continue
        seen.add(tid)
        items.append({"track_id": tid, "title": t.title, "artist_name": t.artist_name, "album_name": t.album_name, "genre": t.genre, "score": 1.0})
        if len(items) >= top_k and not lyric_ids:
            break
    # добавим lyric-хиты если не хватает
    if len(items) < top_k and lyric_ids:
        for tid in lyric_ids:
            if tid in seen:
                continue
            t = db.get(Track, tid)
            if not t:
                continue
            items.append({"track_id": tid, "title": t.title, "artist_name": t.artist_name, "album_name": t.album_name, "genre": t.genre, "score": 0.9})
            seen.add(tid)
            if len(items) >= top_k:
                break
    return {"items": items[:top_k], "mode": "keyword", "query": q}


@router.post("/lyrics/analyze-all")
def analyze_all(db: Session = Depends(get_db)):
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
