"""Пилюля на плеере даёт и фильтр, и характер: _context_overrides. Без БД."""
from app.services.wave import _context_overrides as f


def test_no_pill_keeps_tuning():
    assert f(None, None, 0.12, [0.03, 0.06, 0.10], 0.08) == \
        (0.12, [0.03, 0.06, 0.10], 0.08)


def test_workout_tightens():
    j, mp, nov = f("workout", None, 0.12, [0.03, 0.06, 0.10], 0.08)
    assert j == 0.04
    assert mp == [0.10, 0.20, 0.30]
    assert nov == 0.02


def test_aggressive_mood_is_workout():
    j, mp, nov = f(None, "aggressive", 0.12, [0.03, 0.06, 0.10], 0.08)
    assert (j, mp, nov) == (0.04, [0.10, 0.20, 0.30], 0.02)


def test_rest_softens_jitter_and_novelty_only():
    j, mp, nov = f("rest", None, 0.12, [0.03, 0.06, 0.10], 0.08)
    assert j == 0.08
    assert nov == 0.04
    assert mp == [0.03, 0.06, 0.10]


def test_stricter_saved_tuning_wins():
    # Сохранённое строже контекстного — побеждает оно, не пилюля.
    assert f("workout", None, 0.02, [0.2, 0.3, 0.4], 0.01) == \
        (0.02, [0.2, 0.3, 0.4], 0.01)


def test_party_is_workout_lite():
    j, mp, nov = f("party", None, 0.12, [0.03, 0.06, 0.10], 0.08)
    assert (j, mp, nov) == (0.06, [0.08, 0.15, 0.25], 0.03)


def test_commute_wakeup_walk_light():
    for act in ("commute", "wakeup", "walk"):
        j, mp, nov = f(act, None, 0.12, [0.03, 0.06, 0.10], 0.08)
        assert j == 0.08, act
        assert nov == 0.04, act
        assert mp == [0.03, 0.06, 0.10], act


def test_work_study_untouched():
    # Фон для работы — характер не трогаем сознательно.
    for act in ("work", "study"):
        assert f(act, None, 0.12, [0.03, 0.06, 0.10], 0.08) == \
            (0.12, [0.03, 0.06, 0.10], 0.08)
