"""Этап 2: числовое муд-пространство.

Пилюля настроения — это ТОЧКА в пространстве звука (valence / energy /
arousal / dance) плюс радиус, а не строка-лейбл. Что здесь проверяется:

  * трек без анализа НЕ выпадает из волны: его вектор выводится из жанра,
    и он может попасть в пачку, пока идёт анализ — но с честной
    уверенностью (меньший бонус, без штрафа «не подтверждает»);
  * звук важнее слов: «боевая песня о любви» (лейбл от LLM про текст) в
    «Боевое» не проходит, а громкий трек со словом «спокойный» — проходит;
  * переход настроения стал ЛЕСТНИЦЕЙ (от звука текущего трека к центру
    целевой пилюли), а не «голова — стартовое, хвост — целевое»;
  * разнообразие пачки: кап артиста/жанра — но без ухудшения качества
    (кап не должен вытаскивать в пачку заведомо худшие треки).
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, Track, TrackFeatures
from app.services import wave as _wave

# ---------------- мок-фичи (без БД) ----------------


class _F:
    """Мини-TrackFeatures: только то, что читает wave."""

    def __init__(self, energy=None, valence=None, arousal=None,
                 danceability=None, tempo=None, labels=None):
        self.energy = energy
        self.valence = valence
        self.arousal = arousal
        self.danceability = danceability
        self.tempo_bpm = tempo
        self.mood_labels = labels


# ---------------- пространство ----------------

def test_every_preset_has_numeric_target():
    """У каждой пилюли есть центр в муд-пространстве.

    Иначе пилюля снова становится строкой: «счастливое» и «бодрое» —
    синонимы, а переход к настроению — скачок.
    """
    for code in _wave.MOOD_PRESETS:
        target = _wave.MOOD_TARGETS.get(code)
        assert target, code
        for axis in _wave.MOOD_AXES:
            assert axis in target, (code, axis)
            assert 0.0 <= target[axis] <= 1.0, (code, axis)


def test_mood_vector_reads_four_axes():
    v = _wave.mood_vector(_F(energy=0.8, valence=0.4, arousal=0.7,
                              danceability=0.3))
    assert v == {"valence": 0.4, "energy": 0.8, "arousal": 0.7, "dance": 0.3}
    # Нет анализа — пустой вектор (не «нули»: нули — это данные).
    assert _wave.mood_vector(None) == {}
    assert _wave.mood_vector(_F()) == {}


def test_mood_vector_clamps_out_of_range():
    v = _wave.mood_vector(_F(energy=1.4, valence=-0.2))
    assert v["energy"] == 1.0
    assert v["valence"] == 0.0


def test_mood_vec_dist_basics():
    a = {"valence": 0.5, "energy": 0.5, "arousal": 0.5, "dance": 0.5}
    b = {"valence": 0.5, "energy": 0.5, "arousal": 0.5, "dance": 0.5}
    assert _wave.mood_vec_dist(a, b) == 0.0
    c = {"valence": 0.9, "energy": 0.9, "arousal": 0.9, "dance": 0.9}
    assert _wave.mood_vec_dist(a, c) > 0.3


def test_mood_vec_dist_ignores_missing_axes():
    """Трек без arousal не штрафуется за отсутствие координаты."""
    full = {"valence": 0.5, "energy": 0.5, "arousal": 0.5, "dance": 0.5}
    partial = {"energy": 0.5}
    assert _wave.mood_vec_dist(partial, full) == 0.0
    # А если различается то, что есть — расстояние ненулевое.
    assert _wave.mood_vec_dist({"energy": 0.9}, full) > 0.0


def test_mood_fit_center_radius():
    target = {"valence": 0.5, "energy": 0.5, "arousal": 0.5, "dance": 0.5}
    fit, dist = _wave.mood_fit(target, target, 0.3)
    assert fit == 1.0 and dist == 0.0
    # За радиусом — ноль (дальше «не то»).
    far = {"valence": 0.8, "energy": 0.8, "arousal": 0.8, "dance": 0.8}
    fit2, dist2 = _wave.mood_fit(far, target, 0.3)
    assert dist2 > 0.3
    assert fit2 == 0.0
    # Пустой вектор — нейтрально, без ошибки.
    assert _wave.mood_fit({}, target, 0.3)[0] == 0.0


def test_genre_vector_map_is_mean_of_analyzed():
    feats = {
        "t1": _F(energy=0.8, valence=0.4, arousal=0.8, danceability=0.5),
        "t2": _F(energy=0.6, valence=0.2, arousal=0.6, danceability=0.3),
    }
    genres = {"t1": "rock", "t2": "rock", "t3": ""}
    gv = _wave.genre_vector_map(feats, genres)
    assert "rock" in gv
    assert abs(gv["rock"]["energy"] - 0.7) < 1e-6
    assert abs(gv["rock"]["valence"] - 0.3) < 1e-6
    # Трек без жанра в карту не попадает.
    assert "" not in gv


def test_mood_vectors_for_infers_unanalyzed_by_genre():
    """Трек без анализа получает вектор жанра — он НЕ «немой»."""
    feats = {"t1": _F(energy=0.9, valence=0.3, arousal=0.9, danceability=0.5)}
    genres = {"t1": "rock", "t2": "rock"}
    vecs, gvecs = _wave.mood_vectors_for(feats, genres)
    assert "rock" in gvecs
    assert vecs["t1"]["energy"] == 0.9
    assert vecs["t2"]["energy"] == 0.9
    assert vecs["t2"] is not vecs["t1"]


def test_mood_vectors_for_keeps_own_vector():
    feats = {"t1": _F(energy=0.9, valence=0.3),
             "t2": _F(energy=0.2, valence=0.8)}
    genres = {"t1": "rock", "t2": "rock"}
    vecs, _ = _wave.mood_vectors_for(feats, genres)
    assert vecs["t1"]["energy"] == 0.9
    assert vecs["t2"]["energy"] == 0.2


# ---------------- фильтр пилюли ----------------

def test_filter_lets_unanalyzed_genre_decide():
    want = set(_wave.MOOD_PRESETS["aggressive"]["moods"])
    target = _wave.MOOD_TARGETS["aggressive"]
    # Жанр из таблицы -> решает.
    assert _wave.mood_filter_passes(None, "rock", want, target)
    # Жанр из таблицы, но чужой настроению -> не проходит.
    assert not _wave.mood_filter_passes(None, "ambient", want, target)
    # Неизвестный жанр -> пропуск: фильтр не знает, что сказать, и выкидывать
    # «немые» треки нельзя (пока идёт анализ, они должны оставаться в игре).
    assert _wave.mood_filter_passes(None, "что-то незнакомое", want, target)
    assert _wave.mood_filter_passes(None, None, want, target)


def test_filter_sound_beats_llm_word():
    """Слово от LLM — про текст песни; звук — про музыку."""
    want = set(_wave.MOOD_PRESETS["aggressive"]["moods"])
    target = _wave.MOOD_TARGETS["aggressive"]
    limits = (_wave.MOOD_PRESETS["aggressive"],)
    # Громкий трек, но слово чужое ('спокойный') -> проходит по ЗВУКУ.
    loud = _F(energy=0.9, valence=0.3, arousal=0.9, danceability=0.6,
              tempo=140, labels=["спокойный"])
    assert _wave.mood_filter_passes(loud, "rock", want, target)
    # Тихий трек, но слово 'боевой' (слово LLM) -> не проходит: рамки
    # пресета (энергия/темп) говорят «это не боевой звук».
    quiet = _F(energy=0.2, valence=0.5, arousal=0.2, danceability=0.3,
               tempo=80, labels=["боевой"])
    assert not _wave.mood_filter_passes(quiet, "hip-hop", want, target,
                                        limits=limits)


def test_filter_respects_numeric_limits():
    """Рамки пресета — coarse-фильтр поверх звука."""
    want = set(_wave.MOOD_PRESETS["aggressive"]["moods"])
    target = _wave.MOOD_TARGETS["aggressive"]
    limits = (_wave.MOOD_PRESETS["aggressive"],)
    loud = _F(energy=0.9, valence=0.3, arousal=0.9, danceability=0.6,
              tempo=140, labels=["агрессивный"])
    assert _wave.mood_filter_passes(loud, "rock", want, target,
                                    limits=limits)
    # Темп ниже порога — не проходит, даже если всё остальное про пилюлю.
    slow = _F(energy=0.9, valence=0.3, arousal=0.9, danceability=0.6,
              tempo=90, labels=["агрессивный"])
    assert not _wave.mood_filter_passes(slow, "rock", want, target,
                                        limits=limits)


def test_filter_off_without_pill():
    """Нет пилюли — фильтр выключен, всё проходит."""
    assert _wave.mood_filter_passes(_F(energy=0.99), "rock", set(), None)
    assert _wave.mood_filter_passes(None, "ambient", set(), None)


# ---------------- разнообразие пачки ----------------

def _item(tid, artist="A", genre="g", score=1.0, mood_code=None):
    return {"track_id": tid, "artist_name": artist, "genre": genre,
            "score": score, "mood_code": mood_code}


def test_select_diverse_caps_artist():
    ranked = [_item(f"a{i}", artist="Same") for i in range(6)] + \
             [_item(f"b{i}", artist="Other", genre="g2") for i in range(6)]
    out = _wave.select_diverse(ranked, 6, artist_cap=2, genre_cap=0)
    same = [r for r in out if r["artist_name"] == "Same"]
    other = [r for r in out if r["artist_name"] == "Other"]
    assert len(same) <= 2, same
    assert len(other) >= 4, other
    assert len(out) == 6


def test_select_diverse_caps_genre():
    ranked = [_item(f"a{i}", artist=f"AR{i}", genre="rock") for i in range(8)] + \
             [_item(f"b{i}", artist=f"AP{i}", genre="pop") for i in range(8)]
    out = _wave.select_diverse(ranked, 8, artist_cap=0, genre_cap=4)
    rocks = [r for r in out if r["genre"] == "rock"]
    assert len(rocks) <= 4, rocks
    assert len(out) == 8


def test_select_diverse_keeps_quality_when_no_alternative():
    """Кап не должен ухудшать пачку.

    Если альтернативы заметно хуже по скору — берём лучшие треки, а не
    «разнообразие любой ценой»: в пачке окажется мусор.
    """
    ranked = [_item(f"a{i}", artist="Same", score=0.9) for i in range(6)] + \
             [_item(f"b{i}", artist="Other", score=0.05) for i in range(6)]
    out = _wave.select_diverse(ranked, 4, artist_cap=2)
    assert len(out) == 4
    assert all(r["score"] >= 0.5 for r in out), out


def test_select_diverse_mood_cap_only_when_asked():
    ranked = [_item(f"a{i}", mood_code="calm", score=0.9) for i in range(6)]
    # Без капа настроения — все спокойные (пилюля выбрана: это замысел).
    out = _wave.select_diverse(ranked, 4, mood_cap=None)
    assert len(out) == 4
    # С капом — не больше 2 от одного кода, остальные добираются.
    ranked2 = ranked + [_item(f"b{i}", mood_code="happy", score=0.8)
                        for i in range(4)]
    out2 = _wave.select_diverse(ranked2, 4, mood_cap=2)
    assert len(out2) == 4


def test_select_diverse_preserves_order_and_size():
    ranked = [_item(f"t{i}", artist=f"A{i}", genre=f"g{i}", score=1.0 - i / 100)
              for i in range(20)]
    out = _wave.select_diverse(ranked, 8)
    assert len(out) == 8
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True)


# ---------------- лестница перехода ----------------

def test_order_by_mood_ramp_is_gradual():
    """Переход — лестница: от звука старта к центру цели, а не «голова/хвост»."""
    items = []
    for i in range(5):
        v = 0.1 + i * 0.2  # valence 0.1 .. 0.9
        items.append({"track_id": f"t{i}", "energy": v,
                      "_vec": {"valence": v, "energy": v,
                               "arousal": v, "dance": v}})
    vec_map = {it["track_id"]: it["_vec"] for it in items}
    start = {"valence": 0.1, "energy": 0.1, "arousal": 0.1, "dance": 0.1}
    target = {"valence": 0.9, "energy": 0.9, "arousal": 0.9, "dance": 0.9}
    out = _wave.order_by_mood_ramp(items, start, target, vec_map)
    assert {r["track_id"] for r in out} == {f"t{i}" for i in range(5)}
    seq = [r["_vec"]["valence"] for r in out]
    assert seq == sorted(seq), seq
    # Первым — самый близкий к старту, последним — к цели.
    assert seq[0] < 0.3 and seq[-1] > 0.7, seq


def test_order_by_mood_ramp_keeps_unknown_at_tail():
    """Трек без вектора в лестницу не встаёт — уходит в хвост."""
    items = [{"track_id": "known", "energy": 0.9,
              "_vec": {"valence": 0.9, "energy": 0.9, "arousal": 0.9,
                       "dance": 0.9}},
             {"track_id": "blind", "energy": 0.5}]
    vec_map = {"known": items[0]["_vec"]}
    start = {"valence": 0.1, "energy": 0.1, "arousal": 0.1, "dance": 0.1}
    target = {"valence": 0.9, "energy": 0.9, "arousal": 0.9, "dance": 0.9}
    out = _wave.order_by_mood_ramp(items, start, target, vec_map)
    assert out[-1]["track_id"] == "blind"


# ---------------- интеграция: БД ----------------

# kind, labels, energy, valence, arousal, dance, tempo, genre, кол-во
_SEED_GROUPS = (
    ("aggr", ["агрессивный"], 0.90, 0.30, 0.90, 0.60, 140.0, "rock", 15),
    # 15 каждой стороны: меньше — и не сработает страховка «фильтр откачен».
    ("quiet", ["спокойный"], 0.20, 0.50, 0.20, 0.30, 80.0, "ambient", 15),
    # слово от LLM («боевой»), но звук спокойный
    ("llmquiet", ["боевой"], 0.20, 0.50, 0.20, 0.30, 80.0, "hip-hop", 3),
    # слово спокойное, но звук боевой
    ("loudquiet", ["спокойный"], 0.90, 0.30, 0.90, 0.60, 140.0, "rock", 3),
    # без анализа совсем — жанр rock, звук должен выводиться из жанра
    ("blind", None, None, None, None, None, None, "rock", 5),
)


def _seed():
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="mood_vector_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="mood_vector_tester",
                      username="mood_vector_tester")
        db.add(u)
    for kind, labels, en, va, ar, da, tempo, genre, n in _SEED_GROUPS:
        for i in range(n):
            ext = f"mvx_{kind}_{i}"
            t = db.query(Track).filter_by(external_id=ext).first()
            if t is None:
                t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                          external_id=ext, title=f"{kind} {i}",
                          artist_name=f"{kind} artist {i}", genre=genre)
                db.add(t)
                db.commit()
            if labels is not None and db.query(TrackFeatures).filter_by(
                    track_id=t.id).first() is None:
                db.add(TrackFeatures(track_id=t.id, tempo_bpm=tempo,
                                     energy=en, valence=va, arousal=ar,
                                     danceability=da, mood_labels=labels))
    db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _ids_of_kind(db, kind):
    return [str(r.id) for r in
            db.query(Track).filter(
                Track.external_id.like(f"mvx_{kind}_%")).all()]


def test_aggressive_pill_excludes_llm_word_quiet_audio():
    """«Боевая песня о любви» в «Боевое» не проходит — слово про текст."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10,
                                settings={"mood": "боевой"})
        got = {t["track_id"] for t in r["tracks"]}
        assert got, "волна не должна быть пустой"
        assert not (got & set(_ids_of_kind(db, "llmquiet"))), \
            "трек со словом LLM, но спокойным звуком попал в «Боевое»"
    finally:
        db.close()


def test_aggressive_pill_keeps_loud_track_with_quiet_word():
    """Громкий трек со словом «спокойный» в «Боевое» проходит — звук решает."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=20,
                                settings={"mood": "aggressive"})
        got = {t["track_id"] for t in r["tracks"]}
        assert got & set(_ids_of_kind(db, "loudquiet")), \
            "звуковой «боевой» трек выпал из «Боевого» из-за слова"
    finally:
        db.close()


def test_unanalyzed_track_is_not_punished_and_gets_fit():
    """Трек без анализа не штрафуется и получает оценку по жанру.

    Пока идёт анализ, такой трек должен оставаться в игре: он получает
    вектор жанра (с пониженной уверенностью), а не «штраф за
    неподтверждённое настроение».
    """
    db = SessionLocal()
    try:
        cands = _ids_of_kind(db, "blind") + _ids_of_kind(db, "aggr")
        ranked = _wave.score_candidates(db, UID, cands, [],
                                        settings={"mood": "aggressive"})
        by_id = {r["track_id"]: r for r in ranked}
        blind = _ids_of_kind(db, "blind")
        assert blind, "нет треков без анализа"
        picked = [by_id[b] for b in blind if b in by_id]
        assert picked, "треки без анализа выпали из скоринга совсем"
        for r in picked:
            assert r["comp"]["mood_fit"] > 0, r
            assert r["comp"]["mood_pen"] == 0, r
    finally:
        db.close()


def test_wave_exposes_numeric_target():
    """Ответ отдаёт, ЧТО означает выбранная пилюля в числах."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=5,
                                settings={"mood": "aggressive"})
        assert r["mood_target"] == _wave.MOOD_TARGETS["aggressive"]
        assert r["mood_radius"] == _wave.MOOD_RADIUS
        for t in r["tracks"]:
            assert "valence" in t and "arousal" in t and "dance" in t
    finally:
        db.close()


def test_morph_uses_ramp():
    """Смена настроения на ходу — лестница, а не бинарный «голова/хвост»."""
    db = SessionLocal()
    try:
        quiet_id = _ids_of_kind(db, "quiet")[0]
        r = _wave.wave_continue(db, UID, queue=[], current_track_id=quiet_id,
                                count=10, settings={"mood": "aggressive"})
        assert r["morph"], "переход не распознан"
        assert r["morph"].get("ramp") is True, r["morph"]
        assert r["morph"]["from"] == "calm"
        assert r["morph"]["to"] == "aggressive"
    finally:
        db.close()


def test_batch_has_no_artist_monoculture():
    """В пачке не должно быть 8 треков одного артиста."""
    db = SessionLocal()
    try:
        r = _wave.wave_continue(db, UID, queue=[], count=10,
                                settings={"mood": "aggressive"})
        artists = [t.get("artist_name") for t in r["tracks"]]
        assert artists
        top = max(artists.count(a) for a in set(artists))
        assert top <= 2, artists
    finally:
        db.close()
