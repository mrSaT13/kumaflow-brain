from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.db.models import ScanLog, ScanRun, Track, TrackFeatures, TrackCluster, Lyrics, TrackMetadataEnrich
from app.services.queue import enqueue
from app.workers.tasks import analyze_single

router = APIRouter()


def _track_source(t: Track) -> str:
    ext = (t.external_id or "")
    if ext.startswith("disk:") or ext.startswith("dartist:") or ext.startswith("dalbum:"):
        return "disk"
    if ext.startswith("demo-"):
        return "demo"
    return "navidrome"


def _track_to_dict(t: Track) -> dict:
    return {
        "id": str(t.id),
        "title": t.title,
        "artist_name": t.artist_name,
        "album_name": t.album_name,
        "genre": t.genre,
        "duration_sec": t.duration_sec,
        "year": t.year,
        "cover_art_id": t.cover_art_id,
        "play_count": t.play_count,
        "rating": t.rating,
        "starred": t.starred,
        "last_played_at": t.last_played_at.isoformat() if t.last_played_at else None,
        "server_id": str(t.server_id) if t.server_id else None,
        "source": _track_source(t),
    }


@router.get("", include_in_schema=False)
@router.get("/")
def list_tracks(
    q: str | None = None,
    genre: str | None = None,
    artist: str | None = None,
    source: str | None = None,
    server_id: str | None = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    # Показываем ВСЕ треки (Navidrome + файлы с диска + демо).
    # Раньше был фильтр по active server — файлы с диска, привязанные
    # к другой строке media_servers, пропадали из выдачи.
    query = db.query(Track)
    if server_id:
        query = query.filter(Track.server_id == server_id)
    if source == "disk":
        query = query.filter(Track.external_id.like("disk:%"))
    elif source == "navidrome":
        query = query.filter(~Track.external_id.like("disk:%"), ~Track.external_id.like("demo-%"))
    elif source == "demo":
        query = query.filter(Track.external_id.like("demo-%"))
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            (Track.title.ilike(like)) | (Track.artist_name.ilike(like)) | (Track.album_name.ilike(like))
        )
    if genre:
        query = query.filter(Track.genre == genre)
    if artist:
        query = query.filter(Track.artist_name == artist)
    total = query.count()
    rows = query.order_by(Track.title.asc()).offset(offset).limit(limit).all()
    return {"items": [_track_to_dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{track_id}")
def get_track(track_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(track_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    t = db.get(Track, track_id)
    if not t:
        raise HTTPException(404, "track not found")

    f = db.get(TrackFeatures, track_id)
    cluster = (
        db.query(TrackCluster).filter(TrackCluster.track_id == track_id).first()
    )
    lyric = (
        db.query(Lyrics)
        .filter(Lyrics.track_id == track_id)
        .order_by(Lyrics.fetched_at.desc())
        .first()
    )
    enrich = (
        db.query(TrackMetadataEnrich)
        .filter(TrackMetadataEnrich.track_id == track_id)
        .all()
    )

    return {
        **_track_to_dict(t),
        "features": (
            {
                "tempo_bpm": f.tempo_bpm,
                "key": f.key_name,
                "scale": f.scale,
                "energy": f.energy,
                "danceability": f.danceability,
                "valence": f.valence,
                "arousal": f.arousal,
                "loudness_db": f.loudness_db,
                "spectral_centroid": f.spectral_centroid,
                "spectral_rolloff": f.spectral_rolloff,
                "zero_crossing_rate": f.zero_crossing_rate,
            }
            if f
            else {}
        ),
        "mood_vector": f.mood_vector if f else None,
        "moods": f.mood_labels if f and f.mood_labels else [],
        "cluster": (
            {"id": cluster.cluster_id, "algorithm": cluster.algorithm}
            if cluster
            else None
        ),
        "lyrics": (
            {
                "provider": lyric.provider,
                "text": lyric.text,
                "language": lyric.language,
                "source_url": lyric.source_url,
            }
            if lyric
            else None
        ),
        "metadata": {row.source: row.data for row in enrich},
    }


@router.post("/{track_id}/analyze")
def analyze_track_now(track_id: str, db: Session = Depends(get_db)):
    """Sonic-анализ одного трека прямо сейчас (кнопка на странице трека)."""
    try:
        uuid.UUID(track_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    t = db.get(Track, track_id)
    if not t:
        raise HTTPException(404, "track not found")
    run = ScanRun(
        id=str(uuid.uuid4()),
        server_id=t.server_id,
        phase="analysis",
        status="running",
        total_items=1,
        processed_items=0,
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    job_id = enqueue(analyze_single, str(run.id), str(track_id), job_timeout=1200)
    db.add(ScanLog(id=str(uuid.uuid4()), run_id=run.id, level="info",
                   message=f"Sonic-анализ трека «{t.artist_name} — {t.title}» (job {job_id})"))
    db.commit()
    return {"queued": True, "run_id": str(run.id), "job_id": job_id}


@router.post("/{track_id}/lyrics")
def fetch_track_lyrics_now(track_id: str, db: Session = Depends(get_db)):
    """Загрузка текста одного трека (LRCLIB + AI-настроение)."""
    try:
        uuid.UUID(track_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    t = db.get(Track, track_id)
    if not t:
        raise HTTPException(404, "track not found")
    from app.workers.tasks import lyrics_fetch_one

    res = lyrics_fetch_one(str(track_id))
    if res.get("status") != "success":
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, **res}
