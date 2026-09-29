"""Скип-предиктор: разметка, признаки, обучение, фолбек. Без сети, sqlite."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, PlayEvent, Track, TrackFeatures
from app.services import skip_model as _sm


def test_label_for():
    assert _sm.label_for("skip", 5) == 1
    assert _sm.label_for("skip", 29) == 1
    assert _sm.label_for("skip", 30) is None
    assert _sm.label_for("skip", 200) is None
    assert _sm.label_for("skip", None) is None
    assert _sm.label_for("complete", 10) == 0
    assert _sm.label_for("like", None) == 0
    assert _sm.label_for("replay", 3) == 0
    assert _sm.label_for("seek_back", 3) == 0
    assert _sm.label_for("play", 200) == 0
    assert _sm.label_for("play", 10) is None
    assert _sm.label_for("abandon", 50) is None
    assert _sm.label_for("weird", 1) is None


def test_features_for_shape_and_bounds():
    v = _sm.features_for()
    assert len(v) == len(_sm.FEATURES) == 15
    assert all(isinstance(x, float) for x in v)
    p = _sm.predict_proba(None, v)
    assert p is None  # битая труба — не падаем
    v2 = _sm.features_for(energy=9.9, valence=-3, tempo_bpm="xx",
                          hour="yy", plays=-5, drift_level=99)
    assert len(v2) == 15
    assert 0.0 <= v2[0] <= 1.0
    assert v2[14] == 3.0  # drift clamp


def _synth(n=5200, seed=7):
    import random as _r
    rnd = _r.Random(seed)
    X, y = [], []
    for _ in range(n):
        jump = rnd.random()
        unf = rnd.random() < 0.4
        # истинный риск растёт от скачка энергии и незнакомого жанра
        risk = 0.05 + 0.6 * jump + (0.25 if unf else 0.0)
        lab = 1 if rnd.random() < risk else 0
        X.append(_sm.features_for(energy=0.5, anchor_energy=0.5 - jump,
                                  unfamiliar_genre=unf, hour=20,
                                  plays=rnd.randint(0, 10)))
        y.append(lab)
    return X, y


def test_train_needs_data():
    X, y = _synth(500, seed=1)
    assert _sm.train(X, y) is None  # мало строк — эвристика остаётся
    X2 = [[0.5] * 15 for _ in range(5200)]
    assert _sm.train(X2, [0] * 5200) is None  # нет позитивов


def test_train_and_ordering():
    X, y = _synth()
    res = _sm.train(X, y)
    assert res is not None
    assert res["n"] == len(y)
    assert res["auc_train"] > 0.6  # синтетика разделима
    pipe = res["pipe"]
    hi = _sm.features_for(energy=0.9, anchor_energy=0.0,
                          unfamiliar_genre=True, drift_level=3)
    lo = _sm.features_for(energy=0.5, anchor_energy=0.5,
                          unfamiliar_genre=False, drift_level=0)
    ph, pl = _sm.predict_proba(pipe, hi), _sm.predict_proba(pipe, lo)
    assert ph is not None and pl is not None
    assert 0.0 <= pl <= 1.0 and 0.0 <= ph <= 1.0
    assert ph > pl  # модель выучила направление риска


def _seed_small():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="t", url="http://x").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome",
                          name="t", url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="skip_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="skip_tester", username="skip_tester")
        db.add(u)
        db.commit()
    for i in range(4):
        t = db.query(Track).filter_by(server_id=srv.id,
                                      external_id=f"sk-{i}").first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=f"sk-{i}", title=f"Skip {i}",
                      artist_name="Skipartist", genre="rock")
            db.add(t)
            db.commit()
        if db.get(TrackFeatures, str(t.id)) is None:
            db.add(TrackFeatures(track_id=str(t.id), energy=0.5 + i * 0.1,
                                 tempo_bpm=120.0, valence=0.5,
                                 danceability=0.5))
            db.commit()
        if db.query(PlayEvent).filter_by(user_id=u.id,
                                         track_id=str(t.id)).first() is None:
            db.add(PlayEvent(id=str(_uuid.uuid4()), user_id=u.id,
                             track_id=str(t.id), action="play",
                             position_sec=200, hour=20, day_of_week=2))
            db.commit()
    uid = u.id
    db.close()
    return uid


def test_get_model_falls_back_gracefully():
    from datetime import datetime

    _sm.reset_cache()
    db = SessionLocal()
    try:
        uid = _seed_small()
        # смешные объёмы: модели нет, но и взрыва нет
        assert _sm.build_dataset(db, limit=100)[0] is not None
        assert _sm.get_model(db) is None
        st = _sm.status(db)
        assert st["active"] is False
        assert st["features"] == _sm.FEATURES
        # а wave-хук при pipe=None идёт на эвристику
        from app.services import wave as _wave
        t = db.query(Track).filter_by(external_id="sk-0").first()
        f = db.get(TrackFeatures, str(t.id))
        p = _wave._skip_prob(None, t, f, {"rock": 5.0}, 0.5, None, None,
                             {"plays": 3}, 20, 2)
        assert 0.0 <= p <= 1.0
    finally:
        db.close()
        _sm.reset_cache()
