"""Аддитивная «Моя волна» — серверный порт мобильного MLWaveService.

Мобилу не трогаем: клиент присылает маленькую дельту (что уже в очереди,
что играет сейчас, свежие события, кусок оценок), сервер докладывает
следующие N треков тем же скорингом, что TrackScorer в мобиле.

Веса как в мобиле (_getWeights): мало лайков (<50) — больше жанра и
коллаборативки, много — больше аудио и поведения. Плюс контекст
(activity/mood/час), behaviorBonus по recent_events, diversityPenalty,
random*0.12.
"""
from __future__ import annotations

import random
from collections import Counter
from datetime import datetime
from typing import Any


def _tun_all(db, user_id: str | None = None) -> dict:
    """Эффективный rec_tuning для юзера (дефолты <- глобал <- per-user).

    Один PK-запрос. Любая ошибка (нет таблицы, битая строка) -> {}:
    дальше везде дефолты-константы, поведение 1в1 как раньше.
    """
    try:
        from app.services import rec_tuning as _rt

        return _rt.get_all(db, user_id) or {}
    except Exception:
        return {}


def _tun(tun: dict, group: str, key: str, default):
    try:
        v = (tun.get(group) or {}).get(key, default)
        return default if v is None else v
    except Exception:
        return default


def _tun_float(tun: dict, group: str, key: str, default: float) -> float:
    try:
        return float(_tun(tun, group, key, default))
    except (TypeError, ValueError):
        return float(default)


def _tun_int(tun: dict, group: str, key: str, default: int) -> int:
    try:
        return int(float(_tun(tun, group, key, default)))
    except (TypeError, ValueError):
        return int(default)


def _tun_list(tun: dict, group: str, key: str,
              default: list, n: int) -> list:
    """Список нужной длины, иначе дефолт (защита от битой строки в БД)."""
    try:
        v = (tun.get(group) or {}).get(key)
        if isinstance(v, (list, tuple)) and len(v) >= n:
            return [float(x) for x in list(v)[:n]]
    except Exception:
        pass
    return [float(x) for x in list(default)[:n]]


def _weights(total_likes: int, tun: dict | None = None) -> dict[str, float]:
    cold = {'audio': 0.20, 'genre': 0.30, 'artist': 0.10,
            'behavior': 0.10, 'collab': 0.25, 'novelty': 0.05}
    warm = {'audio': 0.40, 'genre': 0.20, 'artist': 0.10,
            'behavior': 0.20, 'collab': 0.05, 'novelty': 0.05}
    tun = tun or {}
    th = _tun_int(tun, 'character', 'likes_threshold', 50)
    name = 'weights_cold' if total_likes < th else 'weights_warm'
    pick = cold if total_likes < th else warm
    try:
        stored = (tun.get('character') or {}).get(name)
        if isinstance(stored, dict) and all(k in stored for k in pick):
            return {k: float(stored[k]) for k in pick}
    except Exception:
        pass
    return dict(pick)


# Пресеты «Моя волна» (клиент шлёт русские пилюли или англ. коды).
# Настроение: целевые диапазоны energy/valence/dance + родственные муд-лейблы.
MOOD_PRESETS: dict[str, dict] = {
    'бодрое': {'moods': {'energetic', 'excited', 'happy'}, 'energy_min': 0.6, 'tempo_min': 110},
    'весёлое': {'moods': {'happy', 'upbeat', 'energetic'}, 'valence_min': 0.55, 'dance_min': 0.5},
    'спокойное': {'moods': {'calm', 'relaxed', 'peaceful'}, 'energy_max': 0.45, 'arousal_max': 0.45},
    'грустное': {'moods': {'sad', 'melancholic', 'dark'}, 'valence_max': 0.45},
    'тёмное': {'moods': {'dark', 'aggressive', 'melancholic'}, 'valence_max': 0.5},
    'chill': {'moods': {'calm', 'relaxed'}, 'energy_max': 0.5},
}
# Занятие: мягкие бонусы (не жёсткие фильтры — очередь не должна пустеть).
ACTIVITY_PRESETS: dict[str, dict] = {
    'просыпаюсь': {'energy_min': 0.5, 'tempo_min': 100, 'valence_min': 0.4},
    'в дороге': {'energy_min': 0.6, 'tempo_min': 110},
    'работаю': {'energy_max': 0.6, 'tempo_max': 120},
    'work': {'energy_min': 0.3, 'energy_max': 0.6},
    'workout': {'tempo_min': 110, 'energy_min': 0.6},
    'sleep': {'energy_max': 0.35},
}


def _norm_mood(v: str | None) -> str | None:
    s = (v or '').strip().lower()
    if not s:
        return None
    # русские пилюли -> англ. лейблы фичей
    _ru = {'бодрое': 'energetic',
           'весёлое': 'happy',
           'веселое': 'happy', 'спокойное': 'calm', 'грустное': 'sad',
           'тёмное': 'dark', 'темное': 'dark', 'меланхоличное': 'melancholic',
           'энергичное': 'energetic', 'меланхоличный': 'melancholic',
           'тёмный': 'dark', 'темный': 'dark', 'энергичный': 'energetic'}
    return _ru.get(s, s)


def _norm_language(v: str | None) -> str | None:
    s = (v or '').strip().lower()
    if not s:
        return None
    if s in ('ru', 'ru-ru', 'русский', 'russian', 'рус'):
        return 'ru'
    if s in ('foreign', 'en', 'eng', 'иностранный', 'английский', 'не русский'):
        return 'foreign'
    if s in ('instrumental', 'без слов', 'инструментал', 'no_lyrics', 'no lyrics'):
        return 'instrumental'
    return None


def _has_cyrillic(*parts: object) -> bool:
    """Есть ли кириллица в любом из кусков (метаданные трека)."""
    for p in parts:
        if not p:
            continue
        for c in str(p):
            if "\u0400" <= c <= "\u04ff":
                return True
    return False


def _meta_ru_ids(db, ids: list) -> set:
    """Кириллический fast-path: треки БЕЗ текстов (нет в Lyrics), но с
    кириллицей в метаданных (артист/название/альбом) считаем русскоязычными.

    High-precision без сети. Обратное неверно: латиница НЕ означает foreign
    (русские группы с английскими названиями сплошь и рядом), поэтому
    не-кириллические unknown по-прежнему пропускаем (unknown = pass).
    Реально чинит фильтры 'foreign' (убирает русское без текстов) и
    'instrumental' (у кириллического трека точно есть слова). Фильтр 'ru'
    от иностранных без текстов лечится только скачанными текстами или
    language_strict (см. ниже)."""
    from app.db.models import Track as _Track

    out: set = set()
    uniq = list(dict.fromkeys(str(i) for i in (ids or []) if i))[:2000]
    for i in range(0, len(uniq), 500):
        try:
            rows = db.query(_Track).filter(_Track.id.in_(uniq[i:i + 500])).all()
        except Exception:
            break
        for t in rows or []:
            try:
                if _has_cyrillic(getattr(t, "artist_name", None),
                                getattr(t, "title", None),
                                getattr(t, "album_name", None)):
                    out.add(str(getattr(t, "id", "")))
            except Exception:
                continue
    out.discard("")
    return out


def _resolve_ids(db, ids: list[str]) -> list[str]:
    """uuid как есть, external_id (мобильный id) -> наш track_id. Не падает на PG."""
    from app.services.track_resolve import resolve_track_ids as _r

    return _r(db, ids)


def apply_delta(db, user_id: str, ratings_delta: list[dict] | None,
                recent_events: list[dict] | None) -> dict:
    """Применить дельту от клиента: оценки + события. Отдельный синк не нужен."""
    from app.services import taste as _taste

    applied_r = applied_e = 0
    if ratings_delta:
        # Нормализуем к виду sync_from_mobile ratings (external_id или track_id)
        norm: list[dict] = []
        for r in ratings_delta:
            if not isinstance(r, dict):
                continue
            d = dict(r)
            tid = str(d.get('track_id') or '')
            if tid and d.get('external_id') is None:
                from app.services.track_resolve import get_track as _gt
                t = _gt(db, tid)
                if t is not None:
                    d['external_id'] = t.external_id
            norm.append(d)
        res = _taste.sync_from_mobile(db, user_id,
                                      {'ratings': norm, 'profile': {}, 'events': []})
        applied_r = int(res.get('ratings_seen', 0) or 0)
    if recent_events:
        norm_e: list[dict] = []
        for e in recent_events:
            if not isinstance(e, dict):
                continue
            tid = str(e.get('track_id') or '')
            if not tid:
                continue
            from app.services.track_resolve import get_track as _gt2
            _t = _gt2(db, tid)
            if _t is None:
                continue
            tid = str(_t.id)
            norm_e.append({'track_id': tid, 'action': e.get('action'),
                           'position_sec': e.get('position_sec')})
        if norm_e:
            from datetime import datetime as _dt

            from app.db.models import PlayHistory as _PH

            res = _taste.record_events(db, user_id, norm_e, limit=500)
            applied_e = int(res.get('stored', 0) or 0)
            # play-события — ещё и в историю (память мозга для GET history)
            for e in norm_e:
                if str(e.get('action') or '') == 'play':
                    db.add(_PH(user_id=user_id, track_id=str(e['track_id']),
                               played_at=_dt.utcnow()))
            db.commit()
    return {'ratings_applied': applied_r, 'events_applied': applied_e}


def select_seeds(db, user_id: str, characteristic: str | None = None,
                 limit: int = 5) -> list[str]:
    """Сиды как в мобиле: топ по скору + недавние + random."""
    from app.services import taste as _taste

    prof = _taste.user_profile(db, user_id, top_n=200)
    top = prof.get('top_tracks') or []
    if not top:
        return []
    tun = _tun_all(db, user_id)
    _suffix = {'favorite': 'favorite', 'unfamiliar': 'unfamiliar',
               'popular': 'popular'}.get(characteristic or '', 'default')
    _dflt = {'favorite': (0.7, 0.2), 'unfamiliar': (0.3, 0.2),
             'popular': (0.6, 0.3)}.get(characteristic or '', (0.5, 0.25))
    top_ratio = _tun_float(tun, 'playlists', f'seed_top_{_suffix}', _dflt[0])
    recent_ratio = _tun_float(tun, 'playlists', f'seed_recent_{_suffix}', _dflt[1])
    top_n = round(limit * top_ratio)
    rec_n = round(limit * recent_ratio)
    out = [t['track_id'] for t in top[:top_n]]
    # Недавние: по last_played
    dated = [t for t in top if t.get('last_played')]
    dated.sort(key=lambda t: t['last_played'], reverse=True)
    for t in dated:
        if len(out) >= top_n + rec_n:
            break
        if t['track_id'] not in out:
            out.append(t['track_id'])
    rest = [t['track_id'] for t in top if t['track_id'] not in out]
    random.shuffle(rest)
    out.extend(rest[:max(0, limit - len(out))])
    return out[:limit]


def session_drift(db, recent_events: list[dict] | None,
                  user_id: str | None = None) -> dict:
    """Порт мобильного MoodDriftDetector: сессия, а не вечность.

    recent_events — в хронологическом порядке (как шлёт клиент).
    - 3/5/7 скипов подряд в хвосте -> mild/moderate/strong (энергия/темп вниз).
    - 3+ скипа жанра -> временный бан жанра НА ЭТОТ ЗАПРОС (в БД не пишем).
    - скипнутые треки -> исключить из кандидатов НА ЭТОТ ЗАПРОС.
    Постоянные счётчики (3 скипа ever -> автодизлайк) не трогаем.
    """
    events = [e for e in (recent_events or []) if isinstance(e, dict)]
    # Пороги дрейфа из личного тюнинга (дефолт 3/5/7, энергия 0.1/0.2/0.3,
    # темп 10/20/30). Битые значения -> дефолт, сортировка на всякий случай.
    tun = _tun_all(db, user_id)
    _dc = [int(x) for x in _tun_list(tun, 'skips', 'drift_counts', [3, 5, 7], 3)]
    _de = _tun_list(tun, 'skips', 'drift_energy', [0.1, 0.2, 0.3], 3)
    _dt = _tun_list(tun, 'skips', 'drift_tempo', [10, 20, 30], 3)
    _dc = sorted(_dc)
    # Хвостовые скипы подряд (позитив обнуляет серию — как logPositiveInteraction).
    trailing = 0
    for e in reversed(events):
        if str(e.get("action") or "") == "skip":
            trailing += 1
        else:
            break
    if trailing >= _dc[2]:
        severity, energy_shift, tempo_shift = "strong", -_de[2], -_dt[2]
    elif trailing >= _dc[1]:
        severity, energy_shift, tempo_shift = "moderate", -_de[1], -_dt[1]
    elif trailing >= _dc[0]:
        severity, energy_shift, tempo_shift = "mild", -_de[0], -_dt[0]
    else:
        severity, energy_shift, tempo_shift = None, 0.0, 0
    # Зеркало: хвост позитива подряд (like/replay/complete/seek_back) —
    # разогрев волны (порт мобильного разогрева: скипы остужают, любовь греет).
    # Со скипами взаимоисключающе по построению хвоста.
    _POS_ACTS = {"like", "replay", "complete", "seek_back"}
    warm_streak = 0
    for e in reversed(events):
        if str(e.get("action") or "") in _POS_ACTS:
            warm_streak += 1
        else:
            break
    if warm_streak >= _dc[2]:
        warmth, energy_shift, tempo_shift = "strong", _de[2], _dt[2]
    elif warm_streak >= _dc[1]:
        warmth, energy_shift, tempo_shift = "moderate", _de[1], _dt[1]
    elif warm_streak >= _dc[0]:
        warmth, energy_shift, tempo_shift = "mild", _de[0], _dt[0]
    else:
        warmth, warm_streak = None, 0

    skip_ids: list[str] = []
    genre_hits: Counter = Counter()
    if events:
        raws = [str(e.get("track_id") or "") for e in events]
        tids = _resolve_ids(db, raws)
        id_by_raw = dict(zip(raws, tids))
        seen: set[str] = set()
        for e in events:
            if str(e.get("action") or "") != "skip":
                continue
            tid = id_by_raw.get(str(e.get("track_id") or ""))
            if tid and tid not in seen:
                seen.add(tid)
                skip_ids.append(tid)
        if skip_ids:
            try:
                from app.db.models import Track as _T

                for t in db.query(_T).filter(_T.id.in_(skip_ids[:100])).all():
                    if t.genre:
                        genre_hits[str(t.genre).lower()] += 1
            except Exception:
                pass
    _ban_n = _tun_int(tun, 'skips', 'genre_ban_skips', 3)
    temp_banned = sorted([g for g, n in genre_hits.items() if n >= _ban_n])
    return {"severity": severity, "energy_shift": energy_shift,
            "tempo_shift": tempo_shift, "consecutive_skips": trailing,
            "warmth": warmth, "positive_streak": warm_streak,
            "temp_banned_genres": temp_banned, "skip_ids": skip_ids}


def _mood_of(feat) -> str | None:
    try:
        moods = list(feat.mood_labels or [])
        return str(moods[0]).lower() if moods else None
    except Exception:
        return None


def _clap_weight(tun: dict | None = None) -> float:
    """Вес CLAP-косинуса в волне. 0 = выключено → поведение 1в1 как раньше.

    Без анализа/эмбеддингов вклад и так 0 (карта пустая), но флаг позволяет
    откатить фичу из веба без деплоя. Дефолт из личного тюнинга (0.08):
    заметно, но не ломает баланс.
    """
    try:
        from app.services.automation import clap_audio_enabled as _flag

        if not _flag():
            return 0.0
    except Exception:
        pass
    return _tun_float(tun or {}, 'character', 'clap_weight', 0.08)


def _load_clap_map(db, ids: list[str] | None,
                   model: str = "clap_audio") -> dict:
    """track_id -> нормализованный np-вектор CLAP. Пусто = безопасный фолбек.

    Грузим только нужные id (пул скоринга + сиды + сессия, ~1200), батчами.
    Любая ошибка (нет таблицы, нет анализа, нет numpy) → {} и волна
    считается только по librosa, ничего не ломается.
    """
    if not ids:
        return {}
    try:
        import numpy as _np

        from app.db.models import TrackEmbedding as _TE
    except Exception:
        return {}
    try:
        uniq = list(dict.fromkeys(map(str, ids)))[:1500]
        out: dict = {}
        for i in range(0, len(uniq), 500):
            try:
                rows = db.query(_TE).filter(
                    _TE.model == model,
                    _TE.track_id.in_(uniq[i:i + 500])).all()
            except Exception:
                continue
            for r in rows:
                try:
                    v = _np.frombuffer(r.vector, dtype=_np.float32).astype(_np.float32)
                    n = float(_np.linalg.norm(v))
                    if n > 0:
                        out[str(r.track_id)] = v / n
                except Exception:
                    continue
        return out
    except Exception:
        return {}


def infer_auto_mood(db, user_id: str, session_ids: list[str] | None,
                    current_hour: int | None = None) -> str | None:
    """Автодетект настроения из центроида сессии (без пилюль).

    Берём energy/valence среднего по последним трекам сессии; маппим на
    русские пилюли MOOD_PRESETS. Нет фичей/сессии → None (чистое авто-без-муда).
    Ручная пилюля всегда побеждает — см. wave_continue.
    """
    try:
        sids = [str(s) for s in (session_ids or []) if s][:10]
        if not sids:
            return None
        from app.db.models import TrackFeatures as _TF

        feats = {str(f.track_id): f for f in
                 db.query(_TF).filter(_TF.track_id.in_(sids)).all()}
        ens, vas = [], []
        for tid in sids:
            f = feats.get(tid)
            if f is None:
                continue
            try:
                if f.energy is not None:
                    ens.append(float(f.energy))
                if f.valence is not None:
                    vas.append(float(f.valence))
            except (TypeError, ValueError):
                continue
        if not ens:
            return None
        en = sum(ens) / len(ens)
        va = sum(vas) / len(vas) if vas else 0.5
        # Ночь — тянем к спокойному, утро — к бодрому (мягко).
        try:
            h = int(current_hour) if current_hour is not None else -1
        except (TypeError, ValueError):
            h = -1
        if va <= 0.38 and en < 0.55:
            return "грустное"
        if en >= 0.62 and va >= 0.45:
            return "бодрое"
        if en <= 0.42:
            return "спокойное"
        if va >= 0.6 and en >= 0.45:
            return "весёлое"
        if va <= 0.42:
            return "тёмное"
        if h >= 22 or (0 <= h <= 5):
            return "спокойное" if en < 0.55 else "тёмное"
        if 6 <= h <= 10:
            return "бодрое" if en >= 0.5 else "спокойное"
        return "chill" if en < 0.5 else "бодрое"
    except Exception:
        return None


def skip_risk(track, feat, pref_g: dict, cur_energy: float | None,
              drift: dict | None, fatigue: dict | None) -> float:
    """Эвристический P(skip<30с) 0..1. Без ML-модели — прозрачные правила.

    Признаки: скачок энергии/темпа от текущего, незнакомый жанр,
    загонянный артист, активный скип-стрик. Обученная логистическая модель
    (когда наберётся 5-10к событий) просто заменит эту функцию изнутри.
    """
    try:
        r = 0.0
        if feat is not None:
            try:
                en = float(feat.energy) if feat.energy is not None else 0.5
            except (TypeError, ValueError):
                en = 0.5
            try:
                bpm = float(feat.tempo_bpm) if feat.tempo_bpm else 0.0
            except (TypeError, ValueError):
                bpm = 0.0
            if cur_energy is not None:
                dj = abs(en - float(cur_energy))
                if dj > 0.45:
                    r += 0.35
                elif dj > 0.3:
                    r += 0.18
            if bpm and bpm > 150:
                r += 0.08
        g = (getattr(track, "genre", None) or "").strip().lower()
        if g and float(pref_g.get(g, 0.0)) <= 0.0:
            r += 0.10
        if fatigue and getattr(track, "artist_name", None) in fatigue:
            try:
                if int(fatigue[getattr(track, "artist_name")] or 0) > 10:
                    r += 0.20
            except (TypeError, ValueError):
                pass
        sev = (drift or {}).get("severity")
        if sev == "strong":
            r += 0.25
        elif sev == "moderate":
            r += 0.15
        elif sev == "mild":
            r += 0.08
        return min(max(r, 0.0), 1.0)
    except Exception:
        return 0.0


def _skip_prob(pipe, track, feat, pref_g: dict, cur_energy: float | None,
               drift: dict | None, fatigue: dict | None,
               agg_entry: dict | None, hour: int, dow: int) -> float:
    """P(skip<30с): обученная модель, при недоступности — эвристика.

    Сигнатура шире, чем у skip_risk: модели нужны агрегаты пары и время.
    Любая ошибка внутри — тихий фолбек на старые правила.
    """
    if pipe is not None:
        try:
            from app.services import skip_model as _sm

            try:
                en = float(feat.energy) if feat is not None and feat.energy is not None else 0.5
            except (TypeError, ValueError):
                en = 0.5
            try:
                va = float(feat.valence) if feat is not None and feat.valence is not None else 0.5
            except (TypeError, ValueError):
                va = 0.5
            try:
                da = float(feat.danceability) if feat is not None and feat.danceability is not None else 0.5
            except (TypeError, ValueError):
                da = 0.5
            try:
                bpm = float(feat.tempo_bpm) if feat is not None and feat.tempo_bpm else 0.0
            except (TypeError, ValueError):
                bpm = 0.0
            g = (getattr(track, "genre", None) or "").strip().lower()
            try:
                fam = float((pref_g or {}).get(g, 0.0)) if g else 0.0
            except (TypeError, ValueError):
                fam = 0.0
            try:
                an = getattr(track, "artist_name", None)
                _fc = int((fatigue or {}).get(an, 0) or 0) if an else 0
            except (TypeError, ValueError):
                _fc = 0
            a = agg_entry or {}
            try:
                sev = (drift or {}).get("severity")
                dl = 3 if sev == "strong" else (2 if sev == "moderate" else (1 if sev == "mild" else 0))
            except Exception:
                dl = 0
            vec = _sm.features_for(
                energy=en, valence=va, danceability=da, tempo_bpm=bpm,
                anchor_energy=cur_energy,
                unfamiliar_genre=bool(g and fam <= 0.0),
                artist_fatigue=min(_fc, 20) / 20.0,
                hour=hour, day_of_week=dow,
                plays=int(a.get("plays") or 0),
                early_skips=int(a.get("early_skips") or 0),
                completes=int(a.get("completes") or 0),
                drift_level=dl)
            p = _sm.predict_proba(pipe, vec)
            if p is not None:
                return p
        except Exception:
            pass
    return skip_risk(track, feat, pref_g, cur_energy, drift, fatigue)


def score_candidates(db, user_id: str, candidate_ids: list[str],
                     seed_ids: list[str], settings: dict | None = None,
                     recent_events: list[dict] | None = None,
                     collab_scores: dict[str, float] | None = None,
                     current_hour: int | None = None,
                     jitter_seed: str | None = None,
                     drift: dict | None = None,
                     ref_key: tuple | None = None,
                     cluster_map: dict[str, int] | None = None,
                     seed_clusters: set[int] | None = None,
                     ref_sentiment: str | None = None,
                     target_sentiment: str | None = None,
                     session_ids: list[str] | None = None,
                     neg_ids: list[str] | None = None,
                     last_played: dict[str, Any] | None = None,
                     fatigue: dict[str, int] | None = None,
                     time_map: dict[str, float] | None = None,
                     arm_artist: dict[str, float] | None = None,
                     arm_genre: dict[str, float] | None = None,
                     assoc: dict[str, float] | None = None,
                     clap_map: dict | None = None,
                     clap_weight: float | None = None,
                     mood_arm: dict[str, float] | None = None,
                     cur_energy: float | None = None) -> list[dict]:
    """Порт TrackScorer.scoreAndRankTracks на наших таблицах.

    jitter_seed: детерминированный per-user джиттер вместо глобального random
    (иначе у юзеров без данных порядок одинаковый — все вкусовые члены нули).
    """
    from app.db.models import Track, TrackFeatures
    from app.services import taste as _taste
    from app.services.ml import _cosine, _feature_vector

    settings = settings or {}
    tun = _tun_all(db, user_id)
    _rng = random.Random(jitter_seed) if jitter_seed else random
    activity_raw = (settings.get('activity') or '').strip()
    activity = activity_raw.lower() or None
    mood = _norm_mood(settings.get('mood'))
    try:
        from app.core.time import local_hour as _local_hour

        hour = current_hour if current_hour is not None else _local_hour()
    except Exception:
        hour = current_hour if current_hour is not None else datetime.now().hour
    try:
        from app.core.time import server_now as _server_now2

        _dow = _server_now2().isoweekday() % 7
    except Exception:
        _dow = datetime.now().isoweekday() % 7
    # Обученный скип-предиктор: один раз на вызов (TTL-кэш внутри get_model).
    # Нет данных — None, и кандидаты идут на эвристике skip_risk().
    _skip_pipe = None
    try:
        from app.services import skip_model as _sm0

        _skip_pipe = _sm0.get_model(db)
    except Exception:
        _skip_pipe = None
    # Пресеты: русские пилюли клиента -> диапазоны фичей для ctx-бонусов.
    _mood_preset = MOOD_PRESETS.get((settings.get('mood') or '').strip().lower(), {})
    _act_preset = ACTIVITY_PRESETS.get(activity_raw.strip().lower(),
                                       ACTIVITY_PRESETS.get(activity or '', {}))
    if mood and not _mood_preset:
        # англ. mood без пресета — ищем по ключам без учёта регистра
        for k, v in MOOD_PRESETS.items():
            if k == mood:
                _mood_preset = v
                break

    prof = _taste.user_profile(db, user_id, top_n=0)
    likes_total = int((prof.get('counts') or {}).get('likes', 0) or 0)
    w = _weights(likes_total, tun)
    pref_g = {str(k).lower(): float(v) for k, v in
              (prof.get('preferredGenres') or {}).items()}
    pref_a = {str(k): float(v) for k, v in
              (prof.get('preferredArtists') or {}).items()}

    # Агрегаты поведения (playCount/replay для novelty, score для behavior)
    agg = _taste._user_track_aggregates(db, user_id)

    _all_feat_ids = list(dict.fromkeys(
        list(candidate_ids or []) + list(seed_ids or []) +
        list(session_ids or []) + list(neg_ids or [])[:500]))
    feats: dict[str, Any] = {}
    if _all_feat_ids:
        for i in range(0, len(_all_feat_ids), 500):
            try:
                for f in db.query(TrackFeatures).filter(
                        TrackFeatures.track_id.in_(
                            _all_feat_ids[i:i + 500])).all():
                    feats[str(f.track_id)] = f
            except Exception:
                pass
    seed_vecs = [v for v in
                 (_feature_vector(None, feats[s]) for s in seed_ids if s in feats)
                 if v is not None]
    # Средний fingerprint волны = центроид сидов (у мобилы — rolling fingerprint)
    import numpy as _np
    centroid = None
    if seed_vecs:
        try:
            centroid = _np.mean(_np.vstack(seed_vecs), axis=0)
        except Exception:
            centroid = None
    # Сессионный fingerprint (порт mobile sonic_fingerprint): куда увёл
    # пользователь прямо сейчас — центроид недавно игранного.
    sess_centroid = None
    try:
        _sv = [v for v in
               (_feature_vector(None, feats[s]) for s in (session_ids or [])
                if s in feats) if v is not None]
        if _sv:
            sess_centroid = _np.mean(_np.vstack(_sv), axis=0)
    except Exception:
        sess_centroid = None
    # Негативный вектор вкуса: центроид дизлайков — похожее штрафуем.
    neg_centroid = None
    try:
        _nv = [v for v in
               (_feature_vector(None, feats[s]) for s in (neg_ids or [])[:500]
                if s in feats) if v is not None]
        if _nv:
            neg_centroid = _np.mean(_np.vstack(_nv), axis=0)
    except Exception:
        neg_centroid = None
    # CLAP-центроиды (семантика аудио, не только librosa-тембр).
    # Нет эмбеддингов/анализа → None → вклад 0, волна как раньше.
    _cw_max = _tun_float(tun, 'character', 'clap_max', 0.3)
    if clap_weight is not None:
        try:
            _cw = float(clap_weight)
        except (TypeError, ValueError):
            _cw = _clap_weight(tun)
    else:
        _cw = _clap_weight(tun)
    _cw = min(max(_cw, 0.0), max(_cw_max, 0.0))
    clap_map = clap_map or {}
    clap_centroid = None
    clap_sess = None
    if _cw > 0 and clap_map:
        try:
            _cv = [clap_map[s] for s in (seed_ids or []) if s in clap_map]
            if _cv:
                clap_centroid = _np.mean(_np.vstack(_cv), axis=0)
            _sv2 = [clap_map[s] for s in (session_ids or []) if s in clap_map]
            if _sv2:
                clap_sess = _np.mean(_np.vstack(_sv2), axis=0)
        except Exception:
            clap_centroid = None
            clap_sess = None
    else:
        _cw = 0.0

    # behaviorBonus по свежим событиям (окно и таблица — из личного тюнинга,
    # дефолт: окно 10 как в мобиле).
    _b_win = _tun_int(tun, 'skips', 'behavior_window', 10)
    _b_like = _tun_float(tun, 'skips', 'bonus_like', 0.2)
    _b_replay = _tun_float(tun, 'skips', 'bonus_replay', 0.25)
    _b_complete = _tun_float(tun, 'skips', 'bonus_complete', 0.2)
    _b_play = _tun_float(tun, 'skips', 'bonus_play_long', 0.1)
    _play_long = _tun_int(tun, 'skips', 'play_long_sec', 180)
    _p_abandon = _tun_float(tun, 'skips', 'penalty_abandon', -0.15)
    _p_early = _tun_float(tun, 'skips', 'penalty_skip_early', -0.3)
    _early_sec = _tun_int(tun, 'skips', 'skip_early_sec', 30)
    _p_late = _tun_float(tun, 'skips', 'penalty_skip_late', -0.1)
    _late_sec = _tun_int(tun, 'skips', 'skip_late_sec', 120)
    bonus: dict[str, float] = Counter()
    for e in (recent_events or [])[:max(1, _b_win)]:
        if not isinstance(e, dict):
            continue
        tid = str(e.get('track_id') or '')
        a = str(e.get('action') or '')
        pos = e.get('position_sec')
        try:
            pos = int(pos) if pos is not None else 999
        except (TypeError, ValueError):
            pos = 999
        if a == 'like':
            bonus[tid] += _b_like
        elif a in ('seek_back', 'replay'):
            bonus[tid] += _b_replay
        elif a == 'complete':
            bonus[tid] += _b_complete
        elif a == 'play' and pos >= _play_long:
            bonus[tid] += _b_play
        elif a == 'abandon':
            bonus[tid] += _p_abandon
        elif a == 'skip' and pos < _early_sec:
            bonus[tid] += _p_early
        elif a == 'skip' and pos >= _late_sec:
            bonus[tid] += _p_late

    used_artists: dict[str, int] = {}
    used_genres: dict[str, int] = {}
    used_moods: dict[str, int] = {}
    # Личный тюнинг, один раз на вызов (не на трек): штрафы повторов,
    # усталость, recency, веса total. Битые значения -> дефолты выше.
    _ap = _tun_list(tun, 'repeats', 'artist_penalty', [0.05, 0.10, 0.15], 3)
    _gp = _tun_list(tun, 'repeats', 'genre_penalty', [0.03, 0.07, 0.12], 3)
    _mp = _tun_list(tun, 'repeats', 'mood_penalty', [0.03, 0.06, 0.10], 3)
    _ft = [int(x) for x in
           _tun_list(tun, 'repeats', 'fatigue_thresholds', [3, 6, 10], 3)]
    _fpw = _tun_list(tun, 'repeats', 'fatigue_penalty',
                     [0.05, 0.10, 0.15], 3)
    _rw = _tun_list(tun, 'repeats', 'recency_windows_h', [3, 24, 168], 3)
    _rf = _tun_list(tun, 'repeats', 'recency_factors', [0.3, 0.6, 0.85], 3)
    _nov_extra = _tun_float(tun, 'repeats', 'novelty_extra', 0.08)
    _jitter = _tun_float(tun, 'character', 'jitter', 0.12)
    _skip_w = _tun_float(tun, 'character', 'skip_weight', 0.25)
    _key_w = _tun_float(tun, 'character', 'key_weight', 0.06)
    _cluster_w = _tun_float(tun, 'character', 'cluster_weight', 0.06)
    _lyr_w = _tun_float(tun, 'character', 'lyrics_weight', 0.05)
    _assoc_w = _tun_float(tun, 'character', 'assoc_weight', 0.08)
    _srv_star = _tun_float(tun, 'character', 'srv_starred', 0.05)
    _srv_rate = _tun_float(tun, 'character', 'srv_rating', 0.10)
    _neg_w = _tun_float(tun, 'character', 'neg_weight', 0.15)
    _arm_min = _tun_float(tun, 'character', 'arm_min', -0.16)
    _arm_max = _tun_float(tun, 'character', 'arm_max', 0.16)
    if _arm_min > _arm_max:
        _arm_min, _arm_max = -0.16, 0.16
    _marm_min = _tun_float(tun, 'character', 'mood_arm_min', -0.08)
    _marm_max = _tun_float(tun, 'character', 'mood_arm_max', 0.12)
    if _marm_min > _marm_max:
        _marm_min, _marm_max = -0.08, 0.12
    out: list[dict] = []
    tracks = {str(t.id): t for t in
              db.query(Track).filter(Track.id.in_(candidate_ids)).all()} \
        if candidate_ids else {}
    for tid in candidate_ids:
        t = tracks.get(tid)
        if not t:
            continue
        f = feats.get(tid)
        # audio: косинус к центроиду сидов + 0.4 сессионного fingerprint
        # (порт mobile: audio*0.6 + fingerprint*0.4).
        audio = 0.0
        if centroid is not None and f is not None:
            v = _feature_vector(None, f)
            if v is not None:
                try:
                    audio = float(_cosine(v, centroid))
                except Exception:
                    audio = 0.0
                if sess_centroid is not None:
                    try:
                        audio = audio * 0.6 + float(_cosine(v, sess_centroid)) * 0.4
                    except Exception:
                        pass
        # Негативный вектор: похожее на задизлайканное — штраф.
        neg_pen = 0.0
        if neg_centroid is not None and f is not None:
            try:
                v = _feature_vector(None, f)
                if v is not None:
                    neg_pen = max(0.0, float(_cosine(v, neg_centroid))) * _neg_w
            except Exception:
                neg_pen = 0.0
        genre_s = 0.0
        if t.genre:
            genre_s = min(max(pref_g.get(str(t.genre).lower(), 0.0) / 10.0, 0.0), 1.0)
        artist_s = 0.0
        if t.artist_name:
            artist_s = min(max(pref_a.get(str(t.artist_name), 0.0) / 10.0, 0.0), 1.0)
        a = agg.get(tid, {})
        behavior = 0.0
        if a:
            try:
                behavior = min(max((_taste.track_score(a) + 50.0) / 100.0, 0.0), 1.0)
            except Exception:
                behavior = 0.0
        plays = int(a.get('plays', 0) or 0) if a else 0
        replays = int(a.get('replays', 0) or 0) if a else 0
        novelty = 1.0 / (1.0 + plays + replays * 2) if (plays or replays) else 1.0
        novelty = min(max(novelty, 0.0), 1.0)
        # Временное затухание (порт mobile recency): слышал час назад —
        # не stadiть в очередь, даже если счётчики маленькие.
        if last_played and tid in last_played:
            try:
                _age_h = (datetime.utcnow() - last_played[tid]).total_seconds() / 3600.0
                if _age_h < _rw[0]:
                    novelty *= _rf[0]
                elif _age_h < _rw[1]:
                    novelty *= _rf[1]
                elif _age_h < _rw[2]:
                    novelty *= _rf[2]
            except Exception:
                pass
        collab = min(max(float((collab_scores or {}).get(tid, 0.0)), 0.0), 1.0)
        # «Часто в этот час» (порт mobile timeBonus).
        time_b = 0.0
        if time_map:
            try:
                time_b = min(max(float(time_map.get(tid, 0.0)), 0.0), 0.2)
            except (TypeError, ValueError):
                time_b = 0.0
        # Exploit бандита: руки артиста/жанра в текущем контексте.
        arm_b = 0.0
        try:
            if arm_artist and t.artist_name:
                arm_b += float(arm_artist.get(t.artist_name, 0.0))
            if arm_genre and t.genre:
                arm_b += float(arm_genre.get(str(t.genre).lower(), 0.0))
            arm_b = min(max(arm_b, _arm_min), _arm_max)
        except (TypeError, ValueError):
            arm_b = 0.0
        # Ассоциация по сессиям (порт mobile item_similarity).
        assoc_b = 0.0
        if assoc:
            try:
                assoc_b = min(max(float(assoc.get(tid, 0.0)), 0.0), 1.0) * _assoc_w
            except (TypeError, ValueError):
                assoc_b = 0.0
        # Рейтинг/звёздочка из Navidrome (порт mobile serverScore).
        srv_bonus = 0.0
        try:
            if getattr(t, "starred", False):
                srv_bonus += _srv_star
            _rt = getattr(t, "rating", None)
            if _rt:
                srv_bonus += min(max(float(_rt) / 5.0, 0.0), 1.0) * _srv_rate
        except (TypeError, ValueError):
            pass
        # Тональность: совместимость с текущим треком (плавный переход).
        key_c = 0.5
        if ref_key:
            try:
                from app.services.orchestrator import key_compatibility as _kc

                key_c = float(_kc(ref_key[0], ref_key[1],
                                  getattr(f, "key_name", None) if f is not None else None,
                                  getattr(f, "scale", None) if f is not None else None))
            except Exception:
                key_c = 0.5
        # KMeans-кластер: та же «полка» библиотеки, что у сидов вкуса.
        cluster_c = 0.0
        if seed_clusters and cluster_map:
            try:
                cluster_c = 1.0 if cluster_map.get(tid) in seed_clusters else 0.0
            except Exception:
                cluster_c = 0.0
        # Настроение лирики (lyrics-AI sentiment): к целевому муд-пресету,
        # иначе — к текущему треку (держать вайб).
        lyr_c = 0.5
        try:
            mv = (f.mood_vector if f is not None and isinstance(
                getattr(f, "mood_vector", None), dict) else {}) or {}
            want = target_sentiment or ref_sentiment
            got = str(mv.get("ai_sentiment") or "").lower() or None
            if want and got:
                lyr_c = 1.0 if got == want else (0.5 if "neutral" in (got, want) else 0.0)
        except Exception:
            lyr_c = 0.5

        ctx = 0.0
        if f is not None:
            try:
                en = float(f.energy or 0.5)
            except (TypeError, ValueError):
                en = 0.5
            try:
                bpm = float(f.tempo_bpm or 0)
            except (TypeError, ValueError):
                bpm = 0
            try:
                va = float(f.valence if f.valence is not None else 0.5)
            except (TypeError, ValueError):
                va = 0.5
            try:
                da = float(f.danceability if f.danceability is not None else 0.5)
            except (TypeError, ValueError):
                da = 0.5
            # legacy активности (совместимость)
            if activity == 'work' and 0.3 <= en <= 0.6:
                ctx += 0.1
            if activity == 'workout' and bpm > 110:
                ctx += 0.15
            if activity == 'sleep' and en < 0.3:
                ctx += 0.1
            if mood and _mood_of(f) == mood:
                ctx += 0.1
            # пресеты настроения: родственные муд-лейблы + диапазоны
            if _mood_preset:
                _pm = set(_mood_preset.get('moods') or set())
                try:
                    _fm = set(str(m).lower() for m in (f.mood_labels or []))
                except Exception:
                    _fm = set()
                if _fm & _pm:
                    ctx += 0.12
                _ok = True
                if 'energy_min' in _mood_preset and en < _mood_preset['energy_min']:
                    _ok = False
                if 'energy_max' in _mood_preset and en > _mood_preset['energy_max']:
                    _ok = False
                if 'valence_min' in _mood_preset and va < _mood_preset['valence_min']:
                    _ok = False
                if 'valence_max' in _mood_preset and va > _mood_preset['valence_max']:
                    _ok = False
                if 'dance_min' in _mood_preset and da < _mood_preset['dance_min']:
                    _ok = False
                if 'tempo_min' in _mood_preset and bpm and bpm < _mood_preset['tempo_min']:
                    _ok = False
                if _ok:
                    ctx += 0.08
            # пресеты занятия: мягкие бонусы
            if _act_preset:
                _aok = True
                if 'energy_min' in _act_preset and en < _act_preset['energy_min']:
                    _aok = False
                if 'energy_max' in _act_preset and en > _act_preset['energy_max']:
                    _aok = False
                if 'tempo_min' in _act_preset and bpm and bpm < _act_preset['tempo_min']:
                    _aok = False
                if 'tempo_max' in _act_preset and bpm and bpm > _act_preset['tempo_max']:
                    _aok = False
                if 'valence_min' in _act_preset and va < _act_preset['valence_min']:
                    _aok = False
                if _aok:
                    ctx += 0.10
            # время суток: утром — энергия, вечером — спокойствие
            if 6 <= hour <= 10 and en >= 0.6:
                ctx += 0.03
            elif hour >= 22 or hour <= 5:
                if en <= 0.5:
                    ctx += 0.03
            # сессионный дрейф (порт MoodDriftDetector): скипы подряд остужают
            # волну — энергичное/быстрое штрафуем, спокойное чуть поднимаем.
            _dr = drift or {}
            _eshift = float(_dr.get("energy_shift") or 0.0)
            if _eshift:
                ctx += _eshift * (en - 0.4)
            _tshift = float(_dr.get("tempo_shift") or 0)
            if _tshift and bpm > 110:
                ctx += (_tshift / -30.0) * -0.15 * min(max((bpm - 110) / 50.0, 0.0), 1.0)
        # diversity
        pen = 0.0
        if t.artist_name and t.artist_name in used_artists:
            c = used_artists[t.artist_name]
            pen += _ap[0] if c == 1 else (_ap[1] if c == 2 else _ap[2])
        if t.genre and t.genre in used_genres:
            c = used_genres[t.genre]
            pen += _gp[0] if c == 1 else (_gp[1] if c == 2 else _gp[2])
        # Разнообразие настроений: не класть одно и то же настроение пачкой.
        _cm = _mood_of(f)
        if _cm and _cm in used_moods:
            c = used_moods[_cm]
            pen += _mp[0] if c == 1 else (_mp[1] if c == 2 else _mp[2])
        # Усталость артиста за неделю (по истории): загонянное остужаем.
        if fatigue and t.artist_name and t.artist_name in fatigue:
            _fp = int(fatigue[t.artist_name] or 0)
            if _fp > _ft[2]:
                pen += _fpw[2]
            elif _fp >= _ft[1]:
                pen += _fpw[1]
            elif _fp >= _ft[0]:
                pen += _fpw[0]

        # CLAP-семантика: косинус к центроиду сидов + сессия (как audio).
        # Нет эмбеддинга у трека/сидов → 0, волна 1в1 как раньше.
        clap_s = 0.0
        if _cw > 0 and tid in (clap_map or {}):
            try:
                _cv = clap_map[tid]
                if clap_centroid is not None:
                    clap_s = float(_cosine(_cv, clap_centroid))
                    if clap_sess is not None:
                        clap_s = clap_s * 0.6 + float(_cosine(_cv, clap_sess)) * 0.4
                elif clap_sess is not None:
                    clap_s = float(_cosine(_cv, clap_sess))
                clap_s = min(max(clap_s, 0.0), 1.0)
            except Exception:
                clap_s = 0.0
        # Ручка настроения бандита (mood-армы): любит такое в этот контекст.
        mood_b = 0.0
        try:
            if mood_arm and _cm:
                mood_b = min(max(float(mood_arm.get(_cm, 0.0)), _marm_min), _marm_max)
        except (TypeError, ValueError):
            mood_b = 0.0
        # Предикт скипа <30с: обученная модель, иначе эвристика.
        skip_p = _skip_prob(_skip_pipe, t, f, pref_g, cur_energy, drift,
                            fatigue, agg.get(tid), hour, _dow)
        total = (w['audio'] * audio + w['genre'] * genre_s +
                 w['artist'] * artist_s + w['behavior'] * behavior +
                 w['collab'] * collab + w['novelty'] * novelty +
                 _nov_extra * novelty + ctx + bonus.get(tid, 0.0) -
                 pen - neg_pen + srv_bonus + time_b + arm_b + assoc_b +
                 _cw * clap_s + mood_b - _skip_w * skip_p +
                 _rng.random() * _jitter +
                 _key_w * key_c + _cluster_w * cluster_c + _lyr_w * lyr_c)
        total = min(max(total, 0.0), 1.0)
        if collab >= 0.7:
            reason = 'Loved by friends'
        elif audio > 0.8:
            reason = 'Similar to what you love'
        elif artist_s > 0.5:
            reason = f'By {t.artist_name}'
        elif genre_s > 0.5:
            reason = f'{t.genre} you enjoy'
        elif novelty > 0.8:
            reason = 'New discovery'
        else:
            reason = None
        out.append({'track_id': tid, 'external_id': str(t.external_id or ''),
                    'title': t.title,
                    'artist_name': t.artist_name, 'album_name': t.album_name,
                    'genre': t.genre, 'score': round(total, 4),
                    'audio': round(audio, 3), 'reason': reason,
                    'comp': {'audio': round(audio, 3),
                             'genre': round(genre_s, 3),
                             'artist': round(artist_s, 3),
                             'behavior': round(behavior, 3),
                             'collab': round(collab, 3),
                             'novelty': round(novelty, 3),
                             'key': round(key_c, 3),
                             'cluster': round(cluster_c, 3),
                             'lyrics': round(lyr_c, 3),
                             'neg': round(neg_pen, 3),
                             'srv': round(srv_bonus, 3),
                             'time': round(time_b, 3),
                             'arm': round(arm_b, 3),
                             'assoc': round(assoc_b, 3),
                             'clap': round(clap_s, 3),
                             'skip': round(skip_p, 3),
                             'mood_arm': round(mood_b, 3)}})
        if t.artist_name:
            used_artists[t.artist_name] = used_artists.get(t.artist_name, 0) + 1
        if t.genre:
            used_genres[t.genre] = used_genres.get(t.genre, 0) + 1
        if _cm:
            used_moods[_cm] = used_moods.get(_cm, 0) + 1
    out.sort(key=lambda x: x['score'], reverse=True)
    return out


def spread_scores(items: list[dict], key: str = "score") -> list[dict]:
    """Растянуть скоры окна в 0..1 (min-max), чтобы топ не выглядел как
    сплошные 1.00: сырые total часто упираются в кламп 1.0.
    Монотонно — порядок не меняется, только видимая дифференциация."""
    try:
        vals = [float(it.get(key) or 0) for it in items]
    except Exception:
        return items
    if len(vals) < 2:
        return items
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return items
    for it, v in zip(items, vals):
        try:
            it[key] = round((v - lo) / (hi - lo), 3)
        except Exception:
            pass
    return items


def rerank_pool(db, user_id: str, candidate_ids: list[str], *,
                seeds: list[str] | None = None, kind: str = "",
                day: str | None = None, settings: dict | None = None,
                n: int = 30, source: str | None = None) -> list[str]:
    """Единый скоринг плейлистов на ядре волны.

    Плейлистные генерации (daily/smart/weekly/discovery) раньше считали
    каждая свой скор; теперь все отдают свой предфильтрованный пул сюда и
    получают порядок от общего ядра: CLAP, skip-risk (модель/эвристика),
    UCB-руки, auto-mood, коллаборативка, новизна. Предфильтры генераций
    (предикаты night/sport, forgotten-окно, playable) живут у вызывающих —
    характер плейлиста не меняется, меняется только ранжирование внутри.

    Возвращает топ-n track_id. Выдачу фиксирует в rec_feedback (source) —
    это же и обучающая выборка для скип-предиктора.
    """
    from datetime import date as _date

    cand = [str(c) for c in (candidate_ids or []) if c]
    if not cand:
        return []
    n = max(1, min(100, int(n or 30)))
    try:
        from app.core.time import local_hour as _lh, local_today as _lt

        _hour = _lh()
        _day = day or _lt().isoformat()
    except Exception:
        from datetime import datetime as _dt

        _hour = _dt.now().hour
        _day = day or _date.today().isoformat()
    # сиды вкуса
    _seeds = [str(s) for s in (seeds or []) if s]
    if not _seeds:
        try:
            _seeds = select_seeds(db, user_id, limit=5)
        except Exception:
            _seeds = []
    # свежие события -> дрейф + behaviorBonus (последние 60)
    recent_events: list[dict] = []
    try:
        from app.db.models import PlayEvent as _PE

        for r in db.query(_PE).filter(_PE.user_id == str(user_id)).order_by(
                _PE.created_at.desc()).limit(60).all():
            recent_events.append({"track_id": str(r.track_id),
                                  "action": r.action,
                                  "position_sec": r.position_sec})
        recent_events.reverse()
    except Exception:
        recent_events = []
    try:
        drift = session_drift(db, recent_events, user_id)
    except Exception:
        drift = {}
    # коллаборативка
    collab_scores: dict[str, float] = {}
    try:
        from app.services import collab as _cb

        rec = _cb.recommend_for_user(db, user_id, n=200)
        for it in rec.get("items", []):
            collab_scores[str(it["track_id"])] = float(it.get("score", 0) or 0)
    except Exception:
        pass
    # кластеры сидов
    cluster_map: dict[str, int] = {}
    seed_clusters: set[int] = set()
    try:
        from app.db.models import TrackCluster as _TC

        _need = list({str(c) for c in (cand[:800] + _seeds)})
        for i in range(0, len(_need), 500):
            try:
                for row in db.query(_TC).filter(
                        _TC.algorithm == "kmeans",
                        _TC.track_id.in_(_need[i:i + 500])).all():
                    cluster_map[str(row.track_id)] = int(row.cluster_id)
            except Exception:
                pass
        seed_clusters = {cluster_map[s] for s in _seeds if s in cluster_map}
    except Exception:
        pass
    # негатив, recency, усталость
    try:
        from app.db.models import TrackDislike as _TD

        neg_ids = [str(r.track_id) for r in
                   db.query(_TD).filter_by(user_id=str(user_id))
                   .order_by(_TD.created_at.desc()).limit(500).all()]
    except Exception:
        neg_ids = []
    last_played: dict[str, Any] = {}
    try:
        from app.db.models import PlayHistory as _PH

        for i in range(0, len(cand[:800]), 500):
            try:
                for tid, when in db.query(_PH.track_id, _PH.played_at).filter(
                        _PH.user_id == str(user_id),
                        _PH.track_id.in_(cand[:800][i:i + 500])).all():
                    if when and (str(tid) not in last_played or
                                 when > last_played[str(tid)]):
                        last_played[str(tid)] = when
            except Exception:
                pass
    except Exception:
        pass
    fatigue: dict[str, int] = {}
    last_ids: list[str] = []
    try:
        from datetime import datetime as _dt2
        from datetime import timedelta as _td2

        from app.db.models import PlayHistory as _PH2
        from app.db.models import Track as _T2

        _since = _dt2.utcnow() - _td2(days=7)
        _recent = [str(r[0]) for r in
                   db.query(_PH2.track_id).filter(
                       _PH2.user_id == str(user_id),
                       _PH2.played_at >= _since)
                   .order_by(_PH2.played_at.desc()).limit(5000).all()]
        last_ids = list(dict.fromkeys(_recent))[:20]
        if _recent:
            _amap: dict[str, str] = {}
            _uniq = list(dict.fromkeys(_recent))
            for i in range(0, len(_uniq), 500):
                try:
                    for t in db.query(_T2).filter(
                            _T2.id.in_(_uniq[i:i + 500])).all():
                        if t.artist_name:
                            _amap[str(t.id)] = t.artist_name
                except Exception:
                    pass
            for tid in _recent:
                _an = _amap.get(tid)
                if _an:
                    fatigue[_an] = fatigue.get(_an, 0) + 1
    except Exception:
        pass
    session_ids = last_ids[:20]
    # якорь энергии — последний игранный
    cur_energy: float | None = None
    try:
        if last_ids:
            from app.db.models import TrackFeatures as _TF3

            _lf = db.query(_TF3).filter(_TF3.track_id == last_ids[0]).first()
            if _lf is not None and _lf.energy is not None:
                cur_energy = float(_lf.energy)
    except Exception:
        cur_energy = None
    # контекст: час, руки, ассоциации, CLAP, авто-муд
    try:
        from app.core.time import server_now as _sn

        _dow = _sn().isoweekday() % 7
    except Exception:
        from datetime import datetime as _dt3

        _dow = _dt3.now().isoweekday() % 7
    try:
        from app.services.taste import context_of as _ctx_of

        _ctx = _ctx_of(_hour, _dow)
    except Exception:
        _ctx = "day_wd"
    time_map = time_bonus_for(db, user_id, cand[:800], _hour)
    try:
        arm_artist, arm_genre = arm_boost_for(db, user_id, _ctx)
    except Exception:
        arm_artist, arm_genre = {}, {}
    try:
        mood_arm_map = mood_boost_for(db, user_id, _ctx)
    except Exception:
        mood_arm_map = {}
    try:
        assoc = session_assoc(db, user_id, _seeds)
    except Exception:
        assoc = {}
    try:
        _clap_ids = list(dict.fromkeys(
            list(cand[:800]) + list(_seeds) + list(session_ids)))[:1500]
        clap_map = _load_clap_map(db, _clap_ids)
    except Exception:
        clap_map = {}
    settings_eff = dict(settings or {})
    try:
        if not (settings_eff.get("mood") or "").strip():
            _am = infer_auto_mood(db, user_id, session_ids, _hour)
            if _am:
                settings_eff["mood"] = _am
    except Exception:
        pass
    ranked = score_candidates(
        db, user_id, cand[:800], _seeds, settings_eff, recent_events,
        collab_scores, current_hour=_hour,
        jitter_seed=f"{user_id}:{kind or 'pool'}:{_day}",
        drift=drift, cluster_map=cluster_map or None,
        seed_clusters=seed_clusters or None,
        session_ids=session_ids or None, neg_ids=neg_ids or None,
        last_played=last_played or None, fatigue=fatigue or None,
        time_map=time_map or None, arm_artist=arm_artist or None,
        arm_genre=arm_genre or None, assoc=assoc or None,
        clap_map=clap_map or None, clap_weight=None,
        mood_arm=mood_arm_map or None, cur_energy=cur_energy)
    top = ranked[:n]
    try:
        from app.services import rec_feedback as _rfb

        _rfb.mark_served(db, user_id, top, source=source or kind or "wave")
    except Exception:
        pass
    return [str(r["track_id"]) for r in top if r.get("track_id")]


def _recency_w(last) -> float:
    """Затухание паттерна по давности (порт mobile time_aware_history)."""
    if not last:
        return 0.05
    try:
        from datetime import datetime as _dt

        age_d = (_dt.utcnow() - last).total_seconds() / 86400.0
    except Exception:
        return 0.05
    if age_d < 1:
        return 1.0
    if age_d < 2:
        return 0.8
    if age_d < 4:
        return 0.6
    if age_d < 8:
        return 0.3
    if age_d < 31:
        return 0.1
    return 0.05


def time_bonus_for(db, user_id: str, track_ids: list[str], hour: int) -> dict[str, float]:
    """«Часто в этот час» (порт mobile): сумма по h-1/h/h+1
    10*(1+plays/5)*recency, clamp 0..50, /50. Возвращает 0..time_max на трек."""
    out: dict[str, float] = {}
    if not track_ids:
        return out
    _tmax = _tun_float(_tun_all(db, user_id), 'character', 'time_max', 0.2)
    try:
        from app.db.models import TrackTimeStat as _TTS

        hours = {(int(hour) - 1) % 24, int(hour) % 24, (int(hour) + 1) % 24}
        rows: dict[str, float] = {}
        uniq = list(dict.fromkeys(map(str, track_ids)))
        for i in range(0, len(uniq), 500):
            try:
                for r in db.query(_TTS).filter(
                        _TTS.user_id == str(user_id),
                        _TTS.hour.in_(list(hours)),
                        _TTS.track_id.in_(uniq[i:i + 500])).all():
                    rows[str(r.track_id)] = rows.get(str(r.track_id), 0.0) + \
                        10.0 * (1.0 + float(r.plays or 0) / 5.0) * _recency_w(r.last_played)
            except Exception:
                pass
        for tid, v in rows.items():
            out[tid] = min(max(v, 0.0), 50.0) / 50.0 * max(_tmax, 0.0)
    except Exception:
        pass
    return out


def arm_boost_for(db, user_id: str, context: str) -> tuple[dict[str, float], dict[str, float]]:
    """Бандит UCB-lite: exploit среднего + направленный explore неуверенности.

    Было: средний reward -> буст -0.08..+0.08, explore только слепым
    джиттером random*0.12. Стало: exploit тот же + бонус неуверенности
    sqrt(log(total+1)/(pulls+1)) — малоигранные руки в этом контексте
    получают шанс показаться, а не тонут. Возвращает (artist, genre),
    значения -0.08..+0.12. Сигнатура и таблица те же, миграций нет.
    """
    ab: dict[str, float] = {}
    gb: dict[str, float] = {}
    try:
        import math as _math

        from app.db.models import TasteArm as _TA

        rows = db.query(_TA).filter(
            _TA.user_id == str(user_id), _TA.context == str(context)).all()
        total = sum(int(r.pulls or 0) for r in rows) or 0
        for r in rows:
            try:
                if r.kind not in ("artist", "genre"):
                    continue
                pulls = int(r.pulls or 0)
                if pulls <= 0:
                    continue
                avg = float(r.reward or 0.0) / pulls
                exploit = min(max(avg / 10.0, -1.0), 1.0) * 0.08
                explore = 0.05 * _math.sqrt(
                    _math.log(total + 1.0) / (pulls + 1.0)) if total else 0.0
                b = min(max(exploit + explore, -0.08), 0.12)
                if r.kind == "artist":
                    ab[str(r.name)] = b
                else:
                    gb[str(r.name).lower()] = b
            except Exception:
                continue
    except Exception:
        pass
    return ab, gb


def mood_boost_for(db, user_id: str, context: str) -> dict[str, float]:
    """Буст настроения-руки (kind='mood') в контексте. Нет рук → {}.

    Пишется в update_taste_signals best-effort; старые базы без mood-рук
    просто дают пусто и ничего не ломают.
    """
    out: dict[str, float] = {}
    try:
        import math as _math

        from app.db.models import TasteArm as _TA

        rows = db.query(_TA).filter(
            _TA.user_id == str(user_id), _TA.context == str(context),
            _TA.kind == "mood").all()
        total = sum(int(r.pulls or 0) for r in rows) or 0
        for r in rows:
            try:
                pulls = int(r.pulls or 0)
                if pulls <= 0:
                    continue
                avg = float(r.reward or 0.0) / pulls
                exploit = min(max(avg / 10.0, -1.0), 1.0) * 0.08
                explore = 0.04 * _math.sqrt(
                    _math.log(total + 1.0) / (pulls + 1.0)) if total else 0.0
                out[str(r.name).lower()] = min(max(exploit + explore, -0.08), 0.12)
            except Exception:
                continue
    except Exception:
        pass
    return out


def session_assoc(db, user_id: str, seed_ids: list[str],
                  limit_events: int = 1500, gap_min: int = 30) -> dict[str, float]:
    """Item-similarity по сессиям (порт mobile item_similarity, on-demand).

    Сессии — цепочки PlayHistory с разрывом >gap_min. Пара в сессии +1,
    соседние +3; sim=co/(playsA*playsB), нормировка на max; агрегация по
    сидам — MAX, сиды исключаются. Возвращает 0..1 на трек."""
    out: dict[str, float] = {}
    seeds = {str(s) for s in (seed_ids or [])}
    if not seeds:
        return out
    try:
        from datetime import datetime as _dt

        from app.db.models import PlayHistory as _PH

        rows = db.query(_PH.track_id, _PH.played_at).filter(
            _PH.user_id == str(user_id)).order_by(
            _PH.played_at.desc()).limit(max(100, limit_events)).all()
        # в хронологическом порядке, режем на сессии по разрыву
        ordered = sorted([(str(t), w) for t, w in rows if w],
                         key=lambda kv: kv[1])
        sessions: list[list[str]] = []
        cur: list[str] = []
        prev = None
        for tid, when in ordered:
            if prev is not None:
                try:
                    gap = (when - prev).total_seconds() / 60.0
                except Exception:
                    gap = 0.0
                if gap > gap_min:
                    if cur:
                        sessions.append(cur)
                    cur = []
            cur.append(tid)
            prev = when
        if cur:
            sessions.append(cur)
        from collections import Counter as _C

        co: _C = _C()
        plays: _C = _C()
        for sess in sessions[-200:]:
            for tid in sess:
                plays[tid] += 1
            for i, a in enumerate(sess):
                for j, b in enumerate(sess):
                    if a == b:
                        continue
                    if a in seeds or b in seeds:
                        co[(a, b)] += 3 if abs(i - j) == 1 else 1
        scored: dict[str, float] = {}
        for (a, b), c in co.items():
            other = b if a in seeds else (a if b in seeds else None)
            if other is None or other in seeds:
                continue
            try:
                s = float(c) / max(1, plays[a] * plays[b])
            except Exception:
                continue
            if s > scored.get(other, 0.0):
                scored[other] = s
        mx = max(scored.values()) if scored else 0.0
        if mx > 0:
            out = {tid: v / mx for tid, v in scored.items()}
    except Exception:
        pass
    return out


def adaptive_count(requested: int, drift: dict | None,
                   morphing: bool = False,
                   tun: dict | None = None) -> tuple[int, str | None]:
    """Адаптивная пачка: маленькая при смене настроения, большая в стабильном.

    Большая пачка (=10) при смене вкуса — это 30-40 минут старого вайба,
    пока хвост дослушается. Поэтому при скип-стрике и при морфинге в целевой
    муд режем пачку (пол 4 — очередь не голодает, refill и так при остатке
    <=3). В стабильном вайбе и на разогреве — полная пачка.
    Возвращает (effective, reason|None). reason None = выдали сколько просили.
    Капы — из личного тюнинга, дефолт strong 4 / moderate 5 / mild-morph 6.
    """
    tun = tun or {}
    _cap_s = _tun_int(tun, 'playlists', 'adaptive_strong', 4)
    _cap_m = _tun_int(tun, 'playlists', 'adaptive_moderate', 5)
    _cap_mild = _tun_int(tun, 'playlists', 'adaptive_mild', 6)
    _cap_morph = _tun_int(tun, 'playlists', 'adaptive_morphing', 6)
    _floor = _tun_int(tun, 'playlists', 'adaptive_floor', 4)
    try:
        n = max(1, min(100, int(requested or 20)))
    except (TypeError, ValueError):
        n = 20
    if n <= _floor:
        return n, None
    cap, bits = n, []
    sev = (drift or {}).get("severity")
    if sev == "strong":
        cap, bits = min(cap, _cap_s), bits + ["скипы ×7: разворот"]
    elif sev == "moderate":
        cap, bits = min(cap, _cap_m), bits + ["скипы ×5: быстрая пачка"]
    elif sev == "mild":
        cap, bits = min(cap, _cap_mild), bits + ["скипы ×3: пачка меньше"]
    if morphing:
        cap, bits = min(cap, _cap_morph), bits + ["переход настроения"]
    eff = max(min(cap, n), min(_floor, n))
    if eff >= n:
        return n, None
    return eff, " + ".join(bits) or None


def _smooth_energy_pass(items: list[dict], max_step: float = 0.18,
                        start_energy: float | None = None,
                        tun: dict | None = None) -> list[dict]:
    """Плавная раскладка по энергии + отчёт в лог.

    Раньше здесь стоял create_energy_wave в голом try/except pass — то есть
    при любой ошибке оркестрация молча пропадала, и по логам нельзя было понять,
    раскладывалась ли вообще волна. Теперь причина сбоя видна.

    start_energy — энергия трека, который сейчас играет. Без неё раскладка
    стартует от края шкалы и на стыке батчей получается скачок: только что
    играл энергичный трек, а мозг выдал спокойный.
    """
    if not items or len(items) < 3:
        return items
    try:
        from app.services.orchestrator import smooth_energy_order as _seo

        tun = tun or {}
        max_step = _tun_float(tun, 'playlists', 'smooth_max_step', max_step)
        _passes = _tun_int(tun, 'playlists', 'smooth_passes', 8)

        def _en_list(seq: list[dict]) -> list[float]:
            out = []
            for r in seq:
                try:
                    v = r.get("energy")
                    out.append(float(v) if v is not None else 0.5)
                except (TypeError, ValueError):
                    out.append(0.5)
            return out

        def _ragged(seq: list[float]) -> float:
            s = 0.0
            for a, b in zip(seq, seq[1:]):
                d = abs(a - b)
                s += d
                if d > max_step:
                    s += (d - max_step) * 3.0
            return s

        before_r = _ragged(_en_list(items))
        by_id = {str(r.get("track_id") or ""): r for r in items}
        wrapped = [dict(r, id=str(r.get("track_id") or "")) for r in items]
        ordered = _seo(wrapped, None, max_step=max_step,
                       passes=_passes, start_energy=start_energy)
        new = [by_id.get(str(w.get("id"))) for w in ordered]
        new = [r for r in new if r is not None]
        if len(new) != len(items):
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("energy pass: lost tracks ({} -> {}), order untouched",
                                 len(items), len(new))
            return items
        after_r = _ragged(_en_list(new))
        from app.core.logging import get_logger as _gl

        _gl("wave").info("energy pass: raggedness {:.3f} -> {:.3} over {} tracks (start_energy={})",
                         before_r, after_r, len(new), start_energy)
        return new
    except Exception as e:
        from app.core.logging import get_logger as _gl

        _gl("wave").warning("energy pass failed, order untouched: {}", e)
        return items


def _smooth_keys_order(items: list[dict],
                       tun: dict | None = None) -> list[dict]:
    """Key-сглаживание соседей: пузырьковые свопы, улучшающие суммарную
    совместимость тональностей (квинтовый круг). Своп разрешён, только если
    не рвёт энергетическую дугу (|Δenergy| < key_energy_max, дефолт 0.15).
    Порядок множества не меняет — только локальный порядок соседей."""
    items = list(items)
    if len(items) < 2:
        return items
    tun = tun or {}
    _e_max = _tun_float(tun, 'playlists', 'key_energy_max', 0.15)
    _passes = _tun_int(tun, 'playlists', 'key_passes', 3)
    try:
        from app.services.orchestrator import key_compatibility as _kcs
    except Exception:
        return items

    def _en(r: dict) -> float:
        try:
            v = r.get("energy")
            return float(v) if v is not None else 0.5
        except (TypeError, ValueError):
            return 0.5

    def _tot(ts: list[dict]) -> float:
        s = 0.0
        for a, b in zip(ts, ts[1:]):
            try:
                s += float(_kcs(a.get("key"), a.get("scale"),
                                b.get("key"), b.get("scale")))
            except Exception:
                s += 0.5
        return s

    for _ in range(max(0, _passes)):
        improved = False
        for i in range(len(items) - 1):
            if abs(_en(items[i]) - _en(items[i + 1])) >= _e_max:
                continue
            cur = _tot(items)
            items[i], items[i + 1] = items[i + 1], items[i]
            if _tot(items) > cur + 1e-9:
                improved = True
            else:
                items[i], items[i + 1] = items[i + 1], items[i]
        if not improved:
            break
    return items


def wave_continue(db, user_id: str, queue: list[str] | None = None,
                  current_track_id: str | None = None, count: int = 20,
                  settings: dict | None = None,
                  exclude_ids: list[str] | None = None,
                  recent_events: list[dict] | None = None,
                  ratings_delta: list[dict] | None = None) -> dict:
    """Главная функция: дельта -> сиды -> кандидаты -> скоринг -> следующие N."""
    from app.db.models import ArtistBan, Track, TrackDislike

    settings = settings or {}
    count = max(1, min(100, int(count or 20)))

    applied = apply_delta(db, user_id, ratings_delta, recent_events)

    # Сессионный дрейф (порт MoodDriftDetector): скипы подряд остужают волну,
    # скипнутое исключаем из кандидатов — только на этот запрос, в БД не пишем.
    drift = session_drift(db, recent_events, user_id)

    _raw_queue = list(queue or []) + list(exclude_ids or [])
    played = set(_resolve_ids(db, _raw_queue))
    if _raw_queue and len(played) < len(set(map(str, _raw_queue))):
        # Часть id очереди не резолвится в Track (внешние id без совпадения
        # по external_id/uuid): такие треки фильтр по id не прикроет —
        # дубли и возвраты возможны. Логируем для диагностики.
        try:
            from app.core.logging import get_logger as _gl2

            _gl2("wave").warning("wave queue resolve miss: {}/{} ids unresolved",
                                 len(set(map(str, _raw_queue))) - len(played),
                                 len(set(map(str, _raw_queue))))
        except Exception:
            pass
    played.update(drift.get("skip_ids") or [])
    if current_track_id:
        played.update(_resolve_ids(db, [current_track_id]))

    seeds = select_seeds(db, user_id,
                         characteristic=settings.get('characteristic'), limit=5)
    cur_ids: list[str] = []
    if current_track_id:
        cur = _resolve_ids(db, [current_track_id])
        cur_ids = cur
        seeds = (cur + seeds)[:5]

    # Стартовое настроение — для плавного морфинга в целевое.
    # Целевое: settings.mood (выбор юзера на странице «Моя волна»).
    from app.db.models import TrackFeatures as _TF0

    target_mood = (settings.get('mood') or '').strip().lower() or None
    start_mood: str | None = None
    ref_key: tuple | None = None
    ref_sentiment: str | None = None
    _cf = None
    if cur_ids:
        try:
            _cf = db.query(_TF0).filter(_TF0.track_id == cur_ids[0]).first()
            start_mood = _mood_of(_cf)
            if _cf is not None:
                ref_key = (getattr(_cf, "key_name", None),
                           getattr(_cf, "scale", None))
                try:
                    _mv = getattr(_cf, "mood_vector", None) or {}
                    ref_sentiment = str(_mv.get("ai_sentiment") or "").lower() or None
                except Exception:
                    ref_sentiment = None
        except Exception:
            start_mood = None
    morphing = bool(target_mood and start_mood and target_mood != start_mood)
    # Целевой сентимент лирики по муд-пресету (иначе держим текущий вайб).
    _POS_MOODS = {"energetic", "excited", "happy", "upbeat"}
    _NEG_MOODS = {"sad", "melancholic", "dark", "aggressive"}
    target_sentiment: str | None = None
    if target_mood:
        if target_mood in _POS_MOODS:
            target_sentiment = "positive"
        elif target_mood in _NEG_MOODS:
            target_sentiment = "negative"

    # Исключения: очередь + дизлайки + баны
    dis = {str(r.track_id) for r in
           db.query(TrackDislike).filter_by(user_id=user_id).all()}
    bans = {str(r.artist_name) for r in
            db.query(ArtistBan).filter_by(user_id=user_id).all()}

    # collab-подмес: кто у похожих в топе — тем выше collabScore.
    # Считаем ДО формирования пула кандидатов: раньше это было ниже, и
    # коллаборативные треки часто просто не попадали в первые 2000 строк
    # выборки (limit без ORDER BY), то есть даже при идеально посчитанной
    # коллаборации её вывод не доходил до скора.
    collab_scores: dict[str, float] = {}
    try:
        from app.services import collab as _cb
        rec = _cb.recommend_for_user(db, user_id, n=200)
        for it in rec.get('items', []):
            collab_scores[str(it['track_id'])] = float(it.get('score', 0) or 0)
    except Exception as e:
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("collab recommend failed: {}", e)
        except Exception:
            pass

    # Пул кандидатов: всё кроме сыгранного/дизлайков/банов, капом 2000.
    # Без IN-чанков: берём с запасом и режем в питоне (старый цикл
    # исключал в SQL только первый чанк skip и всё равно дофильтровывал).
    #
    # Только то, что реально сыграет плеер. Локальные файлы (disk:/demo-) в
    # плейлист не попадают: у них нет Navidrome-id, поэтому при выгрузке они
    # молча выбрасываются и очередь на выходе оказывается короче запрошенного.
    # В демо-режиме фильтр выключается сам (playable.has_navidrome) — там диск
    # и есть единственная библиотека.
    q = db.query(Track.id)
    try:
        from app.services import playable as _pl

        q = _pl.apply(q, db)
    except Exception as e:  # noqa: BLE001 — лучше лишний трек, чем пустая волна
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("playable filter unavailable, using full pool: {}", e)
        except Exception:
            pass
    skip_n = len(played | dis)
    cand: list[str] = [str(r[0]) for r in q.limit(2000 + skip_n).all()]
    cand = [c for c in cand if c not in played and c not in dis][:2000]
    # Гарантируем, что коллаборативные треки в пуле есть: если библиотека
    # больше 2000 и limit без ORDER BY отсёк нужные строки, добавляем их явно.
    # Без этого collab-вес просто не участвовал бы в скоре части треков.
    if collab_scores:
        _cset = set(cand)
        _missing = [t for t in collab_scores if t not in _cset and t not in played and t not in dis]
        if _missing:
            cand = cand + _missing[:200]
            try:
                from app.core.logging import get_logger as _gl

                _gl("wave").info("collab: добавлено {} треков в пул сверх лимита", len(_missing[:200]))
            except Exception:
                pass
    # Мета кандидатов — один проход чанками (SQLite держит ~999 vars в IN).
    # Нужна и для банов, и для вырезания «той же песни» под другим row id.
    meta: dict[str, Any] = {}
    if cand:
        for i in range(0, len(cand), 500):
            chunk = cand[i:i + 500]
            try:
                for t in db.query(Track).filter(Track.id.in_(chunk)).all():
                    meta[str(t.id)] = t
            except Exception:
                pass
    # Баны режем по мета (нужен artist) — батчем.
    # Сравнение через artist_names: бан «GASHI» ловит и трек
    # «Dark Polo Gang/GASHI/Capo Plaza», регистр не важен.
    if bans and cand:
        from app.services.artist_names import is_banned as _is_banned

        cand = [c for c in cand
                if not (meta.get(c) and _is_banned(meta[c].artist_name, bans))]
    # «Та же песня» под другим row id (дубль после перескана): по id-фильтр
    # её не ловит — получаем дубли в очереди и возврат задизлайканного.
    # Ключ — нормализованные артист+название (как в dedup.norm_text).
    # Сюда же — интра-дедуп: один ответ не содержит песню дважды.
    excluded_keys: set[tuple] = set()
    try:
        from app.services.dedup import norm_text as _norm_text

        def _song_key(artist: Any, title: Any) -> tuple | None:
            a, t = _norm_text(artist), _norm_text(title)
            return (a, t) if (a and t) else None

        _missing = [str(tid) for tid in
                    list(played) + list(dis) + list(drift.get("skip_ids") or [])
                    if str(tid) not in meta]
        for i in range(0, len(_missing), 500):
            try:
                for t in db.query(Track).filter(
                        Track.id.in_(_missing[i:i + 500])).all():
                    meta[str(t.id)] = t
            except Exception:
                pass
        for tid in list(played) + list(dis) + list(drift.get("skip_ids") or []):
            tm = meta.get(str(tid))
            if tm is not None:
                k = _song_key(tm.artist_name, tm.title)
                if k:
                    excluded_keys.add(k)
        if excluded_keys or cand:
            seen_keys: set[tuple] = set()
            deduped: list[str] = []
            for c in cand:
                tm = meta.get(c)
                k = _song_key(tm.artist_name, tm.title) if tm is not None else None
                if k and (k in excluded_keys or k in seen_keys):
                    continue
                if k:
                    seen_keys.add(k)
                deduped.append(c)
            if len(deduped) != len(cand):
                from app.core.logging import get_logger as _gl

                _gl("wave").info("wave song-key dedup: {} -> {} (queue/dis/skip)",
                                 len(cand), len(deduped))
            cand = deduped
    except Exception:
        pass
    # Временный бан жанра из дрейфа (3+ скипа жанра за сессию): только запрос.
    _tmp_genres = set(drift.get("temp_banned_genres") or [])
    if _tmp_genres and cand:
        try:
            _gm = {str(t.id): (t.genre or "") for t in
                   db.query(Track).filter(Track.id.in_(cand[:2000])).all()}
        except Exception:
            _gm = {}
        cand = [c for c in cand if _gm.get(c, "").lower() not in _tmp_genres]
    # mood-фильтр как в мобиле (по первому муд-лейблу).
    # При морфинге (старт → цель) жёсткий фильтр снимаем: пускаем оба
    # настроения + безфичные, а градиент раскладываем после скоринга.
    mood = target_mood
    if mood and cand:
        from app.db.models import TrackFeatures
        fm = {str(f.track_id): f for f in
              db.query(TrackFeatures).filter(
                  TrackFeatures.track_id.in_(cand[:2000])).all()}
        if morphing:
            allowed = {start_mood, mood}
            cand = [c for c in cand
                    if (c not in fm or _mood_of(fm[c]) in allowed)]
        else:
            cand = [c for c in cand
                    if (_mood_of(fm[c]) == mood if c in fm else True)]

    # Фильтр по языку (пилюли клиента «по языку»): ru / foreign / instrumental.
    # Язык берём из Lyrics.language (детект при скачивании текстов).
    # Треки без текстов НЕ выкидываем (unknown = пропуск) — иначе пустая очередь,
    # кроме опционального settings.language_strict=true (только доказанные).
    # Кириллический fast-path (_meta_ru_ids): трек без текстов, но с кириллицей
    # в метаданных — точно русский: держится в 'ru', выкидывается из 'foreign'
    # и 'instrumental'. Латиница без текстов — по-прежнему unknown=pass.
    lang = _norm_language(settings.get('language'))
    strict = bool(settings.get('language_strict'))
    if lang and cand:
        from app.db.models import Lyrics as _Ly

        try:
            from app.services.lyrics import detect_lyrics_language as _det_lang

            _lm: dict[str, str] = {}
            for r in db.query(_Ly).filter(_Ly.track_id.in_(cand[:2000])).all():
                _lang_v = (r.language or '').strip().lower()
                if not _lang_v:
                    try:
                        _lang_v = _det_lang(getattr(r, 'text', None))
                    except Exception:
                        _lang_v = ''
                _lm[str(r.track_id)] = _lang_v
        except Exception:
            _lm = {}
        _meta_ru: set = set()
        if lang in ('foreign', 'instrumental') or strict:
            try:
                _meta_ru = _meta_ru_ids(db, [c for c in cand[:2000] if c not in _lm])
            except Exception:
                _meta_ru = set()
        if lang == 'ru':
            if strict:
                cand = [c for c in cand
                        if (_lm.get(c) or '') == 'ru' or c in _meta_ru]
            else:
                cand = [c for c in cand
                        if c not in _lm or (_lm.get(c) or '') == 'ru']
        elif lang == 'foreign':
            if strict:
                cand = [c for c in cand if (_lm.get(c) or '') == 'foreign']
            else:
                cand = [c for c in cand
                        if (_lm.get(c) or '') != 'ru' and c not in _meta_ru]
        elif lang == 'instrumental':
            if strict:
                cand = [c for c in cand if (_lm.get(c) or '') == 'instrumental']
            else:
                cand = [c for c in cand
                        if (c not in _lm or (_lm.get(c) or '') == 'instrumental')
                        and c not in _meta_ru]

    # collab-подмес считается выше по коду (до формирования пула кандидатов),
    # чтобы его треки гарантированно попадали в скоренное окно.

    # recent_events для behaviorBonus — нормализуем id к нашим
    norm_events: list[dict] = []
    for e in (recent_events or []):
        if not isinstance(e, dict):
            continue
        tids = _resolve_ids(db, [str(e.get('track_id') or '')])
        if tids:
            norm_events.append({'track_id': tids[0], 'action': e.get('action'),
                               'position_sec': e.get('position_sec')})

    # KMeans-кластеры сидов: кандидаты с той же «полки» библиотеки —
    # точнее попадание (бонус в скоринге). Нет кластеров — тихо пропускаем.
    cluster_map: dict[str, int] = {}
    seed_clusters: set[int] = set()
    try:
        from app.db.models import TrackCluster as _TC

        _need_cl = list({str(c) for c in (cand[:800] + seeds)})
        for i in range(0, len(_need_cl), 500):
            try:
                for row in db.query(_TC).filter(
                        _TC.algorithm == "kmeans",
                        _TC.track_id.in_(_need_cl[i:i + 500])).all():
                    cluster_map[str(row.track_id)] = int(row.cluster_id)
            except Exception:
                pass
        seed_clusters = {cluster_map[s] for s in seeds if s in cluster_map}
    except Exception:
        cluster_map, seed_clusters = {}, set()

    # Сессионный fingerprint: что играет/доиграно прямо сейчас (порт mobile
    # rolling fingerprint) — волна подстраивается под текущий заход.
    session_ids: list[str] = list(cur_ids)
    for e in norm_events:
        if str(e.get("action") or "") in ("play", "complete", "replay", "like"):
            _sid = str(e.get("track_id") or "")
            if _sid and _sid not in session_ids:
                session_ids.append(_sid)
    session_ids = session_ids[:20]
    # Негатив: дизлайки (топ-500 свежих) — похожее штрафуем вектором.
    neg_ids: list[str] = []
    try:
        neg_ids = [str(r.track_id) for r in
                   db.query(TrackDislike).filter_by(user_id=user_id)
                   .order_by(TrackDislike.created_at.desc()).limit(500).all()]
    except Exception:
        neg_ids = list(dis)[:500]
    # Recency: последнее прослушивание кандидатов (затухание новизны).
    last_played: dict[str, Any] = {}
    try:
        from datetime import timedelta as _td

        from app.db.models import PlayHistory as _PH

        _lp_ids = list(dict.fromkeys(list(cand[:800]) + list(played)))[:1200]
        for i in range(0, len(_lp_ids), 500):
            try:
                for tid, when in db.query(_PH.track_id, _PH.played_at).filter(
                        _PH.user_id == user_id,
                        _PH.track_id.in_(_lp_ids[i:i + 500])).all():
                    if when and (str(tid) not in last_played or
                                 when > last_played[str(tid)]):
                        last_played[str(tid)] = when
            except Exception:
                pass
    except Exception:
        pass
    # Усталость артиста: сколько раз гоняли за 7 дней.
    fatigue: dict[str, int] = {}
    try:
        from datetime import datetime as _dt2
        from datetime import timedelta as _td2

        from app.db.models import PlayHistory as _PH2

        _since = _dt2.utcnow() - _td2(days=7)
        _recent: list[str] = []
        try:
            _recent = [str(r[0]) for r in
                       db.query(_PH2.track_id).filter(
                           _PH2.user_id == user_id,
                           _PH2.played_at >= _since)
                       .order_by(_PH2.played_at.desc()).limit(5000).all()]
        except Exception:
            _recent = []
        if _recent:
            _amap: dict[str, str] = {}
            _uniq = list(dict.fromkeys(_recent))
            for i in range(0, len(_uniq), 500):
                try:
                    for t in db.query(Track).filter(
                            Track.id.in_(_uniq[i:i + 500])).all():
                        if t.artist_name:
                            _amap[str(t.id)] = t.artist_name
                except Exception:
                    pass
            _cnt: dict[str, int] = {}
            for tid in _recent:
                _an = _amap.get(tid)
                if _an:
                    _cnt[_an] = _cnt.get(_an, 0) + 1
            fatigue = _cnt
    except Exception:
        pass

    # Джиттер — от содержимого очереди, а не только её длины: иначе две
    # разные очереди одной длины дают одинаковый порядок (повторы хит-парада).
    try:
        import hashlib as _hl

        _pq = _hl.blake2s(",".join(sorted(played)).encode(),
                          digest_size=8).hexdigest()
    except Exception:
        _pq = str(len(played))
    # Час и контекст: «часто в этот час» + руки бандита + ассоциации.
    try:
        from app.core.time import local_hour as _local_hour

        _cur_hour = _local_hour()
    except Exception:
        _cur_hour = datetime.now().hour
    try:
        from app.core.time import server_now as _server_now
        from app.services.taste import context_of as _ctx_of

        _dow = _server_now().isoweekday() % 7
        _ctx = _ctx_of(_cur_hour, _dow)
    except Exception:
        _ctx = "day_wd"
    time_map = time_bonus_for(db, user_id, cand[:800], _cur_hour)
    arm_artist, arm_genre = arm_boost_for(db, user_id, _ctx)
    try:
        mood_arm_map = mood_boost_for(db, user_id, _ctx)
    except Exception:
        mood_arm_map = {}
    assoc = session_assoc(db, user_id, seeds)
    # CLAP-карта пула+сидов+сессии: нет анализа → {} → вклад 0, не ломается.
    try:
        _clap_ids = list(dict.fromkeys(
            list(cand[:800]) + list(seeds) + list(session_ids)))[:1500]
        clap_map = _load_clap_map(db, _clap_ids)
    except Exception:
        clap_map = {}
    # Энергия текущего трека — якорь скип-риска (резкий скачок = риск).
    _cur_en2: float | None = None
    try:
        if _cf is not None and getattr(_cf, "energy", None) is not None:
            _cur_en2 = float(_cf.energy)
    except (TypeError, ValueError):
        _cur_en2 = None
    # Авто-настроение: ручная пилюля побеждает всегда; авто — только мягкий
    # ctx-бонус в скоринге, жёсткий mood-фильтр выше уже отработал по ручной.
    _manual_mood = (settings.get('mood') or '').strip().lower() or None
    _auto_mood: str | None = None
    try:
        if not _manual_mood:
            _auto_mood = infer_auto_mood(db, user_id, session_ids, _cur_hour)
    except Exception:
        _auto_mood = None
    settings_eff = dict(settings)
    mood_source = "manual" if _manual_mood else ("auto" if _auto_mood else "none")
    if mood_source == "auto" and _auto_mood:
        settings_eff["mood"] = _auto_mood
    # Целевой сентимент под эффективное настроение (для lyr_c).
    _eff_mood_norm = None
    try:
        _eff_mood_norm = _norm_mood(settings_eff.get("mood"))
    except Exception:
        _eff_mood_norm = None
    target_sentiment_eff = target_sentiment
    try:
        if _eff_mood_norm and not _manual_mood:
            if _eff_mood_norm in _POS_MOODS:
                target_sentiment_eff = "positive"
            elif _eff_mood_norm in _NEG_MOODS:
                target_sentiment_eff = "negative"
    except Exception:
        pass
    # Скоренное окно: обычный пул режем 800, но коллаборативные треки,
    # добавленные в cand сверх лимита, в это окно не попали бы — и их вклад
    # в скор снова исчез бы. Поэтому берём обычные 800 и сверху добавляем
    # сами коллаборативные (их не больше 200, всего ~1000 строк на скоринг).
    _score_pool = cand[:800]
    if collab_scores:
        _in_pool = set(_score_pool)
        _extra = [t for t in collab_scores if t in cand and t not in _in_pool]
        if _extra:
            _score_pool = _score_pool + _extra[:200]
    ranked = score_candidates(db, user_id, _score_pool, seeds, settings_eff,
                              norm_events, collab_scores,
                              current_hour=_cur_hour,
                              jitter_seed=f"{user_id}:{len(played)}:{_pq}",
                              drift=drift, ref_key=ref_key,
                              cluster_map=cluster_map or None,
                              seed_clusters=seed_clusters or None,
                              ref_sentiment=ref_sentiment,
                              target_sentiment=target_sentiment_eff,
                              session_ids=session_ids or None,
                              neg_ids=neg_ids or None,
                              last_played=last_played or None,
                              fatigue=fatigue or None,
                              time_map=time_map or None,
                              arm_artist=arm_artist or None,
                              arm_genre=arm_genre or None,
                              assoc=assoc or None,
                              clap_map=clap_map or None,
                              clap_weight=None,
                              mood_arm=mood_arm_map or None,
                              cur_energy=_cur_en2)
    # Недавнее реже (новизна как у cold-start novelty=True): уже учтено
    # novelty-членом, дубли очереди на всякий случай режем ещё раз
    ranked = [r for r in ranked if r['track_id'] not in played]
    # Адаптивная пачка: при смене настроения — меньше, чтобы новый вайб
    # было слышно через 4-5 треков, а не через 10. Капы — личные.
    eff_count, adapt_reason = adaptive_count(count, drift, morphing,
                                            _tun_all(db, user_id))
    top = ranked[:eff_count]
    # Финальная страховка на ответе: id + баны артистов + «та же песня».
    # Ловит всё, что просочилось через срез пула (cand[:800]/топ).
    if top and (dis or bans):
        try:
            from app.services.artist_names import is_banned as _is_banned2
            from app.services.dedup import norm_text as _norm_text2

            def _sk2(artist: Any, title: Any) -> tuple | None:
                a, t = _norm_text2(artist), _norm_text2(title)
                return (a, t) if (a and t) else None

            _safe: list[dict] = []
            for r in top:
                tid = str(r.get("track_id") or "")
                if tid in played or tid in dis:
                    continue
                if _is_banned2(r.get("artist_name"), bans):
                    continue
                if _sk2(r.get("artist_name"), r.get("title")) in excluded_keys:
                    continue
                _safe.append(r)
            if len(_safe) != len(top):
                from app.core.logging import get_logger as _gl3

                _gl3("wave").warning("wave final net dropped {} tracks (ban/dis/dupe)",
                                     len(top) - len(_safe))
            top = _safe
        except Exception:
            pass
    # Обогащаем ответ настроением/энергией/обложкой для страницы «Моя волна».
    # Без отдельных запросов за треками.
    try:
        from app.db.models import Track as _T
        from app.db.models import TrackFeatures as _TF
        from app.services.covers import resolve_track_cover_id as _resolve_cover

        _ids = [str(r.get("track_id") or "") for r in top if r.get("track_id")]
        _fm = {str(f.track_id): f for f in
               db.query(_TF).filter(_TF.track_id.in_(_ids)).all()} if _ids else {}
        _tm = {str(t.id): t for t in
               db.query(_T).filter(_T.id.in_(_ids)).all()} if _ids else {}
        for r in top:
            f = _fm.get(str(r.get("track_id") or ""))
            try:
                moods = list(f.mood_labels or []) if f is not None else []
            except Exception:
                moods = []
            r["mood"] = str(moods[0]).lower() if moods else None
            r["moods"] = [str(m).lower() for m in moods[:3]]
            try:
                r["energy"] = float(f.energy) if f is not None and f.energy is not None else None
            except (TypeError, ValueError):
                r["energy"] = None
            try:
                r["tempo"] = float(f.tempo_bpm) if f is not None and f.tempo_bpm else None
            except (TypeError, ValueError):
                r["tempo"] = None
            try:
                r["key"] = getattr(f, "key_name", None) if f is not None else None
                r["scale"] = getattr(f, "scale", None) if f is not None else None
            except Exception:
                r["key"] = r["scale"] = None
            try:
                t = _tm.get(str(r.get("track_id") or ""))
                r["cover_art_id"] = _resolve_cover(db, t) if t is not None else None
            except Exception:
                r["cover_art_id"] = None
    except Exception:
        pass
    morph = None
    # Энергия текущего трека — якорь для плавной раскладки (см. _smooth_energy_pass).
    _cur_en: float | None = None
    if _cf is not None:
        try:
            if _cf.energy is not None:
                _cur_en = float(_cf.energy)
        except (TypeError, ValueError):
            _cur_en = None
    if morphing and top:
        # Градиент: голова — стартовое настроение, хвост — целевое,
        # середина — лучшее остальное. Плавный уход в выбранный муд.
        g_start = [r for r in top if (r.get("mood") or None) == start_mood]
        g_target = [r for r in top if (r.get("mood") or None) == target_mood]
        _mtun = _tun_all(db, user_id)
        head = _smooth_keys_order(g_start[:3], _mtun)
        tail = _smooth_keys_order(g_target[:4], _mtun)
        used = {str(r.get("track_id")) for r in head + tail}
        mid_n = max(0, len(top) - len(head) - len(tail))
        mid = _smooth_keys_order(
            [r for r in top if str(r.get("track_id")) not in used][:mid_n],
            _mtun)
        top = head + mid + tail
        morph = {"from": start_mood, "to": target_mood}
        # Раньше здесь энергетическая дуга не применялась вовсе: ветка morph
        # шла вместо неё, то есть при ВЫБРАННОМ настроении (главный сценарий)
        # переходов по энергии не было. Теперь дуга есть в обеих ветках.
        top = _smooth_energy_pass(top, start_energy=_cur_en, tun=_mtun)
    elif len(top) > 3:
        # Без целевого настроения — раскладываем оркестратором: плавные
        # переходы по энергии, затем key-сглаживание.
        _mtun = _tun_all(db, user_id)
        top = _smooth_energy_pass(top, start_energy=_cur_en, tun=_mtun)
        top = _smooth_keys_order(top, _mtun)
    # Мостик: первый трек выдачи — плавное продолжение текущего.
    # Если переход резкий — подтягиваем лучший мостик из топ-10 окна.
    if cur_ids and top and _cf is not None:
        try:
            from app.services.orchestrator import key_compatibility as _kcb

            try:
                _cur_en = float(_cf.energy) if _cf is not None and \
                    _cf.energy is not None else 0.5
            except (TypeError, ValueError):
                _cur_en = 0.5

            def _bridge(r: dict) -> float:
                try:
                    _de = abs(float(r.get("energy")
                                    if r.get("energy") is not None else 0.5) - _cur_en)
                except (TypeError, ValueError):
                    _de = 0.5
                try:
                    _kk = float(_kcb(ref_key[0] if ref_key else None,
                                      ref_key[1] if ref_key else None,
                                      r.get("key"), r.get("scale")))
                except Exception:
                    _kk = 0.5
                _mm = 1.0 if (r.get("mood") and start_mood and
                              r.get("mood") == start_mood) else 0.5
                return _kk * 0.5 + (1.0 - min(_de, 1.0)) * 0.3 + _mm * 0.2

            _b0 = _bridge(top[0])
            _bi, _bv = 0, _b0
            for _i in range(1, min(10, len(top))):
                _s = _bridge(top[_i])
                if _s > _bv:
                    _bi, _bv = _i, _s
            if _bi and _bv > _b0 + 0.05:
                top[0], top[_bi] = top[_bi], top[0]
        except Exception:
            pass
    # Растяжка скоров окна: сырые total упираются в кламп 1.0 и весь топ
    # выглядит как «1.00, 1.00, …». Монотонно — порядок не трогаем.
    spread_scores(top)
    # Метрики волны: фиксируем, что эти треки реально показаны, с их score и
    # причиной. Без этого в истории не отличить «мозг предложил» от «юзер сам
    # поставил», и улучшать скоринг приходится вслепую.
    try:
        from app.services import rec_feedback as _rfb

        _rfb.mark_served(db, user_id, top, source="wave")
    except Exception as e:
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("rec_feedback mark_served failed: {}", e)
        except Exception:
            pass
    return {'tracks': top, 'seeds': seeds,
            'applied': applied,
            'current_mood': start_mood,
            'morph': morph,
            'count_requested': count,
            'count_effective': eff_count,
            'adaptive': adapt_reason,
            'auto_mood': _auto_mood,
            'mood_source': mood_source,
            'mood_effective': settings_eff.get('mood') or None,
            'drift': {k: drift.get(k) for k in
                      ("severity", "consecutive_skips", "temp_banned_genres",
                       "warmth", "positive_streak")}
            if (drift.get("severity") or drift.get("warmth")) else None,
            'profile_version': datetime.utcnow().isoformat() + 'Z'}
