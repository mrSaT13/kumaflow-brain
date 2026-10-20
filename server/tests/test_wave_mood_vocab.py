"""Настроение волны: открытый словарь -> канонические коды.

Регресс на реальный баг из прод-волны: в плеере выбрано настроение
«боевой», автопродление выдало обычную подборку (лоу-фай рэп, баллады).

Причина была не в плеере и не в анализе, а в словаре:
  * «боевой» — это слово, придуманное LLM по тексту песни
    (lyrics_ai просит «3-5 short mood words in russian»);
  * norm_mood() возвращал неизвестное слово КАК ЕСТЬ -> пресет не
    находился -> жёсткий фильтр строил _want={«боевой»}, оставлял
    2-3 трека из 2000 и ОТКАТЫВАЛСЯ целиком;
  * в результате пилюля «боевой» работала как «настроение не выбрано»,
    а в очередь попадало всё, что мозг не смог классифицировать.

Здесь — контракт: любой лейбл из БД нормализуется в код, у каждого кода
есть пресет, неизвестная пилюля не ломает волну, а жанр без записи в
таблице выводится из данных.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, Track, TrackFeatures
from app.services import wave as _wave

# 15 каждой стороны: меньше — и не сработает страховка «фильтр откачен,
# если осталось меньше пачки» (min_keep = max(10, count)).
_N = 15

# Лейблы, которые реально кладёт анализатор (audio_analysis.mood_from_features).
ANALYZER_LABELS = [
    "энергичный", "спокойный", "танцевальный", "счастливый",
    "меланхоличный", "агрессивный", "мечтательный", "тёплый", "тёмный",
]

# Слова, которые может придумать LLM по тексту песни.
LLM_LABELS = ["боевой", "тревожный", "тоска", "дерзкий", "мрачный",
               "мощный", "оптимистичный", "бодрый", "надрывный", "эпичный"]


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="mood_vocab_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="mood_vocab_tester",
                      username="mood_vocab_tester")
        db.add(u)
    for i in range(_N):
        for kind, labels, energy, tempo, genre in (
            ("aggr", ["агрессивный"], 0.93, 140.0, "rock"),
            ("calm", ["спокойный"], 0.22, 82.0, "ambient"),
            ("llm", ["боевой"], 0.88, 135.0, "hip-hop"),
        ):
            ext = f"mv_{kind}_{i}"
            t = db.query(Track).filter_by(external_id=ext).first()
            if t is None:
                t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                          external_id=ext, title=f"{kind} {i}",
                          artist_name=f"{kind} artist {i}", genre=genre)
                db.add(t)
                db.commit()
            if db.query(TrackFeatures).filter_by(track_id=t.id).first() is None:
                db.add(TrackFeatures(track_id=t.id, tempo_bpm=tempo,
                                     energy=energy, valence=0.6,
                                     danceability=0.5, arousal=energy,
                                     mood_labels=labels))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


# ---------------- нормализация ----------------

def test_llm_words_map_to_codes():
    """Слова LLM обязаны превращаться в канонические коды."""
    assert _wave.norm_mood("боевой") == "aggressive"
    assert _wave.norm_mood("Боевой") == "aggressive"
    assert _wave.norm_mood("  боевой  ") == "aggressive"
    assert _wave.norm_mood("тревожный") == "tense"
    assert _wave.norm_mood("тоска") == "sad"
    assert _wave.norm_mood("мрачный") == "dark"
    assert _wave.norm_mood("дерзкий") == "aggressive"
    assert _wave.norm_mood("мощный") == "energetic"


def test_every_analyzer_label_has_preset():
    """Каждое слово анализатора -> код, и у кода есть пресет.

    Раньше 5 из 9 слов (танцевальный, мечтательный, меланхоличный,
    тёплый, счастливый) не имели пресета: пилюля выбиралась, а фильтр
    молча выключался.
    """
    for lab in ANALYZER_LABELS:
        code = _wave.norm_mood(lab)
        assert code, f"{lab} не нормализуется"
        assert code in _wave.MOOD_PRESETS, f"{lab} -> {code}: нет пресета"


def test_happy_preset_reachable():
    """'счастливый' (муж.р., как кладёт анализатор) и 'счастливое'
    (жен.р., как пилюля UI) — один и тот же код."""
    assert _wave.norm_mood("счастливый") == _wave.norm_mood("счастливое")
    assert _wave.norm_mood("счастливый") == "happy"
    assert "happy" in _wave.MOOD_PRESETS


def test_all_vocab_words_have_preset():
    """Словарь для LLM — из тех же кодов, что и пресеты."""
    for w in _wave.MOOD_VOCAB:
        code = _wave.norm_mood(w)
        assert code, w
        assert code in _wave.MOOD_PRESETS, f"{w} -> {code}: нет пресета"


def test_unknown_mood_is_none_not_itself():
    """Неизвестное слово -> None, а НЕ само по себе.

    Возврат «как есть» ломал всё: пресет не находился, а фильтр
    вырождался в «оставь треки с этим словом» и откатывался целиком.
    """
    assert _wave.norm_mood("какая-то ерунда") is None
    assert _wave.norm_mood("") is None
    assert _wave.norm_mood(None) is None


def test_no_degenerate_family_without_preset():
    """Семейство мудов без пресета — пустое, а не {код}."""
    for code in ("energetic", "happy", "calm", "sad", "dark", "aggressive",
                 "dance", "dreamy", "melancholic", "warm", "tense",
                 "romantic", "epic", "chill", "relaxed"):
        preset = _wave.MOOD_PRESETS.get(code) or {}
        family = set(preset.get("moods") or set())
        assert family, code
        assert code in family or code in ("chill", "relaxed"), code


def test_preset_ranges_are_used():
    """У пресетов есть не только лейблы, но и числовые рамки."""
    assert _wave.MOOD_PRESETS["aggressive"]["energy_min"] >= 0.6
    assert _wave.MOOD_PRESETS["aggressive"]["tempo_min"] >= 120
    assert "valence_min" in _wave.MOOD_PRESETS["happy"]
    assert "dance_min" in _wave.MOOD_PRESETS["happy"]
    assert "arousal_max" in _wave.MOOD_PRESETS["calm"]
    assert "valence_max" in _wave.MOOD_PRESETS["melancholic"]
    assert "energy_max" in _wave.MOOD_PRESETS["warm"]


def test_mood_labels_have_russian_caption():
    """У каждого кода есть подпись — из неё строится пилюля в плеере."""
    for code in _wave.MOOD_PRESETS:
        assert code in _wave.MOOD_LABELS, code


def test_preset_codes_normalize_to_themselves():
    """Канонический код обязан нормализоваться САМ В СЕБЯ.

    Ловушка: 'aggressive' не был в таблице алиасов. Раньше это не мешало
    (norm_mood возвращал «как есть»), а со строгим нормализатором пилюля
    плеера 'aggressive' перестала узнаваться — пресет не находился,
    фильтр выключался, и «Боевое» снова превращалось в «настроение не
    выбрано». Такой баг невозможно заметить вручную — только тестом.
    """
    for code in _wave.MOOD_PRESETS:
        assert _wave.norm_mood(code) == code, code


# ---------------- жанры ----------------

def test_genre_table_covers_common_genres():
    """Жанры, которых не было в таблице, и из-за которых в «Боевое»
    попадал лоу-фай рэп."""
    for g in ("rap", "hip-hop", "pop", "funk", "soul", "r&b", "indie",
              "lo-fi", "chillhop", "drum and bass"):
        assert _wave._genre_mood(g) is not None, g


def test_genre_mood_from_data():
    """Жанр без записи в таблице выводится из ДАННЫХ: муд большинства
    проанализированных треков этого жанра."""
    auto = {"k-pop": "happy", "drum & bass": "energetic"}
    assert _wave._genre_mood("k-pop", auto) == "happy"
    assert _wave._genre_mood("drum & bass", auto) == "energetic"
    # Таблица важнее данных (точнее, она руками выверена).
    assert _wave._genre_mood("rock", {"rock": "calm"}) == "aggressive"
    # Ни таблицы, ни данных -> None (трек проходит, но со штрафом).
    assert _wave._genre_mood("что-то совсем неизвестное", auto) is None


# ---------------- интеграция: wave_continue ----------------

def _titles(db):
    return {str(t.id): (t.title, t.genre) for t in db.query(Track).all()}


def test_aggressive_pill_keeps_aggressive_and_llm_boevoi():
    """Пилюля «боевой» (слово из LLM) даёт боевые треки, а не микс.

    Регресс на исходный баг: «боевой» -> _want={'боевой'} -> пул
    схлопывался -> фильтр откатывался -> в очередь шло всё подряд.
    """
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10,
                                settings={"mood": "боевой"})
        got = [t["track_id"] for t in r["tracks"]]
        assert got, "волна не должна быть пустой"
        meta = _titles(db)
        for tid in got:
            title, _g = meta[tid]
            assert not title.startswith("calm"), \
                f"спокойный трек попал в «боевой»: {title}"
    finally:
        db.close()


def test_aggressive_code_pill_matches_boevoi_pill():
    """Код пилюли и слово из LLM дают одинаковый результат."""
    db = SessionLocal()
    try:
        a = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "aggressive"})
        b = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "боевой"})
        sa = {t["track_id"] for t in a["tracks"]}
        sb = {t["track_id"] for t in b["tracks"]}
        assert sa and sa == sb
    finally:
        db.close()


def test_unknown_pill_does_not_break_wave():
    """Слово, которого мозг не знает, -> волна живая, не пустая."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10,
                                settings={"mood": "какая-то ерунда"})
        assert r["tracks"], "волна не должна пустеть"
    finally:
        db.close()


def test_calm_pill_excludes_aggressive():
    """Обратная сторона: «спокойное» не должно тащить боевые треки."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10,
                                settings={"mood": "спокойное"})
        got = [t["track_id"] for t in r["tracks"]]
        meta = _titles(db)
        for tid in got:
            title, _g = meta[tid]
            assert not title.startswith("aggr"), \
                f"боевой трек попал в «спокойное»: {title}"
    finally:
        db.close()


def test_mood_code_exposed_for_morph_gradient():
    """В ответе есть канонический код настроения.

    Градиент морфа сравнивал сырой лейбл ('спокойный') с каноническим
    кодом ('calm') — сравнение всегда было False, обе группы пусты,
    и «голова — стартовое, хвост — целевое» не строилось НИ РАЗУ.
    """
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10,
                                settings={"mood": "aggressive"})
        seen_code = False
        for t in r["tracks"]:
            assert "mood_code" in t, t
            if t["mood_code"] is None:
                # Трек без анализа — кода у него нет, и это честно.
                continue
            seen_code = True
            assert t["mood_code"] in _wave.MOOD_PRESETS, t
        assert seen_code, "в выдаче не оказалось ни одного трека с кодом"
    finally:
        db.close()


def test_same_pill_same_result():
    """Одна и та же пилюля -> одна и та же выдача."""
    db = SessionLocal()
    try:
        a = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "calm"})
        b = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "calm"})
        sa = {t["track_id"] for t in a["tracks"]}
        sb = {t["track_id"] for t in b["tracks"]}
        assert sa and sa == sb
    finally:
        db.close()
