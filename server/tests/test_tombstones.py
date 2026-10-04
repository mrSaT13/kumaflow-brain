"""Надгробия вкусов: удаления расходятся по устройствам.

Разбан/анлайк пишет TasteTombstone; sync_to_mobile(since) отдаёт removed.
Без курсора — как раньше (только added). Часы сервера могут врать —
порядок задаёт монотонный id, время лишь курсор отображения.
"""
import uuid as _uuid
from datetime import datetime, timedelta

from app.api.users import unban_artist
from app.db.database import SessionLocal, init_db
from app.db.models import ArtistBan, Favorite, MediaServer, MediaUser, Track, TrackDislike, TasteTombstone
from app.services import taste as _taste


class _P:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="tomb-t").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome",
                          name="tomb-t", url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="tomb_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="tomb_tester", username="tomb_tester")
        db.add(u)
        db.commit()
    t = db.query(Track).filter_by(server_id=srv.id, external_id="tomb-song-1").first()
    if t is None:
        t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                  external_id="tomb-song-1", title="T", artist_name="Tomb Artist",
                  artist_external_id="tomb-artist-1")
        db.add(t)
        db.commit()
    uid = str(u.id)
    db.close()
    return uid


UID = _seed()


def _rate(track_ext, like):
    from app.services.track_resolve import get_track as _gt
    db = SessionLocal()
    try:
        t = db.query(Track).filter_by(external_id=track_ext).first()
        return _taste.record_rate(db, UID, str(t.id), like)
    finally:
        db.close()


def test_rate_flips_write_tombstones_and_removed():
    _rate("tomb-song-1", True)
    _rate("tomb-song-1", False)  # дизлайк стирает лайк → надгробие like
    db = SessionLocal()
    try:
        snap0 = _taste.sync_to_mobile(db, UID)
        assert snap0["removed"] == []  # без курсора — как раньше
        assert "tomb-song-1" in snap0["dislikedSongs"]
        cur = snap0["server_now"]
        assert cur
        _rate("tomb-song-1", None)  # снять оценку → надгробие dislike
        snap1 = _taste.sync_to_mobile(db, UID, since=cur)
        kinds = {(r["kind"], r["key"]) for r in snap1["removed"]}
        assert ("dislike", "tomb-song-1") in kinds, snap1["removed"]
        assert snap1["dislikedSongs"] == []
        # мусорный курсор — консервативно: без удалений, без падения
        snap2 = _taste.sync_to_mobile(db, UID, since="not-a-date")
        assert snap2["ok"] is True and snap2["removed"] == []
    finally:
        db.close()


def test_unban_writes_tombstone_visible_via_since():
    db = SessionLocal()
    try:
        db.query(ArtistBan).filter_by(user_id=UID).delete()
        db.commit()
        _taste.sync_from_mobile(db, UID, {"source": "desktop", "ratings": [],
            "profile": {"bannedArtists": [{"id": "tomb-artist-1", "name": "Tomb Artist"}]},
            "events": []})
        snap0 = _taste.sync_to_mobile(db, UID)
        assert any(b["name"] == "Tomb Artist" for b in snap0["bannedArtistsDetailed"])
        cur = snap0["server_now"]
        unban_artist(UID, _P(artist_name="Tomb Artist", artist_id=""), db=db)
        snap1 = _taste.sync_to_mobile(db, UID, since=cur)
        assert snap1["bannedArtists"] == []
        kinds = {(r["kind"], r["key"]) for r in snap1["removed"]}
        assert ("ban", "Tomb Artist") in kinds, snap1["removed"]
    finally:
        db.close()


def test_old_tombstones_pruned():
    db = SessionLocal()
    try:
        db.add(TasteTombstone(user_id=UID, kind="like", key="ancient",
                              deleted_at=datetime.utcnow() - timedelta(days=31)))
        db.commit()
        _taste.sync_to_mobile(db, UID)
        left = db.query(TasteTombstone).filter_by(
            user_id=UID, kind="like", key="ancient").count()
        assert left == 0
    finally:
        db.close()
