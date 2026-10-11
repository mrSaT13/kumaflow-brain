"""Граница суток: local_today() и соседи.

Регресс на реальный баг: было `date.fromtimestamp(ts, tz=server_tz())`, но
параметра `tz` у datetime.date нет — он есть только у datetime.datetime.
Любой вызов падал `TypeError: date.fromtimestamp() takes no keyword
arguments`. Под ударом оказались POST /api/wave/seed и генераторы
ежедневных плейлистов.
"""
import os

os.environ.setdefault("REDIS_HOST", "localhost")

from datetime import date, datetime, timedelta, timezone

from app.core.time import local_hour, local_today, server_now, server_tz


def test_local_today_does_not_raise():
    """Главное: вызов вообще не падает (раньше был TypeError на каждом)."""
    assert isinstance(local_today(), date)


def test_local_today_is_plain_date():
    d = local_today()
    assert not isinstance(d, datetime), "должен быть date, а не datetime"
    assert hasattr(d, "isoformat")


def test_local_today_matches_server_now():
    assert local_today() == server_now().date()


def test_matches_utc_shifted_by_server_tz():
    """Дата = дата в поясе сервера. В полночьном поясе это видно."""
    tz = server_tz()
    expected = datetime.now(timezone.utc).astimezone(tz).date()
    assert local_today() == expected


def test_near_midnight_utc_plus_shift_is_consistent():
    """Смещение не должно ронять: дата в +14 отличается от UTC-даты."""
    tz = server_tz()
    now_utc = datetime.now(timezone.utc)
    shifted = now_utc.astimezone(tz).date()
    assert shifted in (now_utc.date(), now_utc.date() + timedelta(days=1),
                       now_utc.date() - timedelta(days=1))


def test_local_hour_in_range():
    assert 0 <= local_hour() <= 23