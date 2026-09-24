from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.core.auth import check_body_user, require_admin, require_brain_auth
from app.db.models import Track
from app.services.yandex_music.client import get_yandex_config, save_yandex_config, fetch_metadata_sync
from app.services.queue import enqueue

router = APIRouter(dependencies=[Depends(require_brain_auth)])


def _require_user(db: Session, user_id: str):
    from app.db.models import MediaUser

    try:
        uuid.UUID(str(user_id))
    except ValueError:
        u = db.query(MediaUser).filter(MediaUser.external_id == str(user_id)).first()
        if not u:
            raise HTTPException(400, "invalid user_id")
        return u
    u = db.get(MediaUser, str(user_id))
    if not u:
        raise HTTPException(404, "user not found")
    return u


@router.get("/status")
def status(db: Session = Depends(get_db)):
    cfg = get_yandex_config(db)
    return {"enabled": cfg["enabled"] and bool(cfg["token"]), "has_token": bool(cfg["token"]), "throttle_sec": 1.2}


@router.get("/config")
def get_config(db: Session = Depends(get_db)):
    cfg = get_yandex_config(db)
    return {"token": "***" if cfg["token"] else "", "enabled": cfg["enabled"], "has_token": bool(cfg["token"])}


@router.post("/config", dependencies=[Depends(require_admin)])
def save_config(payload: dict, db: Session = Depends(get_db)):
    val = save_yandex_config(db, payload or {})
    return {"ok": True, "saved": {"enabled": val["enabled"], "has_token": bool(val["token"])}}


@router.post("/test", dependencies=[Depends(require_admin)])
def test(payload: dict | None = None, db: Session = Depends(get_db)):
    artist = (payload or {}).get("artist") or "Земфира"
    title = (payload or {}).get("title") or "Искала"
    res = fetch_metadata_sync(db, artist, title)
    if res is None:
        cfg = get_yandex_config(db)
        if not cfg["enabled"] or not cfg["token"]:
            return {"ok": False, "error": "yandex disabled or token empty (set YANDEX_MUSIC_TOKEN + enabled)"}
        return {"ok": False, "error": "not found or yandex throttled (429) — retry in 2s"}
    return {"ok": True, "result": res}


@router.post("/enrich", dependencies=[Depends(require_admin)])
def enrich(limit: int = 20, db: Session = Depends(get_db)):
    """Обогатить до limit треков без yandex-метаданных (фоновая задача)."""
    from app.db.models import TrackMetadataEnrich

    cfg = get_yandex_config(db)
    if not cfg["enabled"] or not cfg["token"]:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="yandex disabled or token empty")
    # выбрать треки без yandex enrich
    have = db.query(TrackMetadataEnrich.track_id).filter(TrackMetadataEnrich.source == "yandex").subquery()
    q = db.query(Track).filter(~Track.id.in_(db.query(have.c.track_id))).limit(limit).all()
    if not q:
        return {"queued": False, "reason": "all enriched"}
    from app.workers.tasks import yandex_enrich

    # создаём ScanRun
    import uuid
    from datetime import datetime

    from app.db.models import ScanLog, ScanRun
    from app.services.media_server import resolve_active_server

    server = resolve_active_server(db)
    db.commit()
    run = ScanRun(id=str(uuid.uuid4()), server_id=server.id, phase="yandex", status="running", total_items=len(q), processed_items=0, started_at=datetime.utcnow())
    db.add(run)
    db.flush()
    job_id = enqueue(yandex_enrich, str(run.id), job_timeout=3600)
    db.add(ScanLog(id=str(uuid.uuid4()), run_id=run.id, level="info", message=f"Yandex enrich queued {job_id} for {len(q)} tracks"))
    db.commit()
    return {"queued": True, "run_id": str(run.id), "job_id": job_id, "tracks": len(q)}


@router.get("/track/{track_id}")
def track_meta(track_id: str, db: Session = Depends(get_db)):
    from app.db.models import TrackMetadataEnrich

    rows = db.query(TrackMetadataEnrich).filter(TrackMetadataEnrich.track_id == track_id, TrackMetadataEnrich.source == "yandex").all()
    return {"items": [{"source": r.source, "data": r.data, "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None} for r in rows]}


# --- Персональный токен Яндекс Музыки (шифр в БД, как vault) ---

@router.post("/user-token")
def user_token_save(payload: dict, request: Request, db: Session = Depends(get_db)):
    from app.services.yandex_music import library as _lib

    _uid = str((payload or {}).get("user_id") or "")
    check_body_user(getattr(request.state, "brain_token", None), _uid)
    u = _require_user(db, _uid)
    try:
        return _lib.save_user_token(db, str(u.id), str((payload or {}).get("token") or ""))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


@router.get("/user-token-status")
def user_token_status(user_id: str, request: Request, db: Session = Depends(get_db)):
    from app.services.yandex_music import library as _lib

    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    return {"ok": True, "stored": _lib.has_user_token(db, str(u.id))}


@router.delete("/user-token")
def user_token_forget(user_id: str, request: Request, db: Session = Depends(get_db)):
    from app.services.yandex_music import library as _lib

    check_body_user(getattr(request.state, "brain_token", None), user_id)
    u = _require_user(db, user_id)
    return _lib.forget_user_token(db, str(u.id))


# --- Тумблеры импорта ---

@router.get("/import-settings")
def import_settings_get(db: Session = Depends(get_db)):
    from app.services.yandex_music import library as _lib

    return {"ok": True, "settings": _lib.get_import_settings(db)}


@router.put("/import-settings", dependencies=[Depends(require_admin)])
def import_settings_put(payload: dict, db: Session = Depends(get_db)):
    from app.services.yandex_music import library as _lib

    return {"ok": True, "settings": _lib.save_import_settings(db, payload or {})}


# --- Импорт библиотеки пользователя в taste engine ---

@router.post("/import-taste")
def import_taste(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Лайки/дизлайки из Яндекс Музыки -> вкус мозга. Тумблера нет, всегда доступен."""
    from app.services.yandex_music import library as _lib

    _uid = str((payload or {}).get("user_id") or "")
    check_body_user(getattr(request.state, "brain_token", None), _uid)
    u = _require_user(db, _uid)
    try:
        return _lib.import_taste(db, str(u.id))
    except Exception as e:  # noqa: BLE001
        from app.core.logging import get_logger as _gl

        _gl("api.yandex").exception("import-taste failed")
        return {"ok": False, "error": str(e)[:300]}


@router.post("/import-history")
def import_history(payload: dict, request: Request, db: Session = Depends(get_db)):
    """История прослушиваний -> PlayHistory/events. Только при history_enabled."""
    from app.services.yandex_music import library as _lib

    _uid = str((payload or {}).get("user_id") or "")
    check_body_user(getattr(request.state, "brain_token", None), _uid)
    u = _require_user(db, _uid)
    try:
        limit = int((payload or {}).get("limit") or 300)
    except (TypeError, ValueError):
        limit = 300
    try:
        return _lib.import_history(db, str(u.id), limit=max(1, min(1000, limit)))
    except Exception as e:  # noqa: BLE001
        from app.core.logging import get_logger as _gl

        _gl("api.yandex").exception("import-history failed")
        return {"ok": False, "error": str(e)[:300]}


# --- Чарты/новинки для cold start ---

@router.post("/charts/refresh", dependencies=[Depends(require_admin)])
def charts_refresh(db: Session = Depends(get_db)):
    from app.services.yandex_music import library as _lib

    try:
        return _lib.refresh_charts(db)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


@router.get("/charts")
def charts_get(db: Session = Depends(get_db)):
    from app.services.yandex_music import library as _lib

    return {"ok": True, **_lib.get_charts(db)}


# --- Контроль коррекций метаданных ---

@router.get("/corrections")
def corrections_list(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    """Что Яндекс поправил в локальных метаданных: трек, поле, было -> стало."""
    from app.services.yandex_music import library as _lib

    return {"ok": True, **_lib.list_corrections(db, limit, offset)}


@router.post("/corrections/revert", dependencies=[Depends(require_admin)])
def corrections_revert(payload: dict, db: Session = Depends(get_db)):
    """Откатить правки Яндекса по треку (все поля или fields=[...])."""
    from app.services.yandex_music import library as _lib

    track_id = str((payload or {}).get("track_id") or "")
    if not track_id:
        raise HTTPException(400, "track_id required")
    fields = (payload or {}).get("fields")
    try:
        return _lib.revert_correction(db, track_id, list(fields) if fields else None)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
