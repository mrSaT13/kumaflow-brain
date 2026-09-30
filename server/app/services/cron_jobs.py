"""Сидинг и проверка крон-задач.

Раньше дефолтные cron-задачи создавались ТОЛЬКО внутри GET /api/cron/, то есть
только когда кто-то открыл вкладку «Настройки → Автоматизация». Пока вкладку не
открывали (или запрос упал по 401 при включённой авторизации) таблица cron_jobs
оставалась пустой, а scheduler тикал по ней раз в минуту и не делал ничего —
выглядело как «крон сломан», хотя он даже не знал, что запускать.

Теперь ensure_defaults() зовут и backend при старте (lifespan), и сам scheduler —
сидинг идемпотентный (upsert по kind), так что новые задачи подтягиваются и на
старых базах, а отключённые пользователем задачи не включаются обратно.
"""
from __future__ import annotations

# (имя, kind, cron-выражение)
DEFAULTS: list[tuple[str, str, str]] = [
    ("Daily per-user", "daily", "0 3 * * *"),
    ("Taste refresh", "refresh_tastes", "30 4 * * *"),
    ("CLAP embed", "clap", "0 4 * * *"),
    ("Smart playlists", "smart", "0 6 * * *"),
    ("Taste snapshots", "snapshots", "0 7 * * 0"),
    ("Weekly discovery", "weekly", "0 6 * * 1"),
    ("Cover GC 7d", "covers_gc", "0 5 * * 0"),
    ("Auto-tune волны", "auto_tune", "20 5 * * *"),
]

# Допустимые виды выражения для 5-полей crontab: минута час день месяц день-недели.
# Без этой проверки битое выражение (его пишет UI без валидации) приводило к
# тому, что scheduler считал задачу «пора запускать» на каждом тике — то есть
# раз в минуту.
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

# Ключ heartbeat'а: scheduler пишет время последнего тика сюда, а
# GET /api/cron/health читает и говорит «планировщик жив / молчит».
HEARTBEAT_KEY = "cron_heartbeat"
# Тик раз в минуту, поэтому три пропущенных подряд = считаем scheduler мёртвым.
HEARTBEAT_STALE_SEC = 180

# Задачи, которые при первом запуске НЕ стоит выполнять «догоняющим» прогоном:
# они тяжёлые (полный проход по библиотеке/всем юзерам) и и так запустятся
# по своему расписанию. Остальные (GC кэша, снапшоты вкуса) дёшевые — их
# безопасно догнать сразу, чтобы на свежей базе UI не выглядел «мёртвым».
CATCH_UP_SAFE_KINDS = {"covers_gc", "snapshots"}


def is_valid_cron(expr: str | None) -> bool:
    """True, если expr — валидное 5-полевое crontab-выражение."""
    if not expr:
        return False
    parts = str(expr).split()
    if len(parts) != 5:
        return False
    for part, (lo, hi) in zip(parts, _FIELD_RANGES):
        if not part:
            return False
        for chunk in part.split(","):
            # допускаем *, */step, a-b, a-b/step, число
            base = chunk.split("/", 1)[0].strip()
            if not base or base == "*":
                continue
            bounds = base.split("-")
            for bound in bounds:
                if not bound.isdigit():
                    return False
                if not (lo <= int(bound) <= hi):
                    return False
            # перевёрнутый диапазон (5-1) — опечатка, а не «каждые 4 минуты»
            if len(bounds) == 2 and int(bounds[0]) > int(bounds[1]):
                return False
    return True


def ensure_defaults(db) -> int:
    """Создать недостающие дефолтные задачи. Возвращает число добавленных.

    Идемпотентно и безопасно для повторного вызова: существующие задачи
    (в т.ч. с изменённым пользователем cron_expr или enabled=False) не трогаем.
    """
    import uuid

    from app.db.models import CronJob

    have = {r[0] for r in db.query(CronJob.kind).all()}
    added = 0
    for name, kind, expr in DEFAULTS:
        if kind in have:
            continue
        db.add(CronJob(id=str(uuid.uuid4()), name=name, kind=kind, cron_expr=expr, enabled=True))
        added += 1
    if added:
        db.commit()
    return added
