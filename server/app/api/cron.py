from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import CronJob
from app.core.auth import require_admin, require_brain_auth
from app.services.cron_jobs import HEARTBEAT_KEY, HEARTBEAT_STALE_SEC, is_valid_cron
from app.services.queue import enqueue

router = APIRouter(dependencies=[Depends(require_brain_auth)])

import uuid
from datetime import datetime


@router.get("/health", dependencies=[Depends(require_admin)])
def cron_health(db: Session = Depends(get_db)):
    """Жив ли планировщик и что с последними запусками.

    Отдельный лёгкий эндпоинт: веб зовёт его, когда /api/cron/ не ответил, и
    показывает человеку «scheduler молчит / задача падает», а не пустую таблицу.
    """
    from app.core.time import utcnow
    from app.db.models import AppSetting
    from app.services.cron_jobs import DEFAULTS as _DEFAULTS, is_valid_cron as _valid

    row = db.get(AppSetting, HEARTBEAT_KEY)
    val = row.value if row and isinstance(row.value, dict) else {}
    last_tick = val.get("last_tick_at")
    age = None
    if last_tick:
        try:
            last = datetime.fromisoformat(str(last_tick))
            age = (utcnow() - last).total_seconds()
        except Exception:
            last_tick = None
    jobs = db.query(CronJob).all()
    broken = [
        {"id": str(j.id), "kind": j.kind, "cron_expr": j.cron_expr}
        for j in jobs
        if j.enabled and not _valid(j.cron_expr)
    ]
    known_kinds = {k for _, k, _ in _DEFAULTS}
    return {
        "scheduler_alive": bool(age is not None and age <= HEARTBEAT_STALE_SEC),
        "last_tick_at": last_tick,
        "last_tick_age_sec": int(age) if age is not None else None,
        "tick_error": val.get("last_tick_error"),
        "jobs_total": len(jobs),
        "jobs_enabled": sum(1 for j in jobs if j.enabled),
        "jobs_never_run": [j.kind for j in jobs if j.enabled and j.last_run_at is None],
        "jobs_failed": [
            {"kind": j.kind, "error": (j.last_error or "")[:200]}
            for j in jobs
            if j.last_status == "error"
        ],
        "broken_cron_exprs": broken,
        "missing_kinds": sorted(known_kinds - {j.kind for j in jobs}),
        "hint": (
            "scheduler не отвечает — проверь, что сервис scheduler запущен "
            "(docker compose ps scheduler, docker compose logs scheduler). "
            "Если бейджей не хватает, авто-пуш в Navidrome не сработает."
        ),
    }


# Задачи сидятся при старте backend и scheduler (services/cron_jobs.py).
# В GET больше НЕ пишем в БД: чтение статуса не должно блокироваться на коммите
# (а GET /api/cron/ веб опрашивает каждые 5 с). Если таблица пуста — это теперь
# видно по флагу seeded=false, а не молчаливый ноль задач.
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
                "last_status": j.last_status,
                "last_error": j.last_error,
                "last_started_at": j.last_started_at.isoformat() if j.last_started_at else None,
                "last_finished_at": j.last_finished_at.isoformat() if j.last_finished_at else None,
                "next_run_at": j.next_run_at.isoformat() if j.next_run_at else None,
                "run_count": j.run_count or 0,
                "fail_count": j.fail_count or 0,
            }
            for j in rows
        ]
    }


@router.post("/", dependencies=[Depends(require_admin)])
def create_job(payload: dict, db: Session = Depends(get_db)):
    expr = payload.get("cron_expr") or "0 3 * * *"
    if not is_valid_cron(expr):
        from fastapi import HTTPException as _HE

        raise _HE(400, f"некорректное cron-выражение: {expr!r} (нужно 5 полей, например 0 3 * * *)")
    j = CronJob(
        id=str(uuid.uuid4()),
        name=payload.get("name") or payload.get("kind") or "job",
        kind=payload.get("kind") or "custom",
        cron_expr=expr,
        enabled=bool(payload.get("enabled", True)),
        payload=payload.get("payload"),
    )
    db.add(j)
    db.commit()
    return {"ok": True, "id": str(j.id)}


@router.put("/{job_id}", dependencies=[Depends(require_admin)])
def update_job(job_id: str, payload: dict, db: Session = Depends(get_db)):
    j = db.get(CronJob, job_id)
    if not j:
        from fastapi import HTTPException
        raise HTTPException(404, "not found")
    if "cron_expr" in payload and not is_valid_cron(payload.get("cron_expr")):
        # Без проверки битое выражение проходило в БД, а scheduler считал его
        # «пора запускать» на каждом тике — задача молотила раз в минуту.
        from fastapi import HTTPException as _HE

        raise _HE(400, f"некорректное cron-выражение: {payload.get('cron_expr')!r}")
    for k in ("name", "kind", "cron_expr", "enabled", "payload"):
        if k in payload:
            setattr(j, k, payload[k])
    db.commit()
    return {"ok": True}


@router.delete("/{job_id}", dependencies=[Depends(require_admin)])
def delete_job(job_id: str, db: Session = Depends(get_db)):
    j = db.get(CronJob, job_id)
    if j:
        db.delete(j)
        db.commit()
    return {"ok": True}


@router.post("/{job_id}/run", dependencies=[Depends(require_admin)])
def run_now(job_id: str, db: Session = Depends(get_db)):
    j = db.get(CronJob, job_id)
    if not j:
        return {"queued": False, "error": "not found"}
    # диспетчер по kind
    kind = (j.kind or "").lower()
    if kind == "daily":
        from app.services.queue import enqueue_light
        from app.workers.tasks import daily_per_user

        # Раньше уходило в очередь default, а крон кладёт в light. Из-за разнобоя
        # «запустить сейчас» и ночной прогон попадали в разные пулы воркеров.
        job = enqueue_light(daily_per_user, job_timeout=3600)
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
    if kind == "snapshots":
        from app.workers.tasks import taste_snapshots

        job = enqueue(taste_snapshots, job_timeout=1800)
        j.last_run_at = datetime.utcnow()
        db.commit()
        return {"queued": True, "job_id": job}
    if kind == "weekly":
        from app.workers.tasks import weekly_discovery_all

        job = enqueue(weekly_discovery_all, job_timeout=3600)
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
    return {"queued": False, "error": f"неизвестный тип задачи: {kind!r}"}
