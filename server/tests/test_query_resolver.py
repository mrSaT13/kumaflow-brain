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
