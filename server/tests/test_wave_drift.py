"""Сессионный дрейф волны (порт MoodDriftDetector). Без сети, sqlite."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, Track
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
    u = db.query(MediaUser).filter_by(username="drift_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="drift_tester", username="drift_tester")
        db.add(u)
    for i, (ext, genre) in enumerate([("dr1", "rock"), ("dr2", "rock"),
                                      ("dr3", "rock"), ("dr4", "pop"),
                                      ("dr5", "jazz")]):
        if db.query(Track).filter_by(external_id=ext).first() is None:
            db.add(Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                         title=f"T{i}", artist_name="A", genre=genre))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _db():
    return SessionLocal()


def _ev(ext, action="skip"):
    return {"track_id": ext, "action": action, "position_sec": 5}


def test_no_drift_without_skips():
    db = _db()
    try:
        d = _wave.session_drift(db, [_ev("dr1", "play"), _ev("dr2", "play")])
        assert d["severity"] is None and d["consecutive_skips"] == 0
        assert d["temp_banned_genres"] == [] and d["skip_ids"] == []
    finally:
        db.close()


def test_trailing_thresholds():
    db = _db()
    try:
        assert _wave.session_drift(db, [_ev("dr1"), _ev("dr2")])["severity"] is None
        assert _wave.session_drift(
            db, [_ev("dr1", "play"), _ev("dr2"), _ev("dr3"), _ev("dr4")],
        )["severity"] == "mild"
        assert _wave.session_drift(
            db, [_ev(f"dr{i}") for i in (1, 2, 3, 4, 5)],
        )["severity"] == "moderate"
        d = _wave.session_drift(db, [_ev(f"dr{i}") for i in (1, 2, 3, 4, 5, 1, 2)])
        assert d["severity"] == "strong" and d["consecutive_skips"] == 7
    finally:
        db.close()


def test_positive_breaks_streak():
    db = _db()
    try:
        d = _wave.session_drift(
            db, [_ev("dr1"), _ev("dr2"), _ev("dr3"), _ev("dr4", "play"),
                 _ev("dr5"), _ev("dr1")])
        assert d["severity"] is None and d["consecutive_skips"] == 2
    finally:
        db.close()


def test_temp_genre_ban_and_skip_ids():
    db = _db()
    try:
        d = _wave.session_drift(db, [_ev("dr1"), _ev("dr2"), _ev("dr3")])
        assert d["temp_banned_genres"] == ["rock"]
        assert len(d["skip_ids"]) == 3
        # 2 скипа жанра — бана нет
        d2 = _wave.session_drift(db, [_ev("dr1"), _ev("dr2")])
        assert d2["temp_banned_genres"] == []
    finally:
        db.close()


def test_wave_continue_applies_drift():
    db = _db()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10, settings={},
                                recent_events=[_ev("dr1"), _ev("dr2"), _ev("dr3")])
        assert r["drift"] is not None and r["drift"]["severity"] == "mild"
        got = [t["track_id"] for t in r["tracks"]]
        db2 = _db()
        try:
            skip_uuids = {str(t.id) for t in
                          db2.query(Track).filter(Track.external_id.in_(["dr1", "dr2", "dr3"])).all()}
        finally:
            db2.close()
        assert not (set(got) & skip_uuids), "скипнутое не должно докладываться"
        # рок временно забанен — в докладе его нет
        assert all(t.get("genre") != "rock" for t in r["tracks"])
    finally:
        db.close()

