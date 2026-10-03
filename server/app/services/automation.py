"""Флаги автоматизации (БД, без перезапуска и правок compose).

Хранятся в AppSetting.automation dict. Дефолты — в DEFAULTS.
Читают и backend, и worker (одна postgres).
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings

KEY = "automation"

DEFAULTS: dict[str, Any] = {
    # тянуть текст (LRCLIB) сразу после sonic-анализа трека
    "analysis_fetch_lyrics": True,
    # прогонять текст через AI-настроение (moods/ai_*). Без AI-провайдера — no-op
    "analysis_ai_mood": True,
    # авто-отправка созданных автоплейлистов (daily/smart/weekly) в Navidrome
    # после генерации. Выкл по дефолту — только локально в мозге.
    "playlists_push_navidrome": False,
    # CLAP включался только через env CLAP_ENABLED + пересоздание контейнеров.
    # Теперь это тумблер в вебе: модель уже запечена в образ, перезапуск не нужен.
    # Дефолт True — иначе у тех, кто никогда не открывал тумблер, CLAP молча
    # выключился бы (env-дефолт в config.py = False).
    "clap_enabled": True,
    "clap_audio_enabled": True,
    # «Сонар» (фингерпринты): recognize для плееров + enroll/dedup.
    # Opt-in админом: enroll качает аудио и считает пики — нагрузка на CPU/сеть.
    "sonar_enabled": False,
    # Фильтр немузыкальных треков в волне (скиты/интерлюдии/интро-аутро).
    # Тумблер в вебе (Автоматизация). Выкл — волна как раньше, со скитами.
    "wave_skip_non_music": True,
}

# Кэш чтения флагов. is_available()/is_audio_available() дёргаются на КАЖДЫЙ
# трек в циклах clap_embed и sonic_analysis, а get_flags() открывает сессию к БД.
# Поэтому кэшируем на TTL — образец из core/time.py.
_TTL_SEC = 20.0
_cache: dict[str, Any] = {"at": 0.0, "flags": None}


def _flags_cached() -> dict[str, Any]:
    import time as _time

    now = _time.time()
    flags = _cache.get("flags")
    if flags is not None and (now - float(_cache.get("at") or 0.0)) < _TTL_SEC:
        return flags  # type: ignore[return-value]
    flags = get_flags()
    _cache["flags"] = flags
    _cache["at"] = now
    return flags


def invalidate_cache() -> None:
    """Сбросить кэш после записи флагов — чтобы backend увидел тумблер сразу."""
    _cache["flags"] = None
    _cache["at"] = 0.0


def clap_enabled(db=None) -> bool:
    """CLAP-поиск по смыслу. Флаг из веба, с фолбэком на CLAP_ENABLED из env."""
    try:
        if db is not None:
            return bool(get_flags(db).get("clap_enabled", True))
        val = bool(_flags_cached().get("clap_enabled", True))
    except Exception:
        val = True
    if val is False:
        # Явное «выключено в вебе» уважаем. Но если флага в БД нет вовсе
        # (настройку не открывали) — берём env, иначе у всех молча выключится.
        stored = _flag_stored("clap_enabled")
        if stored is not None:
            return stored
    try:
        return bool(get_settings().clap_enabled)
    except Exception:
        return val


def clap_audio_enabled(db=None) -> bool:
    try:
        if db is not None:
            return bool(get_flags(db).get("clap_audio_enabled", True))
        val = bool(_flags_cached().get("clap_audio_enabled", True))
    except Exception:
        val = True
    if val is False:
        stored = _flag_stored("clap_audio_enabled")
        if stored is not None:
            return stored
    try:
        return bool(getattr(get_settings(), "clap_audio_enabled", False))
    except Exception:
        return val


def _flag_stored(key: str) -> bool | None:
    """Значение флага в БД или None, если его там ещё не задавали."""
    try:
        from app.db.database import session_scope
        from app.db.models import AppSetting

        with session_scope() as db:
            row = db.get(AppSetting, KEY)
            if row is not None and isinstance(row.value, dict) and key in row.value:
                return bool(row.value.get(key))
    except Exception:
        return None
    return None


def playlists_push_enabled(db=None) -> bool:
    try:
        return bool(get_flags(db).get("playlists_push_navidrome", False))
    except Exception:
        return False


def get_flags(db=None) -> dict[str, Any]:
    flags = dict(DEFAULTS)
    try:
        if db is None:
            from app.db.database import session_scope

            with session_scope() as _db:
                return get_flags(_db)
        from app.db.models import AppSetting

        row = db.get(AppSetting, KEY)
        if row is not None and isinstance(row.value, dict):
            for k, v in row.value.items():
                if k in DEFAULTS:
                    flags[k] = v
    except Exception:
        pass
    return flags


def analysis_fetch_lyrics_enabled(db=None) -> bool:
    try:
        return bool(get_flags(db).get("analysis_fetch_lyrics", True))
    except Exception:
        return True


def analysis_ai_mood_enabled(db=None) -> bool:
    try:
        return bool(get_flags(db).get("analysis_ai_mood", True))
    except Exception:
        return True


def sonar_enabled(db=None) -> bool:
    try:
        return bool(get_flags(db).get("sonar_enabled", False))
    except Exception:
        return False


def wave_skip_non_music_enabled(db=None) -> bool:
    try:
        return bool(get_flags(db).get("wave_skip_non_music", True))
    except Exception:
        return True


def set_flags(patch: dict[str, Any], db=None) -> dict[str, Any]:
    clean = {k: patch[k] for k in DEFAULTS if k in patch}
    if db is None:
        from app.db.database import session_scope

        with session_scope() as _db:
            return set_flags(clean, _db)
    from app.db.models import AppSetting

    row = db.get(AppSetting, KEY)
    merged = get_flags(db)
    merged.update(clean)
    if row is None:
        db.add(AppSetting(key=KEY, value=merged))
    else:
        row.value = merged
    db.commit()
    # Читатели (воркеры) держат TTL-кэш — сбрасываем, чтобы тумблер
    # применился без ожидания и перезапуска контейнеров.
    invalidate_cache()
    return merged
