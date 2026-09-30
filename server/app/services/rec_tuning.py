"""Тюнинг рекомендаций (R2.1, фундамент — поведение не меняет).

Два уровня (как зафиксировано в роадмапе: per-user значения + глобал):
- глобал `AppSetting(rec_tuning)` — дефолты/лимиты для всех;
- per-user `AppSetting(rec_tuning:user:<user_id>)` — overlay конкретного
  слушателя (его пишет ручная настройка в профиле и авто-мозг R3).

Эффект для юзера = дефолты <- глобал <- per-user. Скоринг всегда зовёт
get_all(db, user_id) — у каждого своя адаптация под его прослушивание,
а глобалом админ держит рамки. Секция auto — только глобальная
(авто-мозг вкл/выкл целиком по группам, значения при этом всё равно
per-user).

Дефолты = текущие константы из wave.py/smart.py/playlist_ai.py, поэтому
чтение через этот модуль вместо констант даёт 1в1 тот же скоринг.

Врезка в скоринг (замена констант на get_group()) — отдельный шаг (R2.2+),
здесь только чтение/запись/валидация. Образец — services/automation.py:
TTL-кэш чтения, set_flags с deep-merge только известных ключей.

Схема групп (см. docs/roadmap.md R2):
- repeats: штрафы повторов артист/жанр/муд, усталость, recency, novelty, cap.
- character: веса холодной/тёплой волны, jitter, skip/clap/key/cluster/lyrics
  и прочие мелкие веса total.
- skips: пороги дрейфа, energy/tempo-сдвиги, бан жанра, behaviorBonus, окно.
- playlists: forgotten_days, night/sport-пороги, пулы, adaptive caps,
  smooth-раскладка, сиды.
- auto: per-group тумблеры авто-мозга (R2.3/R3). False = ручное.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

KEY = "rec_tuning"
# Per-user overlay: AppSetting.key = UKEY_PREFIX + user_id. Миграций не надо
# (key — строковый PK), лимит 128 символов держим с запасом.
UKEY_PREFIX = "rec_tuning:user:"


def _user_key(user_id: str | None) -> str | None:
    s = str(user_id or "").strip()[:64]
    return (UKEY_PREFIX + s) if s else None

GROUPS = ("repeats", "character", "skips", "playlists")

DEFAULTS: dict[str, dict[str, Any]] = {
    "repeats": {
        # diversityPenalty за 1/2/3+ повтора в выдаче (wave.py score_candidates)
        "artist_penalty": [0.05, 0.10, 0.15],
        "genre_penalty": [0.03, 0.07, 0.12],
        "mood_penalty": [0.03, 0.06, 0.10],
        # усталость артиста: окно дней + пороги повторов + штрафы
        "fatigue_days": 7,
        "fatigue_thresholds": [3, 6, 10],
        "fatigue_penalty": [0.05, 0.10, 0.15],
        # recency-затухание новизны: окна (часы) + множители
        "recency_windows_h": [3, 24, 168],
        "recency_factors": [0.3, 0.6, 0.85],
        # добавка novelty сверх веса (0.08 * novelty в total)
        "novelty_extra": 0.08,
        # diversity cap AI/weekly: макс треков одного артиста
        "artist_cap": 2,
    },
    "character": {
        # порог "прогретости" (лайков) для переключения пресетов весов
        "likes_threshold": 50,
        "weights_cold": {"audio": 0.20, "genre": 0.30, "artist": 0.10,
                         "behavior": 0.10, "collab": 0.25, "novelty": 0.05},
        "weights_warm": {"audio": 0.40, "genre": 0.20, "artist": 0.10,
                         "behavior": 0.20, "collab": 0.05, "novelty": 0.05},
        "jitter": 0.12,
        "skip_weight": 0.25,
        "clap_weight": 0.08,
        "clap_max": 0.3,
        "key_weight": 0.06,
        "cluster_weight": 0.06,
        "lyrics_weight": 0.05,
        "time_max": 0.2,
        "arm_min": -0.16,
        "arm_max": 0.16,
        "assoc_weight": 0.08,
        "srv_starred": 0.05,
        "srv_rating": 0.10,
        "mood_arm_min": -0.08,
        "mood_arm_max": 0.12,
        "neg_weight": 0.15,
    },
    "skips": {
        # session_drift: N скипов подряд -> mild/moderate/strong
        "drift_counts": [3, 5, 7],
        "drift_energy": [0.1, 0.2, 0.3],
        "drift_tempo": [10, 20, 30],
        # временный бан жанра: столько скипов жанра за сессию
        "genre_ban_skips": 3,
        # behaviorBonus: окно последних событий + таблица
        "behavior_window": 10,
        "bonus_like": 0.2,
        "bonus_replay": 0.25,
        "bonus_complete": 0.2,
        "bonus_play_long": 0.1,
        "play_long_sec": 180,
        "penalty_abandon": -0.15,
        "penalty_skip_early": -0.3,
        "skip_early_sec": 30,
        "penalty_skip_late": -0.1,
        "skip_late_sec": 120,
    },
    "playlists": {
        "forgotten_days": 90,
        "night_energy_max": 0.4,
        "sport_bpm_min": 110,
        "sport_energy_min": 0.7,
        # пул кандидатов / скор-окно / добор коллаборативных сверх лимита
        "pool_limit": 2000,
        "score_window": 800,
        "collab_extra": 200,
        # adaptive_count: капы пачки при дрейфе/морфинге + пол
        "adaptive_strong": 4,
        "adaptive_moderate": 5,
        "adaptive_mild": 6,
        "adaptive_morphing": 6,
        "adaptive_floor": 4,
        # плавная раскладка энергии + key-сглаживание
        "smooth_max_step": 0.18,
        "smooth_passes": 8,
        "key_energy_max": 0.15,
        "key_passes": 3,
        # сиды вкуса: лимит + доли топ/recent по characteristic
        "seed_limit": 5,        "seed_top_favorite": 0.7,
        "seed_recent_favorite": 0.2,
        "seed_top_unfamiliar": 0.3,
        "seed_recent_unfamiliar": 0.2,
        "seed_top_popular": 0.6,
        "seed_recent_popular": 0.3,
        "seed_top_default": 0.5,
        "seed_recent_default": 0.25,
        # радио по артисту: похожих артистов / треков с каждого / серверных
        "radio_similar": 6,
        "radio_per_similar": 15,
        "radio_server": 20,
    },
    "auto": {
        "repeats": False,
        "character": False,
        "skips": False,
        "playlists": False,
    },
}

# (группа, ключ) -> (тип, min, max). Списки проверяются поэлементно.
# Тип weights — dict весов 0..1 (сумму не требуем: скоринг нормирует сам).
_SCHEMA: dict[tuple[str, str], tuple[str, float | None, float | None]] = {}

for _k in ("artist_penalty", "genre_penalty", "mood_penalty",
           "fatigue_penalty", "recency_factors", "drift_energy"):
    _SCHEMA[("repeats" if _k in ("artist_penalty", "genre_penalty", "mood_penalty",
                                 "fatigue_penalty", "recency_factors")
             else "skips", _k)] = ("float_list", 0.0, 1.0)
for _k in ("fatigue_thresholds", "recency_windows_h", "drift_counts",
           "drift_tempo"):
    _g = "repeats" if _k in ("fatigue_thresholds", "recency_windows_h") else "skips"
    _SCHEMA[(_g, _k)] = ("int_list", 1, 100000)
for _k, _lo, _hi in (
        ("fatigue_days", 1, 90), ("artist_cap", 1, 20),
        ("likes_threshold", 1, 10000), ("behavior_window", 1, 100),
        ("play_long_sec", 10, 3600), ("skip_early_sec", 5, 300),
        ("skip_late_sec", 30, 3600), ("forgotten_days", 1, 3650),
        ("sport_bpm_min", 60, 220), ("pool_limit", 100, 10000),
        ("score_window", 50, 2000), ("collab_extra", 0, 1000),
        ("adaptive_strong", 1, 100), ("adaptive_moderate", 1, 100),
        ("adaptive_mild", 1, 100), ("adaptive_morphing", 1, 100),
        ("adaptive_floor", 1, 20), ("smooth_passes", 0, 50),
        ("key_passes", 0, 20), ("seed_limit", 1, 20),
        ("radio_similar", 1, 20), ("radio_per_similar", 1, 100),
        ("radio_server", 0, 100)):
    _g2 = ("repeats" if _k in ("fatigue_days", "artist_cap")
           else "character" if _k == "likes_threshold"
           else "skips" if _k in ("behavior_window", "play_long_sec",
                                  "skip_early_sec", "skip_late_sec")
           else "playlists")
    _SCHEMA[(_g2, _k)] = ("int", _lo, _hi)
for _k, _lo, _hi in (
        ("novelty_extra", 0.0, 1.0), ("jitter", 0.0, 1.0),
        ("skip_weight", 0.0, 2.0), ("clap_weight", 0.0, 1.0),
        ("clap_max", 0.0, 1.0), ("key_weight", 0.0, 1.0),
        ("cluster_weight", 0.0, 1.0), ("lyrics_weight", 0.0, 1.0),
        ("time_max", 0.0, 1.0), ("arm_min", -1.0, 1.0),
        ("arm_max", -1.0, 1.0), ("assoc_weight", 0.0, 1.0),
        ("srv_starred", 0.0, 1.0), ("srv_rating", 0.0, 1.0),
        ("mood_arm_min", -1.0, 1.0), ("mood_arm_max", -1.0, 1.0),
        ("neg_weight", 0.0, 1.0), ("bonus_like", -1.0, 1.0),
        ("bonus_replay", -1.0, 1.0), ("bonus_complete", -1.0, 1.0),
        ("bonus_play_long", -1.0, 1.0), ("penalty_abandon", -1.0, 1.0),
        ("penalty_skip_early", -1.0, 1.0), ("penalty_skip_late", -1.0, 1.0),
        ("genre_ban_skips", 1, 20), ("night_energy_max", 0.0, 1.0),
        ("sport_energy_min", 0.0, 1.0), ("smooth_max_step", 0.01, 1.0),
        ("key_energy_max", 0.01, 1.0), ("seed_top_favorite", 0.0, 1.0),
        ("seed_recent_favorite", 0.0, 1.0), ("seed_top_unfamiliar", 0.0, 1.0),
        ("seed_recent_unfamiliar", 0.0, 1.0), ("seed_top_popular", 0.0, 1.0),
        ("seed_recent_popular", 0.0, 1.0), ("seed_top_default", 0.0, 1.0),
        ("seed_recent_default", 0.0, 1.0)):
    if _k in ("bonus_like", "bonus_replay", "bonus_complete",
              "bonus_play_long", "penalty_abandon", "penalty_skip_early",
              "penalty_skip_late", "genre_ban_skips"):
        _g3 = "skips"
        _t3 = "int" if _k == "genre_ban_skips" else "float"
    elif _k in ("night_energy_max", "sport_energy_min", "smooth_max_step",
                "key_energy_max") or _k.startswith(("seed_", "adaptive")) \
            or _k in ("forgotten_days", "sport_bpm_min"):
        _g3 = "playlists"
        _t3 = "float"
    else:
        _g3 = "repeats" if _k == "novelty_extra" else "character"
        _t3 = "float"
    _SCHEMA[(_g3, _k)] = (_t3, _lo, _hi)
del _k, _g, _g2, _g3, _t3, _lo, _hi

_WEIGHT_KEYS = ("audio", "genre", "artist", "behavior", "collab", "novelty")

# Кэш чтения (образец automation.py): скоринг дёргает тюнинг на каждый
# запрос волны, сессия к БД на каждый трек — дорого. Ключ кэша — user_id
# ("" = глобал), кап — чтобы память не росла на сотнях юзеров.
_TTL_SEC = 20.0
_CACHE_MAX = 200
_cache: dict[str, Any] = {"at": {}, "flags": {}}


def _coerce(kind: str, v: Any, lo: float | None, hi: float | None) -> Any:
    if kind == "bool":
        return bool(v)
    if kind == "int":
        iv = int(v)
        if lo is not None:
            iv = max(int(lo), iv)
        if hi is not None:
            iv = min(int(hi), iv)
        return iv
    if kind == "float":
        fv = float(v)
        if lo is not None:
            fv = max(float(lo), fv)
        if hi is not None:
            fv = min(float(hi), fv)
        return fv
    if kind in ("int_list", "float_list"):
        out = []
        for x in (v if isinstance(v, (list, tuple)) else []):
            try:
                n = int(x) if kind == "int_list" else float(x)
            except (TypeError, ValueError):
                continue
            if lo is not None:
                n = max(lo, n)
            if hi is not None:
                n = min(hi, n)
            out.append(n)
        return out
    return v


def _coerce_weights(v: Any) -> dict[str, float]:
    base = {"audio": 0.2, "genre": 0.2, "artist": 0.1,
            "behavior": 0.1, "collab": 0.2, "novelty": 0.1}
    if isinstance(v, dict):
        for k in _WEIGHT_KEYS:
            if k in v:
                try:
                    base[k] = min(1.0, max(0.0, float(v[k])))
                except (TypeError, ValueError):
                    pass
    return base


def invalidate_cache(user_id: str | None = None) -> None:
    """Сбросить кэш после записи — читатели увидят значения сразу.

    user_id=None = сбросить всё (глобал поменялся — инвалидируем всех,
    т.к. эффект каждого включает глобал).
    """
    if user_id is None:
        _cache["flags"] = {}
        _cache["at"] = {}
        return
    ck = str(user_id).strip() or "-"
    _cache["flags"].pop(ck, None)
    _cache["at"].pop(ck, None)


def _apply_row(flags: dict[str, dict[str, Any]], value: Any,
               allow_auto: bool) -> None:
    """Влить одну строку БД поверх flags (только схема, с валидацией)."""
    if not isinstance(value, dict):
        return
    for group in list(flags):
        patch = value.get(group)
        if not isinstance(patch, dict):
            continue
        if group == "auto":
            if not allow_auto:
                continue
            for k in patch:
                if k in flags["auto"]:
                    flags["auto"][k] = bool(patch[k])
            continue
        for k, v in patch.items():
            spec = _SCHEMA.get((group, k))
            if spec is None:
                continue
            kind, lo, hi = spec
            if k in ("weights_cold", "weights_warm"):
                flags[group][k] = _coerce_weights(v)
                continue
            try:
                nv = _coerce(kind, v, lo, hi)
            except (TypeError, ValueError):
                continue
            # пустой список = мусор, а не значение — дефолт целее
            if isinstance(nv, list) and not nv:
                continue
            flags[group][k] = nv


def _read_row(db, key: str) -> Any:
    try:
        from app.db.models import AppSetting

        row = db.get(AppSetting, key)
        return row.value if row is not None else None
    except Exception:
        return None


def get_all(db=None, user_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Эффект для юзера: дефолты <- глобал <- per-user overlay.

    user_id=None = только глобал. Любая ошибка чтения -> дефолты
    (скоринг не должен падать из-за настроек).
    """
    flags: dict[str, dict[str, Any]] = deepcopy(DEFAULTS)
    try:
        if db is None:
            from app.db.database import session_scope

            with session_scope() as _db:
                return get_all(_db, user_id)
        _apply_row(flags, _read_row(db, KEY), allow_auto=True)
        uk = _user_key(user_id)
        if uk:
            # auto-секция — только глобальная, из per-user её игнорим,
            # чтобы личный оверлей не включал/выключал мозг за всех.
            _apply_row(flags, _read_row(db, uk), allow_auto=False)
    except Exception:
        pass
    return flags


def _cached(user_id: str | None) -> dict[str, dict[str, Any]]:
    import time as _time

    now = _time.time()
    ck = str(user_id or "").strip() or "-"
    at = _cache["at"].get(ck, 0.0)
    if ck in _cache["flags"] and (now - float(at)) < _TTL_SEC:
        return _cache["flags"][ck]  # type: ignore[return-value]
    flags = get_all(user_id=user_id)
    _cache["flags"][ck] = flags
    _cache["at"][ck] = now
    if len(_cache["flags"]) > _CACHE_MAX:
        oldest = min(_cache["at"], key=lambda k: _cache["at"].get(k, 0.0))
        _cache["flags"].pop(oldest, None)
        _cache["at"].pop(oldest, None)
    return flags


def get_group(group: str, db=None,
              user_id: str | None = None) -> dict[str, Any]:
    """Одна группа (repeats/character/skips/playlists/auto) для юзера."""
    if group not in DEFAULTS:
        return {}
    if db is not None:
        return get_all(db, user_id).get(group, {})
    return _cached(user_id).get(group, {})


def is_auto(group: str, db=None) -> bool:
    """Включён ли авто-мозг на группу (R2.3/R3). Глобально. Дефолт False."""
    try:
        return bool(get_group("auto", db).get(group, False))
    except Exception:
        return False


def _clean_patch(patch: dict[str, Any], for_user: bool) -> dict[str, dict[str, Any]]:
    clean: dict[str, dict[str, Any]] = {}
    for group, sub in (patch or {}).items():
        if group not in DEFAULTS or not isinstance(sub, dict):
            continue
        if group == "auto":
            if for_user:
                continue
            for k, v in sub.items():
                if k in DEFAULTS["auto"]:
                    clean.setdefault(group, {})[k] = bool(v)
            continue
        for k, v in sub.items():
            spec = _SCHEMA.get((group, k))
            if spec is None:
                continue
            kind, lo, hi = spec
            try:
                if k in ("weights_cold", "weights_warm"):
                    clean.setdefault(group, {})[k] = _coerce_weights(v)
                else:
                    nv = _coerce(kind, v, lo, hi)
                    if isinstance(nv, list) and not nv:
                        continue
                    clean.setdefault(group, {})[k] = nv
            except (TypeError, ValueError):
                continue
    return clean


def set_flags(patch: dict[str, Any], db=None,
              user_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Сохранить патч {group: {key: value}}.

    user_id=None -> глобал; иначе — per-user overlay (auto-секцию туда
    не пишем: она глобальная). В строке хранится только патч, не весь
    эффект — глобал и оверлей не смешиваются. Возвращает эффект для scope.
    """
    uk = _user_key(user_id)
    clean = _clean_patch(patch, for_user=bool(uk))
    if db is None:
        from app.db.database import session_scope

        with session_scope() as _db:
            return set_flags(clean, _db, user_id)
    from app.db.models import AppSetting

    row_key = uk or KEY
    row = db.get(AppSetting, row_key)
    if row is None:
        db.add(AppSetting(key=row_key, value=clean))
    else:
        base = dict(row.value) if isinstance(row.value, dict) else {}
        for group, sub in clean.items():
            prev = base.get(group)
            if isinstance(prev, dict):
                merged_sub = dict(prev)
                merged_sub.update(sub)
                base[group] = merged_sub
            else:
                base[group] = dict(sub)
        row.value = base
    db.commit()
    # Глобал входит в эффект каждого — при его смене сбрасываем весь кэш.
    invalidate_cache(None if not uk else user_id)
    return get_all(db, user_id)


def reset_flags(db=None, user_id: str | None = None,
                global_too: bool = False) -> dict[str, dict[str, Any]]:
    """Сброс к дефолтам: per-user overlay удаляется всегда; глобал —
    только при global_too=True. Возвращает эффект."""
    if db is None:
        from app.db.database import session_scope

        with session_scope() as _db:
            return reset_flags(_db, user_id, global_too)
    from app.db.models import AppSetting

    try:
        uk = _user_key(user_id)
        if uk:
            db.query(AppSetting).filter(AppSetting.key == uk).delete()
        if global_too or not uk:
            db.query(AppSetting).filter(AppSetting.key == KEY).delete()
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    invalidate_cache(None if global_too or not uk else user_id)
    return get_all(db, user_id)
