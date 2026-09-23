from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from app.core.config import get_settings as _gs

    from app.db import models  # noqa: F401  (register models)

    # prod: схема — через alembic, если есть версии; иначе create_all,
    # чтобы свежий прод не остался без таблиц (migrations/ пока пустая).
    # dev/sqlite: всегда create_all для быстрого старта.
    try:
        is_prod = _gs().env == "prod"
    except Exception:
        is_prod = False
    if is_prod:
        try:
            from pathlib import Path

            from alembic import command as _alembic_cmd
            from alembic.config import Config as _AlembicConfig

            _mig = Path(__file__).resolve().parent / "migrations"
            _versions = _mig / "versions"
            _has_versions = _versions.is_dir() and any(_versions.glob("*.py"))
            if _has_versions:
                _cfg = _AlembicConfig()
                _cfg.set_main_option("script_location", str(_mig))
                _cfg.set_main_option("sqlalchemy.url", _gs().database_url)
                _alembic_cmd.upgrade(_cfg, "head")
                return
        except Exception:
            pass
        import logging as _lg

        _lg.getLogger("db").warning(
            "prod init_db: версий alembic нет, создаём схему через create_all"
        )
    Base.metadata.create_all(bind=engine)
