"""Добавление недостающих колонок в существующие таблицы.

Зачем это отдельным шагом. В проекте alembic-версий нет (папка
app/db/migrations отсутствует), поэтому init_db в проде доходит до
Base.metadata.create_all(). А create_all умеет только СОЗДАВАТЬ отсутствующие
таблицы — он никогда не добавляет колонки в уже существующие. В итоге после
обновления кода на живой базе запросы падают с UndefinedColumn:
  * playlists.comment            — «почему такой микс»
  * cron_jobs.last_status и ещё 6 колонок — наблюдаемость крона
Новые таблицы (recommendation_feedback) create_all создаст сам, а вот колонки
приходится досоздавать вручную.

Скрипт идемпотентен и безопасен: сначала смотрим, что уже есть, и добавляем
только отсутствующее. Работает и на Postgres, и на sqlite.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

# (таблица, колонка, DDL-тип)
COLUMNS: list[tuple[str, str, str]] = [
    # Пояснение к плейлисту (ИИ/шаблонное), выгружается в Navidrome.
    ("playlists", "comment", "TEXT"),
    # Наблюдаемость крона: что с последним запуском.
    ("cron_jobs", "last_status", "VARCHAR(16)"),
    ("cron_jobs", "last_error", "TEXT"),
    ("cron_jobs", "last_started_at", "TIMESTAMP"),
    ("cron_jobs", "last_finished_at", "TIMESTAMP"),
    ("cron_jobs", "next_run_at", "TIMESTAMP"),
    ("cron_jobs", "run_count", "INTEGER NOT NULL DEFAULT 0"),
    ("cron_jobs", "fail_count", "INTEGER NOT NULL DEFAULT 0"),
]


def ensure_columns(engine) -> list[str]:
    """Добавить отсутствующие колонки. Возвращает список добавленных.

    Не падает: если таблицы ещё нет (свежая база) — create_all её создаст
    вместе с колонками, и патч просто ничего не сделает.
    """
    added: list[str] = []
    try:
        insp = inspect(engine)
        existing_tables = set(insp.get_table_names())
    except Exception:
        return added
    for table, column, ddl in COLUMNS:
        if table not in existing_tables:
            # Таблицы ещё нет — её создаст create_all вместе с колонками.
            continue
        try:
            have = {c["name"] for c in insp.get_columns(table)}
        except Exception:
            continue
        if column in have:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            added.append(f"{table}.{column}")
        except Exception:
            # Гонка с другим процессом (два контейнера стартуют одновременно) —
            # тогда колонка уже есть, и это не ошибка.
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
                added.append(f"{table}.{column}")
            except Exception:
                continue
    if added:
        try:
            import logging as _lg

            _lg.getLogger("db").info("schema patch: добавлены колонки {}", added)
        except Exception:
            pass
    # Backfill: на живых базах run_count/fail_count могли создаться nullable
    # (старый патч делал bare INTEGER) и уже содержат NULL, который ORM
    # считает non-Optional. Приводим к 0, чтобы чтение не давало None.
    try:
        if "cron_jobs" in existing_tables:
            with engine.begin() as conn:
                conn.execute(text("UPDATE cron_jobs SET run_count=0 WHERE run_count IS NULL"))
                conn.execute(text("UPDATE cron_jobs SET fail_count=0 WHERE fail_count IS NULL"))
    except Exception:
        pass
    return added
