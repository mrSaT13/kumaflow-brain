"""Слепок вкуса по источникам: мобил/десктоп/яндекс не затирают друг друга.

Баг, который чинится: слепок вкуса был ОДИН на всех клиентов. Кто последним
прислал профиль — тот и записал, а предыдущего стирал. Яндекс-импорт шёл
как «mobile» (не передавал source) и с пустым preferredGenres стирал
мобильные жанры, а со своим preferredArtists — мобильных артистов.

Теперь у каждого источника своя корзина (`TasteProfile.sync_profiles`), а
legacy-поля mobile_* — СЛИЯНИЕ всех корзин (их читает скоринг).
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, TasteProfile
from app.services import taste as _taste


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="src_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="src_tester", username="src_tester")
        db.add(u)
        db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _sync(source, profile):
    db = SessionLocal()
    try:
        _taste.sync_from_mobile(db, UID, {
            "source": source, "ratings": [], "profile": profile, "events": [],
        })
        db.commit()
        tp = db.get(TasteProfile, UID)
        return (dict(tp.mobile_genres or {}), dict(tp.mobile_artists or {}),
                list(tp.mobile_liked or []), list(tp.mobile_disliked or []),
                list(tp.mobile_banned or []), dict(tp.mobile_counts or {}),
                dict(getattr(tp, "sync_profiles", None) or {}))
    finally:
        db.close()


def test_sources_do_not_erase_each_other():
    _sync("mobile", {"preferredGenres": {"rock": 5},
                      "preferredArtists": {"MobileArtist": 3},
                      "likedSongs": ["m1"],
                      "dislikedSongs": ["m2"],
                      "bannedArtists": ["BadArtist"],
                      "artistDislikeCounts": {"BadArtist": 2}})
    # Яндекс присылает ВСЕ шесть ключей, причём жанры у него пустые.
    _sync("yandex", {"preferredGenres": {},
                      "preferredArtists": {"YandexArtist": 7},
                      "likedSongs": ["y1"],
                      "dislikedSongs": ["y2"],
                      "bannedArtists": [],
                      "artistDislikeCounts": {}})
    genres, artists, liked, disliked, banned, counts, profs = _sync(
        "yandex", {})
    # мобильный вкус уцелел
    assert genres.get("rock") == 5, genres
    assert artists.get("MobileArtist") == 3, artists
    assert "m1" in liked and "y1" in liked, liked
    assert "m2" in disliked and "y2" in disliked, disliked
    assert "BadArtist" in banned, banned
    assert counts.get("BadArtist") == 2, counts
    # и яндекс не потерялся
    assert artists.get("YandexArtist") == 7, artists
    # корзины раздельные
    assert "mobile" in profs and "yandex" in profs, list(profs)
    assert profs["mobile"]["genres"] == {"rock": 5}


def test_same_source_can_clear_its_own_bucket():
    _sync("mobile", {"preferredGenres": {"rock": 5}})
    _sync("yandex", {"preferredGenres": {"pop": 2}})
    genres, _a, _l, _d, _b, _c, _p = _sync("mobile", {"preferredGenres": {}})
    # мобила очистила своё — яндекс остался
    assert genres == {"pop": 2}, genres


def test_missing_key_does_not_clear():
    _sync("mobile", {"preferredGenres": {"rock": 5}, "likedSongs": ["m1"]})
    # прислали только артистов — жанры и лайки на месте
    genres, artists, liked, _d, _b, _c, _p = _sync(
        "mobile", {"preferredArtists": {"A": 1}})
    assert genres == {"rock": 5}, genres
    assert liked == ["m1"], liked
    assert artists.get("A") == 1


def test_legacy_snapshot_survives_first_split_sync():
    """Данные, накопленные до разделения корзин, не теряются."""
    db = SessionLocal()
    try:
        tp = db.get(TasteProfile, UID)
        if tp is None:
            tp = TasteProfile(user_id=UID)
            db.add(tp)
        # emulate старого состояния: слепок есть, разбивки нет
        tp.sync_profiles = {}
        tp.mobile_genres = {"legacy": 9}
        tp.mobile_artists = {"LegacyArtist": 4}
        tp.mobile_liked = ["legacy1"]
        tp.mobile_sync_source = "mobile"
        db.commit()
    finally:
        db.close()
    genres, artists, liked, _d, _b, _c, _p = _sync(
        "yandex", {"preferredArtists": {"Y": 1}})
    assert genres.get("legacy") == 9, genres
    assert artists.get("LegacyArtist") == 4, artists
    assert "legacy1" in liked, liked


def test_counts_are_summed():
    _sync("mobile", {"artistDislikeCounts": {"A": 1}})
    _counts = _sync("yandex", {"artistDislikeCounts": {"A": 2}})[5]
    assert _counts.get("A") == 3, _counts
