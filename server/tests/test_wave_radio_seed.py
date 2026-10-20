"""Радио-якорь в волне + живые тюнеры + «Боевое» ≠ «бодрое».

Три проверки:

  * сохранённый сид радио (`wave_seeds`) участвует в волне — стиль («как у
    Bodies») не теряется, когда очередь радио доиграна;
  * ручки, которые были пустыми кнопками (пул, окно скоринга, сиды, добор
    коллаборативных, окно усталости), реально применяются;
  * пилюля «Боевое» больше не пускает быстрый МАЖОРНЫЙ EDM.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import (
    AppSetting,
    MediaServer,
    MediaUser,
    Track,
    TrackCluster,
    TrackFeatures,
    WaveSeed,
)
from app.services import rec_tuning as _rt
from app.services import wave as _wave


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="radio_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="radio_tester", username="radio_tester")
        db.add(u)
    ids = {}
    for i in range(14):
        ext = f"rd_{i}"
        t = db.query(Track).filter_by(external_id=ext).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                      title=f"track {i}", artist_name=f"artist {i}",
                      genre="metal", play_count=10 - i)
            db.add(t)
            db.commit()
        if db.query(TrackFeatures).filter_by(track_id=t.id).first() is None:
            db.add(TrackFeatures(
                track_id=t.id, energy=0.9, valence=0.3, arousal=0.85,
                danceability=0.4, tempo_bpm=155.0,
                mood_labels=["агрессивный", "энергичный"]))
        ids[ext] = str(t.id)
    # кластер: якорь и его соседи — в одной «полке»
    for ext in ids:
        db.query(TrackCluster).filter_by(track_id=ids[ext]).delete()
        db.add(TrackCluster(track_id=ids[ext], algorithm="kmeans",
                            cluster_id=7, distance_to_center=0.1))
    db.commit()
    uid = u.id
    db.close()
    return uid, ids


UID, IDS = _seed()
ANCHOR = IDS["rd_0"]


def _wipe():
    db = SessionLocal()
    try:
        db.query(AppSetting).filter(
            AppSetting.key.in_([_rt.KEY, _rt._user_key(UID)])).delete(
            synchronize_session=False)
        db.query(WaveSeed).filter_by(user_id=UID).delete()
        db.commit()
    finally:
        db.close()
    _rt.invalidate_cache(None)


# ---------------- радио-якорь ----------------

def test_radio_seed_becomes_wave_anchor():
    """Сохранённый сид радио — первый сид волны."""
    _wipe()
    db = SessionLocal()
    try:
        db.add(WaveSeed(user_id=UID, kind="track", ref=ANCHOR,
                        label="Bodies — Drowning Pool"))
        db.commit()
        assert _wave._radio_seed_anchor(db, UID) == ANCHOR
        res = _wave.wave_continue(db, UID, queue=[], count=5, settings={})
        assert res["seeds"], res["seeds"]
        assert res["seeds"][0] == ANCHOR, res["seeds"]
    finally:
        db.close()
        _wipe()


def test_radio_anchor_by_artist():
    _wipe()
    db = SessionLocal()
    try:
        db.add(WaveSeed(user_id=UID, kind="artist", ref="artist 3",
                        label="artist 3"))
        db.commit()
        # якорь — самый играемый трек артиста
        assert _wave._radio_seed_anchor(db, UID) == IDS["rd_3"]
    finally:
        db.close()
        _wipe()


def test_without_radio_seed_no_anchor():
    _wipe()
    db = SessionLocal()
    try:
        assert _wave._radio_seed_anchor(db, UID) is None
        res = _wave.wave_continue(db, UID, queue=[], count=5, settings={})
        assert ANCHOR not in (res["seeds"] or [])
    finally:
        db.close()
        _wipe()


def test_radio_cluster_pool_collects_neighbours():
    db = SessionLocal()
    try:
        pool = _wave._radio_cluster_pool(db, ANCHOR, 50)
        assert len(pool) >= 10, len(pool)
        assert ANCHOR in pool
    finally:
        db.close()


# ---------------- тюнеры ----------------

def test_wave_limits_follow_tuning():
    _wipe()
    db = SessionLocal()
    try:
        lim = _wave.wave_limits(db, UID)
        assert lim["pool_limit"] == 2000
        assert lim["score_window"] == 800
        assert lim["seed_limit"] == 5
        assert lim["fatigue_days"] == 7
        assert lim["collab_extra"] == 200
        _rt.set_flags({"playlists": {"pool_limit": 500, "score_window": 300,
                                     "seed_limit": 2, "collab_extra": 50},
                       "repeats": {"fatigue_days": 3}}, db, UID)
        lim = _wave.wave_limits(db, UID)
        assert lim["pool_limit"] == 500, lim
        assert lim["score_window"] == 300, lim
        assert lim["seed_limit"] == 2, lim
        assert lim["collab_extra"] == 50, lim
        assert lim["fatigue_days"] == 3, lim
    finally:
        db.close()
        _wipe()


def test_seed_limit_caps_wave_seeds():
    _wipe()
    db = SessionLocal()
    try:
        _rt.set_flags({"playlists": {"seed_limit": 1}}, db, UID)
        res = _wave.wave_continue(db, UID, queue=[], count=5, settings={})
        assert len(res["seeds"] or []) <= 1, res["seeds"]
    finally:
        db.close()
        _wipe()


# ---------------- «Боевое» ----------------

def test_aggressive_pill_rejects_fast_major():
    """Быстрый мажорный EDM — не «Боевое»: раньше проходил наравне с metal."""
    db = SessionLocal()
    try:
        preset = _wave.MOOD_PRESETS["aggressive"]
        target = _wave.MOOD_TARGETS["aggressive"]
        want = set(preset["moods"])
        edm = db.query(TrackFeatures).filter(
            TrackFeatures.track_id == IDS["rd_1"]).first()
        # «металл» — трек из фикстуры (valence 0.3, arousal 0.85)
        assert _wave.mood_filter_passes(edm, "metal", want, target,
                                         _wave.MOOD_RADIUS, (preset,))
        # быстрый мажорный: подменяем вектор трека
        edm.valence = 0.75
        edm.arousal = 0.75
        edm.danceability = 0.9
        db.commit()
        assert not _wave.mood_filter_passes(edm, "pop", want, target,
                                             _wave.MOOD_RADIUS, (preset,))
    finally:
        db.close()
