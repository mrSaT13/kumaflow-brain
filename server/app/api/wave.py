from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.core.auth import check_body_user, require_scope

router = APIRouter(dependencies=[Depends(require_scope("wave"))])

# Живая очередь телефона: мобила публикует свою очередь, веб её показывает.
# Хранилище — Redis (общий для всех uvicorn-workers; in-memory _LIVE умирал
# при --workers >1: publish попадал в один процесс, а /live читал другой).
# Нет Redis — откатываемся на память процесса (dev). TTL 10 минут.
_LIVE: dict[str, dict] = {}
_LIVE_TTL_SEC = 600
_LIVE_MAX_USERS = 200


def _live_key(user_id: str) -> str:
    return f"wave:live:{user_id}"


def _live_get(user_id: str) -> dict | None:
    try:
        from app.services.queue import get_redis as _gr

        raw = _gr().get(_live_key(user_id))
        if raw:
            import json as _json

            d = _json.loads(raw)
            if isinstance(d, dict):
                return d
            return None
    except Exception:
        pass
    return _LIVE.get(user_id)


def _live_age(user_id: str, entry: dict) -> float:
    try:
        from app.services.queue import get_redis as _gr

        ttl = _gr().ttl(_live_key(user_id))
        if ttl is not None and int(ttl) >= 0:
            return max(0.0, float(_LIVE_TTL_SEC - int(ttl)))
    except Exception:
        pass
    import time as _t

    return _t.time() - float(entry.get('ts', 0) or 0)


def _live_put(user_id: str, entry: dict) -> None:
    import time as _t

    entry = dict(entry)
    entry['ts'] = _t.time()
    _LIVE[user_id] = entry
    if len(_LIVE) > _LIVE_MAX_USERS:
        oldest = min(_LIVE, key=lambda k: _LIVE[k].get('ts', 0))
        _LIVE.pop(oldest, None)
    try:
        from app.services.queue import get_redis as _gr

        import json as _json

        _gr().setex(_live_key(user_id), _LIVE_TTL_SEC, _json.dumps(entry))
    except Exception:
        pass


def _live_pop(user_id: str) -> None:
    _LIVE.pop(user_id, None)
    try:
        from app.services.queue import get_redis as _gr

        _gr().delete(_live_key(user_id))
    except Exception:
        pass


def _require_user(db: Session, user_id: str):
    from app.db.models import MediaUser

    try:
        uuid.UUID(str(user_id))
    except ValueError:
        # разрешаем external_id (мобильный id)
        u = db.query(MediaUser).filter(MediaUser.external_id == str(user_id)).first()
        if not u:
            raise HTTPException(400, 'invalid user_id')
        return u
    u = db.get(MediaUser, str(user_id))
    if not u:
        raise HTTPException(404, 'user not found')
    return u


@router.post('/continue')
def wave_continue(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Аддитивная волна: клиент шлёт что уже есть + дельту, сервер докладывает.

    Body: {user_id, queue[], current_track_id?, count=20,
      settings{activity, characteristic, mood, language},
      exclude_ids[], recent_events[], ratings_delta[], profile_version?}
    """
    from app.services import wave as _wave

    user_id = str((payload or {}).get('user_id') or '')
    if not user_id:
        raise HTTPException(400, 'user_id required')
    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    try:
        return {'ok': True, 'user_id': str(u.id),
                **_wave.wave_continue(
                    db, str(u.id),
                    queue=list((payload or {}).get('queue') or []),
                    current_track_id=(payload or {}).get('current_track_id'),
                    count=int((payload or {}).get('count') or 20),
                    settings=dict((payload or {}).get('settings') or {}),
                    exclude_ids=list((payload or {}).get('exclude_ids') or []),
                    recent_events=list((payload or {}).get('recent_events') or []),
                    ratings_delta=list((payload or {}).get('ratings_delta') or []),
                )}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        from app.core.logging import get_logger as _gl
        _gl('api.wave').exception('wave continue failed')
        raise HTTPException(500, f'wave failed: {str(e)[:300]}')


@router.get('/seeds')
def wave_seeds(user_id: str, request: Request, characteristic: str | None = None,
               limit: int = 5, db: Session = Depends(get_db)):
    """Сиды волны для клиента (топ+recent+random как в мобиле)."""
    from app.services import wave as _wave

    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    seeds = _wave.select_seeds(db, str(u.id), characteristic,
                               limit=max(1, min(10, int(limit or 5))))
    return {'ok': True, 'user_id': str(u.id), 'seeds': seeds}


@router.post('/publish')
def wave_publish(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Мобила публикует свою живую очередь — веб показывает её на /wave.

    Body: {user_id, queue[] (external_id или uuid), current_track_id?}.
    """
    user_id = str((payload or {}).get('user_id') or '')
    if not user_id:
        raise HTTPException(400, 'user_id required')
    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    queue = [str(x) for x in (list((payload or {}).get('queue') or [])[:100]) if str(x)]
    cur = (payload or {}).get('current_track_id')
    _live_put(str(u.id), {'queue': queue,
                          'current_track_id': str(cur) if cur else None})
    return {'ok': True, 'user_id': str(u.id), 'queued': len(queue)}


@router.get('/live')
def wave_live(user_id: str, request: Request, db: Session = Depends(get_db)):
    """Живая очередь телефона + обогащение для веба. age_sec — свежесть."""
    from app.services.track_resolve import get_track as _gt

    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    entry = _live_get(str(u.id))
    if not entry:
        return {'ok': True, 'user_id': str(u.id), 'queue': [], 'current': 0,
                'age_sec': None}
    age = _live_age(str(u.id), entry)
    if age > _LIVE_TTL_SEC:
        _live_pop(str(u.id))
        return {'ok': True, 'user_id': str(u.id), 'queue': [],
                'current': 0, 'age_sec': int(age), 'stale': True}
    tracks: list[dict] = []
    cur_idx = 0
    cur_raw = str(entry.get('current_track_id') or '')
    found: list = []
    # Дедуп зеркала: телефон может прислать одну песню дважды
    # (разные row id / external_id после перескана). Режем и по track_id,
    # и по нормализованному «артист — название» как в wave_continue.
    seen_ids: set[str] = set()
    seen_keys: set[tuple] = set()
    try:
        from app.services.dedup import norm_text as _norm_text
    except Exception:
        _norm_text = None  # type: ignore
    for raw in entry.get('queue') or []:
        t = _gt(db, str(raw))
        if t is None:
            continue
        tid = str(t.id)
        if tid in seen_ids:
            continue
        if _norm_text is not None:
            try:
                k = (_norm_text(t.artist_name), _norm_text(t.title))
            except Exception:
                k = None
            if k and (not k[0] or not k[1]):
                k = None
            if k and k in seen_keys:
                continue
            if k:
                seen_keys.add(k)
        seen_ids.add(tid)
        found.append((str(raw), t))
        if len(found) >= 50:
            break
    # Настроение/энергия/темп из sonic+AI анализа — иначе на вебе «без настроения».
    feats: dict[str, Any] = {}
    try:
        from app.db.models import TrackFeatures as _TF

        feats = {str(f.track_id): f for f in
                 db.query(_TF).filter(
                     _TF.track_id.in_([str(t.id) for _, t in found])).all()}
    except Exception:
        feats = {}
    # Оценки пользователя — чтобы веб показывал ♥/👎 и было видно,
    # что лайк с телефона долетел (иначе зеркало молчит об оценках).
    liked: set[str] = set()
    disliked: set[str] = set()
    try:
        from app.db.models import Favorite as _Fav
        from app.db.models import TrackDislike as _Dis

        _ids = [str(t.id) for _, t in found]
        if _ids:
            liked = {str(r.track_id) for r in
                     db.query(_Fav).filter(_Fav.user_id == str(u.id),
                                           _Fav.track_id.in_(_ids)).all()}
            disliked = {str(r.track_id) for r in
                        db.query(_Dis).filter(_Dis.user_id == str(u.id),
                                              _Dis.track_id.in_(_ids)).all()}
    except Exception:
        pass
    for raw, t in found:
        tid = str(t.id)
        if cur_raw and (cur_raw == tid or cur_raw == str(raw)):
            cur_idx = len(tracks)
        try:
            moods = list((feats[tid].mood_labels or [])) if tid in feats else []
        except Exception:
            moods = []
        try:
            energy = float(feats[tid].energy) \
                if tid in feats and feats[tid].energy is not None else None
        except (TypeError, ValueError):
            energy = None
        try:
            tempo = float(feats[tid].tempo_bpm) \
                if tid in feats and feats[tid].tempo_bpm else None
        except (TypeError, ValueError):
            tempo = None
        tracks.append({'track_id': tid, 'title': t.title,
                       'artist_name': t.artist_name, 'album_name': t.album_name,
                       'genre': t.genre, 'cover_art_id': t.cover_art_id,
                       'mood': str(moods[0]).lower() if moods else None,
                       'moods': [str(m).lower() for m in moods[:3]],
                       'energy': energy, 'tempo': tempo,
                       'like': True if tid in liked else (False if tid in disliked else None),
                       'reason': 'очередь телефона'})
    return {'ok': True, 'user_id': str(u.id), 'queue': tracks,
            'current': cur_idx, 'age_sec': int(age)}
