"""GET /api/users/{user_id}/favorite-artists — полный список артистов по лайкам."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid

from fastapi.testclient import TestClient

from app.db import models
from app.db.database import SessionLocal
from app.main import app

client = TestClient(app)


def _make_user_with_favorites():
    db = SessionLocal()
    try:
        srv = db.query(models.MediaServer).first()
        if srv is None:
            srv = models.MediaServer(type="navidrome", name="t", url="http://x")
            db.add(srv)
            db.flush()
        u = models.MediaUser(server_id=srv.id, external_id="fav-" + uuid.uuid4().hex[:8],
                             username="favuser")
        db.add(u)
        db.flush()
        uid = str(u.id)
        # Кино x3 (два написания — схлопнутся), Крематорий x1, безымянный трек — мимо
        tids = []
        for i, (title, artist) in enumerate([
            ("t1", "Кино"), ("t2", "кино "), ("t3", "КИНО"),
            ("t4", "Крематорий"), ("t5", None),
        ]):
            t = models.Track(server_id=srv.id, external_id=f"fav-ext-{uuid.uuid4().hex[:8]}-{i}",
                             title=title, artist_name=artist)
            db.add(t)
            db.flush()
            tids.append(str(t.id))
            db.add(models.Favorite(user_id=uid, track_id=str(t.id)))
        db.commit()
        return uid
    finally:
        db.close()


def test_favorite_artists_full_list_and_dedupe():
    uid = _make_user_with_favorites()
    r = client.get(f"/api/users/{uid}/favorite-artists")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == uid
    assert body["total"] == 2
    assert body["artists"][0] == {"name": "КИНО", "likes": 3}
    assert body["artists"][1] == {"name": "Крематорий", "likes": 1}


def test_favorite_artists_limit_and_errors():
    uid = _make_user_with_favorites()
    r = client.get(f"/api/users/{uid}/favorite-artists", params={"limit": 1})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert len(r.json()["artists"]) == 1

    r = client.get("/api/users/not-a-uuid/favorite-artists")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid id"

    r = client.get(f"/api/users/{uuid.uuid4()}/favorite-artists")
    assert r.status_code == 404


def test_favorite_artists_empty():
    db = SessionLocal()
    try:
        srv = db.query(models.MediaServer).first()
        if srv is None:
            srv = models.MediaServer(type="navidrome", name="t", url="http://x")
            db.add(srv)
            db.flush()
        u = models.MediaUser(server_id=srv.id, external_id="fav-empty-" + uuid.uuid4().hex[:8],
                             username="emptyuser")
        db.add(u)
        db.commit()
        uid = str(u.id)
    finally:
        db.close()
    r = client.get(f"/api/users/{uid}/favorite-artists")
    assert r.status_code == 200
    assert r.json() == {"user_id": uid, "total": 0, "artists": []}
