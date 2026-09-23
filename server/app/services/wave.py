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
                     drift: dict | None = None) -> list[dict]:
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

    feats = {str(f.track_id): f for f in
             db.query(TrackFeatures).filter(
                 TrackFeatures.track_id.in_(candidate_ids + seed_ids)).all()} \
        if (candidate_ids or seed_ids) else {}
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
        elif a == 'abandon':
            bonus[tid] -= 0.15
        elif a == 'skip' and pos < 30:
            bonus[tid] -= 0.3

    used_artists: dict[str, int] = {}
    used_genres: dict[str, int] = {}
    out: list[dict] = []
    tracks = {str(t.id): t for t in
              db.query(Track).filter(Track.id.in_(candidate_ids)).all()} \
        if candidate_ids else {}
    for tid in candidate_ids:
        t = tracks.get(tid)
        if not t:
            continue
        f = feats.get(tid)
        # audio: косинус к центроиду сидов
        audio = 0.0
        if centroid is not None and f is not None:
            v = _feature_vector(None, f)
            if v is not None:
                try:
                    audio = float(_cosine(v, centroid))
                except Exception:
                    audio = 0.0
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
        collab = min(max(float((collab_scores or {}).get(tid, 0.0)), 0.0), 1.0)

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

        total = (w['audio'] * audio + w['genre'] * genre_s +
                 w['artist'] * artist_s + w['behavior'] * behavior +
                 w['collab'] * collab + w['novelty'] * novelty +
                 0.08 * novelty + ctx + bonus.get(tid, 0.0) -
                 pen + _rng.random() * 0.12)
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
                             'novelty': round(novelty, 3)}})
        if t.artist_name:
            used_artists[t.artist_name] = used_artists.get(t.artist_name, 0) + 1
        if t.genre:
            used_genres[t.genre] = used_genres.get(t.genre, 0) + 1
    out.sort(key=lambda x: x['score'], reverse=True)
    return out


def wave_continue(db, user_id: str, queue: list[str] | None = None,
                  current_track_id: str | None = None, count: int = 20,
                  settings: dict | None = None,
                  exclude_ids: list[str] | None = None,
                  recent_events: list[dict] | None = None,
                  ratings_delta: list[dict] | None = None) -> dict:
    """Главная функция: дельта -> сиды -> кандидаты -> скоринг -> следующие N."""
    from app.db.models import ArtistBan, Favorite, Track, TrackDislike

    settings = settings or {}
    count = max(1, min(100, int(count or 20)))

    applied = apply_delta(db, user_id, ratings_delta, recent_events)

    # Сессионный дрейф (порт MoodDriftDetector): скипы подряд остужают волну,
    # скипнутое исключаем из кандидатов — только на этот запрос, в БД не пишем.
    drift = session_drift(db, recent_events)

    played = set(_resolve_ids(db, list(queue or []) + list(exclude_ids or [])))
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
    if cur_ids:
        try:
            _cf = db.query(_TF0).filter(_TF0.track_id == cur_ids[0]).first()
            start_mood = _mood_of(_cf)
        except Exception:
            start_mood = None
    morphing = bool(target_mood and start_mood and target_mood != start_mood)

    # Исключения: очередь + дизлайки + баны
    dis = {str(r.track_id) for r in
           db.query(TrackDislike).filter_by(user_id=user_id).all()}
    bans = {str(r.artist_name) for r in
            db.query(ArtistBan).filter_by(user_id=user_id).all()}
    fav = {str(r.track_id) for r in
           db.query(Favorite).filter_by(user_id=user_id).all()}

    # Пул кандидатов: всё кроме сыгранного/дизлайков/банов, капом 2000
    q = db.query(Track.id)
    if played or dis:
        skip = list((played | dis))
        # чанкуем IN чтобы не упереться в лимиты переменных
        cand: list[str] = []
        for i in range(0, max(1, len(skip)), 500):
            chunk = skip[i:i + 500]
            rows = db.query(Track.id).filter(~Track.id.in_(chunk)).limit(2000).all()
            cand = [str(r[0]) for r in rows]
            if cand:
                break
        if not skip:
            cand = [str(r[0]) for r in db.query(Track.id).limit(2000).all()]
    else:
        cand = [str(r[0]) for r in q.limit(2000).all()]
    cand = [c for c in cand if c not in played and c not in dis]
    # Баны режем по мета (нужен artist) — батчем.
    # Сравнение через artist_names: бан «GASHI» ловит и трек
    # «Dark Polo Gang/GASHI/Capo Plaza», регистр не важен.
    if bans and cand:
        from app.services.artist_names import is_banned as _is_banned

        meta = {str(t.id): t for t in
                db.query(Track).filter(Track.id.in_(cand[:2000])).all()}
        cand = [c for c in cand
                if not (meta.get(c) and _is_banned(meta[c].artist_name, bans))]
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

    ranked = score_candidates(db, user_id, cand[:800], seeds, settings,
                              norm_events, collab_scores,
                              jitter_seed=f"{user_id}:{len(played)}",
                              drift=drift)
    # Недавнее реже (новизна как у cold-start novelty=True): уже учтено
    # novelty-членом, дубли очереди на всякий случай режем ещё раз
    ranked = [r for r in ranked if r['track_id'] not in played]
    top = ranked[:count]
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
        head = g_start[:3]
        tail = g_target[:4]
        used = {str(r.get("track_id")) for r in head + tail}
        mid_n = max(0, len(top) - len(head) - len(tail))
        mid = [r for r in top if str(r.get("track_id")) not in used][:mid_n]
        top = head + mid + tail
        morph = {"from": start_mood, "to": target_mood}
    return {'tracks': top, 'seeds': seeds,
            'applied': applied,
            'current_mood': start_mood,
            'morph': morph,
            'drift': {k: drift.get(k) for k in
                      ("severity", "consecutive_skips", "temp_banned_genres")}
            if drift.get("severity") else None,
            'profile_version': datetime.utcnow().isoformat() + 'Z'}
