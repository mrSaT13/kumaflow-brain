from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import ScanLog, ScanRun, Track, TrackCluster, TrackFeatures
from app.services.media_server import resolve_active_server
from app.services.queue import enqueue
from app.workers.tasks import cluster_build

router = APIRouter()


@router.post("/build")
def build(db: Session = Depends(get_db)):
    """Пересобрать кластеры: создаёт задачу (видно в истории и логах)."""
    server = resolve_active_server(db)
    db.commit()
    run = ScanRun(
        id=str(uuid.uuid4()),
        server_id=server.id,
        phase="clusters",
        status="running",
        total_items=0,
        processed_items=0,
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    job_id = enqueue(cluster_build, str(run.id), job_timeout=3600)
    db.add(ScanLog(id=str(uuid.uuid4()), run_id=run.id, level="info",
                   message=f"Кластеризация поставлена в очередь (job {job_id})"))
    db.commit()
    return {"queued": True, "run_id": str(run.id), "job_id": job_id}


@router.get("/")
def list_clusters(db: Session = Depends(get_db)):
    """Кластеры активного сервера: размер, жанры, примеры треков."""
    from collections import Counter

    from sqlalchemy import func

    server = resolve_active_server(db)
    rows = (
        db.query(TrackCluster.cluster_id, func.count(TrackCluster.track_id))
        .join(Track, Track.id == TrackCluster.track_id)
        .filter(Track.server_id == server.id, TrackCluster.algorithm == "kmeans")
        .group_by(TrackCluster.cluster_id)
        .order_by(TrackCluster.cluster_id.asc())
        .all()
    )
    clusters = []
    for cid, cnt in rows:
        members = (
            db.query(Track)
            .join(TrackCluster, TrackCluster.track_id == Track.id)
            .filter(Track.server_id == server.id,
                    TrackCluster.algorithm == "kmeans",
                    TrackCluster.cluster_id == cid)
            .order_by(Track.play_count.desc())
            .limit(6)
            .all()
        )
        genres = Counter(t.genre for t in members if t.genre)
        feats = (
            db.query(TrackFeatures)
            .join(Track, TrackFeatures.track_id == Track.id)
            .join(TrackCluster, TrackCluster.track_id == Track.id)
            .filter(Track.server_id == server.id,
                    TrackCluster.algorithm == "kmeans",
                    TrackCluster.cluster_id == cid)
            .all()
        )
        avg_energy = (
            round(sum(f.energy for f in feats if f.energy is not None) / len(feats), 3)
            if feats else None
        )
        clusters.append({
            "id": cid,
            "algorithm": "kmeans",
            "size": cnt,
            "top_genres": [g for g, _ in genres.most_common(3)],
            "avg_energy": avg_energy,
            "sample": [{"id": str(t.id), "title": t.title, "artist_name": t.artist_name} for t in members[:6]],
        })
    return {"clusters": clusters, "server_id": str(server.id)}
