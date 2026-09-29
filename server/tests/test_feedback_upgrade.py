"""Лайк-апгрейд показов, ранжирные бакеты, источники синка. Без сети, sqlite."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import (MediaServer, MediaUser, RecommendationFeedback,
                           TasteProfile, Track)
from app.services import rec_feedback as _rfb
from app.services import taste as _taste


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="t", url="http://x").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome",
                          name="t", url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="fb_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="fb_tester", username="fb_tester")
        db.add(u)
        db.commit()
    tids = []
    for i in range(15):
        t = db.query(Track).filter_by(server_id=srv.id,
                                      external_id=f"fb-{i}").first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=f"fb-{i}", title=f"Fb {i}",
                      artist_name="Fbartist", genre="rock",
                      duration_sec=200)
            db.add(t)
            db.commit()
        tids.append(str(t.id))
    uid = u.id
    db.close()
    return uid, tids


UID, TIDS = _seed()


def _clean(db):
    db.query(RecommendationFeedback).filter_by(user_id=UID).delete()
    db.commit()


def test_like_upgrades_decided_row():
    db = SessionLocal()
    try:
        _clean(db)
        # показ -> complete (строка закрыта) -> лайк позже
        assert _rfb.mark_served(db, UID, [{"track_id": TIDS[0], "score": 0.9,
                                           "reason": "t", "rank": 0}],
                                source="wave") == 1
        assert _rfb.mark_outcome(db, UID, TIDS[0], "complete", 200, 200) is True
        row = db.query(RecommendationFeedback).filter_by(user_id=UID).first()
        assert row.outcome == "completed"
        # раньше тут было False и «лайк с волны 0%»
        assert _rfb.mark_outcome(db, UID, TIDS[0], "like", 200, 200) is True
        db.refresh(row)
        assert row.outcome == "liked"
        # обычные действия закрытую строку не трогают
        assert _rfb.mark_outcome(db, UID, TIDS[0], "skip", 5, 200) is False
        db.refresh(row)
        assert row.outcome == "liked"
        # дизлайк апгрейдит тоже
        assert _rfb.mark_outcome(db, UID, TIDS[0], "dislike", 5, 200) is True
        db.refresh(row)
        assert row.outcome == "disliked"
    finally:
        db.close()


def test_rank_buckets_in_summary():
    db = SessionLocal()
    try:
        _clean(db)
        rows = ([{"track_id": t, "score": 0.9, "rank": i} for i, t in enumerate(TIDS[:3])]
                + [{"track_id": t, "score": 0.2, "rank": i} for i, t in enumerate(TIDS[3:13], start=3)]
                + [{"track_id": t, "score": 0.1, "rank": i} for i, t in enumerate(TIDS[13:], start=13)])
        assert _rfb.mark_served(db, UID, rows, source="wave") == 15
        # топ-3 дослушали, хвост скипнул рано
        for t in TIDS[:3]:
            _rfb.mark_outcome(db, UID, t, "complete", 200, 200)
        for t in TIDS[3:]:
            _rfb.mark_outcome(db, UID, t, "skip", 5, 200)
        s = _rfb.summary(db, user_id=UID, days=7)
        by = {b["bucket"]: b for b in s["by_score"]}
        assert set(by) == {"топ-3", "4–10", "11+"}
        assert by["топ-3"]["completion_rate"] == 100.0
        assert by["топ-3"]["early_skip_rate"] == 0.0
        assert by["11+"]["early_skip_rate"] == 100.0
        assert by["11+"]["served"] == 5
    finally:
        db.close()


def test_custom_sync_source_recorded():
    db = SessionLocal()
    try:
        res = _taste.sync_from_mobile(db, UID, {"source": "esp_kitchen",
                                                "ratings": [], "profile": {},
                                                "events": []})
        assert res["ok"] is True and res["source"] == "esp_kitchen"
        tp = db.get(TasteProfile, UID)
        assert tp is not None
        srcs = dict(getattr(tp, "sync_sources", None) or {})
        assert "esp_kitchen" in srcs
        prof = _taste.user_profile(db, UID)
        names = [s["source"] for s in prof["mobile"]["sync_sources"]]
        assert "esp_kitchen" in names
        # мусорный source схлопывается в mobile, как раньше
        res2 = _taste.sync_from_mobile(db, UID, {"source": "!!!",
                                                 "ratings": [], "profile": {},
                                                 "events": []})
        assert res2["source"] == "mobile"
    finally:
        db.close()
