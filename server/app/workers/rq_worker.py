#!/usr/bin/env python
"""Запуск воркера RQ.

Новые очереди: high/default (legacy) + audio (librosa) + clap + light.
Выбор очередей через env QUEUES="high,default,audio,clap,light" или аргументы.

Примеры:
  QUEUES=audio python -m app.workers.rq_worker        # только тяжёлый анализ
  QUEUES=light,high,default python -m app.workers.rq_worker
  python -m app.workers.rq_worker audio clap          # аргументы

Встроенный scheduler УДАЛЁН — используйте отдельный сервис:
  python -m app.workers.scheduler
Legacy-режим (не рекомендуется): ENABLE_EMBEDDED_SCHEDULER=1
"""
from __future__ import annotations

import os
import socket
import sys
import uuid

from rq import Worker

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("worker")


def main() -> int:
    s = get_settings()
    configure_logging(s.log_level)
    from app.services.queue import get_queue, get_redis

    # очереди из argv или env
    if len(sys.argv) > 1:
        qnames = [a.strip() for a in sys.argv[1:] if a.strip()]
    else:
        qnames = [
            q.strip()
            for q in os.getenv("QUEUES", "high,default").split(",")
            if q.strip()
        ]
    if not qnames:
        qnames = ["high", "default"]
    queues = [get_queue(q) for q in qnames]
    logger.info("worker listening: {}", ",".join(qnames))

    if os.getenv("ENABLE_EMBEDDED_SCHEDULER", "") == "1":
        logger.warning("embedded scheduler enabled (legacy, may duplicate with scheduler service)")

    _uniq = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        queues, connection=get_redis(), name=f"kumaflow-{s.app_version}-{_uniq}"
    )
    worker.work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
