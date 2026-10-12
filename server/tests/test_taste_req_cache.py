"""Инварианты кэша на сессию в taste.py.

user_profile / _user_track_aggregates кладут результат в Session.info, чтобы
не перечитывать историю пользователя по 2-3 раза за один HTTP-запрос (запрос
идёт в минуту, а виджет поллит каждые 15с — лавина выедала пул БД).

Кэш не должен искажать данные. Проверяем ровно те места, где правка заменила
SQL на уже загруженные строки:
  - counts.events / counts.plays вместо двух COUNT-запросов;
  - гистограмма hours/days из строк PlayEvent вместо ещё одного скана;
  - plays и last_played из одного прохода по PlayHistory вместо двух;
  - top_n не влияет ни на что, кроме среза top_tracks.
"""
from datetime import datetime, timedelta

import pytest

from app.db.database import SessionLocal
from app.db.models import (
    Favorite,
    MediaServer,
    MediaUser,
    PlayEvent,
    PlayHistory,
    Track,
    TrackDislike,
    TrackStat,
)
from app.services import taste as _taste

_UID = "u1"
_BASE = datetime(2026, 1, 1, 10, 0, 0)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        s.add(MediaServer(id="srv1", type="navidrome", name="t", url="http://x"))
        s.add(MediaUser(id=_UID, server_id="srv1", external_id="ext1", username="tester"))
        s.add(Track(id="t1", server_id="srv1", external_id="e1",
                    title="One", artist_name="A", genre="Rock"))
        s.add(Track(id="t2", server_id="srv1", external_id="e2",
                    title="Two", artist_name="B", genre="Jazz"))
        s.flush()
        # Два прогона t1 (разные played_at — проверяем last_played) и один t2.
        s.add(PlayHistory(id="ph1", user_id=_UID, track_id="t1", played_at=_BASE))
        s.add(PlayHistory(id="ph2", user_id=_UID, track_id="t1",
                          played_at=_BASE + timedelta(days=1)))
        s.add(PlayHistory(id="ph3", user_id=_UID, track_id="t2",
                          played_at=_BASE + timedelta(days=2)))
        # hour/day_of_week заполнены не у всех — как в жизни (None тоже).
        s.add(PlayEvent(id="pe1", user_id=_UID, track_id="t1", action="skip",
                        position_sec=5, hour=10, day_of_week=3))
        s.add(PlayEvent(id="pe2", user_id=_UID, track_id="t1", action="skip",
                        position_sec=200, hour=11, day_of_week=3))
        s.add(PlayEvent(id="pe3", user_id=_UID, track_id="t2", action="complete",
                        position_sec=None, hour=10, day_of_week=4))
        s.add(PlayEvent(id="pe4", user_id=_UID, track_id="t2", action="replay",
                        position_sec=None, hour=None, day_of_week=None))
        s.add(Favorite(user_id=_UID, track_id="t1", starred_at=_BASE))
        s.add(TrackDislike(user_id=_UID, track_id="t1", source="manual"))
        s.add(TrackStat(user_id=_UID, track_id="t2", plays=7, skips=1, early_skips=0,
                        replays=0, seek_backs=0, abandons=0, completes=2,
                        mobile_score=3, last_played=_BASE + timedelta(days=3)))
        s.flush()
        yield s
    finally:
        s.close()


def test_counts_match_real_count(db):
    """counts.events/plays считаются по загруженным строкам — они должны
    совпадать с настоящими COUNT(*) в БД."""
    prof = _taste.user_profile(db, _UID, top_n=50)
    assert prof["counts"]["plays"] == db.query(PlayHistory).filter_by(
        user_id=_UID).count()
    assert prof["counts"]["events"] == db.query(PlayEvent).filter_by(
        user_id=_UID).count()


def test_hours_days_match_direct_count(db):
    """Гистограмма часов/дней — по тем же строкам PlayEvent, что и агрегаты."""
    prof = _taste.user_profile(db, _UID, top_n=50)
    want_h = [0] * 24
    want_d = [0] * 7
    for ev in db.query(PlayEvent).filter(PlayEvent.user_id == _UID).all():
        if isinstance(ev.hour, int) and 0 <= ev.hour < 24:
            want_h[ev.hour] += 1
        if isinstance(ev.day_of_week, int) and 0 <= ev.day_of_week < 7:
            want_d[ev.day_of_week] += 1
    assert prof["hours"] == want_h
    assert prof["days"] == want_d
    # События с hour=None не должны попадать ни в одну ячейку.
    assert sum(prof["hours"]) == 3


def test_aggregates_plays_and_last_played(db):
    """plays = строки PlayHistory + TrackStat.plays; last_played = максимум."""
    agg = _taste._user_track_aggregates(db, _UID)
    # t1: 2 строки PlayHistory, t2: 1 строка + TrackStat.plays=7
    assert agg["t1"]["plays"] == 2
    assert agg["t2"]["plays"] == 1 + 7
    assert agg["t1"]["last_played"] == _BASE + timedelta(days=1)
    # TrackStat.last_played позже PlayHistory — должен победить
    assert agg["t2"]["last_played"] == _BASE + timedelta(days=3)
    # Ранний скип (<30c) считается отдельно от обычного
    assert agg["t1"]["skips"] == 2 and agg["t1"]["early_skips"] == 1
    assert agg["t1"]["fav"] is True and agg["t1"]["dislike"] is True
    # completes/replays складываются из PlayEvent и TrackStat, как и plays
    assert agg["t2"]["completes"] == 1 + 2
    assert agg["t2"]["replays"] == 1 + 0


def test_top_n_only_slices_top_tracks(db):
    """top_n=200 и top_n=0 должны дать одинаковые данные кроме top_tracks —
    ради этого общий профиль кэшируется, а срез делается на выходе."""
    full = _taste.user_profile(db, _UID, top_n=200)
    zero = _taste.user_profile(db, _UID, top_n=0)
    assert zero["top_tracks"] == []
    assert full["top_tracks"]
    for key in full:
        if key == "top_tracks":
            continue
        assert full[key] == zero[key], key
    assert zero["top_tracks"] == full["top_tracks"][:0]
    one = _taste.user_profile(db, _UID, top_n=1)
    assert one["top_tracks"] == full["top_tracks"][:1]


def test_repeat_call_returns_same_result(db):
    """Второй вызов на той же сессии берётся из кэша и не должен отличаться."""
    a = _taste.user_profile(db, _UID, top_n=50)
    a["top_tracks"].append({"track_id": "мусор", "score": 999})
    b = _taste.user_profile(db, _UID, top_n=50)
    # Кэш не должен пострадать от мутации результата снаружи.
    assert "мусор" not in {t.get("track_id") for t in b["top_tracks"]}


def test_cache_does_not_outlive_session(db):
    """Кэш живёт на сессии: в новой сессии данные считаются заново."""
    _taste.user_profile(db, _UID, top_n=50)
    db.add(PlayHistory(id="ph4", user_id=_UID, track_id="t1",
                       played_at=_BASE + timedelta(days=9)))
    db.flush()
    # Эта же сессия — закэшировано, новая строка не видна (ожидаемо).
    assert _taste.user_profile(db, _UID, top_n=50)["counts"]["plays"] == 3
    db.expire_all()
    other = SessionLocal()
    try:
        assert _taste.user_profile(other, _UID, top_n=50)["counts"]["plays"] == 4
    finally:
        other.close()