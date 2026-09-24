"""Дедуп волны: без сети, sqlite.

Регресс на жалобу «дубли при добавлении очереди + забаненный артист»:
1. Одна песня под двумя row id (дубль после перескана): если row A уже
   в очереди — wave_continue не должен возвращать row B.
2. Забаненный артист не попадает в выдачу.
3. Задизлайканный трек не возвращается и под другим row id.
4. В одном ответе песня не повторяется дважды.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import ArtistBan, MediaServer, MediaUser, Track, TrackDislike
from app.services import wave as _wave


def _mk_track(db, srv, ext, title, artist):
    t = db.query(Track).filter_by(server_id=srv.id, external_id=ext).first()
    if t is None:
        t = Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                  title=title, artist_name=artist)
        db.add(t)
        db.commit()
    return t


def _seed():
    # Идемпотентно: engine общий на все модули, дубли не плодим.
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="t", url="http://x").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="dedup_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="dedup_tester", username="dedup_tester")
        db.add(u)
        db.commit()
    # Дубль одной песни под разными external_id/row id
    _mk_track(db, srv, "dup-a", "Battle Station 3", "Inon Zur")
    _mk_track(db, srv, "dup-b", "Battle Station 3", "Inon Zur")
    # Трек забаненного артиста
    _mk_track(db, srv, "ban-1", "Banned Song", "Banned Artist")
    # Обычные треки для наполнения пула
    for i in range(12):
        _mk_track(db, srv, f"ok-{i}", f"Song {i}", f"Artist {i}")
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _db():
    return SessionLocal()


def test_dupe_row_not_returned_when_twin_in_queue():
    db = _db()
    try:
        res = _wave.wave_continue(db, UID, queue=["dup-a"], count=20)
        got = {(t["artist_name"], t["title"]) for t in res["tracks"]}
        assert ("Inon Zur", "Battle Station 3") not in got
        ids = [t["external_id"] for t in res["tracks"]]
        assert "dup-a" not in ids and "dup-b" not in ids
    finally:
        db.close()


def test_banned_artist_excluded():
    db = _db()
    try:
        db.add(ArtistBan(user_id=UID, artist_name="Banned Artist",
                         reason="test"))
        db.commit()
        res = _wave.wave_continue(db, UID, queue=[], count=20)
        artists = {t["artist_name"] for t in res["tracks"]}
        assert "Banned Artist" not in artists
    finally:
        db.query(ArtistBan).filter_by(user_id=UID).delete()
        db.commit()
        db.close()


def test_disliked_song_excluded_under_other_row_id():
    db = _db()
    try:
        row_a = db.query(Track).filter_by(external_id="dup-a").first()
        db.add(TrackDislike(user_id=UID, track_id=str(row_a.id)))
        db.commit()
        res = _wave.wave_continue(db, UID, queue=[], count=20)
        got = {(t["artist_name"], t["title"]) for t in res["tracks"]}
        assert ("Inon Zur", "Battle Station 3") not in got
    finally:
        db.query(TrackDislike).filter_by(user_id=UID).delete()
        db.commit()
        db.close()


def test_no_repeat_song_within_single_response():
    db = _db()
    try:
        res = _wave.wave_continue(db, UID, queue=[], count=20)
        keys = [((t["artist_name"] or "").lower(),
                 (t["title"] or "").lower()) for t in res["tracks"]]
        assert len(keys) == len(set(keys))
    finally:
        db.close()

