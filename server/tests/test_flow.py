"""ПОТОК волны: тембр, k-NN и попарное ограничение соседей.

Этап 3, часть 2. Проверяем три вещи:

  * тембр (chroma + форма MFCC) считается и различает «тот же материал»;
  * k-NN смотрит на КОНКРЕТНЫЕ опорные треки, а не на их среднее;
  * попарное ограничение потока (темп/тембр соседей) не ломает форму волны
    (энергетический допуск) и не трогает множество треков.

Плюс: скоринг отдаёт новые компоненты (comp.timbre / comp.knn), и вес 0
выключает их — как у остальных фич.
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
    TrackFeatures,
)
from app.services import flow as _flow
from app.services import rec_tuning as _rt
from app.services import wave as _wave


class _F:
    """Фича-объект для flow.py (как TrackFeatures, но без БД)."""

    def __init__(self, mfcc=None, chroma=None, **kw):
        self.mfcc_summary = {"mean": list(mfcc)} if mfcc else None
        self.chroma_summary = {"mean": list(chroma)} if chroma else None
        for k, v in kw.items():
            setattr(self, k, v)


def _mfcc_rising():
    return [float(i) for i in range(13)]


def _mfcc_falling():
    return [float(12 - i) for i in range(13)]


def _chroma(peak=0):
    return [1.0 if i == peak else 0.1 for i in range(12)]


# ---------------- тембр ----------------

def test_timbre_of_same_material_is_one():
    a = _flow.timbre_vector(_F(mfcc=_mfcc_rising(), chroma=_chroma(0)))
    b = _flow.timbre_vector(_F(mfcc=_mfcc_rising(), chroma=_chroma(0)))
    assert a and b
    assert _flow.timbre_similarity(a, b) == 1.0


def test_timbre_of_other_material_is_lower():
    a = _flow.timbre_vector(_F(mfcc=_mfcc_rising(), chroma=_chroma(0)))
    b = _flow.timbre_vector(_F(mfcc=_mfcc_falling(), chroma=_chroma(7)))
    assert _flow.timbre_similarity(a, b) < 0.3


def test_timbre_needs_analysis():
    assert _flow.timbre_vector(_F()) is None
    assert _flow.chroma_profile(_F(chroma=[1.0, 2.0])) is None
    assert _flow.mfcc_profile(_F(mfcc=[1.0, 2.0, 3.0])) is None


def test_timbre_centroid_similar_to_its_parts():
    vecs = [_flow.timbre_vector(_F(mfcc=_mfcc_rising(), chroma=_chroma(0))),
            _flow.timbre_vector(_F(mfcc=_mfcc_rising(), chroma=_chroma(0)))]
    cent = _flow.timbre_centroid(vecs)
    assert cent
    assert _flow.timbre_to_centroid(vecs[0], cent) > 0.9


# ---------------- k-NN ----------------

def test_knn_prefers_similar_reference():
    refs = {"r1": [1.0, 0.0, 0.0], "r2": [0.0, 1.0, 0.0]}
    cands = {"near": [0.9, 0.1, 0.0], "far": [0.0, 0.0, 1.0]}
    out = _flow.knn_scores(cands, refs, ["r1", "r2"], top_k=1)
    assert out["near"] > out["far"]
    assert 0.0 <= out["far"] <= 1.0


def test_knn_top_k_is_averaged():
    refs = {"r1": [1.0, 0.0, 0.0], "r2": [0.0, 1.0, 0.0]}
    cands = {"c": [1.0, 0.0, 0.0]}
    one = _flow.knn_scores(cands, refs, ["r1", "r2"], top_k=1)
    two = _flow.knn_scores(cands, refs, ["r1", "r2"], top_k=2)
    assert one["c"] == 1.0
    assert 0.0 < two["c"] < 1.0


def test_knn_without_references_is_empty():
    assert _flow.knn_scores({"c": [1.0]}, {}, ["r1"]) == {}
    assert _flow.knn_scores({}, {"r1": [1.0]}, ["r1"]) == {}


# ---------------- темп ----------------

def test_tempo_compatibility():
    assert _flow.tempo_compatibility(120, 120, 40) == 1.0
    assert _flow.tempo_compatibility(120, 160, 40) == 0.0
    assert 0.0 < _flow.tempo_compatibility(120, 130, 40) < 1.0
    # нет темпа — нейтрально
    assert _flow.tempo_compatibility(None, 120, 40) == 0.5


# ---------------- попарное ограничение потока ----------------

def _item(tid, tempo, energy=0.5):
    return {"track_id": tid, "tempo": tempo, "energy": energy}


def test_flow_order_groups_close_tempos():
    items = [_item("a", 100), _item("b", 140), _item("c", 102), _item("d", 138)]
    out = _wave._smooth_flow_order(items, {})
    ids = [i["track_id"] for i in out]
    assert sorted(ids) == ["a", "b", "c", "d"]
    assert abs(ids.index("c") - ids.index("a")) == 1, ids


def test_flow_order_respects_energy_guard():
    """Своп не должен рвать форму волны (|Δenergy| < flow_energy_max)."""
    items = [_item("a", 100, 0.2), _item("b", 140, 0.9),
             _item("c", 102, 0.2), _item("d", 138, 0.9)]
    out = _wave._smooth_flow_order(items, {})
    assert [i["track_id"] for i in out] == ["a", "b", "c", "d"]


def test_flow_order_off_when_weights_zero():
    items = [_item("a", 100), _item("b", 140), _item("c", 102), _item("d", 138)]
    tun = {"playlists": {"flow_tempo_weight": 0.0, "flow_timbre_weight": 0.0}}
    out = _wave._smooth_flow_order(items, tun)
    assert [i["track_id"] for i in out] == ["a", "b", "c", "d"]


# ---------------- скоринг ----------------

def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="flow_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="flow_tester", username="flow_tester")
        db.add(u)
    ids = {}
    for name, mfcc, chroma, bpm in (
            ("fl_a", _mfcc_rising(), _chroma(0), 120),
            ("fl_b", _mfcc_rising(), _chroma(0), 122),
            ("fl_c", _mfcc_falling(), _chroma(7), 124),
            ("fl_d", _mfcc_falling(), _chroma(7), 126)):
        t = db.query(Track).filter_by(external_id=name).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=name, title=name,
                      artist_name="fl_artist_%s" % name, genre="fl_genre")
            db.add(t)
            db.commit()
        if db.query(TrackFeatures).filter_by(track_id=t.id).first() is None:
            db.add(TrackFeatures(
                track_id=t.id, energy=0.5, valence=0.5, arousal=0.5,
                danceability=0.5, tempo_bpm=float(bpm),
                mfcc_summary={"mean": mfcc}, chroma_summary={"mean": chroma}))
        ids[name] = str(t.id)
    db.commit()
    uid = u.id
    db.close()
    return uid, ids


UID, IDS = _seed()


def _wipe():
    db = SessionLocal()
    try:
        db.query(AppSetting).filter(
            AppSetting.key.in_([_rt.KEY, _rt._user_key(UID)])).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    _rt.invalidate_cache(None)


def test_score_has_flow_components():
    _wipe()
    db = SessionLocal()
    try:
        cands = [IDS["fl_a"], IDS["fl_b"], IDS["fl_c"], IDS["fl_d"]]
        ranked = _wave.score_candidates(
            db, UID, cands, [IDS["fl_a"]],
            settings={}, jitter_seed="flow1",
            session_ids=[IDS["fl_a"]])
        assert ranked
        comps = [r["comp"] for r in ranked]
        assert all("timbre" in c and "knn" in c for c in comps)
        by_id = {r["track_id"]: r["comp"] for r in ranked}
        # свой материал — выше тембровый балл
        assert by_id[IDS["fl_b"]]["timbre"] > by_id[IDS["fl_c"]]["timbre"]
    finally:
        db.close()
        _wipe()


def test_flow_weights_zero_disable_components():
    _wipe()
    db = SessionLocal()
    try:
        _rt.set_flags({"character": {"timbre_weight": 0.0, "knn_weight": 0.0}},
                      db, UID)
        cands = [IDS["fl_a"], IDS["fl_b"], IDS["fl_c"], IDS["fl_d"]]
        ranked = _wave.score_candidates(
            db, UID, cands, [IDS["fl_a"]],
            settings={}, jitter_seed="flow2",
            session_ids=[IDS["fl_a"]])
        assert ranked
        assert all(r["comp"]["timbre"] == 0.0 for r in ranked)
        assert all(r["comp"]["knn"] == 0.0 for r in ranked)
    finally:
        db.close()
        _wipe()


def test_wave_continue_carries_flow_components():
    """Волна целиком: в ответе есть оба новых компонента разбора."""
    _wipe()
    db = SessionLocal()
    try:
        res = _wave.wave_continue(db, UID, queue=[], count=5, settings={})
        assert res["tracks"]
        for t in res["tracks"]:
            assert "timbre" in t["comp"]
            assert "knn" in t["comp"]
    finally:
        db.close()
        _wipe()
