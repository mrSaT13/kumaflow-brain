"""Настроение волны: код пилюли vs муд-лейблы анализатора.

Регресс на реальный баг: пилюля плеера — это англ. код ('calm'), а
`mood_from_features` кладёт в `TrackFeatures.mood_labels` РУССКИЕ лейблы
('спокойный', 'агрессивный'). Жёсткий mood-фильтр сравнивал их напрямую,
не совпал никогда — и выкидывал из пула ВСЕ проанализированные треки.
В «Засыпаю • Спокойное» оставался ровно тот хвост, о котором мозг ничего
не знает: Papa Roach, Rammstein, Hellowen.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, Track, TrackFeatures
from app.services import wave as _wave

# Муд-лейблы пишет mood_from_features — по-русски. Это не опечатка.
CALM_LABELS = ["спокойный", "мечтательный", "тёплый"]
LOUD_LABELS = ["энергичный", "агрессивный", "танцевальный"]
# 15 каждой стороны: меньше — и не сработает страховка «фильтр откачен,
# если осталось меньше пачки» (min_keep = max(10, count)).
_N = 15


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="mood_codes_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="mood_codes_tester",
                      username="mood_codes_tester")
        db.add(u)
    for i in range(_N):
        for kind, labels, energy, tempo in (
            ("calm", CALM_LABELS, 0.22, 82.0),
            ("loud", LOUD_LABELS, 0.93, 140.0),
        ):
            ext = f"mc_{kind}_{i}"
            t = db.query(Track).filter_by(external_id=ext).first()
            if t is None:
                t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                          external_id=ext, title=f"{kind} {i}",
                          artist_name=f"{kind} artist {i}", genre="rock")
                db.add(t)
                db.commit()
            if db.query(TrackFeatures).filter_by(track_id=t.id).first() is None:
                db.add(TrackFeatures(track_id=t.id, tempo_bpm=tempo,
                                     energy=energy, valence=0.5,
                                     danceability=0.5, arousal=energy,
                                     mood_labels=labels))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


# ---------------- словари ----------------

def test_norm_mood_unifies_pill_and_label():
    # пилюля UI
    assert _wave.norm_mood("Спокойное") == "calm"
    assert _wave.norm_mood("Бодрое") == "energetic"
    assert _wave.norm_mood("Тёмное") == "dark"
    # код плеера
    assert _wave.norm_mood("calm") == "calm"
    assert _wave.norm_mood("  CALM ") == "calm"
    # муд-лейбл анализатора (род. падеж, другой словарь!)
    assert _wave.norm_mood("спокойный") == "calm"
    assert _wave.norm_mood("агрессивный") == "aggressive"
    assert _wave.norm_mood(None) is None
    assert _wave.norm_mood("") is None
    # старое имя живо
    assert _wave._norm_mood("спокойное") == "calm"


def test_preset_lookup_by_player_code():
    # Плеер шлёт 'calm' — пресет обязан находиться.
    assert "calm" in _wave.MOOD_PRESETS
    for code in ("energetic", "happy", "calm", "sad", "dark", "chill"):
        assert code in _wave.MOOD_PRESETS, code
        assert _wave.MOOD_PRESETS[code].get("moods"), code


def test_mood_codes_of_feature():
    class _F:
        mood_labels = ["спокойный", "мечтательный", "тёплый"]

    assert _wave._mood_codes_of(_F()) == {"calm", "dreamy", "warm"}
    assert _wave._mood_of(_F()) in {"calm", "dreamy", "warm"}

    class _Bad:
        mood_labels = None

    assert _wave._mood_codes_of(_Bad()) == set()
    assert _wave._mood_of(_Bad()) is None


def test_calm_family_excludes_aggressive():
    family = set(_wave.MOOD_PRESETS["calm"]["moods"])
    assert "aggressive" not in family
    assert "energetic" not in family
    assert "calm" in family


# ---------------- интеграция: wave_continue ----------------

def _genres_of(db):
    return {str(t.id): (t.genre, t.title) for t in db.query(Track).all()}


def test_wave_continue_calm_keeps_analyzed_tracks():
    """Пиллюля 'calm' от плеера: в докладе спокойные, не громкие."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10,
                                settings={"mood": "calm", "activity": "sleep"})
        got = [t["track_id"] for t in r["tracks"]]
        assert got, "воля не должна быть пустой"
        meta = _genres_of(db)
        loud = {tid for tid, (_, title) in meta.items() if title.startswith("loud")}
        calm = {tid for tid, (_, title) in meta.items() if title.startswith("calm")}
        assert set(got) & loud == set(), "громкий трек в «Спокойное»"
        assert set(got) & calm, "спокойные треки выкинуло фильтром"
        assert all(t.get("mood") for t in r["tracks"]), "муд не отдался"
    finally:
        db.close()


def test_wave_continue_russian_pill_same_result():
    """Веб отдаёт русскую подпись из каталога — результат должен совпасть."""
    db = SessionLocal()
    try:
        a = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "calm"})
        b = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "спокойное"})
        sa = {t["track_id"] for t in a["tracks"]}
        sb = {t["track_id"] for t in b["tracks"]}
        assert sa and sa == sb
    finally:
        db.close()


def test_pills_from_web_ui_still_normalize():
    """Веб отдаёт подпись из каталога (может прийти с '  ' и регистром)."""
    assert _wave.norm_mood(" Спокойный ") == "calm"
    assert _wave.norm_mood("Агрессивный") == "aggressive"
    assert _wave.norm_mood("ТЁПЛЫЙ") == "warm"


def test_wave_continue_falls_back_when_no_match():
    """Слово, под которое нет ни одного трека, — волна всё равно живая.

    Раньше тут было 'peaceful': он попадал в семейство 'calm' и матчился.
    Теперь проверяем настоящий фолбэк — слово, которого мозг не знает вовсе.
    """
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "какая-то ерунда"})
        assert r["tracks"], "очередь не должна пустеть"
    finally:
        db.close()


def test_sleep_activity_is_hard_filter():
    """'Засыпаю' — это не настроение, а темп: энергия/темп режутся жёстко."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"activity": "sleep"})
        got = [t["track_id"] for t in r["tracks"]]
        meta = _genres_of(db)
        loud = {tid for tid, (_, title) in meta.items() if title.startswith("loud")}
        assert not (set(got) & loud), "громкий трек в «Засыпаю»"
    finally:
        db.close()


def test_work_activity_stays_soft():
    """'Работаю' остаётся мягким — жёсткий фильтр тут превратил бы фон в запрет."""
    assert "work" not in _wave.ACTIVITY_HARD
    assert "study" not in _wave.ACTIVITY_HARD
    assert "sleep" in _wave.ACTIVITY_HARD
    assert "rest" in _wave.ACTIVITY_HARD


def test_activity_change_alone_triggers_morph():
    """Смена только занятия — тоже переход.

    Раньше морф определялся исключительно как 'муд текущего трека != муд
    цели'. Смена 'Тренировка' -> 'Засыпаю' при той же пилюле настроения
    морфом не считалась: пачка оставалась полной, и новый контекст был
    слышен через десятки треков. Здесь играет громкий трек (energy 0.93),
    а цель — sleep (energy_max 0.4) — расхождение обязано считаться
    переходом и ужимать пачку.
    """
    db = SessionLocal()
    try:
        loud_id = db.query(Track).filter_by(external_id="mc_loud_0").first().id
        r = _wave.wave_continue(
            db, UID, queue=[], current_track_id=str(loud_id), count=20,
            settings={"activity": "sleep"},
        )
        assert r["adaptive"], "смена занятия должна ужимать пачку"
        assert r["count_effective"] < r["count_requested"], r
    finally:
        db.close()


def test_no_morph_when_activity_already_fits():
    """Уже спокойный трек под 'Засыпаю' переходом не считается."""
    db = SessionLocal()
    try:
        calm_id = db.query(Track).filter_by(external_id="mc_calm_0").first().id
        r = _wave.wave_continue(
            db, UID, queue=[], current_track_id=str(calm_id), count=10,
            settings={"activity": "sleep"},
        )
        assert not r["adaptive"], r["adaptive"]
    finally:
        db.close()


def test_wave_continue_no_mood_keeps_mix():
    """Без пилюли настроения фильтр не включается — микс остаётся миксом."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=20, settings={})
        assert len(r["tracks"]) > 1
        assert r.get("mood_source") in (None, "auto", "none")
    finally:
        db.close()