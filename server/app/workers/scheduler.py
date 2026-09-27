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

    from app.core.time import server_tz as _server_tz
    from app.core.time import utcnow as _utcnow
    from app.db.database import session_scope
    from app.db.models import CronJob, ScanRun
    from app.services.cron_jobs import is_valid_cron
    from app.services.queue import enqueue, enqueue_clap, enqueue_light

    import uuid as _uuid

    from app.services.media_server import resolve_active_server as _ras

    now = _utcnow()
    # Крон живёт по «домашнему» времени (APP_TIMEZONE): naive-стенка local,
    # чтобы daily 0 3 * * * стрелял в 3 ночи по дому, а не по UTC.
    # В БД по-прежнему пишем naive UTC (совместимость колонок).
    now_local = now.replace(tzinfo=timezone.utc).astimezone(_server_tz()).replace(tzinfo=None)
    with session_scope() as db:
        jobs = db.query(CronJob).filter(CronJob.enabled.is_(True)).all()
        # Раньше тик молчал, а крон «не работал»: непонятно было, пустая таблица
        # или просто не наступило время. Теперь видно и то, и другое.
        logger.info("cron tick: {} задач(и) активно", len(jobs))
        # Ограничитель «догоняющих» запусков. У задачи, которая ещё ни разу не
        # выполнялась (last_run_at пуст — свежая база), условие расписания не
        # проверяется вовсе, то есть запускается она сразу. Раньше так стартовали
        # ВСЕ задачи одним тиком: на пустой базе это полный проход по библиотеке
        # плюс эмбеддинги разом. Теперь не больше MAX_PER_TICK за тик, остальные
        # подхватываются следующими тиками (минута) — и это видно в UI.
        MAX_PER_TICK = 2
        started = 0
        # Сначала дешёвые catch-up-задачи, потом остальные по расписанию.
        for j in sorted(jobs, key=lambda x: (x.last_run_at is not None, x.kind)):
            never_run = j.last_run_at is None
            if started >= MAX_PER_TICK:
                if never_run:
                    logger.info("cron {}: ждёт очереди (лимит {} запусков за тик)", j.kind, MAX_PER_TICK)
                continue
            try:
                if not is_valid_cron(j.cron_expr):
                    # Раньше битое выражение попадало в except ниже и задача
                    # считалась «пора запускать» на КАЖДОМ тике — то есть раз в
                    # минуту. Теперь такую задачу пропускаем и пишем в лог.
                    logger.warning("cron {}: некорректное выражение {!r} — задача пропущена",
                                   j.kind, j.cron_expr)
                    continue
                if not never_run:
                    trig = CronTrigger.from_crontab(j.cron_expr)
                    last_local = j.last_run_at.replace(tzinfo=timezone.utc) \
                        .astimezone(_server_tz()).replace(tzinfo=None)
                    nxt = trig.get_next_fire_time(None, last_local)
                    if nxt and nxt.replace(tzinfo=None) > now_local:
                        continue
                else:
                    logger.info("cron {}: ни разу не запускалась — догоняющий запуск", j.kind)
            except Exception as e:
                logger.warning("cron {}: не удалось вычислить время следующего запуска: {}",
                               j.kind, e)
                continue
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

                    # Тумблер CLAP в вебе: выключен — не дёргаем воркер заново.
                    # Раньше крон всё равно ставил джобу, и та молча отписывалась
                    # «skipped», хотя очередь clap продолжала расходовать ресурсы.
                    try:
                        from app.services.automation import clap_enabled as _clap_flag

                        if not _clap_flag():
                            logger.info("cron clap: выключен тумблером в настройках — пропускаю")
                            j.last_run_at = now
                            j.last_status = "ok"
                            j.last_error = "выключен тумблером CLAP"
                            continue
                    except Exception:
                        pass
                    enqueue_clap(_t.clap_embed, str(j.id), job_timeout=3600)
                elif kind == "covers_gc":
                    try:
                        from app.services.covers import clear_expired as _gc

                        _gc()
                    except Exception:
                        pass
                else:
                    continue
                # Статусы видны в UI: без них «крон не работает» и «крон упал» выглядели одинаково.
                j.last_run_at = now
                j.last_started_at = now
                j.last_finished_at = now
                j.last_status = "ok"
                j.last_error = None
                j.run_count = (j.run_count or 0) + 1
                _next = None
                try:
                    _t2 = CronTrigger.from_crontab(j.cron_expr)
                    _f = _t2.get_next_fire_time(None, now_local)
                    if _f:
                        _next = _f.replace(tzinfo=None)
                except Exception:
                    pass
                j.next_run_at = _next
                started += 1
                logger.info("cron {} enqueued (следующий запуск: {})", kind, j.next_run_at)
            except Exception as e:
                j.last_status = "error"
                j.last_error = str(e)[:500]
                j.fail_count = (j.fail_count or 0) + 1
                logger.warning("cron {} failed: {}", kind, e)
        db.commit()
        # Сторож sonic-анализа: RQ убивает job по job_timeout жёстко (work horse),
        # except внутри задачи не выполняется и автопродолжение не встаёт —
        # цепочка молча умирает («всё встало»). Детектим зависший running-run
        # по отсутствию прогресса в ScanLog >45 мин и перезапускаем остаток.
        try:
            _watchdog_analysis(db, now)
            db.commit()
        except Exception as e:
            logger.warning("analysis watchdog failed: {}", e)
            try:
                db.rollback()
            except Exception:
                pass
        _write_heartbeat(None)


def _write_heartbeat(error: str | None) -> None:
    """Отметка «scheduler жив». Её читает GET /api/cron/health.

    Пишется КАЖДЫЙ тик даже при ошибке — иначе по «молчанию» нельзя отличить
    «упал» от «тик был, но все задачи пропущены».
    """
    from app.core.time import utcnow
    from app.db.database import session_scope
    from app.db.models import AppSetting
    from app.services.cron_jobs import HEARTBEAT_KEY

    try:
        with session_scope() as db:
            row = db.get(AppSetting, HEARTBEAT_KEY)
            val = dict(row.value) if row and isinstance(row.value, dict) else {}
            val["last_tick_at"] = utcnow().isoformat()
            val["last_tick_error"] = error
            if row is None:
                db.add(AppSetting(key=HEARTBEAT_KEY, value=val))
            else:
                row.value = val
    except Exception as e:
        logger.warning("не удалось записать heartbeat крона: {}", e)


STALE_ANALYSIS_MIN = 45


def _watchdog_analysis(db, now) -> None:
    """Перезапуск sonic-анализа, умершего без автопродолжения.

    Условия: есть run phase=analysis status=running, а свежих ScanLog
    (прогресс пишется каждые 25 треков) нет дольше STALE_ANALYSIS_MIN.
    Остаток задача пересчитает сама из БД — просто ставим новый чанк.
    """
    from datetime import timedelta

    from app.db.models import ScanLog, ScanRun, Track, TrackFeatures

    running = (
        db.query(ScanRun)
        .filter(ScanRun.phase == "analysis", ScanRun.status == "running")
        .order_by(ScanRun.started_at.desc())
        .all()
    )
    if not running:
        return
    # свежий run (стартовал меньше порога назад) — даём работать
    latest = running[0]
    try:
        last_log = (
            db.query(ScanLog)
            .filter(ScanLog.run_id == latest.id)
            .order_by(ScanLog.created_at.desc())
            .first()
        )
    except Exception:
        last_log = None
    anchor = None
    if last_log is not None and last_log.created_at:
        anchor = last_log.created_at
    elif latest.started_at:
        anchor = latest.started_at
    if anchor is not None and now - anchor < timedelta(minutes=STALE_ANALYSIS_MIN):
        return
    # Двойная защита от ложного срабатывания: молодой run (старт <3ч назад)
    # не трогаем даже без свежих логов — пачка медленных треков может молчать
    # дольше порога, а дубль чанка = двойная работа по тем же трекам.
    if latest.started_at is not None and now - latest.started_at < timedelta(hours=3):
        return
    remaining = (
        db.query(Track)
        .outerjoin(TrackFeatures, TrackFeatures.track_id == Track.id)
        .filter(TrackFeatures.track_id.is_(None))
        .count()
    )
    if remaining <= 0:
        for r in running:
            r.status = "success"
            r.finished_at = now
        logger.info("analysis watchdog: остатка нет, закрыл {} зависших run", len(running))
        return
    import uuid as _uuid

    from app.db.models import ScanLog as _SL

    from app.workers import tasks as _t

    for r in running:
        r.status = "failure"
        r.finished_at = now
        r.error = "Сторож: нет прогресса >45 мин (RQ убил job по таймауту) — остаток перезапущен"
    new_id = str(_uuid.uuid4())
    db.add(ScanRun(id=new_id, server_id=latest.server_id, phase="analysis",
                   status="running", total_items=remaining, processed_items=0,
                   started_at=now))
    db.add(_SL(id=str(_uuid.uuid4()), run_id=new_id, level="info",
               message=f"Сторож перезапустил анализ: осталось {remaining} (прошлый run {str(latest.id)[:8]})"))
    db.flush()
    enqueue(_t.sonic_analysis, new_id, job_timeout=7200)
    logger.info("analysis watchdog: перезапустил остаток {} (прошлый {})", remaining, str(latest.id)[:8])


def main() -> int:
    s = get_settings()
    configure_logging(s.log_level)
    from app.services.queue import init_redis

    init_redis()
    logger.info("scheduler started (tick 60s)")
    # Своим ходом создаём недостающие cron-задачи: так scheduler не зависит от
    # того, открывал ли кто-нибудь веб. На чистой базе без этого тикать не по чему.
    try:
        from app.db.database import session_scope as _ss
        from app.services.cron_jobs import ensure_defaults as _ensure_cron_defaults

        with _ss() as _db:
            _added = _ensure_cron_defaults(_db)
        if _added:
            logger.info("cron defaults seeded: {}", _added)
    except Exception as e:
        logger.warning("не удалось создать cron-задачи по умолчанию: {}", e)
    # Первый тик сразу: он же догонит дешёвые задачи, которые ни разу не
    # запускались (last_run_at пуст), чтобы на свежей базе UI не выглядел мёртвым.
    _run_tick()
    while True:
        time.sleep(60)
        _run_tick()
    return 0


def _run_tick() -> None:
    """Один тик. Никогда не пробрасывает исключение наверх: иначе main() упал бы
    и крон перестал бы тикать навсегда (а «падающий» крон выглядит как полная
    тишина — самый непонятный симптом из возможных)."""
    err: str | None = None
    try:
        _tick_once()
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        logger.warning("cron tick failed: {}", e)
    try:
        _write_heartbeat(err)
    except Exception:
        pass


if __name__ == "__main__":
    import sys

    sys.exit(main())
