from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import MediaUser
from app.services.demo import ensure_demo_server as _ensure_demo  # noqa: F401 (реэкспорт для совместимости)
from app.services.media_server import resolve_active_server

router = APIRouter()


class UserIn(BaseModel):
    external_id: str
    username: str
    is_admin: bool = False


class UserPatch(BaseModel):
    username: str | None = None
    is_admin: bool | None = None


class TasteImportIn(BaseModel):
    username: str
    password: str


class UserTasteImportIn(BaseModel):
    password: str = ""


def _to_dict(u: MediaUser) -> dict:
    return {
        "id": str(u.id),
        "external_id": u.external_id,
        "username": u.username,
        "is_admin": u.is_admin,
        "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
    }


@router.get("", include_in_schema=False)
@router.get("/")
def list_users(db: Session = Depends(get_db)):
    rows = db.query(MediaUser).order_by(MediaUser.username.asc()).all()
    return {"users": [_to_dict(u) for u in rows]}


@router.post("", include_in_schema=False)
@router.post("/")
def create_user(payload: UserIn, db: Session = Depends(get_db)):
    server = resolve_active_server(db)
    db.commit()
    exists = (
        db.query(MediaUser)
        .filter_by(server_id=server.id, external_id=payload.external_id)
        .first()
    )
    if exists:
        raise HTTPException(409, "user already exists")
    u = MediaUser(
        id=str(uuid.uuid4()),
        server_id=server.id,
        external_id=payload.external_id,
        username=payload.username,
        is_admin=payload.is_admin,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"user": _to_dict(u)}


@router.patch("/{user_id}")
def update_user(user_id: str, payload: UserPatch, db: Session = Depends(get_db)):
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    u = db.get(MediaUser, user_id)
    if not u:
        raise HTTPException(404, "not found")
    if payload.username is not None:
        u.username = payload.username
    if payload.is_admin is not None:
        u.is_admin = payload.is_admin
    db.commit()
    db.refresh(u)
    return {"user": _to_dict(u)}


@router.delete("/{user_id}")
def delete_user(user_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    u = db.get(MediaUser, user_id)
    if not u:
        raise HTTPException(404, "not found")
    db.delete(u)
    db.commit()
    return {"ok": True}


@router.post("/sync")
def sync_users(db: Session = Depends(get_db)):
    """Синхронизировать пользователей из Navidrome (getUsers) — сразу, без очереди."""
    from app.workers.tasks import sync_navidrome_users

    server = resolve_active_server(db)
    db.commit()
    res = sync_navidrome_users(str(server.id))
    if res.get("status") != "success":
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, **res}


@router.post("/by-credentials")
def create_user_by_credentials(payload: TasteImportIn, db: Session = Depends(get_db)):
    """Создать пользователя по логину/паролю Navidrome + сразу импортировать вкусы.

    Пароль нигде не хранится — используется только для одного запроса
    getStarred2/playlists под учёткой пользователя (Subsonic отдаёт
    избранное только своему владельцу).
    """
    server = resolve_active_server(db)
    db.commit()
    from app.services.taste_import import import_user_tastes

    res = import_user_tastes(str(server.id), payload.username, payload.password)
    if res.get("status") != "success":
        return {"ok": False, "error": res.get("error")}
    u = db.get(MediaUser, res["user_id"])
    return {"ok": True, "user": _to_dict(u) if u else None, "import": res}


@router.post("/{user_id}/import-tastes")
def import_tastes(user_id: str, payload: UserTasteImportIn, db: Session = Depends(get_db)):
    """Импорт вкусов (лайки/плейлисты) для существующего пользователя.

    Нужен пароль ЭТОГО пользователя в Navidrome (в body: {"password": "..."}).
    Если пароль не передан — пробуем глобальный пароль медиа-сервера
    (сработает, только если совпадает).
    """
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    u = db.get(MediaUser, user_id)
    if not u:
        raise HTTPException(404, "not found")
    password = (payload.password or "").strip()
    if not password:
        from app.services.media_server import get_media_server_config

        try:
            password = (get_media_server_config(db).get("password") or "")
        except Exception:
            password = ""
    if not password:
        return {"ok": False, "error": "Нужен пароль пользователя Navidrome — передайте {\"password\": \"...\"}"}
    from app.services.taste_import import import_user_tastes

    res = import_user_tastes(str(u.server_id), u.external_id, password)
    if res.get("status") != "success":
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, **res}


@router.get("/{user_id}/tastes")
def user_tastes(user_id: str, db: Session = Depends(get_db)):
    """Счётчики вкусов пользователя: лайки, плейлисты — для бейджей в UI."""
    from app.db.models import Favorite, Playlist, PlaylistTrack

    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    u = db.get(MediaUser, user_id)
    if not u:
        raise HTTPException(404, "not found")
    fav = db.query(Favorite).filter_by(user_id=u.id).count()
    pls = db.query(Playlist).filter_by(owner_user_id=u.id).count()
    pl_tracks = (
        db.query(PlaylistTrack)
        .join(Playlist, Playlist.id == PlaylistTrack.playlist_id)
        .filter(Playlist.owner_user_id == u.id)
        .count()
    )
    return {"user_id": str(u.id), "favorites": fav, "playlists": pls, "playlist_tracks": pl_tracks}


class SeedTasteIn(BaseModel):
    genres: list[str] = []
    artists: list[str] = []
    track_ids: list[str] = []


@router.post("/{user_id}/seed-taste")
def seed_taste(user_id: str, payload: SeedTasteIn, db: Session = Depends(get_db)):
    """Seed вкусов из визарда холодного старта (как mobile MLService.seedTaste).

    Принимает выбранные жанры/артистов/треки и раскладывает их в наши таблицы:
    артисты и треки → Favorite (сильный сигнал, x10 в cold-start),
    жанры и артисты → несколько PlayHistory (весовой сигнал, как fan-out
    seedTaste в мобильном). После этого generate-daily/cold-start с user_id
    становится персональным.
    """
    from datetime import datetime

    from app.db.models import Favorite, PlayHistory, Track

    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    u = db.get(MediaUser, user_id)
    if not u:
        raise HTTPException(404, "not found")

    genres = [g for g in (payload.genres or []) if g][:50]
    artists = [a for a in (payload.artists or []) if a][:100]
    track_ids = [t for t in (payload.track_ids or []) if t][:200]

    fav_added = hist_added = 0

    def _fav(tid: str) -> None:
        nonlocal fav_added
        if db.query(Favorite).filter_by(user_id=u.id, track_id=tid).first() is None:
            db.add(Favorite(user_id=u.id, track_id=tid))
            fav_added += 1

    def _hist(tid: str, n: int = 1) -> None:
        nonlocal hist_added
        for _ in range(n):
            db.add(PlayHistory(user_id=u.id, track_id=tid, played_at=datetime.utcnow()))
            hist_added += 1

    # явные треки → лайки
    for tid in track_ids:
        if db.get(Track, tid) is not None:
            _fav(tid)

    # артисты: топ-3 по play_count → лайк лучшему + история остальным
    for aname in artists:
        tops = (
            db.query(Track)
            .filter(Track.artist_name == aname)
            .order_by(Track.play_count.desc().nullslast())
            .limit(3)
            .all()
        )
        for i, t in enumerate(tops):
            if i == 0:
                _fav(str(t.id))
            _hist(str(t.id), 2 if i == 0 else 1)

    # жанры: топ-5 по play_count → история (жанровый вес)
    for g in genres:
        tops = (
            db.query(Track)
            .filter(Track.genre == g)
            .order_by(Track.play_count.desc().nullslast())
            .limit(5)
            .all()
        )
        for t in tops[:3]:
            _hist(str(t.id), 1)

    db.commit()
    fav_total = db.query(Favorite).filter_by(user_id=u.id).count()
    return {
        "ok": True,
        "user_id": str(u.id),
        "favorites_added": fav_added,
        "history_added": hist_added,
        "favorites_total": fav_total,
    }
