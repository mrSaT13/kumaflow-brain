"""Фильтр треков по настроению + справочник настроений. Живая sqlite."""
import uuid as _uuid

from app.api.library import list_moods
from app.api.tracks import list_tracks
from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, Track, TrackFeatures


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="mood-t").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome",
                          name="mood-t", url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    specs = [
        ("m-0", "Песня А", ["энергичный", "танцевальный"]),
        ("m-1", "Песня Б", ["спокойный"]),
        ("m-2", "Песня В", None),
    ]
    for ext, title, moods in specs:
        t = db.query(Track).filter_by(server_id=srv.id, external_id=ext).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=ext, title=title, artist_name="A")
            db.add(t)
            db.commit()
        if moods is not None and db.get(TrackFeatures, str(t.id)) is None:
            db.add(TrackFeatures(track_id=str(t.id), mood_labels=moods))
            db.commit()
    db.close()


def test_mood_filter_and_catalog():
    _seed()
    db = SessionLocal()
    try:
        r = list_tracks(mood="энергичный", limit=100, offset=0, db=db)
        assert r["total"] == 1
        assert r["items"][0]["title"] == "Песня А"
        r2 = list_tracks(mood="спокойный", limit=100, offset=0, db=db)
        assert r2["total"] == 1
        r3 = list_tracks(mood="тёмный", limit=100, offset=0, db=db)
        assert r3["total"] == 0
        cat = list_moods(db=db)
        names = {m["name"] for m in cat["moods"]}
        assert {"энергичный", "спокойный"} <= names
    finally:
        db.close()
