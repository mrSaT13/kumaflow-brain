"""Мост как АДДИТИВНЫЙ источник метаданных: кэш в БД + подмес без поломки базовой логики."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import models
from app.db.database import SessionLocal
from app.main import app
from app.services import bridge as bridge_svc
from app.services import ml as ml_svc
from app.services import seed_radio

client = TestClient(app)


def _server(db):
    srv = db.query(models.MediaServer).first()
    if srv is None:
        srv = models.MediaServer(type="navidrome", name="bridge-t", url="http://x")
        db.add(srv)
        db.flush()
    return srv


def _enable_bridge(db):
    row = db.get(models.AppSetting, "bridge")
    val = {"url": "http://bridge.test", "enabled": True}
    if row is None:
        db.add(models.AppSetting(key="bridge", value=val))
    else:
        row.value = val
    db.commit()


def _track(db, srv, artist, title, genre=None):
    t = models.Track(
        server_id=srv.id,
        external_id="bm-" + uuid.uuid4().hex[:12],
        title=title,
        artist_name=artist,
        genre=genre,
    )
    db.add(t)
    db.flush()
    return t


def _meta(db, artist, tags=None, similar=None):
    row = models.ArtistMetadata(
        name_key=bridge_svc.artist_key(artist),
        name=artist,
        tags=tags or [],
        similar_artists=similar or [],
        bio_summary="bio from bridge",
    )
    db.add(row)
    db.commit()
    return row


def test_enrich_caches_bridge_response_and_never_calls_twice(monkeypatch):
    db = SessionLocal()
    _enable_bridge(db)
    calls = []

    def _fake(url, name):
        calls.append(name)
        return {
            "name": "Radiohead", "mbid": "mbid-1", "country": "GB", "type": "Group",
            "tags": ["alternative rock", "electronic"],
            "similar_artists": [{"name": "Muse", "url": ""}],
            "bio_summary": "bio", "lastfm_url": "https://lfm/radiohead",
            "listeners": "8531521", "playcount": "1438344660",
            "image_url": "https://img/radiohead.png",
            "mood": {"valence": -0.7, "energy": 0.2, "mood": "negative/moderate",
                     "mood_tags": ["dark"]},
        }

    monkeypatch.setattr(bridge_svc, "_fetch_sync", _fake)
    first = bridge_svc.enrich_artist(db, "radiohead")
    second = bridge_svc.enrich_artist(db, "RADIOHEAD ")
    assert calls == ["radiohead"], "второй вызов должен попасть в кэш БД"
    assert first["tags"] == ["alternative rock", "electronic"]
    assert second["name"] == "Radiohead"
    assert second["listeners"] == 8531521 and second["playcount"] == 1438344660
    assert second["image_url"] == "https://img/radiohead.png"
    assert second["mood"]["valence"] == -0.7 and second["mood"]["label"] == "negative/moderate"
    assert db.query(models.ArtistMetadata).count() == 1


def test_enrich_is_noop_when_bridge_disabled(monkeypatch):
    db = SessionLocal()

    def _boom(url, name):
        raise AssertionError("внешний вызов при выключенном мосте")

    monkeypatch.setattr(bridge_svc, "_fetch_sync", _boom)
    assert bridge_svc.enrich_artist(db, "Anyone") is None
    assert db.query(models.ArtistMetadata).count() == 0


def test_bridge_similar_is_additive_and_local_only():
    db = SessionLocal()
    srv = _server(db)
    _meta(db, "Radiohead", similar=["Muse", "Ghost", "No Local Band"])
    _track(db, srv, "Muse", "m1")
    out = seed_radio._bridge_similar(db, "Radiohead", {"radiohead"}, 5)
    assert out == ["Muse"], "инородные/отсутствующие в библиотеке имена не должны попадать"


def test_cold_start_bridge_tags_add_small_weight_only_to_existing_genres():
    rows = [
        SimpleNamespace(id=str(uuid.uuid4()), artist_name="Radiohead", genre="Alternative Rock",
                        play_count=10, starred=False, rating=None, last_played_at=None),
        SimpleNamespace(id=str(uuid.uuid4()), artist_name="Muse", genre="Rock",
                        play_count=5, starred=False, rating=None, last_played_at=None),
    ]
    base = ml_svc._step1_signal(rows, {})
    with_tags = ml_svc._step1_signal(rows, {}, bridge_tags={"radiohead": ["alternative rock"]})
    key = "genre::Alternative Rock"
    assert with_tags[key] > base[key], "тег моста должен добавить вес существующему жанру"
    assert set(with_tags) == set(base), "нельзя создавать новые ключи сигнала"
    assert all(not k.startswith("bridge") for k in with_tags)


def test_track_endpoint_exposes_bridge_metadata_with_source_and_date():
    db = SessionLocal()
    srv = _server(db)
    t = _track(db, srv, "Radiohead", "Creep")
    _meta(db, "Radiohead", tags=["alternative rock"])
    r = client.get(f"/api/tracks/{t.id}")
    assert r.status_code == 200
    m = r.json()["artist_metadata"]
    assert m is not None and m["source"] == "bridge"
    assert m["tags"] == ["alternative rock"]
    assert m["synced_at"]

    r2 = client.get(f"/api/bridge/artist-meta?name=radiohead")
    assert r2.status_code == 200 and r2.json()["ok"] is True


def test_mood_tags_exclude_genres_and_unknown_words():
    # electronic/acoustic — жанры, а не настроения: в пилюли не идут и в UI
    # под «Настроениями» не показываются.
    codes = bridge_svc.mood_codes(
        ["melancholic", "electronic", "acoustic", "party", "dark", "неизвестный тег"])
    assert codes == ["melancholic", "dark"]
    assert bridge_svc.clean_mood_tags(["electronic", "dark", "sad"]) == ["dark", "sad"]


def test_track_meta_is_cached_and_used_as_mood_fallback(monkeypatch):
    db = SessionLocal()
    srv = _server(db)
    t = _track(db, srv, "Radiohead", "Creep")
    _enable_bridge(db)
    calls = []

    def _fake(url, endpoint, **params):
        calls.append((endpoint, params))
        return {"name": "Creep", "artist": "Radiohead", "mbid": "", "tags": ["alternative rock"],
                "mood_tags": ["melancholic", "unknown junk"], "similar": [], "summary": "", "url": ""}

    monkeypatch.setattr(bridge_svc, "_fetch_entity_sync", _fake)
    first = bridge_svc.enrich_track(db, t)
    bridge_svc.enrich_track(db, t)
    assert len(calls) == 1, "второй вызов должен попасть в кэш"
    assert first["mood_codes"] == ["melancholic"]
    m = client.get(f"/api/tracks/{t.id}").json()["metadata"]["bridge_track"]
    assert m["source"] == "bridge_track" and m["synced_at"]

    from app.services import wave as wave_svc

    codes = set(bridge_svc.bridge_mood_map(db, [str(t.id)]).get(str(t.id), []))
    assert codes == {"melancholic"}
    # Жанр 'rock' даёт 'aggressive' и не пускает в семейство 'sad'; тег моста
    # добавляет проход, но никогда не убирает то, что проходило раньше.
    want = set(wave_svc.MOOD_PRESETS["sad"]["moods"])
    assert wave_svc.mood_filter_passes(None, "rock", want) is False
    assert wave_svc.mood_filter_passes(None, "rock", want, bridge_codes=codes) is True


def test_album_meta_uses_existing_basket_without_new_table(monkeypatch):
    db = SessionLocal()
    srv = _server(db)
    t = _track(db, srv, "Radiohead", "Creep")
    a = models.Album(server_id=srv.id, external_id="alb-1", title="OK Computer",
                     artist_name="Radiohead")
    db.add(a)
    db.commit()
    _enable_bridge(db)

    def _fake(url, endpoint, **params):
        assert endpoint == "album"
        return {"name": "OK Computer", "artist": "Radiohead", "mbid": "mbid-alb",
                "tags": ["alternative rock"], "mood_tags": ["melancholic"],
                "cover_url": "https://coverartarchive.org/release-group/mbid-alb/front-500",
                "summary": "", "url": ""}

    monkeypatch.setattr(bridge_svc, "_fetch_entity_sync", _fake)
    data = bridge_svc.enrich_album(db, a, str(t.id))
    assert data["mbid"] == "mbid-alb" and data["source"] == "bridge_album"
    assert data["cover_url"].endswith("front-500")
    stored = client.get(f"/api/tracks/{t.id}").json()["metadata"]["bridge_album"]
    assert stored["title"] == "OK Computer"


def test_artist_page_endpoint_combines_library_and_bridge():
    db = SessionLocal()
    srv = _server(db)
    t = _track(db, srv, "Radiohead", "Creep", genre="Alternative Rock")
    t.play_count = 42
    a = models.Album(server_id=srv.id, external_id="alb-artist-page", title="OK Computer",
                     artist_name="Radiohead", year=1997)
    db.add(a)
    db.commit()
    row = _meta(db, "Radiohead", tags=["alternative rock"], similar=["Muse"])
    row.mood_tags = ["electronic", "dark"]
    db.commit()

    r = client.get("/api/library/artist?name=radiohead")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["name"] == "Radiohead"
    assert body["in_library"] is True and body["track_count"] == 1
    assert body["stats"]["plays"] == 42
    assert body["tracks"][0]["track_id"] == str(t.id)
    assert body["albums"][0]["title"] == "OK Computer"
    assert body["bridge"]["tags"] == ["alternative rock"]
    assert body["bridge"]["mood_tags"] == ["dark"], "жанр electronic не должен выдаваться за настроение"
    assert any(s["name"] == "Muse" and s["from_bridge"] is True for s in body["similar"])


def test_country_filter_uses_bridge_metadata():
    db = SessionLocal()
    srv = _server(db)
    t = _track(db, srv, "Кино", "Группа крови")
    row = models.ArtistMetadata(name_key=bridge_svc.artist_key("Кино"), name="Кино", country="RU")
    db.add(row)
    db.commit()

    r = client.get("/api/tracks?country=RU")
    assert r.status_code == 200 and r.json()["total"] >= 1
    assert any(str(i["id"]) == str(t.id) for i in r.json()["items"])
    assert client.get("/api/tracks?country=US").json()["total"] == 0
    assert any(c["code"] == "RU" for c in client.get("/api/library/countries").json()["countries"])

    from app.services import wave_settings

    saved = wave_settings.save_settings(db, "country-test", {"country": "RU"})
    assert "country" in wave_settings.effective_settings(db, "country-test", None)


def test_chart_endpoints_proxy_bridge(monkeypatch):
    db = SessionLocal()
    _enable_bridge(db)
    row = db.get(models.AppSetting, "bridge")
    row.value = {"url": "http://bridge.test", "enabled": True, "lastfm_user": "sasha"}
    db.commit()

    seen = []

    def _fake(url, endpoint, **params):
        seen.append((endpoint, params))
        if endpoint == "geo/artists":
            return {"country": "Russian Federation", "artists": [{"name": "Кино"}]}
        return {"artists": [{"name": "Radiohead", "playcount": "100"}]}

    monkeypatch.setattr(bridge_svc, "_fetch_entity_sync", _fake)
    r1 = client.get("/api/bridge/chart-artists?limit=20")
    r2 = client.get("/api/bridge/geo-artists?country=Russia")
    r3 = client.get("/api/bridge/top-artists?period=1month")
    assert r1.json()["artists"][0]["name"] == "Radiohead"
    assert r2.json()["country"] == "Russian Federation"
    assert r3.json()["user"] == "sasha" and r3.json()["period"] == "1month"
    assert seen[0][0] == "chart/artists" and seen[1][0] == "geo/artists"
    assert seen[2][1]["user"] == "sasha"
