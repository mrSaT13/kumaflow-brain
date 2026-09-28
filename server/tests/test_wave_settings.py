"""Общая волна: настройки одни на всех устройствах + лайки из events не теряются."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import Favorite, MediaServer, MediaUser, RecommendationFeedback, Track
from app.services import taste as _taste
from app.services import wave_settings as _wset


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="waveset_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="waveset_tester", username="waveset_tester")
        db.add(u)
    if db.query(Track).filter_by(external_id="ws1").first() is None:
        db.add(Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id="ws1",
                     title="WS", artist_name="AA"))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _db():
    return SessionLocal()


def test_settings_crud_and_version():
    from app.db.models import WaveSettings as _WS

    db = _db()
    try:
        db.query(_WS).filter_by(user_id=UID).delete()
        db.commit()
        g = _wset.get_settings(db, UID)
        assert g["settings"] == {} and g["version"] == 1
        s1 = _wset.save_settings(db, UID, {"mood": "chill", "junk": "x"})
        assert s1["settings"] == {"mood": "chill"}  # мусор отрезаем
        assert s1["version"] == 2
        s2 = _wset.save_settings(db, UID, {"mood": ""})  # пустая стирает
        assert s2["settings"] == {} and s2["version"] == 3
        r = _wset.reset_settings(db, UID)
        assert r["settings"] == {} and r["version"] == 4
    finally:
        db.close()


def test_effective_settings_merge():
    db = _db()
    try:
        _wset.save_settings(db, UID, {"mood": "chill", "activity": "work"})
        # запрос без ключей — берём stored
        assert _wset.effective_settings(db, UID, {}) == {"mood": "chill", "activity": "work"}
        assert _wset.effective_settings(db, UID, None) == {"mood": "chill", "activity": "work"}
        # явный ключ побеждает
        eff = _wset.effective_settings(db, UID, {"mood": "dark"})
        assert eff == {"mood": "dark", "activity": "work"}
        # явное «авто» (пусто) — НЕ тянет stored
        eff = _wset.effective_settings(db, UID, {"mood": ""})
        assert eff == {"activity": "work"}
        _wset.reset_settings(db, UID)
    finally:
        db.close()


def test_like_via_events_creates_favorite_and_outcome():
    db = _db()
    try:
        t = db.query(Track).filter_by(external_id="ws1").first()
        db.query(Favorite).filter_by(user_id=UID).delete()
        db.query(RecommendationFeedback).filter_by(user_id=UID).delete()
        db.commit()
        # мозг показал трек волной
        from app.services import rec_feedback as _rfb

        assert _rfb.mark_served(db, UID, [{"track_id": str(t.id), "score": 0.9,
                                          "reason": "test"}], source="wave") == 1
        # плеер прислал лайк как событие (как шлёт мобила с волны)
        res = _taste.record_events(db, UID, [{"track_id": "ws1", "action": "like"}])
        assert res["stored"] == 1 and res["skipped"] == 0
        assert db.query(Favorite).filter_by(user_id=UID, track_id=str(t.id)).first() is not None
        row = db.query(RecommendationFeedback).filter_by(user_id=UID).first()
        assert row is not None and row.outcome == "liked"
        # повторный dislike через events — тоже доходит
        res = _taste.record_events(db, UID, [{"track_id": "ws1", "action": "dislike"}])
        assert res["stored"] == 1
    finally:
        db.close()


def test_settings_http_roundtrip():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.put("/api/wave/settings", json={"user_id": UID, "settings": {"mood": "dark"}})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["mood"] == "dark" and body["version"] >= 2
    g = c.get("/api/wave/settings", params={"user_id": UID}).json()
    assert g["settings"]["mood"] == "dark" and g["version"] == body["version"]
    d = c.delete("/api/wave/settings", params={"user_id": UID}).json()
    assert d["settings"] == {} and d["version"] == body["version"] + 1
