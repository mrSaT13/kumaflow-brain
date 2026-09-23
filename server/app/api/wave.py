from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.core.auth import check_body_user, require_scope

router = APIRouter(dependencies=[Depends(require_scope("wave"))])

# Живая очередь телефона: мобила публикует свою очередь, веб её показывает.
# In-memory (один web-процесс): для дома достаточно, честно — не переживает
# рестарт и не делится между репликами. TTL 10 минут.
_LIVE: dict[str, dict] = {}
_LIVE_TTL_SEC = 600
_LIVE_MAX_USERS = 200


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
def wave_seeds(user_id: str, characteristic: str | None = None,
               limit: int = 5, db: Session = Depends(get_db)):
    """Сиды волны для клиента (топ+recent+random как в мобиле)."""
    from app.services import wave as _wave

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
    if len(_LIVE) >= _LIVE_MAX_USERS and str(u.id) not in _LIVE:
        # вытесняем самую старую
        oldest = min(_LIVE, key=lambda k: _LIVE[k].get('ts', 0))
        _LIVE.pop(oldest, None)
    _LIVE[str(u.id)] = {'queue': queue,
                        'current_track_id': str(cur) if cur else None,
                        'ts': time.time()}
    return {'ok': True, 'user_id': str(u.id), 'queued': len(queue)}


@router.get('/live')
def wave_live(user_id: str, db: Session = Depends(get_db)):
    """Живая очередь телефона + обогащение для веба. age_sec — свежесть."""
    from app.services.track_resolve import get_track as _gt

    u = _require_user(db, user_id)
    entry = _LIVE.get(str(u.id))
    if not entry:
        return {'ok': True, 'user_id': str(u.id), 'queue': [], 'current': 0,
                'age_sec': None}
    now = time.time()
    age = now - float(entry.get('ts', 0) or 0)
    if age > _LIVE_TTL_SEC:
        _LIVE.pop(str(u.id), None)
        return {'ok': True, 'user_id': str(u.id), 'queue': [],
                'current': 0, 'age_sec': int(age), 'stale': True}
    tracks: list[dict] = []
    cur_idx = 0
    cur_raw = str(entry.get('current_track_id') or '')
    for raw in entry.get('queue') or []:
        t = _gt(db, str(raw))
        if t is None:
            continue
        tid = str(t.id)
        if cur_raw and (cur_raw == tid or cur_raw == str(raw)):
            cur_idx = len(tracks)
        tracks.append({'track_id': tid, 'title': t.title,
                       'artist_name': t.artist_name, 'album_name': t.album_name,
                       'genre': t.genre, 'cover_art_id': t.cover_art_id,
                       'reason': 'очередь телефона'})
        if len(tracks) >= 50:
            break
    return {'ok': True, 'user_id': str(u.id), 'queue': tracks,
            'current': cur_idx, 'age_sec': int(age)}
