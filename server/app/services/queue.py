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


def enqueue(fn: Callable[..., Any], *args: Any, queue: str = "default", **kwargs: Any) -> str:
    """Enqueue a callable. Returns a job id (real RQ id or synthetic 'bg-<n>')."""
    if _rq_default is not None:
        q = _rq_high if queue == "high" else _rq_default
        job = q.enqueue(fn, *args, **kwargs)
        return job.get_id()
    if _executor is None:
        init_redis()
    assert _executor is not None
    fut: Future = _executor.submit(fn, *args, **kwargs)

    job_id = f"bg-{id(fut)}"
    logger.info("submitted to in-process executor: {} -> {}", job_id, fn.__name__)
    return job_id
