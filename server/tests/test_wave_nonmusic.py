"""Фильтр немузыкальных треков волны (скиты/интерлюдии). Чистый, без БД."""
from app.services.wave import _is_non_music_track


def test_bracket_markers():
    assert _is_non_music_track("My Song (skit)") is True
    assert _is_non_music_track("Song [Interlude]") is True
    assert _is_non_music_track("Track (Intro)") is True
    assert _is_non_music_track("Track (OUTRO)") is True
    assert _is_non_music_track("Song (Remix)") is False
    assert _is_non_music_track("Song (live)") is False


def test_standalone_titles():
    assert _is_non_music_track("Skit") is True
    assert _is_non_music_track("Skit 3") is True
    assert _is_non_music_track("Intro") is True
    assert _is_non_music_track("Outro 2") is True
    assert _is_non_music_track("Interlude #4") is True


def test_real_songs_survive():
    assert _is_non_music_track("Song") is False
    assert _is_non_music_track("Skitter") is False
    assert _is_non_music_track("Introduction") is False
    assert _is_non_music_track("Introspective") is False
    assert _is_non_music_track("Interstellar") is False
    assert _is_non_music_track("Prelude in C") is False
    assert _is_non_music_track("") is False
    assert _is_non_music_track(None) is False
