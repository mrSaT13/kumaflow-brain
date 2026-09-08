from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import Lyrics, Track
from app.services.queue import enqueue
from app.services import lyrics as lyrics_svc
from app.services import lyrics_ai
from app.db.models import TrackFeatures

router = APIRouter()


@router.post("/fetch")
async def fetch_lyrics(db: Session = Depends(get_db)):
    """Синхронный фоллбэк: если есть очередь — через нее, иначе напрямую."""
    from app.workers.tasks import lyrics_fetch

    # быстрый старт задачи как в /api/scan/lyrics
    from app.db.models import ScanRun, ScanLog
    from app.services.demo import ensure_demo_server

    server = ensure_demo_server(db)
    db.commit()
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
    db.add(ScanLog(id=str(uuid.uuid4()), run_id=run.id, level="info", message=f"Задача lyrics через /api/lyrics/fetch (job {job_id})"))
    db.commit()
    return {"queued": True, "job_id": job_id, "run_id": str(run.id)}


@router.get("/{track_id}")
async def get_lyrics(track_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(track_id)
    except ValueError:
        raise HTTPException(400, "invalid track_id")
    t = db.get(Track, track_id)
    if not t:
        raise HTTPException(404, "track not found")
    row = db.query(Lyrics).filter(Lyrics.track_id == track_id).order_by(Lyrics.fetched_at.desc()).first()
    if not row:
        # пробуем подтянуть on-demand с lrclib (без очереди) — быстрый фоллбэк для UI
        fetched = None
        try:
            fetched = lyrics_svc.fetch(artist=t.artist_name or "", title=t.title or "", album=t.album_name, duration_sec=t.duration_sec)
        except Exception:
            fetched = None
        if fetched and fetched.get("text"):
            # сохраним для будущих запросов
            try:
                db.add(Lyrics(track_id=track_id, provider="lrclib", text=fetched["text"], synced=fetched.get("synced"), language=fetched.get("language"), source_url=fetched.get("source_url")))
                db.commit()
                row = db.query(Lyrics).filter(Lyrics.track_id == track_id).first()
            except Exception:
                db.rollback()
                return {"lyrics": {"provider": "lrclib", "text": fetched["text"], "synced": fetched.get("synced"), "source_url": fetched.get("source_url")}, "track_id": track_id, "cached": False}
        if not row:
            return {"lyrics": None, "track_id": track_id}
    return {
        "lyrics": {
            "provider": row.provider,
            "text": row.text,
            "synced": row.synced,
            "language": row.language,
            "source_url": row.source_url,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        },
        "track_id": track_id,
        "cached": True,
    }


@router.post("/{track_id}/refresh")
async def refresh_lyrics(track_id: str, db: Session = Depends(get_db)):
    """Принудительно перетянуть текст с lrclib и прогнать AI анализ."""
    try:
        uuid.UUID(track_id)
    except ValueError:
        raise HTTPException(400, "invalid track_id")
    t = db.get(Track, track_id)
    if not t:
        raise HTTPException(404, "track not found")
    fetched = lyrics_svc.fetch(artist=t.artist_name or "", title=t.title or "", album=t.album_name, duration_sec=t.duration_sec)
    if not fetched or not fetched.get("text"):
        raise HTTPException(404, "lyrics not found on lrclib")
    # upsert
    row = db.query(Lyrics).filter(Lyrics.track_id == track_id, Lyrics.provider == "lrclib").first()
    if row:
        row.text = fetched["text"]
        row.synced = fetched.get("synced")
        row.source_url = fetched.get("source_url")
        from datetime import datetime
        row.fetched_at = datetime.utcnow()
    else:
        row = Lyrics(track_id=track_id, provider="lrclib", text=fetched["text"], synced=fetched.get("synced"), language=fetched.get("language"), source_url=fetched.get("source_url"))
        db.add(row)
    # AI анализ
    try:
        ai_res = lyrics_ai.analyze(fetched["text"])
        if ai_res:
            f = db.get(TrackFeatures, track_id)
            if not f:
                f = TrackFeatures(track_id=track_id)
                db.add(f)
            for k in ("valence", "arousal", "energy"):
                if k in ai_res:
                    try:
                        setattr(f, k, float(ai_res[k]))
                    except Exception:
                        pass
            moods = (f.mood_labels or []) + ai_res.get("moods", [])
            f.mood_labels = list(dict.fromkeys([str(m).lower() for m in moods if str(m).strip()]))[:8]
            mv = dict(f.mood_vector or {})
            mv["ai_sentiment"] = ai_res.get("sentiment", "neutral")
            mv["ai_language"] = ai_res.get("language", "")
            mv["ai_themes"] = ai_res.get("themes", [])
            f.mood_vector = mv
    except Exception:
        pass
    db.commit()
    return {"ok": True, "lyrics": {"provider": "lrclib", "text": fetched["text"][:5000]}}

