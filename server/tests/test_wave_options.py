"""Занятия волны: коды, алиасы, каталог для плеера."""
from app.services.wave import (
    ACTIVITY_ALIASES,
    ACTIVITY_CATALOG,
    ACTIVITY_PRESETS,
    CHARACTERISTIC_CATALOG,
    LANGUAGE_CATALOG,
    _norm_activity,
)


def test_canonical_codes_resolve():
    for code in ("wakeup", "commute", "work", "workout", "sleep",
                 "study", "party", "walk", "rest"):
        assert code in ACTIVITY_PRESETS, code
        assert _norm_activity(code) == code


def test_russian_aliases():
    assert _norm_activity("просыпаюсь") == "wakeup"
    assert _norm_activity("в дороге") == "commute"
    assert _norm_activity("работаю") == "work"
    assert _norm_activity("тренируюсь") == "workout"
    assert _norm_activity("засыпаю") == "sleep"
    assert _norm_activity("") is None
    assert _norm_activity(None) is None
    # неизвестное — как есть (без бонусов, очередь не пустеет)
    assert _norm_activity("медитация") == "медитация"


def test_catalog_covers_presets():
    codes = {a["code"] for a in ACTIVITY_CATALOG}
    assert set(ACTIVITY_PRESETS) <= codes
    for a in ACTIVITY_CATALOG:
        assert a["label"].strip()
    assert {c["code"] for c in CHARACTERISTIC_CATALOG} == {"favorite", "unfamiliar", "popular"}
    assert {c["code"] for c in LANGUAGE_CATALOG} == {"ru", "foreign", "instrumental"}


def test_options_endpoint_shape():
    from app.api.wave import wave_options
    from app.db.database import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        r = wave_options(db=db)
        assert r["ok"] is True
        assert len(r["activities"]) == len(ACTIVITY_CATALOG)
        assert isinstance(r["moods"], list)
    finally:
        db.close()
