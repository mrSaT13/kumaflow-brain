#!/usr/bin/env python
"""Запуск воркера RQ: слушает очереди default и high."""
from __future__ import annotations

import sys

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
                        # простая проверка: если last_run_at сегодня — скип (daily)
                        # точный cron парсим через CronTrigger
                        try:
                            trig = CronTrigger.from_crontab(j.cron_expr)
                            now = __import__("datetime").datetime.utcnow()
                            # если last_run уже после предыдущего fire — скип
                            if j.last_run_at and trig.get_next_fire_time(None, j.last_run_at) and trig.get_next_fire_time(None, j.last_run_at) > now:
                                continue
                        except Exception:
                            pass
                        if j.kind == "daily":
                            enqueue(__import__("app.workers.tasks", fromlist=["daily_per_user"]).daily_per_user, job_timeout=3600)
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
        sched.add_job(_tick, "interval", minutes=60, id="kumaflow-cron")
        sched.start()
    except Exception as e:
        from app.core.logging import get_logger
        get_logger("cron").warning("scheduler not started: {}", e)
    queues = [get_queue("high"), get_queue("default")]
    worker = Worker(queues, connection=get_redis(), name=f"kumaflow-{s.app_version}")
    worker.work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
