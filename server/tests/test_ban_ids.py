"""Баны по Navidrome-ID + именам: двойной матч, без ломки старого.

Регресс на «дизлайкнутые артисты с плеера лезут в волну»:
десктоп шлёт пары {id, name} (у него в bannedArtists лежат ID),
сервер хранит оба и режет по совпадению имени ИЛИ id.
Старые именные строки и мобила (имена) работают как раньше.
"""
import uuid as _uuid

from app.api.users import ban_artist, unban_artist
from app.db.database import SessionLocal, init_db
from app.db.models import ArtistBan, MediaServer, MediaUser, Track
from app.services.artist_names import (
    backfill_ban_ids,
    is_banned_either,
    load_bans,
)
from app.services import taste as _taste


class _P:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mk_track(db, srv, ext, title, artist, artist_ext):
    t = db.query(Track).filter_by(server_id=srv.id, external_id=ext).first()
    if t is None:
        t = Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                  title=title, artist_name=artist,
                  artist_external_id=artist_ext)
        db.add(t)
        db.commit()
    return t


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="banid-t").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome",
                          name="banid-t", url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="banid_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="banid_tester", username="banid_tester")
        db.add(u)
        db.commit()
    _mk_track(db, srv, "nd-song-1", "Hit One", "Neon Artist", "nd-artist-1")
    _mk_track(db, srv, "nd-song-2", "Hit Two", "Plain Name", "nd-artist-2")
    uid, sid = str(u.id), str(srv.id)
    db.close()
    return uid, sid


UID, _SID = _seed()


def test_sync_stores_id_name_pairs():
    db = SessionLocal()
    try:
        res = _taste.sync_from_mobile(db, UID, {
            "source": "desktop",
            "ratings": [],
            "profile": {"bannedArtists": [
                {"id": "nd-artist-1", "name": "Neon Artist"},
            ]},
            "events": [],
        })
        assert res["ok"] is True
        assert res["profile_banned"] == 1, res
        row = db.query(ArtistBan).filter_by(
            user_id=UID, artist_name="Neon Artist").first()
        assert row is not None
        assert str(row.artist_external_id or "") == "nd-artist-1"
    finally:
        db.close()


def test_legacy_plain_id_string_resolves_via_backfill():
    db = SessionLocal()
    try:
        # как слал старый десктоп: голый ID строкой
        res = _taste.sync_from_mobile(db, UID, {
            "source": "desktop",
            "ratings": [],
            "profile": {"bannedArtists": ["nd-artist-2"]},
            "events": [],
        })
        assert res["ok"] is True
        names, ids = load_bans(db, UID)
        # имя подтянулось с трека, ID лёг в колонку (а не болтается в именах)
        assert "Plain Name" in names
        assert "nd-artist-2" in ids
        assert "nd-artist-2" not in names
    finally:
        db.close()


def test_plain_name_still_works_and_resolves_id():
    db = SessionLocal()
    try:
        _taste.sync_from_mobile(db, UID, {
            "source": "mobile",
            "ratings": [],
            "profile": {"bannedArtists": ["Neon Artist"]},
            "events": [],
        })
        names, ids = load_bans(db, UID)
        assert "Neon Artist" in names
        # именной бан тоже получил ID (wave сможет матчить точнее)
        assert "nd-artist-1" in ids
    finally:
        db.close()


def test_is_banned_either_matrix():
    # по ID, без имени трека вообще
    assert is_banned_either(None, "nd-artist-1", set(), {"nd-artist-1"}) is True
    assert is_banned_either("Someone Else", "nd-x", {"Neon Artist"}, {"nd-artist-1"}) is False
    # по имени с токенами — как раньше
    assert is_banned_either("Dark Polo Gang/GASHI/Capo Plaza", "nd-y",
                            {"GASHI"}, set()) is True
    # диск без ID — только имя
    assert is_banned_either("Neon Artist", None, {"Neon Artist"}, set()) is True
    assert is_banned_either("Neon Artist", None, set(), {"nd-artist-1"}) is False


def test_ban_unban_endpoints_with_id():
    db = SessionLocal()
    try:
        r = ban_artist(UID, _P(artist_name="Guest Star", artist_id="nd-artist-9"),
                       db=db)
        assert r["ok"] is True
        row = db.query(ArtistBan).filter_by(
            user_id=UID, artist_name="Guest Star").first()
        assert row is not None
        assert str(row.artist_external_id or "") == "nd-artist-9"
        # разбан только по ID (имя плеер может не знать)
        r2 = unban_artist(UID, _P(artist_name="", artist_id="nd-artist-9"),
                          db=db)
        assert r2["ok"] is True
        assert db.query(ArtistBan).filter_by(
            user_id=UID, artist_name="Guest Star").first() is None
    finally:
        db.close()


def test_backfill_is_idempotent_and_safe():
    db = SessionLocal()
    try:
        n1 = backfill_ban_ids(db, UID)
        n2 = backfill_ban_ids(db, UID)
        assert n2 == 0  # второй прогон делать нечего
        assert n1 >= 0
    finally:
        db.close()
