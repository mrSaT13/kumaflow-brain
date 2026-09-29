"""rerank_pool: единый скоринг плейлистов на ядре волны. Без сети, sqlite."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import (Favorite, MediaServer, MediaUser, PlayEvent,
                           RecommendationFeedback, Track, TrackFeatures)
from app.services import wave as _wave


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="t", url="http://x").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome",
                          name="t", url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="rerank_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="rerank_tester", username="rerank_tester")
        db.add(u)
        db.commit()
    tids = []
    for i in range(10):
        t = db.query(Track).filter_by(server_id=srv.id,
                                      external_id=f"rk-{i}").first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=f"rk-{i}", title=f"Rerank {i}",
                      artist_name=f"Rkartist {i % 3}", genre="rock")
            db.add(t)
            db.commit()
        tids.append(str(t.id))
        if db.get(TrackFeatures, str(t.id)) is None:
            db.add(TrackFeatures(track_id=str(t.id),
                                 energy=0.3 + (i % 5) * 0.1,
                                 tempo_bpm=110.0 + i * 3,
                                 valence=0.5, danceability=0.5,
                                 mood_labels=["happy"]))
            db.commit()
    # вкус: лайк + пара прослушиваний (сиды и behaviorBonus не пустые)
    if db.query(Favorite).filter_by(user_id=u.id).first() is None:
        db.add(Favorite(user_id=u.id, track_id=tids[0]))
        db.commit()
    for tid in tids[:3]:
        if db.query(PlayEvent).filter_by(user_id=u.id,
                                         track_id=tid).first() is None:
            db.add(PlayEvent(id=str(_uuid.uuid4()), user_id=u.id,
                             track_id=tid, action="complete",
                             position_sec=200, hour=20, day_of_week=2))
            db.commit()
    uid = u.id
    db.close()
    return uid, tids


UID, TIDS = _seed()


def test_rerank_pool_returns_subset_and_marks_served():
    db = SessionLocal()
    try:
        db.query(RecommendationFeedback).filter_by(user_id=UID).delete()
        db.commit()
        out = _wave.rerank_pool(db, UID, TIDS, kind="smart-test",
                                n=5, source="smart")
        assert len(out) == 5
        assert set(out) <= set(TIDS)
        assert len(set(out)) == 5  # без дублей
        rows = db.query(RecommendationFeedback).filter_by(
            user_id=UID, source="smart").all()
        assert len(rows) >= 5  # выдача зафиксирована для обучения
    finally:
        db.close()


def test_rerank_pool_empty_and_fallback():
    db = SessionLocal()
    try:
        assert _wave.rerank_pool(db, UID, [], kind="x") == []
        # мусорные id отсекаются скорингом, пусто не падает
        out = _wave.rerank_pool(db, UID, ["nope-1", "nope-2"], kind="x", n=5)
        assert out == []
    finally:
        db.close()


def test_smart_kinds_use_unified_core():
    from app.services import smart as _smart
    db = SessionLocal()
    try:
        disc = _smart.discoveries(db, UID, n=5)
        assert isinstance(disc, list)
        assert all(isinstance(t, str) for t in disc)
        forg = _smart.forgotten(db, UID, n=5)
        assert isinstance(forg, list)
        night = _smart.night(db, UID, n=5)
        assert isinstance(night, list)
        sport = _smart.sport(db, UID, n=5)
        assert isinstance(sport, list)
    finally:
        db.close()
