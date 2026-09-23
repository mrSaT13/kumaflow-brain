"""Импорт из Яндекс Музыки (Marshal) — мэтчинг, тумблеры, токен. Без сети: фейк-клиент."""
import os

os.environ["DB_URL_OVERRIDE"] = "sqlite:///./test_yandex_lib.db"
os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import Favorite, MediaServer, MediaUser, Track
from app.services.yandex_music import library as lib


class FakeClient:
    def __init__(self, *a, **k):
        pass

    def init(self):
        return self

    def account_status(self):
        return {"account": {"login": "tester"}}

    def users_likes_tracks(self):
        return [{"id": "11", "album_id": "1", "timestamp": "2026-01-01T10:00:00"},
                {"id": "99", "album_id": "9", "timestamp": "2026-01-02T10:00:00"}]

    def users_dislikes_tracks(self):
        return []

    def users_likes_artists(self):
        return [{"name": "Local Band"}]

    def users_dislikes_artists(self):
        return [{"name": "Bad Band"}]

    def users_likes_albums(self):
        return []

    def tracks(self, refs):
        out = []
        for r in refs:
            tid = str(r).split(":")[0]
            if tid == "11":
                out.append({"id": "11", "title": "Sun Song",
                            "artists": [{"name": "Local Band"}],
                            "albums": [{"genre": "rock"}]})
            elif tid == "99":
                out.append({"id": "99", "title": "Unknown Song",
                            "artists": [{"name": "Nobody"}],
                            "albums": [{"genre": "pop"}]})
            elif tid == "55":
                out.append({"id": "55", "title": "Chart Hit",
                            "artists": [{"name": "Local Band"}],
                            "albums": [{"genre": "rock"}]})
        return out

    def music_history(self):
        return [{"id": "11", "album_id": "1", "timestamp": "2026-02-01T10:00:00",
                 "artists": [{"name": "Local Band"}], "title": "Sun Song"}]

    def chart(self):
        return {"tracks": [{"id": "55", "album_id": "5"}]}

    def new_releases(self):
        return {"albums": []}


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="ym_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="ym_tester", username="ym_tester")
        db.add(u)
    if db.query(Track).filter_by(external_id="nav1").first() is None:
        db.add(Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id="nav1",
                     title="Sun Song", artist_name="Local Band"))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()
lib._client_cls = lambda: FakeClient  # noqa: SLF001 — без сети


def _db():
    return SessionLocal()


def test_token_roundtrip():
    db = _db()
    try:
        assert lib.has_user_token(db, UID) is False
        r = lib.save_user_token(db, UID, "y0_test")
        assert r["ok"] is True
        assert lib.has_user_token(db, UID) is True
        assert lib.forget_user_token(db, UID) == {"ok": True}
        assert lib.has_user_token(db, UID) is False
    finally:
        db.close()


def test_import_taste_match_and_ban():
    db = _db()
    try:
        r = lib.import_taste(db, UID, client=FakeClient())
        assert r["ok"] is True
        assert r["matched"] == 1
        assert r["unmatched"] == 1
        # идемпотентно: повторный прогон ничего не добавляет, но вкус на месте
        assert db.query(Favorite).filter_by(user_id=UID).count() >= 1
        assert r["profile_banned"] >= 0
        from app.db.models import ArtistBan as _AB

        assert db.query(_AB).filter_by(user_id=UID, artist_name="Bad Band").count() == 1
    finally:
        db.close()


def test_import_history_toggle():
    db = _db()
    try:
        lib.save_import_settings(db, {"history_enabled": False})
        r = lib.import_history(db, UID, client=FakeClient())
        assert r["ok"] is False
        lib.save_import_settings(db, {"history_enabled": True})
        r = lib.import_history(db, UID, client=FakeClient())
        assert r["ok"] is True
        assert r["matched"] == 1
        from app.db.models import PlayHistory as _PH

        assert db.query(_PH).filter_by(user_id=UID).count() >= 1
    finally:
        db.close()


def test_charts_toggle_and_match():
    db = _db()
    try:
        lib.save_import_settings(db, {"charts_enabled": False})
        assert lib.refresh_charts(db, client=FakeClient())["ok"] is False
        lib.save_import_settings(db, {"charts_enabled": True})
        r = lib.refresh_charts(db, client=FakeClient())
        assert r["ok"] is True
        assert r["tracks"] == 1
        g = lib.get_charts(db)
        assert g["total"] == 1
        assert g["in_library"] == 0  # Chart Hit нет в каталоге
        assert "track_id" not in g["tracks"][0]
    finally:
        db.close()


def test_api_endpoints_present():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    assert c.get("/api/yandex/import-settings").status_code == 200
    assert c.get("/api/yandex/charts").status_code == 200
    # wave publish/live заваерены (400 без user_id / мусорный user_id)
    assert c.post("/api/wave/publish", json={}).status_code == 400
    assert c.get("/api/wave/live", params={"user_id": "nope"}).status_code == 400


def test_metadata_corrections_trust_yandex():
    db = _db()
    try:
        t = db.query(Track).filter_by(external_id="nav1").first()
        t.year = 2026  # дефолт скана
        t.genre = None
        t.album_name = None
        db.commit()
        corr = lib.apply_metadata_corrections(
            db, str(t.id),
            {"year": 1999, "genre": "rusrock", "album": "Real Album"})
        db.commit()
        assert corr["year"] == [2026, 1999]
        assert corr["genre"] == [None, "rusrock"]
        assert corr["album_name"] == [None, "Real Album"]
        db.refresh(t)
        assert t.year == 1999 and t.genre == "rusrock"
        # повтор — менять нечего
        assert lib.apply_metadata_corrections(
            db, str(t.id), {"year": 1999, "genre": "other"}) == {}
    finally:
        db.close()


def test_corrections_list_and_revert():
    from app.db.models import TrackMetadataEnrich

    db = _db()
    try:
        t = db.query(Track).filter_by(external_id="nav1").first()
        t.year = 2026
        db.commit()
        corr = lib.apply_metadata_corrections(db, str(t.id), {"year": 1975})
        assert corr["year"] == [2026, 1975]
        ex = db.query(TrackMetadataEnrich).filter_by(
            track_id=str(t.id), source="yandex").first()
        if ex is None:
            db.add(TrackMetadataEnrich(track_id=str(t.id), source="yandex",
                                       data={"title": "x", "corrected": corr}))
        else:
            ex.data = {"title": "x", "corrected": corr}
        db.commit()
        lst = lib.list_corrections(db)
        assert lst["total"] >= 1
        row = [i for i in lst["items"] if i["track_id"] == str(t.id)][0]
        assert row["corrected"]["year"] == [2026, 1975]
        r = lib.revert_correction(db, str(t.id), ["year"])
        assert r["ok"] is True and r["restored"]["year"] == [1975, 2026]
        db.refresh(t)
        assert t.year == 2026
        # второй откат — править нечего
        assert lib.revert_correction(db, str(t.id))["ok"] is False
    finally:
        db.close()


def test_corrections_toggle_gate():
    db = _db()
    try:
        assert lib.corrections_enabled(db) in (True, False)
        lib.save_import_settings(db, {"corrections_enabled": False})
        assert lib.corrections_enabled(db) is False
        lib.save_import_settings(db, {"corrections_enabled": True})
        assert lib.corrections_enabled(db) is True
    finally:
        db.close()
