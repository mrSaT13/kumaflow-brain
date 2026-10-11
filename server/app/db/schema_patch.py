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
    # Бан по Navidrome-ID артиста (десктоп шлёт пары {id, name}).
    # Nullable: старые именные строки работают как раньше, матчинг — имя ИЛИ id.
    ("artist_bans", "artist_external_id", "VARCHAR(128)"),
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


# (имя индекса, DDL). create_all индексы на СУЩЕСТВУЮЩИЕ таблицы не добавляет,
# поэтому живые базы после обновления их не получали — и горячие запросы
# (агрегаты вкуса на каждый опрос, сортировка сканов) шли Seq Scan'ом,
# держа слоты пула занятыми до QueuePool timeout.
INDEXES: list[tuple[str, str]] = [
    ("ix_play_history_user",
     "CREATE INDEX IF NOT EXISTS ix_play_history_user ON play_history (user_id)"),
    ("ix_play_history_user_track",
     "CREATE INDEX IF NOT EXISTS ix_play_history_user_track ON play_history (user_id, track_id)"),
    ("ix_scan_runs_started",
     "CREATE INDEX IF NOT EXISTS ix_scan_runs_started ON scan_runs (started_at DESC)"),
    ("ix_playlists_owner",
     "CREATE INDEX IF NOT EXISTS ix_playlists_owner ON playlists (owner_user_id)"),
]


def ensure_indexes(engine) -> list[str]:
    """Создать отсутствующие индексы. Идемпотентно (IF NOT EXISTS)."""
    added: list[str] = []
    try:
        insp = inspect(engine)
        existing_tables = set(insp.get_table_names())
    except Exception:
        return added
    for name, ddl in INDEXES:
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            added.append(name)
        except Exception:
            continue
    if added:
        try:
            import logging as _lg

            _lg.getLogger("db").info("schema patch: индексы проверены/созданы {}", added)
        except Exception:
            pass
    return added
