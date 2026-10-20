"""Этап 3: форма волны — разгон -> вершина -> спад.

Раньше пачка раскладывалась по энергии МОНОТОННО (smooth_energy_order):
сначала тихо, потом громко — и до самого конца. Это «плейлист с плавными
переходами», а не волна. Теперь у пачки есть форма, и точка входа — звук
текущего трека, поэтому стык пачек не рвётся.

Что проверяется:
  * кривая дуги: рост -> вершина -> спад;
  * раскладка пачки по дуге (энергия НЕ монотонна, есть вершина и спад);
  * точка входа: волна продолжается от текущего трека;
  * вершина дуги не выше выбранной пилюли;
  * дуга не ломает волну, когда строить нечего (нет анализа).
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

import pytest

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, Track, TrackFeatures
from app.services import wave as _wave


# ---------------- кривая ----------------

def test_arc_curve_shape():
    """Разгон -> вершина -> спад."""
    c = _wave.wave_arc_curve(11, 0.0, peak_level=1.0, peak_pos=0.6,
                             release=0.5)
    assert len(c) == 11
    assert c[0] == 0.0
    assert abs(max(c) - 1.0) < 1e-9
    assert abs(c[6] - 1.0) < 1e-9  # вершина на 60 % пачки
    assert c[-1] < c[6]
    assert abs(c[-1] - 0.5) < 1e-6  # спад до release * вершины
    # монотонность фаз: рост, потом падение
    assert all(c[i] <= c[i + 1] + 1e-9 for i in range(6))
    assert all(c[i] >= c[i + 1] - 1e-9 for i in range(6, 10))


def test_arc_curve_starts_from_current_level():
    """Волна продолжается от того места, где мы уже находимся."""
    c = _wave.wave_arc_curve(10, 0.6, peak_pos=0.6, release=0.5)
    assert abs(c[0] - 0.6) < 1e-9
    assert max(c) >= 0.6


def test_arc_curve_peak_cannot_be_below_start():
    c = _wave.wave_arc_curve(10, 0.9, peak_level=0.2, peak_pos=0.6,
                             release=0.8)
    assert min(c) >= 0.2 - 1e-9
    assert c[0] >= 0.2 - 1e-9


def test_mood_intensity():
    """Интенсивность — это «громко и бодро», а не «позитивно»."""
    loud = _wave.mood_intensity({"valence": 0.1, "energy": 0.9,
                                 "arousal": 0.9, "dance": 0.5})
    quiet = _wave.mood_intensity({"valence": 0.9, "energy": 0.2,
                                  "arousal": 0.2, "dance": 0.2})
    assert loud > quiet
    # нет координат — нейтрально, без исключения
    assert _wave.mood_intensity({}) == 0.5
    assert _wave.mood_intensity(None) == 0.5


# ---------------- раскладка ----------------

def _pool(n=10):
    items = [{"track_id": f"t{i}", "score": 1.0 - i * 0.01}
             for i in range(n)]
    vecs = {f"t{i}": {"valence": 0.5, "energy": 0.2 + 0.07 * i,
                      "arousal": 0.2 + 0.07 * i, "dance": 0.4}
            for i in range(n)}
    return items, vecs


def test_order_by_wave_arc_has_peak_and_release():
    """Энергия в пачке НЕ монотонна: есть разгон, вершина и спад."""
    items, vecs = _pool()
    out = _wave.order_by_wave_arc(items, vecs)
    assert out is not None
    assert len(out) == len(items)
    assert {r["track_id"] for r in out} == {r["track_id"] for r in items}
    energies = [vecs[r["track_id"]]["energy"] for r in out]
    peak = max(range(len(energies)), key=lambda i: energies[i])
    assert 0 < peak < len(energies) - 1, energies
    assert energies[0] < energies[peak], energies
    assert energies[-1] < energies[peak], energies


def test_order_by_wave_arc_continues_from_current_track():
    """Если сейчас громко — волна не лезет ещё выше, а идёт вниз."""
    items, vecs = _pool()
    loud = {"valence": 0.5, "energy": 0.83, "arousal": 0.83, "dance": 0.4}
    out = _wave.order_by_wave_arc(items, vecs, start_vec=loud)
    assert out is not None
    energies = [vecs[r["track_id"]]["energy"] for r in out]
    assert energies[0] > energies[-1], energies
    assert energies[0] >= 0.8, energies


def test_order_by_wave_arc_peak_is_hottest_track():
    """Вершина дуги — самая горячая трека пула (пилюля задаёт пол, не потолок)."""
    items, vecs = _pool()
    out = _wave.order_by_wave_arc(items, vecs)
    assert out is not None
    ints = [_wave.mood_intensity(vecs[r["track_id"]]) for r in out]
    assert max(ints) == pytest.approx(max(
        _wave.mood_intensity(v) for v in vecs.values()))


def test_order_by_wave_arc_pill_sets_colour():
    """При равной динамике первым идёт трек, ближе к цвету пилюли."""
    items = [{"track_id": "a"}, {"track_id": "b"}, {"track_id": "c"}]
    vecs = {
        # одинаковая динамика, разный цвет
        "a": {"valence": 0.1, "energy": 0.5, "arousal": 0.5, "dance": 0.4},
        "b": {"valence": 0.9, "energy": 0.5, "arousal": 0.5, "dance": 0.4},
        "c": {"valence": 0.5, "energy": 0.9, "arousal": 0.9, "dance": 0.4},
    }
    pill = _wave.MOOD_TARGETS["warm"]  # светлое, тёплое
    out = _wave.order_by_wave_arc(items, vecs, peak_vec=pill)
    assert out is not None
    ids = [r["track_id"] for r in out]
    # вершина — самая горячая трека
    ints = [_wave.mood_intensity(vecs[i]) for i in ids]
    assert ids[ints.index(max(ints))] == "c"
    # из «a»/«b» (одинаковая динамика) первым идёт ближе к цвету пилюли
    assert ids.index("b") < ids.index("a")


def test_order_by_wave_arc_needs_vectors():
    """Нет векторов / однородный пул — дуга не строится (None)."""
    items, vecs = _pool()
    assert _wave.order_by_wave_arc(items, {}) is None
    assert _wave.order_by_wave_arc(items[:2], vecs) is None
    flat = {f"t{i}": {"valence": 0.5, "energy": 0.5, "arousal": 0.5,
                      "dance": 0.5} for i in range(10)}
    assert _wave.order_by_wave_arc(items, flat) is None


# ---------------- интеграция: БД ----------------

# энергия от 0.15 до 0.95 — пул с широким диапазоном динамики
_ARC_SEED = tuple((0.15 + 0.08 * i, "arc_genre_%d" % i) for i in range(11))


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="arc_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="arc_tester", username="arc_tester")
        db.add(u)
    for en, genre in _ARC_SEED:
        ext = "arc_%s" % genre
        t = db.query(Track).filter_by(external_id=ext).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=ext, title=ext,
                      artist_name="arc_artist_%s" % genre, genre=genre)
            db.add(t)
            db.commit()
        if db.query(TrackFeatures).filter_by(track_id=t.id).first() is None:
            db.add(TrackFeatures(
                track_id=t.id, tempo_bpm=90.0 + en * 80.0, energy=en,
                valence=0.5, arousal=en, danceability=0.4,
                mood_labels=["агрессивный"] if en >= 0.5 else ["спокойный"]))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _others(db):
    """Чужие треки (тестовые модули делят один sqlite) — выкидываем."""
    return [str(r[0]) for r in db.query(Track.external_id).all()
            if not str(r[0]).startswith("arc_")]


def test_wave_without_pill_has_form():
    """Без пилюли пачка всё равно имеет форму: разгон -> вершина -> спад."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=11,
                                exclude_ids=_others(db))
        tracks = r["tracks"]
        assert len(tracks) >= 5, tracks
        energies = [t["energy"] for t in tracks
                    if t.get("energy") is not None]
        assert len(energies) >= 5
        peak = max(range(len(energies)), key=lambda i: energies[i])
        assert peak > 0, energies
        assert energies[peak] >= energies[0], energies
        assert energies[peak] > energies[-1], energies
    finally:
        db.close()


def test_wave_with_pill_still_filled():
    """Пилюля не должна ломать волну (дуга внутри пилюли)."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=8,
                                settings={"mood": "боевой"},
                                exclude_ids=_others(db))
        assert r["tracks"], "волна с пилюлей пустая"
        for t in r["tracks"]:
            assert t.get("energy") is not None
    finally:
        db.close()


def test_wave_morph_still_ramp():
    """Морф (смена настроения) — это лестница, дуга его не ломает."""
    db = SessionLocal()
    try:
        quiet = db.query(Track).filter(
            Track.external_id == "arc_arc_genre_0").first()
        r = _wave.wave_continue(db, UID, queue=[],
                                current_track_id=str(quiet.id), count=8,
                                settings={"mood": "aggressive"},
                                exclude_ids=_others(db))
        assert r["morph"], "переход не распознан"
        assert r["morph"].get("ramp") is True, r["morph"]
    finally:
        db.close()
