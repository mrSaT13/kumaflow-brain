from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("queue")


try:
    import redis as _redis
    from rq import Queue as _RQQueue

    _HAS_RQ = True
except Exception:
    _HAS_RQ = False


_redis_client: Optional["_redis.Redis"] = None
_rq_default: Optional["_RQQueue"] = None
_rq_high: Optional["_RQQueue"] = None
_executor: Optional[ThreadPoolExecutor] = None


def init_redis() -> None:
    global _redis_client, _rq_default, _rq_high, _executor
    settings = get_settings()
    if _HAS_RQ:
        try:
            _redis_client = _redis.from_url(settings.redis_url, decode_responses=False)
            _redis_client.ping()
            _rq_default = _RQQueue("default", connection=_redis_client)
            _rq_high = _RQQueue("high", connection=_redis_client)
            logger.info("redis connected at {}", settings.redis_url)
            return
        except Exception as e:
            logger.warning("redis unavailable ({}), falling back to in-process executor", e)
    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kumaflow-bg")


def shutdown_redis() -> None:
    global _redis_client, _rq_default, _rq_high, _executor
    try:
        if _redis_client is not None:
            _redis_client.close()
    except Exception:
        pass
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
    _redis_client = None
    _rq_default = None
    _rq_high = None
    _executor = None


def get_redis():
    """Общий Redis-клиент (нужен воркеру). Подключается при первом вызове."""
    global _redis_client
    if _redis_client is None:
        init_redis()
    if _redis_client is None:
        raise RuntimeError("Redis недоступен (проверьте REDIS_HOST/REDIS_PORT)")
    return _redis_client


def get_queue(name: str = "default"):
    """RQ-очередь на общем Redis-клиенте (нужна воркеру)."""
    global _rq_default, _rq_high
    if not _HAS_RQ:
        raise RuntimeError("RQ не установлен")
    conn = get_redis()
    if name == "high":
        if _rq_high is None:
            _rq_high = _RQQueue("high", connection=conn)
        return _rq_high
    if _rq_default is None:
        _rq_default = _RQQueue("default", connection=conn)
    return _rq_default


def enqueue(fn: Callable[..., Any], *args: Any, queue: str = "default", job_timeout: int | None = None, **kwargs: Any) -> str:
    """Enqueue a callable. Returns a job id (real RQ id or synthetic 'bg-<n>')."""
    if _rq_default is not None:
        q = _rq_high if queue == "high" else _rq_default
        # job_timeout=None → дефолт RQ (180 c). Для долгих сканов вызывающий
        # передаёт явное значение (см. app/api/scan.py).
        job = q.enqueue(fn, *args, job_timeout=job_timeout, **kwargs)
        return job.get_id()
    if _executor is None:
        init_redis()
    assert _executor is not None
    fut: Future = _executor.submit(fn, *args, **kwargs)

    job_id = f"bg-{id(fut)}"
    logger.info("submitted to in-process executor: {} -> {}", job_id, fn.__name__)
    return job_id


def cancel_job(job_id: str | None) -> dict:
    """Попытка снять задачу с очереди RQ. Возвращает статус операции."""
    if not job_id:
        return {"ok": False, "error": "no job_id"}
    if job_id.startswith("bg-"):
        # in-process executor отменить нельзя — воркер сам увидит
        # статус failure в БД (кооперативная отмена) и остановится.
        return {"ok": True, "mode": "cooperative"}
    if not _HAS_RQ:
        return {"ok": False, "error": "RQ не установлен"}
    try:
        conn = get_redis()
        from rq.job import Job as _RQJob

        try:
            job = _RQJob.fetch(job_id, connection=conn)
        except Exception:
            return {"ok": False, "error": "job not found (уже выполняется или завершён)"}
        try:
            job.cancel()
        except Exception:
            pass
        # если job ещё в очереди — убрать из обеих очередей
        for qname in ("default", "high"):
            try:
                get_queue(qname).remove(job)
            except Exception:
                pass
        return {"ok": True, "mode": "rq-cancel"}
    except Exception as e:  # noqa: BLE001
        logger.warning("cancel_job failed: {}", e)
        return {"ok": False, "error": str(e)}
