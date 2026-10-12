"""Кэш подборки «следующих треков» в /api/now-playing.

Виджет поллит каждые 15с, а запрос пересчитывал подборку заново. Кэш на 60с
живёт в двух местах: процессный _NEXT_MEM (быстрый путь) и Redis (общий для
--workers 2). Ключ содержит id текущего трека, поэтому смена трека сама даёт
новый ключ и ручная инвалидация не нужна.

Здесь проверяем именно ключи и границы кэша — что он отдаёт готовое, а что
обязан пересчитать.
"""
import pytest

import app.api.now_playing as _np


@pytest.fixture(autouse=True)
def _clean_mem():
    _np._NEXT_MEM.clear()
    yield
    _np._NEXT_MEM.clear()


@pytest.fixture
def fake(monkeypatch):
    """Подменяем тяжёлую функцию: считаем только факт вызова, не считая скоринг."""
    calls: list = []

    def _fake(db, track_id, user_id, n=5, offset=0, jitter_seed=None):
        calls.append((track_id, user_id, n, offset, jitter_seed))
        return [{"track_id": f"{track_id}@{offset}", "score": 1.0}]

    monkeypatch.setattr(_np, "_next_for_track", _fake)
    # Redis в тестах недоступен — проверяем именно процессный путь.
    monkeypatch.setattr(_np, "_next_cache_get", _np._next_cache_get)
    return calls


def test_second_identical_call_is_cached(fake):
    first = _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None)
    second = _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None)
    assert first == second
    assert len(fake) == 1, "второй запрос обязан взять кэш, а не пересчитать"


def test_other_offset_is_a_different_key(fake):
    """Кнопка «Другие 5» меняет offset, а jitter строится из него — ключ
    обязан отличаться, иначе кнопка листала бы один и тот же набор."""
    _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None)
    _np._next_cached(None, "t1", "u1", n=5, offset=5, jitter_seed=None)
    assert len(fake) == 2
    assert [c[3] for c in fake] == [0, 5]


def test_track_change_invalidates_by_key(fake):
    """Смена трека = новый ключ. Это и есть инвалидация без ручной сброса."""
    _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None)
    _np._next_cached(None, "t2", "u1", n=5, offset=0, jitter_seed=None)
    assert [c[0] for c in fake] == ["t1", "t2"]


def test_user_and_n_are_part_of_key(fake):
    _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None)
    _np._next_cached(None, "t1", "u2", n=5, offset=0, jitter_seed=None)
    _np._next_cached(None, "t1", "u1", n=3, offset=0, jitter_seed=None)
    _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed="xyz")
    assert len(fake) == 4


def test_empty_result_is_not_cached(monkeypatch):
    """Пустой список — это «нечего подсказать», а не «ответ»: его не кладём,
    иначе после появления кандидатов виджет до истечения TTL покажет пустоту."""
    calls: list = []

    def _empty(db, track_id, user_id, n=5, offset=0, jitter_seed=None):
        calls.append(track_id)
        return []

    monkeypatch.setattr(_np, "_next_for_track", _empty)
    _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None)
    _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None)
    assert len(calls) == 2


def test_heavy_exception_degrades_to_empty(monkeypatch):
    """Кэш не должен ронять ответ: сбой скоринга — пустой список, как раньше."""

    def _boom(db, track_id, user_id, n=5, offset=0, jitter_seed=None):
        raise RuntimeError("скоринг упал")

    monkeypatch.setattr(_np, "_next_for_track", _boom)
    assert _np._next_cached(None, "t1", "u1", n=5, offset=0, jitter_seed=None) == []


def test_mem_bounded(fake):
    """Процессный кэш не должен расти бесконечно при смене треков."""
    for i in range(_np._NEXT_MEM_MAX + 50):
        _np._next_cached(None, f"t{i}", "u1", n=5, offset=0, jitter_seed=None)
    assert len(_np._NEXT_MEM) <= _np._NEXT_MEM_MAX