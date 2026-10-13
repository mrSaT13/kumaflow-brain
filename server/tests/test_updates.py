"""Проверка обновлений: парсинг версии, сравнение, дедуп уведомлений. Без сети."""
from app.services import updates as up


def test_parse_version():
    assert up.parse_version('    app_version: str = "0.3.12"\n') == "0.3.12"
    assert up.parse_version("no version here") is None
    assert up.parse_version(None) is None
    assert up.parse_version("") is None


def test_is_newer():
    assert up.is_newer("0.3.12", "0.3.11") is True
    assert up.is_newer("0.4.0", "0.3.99") is True
    assert up.is_newer("0.3.11", "0.3.11") is False
    assert up.is_newer("0.3.10", "0.3.11") is False
    assert up.is_newer(None, "0.3.11") is False
    assert up.is_newer("0.3.12", None) is False


def test_check_uses_fetch(monkeypatch):
    monkeypatch.setattr(up, "fetch_latest", lambda timeout=15: "9.9.9")
    monkeypatch.setattr(up, "current_version", lambda: "0.3.12")
    res = up.check()
    assert res == {"current": "0.3.12", "latest": "9.9.9",
                   "update_available": True}


def test_check_no_network(monkeypatch):
    monkeypatch.setattr(up, "fetch_latest", lambda timeout=15: None)
    monkeypatch.setattr(up, "current_version", lambda: "0.3.12")
    res = up.check()
    assert res["latest"] is None
    assert res["update_available"] is False


def test_notify_once_per_version(monkeypatch):
    from app.db.database import SessionLocal, init_db
    from app.db.models import AppSetting, Notification

    monkeypatch.setattr(up, "fetch_latest", lambda timeout=15: "9.9.9")
    monkeypatch.setattr(up, "current_version", lambda: "0.3.12")
    init_db()
    db = SessionLocal()
    try:
        db.query(Notification).delete()
        db.query(AppSetting).filter(AppSetting.key == up.NOTIFIED_KEY).delete()
        db.commit()
        r1 = up.check_and_notify(db)
        assert r1["notified"] is True
        n1 = db.query(Notification).count()
        assert n1 == 1
        # второй прогон — та же версия, уведомления нет
        r2 = up.check_and_notify(db)
        assert r2["notified"] is False
        assert db.query(Notification).count() == 1
    finally:
        db.close()
