#!/usr/bin/env python
"""Отдельный планировщик крона (вместо BackgroundScheduler внутри воркера).

Почему отдельно:
- раньше scheduler жил внутри rq_worker: при --scale >1 задачи дублировались,
  тик раз в 10 мин неточный, librosa блокировала и крон, и очередь.
- теперь: один контейнер `scheduler` тикает каждую минуту, кладёт задачи
  в очереди light/audio/clap через Redis + acquire_lock (без дублей).

Запуск: python -m app.workers.scheduler
"""
from __future__ import annotations

import time

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("scheduler")


def _tick_once() -> None:
    from datetime import datetime, timezone

    from apscheduler.triggers.cron import CronTrigger

    from app.db.database import session_scope
    from app.db.models import CronJob, ScanRun
    from app.services.queue import enqueue, enqueue_clap, enqueue_light

    import uuid as _uuid

    from app.services.media_server import resolve_active_server as _ras

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        jobs = db.query(CronJob).filter(CronJob.enabled.is_(True)).all()
        for j in jobs:
            try:
                trig = CronTrigger.from_crontab(j.cron_expr)
                if j.last_run_at:
                    nxt = trig.get_next_fire_time(None, j.last_run_at)
                    if nxt and nxt.replace(tzinfo=None) > now:
                        continue
            except Exception:
                pass
            kind = j.kind
            try:
                if kind == "daily":
                    from app.workers import tasks as _t

                    enqueue_light(_t.daily_per_user, job_timeout=3600)
                elif kind == "smart":
                    from app.workers import tasks as _t

                    enqueue_light(_t.smart_playlists, job_timeout=3600)
                elif kind == "snapshots":
                    from app.workers import tasks as _t

                    enqueue_light(_t.taste_snapshots, job_timeout=1800)
                elif kind == "weekly":
                    from app.workers import tasks as _t

                    enqueue_light(_t.weekly_discovery_all, job_timeout=3600)
                elif kind == "refresh_tastes":
                    try:
                        _srv = _ras(db)
                        db.commit()
                        _run = ScanRun(
                            id=str(_uuid.uuid4()),
                            server_id=_srv.id,
                            phase="taste_refresh",
                            status="running",
                            total_items=0,
                            processed_items=0,
                            started_at=now,
                        )
                        db.add(_run)
                        db.flush()
                        from app.workers import tasks as _t

                        enqueue_light(_t.refresh_tastes, str(_run.id), job_timeout=3600)
                    except Exception:
                        from app.workers import tasks as _t

                        enqueue_light(_t.refresh_tastes, job_timeout=3600)
                elif kind == "clap":
                    from app.workers import tasks as _t

                    enqueue_clap(_t.clap_embed, str(j.id), job_timeout=3600)
                elif kind == "covers_gc":
                    try:
                        from app.services.covers import clear_expired as _gc

                        _gc()
                    except Exception:
                        pass
                else:
                    continue
                j.last_run_at = now
                logger.info("cron {} enqueued", kind)
            except Exception as e:
                logger.warning("cron {} failed: {}", kind, e)
        db.commit()


def main() -> int:
    s = get_settings()
    configure_logging(s.log_level)
    from app.services.queue import init_redis

    init_redis()
    logger.info("scheduler started (tick 60s)")
    # первый тик сразу
    try:
        _tick_once()
    except Exception as e:
        logger.warning("initial tick failed: {}", e)
    while True:
        time.sleep(60)
        try:
            _tick_once()
        except Exception as e:
            logger.warning("cron tick failed: {}", e)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
