"""Контракт rec_tuning: дефолты = константы кода, per-user overlay,
валидация. Без сети, sqlite.

Главное свойство безопасности: пустая/битая БД -> поведение 1в1 как раньше
(дефолты), скоринг никогда не падает из-за настроек.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import AppSetting, MediaServer, MediaUser
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
    u = db.query(MediaUser).filter_by(username="tuning_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="tuning_tester", username="tuning_tester")
        db.add(u)
        db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _db():
    return SessionLocal()


def _wipe():
    db = _db()
    try:
        db.query(AppSetting).filter(
            AppSetting.key.in_([_rt.KEY, _rt._user_key(UID)])).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    _rt.invalidate_cache(None)


def test_defaults_match_code_constants():
    _wipe()
    db = _db()
    try:
        assert _rt.get_all(db) == _rt.DEFAULTS
        assert _rt.get_all(db, UID) == _rt.DEFAULTS
        assert _wave._weights(0) == _rt.DEFAULTS["character"]["weights_cold"]
        assert _wave._weights(1000) == _rt.DEFAULTS["character"]["weights_warm"]
    finally:
        db.close()


def test_global_and_per_user_overlay():
    _wipe()
    db = _db()
    try:
        _rt.set_flags({"character": {"jitter": 0.5}}, db)
        assert _rt.get_all(db, UID)["character"]["jitter"] == 0.5
        eff = _rt.set_flags({"character": {"jitter": 0.01}}, db, UID)
        assert eff["character"]["jitter"] == 0.01
        # глобал не тронут, чужие не тронуты
        assert _rt.get_all(db)["character"]["jitter"] == 0.5
        assert _rt.get_all(db, "someone-else")["character"]["jitter"] == 0.5
        # auto из оверлея не берётся
        _rt.set_flags({"auto": {"repeats": True}}, db, UID)
        assert _rt.get_all(db, UID)["auto"]["repeats"] is False
        _rt.set_flags({"auto": {"repeats": True}}, db)
        assert _rt.get_all(db, UID)["auto"]["repeats"] is True
    finally:
        db.close()
        _wipe()


def test_garbage_is_dropped_and_clamped():
    _wipe()
    db = _db()
    try:
        eff = _rt.set_flags({"character": {"jitter": 99, "nope": 1},
                             "bogus": {}}, db, UID)
        assert eff["character"]["jitter"] == 1.0
        assert "nope" not in eff["character"] and "bogus" not in eff
    finally:
        db.close()
        _wipe()


def test_drift_honors_per_user_counts():
    _wipe()
    evs = [{"track_id": "nope-1", "action": "skip", "position_sec": 5},
           {"track_id": "nope-2", "action": "skip", "position_sec": 5}]
    db = _db()
    try:
        assert _wave.session_drift(db, evs, UID)["severity"] is None
        _rt.set_flags({"skips": {"drift_counts": [2, 3, 4]}}, db, UID)
        d = _wave.session_drift(db, evs, UID)
        assert d["severity"] == "mild" and d["consecutive_skips"] == 2
        # у остальных — по-прежнему тихо
        assert _wave.session_drift(db, evs, "someone-else")["severity"] is None
    finally:
        db.close()
        _wipe()


def test_artist_cap_and_adaptive_caps():
    _wipe()
    db = _db()
    try:
        from app.services import playlist_ai as _pai

        assert _pai._tuning_cap(db, UID) == 2
        _rt.set_flags({"repeats": {"artist_cap": 1}}, db, UID)
        assert _pai._tuning_cap(db, UID) == 1
        assert _pai._tuning_cap(db, "someone-else") == 2

        tun = {"playlists": {"adaptive_strong": 2, "adaptive_moderate": 3,
                             "adaptive_mild": 4, "adaptive_morphing": 4,
                             "adaptive_floor": 2}}
        n, _ = _wave.adaptive_count(20, {"severity": "strong"}, False, tun)
        assert n == 2
        n, _ = _wave.adaptive_count(20, {"severity": None}, False, tun)
        assert n == 20
        n, _ = _wave.adaptive_count(20, {"severity": "strong"}, False, {})
        assert n == 4
    finally:
        db.close()
        _wipe()
