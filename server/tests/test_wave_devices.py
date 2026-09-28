"""Per-device слоты живой очереди: два плеера не затирают друг друга.

Регрессия на «Сейчас #3 vs Продолжить #1»: раньше был один слот на юзера
(last-writer-wins), телефон и десктоп перезаписывали друг друга.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.api import wave as _api
from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, Track


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="device_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="device_tester", username="device_tester")
        db.add(u)
    for ext in ("devA", "devB"):
        if db.query(Track).filter_by(external_id=ext).first() is None:
            db.add(Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                         title=f"T-{ext}", artist_name="AA"))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def test_norm_device():
    assert _api._norm_device("Pixel 8!!") == "pixel8"
    assert _api._norm_device("") == "default"
    assert _api._norm_device(None) == "default"
    assert _api._norm_device("UPPER-Mix_1") == "upper-mix_1"


def test_two_devices_dont_clobber():
    _api._LIVE.clear()
    _api._live_put(UID, {"queue": ["devA"], "current_track_id": "devA"},
                   device="phone-1")
    _api._live_put(UID, {"queue": ["devB"], "current_track_id": "devB"},
                   device="desktop-2")
    # каждый слот хранит своё
    assert _api._live_get(UID, "phone-1")["queue"] == ["devA"]
    assert _api._live_get(UID, "desktop-2")["queue"] == ["devB"]
    # список слотов — оба
    slots = dict((s, e["queue"]) for s, e, _a in _api._live_slots(UID))
    assert slots.get("phone-1") == ["devA"]
    assert slots.get("desktop-2") == ["devB"]
    # latest — самый свежий (desktop-2 опубликован позже)
    s, e, _a = _api._live_latest(UID)
    assert s == "desktop-2" and e["queue"] == ["devB"]


def test_api_publish_live_per_device():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    _api._LIVE.clear()
    r1 = c.post("/api/wave/publish", json={"user_id": UID, "queue": ["devA"],
                                           "current_track_id": "devA",
                                           "device": "phone-1"})
    assert r1.status_code == 200 and r1.json()["device_id"] == "phone-1"
    r2 = c.post("/api/wave/publish", json={"user_id": UID, "queue": ["devB"],
                                           "current_track_id": "devB",
                                           "device": "desktop-2"})
    assert r2.status_code == 200
    # без ?device — latest (desktop-2), devices[] — оба
    live = c.get("/api/wave/live", params={"user_id": UID}).json()
    assert live["device_id"] == "desktop-2"
    assert {d["device_id"] for d in live["devices"]} >= {"phone-1", "desktop-2"}
    # ?device=phone-1 — слот телефона, не задетый десктопом
    ph = c.get("/api/wave/live", params={"user_id": UID, "device": "phone-1"}).json()
    assert ph["device_id"] == "phone-1"
    assert [t["title"] for t in ph["queue"]] == ["T-devA"]
    # resume того же слота — тот же трек (было: live #3 vs resume #1)
    res = c.get("/api/wave/resume", params={"user_id": UID, "device": "phone-1"}).json()
    assert res["track"]["title"] == "T-devA"
    assert res["device_id"] == "phone-1"
