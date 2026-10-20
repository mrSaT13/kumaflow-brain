"""Идеальные рекомендации, пачка 1: канон жанров, эпоха/дипы, тренд, авто-веса.

Без сети, sqlite. Чужие треки — со своим префиксом, оверлеи тюнинга
подметаем (иначе тесты зависят от порядка модулей).
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid
from datetime import datetime, timedelta

from app.db.database import SessionLocal, init_db
from app.db.models import (
    AppSetting,
    Favorite,
    MediaServer,
    MediaUser,
    PlayEvent,
    PlayHistory,
    RecommendationFeedback,
    Track,
    TrackFeatures,
)
from app.services import auto_tune as _at
from app.services import mood_profile as _mprof
from app.services import rec_feedback as _rfb
from app.services import rec_tuning as _rt
from app.services import taste as _taste
from app.services import wave as _wave
from app.services.genre_canon import backfill_genres, canon_genre


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="ideal_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="ideal_tester", username="ideal_tester")
        db.add(u)
        db.commit()
    specs = [
        # (external_id, artist, genre, year, play_count)
        ("id_fav_20s", "id_artist", "nu metal", 2024, 10),
        ("id_cand_20s", "id_artist", "nu metal", 2022, 5),
        ("id_cand_90s", "id_artist", "nu metal", 1994, 5),
        ("id_hit", "id_artist", "nu metal", 2005, 1000),
        ("id_deep", "id_artist", "nu metal", 2005, 0),
        ("id_raw_numetal", "id_artist2", "numetal", 2001, 1),
        ("id_raw_hiphop", "id_artist2", "Hip-Hop", 2001, 1),
    ]
    for ext, artist, genre, year, pc in specs:
        t = db.query(Track).filter_by(external_id=ext).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=ext, title=ext, artist_name=artist,
                      genre=genre, year=year, play_count=pc)
            db.add(t)
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _tid(ext):
    db = SessionLocal()
    try:
        return str(db.query(Track).filter_by(external_id=ext).first().id)
    finally:
        db.close()


def _wipe_user():
    db = SessionLocal()
    try:
        db.query(Favorite).filter_by(user_id=UID).delete()
        db.query(RecommendationFeedback).filter_by(user_id=UID).delete()
        db.query(AppSetting).filter(
            AppSetting.key.in_([_rt._user_key(UID), _at.LOG_PREFIX + UID])).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    _rt.invalidate_cache(None)


# ---------------- канон жанров ----------------

def test_canon_aliases():
    assert canon_genre("numetal") == "nu metal"
    assert canon_genre("Nu Metal") == "nu metal"
    assert canon_genre("nu-metal") == "nu metal"
    assert canon_genre("Hip-Hop") == "hip-hop"
    assert canon_genre("hiphop") == "hip-hop"
    assert canon_genre("  Alternative   METAL ") == "alternative metal"
    assert canon_genre(None) is None
    assert canon_genre("   ") is None
    # неизвестное — чистим, а не выкидываем
    assert canon_genre("  EBM  ") == "ebm"


def test_backfill_rewrites_old_rows():
    # Стартовый backfill в init_db уже мог всё Zachary — возвращаем сырьё
    # руками и проверяем именно путь backfill_genres.
    db = SessionLocal()
    try:
        db.query(Track).filter(
            Track.external_id == "id_raw_numetal").update({"genre": "numetal"})
        db.query(Track).filter(
            Track.external_id == "id_raw_hiphop").update({"genre": "Hip-Hop"})
        db.commit()
        n = backfill_genres(db)
        assert n >= 2, n
        got = {str(r.external_id): r.genre for r in
               db.query(Track).filter(
                   Track.external_id.in_(["id_raw_numetal", "id_raw_hiphop"])).all()}
        assert got["id_raw_numetal"] == "nu metal", got
        assert got["id_raw_hiphop"] == "hip-hop", got
        # идемпотентность: второй прогон ничего не правит
        assert backfill_genres(db) == 0
    finally:
        db.close()


# ---------------- эпоха в профиле ----------------

def test_decades_in_profile():
    _wipe_user()
    db = SessionLocal()
    try:
        db.add(Favorite(user_id=UID, track_id=_tid("id_fav_20s")))
        db.commit()
        prof = _taste.user_profile(db, UID, top_n=0)
        assert prof["preferredDecades"].get("2020s", 0) > 0, prof["preferredDecades"]
        assert "1990s" not in prof["preferredDecades"]
        assert any(d["name"] == "2020s" for d in prof["decades"])
    finally:
        db.close()
        _wipe_user()


# ---------------- эпоха и дипы в скоринге ----------------

def test_era_bonus_prefers_loved_decade():
    # Вклад эпохи меряем дельтой: штрафы повторов бьют по второму треку
    # пары (±0.08) и забивают сам бонус (0.005) — порядок тут не доказательство.
    # А вот прирост score_raw при era_weight 0 → 1 обязан быть ровно
    # decade_s × 1.0 у трека любимой декады и 0 у чужой.
    _wipe_user()
    db = SessionLocal()
    try:
        db.add(Favorite(user_id=UID, track_id=_tid("id_fav_20s")))
        db.commit()
        cands = [_tid("id_cand_90s"), _tid("id_cand_20s")]
        _rt.set_flags({"character": {"jitter": 0.0, "era_weight": 0.0}}, db, UID)
        base = {r["track_id"]: r["score_raw"] for r in
                _wave.score_candidates(db, UID, cands, [], settings={},
                                       jitter_seed="era1")}
        _rt.set_flags({"character": {"era_weight": 1.0}}, db, UID)
        full = {r["track_id"]: r for r in
                _wave.score_candidates(db, UID, cands, [], settings={},
                                       jitter_seed="era1")}
        t20, t90 = _tid("id_cand_20s"), _tid("id_cand_90s")
        assert "era" in full[t20]["comp"]
        assert abs((full[t20]["score_raw"] - base[t20]) - 0.1) < 1e-6, (
            full[t20]["score_raw"], base[t20])
        assert abs(full[t90]["score_raw"] - base[t90]) < 1e-6
    finally:
        db.close()
        _wipe_user()


def test_depth_flips_hits_vs_deep():
    _wipe_user()
    db = SessionLocal()
    try:
        cands = [_tid("id_deep"), _tid("id_hit")]
        _rt.set_flags({"character": {"jitter": 0.0, "depth": 1.0}}, db, UID)
        ranked = _wave.score_candidates(db, UID, cands, [], settings={},
                                        jitter_seed="depth1")
        assert ranked[0]["track_id"] == _tid("id_hit"), [r["track_id"] for r in ranked]
        _rt.set_flags({"character": {"depth": -1.0}}, db, UID)
        ranked = _wave.score_candidates(db, UID, cands, [], settings={},
                                        jitter_seed="depth1")
        assert ranked[0]["track_id"] == _tid("id_deep"), [r["track_id"] for r in ranked]
    finally:
        db.close()
        _wipe_user()


# ---------------- тренд в сводке ----------------

def _fb_at(uid, tid, outcome, served_days_ago):
    db = SessionLocal()
    try:
        db.add(RecommendationFeedback(
            user_id=uid, track_id=tid, source="wave", reason="t",
            outcome=outcome,
            served_at=datetime.utcnow() - timedelta(days=served_days_ago),
            decided_at=datetime.utcnow() - timedelta(days=served_days_ago)))
        db.commit()
    finally:
        db.close()


def test_summary_trend_current_vs_previous():
    _wipe_user()
    tid = _tid("id_hit")
    # прошлое окно (8–14 дней): всё хорошо; текущее (0–7): всё скипают
    for _ in range(10):
        _fb_at(UID, tid, "completed", 10)
    for _ in range(10):
        _fb_at(UID, tid, "early_skip", 2)
    db = SessionLocal()
    try:
        s = _rfb.summary(db, user_id=UID, days=7)
        assert s["trend"] is not None, s.keys()
        # скипов стало больше (плюс), дослушиваний меньше (минус)
        assert s["trend"]["early_skip_rate"] > 0, s["trend"]
        assert s["trend"]["completion_rate"] < 0, s["trend"]
        assert "wave" in s["trend"]["by_source"]
    finally:
        db.close()
        _wipe_user()


def test_summary_trend_none_without_history():
    _wipe_user()
    db = SessionLocal()
    try:
        s = _rfb.summary(db, user_id=UID, days=7)
        assert s["trend"] is None
        assert s["served"] == 0
    finally:
        db.close()


# ---------------- авто-веса характера ----------------

def _fb(uid, outcome, n):
    db = SessionLocal()
    try:
        for _ in range(n):
            db.add(RecommendationFeedback(user_id=uid, track_id=_tid("id_hit"),
                                          source="wave", reason="t",
                                          outcome=outcome))
        db.commit()
    finally:
        db.close()


def test_auto_tune_character_weights():
    _wipe_user()
    _fb(UID, "early_skip", 60)
    db = SessionLocal()
    try:
        _rt.set_flags({"auto": {"character": True}}, db)
        out = _at.tune_user(db, UID)
        assert out["tuned"] is True, out
        ch = _rt.get_all(db, UID)["character"]
        assert ch["jitter"] > 0.12, ch["jitter"]
        assert ch["timbre_weight"] < 0.05, ch["timbre_weight"]
        assert ch["knn_weight"] < 0.08, ch["knn_weight"]
        assert any("случайность" in r for r in out.get("reasons", [])), out
    finally:
        db.close()
        _wipe_user()


def test_auto_tune_character_off_means_silence():
    _wipe_user()
    _fb(UID, "early_skip", 60)
    db = SessionLocal()
    try:
        out = _at.tune_user(db, UID)  # auto нигде не включён
        assert out["tuned"] is False
        assert _rt.get_all(db, UID) == _rt.DEFAULTS
    finally:
        db.close()
        _wipe_user()


# ---------------- mood-профиль ----------------

def _feat(db, ext, energy, valence=0.5, arousal=0.5, dance=0.5, moods=None):
    tid = str(db.query(Track).filter_by(external_id=ext).first().id)
    if db.query(TrackFeatures).filter_by(track_id=tid).first() is None:
        db.add(TrackFeatures(track_id=tid, energy=energy, valence=valence,
                             arousal=arousal, danceability=dance,
                             tempo_bpm=120.0,
                             mood_labels=moods or ["энергичный"]))
        db.commit()
    return tid


def _history(db, uid, ext, days_ago=1):
    tid = str(db.query(Track).filter_by(external_id=ext).first().id)
    db.add(PlayHistory(user_id=uid, track_id=tid,
                       played_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()
    return tid


def test_mood_profile_centroid_and_minimum():
    _wipe_user()
    db = SessionLocal()
    try:
        # 5 горячих треков с анализом -> центроид ~ их звук
        for ext in ["id_fav_20s", "id_cand_20s", "id_cand_90s",
                    "id_hit", "id_deep"]:
            _feat(db, ext, energy=0.9, valence=0.2, arousal=0.8, dance=0.3)
            _history(db, UID, ext)
        p = _mprof.get_profile(db, UID)
        assert p["centroid"] is not None, p
        assert p["tracks"] >= 5
        assert abs(p["centroid"]["energy"] - 0.9) < 1e-6
        assert abs(p["centroid"]["valence"] - 0.2) < 1e-6
        assert p["spread"] >= 0.0
    finally:
        db.close()
        _wipe_user()


def test_mood_profile_needs_minimum_tracks():
    _wipe_user()
    db = SessionLocal()
    try:
        _feat(db, "id_fav_20s", energy=0.9)
        _history(db, UID, "id_fav_20s")
        p = _mprof.get_profile(db, UID)
        assert p["centroid"] is None
        assert p["tracks"] < 5
    finally:
        db.close()
        _wipe_user()


def test_mood_profile_prior_in_scoring():
    _wipe_user()
    db = SessionLocal()
    try:
        for ext in ["id_fav_20s", "id_cand_20s", "id_cand_90s",
                    "id_hit", "id_deep"]:
            _feat(db, ext, energy=0.9, valence=0.2, arousal=0.8, dance=0.3)
            _history(db, UID, ext)
        # кандидаты: тот же артист/жанр/декада, разница только в энергии
        _feat(db, "id_raw_numetal", energy=0.9, valence=0.2,
              arousal=0.8, dance=0.3)
        _feat(db, "id_raw_hiphop", energy=0.1, valence=0.2,
              arousal=0.8, dance=0.3)
        # уравниваем жанр/год, чтобы изолировать именно профиль
        db.query(Track).filter_by(external_id="id_raw_numetal").update(
            {"genre": "nu metal", "year": 2022, "artist_name": "id_artist"})
        db.query(Track).filter_by(external_id="id_raw_hiphop").update(
            {"genre": "nu metal", "year": 2022, "artist_name": "id_artist"})
        db.commit()
        t_hot = _tid("id_raw_numetal")
        t_cold = _tid("id_raw_hiphop")
        cands = [t_cold, t_hot]
        _rt.set_flags({"character": {"jitter": 0.0,
                                     "mood_profile_weight": 0.0}}, db, UID)
        base = {r["track_id"]: r["score_raw"] for r in
                _wave.score_candidates(db, UID, cands, [], settings={},
                                       jitter_seed="mprof1")}
        _rt.set_flags({"character": {"mood_profile_weight": 1.0}}, db, UID)
        full = {r["track_id"]: r for r in
                _wave.score_candidates(db, UID, cands, [], settings={},
                                       jitter_seed="mprof1")}
        assert "mood_prof" in full[t_hot]["comp"]
        assert full[t_hot]["score_raw"] - base[t_hot] > 0.2, (
            full[t_hot]["score_raw"], base[t_hot])
        # холодному треку приор почти ничего не даёт
        assert full[t_cold]["score_raw"] - base[t_cold] < 0.2
    finally:
        db.close()
        _wipe_user()


# ---------------- MMR-финал ----------------

def _ranked_row(tid, score, artist, genre="g", mood="calm"):
    return {"track_id": tid, "score": score, "score_raw": score,
            "artist_name": artist, "genre": genre, "mood_code": mood}


def test_mmr_off_is_byte_identical():
    ranked = [_ranked_row("t%d" % i, 1.0 - i * 0.01,
                          "art%d" % (i % 3)) for i in range(12)]
    a = _wave.select_diverse(ranked, 5, artist_cap=2, genre_cap=0)
    b = _wave.select_diverse(ranked, 5, artist_cap=2, genre_cap=0,
                             mmr_lambda=0.0)
    assert [r["track_id"] for r in a] == [r["track_id"] for r in b]


def test_mmr_prefers_different_over_strong_dupe():
    ranked = [_ranked_row("a1", 1.0, "artA"),
              _ranked_row("a2", 0.99, "artA"),
              _ranked_row("b1", 0.9, "artB"),
              _ranked_row("c1", 0.8, "artC")]
    plain = _wave.select_diverse(ranked, 2, artist_cap=2, genre_cap=0)
    assert [r["track_id"] for r in plain] == ["a1", "a2"]
    mmr = _wave.select_diverse(ranked, 2, artist_cap=2, genre_cap=0,
                               mmr_lambda=1.0)
    got = [r["track_id"] for r in mmr]
    assert got[0] == "a1"  # лучший — всегда первый
    assert got == ["a1", "b1"], got
