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
