"""Library Sync — приёмник событий и управление подписками.

POST /events — webhook от внешнего сервиса-поставщика треков.
Тяжёлая обработка (скан, апсерт, уведомления) уходит в воркер light,
здесь только валидация и постановка в очередь.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_admin, require_brain_auth
from app.db import get_db
from app.db.models import ArtistSubscription
from app.services import library_sync as ls
from app.services.queue import enqueue_light
from app.workers.tasks import library_sync_event

router = APIRouter(dependencies=[Depends(require_brain_auth)])


@router.post("/events")
def receive_event(payload: dict, db: Session = Depends(get_db)):
    """Webhook поставщика. Отвечаем сразу, работу делает воркер."""
    event = str((payload or {}).get("event") or "")
    try:
        from app.services.library_sync import get_config as _gc

        cfg = _gc(db)
        if not cfg["enabled"]:
            return {"ok": False, "error": "Library Sync выключен"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    if event == "test":
        # пробный пинг из настроек поставщика — отвечаем сразу, в очередь не кладём
        return {"ok": True, "echo": "test"}
    if event != "download.completed":
        return {"ok": False, "error": f"unknown event: {event or '(empty)'}"}
    try:
        job_id = enqueue_light(library_sync_event, dict(payload or {}), job_timeout=1200)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"очередь недоступна: {e}"}
    return {"ok": True, "queued": True, "job_id": job_id}


@router.get("/config")
def get_cfg(db: Session = Depends(get_db)):
    return ls.masked_config(db)


@router.post("/config", dependencies=[Depends(require_admin)])
def save_cfg(payload: dict, db: Session = Depends(get_db)):
    value = ls.save_config(db, payload or {})
    return {"ok": True, "saved": {**value, "token": "***" if value["token"] else ""}}


@router.post("/config/test", dependencies=[Depends(require_admin)])
async def test_cfg(payload: dict | None = None, db: Session = Depends(get_db)):
    """Проверить поставщика. Без body — сохранённые настройки."""
    saved = ls.get_config(db)
    body = payload or {}
    url = (body.get("url") or saved.get("url") or "").strip()
    tok = (body.get("token") or "").strip() or (saved.get("token") or "")
    if not url:
        return {"ok": False, "error": "Укажите URL сервиса"}
    try:
        res = await ls.test_connection(url, tok)
        return {"ok": True, **res}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


def _resolve_user(db: Session, user_id: str | None, username: str | None):
    from app.services.track_resolve import get_user

    raw = (user_id or username or "").strip()
    if not raw:
        return None
    u = get_user(db, raw)
    if u is None:
        raise HTTPException(404, "user not found (нужен user_id или логин Navidrome)")
    return u


@router.get("/subscriptions")
def list_subs(user_id: str | None = None, db: Session = Depends(get_db)):
    q = db.query(ArtistSubscription)
    if user_id:
        u = _resolve_user(db, user_id, None)
        q = q.filter(ArtistSubscription.user_id == str(u.id))
    rows = q.order_by(ArtistSubscription.created_at.desc()).all()
    return {"subscriptions": [
        {"id": str(r.id), "user_id": r.user_id, "artist_name": r.artist_name,
         "artist_external_id": r.artist_external_id,
         "provider_artist_id": r.provider_artist_id,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]}


@router.post("/subscriptions")
def add_sub(payload: dict, db: Session = Depends(get_db)):
    """Подписка игрока на артиста: сохраняем локально + ретранслируем наружу."""
    artist_name = str((payload or {}).get("artist_name") or "").strip()
    if not artist_name:
        return {"ok": False, "error": "artist_name required"}
    artist_external_id = str((payload or {}).get("artist_external_id") or "").strip() or None
    u = _resolve_user(db, (payload or {}).get("user_id"), (payload or {}).get("username"))
    uid = str(u.id) if u else None

    existing = ls.find_existing_subscription(db, uid, artist_name)
    sub = existing
    if sub is None:
        sub = ArtistSubscription(user_id=uid, artist_name=artist_name[:512],
                                 artist_external_id=artist_external_id)
        db.add(sub)
        db.commit()
        db.refresh(sub)
    else:
        if artist_external_id and not sub.artist_external_id:
            sub.artist_external_id = artist_external_id
            db.commit()

    provider: dict = {"ok": False, "skipped": True}
    try:
        cfg = ls.get_config(db)
        if cfg["enabled"] and cfg["url"]:
            res = ls.push_subscription(cfg, artist_name)
            # Имя поставщика — каноническое: фиксируем его локально вместе с id.
            # Матчинг ответа — по каноническому ключу, а не точным ==.
            try:
                subs = (res or {}).get("subs") or []
                want = ls.canonical_key(artist_name)
                hit = next((s for s in subs
                            if ls.canonical_key(str(s.get("artistName") or "")) == want
                            and str(s.get("artistName") or "")), None)
                if hit:
                    if hit.get("artistId"):
                        sub.provider_artist_id = str(hit["artistId"])[:128]
                    cname = str(hit.get("artistName") or "").strip()
                    if cname and cname != sub.artist_name:
                        sub.artist_name = cname[:512]
                    db.commit()
                    provider = {"ok": True, "skipped": False, "response": res,
                                "ambiguous": False}
                else:
                    provider = {"ok": False, "skipped": False,
                                "error": "поставщик не вернул подписку (возможно, неоднозначное имя)"}
            except Exception:
                provider = {"ok": True, "skipped": False, "response": res}
        else:
            provider = {"ok": False, "skipped": True,
                        "error": "Library Sync выключен или не настроен"}
    except Exception as e:  # noqa: BLE001
        provider = {"ok": False, "skipped": False, "error": str(e)[:300]}
    return {"ok": True,
            "subscription": {"id": str(sub.id), "user_id": sub.user_id,
                             "artist_name": sub.artist_name,
                             "provider_artist_id": sub.provider_artist_id},
            "provider": provider}


@router.delete("/subscriptions")
def remove_sub(payload: dict, db: Session = Depends(get_db)):
    """Отписка: убираем локально + сообщаем наружу."""
    artist_name = str((payload or {}).get("artist_name") or "").strip()
    if not artist_name:
        return {"ok": False, "error": "artist_name required"}
    u = _resolve_user(db, (payload or {}).get("user_id"), (payload or {}).get("username"))
    uid = str(u.id) if u else None
    sub = ls.find_existing_subscription(db, uid, artist_name)
    provider: dict = {"ok": False, "skipped": True}
    if sub is not None and sub.provider_artist_id:
        try:
            cfg = ls.get_config(db)
            if cfg["enabled"] and cfg["url"]:
                provider = {**ls.remove_subscription(cfg, sub.provider_artist_id),
                            "skipped": False}
        except Exception as e:  # noqa: BLE001
            provider = {"ok": False, "skipped": False, "error": str(e)[:300]}
    if sub is not None:
        db.delete(sub)
        db.commit()
    return {"ok": True, "removed": sub is not None, "provider": provider}
