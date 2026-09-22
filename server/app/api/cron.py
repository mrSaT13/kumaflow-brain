from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import CronJob
from app.services.queue import enqueue

router = APIRouter()

import uuid
from datetime import datetime


def _ensure_defaults(db: Session):
    defaults = [
        ("Daily per-user", "daily", "0 3 * * *"),
        ("Taste refresh", "refresh_tastes", "30 4 * * *"),
        ("CLAP embed", "clap", "0 4 * * *"),
        ("Smart playlists", "smart", "0 6 * * *"),
        ("Cover GC 7d", "covers_gc", "0 5 * * 0"),
    ]
    # upsert по kind — подтягивает новые задачи и на старых базах
    have = {r[0] for r in db.query(CronJob.kind).all()}
    for name, kind, expr in defaults:
        if kind not in have:
            db.add(CronJob(id=str(uuid.uuid4()), name=name, kind=kind, cron_expr=expr, enabled=True))
    db.commit()


@router.get("/")
def list_jobs(db: Session = Depends(get_db)):
    _ensure_defaults(db)
    rows = db.query(CronJob).all()
    return {
        "jobs": [
            {
                "id": str(j.id),
                "name": j.name,
                "kind": j.kind,
                "cron_expr": j.cron_expr,
                "enabled": j.enabled,
                "last_run_at": j.last_run_at.isoformat() if j.last_run_at else None,
            }
            for j in rows
        ]
    }


@router.post("/")
def create_job(payload: dict, db: Session = Depends(get_db)):
    j = CronJob(
        id=str(uuid.uuid4()),
        name=payload.get("name") or payload.get("kind") or "job",
        kind=payload.get("kind") or "custom",
        cron_expr=payload.get("cron_expr") or "0 3 * * *",
        enabled=bool(payload.get("enabled", True)),
        payload=payload.get("payload"),
    )
    db.add(j)
    db.commit()
    return {"ok": True, "id": str(j.id)}


@router.put("/{job_id}")
def update_job(job_id: str, payload: dict, db: Session = Depends(get_db)):
    j = db.get(CronJob, job_id)
    if not j:
        from fastapi import HTTPException
        raise HTTPException(404, "not found")
    for k in ("name", "kind", "cron_expr", "enabled", "payload"):
        if k in payload:
            setattr(j, k, payload[k])
    db.commit()
    return {"ok": True}


@router.delete("/{job_id}")
def delete_job(job_id: str, db: Session = Depends(get_db)):
    j = db.get(CronJob, job_id)
    if j:
        db.delete(j)
        db.commit()
    return {"ok": True}


@router.post("/{job_id}/run")
def run_now(job_id: str, db: Session = Depends(get_db)):
    j = db.get(CronJob, job_id)
    if not j:
        return {"queued": False, "error": "not found"}
    # диспетчер по kind
    kind = (j.kind or "").lower()
    if kind == "daily":
        from app.workers.tasks import daily_per_user

        job = enqueue(daily_per_user, job_timeout=3600)
        j.last_run_at = datetime.utcnow()
        db.commit()
        return {"queued": True, "job_id": job}
    if kind == "refresh_tastes":
        from app.db.models import ScanRun
        from app.services.media_server import resolve_active_server
        from app.workers.tasks import refresh_tastes

        server = resolve_active_server(db)
        db.commit()
        run = ScanRun(
            id=str(uuid.uuid4()), server_id=server.id, phase="taste_refresh",
            status="running", total_items=0, processed_items=0,
            started_at=datetime.utcnow(),
        )
        db.add(run)
        db.flush()
        job = enqueue(refresh_tastes, str(run.id), job_timeout=3600)
        j.last_run_at = datetime.utcnow()
        db.commit()
        return {"queued": True, "job_id": job, "run_id": str(run.id)}
    if kind == "smart":
        from app.workers.tasks import smart_playlists

        job = enqueue(smart_playlists, job_timeout=3600)
        j.last_run_at = datetime.utcnow()
        db.commit()
        return {"queued": True, "job_id": job}
    if kind == "clap":
        from app.workers.tasks import clap_embed

        job = enqueue(clap_embed, str(j.id), job_timeout=3600)
        j.last_run_at = datetime.utcnow()
        db.commit()
        return {"queued": True, "job_id": job}
    if kind == "covers_gc":
        from app.services.covers import clear_expired

        clear_expired()
        j.last_run_at = datetime.utcnow()
        db.commit()
        return {"queued": True, "cleared": True}
    job = enqueue(lambda: None)
    return {"queued": True, "job_id": job}
