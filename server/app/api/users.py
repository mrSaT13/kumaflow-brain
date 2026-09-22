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
    remember: bool = False  # opt-in: зашифровать пароль в сейф для автообновления


class SeedTasteIn(BaseModel):
    genres: list[str] = []
    artists: list[str] = []
    track_ids: list[str] = []


class RateIn(BaseModel):
    track_id: str
    like: bool | None  # True лайк / False дизлайк / None снять оценку


class EventsIn(BaseModel):
    events: list[dict] = []


class BanIn(BaseModel):
    artist_name: str


class VaultIn(BaseModel):
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
    vault_stored: bool | str = False
    if payload.remember and u is not None:
        from app.db.models import UserCredential as _UC
        from app.services import vault as _vault

        try:
            blob = _vault.encrypt_password(payload.password)
            row = db.query(_UC).filter_by(user_id=u.id).first()
            if row is None:
                db.add(_UC(user_id=u.id, enc_password=blob))
            else:
                row.enc_password = blob
            db.commit()
            vault_stored = True
        except RuntimeError as e:
            vault_stored = str(e)
    return {"ok": True, "user": _to_dict(u) if u else None, "import": res,
            "vault_stored": vault_stored}


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

    try:
        res = import_user_tastes(str(u.server_id), u.external_id, password)
    except Exception as e:  # noqa: BLE001 — читаемая ошибка вместо голого 500
        from app.core.logging import get_logger as _gl

        _gl("api.users").exception("import-tastes failed")
        return {"ok": False, "error": f"Импорт не удался: {str(e)[:400]}"}
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

    # артисты: раз выбрал артиста — значит нравятся его треки:
    # топ-10 по play_count → в лайки, топ-3 → ещё и в историю (вес)
    for aname in artists:
        tops = (
            db.query(Track.id)
            .filter(Track.artist_name == aname)
            .order_by(Track.play_count.desc().nullslast())
            .limit(10)
            .all()
        )
        for i, (tid,) in enumerate(tops):
            _fav(str(tid))
            if i < 3:
                _hist(str(tid), 2 if i == 0 else 1)

    # жанры: топ-8 по play_count → лайки первым 3 + история (жанровый вес)
    for g in genres:
        tops = (
            db.query(Track.id)
            .filter(Track.genre == g)
            .order_by(Track.play_count.desc().nullslast())
            .limit(8)
            .all()
        )
        for i, (tid,) in enumerate(tops):
            if i < 3:
                _fav(str(tid))
            _hist(str(tid), 1)

    db.commit()
    fav_total = db.query(Favorite).filter_by(user_id=u.id).count()
    return {
        "ok": True,
        "user_id": str(u.id),
        "favorites_added": fav_added,
        "history_added": hist_added,
        "favorites_total": fav_total,
    }


def _require_user(db: Session, user_id: str):
    from app.db.models import MediaUser as _MU

    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    u = db.get(_MU, user_id)
    if not u:
        raise HTTPException(404, "not found")
    return u


@router.post("/{user_id}/rate")
def rate_track(user_id: str, payload: RateIn, db: Session = Depends(get_db)):
    """Оценка трека (обмен с плеером, порт mobile rateSong).

    like=true → лайк, false → дизлайк (+проверка автобана артиста),
    null → снять оценку.
    """
    from app.services import taste as _taste

    u = _require_user(db, user_id)
    try:
        uuid.UUID(payload.track_id)
    except ValueError:
        raise HTTPException(400, "invalid track_id")
    return _taste.record_rate(db, str(u.id), payload.track_id, payload.like)


@router.post("/{user_id}/events")
def push_events(user_id: str, payload: EventsIn, db: Session = Depends(get_db)):
    """Пакет событий плеера: play/complete/skip/replay/seek_back/abandon.

    Применяет правила mobile: 3 скипа → автодизлайк, 3 дизлайка артиста → автобан.
    """
    from app.services import taste as _taste

    u = _require_user(db, user_id)
    return _taste.record_events(db, str(u.id), payload.events or [])


@router.post("/{user_id}/ban-artist")
def ban_artist(user_id: str, payload: BanIn, db: Session = Depends(get_db)):
    """Ручной бан артиста (порт mobile banArtist)."""
    from app.db.models import ArtistBan as _AB

    u = _require_user(db, user_id)
    name = (payload.artist_name or "").strip()
    if not name:
        raise HTTPException(400, "artist_name required")
    if db.query(_AB).filter_by(user_id=u.id, artist_name=name).first() is None:
        db.add(_AB(user_id=u.id, artist_name=name[:512], reason="manual"))
        db.commit()
    return {"ok": True, "artist_name": name}


@router.post("/{user_id}/unban-artist")
def unban_artist(user_id: str, payload: BanIn, db: Session = Depends(get_db)):
    """Разбан артиста (порт mobile unbanArtist)."""
    from app.db.models import ArtistBan as _AB

    u = _require_user(db, user_id)
    db.query(_AB).filter_by(user_id=u.id, artist_name=(payload.artist_name or "").strip()).delete()
    db.commit()
    return {"ok": True}


@router.get("/{user_id}/profile")
def taste_profile(user_id: str, top_n: int = 50, db: Session = Depends(get_db)):
    """Вкусовой профиль пользователя: веса жанров/артистов, скоры треков,
    паттерны часов/дней, дизлайки, баны — для веба и обмена с плеером."""
    from app.services import taste as _taste

    _require_user(db, user_id)
    return _taste.user_profile(db, user_id, top_n=max(1, min(200, top_n)))


@router.post("/{user_id}/sync-from-mobile")
def sync_from_mobile(user_id: str, payload: dict, db: Session = Depends(get_db)):
    """Полный синк истории с мобильного плеера (обучение волны и плейлистов).

    Body: {ratings: [{external_id, like, playCount, skipCount, replayCount,
    seekBackCount, abandonCount, score, lastPlayed}], profile: {MLProfile},
    events: [{track_id|external_id, action, position_sec}]}.
    Мобила шлёт историю страницами (events до 5000/запрос, ratings до 20000).
    """
    from app.services import taste as _taste

    _require_user(db, user_id)
    try:
        return _taste.sync_from_mobile(db, user_id, payload or {})
    except Exception as e:  # noqa: BLE001
        from app.core.logging import get_logger as _gl

        _gl("api.users").exception("sync-from-mobile failed")
        return {"ok": False, "error": f"Синк не удался: {str(e)[:400]}"}


@router.get("/{user_id}/sync-to-mobile")
def sync_to_mobile(user_id: str, db: Session = Depends(get_db)):
    """Слепок сервера для мобилы: лайки/дизлайки/баны (external ids), веса, статы."""
    from app.services import taste as _taste

    _require_user(db, user_id)
    return _taste.sync_to_mobile(db, user_id)


@router.get("/{user_id}/vault")
def vault_status(user_id: str, db: Session = Depends(get_db)):
    """Есть ли запомненный пароль для автообновления (сам пароль не отдаём)."""
    from app.db.models import UserCredential as _UC
    from app.services import vault as _vault

    u = _require_user(db, user_id)
    stored = db.query(_UC).filter_by(user_id=u.id).first() is not None
    return {"stored": stored, "available": _vault.vault_available()}


@router.post("/{user_id}/vault")
def vault_store(user_id: str, payload: VaultIn, db: Session = Depends(get_db)):
    """Запомнить пароль (opt-in автообновление). Шифр Fernet, ключ в env."""
    from app.db.models import UserCredential as _UC
    from app.services import vault as _vault

    u = _require_user(db, user_id)
    if not (payload.password or "").strip():
        raise HTTPException(400, "password required")
    try:
        blob = _vault.encrypt_password(payload.password)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    row = db.query(_UC).filter_by(user_id=u.id).first()
    if row is None:
        db.add(_UC(user_id=u.id, enc_password=blob))
    else:
        row.enc_password = blob
    db.commit()
    return {"ok": True, "stored": True}


@router.delete("/{user_id}/vault")
def vault_forget(user_id: str, db: Session = Depends(get_db)):
    """Забыть пароль (выключить автообновление)."""
    from app.db.models import UserCredential as _UC

    u = _require_user(db, user_id)
    db.query(_UC).filter_by(user_id=u.id).delete()
    db.commit()
    return {"ok": True, "stored": False}


@router.post("/{user_id}/refresh-now")
def refresh_now(user_id: str, db: Session = Depends(get_db)):
    """Срочно обновить вкусы из Navidrome по запомненному паролю (без ожидания ночи)."""
    from app.db.models import UserCredential as _UC
    from app.services import taste_import as _ti
    from app.services import vault as _vault

    u = _require_user(db, user_id)
    row = db.query(_UC).filter_by(user_id=u.id).first()
    if not row:
        return {"ok": False, "error": "Пароль не запомнен — включите автообновление (vault) или импортируйте с паролем вручную"}
    try:
        password = _vault.decrypt_password(row.enc_password)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    try:
        res = _ti.import_user_tastes(str(u.server_id), u.external_id, password)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Импорт не удался: {str(e)[:400]}"}
    if res.get("status") != "success":
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, **res}
