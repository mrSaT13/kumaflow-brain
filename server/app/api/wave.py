from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.core.auth import check_body_user, require_scope

router = APIRouter(dependencies=[Depends(require_scope("wave"))])

# Живая очередь: КАЖДОЕ устройство публикует в СВОЙ слот, очередь живёт
# на клиенте (как у Яндекс Музыки), мозг только анализирует и помнит позицию.
# Раньше был один слот на юзера (last-writer-wins): телефон и десктоп
# перезаписывали друг друга, а веб тремя поллингами видел разные снапшоты —
# «Сейчас #3» спорил с «Продолжить #1». Теперь ключ — (user, device).
# Хранилище — Redis (общий для uvicorn-workers), без него — память процесса.
# TTL 10 минут на слот.
_LIVE: dict[str, dict] = {}
_LIVE_TTL_SEC = 600
_LIVE_MAX_SLOTS = 400


def _norm_device(v: object) -> str:
    """Стабильный id слота. Клиент обязан слать ОДИН И ТОТ ЖЕ device всегда
    (сгенерировал UUID один раз — хранит). Пусто/мусор → слот 'default'
    (совместимость со старыми клиентами)."""
    import re as _re

    s = _re.sub(r'[^a-z0-9_-]', '', str(v or '').strip().lower())
    return s[:32] or 'default'


def _live_key(user_id: str, device: str) -> str:
    return f"wave:live:{user_id}:{_norm_device(device)}"


def _live_get(user_id: str, device: str = 'default') -> dict | None:
    try:
        from app.services.queue import get_redis as _gr

        raw = _gr().get(_live_key(user_id, device))
        if raw:
            import json as _json

            d = _json.loads(raw)
            if isinstance(d, dict):
                return d
            return None
    except Exception:
        pass
    return _LIVE.get(f"{user_id}\x00{_norm_device(device)}")


def _live_age(user_id: str, entry: dict, device: str = 'default') -> float:
    try:
        from app.services.queue import get_redis as _gr

        ttl = _gr().ttl(_live_key(user_id, device))
        if ttl is not None and int(ttl) >= 0:
            return max(0.0, float(_LIVE_TTL_SEC - int(ttl)))
    except Exception:
        pass
    import time as _t

    return _t.time() - float(entry.get('ts', 0) or 0)


def _live_put(user_id: str, entry: dict, device: str = 'default') -> None:
    import time as _t

    slot = _norm_device(device)
    entry = dict(entry)
    entry['ts'] = _t.time()
    entry['device_id'] = slot
    _LIVE[f"{user_id}\x00{slot}"] = entry
    if len(_LIVE) > _LIVE_MAX_SLOTS:
        oldest = min(_LIVE, key=lambda k: _LIVE[k].get('ts', 0))
        _LIVE.pop(oldest, None)
    try:
        from app.services.queue import get_redis as _gr

        import json as _json

        _gr().setex(_live_key(user_id, slot), _LIVE_TTL_SEC, _json.dumps(entry))
    except Exception:
        pass


def _live_pop(user_id: str, device: str = 'default') -> None:
    _LIVE.pop(f"{user_id}\x00{_norm_device(device)}", None)
    try:
        from app.services.queue import get_redis as _gr

        _gr().delete(_live_key(user_id, device))
    except Exception:
        pass


def _live_slots(user_id: str) -> list[tuple[str, dict, float]]:
    """Все живые слоты юзера: [(device_slot, entry, age_sec)]. Протухшие
    (старше TTL) выкидываются. In-memory + Redis объединяются, побеждает
    более свежий ts."""
    import time as _t

    now = _t.time()
    found: dict[str, dict] = {}
    prefix = f"{user_id}\x00"
    for k, e in list(_LIVE.items()):
        if k.startswith(prefix) and isinstance(e, dict):
            found[k[len(prefix):]] = e
    try:
        from app.services.queue import get_redis as _gr

        import json as _json

        r = _gr()
        keys: list = []
        try:
            keys = list(r.scan_iter(f"wave:live:{user_id}:*", count=50))
        except Exception:
            try:
                keys = list(r.keys(f"wave:live:{user_id}:*") or [])
            except Exception:
                keys = []
        for k in keys:
            try:
                ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                slot = ks.rsplit(':', 1)[-1] or 'default'
                raw = r.get(ks)
                if not raw:
                    continue
                d = _json.loads(raw)
                if not isinstance(d, dict):
                    continue
                prev = found.get(slot)
                if prev is None or float(d.get('ts', 0) or 0) > float(prev.get('ts', 0) or 0):
                    found[slot] = d
            except Exception:
                continue
    except Exception:
        pass
    out: list[tuple[str, dict, float]] = []
    for slot, e in found.items():
        try:
            ttl_age: float | None = None
            try:
                from app.services.queue import get_redis as _gr2

                ttl = _gr2().ttl(_live_key(user_id, slot))
                if ttl is not None and int(ttl) >= 0:
                    ttl_age = max(0.0, float(_LIVE_TTL_SEC - int(ttl)))
            except Exception:
                ttl_age = None
            age = ttl_age if ttl_age is not None else now - float(e.get('ts', 0) or 0)
        except Exception:
            age = now
        if age > _LIVE_TTL_SEC:
            continue
        out.append((slot, e, age))
    out.sort(key=lambda t: t[2])
    return out


def _live_latest(user_id: str) -> tuple[str | None, dict | None, float | None]:
    """Самый свежий слот: (device_slot, entry, age_sec)."""
    slots = _live_slots(user_id)
    if not slots:
        return None, None, None
    s, e, a = slots[0]
    return s, e, a


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
    """Клиент публикует свою живую очередь в СВОЙ слот — веб показывает её на /wave.

    Body: {user_id, queue[] (external_id или uuid), current_track_id?,
           position_sec?, duration_sec?, device?, paused?}.

    device — СТАБИЛЬНЫЙ id плеера (сгенерировал UUID один раз — шлёшь всегда).
    Каждый device — отдельный слот: телефон и десктоп друг друга НЕ затирают.
    Без device — слот 'default' (старые клиенты).

    position_sec — это и есть handoff: клиент сообщает, на какой секунде стоит
    трек, и другое устройство может продолжить с того же места.
    """
    user_id = str((payload or {}).get('user_id') or '')
    if not user_id:
        raise HTTPException(400, 'user_id required')
    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    queue = [str(x) for x in (list((payload or {}).get('queue') or [])[:100]) if str(x)]
    cur = (payload or {}).get('current_track_id')
    slot = _norm_device((payload or {}).get('device'))
    def _int(v):
        try:
            return max(0, int(v)) if v is not None else None
        except (TypeError, ValueError):
            return None
    _live_put(str(u.id), {'queue': queue,
                          'current_track_id': str(cur) if cur else None,
                          'position_sec': _int((payload or {}).get('position_sec')),
                          'duration_sec': _int((payload or {}).get('duration_sec')),
                          'device': str((payload or {}).get('device') or '')[:64] or None,
                          'paused': bool((payload or {}).get('paused'))},
              device=slot)
    return {'ok': True, 'user_id': str(u.id), 'queued': len(queue), 'device_id': slot}


def _devices_summary(user_id: str) -> list[dict]:
    """Кратко по всем живым слотам: что показать в селекторе устройств."""
    out: list[dict] = []
    for slot, e, age in _live_slots(user_id):
        q = [str(x) for x in (e.get('queue') or []) if str(x)]
        out.append({'device_id': slot,
                    'device': e.get('device') or slot,
                    'age_sec': int(age),
                    'queue_len': len(q),
                    'paused': bool(e.get('paused'))})
    return out


@router.get('/resume')
def wave_resume(user_id: str, request: Request, device: str | None = None,
                db: Session = Depends(get_db)):
    """Откуда продолжить прослушивание на другом устройстве.

    ?device=<slot> — конкретный плеер; без него — самый свежий слот.
    Отдаёт: трек, позицию в секундах, очередь и «свежесть» (age_sec), плюс
    devices[] — все живые плееры для селектора. Если клиент давно не
    публиковал очередь, отдаём stale=True — продолжать вслепую не стоит.
    """
    from app.services.track_resolve import get_track as _gt

    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    devices = _devices_summary(str(u.id))
    slot: str | None = _norm_device(device) if device else None
    entry: dict | None = None
    age: float | None = None
    if slot:
        entry = _live_get(str(u.id), slot)
        if entry is not None:
            age = _live_age(str(u.id), entry, slot)
            if age > _LIVE_TTL_SEC:
                entry, age = None, None
    else:
        slot, entry, age = _live_latest(str(u.id))
    if not entry or age is None:
        return {'ok': True, 'user_id': str(u.id), 'available': False,
                'reason': 'нет опубликованной очереди (клиент не публиковал или TTL истёк)',
                'age_sec': None, 'stale': True, 'device_id': slot,
                'devices': devices}
    stale = age > _LIVE_TTL_SEC
    cur_raw = str(entry.get('current_track_id') or '')
    track = None
    if cur_raw:
        try:
            track = _gt(db, cur_raw)
        except Exception:
            track = None
    pos = entry.get('position_sec')
    dur = entry.get('duration_sec')
    try:
        pos = int(pos) if pos is not None else None
    except (TypeError, ValueError):
        pos = None
    try:
        dur = int(dur) if dur is not None else (track.duration_sec if track is not None else None)
    except (TypeError, ValueError):
        dur = None
    if track is not None and dur is None:
        dur = track.duration_sec
    if pos is not None and dur:
        pos = min(max(0, pos), max(0, int(dur) - 3))
    # Очередь после текущего трека — чтобы продолжить не одним треком, а потоком.
    rest: list[str] = []
    if cur_raw:
        try:
            idx = [str(x) for x in (entry.get('queue') or [])].index(cur_raw)
        except ValueError:
            idx = -1
        rest = [str(x) for x in (list(entry.get('queue') or [])[idx + 1:] or []) if str(x)]
    return {
        'ok': True,
        'user_id': str(u.id),
        'available': bool(track is not None),
        'stale': bool(stale),
        'age_sec': int(age),
        'device': entry.get('device'),
        'device_id': slot,
        'devices': devices,
        'paused': bool(entry.get('paused')),
        'track': ({
            'track_id': str(track.id),
            'title': track.title,
            'artist_name': track.artist_name,
            'album_name': track.album_name,
            'cover_art_id': track.cover_art_id,
            'external_id': track.external_id,
            'duration_sec': dur,
        } if track is not None else None),
        'position_sec': pos,
        'position_ratio': (round(pos / dur, 3) if (pos is not None and dur) else None),
        'queue': rest[:50],
    }


@router.get('/feedback')
def wave_feedback(user_id: str | None = None, request: Request = None, days: int = 7,
                  source: str | None = None, db: Session = Depends(get_db)):
    """Метрики волны: скипы <30 c, дослушивания, лайки + разбивка по причинам.

    Без этого улучшать скоринг нечем — правки приходилось проверять на глаз.
    """
    from app.services import rec_feedback as _rfb

    if user_id:
        check_body_user(getattr(getattr(request, "state", None), "brain_token", None), user_id)
        u = _require_user(db, user_id)
        target = str(u.id)
    else:
        # Без user_id — сводка по всем пользователям. Роутер защищён скоупом
        # wave, который выдаётся и обычному пользователю, поэтому:
        #   * info is None  → авторизация выключена (доверенная LAN), доступ
        #     и так открыт каждому — сужать тут нечего, отдаём сводку по всем.
        #     Раньше здесь стоял 403, из-за чего выбор «все пользователи» в UI
        #     упирался в ошибку на любой стенде без BRAIN_API_TOKEN.
        #   * info есть     → админ видит всех, остальные только себя.
        # Берём info из request.state (его уже выставил require_scope и он
        # понимает и Bearer-заголовок, и ?token=). Повторный вызов
        # _auth_state(request, None) здесь был дырой: creds=None игнорировал
        # заголовок, header-юзер получал info=None и видел чужие данные.
        info = getattr(getattr(request, "state", None), "brain_token", None) \
            if request is not None else None
        if info is None:
            target = None
        elif not info.get("is_admin"):
            owner = (info or {}).get("owner_user_id")
            if not owner:
                raise HTTPException(403, "token bound to another user")
            target = str(owner)
        else:
            target = None
    return {'ok': True, **_rfb.summary(db, user_id=target, days=days, source=source)}


@router.get('/live')
def wave_live(user_id: str, request: Request, device: str | None = None,
              db: Session = Depends(get_db)):
    """Живая очередь устройства + обогащение для веба. age_sec — свежесть.

    ?device=<slot> — конкретный плеер; без него — самый свежий слот.
    devices[] — все живые плееры (селектор устройств на вебе).
    """
    from app.services.track_resolve import get_track as _gt

    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    devices = _devices_summary(str(u.id))
    slot: str | None = _norm_device(device) if device else None
    entry: dict | None = None
    age: float | None = None
    if slot:
        entry = _live_get(str(u.id), slot)
        if entry is not None:
            age = _live_age(str(u.id), entry, slot)
            if age > _LIVE_TTL_SEC:
                entry, age = None, None
    else:
        slot, entry, age = _live_latest(str(u.id))

    def _empty(extra=None):
        base = {'ok': True, 'user_id': str(u.id), 'queue': [], 'current': 0,
                'age_sec': None, 'current_track_id': None,
                'position_sec': None, 'duration_sec': None,
                'device': None, 'device_id': slot, 'paused': False,
                'devices': devices}
        if extra:
            base.update(extra)
        return base
    if not entry or age is None:
        return _empty()
    if age > _LIVE_TTL_SEC:
        if slot:
            _live_pop(str(u.id), slot)
        return _empty({'age_sec': int(age), 'stale': True})
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
    cur_tid: str | None = None
    for raw, t in found:
        tid = str(t.id)
        if cur_raw and (cur_raw == tid or cur_raw == str(raw)):
            cur_idx = len(tracks)
            cur_tid = tid
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
    # Handoff-поля из того же слепка (чтобы веб не дёргал /resume
    # отдельным поллингом и не видел другой снапшот, чем /live):
    # current_track_id — резолвленный uuid текущего, position/device/paused —
    # как прислал клиент в /publish.
    def _pos(v):
        try:
            return max(0, int(v)) if v is not None else None
        except (TypeError, ValueError):
            return None
    return {'ok': True, 'user_id': str(u.id), 'queue': tracks,
            'current': cur_idx, 'age_sec': int(age),
            'current_track_id': cur_tid,
            'position_sec': _pos(entry.get('position_sec')),
            'duration_sec': _pos(entry.get('duration_sec')),
            'device': entry.get('device'),
            'device_id': slot,
            'devices': devices,
            'paused': bool(entry.get('paused'))}
