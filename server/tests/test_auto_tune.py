"""Авто-мозг R3: каждый юзер тюнится отдельно. Без сети, sqlite."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

import pytest

from app.db.database import SessionLocal, init_db
from app.db.models import (AppSetting, MediaServer, MediaUser,
                           RecommendationFeedback, Track)
from app.services import auto_tune as _at
from app.services import rec_tuning as _rt


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    uids = {}
    for name in ("tune_skipper", "tune_lover", "tune_cold"):
        u = db.query(MediaUser).filter_by(username=name).first()
        if u is None:
            u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                          external_id=name, username=name)
            db.add(u)
            db.commit()
        uids[name] = u.id
    if db.query(Track).filter_by(external_id="tune_tr").first() is None:
        db.add(Track(id=str(_uuid.uuid4()), server_id=srv.id,
                     external_id="tune_tr", title="T", artist_name="A"))
        db.commit()
    tid = db.query(Track).filter_by(external_id="tune_tr").first().id
    db.close()
    return uids, tid


UIDS, TID = _seed()


def _wipe():
    db = SessionLocal()
    try:
        db.query(RecommendationFeedback).filter(
            RecommendationFeedback.user_id.in_(list(UIDS.values()))).delete(
            synchronize_session=False)
        db.query(AppSetting).filter(
            AppSetting.key.in_(
                [_rt.KEY] + [_rt._user_key(u) for u in UIDS.values()]
                + [_at.LOG_PREFIX + u for u in UIDS.values()])).delete(
            synchronize_session=False)
        # tune_all пишет skip-историю и чужим юзерам общей тестовой базы —
        # подметаем все логи, чтобы не литтерить соседним тестам
        db.query(AppSetting).filter(
            AppSetting.key.like(_at.LOG_PREFIX + "%")).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    _rt.invalidate_cache(None)


def _fb(uid, outcome, reason, n):
    db = SessionLocal()
    try:
        for _ in range(n):
            db.add(RecommendationFeedback(user_id=uid, track_id=TID,
                                          source="wave", reason=reason,
                                          outcome=outcome))
        db.commit()
    finally:
        db.close()


def test_users_diverge_and_global_untouched():
    _wipe()
    # A: всё скипает (причины про артиста/жанр/открытия), B: всё любит
    _fb(UIDS["tune_skipper"], "early_skip", "By Somebody", 20)
    _fb(UIDS["tune_skipper"], "early_skip", "rock you enjoy", 20)
    _fb(UIDS["tune_skipper"], "early_skip", "New discovery", 20)
    for _ in range(50):
        _fb(UIDS["tune_lover"], "completed", "Similar to what you love", 1)
    for _ in range(10):
        _fb(UIDS["tune_lover"], "liked", "New discovery", 1)
    _fb(UIDS["tune_cold"], "completed", "Similar to what you love", 10)

    db = SessionLocal()
    try:
        _rt.set_flags({"auto": {"repeats": True}}, db)
        out = _at.tune_all(db)
        # tune_all идёт по ВСЕМ юзерам общей тестовой базы (их создают и
        # соседние модули) — смотрим только на своих троих
        mine = [r for r in out["results"]
                if r.get("user_id") in UIDS.values()]
        assert len(mine) == 3
        assert sum(1 for r in mine if r.get("tuned")) == 2  # холодный молчит
        by_id = {r["user_id"]: r for r in mine}

        a = _rt.get_all(db, UIDS["tune_skipper"])["repeats"]
        assert a["artist_penalty"] == pytest.approx([0.0575, 0.115, 0.1725])
        assert a["genre_penalty"] == pytest.approx([0.0345, 0.0805, 0.138])
        assert by_id[UIDS["tune_skipper"]]["tuned"] is True

        b = _rt.get_all(db, UIDS["tune_lover"])["repeats"]
        assert b["artist_penalty"] == pytest.approx([0.0475, 0.095, 0.1425])
        assert a["artist_penalty"] != b["artist_penalty"]  # разъехались

        # холодный — без оверлея, но с записью в историю
        assert _rt._user_key(UIDS["tune_cold"]) is not None
        db2 = SessionLocal()
        try:
            assert db2.get(AppSetting,
                           _rt._user_key(UIDS["tune_cold"])) is None
        finally:
            db2.close()
        assert by_id[UIDS["tune_cold"]]["tuned"] is False

        # глобал: только auto, значений нет
        row = db.get(AppSetting, _rt.KEY)
        assert row is not None and "repeats" not in (row.value or {})

        # история для панели «Сейчас применено»
        log = _at.get_log(db, UIDS["tune_skipper"])
        assert log and log[0]["tuned"] is True
        assert "changes" in log[0] and "reasons" in log[0]
    finally:
        db.close()
        _wipe()


def test_auto_off_means_silence():
    _wipe()
    _fb(UIDS["tune_skipper"], "early_skip", "By Somebody", 60)
    db = SessionLocal()
    try:
        out = _at.tune_all(db)  # auto нигде не включён
        mine = [r for r in out["results"]
                if r.get("user_id") in UIDS.values()]
        assert all(not r.get("tuned") for r in mine)
        assert _rt.get_all(db, UIDS["tune_skipper"]) == _rt.DEFAULTS
    finally:
        db.close()
        _wipe()
