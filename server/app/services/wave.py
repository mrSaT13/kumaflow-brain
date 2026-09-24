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


def _weights(total_likes: int) -> dict[str, float]:
    if total_likes < 50:
        return {'audio': 0.20, 'genre': 0.30, 'artist': 0.10,
                'behavior': 0.10, 'collab': 0.25, 'novelty': 0.05}
    return {'audio': 0.40, 'genre': 0.20, 'artist': 0.10,
            'behavior': 0.20, 'collab': 0.05, 'novelty': 0.05}


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
    if characteristic == 'favorite':
        top_ratio, recent_ratio = 0.7, 0.2
    elif characteristic == 'unfamiliar':
        top_ratio, recent_ratio = 0.3, 0.2
    elif characteristic == 'popular':
        top_ratio, recent_ratio = 0.6, 0.3
    else:
        top_ratio, recent_ratio = 0.5, 0.25
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


def session_drift(db, recent_events: list[dict] | None) -> dict:
    """Порт мобильного MoodDriftDetector: сессия, а не вечность.

    recent_events — в хронологическом порядке (как шлёт клиент).
    - 3/5/7 скипов подряд в хвосте -> mild/moderate/strong (энергия/темп вниз).
    - 3+ скипа жанра -> временный бан жанра НА ЭТОТ ЗАПРОС (в БД не пишем).
    - скипнутые треки -> исключить из кандидатов НА ЭТОТ ЗАПРОС.
    Постоянные счётчики (3 скипа ever -> автодизлайк) не трогаем.
    """
    events = [e for e in (recent_events or []) if isinstance(e, dict)]
    # Хвостовые скипы подряд (позитив обнуляет серию — как logPositiveInteraction).
    trailing = 0
    for e in reversed(events):
        if str(e.get("action") or "") == "skip":
            trailing += 1
        else:
            break
    if trailing >= 7:
        severity, energy_shift, tempo_shift = "strong", -0.3, -30
    elif trailing >= 5:
        severity, energy_shift, tempo_shift = "moderate", -0.2, -20
    elif trailing >= 3:
        severity, energy_shift, tempo_shift = "mild", -0.1, -10
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
    if warm_streak >= 7:
        warmth, energy_shift, tempo_shift = "strong", 0.3, 30
    elif warm_streak >= 5:
        warmth, energy_shift, tempo_shift = "moderate", 0.2, 20
    elif warm_streak >= 3:
        warmth, energy_shift, tempo_shift = "mild", 0.1, 10
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
    temp_banned = sorted([g for g, n in genre_hits.items() if n >= 3])
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
                     assoc: dict[str, float] | None = None) -> list[dict]:
    """Порт TrackScorer.scoreAndRankTracks на наших таблицах.

    jitter_seed: детерминированный per-user джиттер вместо глобального random
    (иначе у юзеров без данных порядок одинаковый — все вкусовые члены нули).
    """
    from app.db.models import Track, TrackFeatures
    from app.services import taste as _taste
    from app.services.ml import _cosine, _feature_vector

    settings = settings or {}
    _rng = random.Random(jitter_seed) if jitter_seed else random
    activity_raw = (settings.get('activity') or '').strip()
    activity = activity_raw.lower() or None
    mood = _norm_mood(settings.get('mood'))
    try:
        from app.core.time import local_hour as _local_hour

        hour = current_hour if current_hour is not None else _local_hour()
    except Exception:
        hour = current_hour if current_hour is not None else datetime.now().hour
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
    w = _weights(likes_total)
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

    # behaviorBonus по свежим событиям (last 10 как в мобиле)
    bonus: dict[str, float] = Counter()
    for e in (recent_events or [])[:10]:
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
            bonus[tid] += 0.2
        elif a in ('seek_back', 'replay'):
            bonus[tid] += 0.25
        elif a == 'complete':
            bonus[tid] += 0.2
        elif a == 'play' and pos >= 180:
            bonus[tid] += 0.1
        elif a == 'abandon':
            bonus[tid] -= 0.15
        elif a == 'skip' and pos < 30:
            bonus[tid] -= 0.3
        elif a == 'skip' and pos >= 120:
            bonus[tid] -= 0.1

    used_artists: dict[str, int] = {}
    used_genres: dict[str, int] = {}
    used_moods: dict[str, int] = {}
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
                    neg_pen = max(0.0, float(_cosine(v, neg_centroid))) * 0.15
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
                if _age_h < 3:
                    novelty *= 0.3
                elif _age_h < 24:
                    novelty *= 0.6
                elif _age_h < 24 * 7:
                    novelty *= 0.85
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
            arm_b = min(max(arm_b, -0.16), 0.16)
        except (TypeError, ValueError):
            arm_b = 0.0
        # Ассоциация по сессиям (порт mobile item_similarity).
        assoc_b = 0.0
        if assoc:
            try:
                assoc_b = min(max(float(assoc.get(tid, 0.0)), 0.0), 1.0) * 0.08
            except (TypeError, ValueError):
                assoc_b = 0.0
        # Рейтинг/звёздочка из Navidrome (порт mobile serverScore).
        srv_bonus = 0.0
        try:
            if getattr(t, "starred", False):
                srv_bonus += 0.05
            _rt = getattr(t, "rating", None)
            if _rt:
                srv_bonus += min(max(float(_rt) / 5.0, 0.0), 1.0) * 0.10
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
            pen += 0.05 if c == 1 else (0.10 if c == 2 else 0.15)
        if t.genre and t.genre in used_genres:
            c = used_genres[t.genre]
            pen += 0.03 if c == 1 else (0.07 if c == 2 else 0.12)
        # Разнообразие настроений: не класть одно и то же настроение пачкой.
        _cm = _mood_of(f)
        if _cm and _cm in used_moods:
            c = used_moods[_cm]
            pen += 0.03 if c == 1 else (0.06 if c == 2 else 0.10)
        # Усталость артиста за неделю (по истории): загонянное остужаем.
        if fatigue and t.artist_name and t.artist_name in fatigue:
            _fp = int(fatigue[t.artist_name] or 0)
            if _fp > 10:
                pen += 0.15
            elif _fp >= 6:
                pen += 0.10
            elif _fp >= 3:
                pen += 0.05

        total = (w['audio'] * audio + w['genre'] * genre_s +
                 w['artist'] * artist_s + w['behavior'] * behavior +
                 w['collab'] * collab + w['novelty'] * novelty +
                 0.08 * novelty + ctx + bonus.get(tid, 0.0) -
                 pen - neg_pen + srv_bonus + time_b + arm_b + assoc_b +
                 _rng.random() * 0.12 +
                 0.06 * key_c + 0.06 * cluster_c + 0.05 * lyr_c)
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
                             'assoc': round(assoc_b, 3)}})
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
    10*(1+plays/5)*recency, clamp 0..50, /50. Возвращает 0..0.2 на трек."""
    out: dict[str, float] = {}
    if not track_ids:
        return out
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
            out[tid] = min(max(v, 0.0), 50.0) / 50.0 * 0.2
    except Exception:
        pass
    return out


def arm_boost_for(db, user_id: str, context: str) -> tuple[dict[str, float], dict[str, float]]:
    """Exploit бандита (порт mobile): средний reward руки -> буст артиста/жанра.

    Возвращает (artist_boost, genre_boost), значения -0.08..+0.08.
    Explore — джиттером скоринга, отдельно не нужен."""
    ab: dict[str, float] = {}
    gb: dict[str, float] = {}
    try:
        from app.db.models import TasteArm as _TA

        for r in db.query(_TA).filter(
                _TA.user_id == str(user_id), _TA.context == str(context)).all():
            try:
                pulls = int(r.pulls or 0)
                if pulls <= 0:
                    continue
                avg = float(r.reward or 0.0) / pulls
                b = min(max(avg / 10.0, -1.0), 1.0) * 0.08
                if r.kind == "artist":
                    ab[str(r.name)] = b
                elif r.kind == "genre":
                    gb[str(r.name).lower()] = b
            except Exception:
                continue
    except Exception:
        pass
    return ab, gb


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
                   morphing: bool = False) -> tuple[int, str | None]:
    """Адаптивная пачка: маленькая при смене настроения, большая в стабильном.

    Большая пачка (=10) при смене вкуса — это 30-40 минут старого вайба,
    пока хвост дослушается. Поэтому при скип-стрике и при морфинге в целевой
    муд режем пачку (пол 4 — очередь не голодает, refill и так при остатке
    <=3). В стабильном вайбе и на разогреве — полная пачка.
    Возвращает (effective, reason|None). reason None = выдали сколько просили.
    """
    try:
        n = max(1, min(100, int(requested or 20)))
    except (TypeError, ValueError):
        n = 20
    if n <= 4:
        return n, None
    cap, bits = n, []
    sev = (drift or {}).get("severity")
    if sev == "strong":
        cap, bits = min(cap, 4), bits + ["скипы ×7: разворот"]
    elif sev == "moderate":
        cap, bits = min(cap, 5), bits + ["скипы ×5: быстрая пачка"]
    elif sev == "mild":
        cap, bits = min(cap, 6), bits + ["скипы ×3: пачка меньше"]
    if morphing:
        cap, bits = min(cap, 6), bits + ["переход настроения"]
    eff = max(min(cap, n), min(4, n))
    if eff >= n:
        return n, None
    return eff, " + ".join(bits) or None


def _smooth_keys_order(items: list[dict]) -> list[dict]:
    """Key-сглаживание соседей: пузырьковые свопы, улучшающие суммарную
    совместимость тональностей (квинтовый круг). Своп разрешён, только если
    не рвёт энергетическую дугу (|Δenergy| < 0.15). Порядок множества не
    меняет — только локальный порядок соседей."""
    items = list(items)
    if len(items) < 2:
        return items
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

    for _ in range(3):
        improved = False
        for i in range(len(items) - 1):
            if abs(_en(items[i]) - _en(items[i + 1])) >= 0.15:
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
    drift = session_drift(db, recent_events)

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

    # Пул кандидатов: всё кроме сыгранного/дизлайков/банов, капом 2000.
    # Без IN-чанков: берём с запасом и режем в питоне (старый цикл
    # исключал в SQL только первый чанк skip и всё равно дофильтровывал).
    q = db.query(Track.id)
    skip_n = len(played | dis)
    cand: list[str] = [str(r[0]) for r in q.limit(2000 + skip_n).all()]
    cand = [c for c in cand if c not in played and c not in dis][:2000]
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
    # Треки без текстов НЕ выкидываем (unknown = пропуск) — иначе пустая очередь.
    lang = _norm_language(settings.get('language'))
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
        if lang == 'ru':
            cand = [c for c in cand
                    if c not in _lm or (_lm.get(c) or '') == 'ru']
        elif lang == 'foreign':
            cand = [c for c in cand if (_lm.get(c) or '') != 'ru']
        elif lang == 'instrumental':
            cand = [c for c in cand
                    if c not in _lm or (_lm.get(c) or '') == 'instrumental']

    # collab-подмес: кто у похожих в топе — тем выше collabScore
    collab_scores: dict[str, float] = {}
    try:
        from app.services import collab as _cb
        rec = _cb.recommend_for_user(db, user_id, n=200)
        for it in rec.get('items', []):
            collab_scores[str(it['track_id'])] = float(it.get('score', 0) or 0)
    except Exception:
        pass

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
    assoc = session_assoc(db, user_id, seeds)
    ranked = score_candidates(db, user_id, cand[:800], seeds, settings,
                              norm_events, collab_scores,
                              current_hour=_cur_hour,
                              jitter_seed=f"{user_id}:{len(played)}:{_pq}",
                              drift=drift, ref_key=ref_key,
                              cluster_map=cluster_map or None,
                              seed_clusters=seed_clusters or None,
                              ref_sentiment=ref_sentiment,
                              target_sentiment=target_sentiment,
                              session_ids=session_ids or None,
                              neg_ids=neg_ids or None,
                              last_played=last_played or None,
                              fatigue=fatigue or None,
                              time_map=time_map or None,
                              arm_artist=arm_artist or None,
                              arm_genre=arm_genre or None,
                              assoc=assoc or None)
    # Недавнее реже (новизна как у cold-start novelty=True): уже учтено
    # novelty-членом, дубли очереди на всякий случай режем ещё раз
    ranked = [r for r in ranked if r['track_id'] not in played]
    # Адаптивная пачка: при смене настроения — меньше, чтобы новый вайб
    # было слышно через 4-5 треков, а не через 10.
    eff_count, adapt_reason = adaptive_count(count, drift, morphing)
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
    if morphing and top:
        # Градиент: голова — стартовое настроение, хвост — целевое,
        # середина — лучшее остальное. Плавный уход в выбранный муд.
        g_start = [r for r in top if (r.get("mood") or None) == start_mood]
        g_target = [r for r in top if (r.get("mood") or None) == target_mood]
        head = _smooth_keys_order(g_start[:3])
        tail = _smooth_keys_order(g_target[:4])
        used = {str(r.get("track_id")) for r in head + tail}
        mid_n = max(0, len(top) - len(head) - len(tail))
        mid = _smooth_keys_order(
            [r for r in top if str(r.get("track_id")) not in used][:mid_n])
        top = head + mid + tail
        morph = {"from": start_mood, "to": target_mood}
    elif len(top) > 3:
        # Без целевого настроения — раскладываем оркестратором: энергетическая
        # волна calm -> energetic -> calm по реальной energy, затем key-сглаживание.
        try:
            from types import SimpleNamespace as _SN

            from app.services.orchestrator import create_energy_wave as _ew

            _fmap = {str(r.get("track_id") or ""):
                     _SN(energy=r.get("energy")) for r in top}
            _wrapped = [dict(r, id=str(r.get("track_id") or "")) for r in top]
            _waved = _ew(_wrapped, _fmap, segments=3)
            _by_id = {str(r.get("track_id") or ""): r for r in top}
            _new = [_by_id.get(str(w.get("id"))) for w in _waved]
            _new = [r for r in _new if r is not None]
            if len(_new) == len(top):
                top = _new
        except Exception:
            pass
        top = _smooth_keys_order(top)
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
    return {'tracks': top, 'seeds': seeds,
            'applied': applied,
            'current_mood': start_mood,
            'morph': morph,
            'count_requested': count,
            'count_effective': eff_count,
            'adaptive': adapt_reason,
            'drift': {k: drift.get(k) for k in
                      ("severity", "consecutive_skips", "temp_banned_genres",
                       "warmth", "positive_streak")}
            if (drift.get("severity") or drift.get("warmth")) else None,
            'profile_version': datetime.utcnow().isoformat() + 'Z'}
