"""Library Sync: приёмник событий, конфиг, подписки."""
import os

os.environ["REDIS_HOST"] = "localhost"

from fastapi.testclient import TestClient

from app.db.database import SessionLocal
from app.db import models
from app.main import app
from app.services import library_sync as ls

client = TestClient(app)


def test_events_unknown_rejected():
    r = client.post("/api/library-sync/events", json={"event": "nope"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_events_disabled_without_config():
    r = client.post("/api/library-sync/events",
                    json={"event": "download.completed", "jobId": "j1", "tracks": []})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "выключен" in body["error"]


def test_config_masked_no_token_leak():
    r = client.get("/api/library-sync/config")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["token_configured"] is False
    assert "token" not in body or body.get("token") in (None, "", "***")


def test_subscriptions_crud_global():
    # add (глобальная, без юзера; провайдер выключен — только локальная запись)
    r = client.post("/api/library-sync/subscriptions",
                    json={"artist_name": "Кино"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["subscription"]["artist_name"] == "Кино"
    assert body["provider"]["skipped"] is True

    # list
    r = client.get("/api/library-sync/subscriptions")
    assert r.status_code == 200
    names = [s["artist_name"] for s in r.json()["subscriptions"]]
    assert "Кино" in names

    # повторный add — не дубль
    r = client.post("/api/library-sync/subscriptions",
                    json={"artist_name": "Кино"})
    assert r.json()["ok"] is True
    r = client.get("/api/library-sync/subscriptions")
    assert [s["artist_name"] for s in r.json()["subscriptions"]].count("Кино") == 1

    # delete
    r = client.request("DELETE", "/api/library-sync/subscriptions",
                       json={"artist_name": "Кино"})
    assert r.status_code == 200
    assert r.json()["removed"] is True
    r = client.get("/api/library-sync/subscriptions")
    assert "Кино" not in [s["artist_name"] for s in r.json()["subscriptions"]]


def test_subscriptions_per_user():
    db = SessionLocal()
    try:
        srv = db.query(models.MediaServer).first()
        if srv is None:
            srv = models.MediaServer(type="navidrome", name="t", url="http://x")
            db.add(srv)
            db.flush()
        u = models.MediaUser(server_id=srv.id, external_id="u1", username="u1")
        db.add(u)
        db.commit()
        uid = str(u.id)
    finally:
        db.close()
    r = client.post("/api/library-sync/subscriptions",
                    json={"user_id": uid, "artist_name": "Мумий Тролль"})
    assert r.status_code == 200
    assert r.json()["subscription"]["user_id"] == uid
    r = client.get("/api/library-sync/subscriptions", params={"user_id": uid})
    assert [s["artist_name"] for s in r.json()["subscriptions"]] == ["Мумий Тролль"]
    # чужой юзер — 404
    r = client.post("/api/library-sync/subscriptions",
                    json={"user_id": "00000000-0000-0000-0000-000000000000",
                          "artist_name": "X"})
    assert r.status_code == 404


def test_record_event_dedupe_and_subscribers():
    db = SessionLocal()
    try:
        assert ls.record_event(db, "job-1") is True
        assert ls.record_event(db, "job-1") is False
        db.add(models.ArtistSubscription(user_id=None, artist_name="Кино"))
        db.commit()
        subs = ls.find_subscribers(db, "  кино ")
        assert len(subs) == 1
        assert ls.find_subscribers(db, "Аквариум") == []
    finally:
        db.close()


def test_save_config_keeps_token_on_empty_form():
    """Регрессия 0.3.28: пустое поле токена не затирает сохранённый.

    Веб-UI ключ вообще не шлёт, но и явный "" от других клиентов
    токен убивать не должен. Проверка соединения подхватывает
    сохранённый токен (см. test_cfg: body.token or saved.token).
    """
    db = SessionLocal()
    try:
        ls.save_config(db, {"url": "http://srv:3000", "token": "secret123",
                            "enabled": True})
        # форма без ключа — как шлёт веб при пустом поле
        v = ls.save_config(db, {"url": "http://srv:3000", "enabled": True})
        assert v["token"] == "secret123"
        # явный пустой токен — тоже не затирает
        v = ls.save_config(db, {"url": "http://srv:3000", "token": "",
                                "enabled": True})
        assert v["token"] == "secret123"
        assert ls.get_config(db)["token"] == "secret123"
        # наружу токен не отдаётся
        masked = ls.masked_config(db)
        assert masked["token_configured"] is True
        assert "token" not in masked
        # новый токен — меняется как раньше
        v = ls.save_config(db, {"url": "http://srv:3000", "token": "new456",
                                "enabled": True})
        assert v["token"] == "new456"
    finally:
        db.close()
