"""«Спасатель запроса»: голос → текст → кандидаты, и самообучение без тыков.

Проверяем ровно тот случай, с которого началась затея: ASR пишет «Эминем»
кириллицей, в библиотеке «Eminem» — обычный поиск не находит, а мозг находит.
"""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid

from fastapi.testclient import TestClient

from app.db import models
from app.db.database import SessionLocal
from app.main import app
from app.services import query_learning as ql
from app.services import query_resolver as qr
from app.services.text_tools import norm_key, phonetic_key, translit_ru_en

client = TestClient(app)


def _seed_artist(artist_name="Eminem", title="Lose Yourself"):
    """Артист в библиотеке (как её видит мозг)."""
    db = SessionLocal()
    try:
        srv = db.query(models.MediaServer).first()
        if srv is None:
            srv = models.MediaServer(type="navidrome", name="t", url="http://x")
            db.add(srv)
            db.flush()
        a = models.Artist(server_id=srv.id, external_id="ar-" + uuid.uuid4().hex[:8],
                          name=artist_name)
        db.add(a)
        db.flush()
        t = models.Track(server_id=srv.id, external_id="tr-" + uuid.uuid4().hex[:8],
                         title=title, artist_name=artist_name,
                         artist_external_id=a.external_id)
        db.add(t)
        db.flush()
        db.commit()
        qr.invalidate_cache()
        return str(a.id)
    finally:
        db.close()


# --- примитивы -------------------------------------------------------------

def test_translit_ru_en():
    assert translit_ru_en("Эминем") == "eminem"
    assert translit_ru_en("Еминем") == "eminem"
    assert translit_ru_en("Eminem") == "eminem"


def test_phonetic_key_same_for_spellings():
    assert phonetic_key("Эминем") == phonetic_key("Eminem") == phonetic_key("еминем")


def test_norm_key_strips_feat_and_punctuation():
    assert norm_key("  Eminem (feat. Dr. Dre) ") == "eminem"
    assert norm_key("Eminem feat. Dr. Dre") == "eminem"
    assert norm_key("Eminem!") == "eminem"


# --- скоринг ---------------------------------------------------------------

def test_score_translit_is_confident():
    sc, why = qr.score_pair("Эминем", "Eminem")
    assert sc >= qr.GOOD_ENOUGH
    assert why in ("транслитерация", "точное совпадение")


def test_score_phonetic_saves_missing_letter():
    # «эминм» — выпала буква: транслит не совпадает, но звучит так же.
    sc, why = qr.score_pair("эминм", "Eminem")
    assert sc >= qr.GOOD_ENOUGH
    assert why == "звучит одинаково"


def test_score_unrelated_is_low():
    sc, _ = qr.score_pair("Эминем", "Кирпичи")
    assert sc < 0.5


# --- резолвер --------------------------------------------------------------

def test_resolve_finds_artist_by_cyrillic_speech():
    _seed_artist("Eminem")
    db = SessionLocal()
    try:
        res = qr.resolve("Эминем", db, kind="artist", top_k=5)
        assert res["ok"] is True
        assert res["best"] is not None
        assert res["best"]["name"] == "Eminem"
        assert qr.is_confident(res) is True
    finally:
        db.close()


def test_resolve_uses_learned_alias_first():
    _seed_artist("Eminem")
    db = SessionLocal()
    try:
        ql.confirm_alias(db, "эминем", "Eminem", kind="artist")
        res = qr.resolve("Эминем", db, kind="artist", top_k=5)
        assert res["best"]["name"] == "Eminem"
        assert res["best"]["why"] == "выучено"
        assert res["best"]["score"] == 1.0
    finally:
        db.close()


def test_resolve_finds_alias_by_similar_spelling():
    """Голос пишет одно слово по-разному — алиас должен сработать и на близкий
    вариант («имейджен драгонс» / «имайджен драгонс»)."""
    db = SessionLocal()
    try:
        ql.confirm_alias(db, "имейджен драгонс", "Imagine Dragons", kind="artist")
        res = qr.resolve("имайджен драгонс", db, kind="artist", top_k=5)
        assert res["best"] is not None
        assert res["best"]["name"] == "Imagine Dragons"
        assert res["best"]["source"] == "alias"
    finally:
        db.close()


# --- самообучение без тыков ------------------------------------------------

def test_auto_learn_resolves_miss_by_itself():
    _seed_artist("Eminem")
    db = SessionLocal()
    try:
        assert ql.note_miss(db, "Эминем", kind="artist", source="voice") is True
        db.commit()
        stats = ql.auto_learn(db)
        assert stats["learned"] >= 1
        res = qr.resolve("Эминем", db, kind="artist", top_k=5)
        assert res["best"]["name"] == "Eminem"
        assert res["best"]["source"] == "alias"
    finally:
        db.close()


def test_miss_is_not_duplicated():
    db = SessionLocal()
    try:
        assert ql.note_miss(db, "какойто несуществующий запрос", kind="artist") is True
        db.commit()
        assert ql.note_miss(db, "какойто несуществующий запрос", kind="artist") is False
        db.commit()
    finally:
        db.close()


def test_learn_from_behavior():
    """Промах + включённый трек этого артиста = мозг понял сам."""
    db = SessionLocal()
    try:
        srv = db.query(models.MediaServer).first()
        if srv is None:
            srv = models.MediaServer(type="navidrome", name="t", url="http://x")
            db.add(srv)
            db.flush()
        u = models.MediaUser(server_id=srv.id, external_id="u-" + uuid.uuid4().hex[:8],
                             username="learner")
        db.add(u)
        db.flush()
        a = models.Artist(server_id=srv.id, external_id="ar-" + uuid.uuid4().hex[:8],
                          name="Eminem")
        db.add(a)
        db.flush()
        t = models.Track(server_id=srv.id, external_id="tr-" + uuid.uuid4().hex[:8],
                         title="Stan", artist_name="Eminem",
                         artist_external_id=a.external_id)
        db.add(t)
        db.flush()
        db.add(models.PlayEvent(user_id=str(u.id), track_id=str(t.id), action="play"))
        db.commit()

        assert ql.note_miss(db, "Эминем", user_id=str(u.id), kind="artist") is True
        db.commit()
        stats = ql.learn_from_behavior(db)
        assert stats["learned"] >= 1
        res = qr.resolve("Эминем", db, user_id=str(u.id), kind="artist", top_k=5)
        assert res["best"]["name"] == "Eminem"
    finally:
        db.close()


# --- API -------------------------------------------------------------------

def test_api_resolve_query_and_confirm():
    _seed_artist("Eminem")
    r = client.post("/api/resolve/query", json={"q": "Эминем", "kind": "artist",
                                                 "source": "voice"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["best"]["name"] == "Eminem"
    assert body["confident"] is True

    r2 = client.post("/api/resolve/confirm",
                     json={"q": "эминем", "name": "Eminem", "kind": "artist"})
    assert r2.status_code == 200
    assert r2.json()["ok"] is True

    r3 = client.get("/api/resolve/aliases")
    assert r3.status_code == 200
    aliases = r3.json()["aliases"]
    assert any(a["alias"] == "эминем" and a["target"] == "Eminem" for a in aliases)


def test_api_resolve_miss_is_recorded():
    r = client.post("/api/resolve/query", json={"q": "квквкв несуществующий артист",
                                                 "kind": "artist", "source": "voice"})
    assert r.status_code == 200
    body = r.json()
    assert body["confident"] is False
    misses = client.get("/api/resolve/misses").json()["misses"]
    assert any(m["query"] == "квквкв несуществующий артист" for m in misses)


# --- треки и альбомы --------------------------------------------------------

def _seed_track(title="Lose Yourself", artist="Eminem", album="The Eminem Show"):
    db = SessionLocal()
    try:
        srv = db.query(models.MediaServer).first()
        if srv is None:
            srv = models.MediaServer(type="navidrome", name="t", url="http://x")
            db.add(srv)
            db.flush()
        a = models.Artist(server_id=srv.id, external_id="ar-" + uuid.uuid4().hex[:8],
                          name=artist)
        db.add(a)
        db.flush()
        al = models.Album(server_id=srv.id, external_id="al-" + uuid.uuid4().hex[:8],
                           title=album, artist_name=artist)
        db.add(al)
        db.flush()
        t = models.Track(server_id=srv.id, external_id="tr-" + uuid.uuid4().hex[:8],
                         title=title, artist_name=artist, album_name=album,
                         artist_external_id=a.external_id)
        db.add(t)
        db.flush()
        db.commit()
        qr.invalidate_cache()
    finally:
        db.close()


def test_same_title_by_different_artists_both_visible():
    """Регресс: одно название у разных исполнителей («Venom» у Eminem и
    у Terry McVeagh). Индекс держал по ключу названия только ПЕРВУЮ запись,
    и вторая молча выпадала из поиска: голос «включи эминем веном» уходил
    к чужому «Venom», а настоящего не было в кандидатах вообще."""
    _seed_track(title="Venom", artist="Eminem")
    _seed_track(title="Venom", artist="Terry McVeigh/Project X")
    db = SessionLocal()
    try:
        res = qr.resolve("эминем веном", db, kind="track", top_k=10)
        titles = [(c["title"], c["sub"]) for c in res["candidates"]]
        assert ("Venom", "Eminem") in titles, titles
        assert ("Venom", "Terry McVeigh/Project X") in titles, titles
        best = res["best"]
        assert best["title"] == "Venom" and best["sub"] == "Eminem", best
        assert qr.is_confident(res), res
    finally:
        db.close()


def test_exact_title_beats_phonetic_album():
    """Регресс: «эминем эвил» выигрывал альбом «Funny Karaoke, Vol. 4»
    («эвил»→«vol» звучит, 0.9), вместо трека «Evil» (точное совпадение
    слова, но раньше оно давало 0.8). Точное совпадение должно быть
    ВЫШЕ фонетического."""
    _seed_track(title="Evil", artist="Eminem")
    _seed_track(title="Funny Karaoke, Vol. 4", artist="Eminem")
    db = SessionLocal()
    try:
        res = qr.resolve("эминем эвил", db, kind="any", top_k=10)
        best = res["best"]
        assert best["title"] == "Evil", best
        assert best["sub"] == "Eminem", best
        assert qr.is_confident(res), res
    finally:
        db.close()


def test_resolve_finds_track_by_title():
    _seed_track()
    db = SessionLocal()
    try:
        res = qr.resolve("Lose Yourself", db, kind="track", top_k=5)
        assert res["best"] is not None
        assert "Lose Yourself" in res["best"]["name"]
        assert res["best"]["kind"] == "track"
    finally:
        db.close()


def test_resolve_finds_track_by_cyrillic_title():
    _seed_track(title="Какой-то Трек")
    db = SessionLocal()
    try:
        res = qr.resolve("какойто трек", db, kind="track", top_k=5)
        assert res["best"] is not None
        assert "Какой-то Трек" in res["best"]["name"]
    finally:
        db.close()


def test_resolve_voice_phrases_for_track():
    """ASR коверкает название: «включи лусе юрселф» → трек «Lose Yourself».

    Сравнение должно быть слово к слову (и с чисткой глаголов-паразитов),
    иначе «Lose Yourself — Eminem» одной строкой не находится."""
    _seed_track()
    db = SessionLocal()
    try:
        cases = ("лусе юрселф", "включи лусе юрселф", "лус юарсэлф",
                 "эминем лусе юрселф")
        for q in cases:
            res = qr.resolve(q, db, kind="any", top_k=5)
            best = res["best"]
            assert best is not None, f"{q}: не понял"
            assert best["title"] == "Lose Yourself", f"{q}: {best}"
            assert best["sub"] == "Eminem", f"{q}: {best}"
            assert qr.is_confident(res), f"{q}: неуверенно ({best['score']})"
    finally:
        db.close()


def test_asr_garbage_token_does_not_break_dual_recognition():
    """Регресс: ASR превращает английское «the»/«three» в «зз».

    Запрос «зз дейс грай трек зз энд ис нот зз ансвер» = «Three Days Grace —
    The End Is Not The Answer». Обрывок «зз» тянул среднее по фрагменту вниз
    (усредняли по ВСЕМ токенам), и верное совпадение терялось. Совпавшие
    слова должны оцениваться без учёта обрывков."""
    _seed_track(title="The End Is Not The Answer", artist="Three Days Grace")
    db = SessionLocal()
    try:
        res = qr.resolve("зз дейс грай трек зз энд ис нот зз ансвер",
                         db, kind="any", top_k=10)
        best = res["best"]
        assert best is not None, res
        assert best["title"] == "The End Is Not The Answer", best
        assert best["sub"] == "Three Days Grace", best
        assert qr.is_confident(res), res
    finally:
        db.close()


def test_asr_garbage_token_does_not_break_short_dual():
    """«зз дейс грай трек пэин» → «Pain» (Three Days Grace): обрывок «зз»
    в имени артиста не должен ронять уверенность."""
    _seed_track(title="Pain", artist="Three Days Grace")
    db = SessionLocal()
    try:
        res = qr.resolve("зз дейс грай трек пэин", db, kind="any", top_k=10)
        best = res["best"]
        assert best is not None, res
        assert best["title"] == "Pain", best
        assert best["sub"] == "Three Days Grace", best
        assert qr.is_confident(res), res
    finally:
        db.close()


def test_resolve_dual_artist_and_track():
    """Двойное распознавание: «включи эминем веном» → Venom (Eminem).

    Один запрос содержит и артиста, и название: фрагменты должны
    независимо лечь на свои поля. По отдельности «веном» и без того
    находилось фонетикой, парой — раньше падало в «неуверенно»."""
    _seed_track(title="Venom")
    db = SessionLocal()
    try:
        for q in ("включи эминем веном", "веном эмайнем", "эминем веном",
                  "эминем трк веном"):
            res = qr.resolve(q, db, kind="any", top_k=5)
            best = res["best"]
            assert best is not None, f"{q}: не понял"
            assert best["title"] == "Venom", f"{q}: {best}"
            assert best["sub"] == "Eminem", f"{q}: {best}"
            assert qr.is_confident(res), f"{q}: неуверенно ({best['score']})"
            assert res["best"]["why"] == "артист и трек", f"{q}: {best['why']}"
    finally:
        db.close()


def test_resolve_dual_words_of_title_not_confused():
    """Оба слова запроса из названия — двойное распознавание не должно
    считать «план» исполнителем и ломать уверенное совпадение по словам."""
    _seed_track(title="God's Plan", artist="Drake")
    db = SessionLocal()
    try:
        res = qr.resolve("богс план", db, kind="track", top_k=5)
        best = res["best"]
        assert best is not None and best["title"] == "God's Plan"
        assert best["why"] != "артист и трек", best
    finally:
        db.close()


def test_resolve_track_candidate_has_raw_title_and_sub():
    """Клиент ищет по названию в каталоге — ему нужно «чистое» имя,
    а «Название — Исполнитель» остаётся показом для человека."""
    _seed_track()
    db = SessionLocal()
    try:
        res = qr.resolve("Lose Yourself", db, kind="any", top_k=5)
        best = res["best"]
        assert best is not None
        assert best["title"] == "Lose Yourself"
        assert best["sub"] == "Eminem"
        assert "Eminem" in best["name"]  # name по-прежнему показ человеку
    finally:
        db.close()


def test_unrelated_song_is_not_matched():
    """Мусор («отдыхай», «таймер на пять минут») не должен находить ничего."""
    _seed_track()
    db = SessionLocal()
    try:
        for q in ("отдыхай", "таймер на пять минут"):
            res = qr.resolve(q, db, kind="any", top_k=5)
            best = res["best"]
            assert best is None or not qr.is_confident(res), f"{q}: {best}"
    finally:
        db.close()


def test_resolve_finds_album():
    _seed_track()
    db = SessionLocal()
    try:
        res = qr.resolve("The Eminem Show", db, kind="album", top_k=5)
        assert res["best"] is not None
        assert res["best"]["kind"] == "album"
        assert "Eminem Show" in res["best"]["name"]
    finally:
        db.close()


def test_resolve_any_kind_searches_everything():
    _seed_track()
    db = SessionLocal()
    try:
        res = qr.resolve("Lose Yourself", db, kind="any", top_k=5)
        assert res["best"] is not None
        assert res["best"]["kind"] in ("track", "album", "artist")
    finally:
        db.close()


# --- ИИ-предложения ---------------------------------------------------------

def test_ai_suggest_uses_model_and_parses_json(monkeypatch):
    from app.services import query_ai

    db = SessionLocal()
    try:
        _seed_track()
        monkeypatch.setattr("app.services.ai.is_configured", lambda: True)
        monkeypatch.setattr("app.services.ai.chat",
                            lambda **kwargs: '{"suggestions": [{"name": "Eminem", '
                                             '"confidence": 0.9, "why": "похоже"}]}')
        out = query_ai.suggest(db, "эминем", kind="artist")
        assert len(out) == 1
        assert out[0]["name"] == "Eminem"
        assert out[0]["score"] == 0.9
        assert out[0]["source"] == "ai"
    finally:
        db.close()


def test_ai_suggest_without_provider_returns_empty(monkeypatch):
    from app.services import query_ai

    db = SessionLocal()
    try:
        monkeypatch.setattr("app.services.ai.is_configured", lambda: False)
        assert query_ai.suggest(db, "эминем", kind="artist") == []
    finally:
        db.close()


def test_api_decide_accept_creates_alias():
    r = client.post("/api/resolve/decide",
                     json={"query": "квквкв непонятное", "name": "Eminem", "accept": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    aliases = {a["alias"]: a["target"] for a in client.get("/api/resolve/aliases").json()["aliases"]}
    assert aliases.get("квквкв непонятное") == "Eminem"


def test_api_decide_reject_closes_miss():
    client.post("/api/resolve/query", json={"q": "непонятное слово тест", "kind": "artist"})
    r = client.post("/api/resolve/decide",
                     json={"query": "непонятное слово тест", "accept": False})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    misses = client.get("/api/resolve/misses").json()["misses"]
    assert not any(m["query"] == "непонятное слово тест" for m in misses)


# --- правка и массовый импорт ----------------------------------------------

def test_api_delete_alias():
    client.post("/api/resolve/confirm", json={"q": "эминем", "name": "Eminem"})
    assert any(a["alias"] == "эминем" for a in client.get("/api/resolve/aliases").json()["aliases"])
    r = client.delete("/api/resolve/aliases", params={"alias": "эминем"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert not any(a["alias"] == "эминем" for a in client.get("/api/resolve/aliases").json()["aliases"])


def test_api_import_aliases_from_text():
    text = "эминем = Eminem\nпиано гайс = The Piano Guys\n# комментарий\nджем -> Jam\n"
    r = client.post("/api/resolve/import", json={"text": text})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    aliases = {a["alias"]: a["target"] for a in client.get("/api/resolve/aliases").json()["aliases"]}
    assert aliases.get("эминем") == "Eminem"
    assert aliases.get("пиано гайс") == "The Piano Guys"
    assert aliases.get("джем") == "Jam"


def test_parse_alias_text_separators():
    from app.services.query_learning import parse_alias_text

    pairs = parse_alias_text("a = b\nc -> d\ne → f\ng, h\nпустая строка\n")
    got = {left: right for left, right in pairs}
    assert got["a"] == "b"
    assert got["c"] == "d"
    assert got["e"] == "f"
    assert got["g"] == "h"
    assert len(pairs) == 4
