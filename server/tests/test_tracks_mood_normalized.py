"""Регрессия: цифра в дропдауне настроений == выдаче фильтра.

Баг: каталог /moods считал strip().lower() (и только первые 3 метки),
а фильтр /api/tracks?mood= искал JSON-LIKE подстроку в кавычках —
метки с пробелами/регистром (« Агрессивный ») в каталоге были, а фильтр
давал 0. Плюс метки дальше 3-й позиции каталог не считал вовсе.
"""
import uuid

from app.api.library import invalidate_moods_cache, list_moods
from app.api.tracks import list_tracks
from app.db.database import SessionLocal
from app.db.models import MediaServer, Track, TrackFeatures


def _mk_track(db, srv, ext, title, moods):
    t = db.query(Track).filter_by(server_id=srv.id, external_id=ext).first()
    if t is None:
        t = Track(id=str(uuid.uuid4()), server_id=srv.id,
                  external_id=ext, title=title, artist_name="A")
        db.add(t)
        db.flush()
    f = db.get(TrackFeatures, str(t.id))
    if f is None:
        db.add(TrackFeatures(track_id=str(t.id), mood_labels=moods))
    else:
        f.mood_labels = moods
    return t


def test_mood_filter_matches_catalog_despite_dirty_labels():
    invalidate_moods_cache()
    db = SessionLocal()
    try:
        # Точные счётчики — чистим чужие сиды внутри своей транзакции
        # (см. test_tracks_mood.py: wave-тесты сеют свои лейблы в ту же БД).
        db.query(TrackFeatures).delete()
        db.query(Track).delete()
        db.commit()
        srv = db.query(MediaServer).filter_by(name="mood-norm").first()
        if srv is None:
            srv = MediaServer(id=str(uuid.uuid4()), type="navidrome",
                              name="mood-norm", url="http://x", enabled=True)
            db.add(srv)
            db.flush()
        # грязные legacy-метки: пробелы + регистр (миграции данных нет —
        # фильтр обязан находить их как есть)
        _mk_track(db, srv, "n-0", "T0", [" Агрессивный ", "ТАНЦЕВАЛЬНЫЙ"])
        # метка за пределами старой [:3]-нарезки каталога
        _mk_track(db, srv, "n-1", "T1",
                  ["спокойный", "тихий", "мягкий", "тёплый", "агрессивный"])
        db.commit()

        r = list_tracks(mood="агрессивный", limit=100, offset=0, db=db)
        assert r["total"] == 2, r
        r2 = list_tracks(mood="  АГРЕССИВНЫЙ  ", limit=100, offset=0, db=db)
        assert r2["total"] == 2, r2

        cat = list_moods(db=db)
        counts = {m["name"]: m["count"] for m in cat["moods"]}
        # каталог считает те же нормализованные метки по всем позициям и
        # сворачивает их в канонические коды
        assert counts.get("aggressive", 0) >= 2, counts
        assert counts.get("dance", 0) >= 1, counts
        assert counts.get("warm", 0) >= 1, counts
        # счётчик в дропдауне == выдаче фильтра по тому же коду
        r3 = list_tracks(mood="aggressive", limit=100, offset=0, db=db)
        assert r3["total"] == counts.get("aggressive", 0), (r3["total"], counts)
    finally:
        db.close()
        invalidate_moods_cache()
