#!/usr/bin/env python
"""Запуск воркера RQ: слушает очереди default и high."""
from __future__ import annotations

import os
import socket
import sys
import uuid

from rq import Worker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.queue import get_queue, get_redis


def main() -> int:
    s = get_settings()
    configure_logging(s.log_level)
    # лёгкий планировщик крона (APScheduler) — только если redis доступен
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from app.db.database import session_scope
        from app.db.models import CronJob
        from app.services.queue import enqueue

        def _tick():
            try:
                with session_scope() as db:
                    jobs = db.query(CronJob).filter(CronJob.enabled.is_(True)).all()
                    for j in jobs:
                        # точный cron парсим через CronTrigger; last_run_at в UTC
                        try:
                            trig = CronTrigger.from_crontab(j.cron_expr)
                            now = __import__("datetime").datetime.utcnow()
                            if j.last_run_at:
                                nxt = trig.get_next_fire_time(None, j.last_run_at)
                                if nxt and nxt > now:
                                    continue
                        except Exception:
                            pass
                        from app.core.logging import get_logger as _gl2
                        _clog = _gl2("cron")
                        if j.kind == "daily":
                            enqueue(__import__("app.workers.tasks", fromlist=["daily_per_user"]).daily_per_user, job_timeout=3600)
                            j.last_run_at = __import__("datetime").datetime.utcnow()
                            _clog.info("cron daily enqueued")
                        elif j.kind == "smart":
                            enqueue(__import__("app.workers.tasks", fromlist=["smart_playlists"]).smart_playlists, job_timeout=3600)
                            j.last_run_at = __import__("datetime").datetime.utcnow()
                        elif j.kind == "snapshots":
                            enqueue(__import__("app.workers.tasks", fromlist=["taste_snapshots"]).taste_snapshots, job_timeout=1800)
                            j.last_run_at = __import__("datetime").datetime.utcnow()
                        elif j.kind == "weekly":
                            enqueue(__import__("app.workers.tasks", fromlist=["weekly_discovery_all"]).weekly_discovery_all, job_timeout=3600)
                            j.last_run_at = __import__("datetime").datetime.utcnow()
                            _clog.info("cron weekly discovery enqueued")
                        elif j.kind == "refresh_tastes":
                            import uuid as _uuid

                            from app.db.models import ScanRun as _SR
                            from app.services.media_server import resolve_active_server as _ras
                            try:
                                _srv = _ras(db)
                                db.commit()
                                _run = _SR(id=str(_uuid.uuid4()), server_id=_srv.id, phase="taste_refresh",
                                           status="running", total_items=0, processed_items=0,
                                           started_at=__import__("datetime").datetime.utcnow())
                                db.add(_run)
                                db.flush()
                                enqueue(__import__("app.workers.tasks", fromlist=["refresh_tastes"]).refresh_tastes, str(_run.id), job_timeout=3600)
                            except Exception:
                                enqueue(__import__("app.workers.tasks", fromlist=["refresh_tastes"]).refresh_tastes, job_timeout=3600)
                            j.last_run_at = __import__("datetime").datetime.utcnow()
                        elif j.kind == "clap":
                            enqueue(__import__("app.workers.tasks", fromlist=["clap_embed"]).clap_embed, str(j.id), job_timeout=3600)
                            j.last_run_at = __import__("datetime").datetime.utcnow()
                        elif j.kind == "covers_gc":
                            try:
                                __import__("app.services.covers", fromlist=["clear_expired"]).clear_expired()
                            except Exception:
                                pass
                            j.last_run_at = __import__("datetime").datetime.utcnow()
                    db.commit()
            except Exception as e:
                from app.core.logging import get_logger
                get_logger("cron").warning("cron tick failed: {}", e)

        sched = BackgroundScheduler(daemon=True)
        sched.add_job(_tick, "interval", minutes=10, id="kumaflow-cron")
        sched.start()
        # первый тик сразу при старте воркера, чтобы не ждать 10 мин после рестарта
        try:
            _tick()
        except Exception:
            pass
    except Exception as e:
        from app.core.logging import get_logger
        get_logger("cron").warning("scheduler not started: {}", e)
    queues = [get_queue("high"), get_queue("default")]
    # Уникальное имя на инстанс: иначе при быстром рестарте контейнера
    # старый ключ rq:worker:kumaflow-0.1.0 ещё жив в Redis (TTL) и новый
    # процесс падает в register_birth: "There exists an active worker named...".
    # Плюс это позволяет масштабировать worker --scale > 1.
    _uniq = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    worker = Worker(queues, connection=get_redis(), name=f"kumaflow-{s.app_version}-{_uniq}")
    worker.work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
