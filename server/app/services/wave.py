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


def _resolve_ids(db, ids: list[str]) -> list[str]:
    """uuid как есть, external_id (мобильный id) -> наш track_id."""
    from app.db.models import Track

    out: list[str] = []
    for raw in ids or []:
        s = str(raw or '').strip()
        if not s:
            continue
        if db.get(Track, s) is not None:
            out.append(s)
            continue
        t = db.query(Track).filter(Track.external_id == s).first()
        if t is not None:
            out.append(str(t.id))
    return out


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
                from app.db.models import Track
                t = db.get(Track, tid)
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
            from app.db.models import Track
            if db.get(Track, tid) is None:
                t = db.query(Track).filter(Track.external_id == tid).first()
                if t is None:
                    continue
                tid = str(t.id)
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
                     current_hour: int | None = None) -> list[dict]:
    """Порт TrackScorer.scoreAndRankTracks на наших таблицах."""
    from app.db.models import Track, TrackFeatures
    from app.services import taste as _taste
    from app.services.ml import _cosine, _feature_vector

    settings = settings or {}
    activity = settings.get('activity')
    mood = (settings.get('mood') or '').strip().lower() or None
    hour = current_hour if current_hour is not None else datetime.now().hour

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
            if activity == 'work' and 0.3 <= en <= 0.6:
                ctx += 0.1
            if activity == 'workout' and bpm > 110:
                ctx += 0.15
            if activity == 'sleep' and en < 0.3:
                ctx += 0.1
            if mood and _mood_of(f) == mood:
                ctx += 0.1
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
                 pen + random.random() * 0.12)
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
        out.append({'track_id': tid, 'title': t.title,
                    'artist_name': t.artist_name, 'album_name': t.album_name,
                    'genre': t.genre, 'score': round(total, 4),
                    'audio': round(audio, 3), 'reason': reason})
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

    played = set(_resolve_ids(db, list(queue or []) + list(exclude_ids or [])))
    if current_track_id:
        played.update(_resolve_ids(db, [current_track_id]))

    seeds = select_seeds(db, user_id,
                         characteristic=settings.get('characteristic'), limit=5)
    if current_track_id:
        cur = _resolve_ids(db, [current_track_id])
        seeds = (cur + seeds)[:5]

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
    # Баны режем по мета (нужен artist) — батчем
    if bans and cand:
        meta = {str(t.id): t for t in
                db.query(Track).filter(Track.id.in_(cand[:2000])).all()}
        cand = [c for c in cand
                if not (meta.get(c) and meta[c].artist_name in bans)]
    # mood-фильтр как в мобиле (по первому муд-лейблу)
    mood = (settings.get('mood') or '').strip().lower() or None
    if mood and cand:
        from app.db.models import TrackFeatures
        fm = {str(f.track_id): f for f in
              db.query(TrackFeatures).filter(
                  TrackFeatures.track_id.in_(cand[:2000])).all()}
        cand = [c for c in cand
                if (_mood_of(fm[c]) == mood if c in fm else True)]

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
                              norm_events, collab_scores)
    # Недавнее реже (новизна как у cold-start novelty=True): уже учтено
    # novelty-членом, дубли очереди на всякий случай режем ещё раз
    ranked = [r for r in ranked if r['track_id'] not in played]
    top = ranked[:count]
    # Обогащаем ответ настроением/энергией для страницы «Моя волна»
    # (текущее настроение → в какое переходит). Без отдельных запросов за треками.
    try:
        from app.db.models import TrackFeatures as _TF

        _ids = [str(r.get("track_id") or "") for r in top if r.get("track_id")]
        _fm = {str(f.track_id): f for f in
               db.query(_TF).filter(_TF.track_id.in_(_ids)).all()} if _ids else {}
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
    except Exception:
        pass
    return {'tracks': top, 'seeds': seeds,
            'applied': applied,
            'profile_version': datetime.utcnow().isoformat() + 'Z'}
