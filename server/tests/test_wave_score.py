"""Скор волны: кламп, ранг и то, что попадает в БД.

Два бага, оба про одно и то же — «модель говорит, а в базу и в порядок
попадает не то»:

  * КЛАМП 0..1 был и рангом: сумма потолков (~2.6) упирает почти всю пачку
    в 1.0, и треки с разным качеством шли в порядке обхода пула. Теперь
    ранжирование идёт по сырому score_raw, а 'score' (0..1) — только для
    показа.
  * В БД (RecommendationFeedback) писался растянутый min-max по окну, а не
    скор модели: цикл обучения получал неверные числа.

Плюс: пустой профиль при синке больше не стирает снапшот вкуса.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

import pytest as _pytest

from app.db.database import SessionLocal, init_db
from app.db.models import (
    MediaServer,
    MediaUser,
    RecommendationFeedback,
    TasteProfile,
    Track,
    TrackFeatures,
)
from app.services import rec_feedback as _rfb
from app.services import taste as _taste
from app.services import wave as _wave


# ---------------- сьют ----------------

def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="score_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="score_tester", username="score_tester")
        db.add(u)
    for i in range(6):
        ext = "sc_%d" % i
        t = db.query(Track).filter_by(external_id=ext).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                      title=ext, artist_name="sc_artist_%d" % i,
                      genre="genre_%d" % i)
            db.add(t)
            db.commit()
        if db.query(TrackFeatures).filter_by(track_id=t.id).first() is None:
            db.add(TrackFeatures(track_id=t.id, energy=0.3 + 0.1 * i,
                                 valence=0.5, arousal=0.4, danceability=0.5,
                                 tempo_bpm=100.0 + 10 * i))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _cands(db):
    return [str(r.id) for r in
            db.query(Track).filter(Track.external_id.like("sc_%")).all()]


# ---------------- скор ----------------

def test_score_is_clamped_but_raw_is_kept():
    """score — 0..1 для показа, score_raw — то, что выдала модель."""
    db = SessionLocal()
    try:
        ranked = _wave.score_candidates(db, UID, _cands(db), [],
                                         settings={}, jitter_seed="t1")
        assert ranked
        for r in ranked:
            assert 0.0 <= r["score"] <= 1.0
            assert "score_raw" in r
            if r["score_raw"] > 1.0:
                assert r["score"] == 1.0
    finally:
        db.close()


def test_ranking_uses_raw_score():
    """Порядок — по сырому скору, даже если клампнутый у всех одинаковый."""
    db = SessionLocal()
    try:
        ranked = _wave.score_candidates(db, UID, _cands(db), [],
                                         settings={}, jitter_seed="t2")
        assert ranked
        raws = [r["score_raw"] for r in ranked]
        assert raws == sorted(raws, reverse=True), raws
    finally:
        db.close()


def test_spread_scores_uses_raw_and_keeps_it():
    """Растяжка для показа считается по сырому скору и его не портит."""
    items = [{"track_id": "a", "score": 1.0, "score_raw": 2.4},
             {"track_id": "b", "score": 1.0, "score_raw": 1.2},
             {"track_id": "c", "score": 1.0, "score_raw": 0.4}]
    out = _wave.spread_scores(items)
    # все клампнутые в 1.0 — по ним растяжка не работает, по сырым работает
    # (min-max по [0.4, 1.2, 2.4] -> [0.0, 0.4, 1.0])
    assert [i["score"] for i in out] == [1.0, 0.4, 0.0]
    assert [i["score_raw"] for i in out] == [2.4, 1.2, 0.4]


def test_mark_served_stores_raw_score():
    """В БД пишется скор модели, а не растянутый min-max."""
    db = SessionLocal()
    try:
        db.query(RecommendationFeedback).filter_by(user_id=UID).delete()
        db.commit()
        tids = _cands(db)[:2]
        rows = [{"track_id": tids[0], "score": 1.0, "score_raw": 2.31,
                 "reason": "r"},
                {"track_id": tids[1], "score": 0.0, "score_raw": 0.42,
                 "reason": "r"}]
        assert _rfb.mark_served(db, UID, rows, source="wave") == 2
        got = {str(r.track_id): r.score for r in
               db.query(RecommendationFeedback).filter_by(user_id=UID).all()}
        assert got.get(tids[0]) == 2.31, got
        assert got.get(tids[1]) == 0.42, got
    finally:
        db.query(RecommendationFeedback).filter_by(user_id=UID).delete()
        db.commit()
        db.close()


# ---------------- якорь свежего лайка / раннего скипа ----------------

def _anchor_tracks(db):
    """L — лайкнутый; A — тот же артист; G — тот же жанр; N — нейтральный."""
    srv = db.query(MediaServer).first()

    def mk(ext, artist, genre):
        t = db.query(Track).filter_by(external_id=ext).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                      title=ext, artist_name=artist, genre=genre)
            db.add(t)
            db.flush()
        return str(t.id)

    L = mk("anchor_liked", "Anchor Band", "anchor rock")
    A = mk("anchor_same_artist", "Anchor Band", "other stuff")
    G = mk("anchor_same_genre", "Someone Else", "anchor rock")
    N = mk("anchor_neutral", "Nobody", "elevator")
    return L, A, G, N


def _raws(db, cands, ev=None, seed="anchor"):
    ranked = _wave.score_candidates(db, UID, cands, [], settings={},
                                    recent_events=ev or [], jitter_seed=seed)
    return {r["track_id"]: r for r in ranked}


def test_like_event_anchors_artist_and_genre():
    """Лайк в свежих событиях: +0.15 тому же артисту, +0.10 тому же жанру."""
    db = SessionLocal()
    try:
        L, A, G, N = _anchor_tracks(db)
        cands = [A, G, N]
        base = _raws(db, cands)
        nxt = _raws(db, cands, ev=[{"track_id": L, "action": "like",
                                    "position_sec": 200}])
        assert nxt[A]["score_raw"] - base[A]["score_raw"] == _pytest.approx(0.15)
        assert nxt[G]["score_raw"] - base[G]["score_raw"] == _pytest.approx(0.10)
        assert nxt[N]["score_raw"] - base[N]["score_raw"] == _pytest.approx(0.0)
        assert nxt[A]["comp"]["anchor"] == _pytest.approx(0.15)
        assert nxt[A]["reason"] == "Похоже на ваш лайк: Anchor Band"
    finally:
        db.close()


def test_fresh_favorite_anchors_next_pack():
    """Лайк через ratings_delta (мобила) оседает в Favorite — якорь держится
    и в следующих запросах без событий (окно like_anchor_hours)."""
    from app.db.models import Favorite as _Fav

    db = SessionLocal()
    try:
        L, A, G, N = _anchor_tracks(db)
        cands = [A, G, N]
        base = _raws(db, cands, seed="anchor2")
        db.add(_Fav(user_id=UID, track_id=L))
        db.flush()
        nxt = _raws(db, cands, seed="anchor2")
        assert nxt[A]["score_raw"] - base[A]["score_raw"] == _pytest.approx(0.15)
        assert nxt[G]["score_raw"] - base[G]["score_raw"] == _pytest.approx(0.10)
        assert nxt[N]["score_raw"] - base[N]["score_raw"] == _pytest.approx(0.0)
    finally:
        db.close()


def test_vibe_anchor_pulls_similar_sound_not_same_artist():
    """Якорь тянет похожее по вайбу (тот же муд-вектор), а не дубли артиста:
    другой артист + другой жанр, но тот же звук — вверх."""
    db = SessionLocal()
    try:
        L, _, _, N = _anchor_tracks(db)
        srv = db.query(MediaServer).first()
        v = db.query(Track).filter_by(external_id="anchor_vibe").first()
        if v is None:
            v = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="anchor_vibe", title="anchor_vibe",
                      artist_name="Vibe Other", genre="vibe pop")
            db.add(v)
            db.flush()
        V = str(v.id)
        for tid in (L, V):
            if db.query(TrackFeatures).filter_by(track_id=tid).first() is None:
                db.add(TrackFeatures(track_id=tid, energy=0.9, valence=0.5,
                                     arousal=0.8, danceability=0.7,
                                     tempo_bpm=140.0))
        db.flush()
        cands = [V, N]
        base = _raws(db, cands, seed="anchor4")
        nxt = _raws(db, cands, ev=[{"track_id": L, "action": "like",
                                    "position_sec": 200}], seed="anchor4")
        # вайб 1.0 * 0.2; артист/жанр у V другие — только звук
        assert nxt[V]["score_raw"] - base[V]["score_raw"] == _pytest.approx(0.20)
        assert nxt[N]["score_raw"] - base[N]["score_raw"] == _pytest.approx(0.0)
        assert nxt[V]["comp"]["anchor"] == _pytest.approx(0.20)
    finally:
        db.close()


def test_early_skip_penalizes_genre_immediately():
    """Ранний скип (<30с): −0.10 тому же жанру сразу, не дожидаясь 3 скипов."""
    db = SessionLocal()
    try:
        L, A, G, N = _anchor_tracks(db)
        cands = [G, N]
        base = _raws(db, cands, seed="anchor3")
        nxt = _raws(db, cands, ev=[{"track_id": L, "action": "skip",
                                    "position_sec": 5}], seed="anchor3")
        # L жанра "anchor rock" — скип бьёт по G того же жанра, N не трогает
        assert nxt[G]["score_raw"] - base[G]["score_raw"] == _pytest.approx(-0.10)
        assert nxt[N]["score_raw"] - base[N]["score_raw"] == _pytest.approx(0.0)
    finally:
        db.close()


# ---------------- снапшот вкуса ----------------

def _profile(db):
    return db.get(TasteProfile, UID)


def test_sync_with_empty_profile_keeps_snapshot():
    """apply_delta зовёт sync с профилем {} — снапшот вкуса должен уцелеть.

    Раньше отсутствующие ключи профиля считались «пустыми» и затирали
    мобильный слепок (жанры/артисты/лайки/баны/счётчики) на каждой волне
    с дельтой оценок.
    """
    db = SessionLocal()
    try:
        _taste.sync_from_mobile(db, UID, {
            "source": "mobile",
            "ratings": [],
            "profile": {
                "preferredGenres": {"rock": 5},
                "preferredArtists": {"Artist": 3},
                "likedSongs": ["x1", "x2"],
                "dislikedSongs": ["y1"],
                "bannedArtists": ["Bad"],
                "artistDislikeCounts": {"Bad": 2},
            },
            "events": [],
        })
        db.commit()
        p = _profile(db)
        assert p is not None
        assert p.mobile_genres == {"rock": 5}
        assert p.mobile_liked == ["x1", "x2"]
        assert p.mobile_banned == ["Bad"]
        assert p.mobile_counts == {"Bad": 2}

        # пустой профиль (apply_delta) — снапшот не трогаем
        _taste.sync_from_mobile(db, UID, {
            "source": "desktop", "ratings": [], "profile": {}, "events": [],
        })
        db.commit()
        p = _profile(db)
        assert p.mobile_genres == {"rock": 5}, p.mobile_genres
        assert p.mobile_artists == {"Artist": 3}, p.mobile_artists
        assert p.mobile_liked == ["x1", "x2"], p.mobile_liked
        assert p.mobile_disliked == ["y1"], p.mobile_disliked
        assert p.mobile_banned == ["Bad"], p.mobile_banned
        assert p.mobile_counts == {"Bad": 2}, p.mobile_counts
    finally:
        db.close()


def test_sync_with_explicit_empty_clears_snapshot():
    """«Прислал пустым» = очистить (в отличие от «не прислал»)."""
    db = SessionLocal()
    try:
        _taste.sync_from_mobile(db, UID, {
            "source": "mobile",
            "ratings": [],
            "profile": {"preferredGenres": {"rock": 5},
                        "likedSongs": ["x1"]},
            "events": [],
        })
        db.commit()
        _taste.sync_from_mobile(db, UID, {
            "source": "mobile",
            "ratings": [],
            "profile": {"preferredGenres": {}, "likedSongs": []},
            "events": [],
        })
        db.commit()
        p = _profile(db)
        assert p.mobile_genres == {}, p.mobile_genres
        assert p.mobile_liked == [], p.mobile_liked
    finally:
        db.close()
