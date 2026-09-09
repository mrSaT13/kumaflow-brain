from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db import get_db, models
from app.db.models import ScanRun, ScanLog
from app.services.demo import ensure_demo_server, seed_demo_library
from app.services.media_server import ensure_media_server_row, get_media_server_config
from app.services.queue import enqueue
from app.workers.tasks import (
    library_scan,
    sonic_analysis,
    lyrics_fetch,
    cluster_build,
    collab_build,
)

logger = get_logger("api.scan")
router = APIRouter()


def _run_to_dict(r: ScanRun) -> dict:
    return {
        "id": str(r.id),
        "phase": r.phase,
        "status": r.status,
        "total_items": r.total_items,
        "processed_items": r.processed_items,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "error": r.error,
    }


def _start_run(phase: str, fn, db: Session, *, total: int = 0, job_kwargs: dict | None = None, job_timeout: int = 1800) -> ScanRun:
    # защита от дублей: если уже есть running для этой фазы — не создаём новый
    existing = (
        db.query(ScanRun)
        .filter(ScanRun.phase == phase, ScanRun.status.in_(["queued", "running"]))
        .first()
    )
    if existing:
        raise HTTPException(409, f"Задача {phase} уже выполняется ({existing.id})")
    cfg = get_media_server_config(db)
    # если настроен реальный сервер (url не пустой и не localhost) — используем его
    if cfg.get("url") and cfg["url"] not in ("http://localhost", "https://localhost"):
        server = ensure_media_server_row(db, cfg)
    else:
        server = ensure_demo_server(db)
    db.commit()
    run = ScanRun(
        id=str(uuid.uuid4()),
        server_id=server.id,
        phase=phase,
        status="running",
        total_items=total,
        processed_items=0,
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    job_id = enqueue(fn, str(run.id), job_timeout=job_timeout, **(job_kwargs or {}))
    db.add(
        ScanLog(
            id=str(uuid.uuid4()),
            run_id=run.id,
            level="info",
            message=f"Задача '{phase}' поставлена в очередь (job {job_id})",
        )
    )
    db.commit()
    return run


@router.post("/library")
def start_library_scan(db: Session = Depends(get_db)):
    cfg = get_media_server_config(db)
    is_real = bool(cfg.get("url") and cfg["url"] not in ("http://localhost", "https://localhost"))
    # демо сидим только если реального сервера нет и база пуста
    if not is_real and db.query(models.Track).count() == 0:
        server = ensure_demo_server(db)
        db.commit()
        seed_demo_library(str(server.id))

    run = _start_run("library", library_scan, db, job_timeout=7200)
    return {"queued": True, "run_id": str(run.id)}


@router.post("/analysis")
def start_analysis(force: bool = False, limit: int = 0, db: Session = Depends(get_db)):
    """Реальный sonic-анализ. force=1 — пересчитать всё, limit=N — взять N треков."""
    total = db.query(models.Track).count()
    run = _start_run("analysis", sonic_analysis, db, total=total,
                     job_kwargs={"force": force, "limit": limit}, job_timeout=7200)
    return {"queued": True, "run_id": str(run.id)}


@router.post("/lyrics")
def start_lyrics(db: Session = Depends(get_db)):
    total = db.query(models.Track).count()
    run = _start_run("lyrics", lyrics_fetch, db, total=total, job_timeout=3600)
    return {"queued": True, "run_id": str(run.id)}


@router.post("/clusters")
def start_clusters(db: Session = Depends(get_db)):
    run = _start_run("clusters", cluster_build, db, job_timeout=3600)
    return {"queued": True, "run_id": str(run.id)}


@router.post("/collab")
def start_collab(db: Session = Depends(get_db)):
    run = _start_run("collab", collab_build, db, job_timeout=900)
    return {"queued": True, "run_id": str(run.id)}


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)):
    rows = db.query(ScanRun).order_by(ScanRun.started_at.desc()).limit(100).all()
    return {"runs": [_run_to_dict(r) for r in rows]}


@router.get("/runs/current")
def current_run(db: Session = Depends(get_db)):
    r = (
        db.query(ScanRun)
        .filter(ScanRun.status.in_(["queued", "running"]))
        .order_by(ScanRun.started_at.desc())
        .first()
    )
    return {"current": _run_to_dict(r) if r else None}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run_id")
    r = db.get(ScanRun, run_id)
    if not r:
        raise HTTPException(404, "not found")
    if r.status in ("success", "failure"):
        return {"ok": False, "error": "already finished"}
    r.status = "failure"
    r.finished_at = datetime.utcnow()
    r.error = "Отменено пользователем"
    db.add(
        ScanLog(
            id=str(uuid.uuid4()),
            run_id=r.id,
            level="warn",
            message="Задача отменена пользователем",
        )
    )
    db.commit()
    return {"ok": True}


@router.get("/runs/{run_id}/logs")
def run_logs(run_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run_id")
    rows = (
        db.query(ScanLog)
        .filter(ScanLog.run_id == run_id)
        .order_by(ScanLog.created_at.asc())
        .limit(1000)
        .all()
    )
    return {
        "logs": [
            {
                "id": str(l.id),
                "level": l.level,
                "message": l.message,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in rows
        ]
    }


@router.get("/runs/{run_id}/logs/stream")
async def run_logs_stream(run_id: str):
    """Server-Sent Events: пушим новые логи в реалтайме."""
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run_id")

    async def gen():
        from app.db.database import session_scope

        last_id: str | None = None
        # initial snapshot
        with session_scope() as db2:
            rows = (
                db2.query(ScanLog)
                .filter(ScanLog.run_id == run_id)
                .order_by(ScanLog.created_at.asc())
                .limit(500)
                .all()
            )
            snapshot = [(str(l.id), l.level, l.message, l.created_at) for l in rows]
        for lid, level, message, created_at in snapshot:
            yield f"data: {json.dumps({'id': lid, 'level': level, 'message': message, 'created_at': created_at.isoformat() if created_at else None}, ensure_ascii=False)}\n\n"
        last_id = snapshot[-1][0] if snapshot else None

        while True:
            await asyncio.sleep(0.6)
            with session_scope() as db2:
                q = db2.query(ScanLog).filter(ScanLog.run_id == run_id)
                if last_id is not None:
                    q = q.filter(ScanLog.id > last_id)
                new_rows = q.order_by(ScanLog.created_at.asc()).limit(100).all()
                batch = [(str(l.id), l.level, l.message, l.created_at) for l in new_rows]
                run_row = db2.get(ScanRun, run_id)
                run_status = run_row.status if run_row else None
            for lid, level, message, created_at in batch:
                yield f"data: {json.dumps({'id': lid, 'level': level, 'message': message, 'created_at': created_at.isoformat() if created_at else None}, ensure_ascii=False)}\n\n"
                last_id = lid
            if run_status in ("success", "failure"):
                yield f"event: done\ndata: {{\"status\":\"{run_status}\"}}\n\n"
                break

    return StreamingResponse(gen(), media_type="text/event-stream")
