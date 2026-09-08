from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.db.models import Track, TrackFeatures, TrackCluster, Lyrics, TrackMetadataEnrich

router = APIRouter()


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
    }


@router.get("", include_in_schema=False)
@router.get("/")
async def list_tracks(
    q: str | None = None,
    genre: str | None = None,
    artist: str | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    from app.services.media_server import get_media_server_config

    cfg = get_media_server_config(db)
    real_url = cfg.get("url") if cfg.get("url") not in ("", "http://localhost", "https://localhost") else None
    query = db.query(Track)
    if real_url:
        from app.db.models import MediaServer

        srv = db.query(MediaServer).filter(MediaServer.url == real_url).first()
        if srv:
            query = query.filter(Track.server_id == srv.id)
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
async def get_track(track_id: str, db: Session = Depends(get_db)):
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
