from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import CronJob
from app.services.queue import enqueue

router = APIRouter()


@router.get("/")
def list_jobs(db: Session = Depends(get_db)):
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


@router.post("/{job_id}/run")
def run_now(job_id: str, db: Session = Depends(get_db)):
    j = db.get(CronJob, job_id)
    if not j:
        return {"queued": False, "error": "not found"}
    job = enqueue(lambda: None)
    return {"queued": True, "job_id": job}
