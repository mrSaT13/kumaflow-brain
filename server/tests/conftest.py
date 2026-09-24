"""Общая тестовая инфраструктура — как у крупняков.

- Один свежий sqlite-файл на прогон (в temp, стирается в конце): никаких
  накоплений между запусками, порядок модулей не важен.
- Каждый тест в своей транзакции с rollback: тесты не травят друг друга,
  коммиты внутри кода (record_events и т.п.) откатываются внешней транзакцией.
- Per-file DB_URL_OVERRIDE больше не нужны: engine уже привязан здесь,
  до импорта тестовых модулей (conftest импортируется первым).

Паттерн: connection-per-test + SessionLocal.configure(bind=conn),
в конце rollback. TestClient ходит через тот же SessionLocal.
"""
import os
import tempfile

_fd, _DB_PATH = tempfile.mkstemp(prefix="kfbrain_test_", suffix=".db")
os.close(_fd)
os.environ["DB_URL_OVERRIDE"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ.setdefault("REDIS_HOST", "localhost")

import pytest  # noqa: E402

from app.db import models  # noqa: E402,F401  (register models)
from app.db.database import SessionLocal, engine, init_db, reset_engine  # noqa: E402

reset_engine("sqlite:///" + _DB_PATH.replace("\\", "/"))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()
    yield
    try:
        from app.db.database import engine as _eng

        _eng.dispose()
    except Exception:
        pass
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _test_tx():
    conn = engine.connect()
    tx = conn.begin()
    SessionLocal.configure(bind=conn)
    try:
        yield
    finally:
        SessionLocal.configure(bind=engine)
        try:
            tx.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
