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
    queues = [get_queue("high"), get_queue("default")]
    worker = Worker(queues, connection=get_redis(), name=f"kumaflow-{s.app_version}")
    worker.work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
