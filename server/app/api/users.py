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
    include_playlists: bool = False  # по умолчанию только вкусы (быстро); плейлисты — отдельным вызовом


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
    include_playlists: bool = False  # вкусы (starred) — быстро; плейлисты — отдельно, там долго
    include_starred: bool = True


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

    try:
        res = import_user_tastes(str(server.id), payload.username, payload.password,
                                 include_playlists=bool(payload.include_playlists))
    except Exception as e:  # noqa: BLE001 — читаемая ошибка вместо голого 500
        from app.core.logging import get_logger as _gl

        _gl("api.users").exception("by-credentials failed")
        return {"ok": False, "error": f"Импорт не удался: {str(e)[:400]}"}
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
    """Импорт вкусов (лайки) для существующего пользователя.

    По умолчанию — только starred (быстро, без таймаутов).
    Плейлисты — отдельно через /{user_id}/sync-playlists (там долго,
    лучше фоном через refresh-async).

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
        res = import_user_tastes(str(u.server_id), u.external_id, password,
                                 include_starred=bool(payload.include_starred),
                                 include_playlists=bool(payload.include_playlists))
    except Exception as e:  # noqa: BLE001 — читаемая ошибка вместо голого 500
        from app.core.logging import get_logger as _gl

        _gl("api.users").exception("import-tastes failed")
        return {"ok": False, "error": f"Импорт не удался: {str(e)[:400]}"}
    if res.get("status") != "success":
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, **res}


@router.post("/{user_id}/sync-playlists")
def sync_playlists(user_id: str, payload: UserTasteImportIn, db: Session = Depends(get_db)):
    """Отдельная синхронизация плейлистов (долгая — дёргает getPlaylist на каждый).

    Вкусы (starred) не трогает. Для больших библиотек лучше фоном:
    запомните пароль (vault) и жмите refresh-async.
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
        res = import_user_tastes(str(u.server_id), u.external_id, password,
                                 include_starred=False, include_playlists=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Синк плейлистов не удался: {str(e)[:400]}"}
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


@router.post("/{user_id}/refresh-async")
def refresh_async(user_id: str, db: Session = Depends(get_db)):
    """Фоновый синк вкусов: ставит refresh_tastes(user_id) в очередь, не блокируя HTTP (для PWA-кнопки)."""
    import uuid as _uuid
    from datetime import datetime as _dt

    from app.db.models import ScanLog as _SL
    from app.db.models import ScanRun as _SR
    from app.db.models import UserCredential as _UC
    from app.services.queue import enqueue as _enq

    u = _require_user(db, user_id)
    row = db.query(_UC).filter_by(user_id=u.id).first()
    if not row:
        return {"ok": False, "error": "Пароль не запомнен — включите автообновление (vault) или импортируйте с паролем вручную"}
    # защита от дубля: уже бежит одиночный синк этого юзера
    busy = db.query(_SR).filter(
        _SR.phase == "taste_refresh_single", _SR.status.in_(["queued", "running"])).first()
    if busy:
        return {"ok": False, "error": "Синк уже выполняется — дождитесь завершения", "run_id": str(busy.id)}
    run = _SR(id=str(_uuid.uuid4()), server_id=u.server_id, phase="taste_refresh_single",
              status="running", total_items=1, processed_items=0, started_at=_dt.utcnow(),
              metadata_extra={"user_id": str(u.id)})
    db.add(run)
    db.flush()
    from app.workers.tasks import refresh_tastes as _rt

    job_id = _enq(_rt, str(run.id), job_timeout=600, user_id=str(u.id))
    try:
        run.metadata_extra = dict(run.metadata_extra or {}) | {"job_id": job_id}
    except Exception:
        run.metadata_extra = {"job_id": job_id}
    db.add(_SL(id=str(_uuid.uuid4()), run_id=run.id, level="info",
               message=f"Фоновый синк вкусов {u.username} (job {job_id})"))
    db.commit()
    return {"ok": True, "queued": True, "run_id": str(run.id), "job_id": job_id}


def _resolve_track(db: Session, raw: str):
    """Наш uuid как есть, иначе external_id (мобильный id) -> трек."""
    from app.db.models import Track

    s = (raw or '').strip()
    if not s:
        return None
    t = db.get(Track, s)
    if t is not None:
        return t
    return db.query(Track).filter(Track.external_id == s).first()


@router.post("/{user_id}/history")
def log_history(user_id: str, payload: dict, db: Session = Depends(get_db)):
    """Память мозга: клиент сообщает «сыграл трек».

    Body: {track_id (наш uuid или external_id), played_at? ISO, source?}.
    Пишет PlayHistory + PlayEvent(play) + TrackStat.plays/last_played —
    это и кормит волну, и отдаётся в GET history.
    """
    from datetime import datetime

    from app.db.models import PlayEvent as _PE
    from app.db.models import PlayHistory as _PH
    from app.db.models import TrackStat as _TS

    u = _require_user(db, user_id)
    t = _resolve_track(db, str((payload or {}).get('track_id') or ''))
    if t is None:
        raise HTTPException(404, 'track not found')
    played_at = None
    raw_ts = (payload or {}).get('played_at')
    if raw_ts:
        try:
            played_at = datetime.fromisoformat(str(raw_ts).replace('Z', ''))
        except (TypeError, ValueError):
            played_at = None
    now = played_at or datetime.utcnow()
    db.add(_PH(user_id=str(u.id), track_id=str(t.id), played_at=now))
    db.add(_PE(user_id=str(u.id), track_id=str(t.id), action='play',
               hour=now.hour, day_of_week=now.isoweekday() % 7))
    st = db.get(_TS, {'user_id': str(u.id), 'track_id': str(t.id)})
    if st is None:
        st = _TS(user_id=str(u.id), track_id=str(t.id))
        db.add(st)
    st.plays = (st.plays or 0) + 1
    if not st.last_played or now > st.last_played:
        st.last_played = now
    db.commit()
    return {'ok': True, 'track_id': str(t.id)}


@router.get("/{user_id}/history")
def get_history(user_id: str, limit: int = 50, offset: int = 0,
                db: Session = Depends(get_db)):
    """Последние прослушивания, новые первые: [{track_id, title, artist, played_at}]."""
    from app.db.models import PlayHistory as _PH
    from app.db.models import Track as _T

    u = _require_user(db, user_id)
    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    rows = (db.query(_PH, _T)
            .join(_T, _T.id == _PH.track_id)
            .filter(_PH.user_id == str(u.id))
            .order_by(_PH.played_at.desc())
            .offset(offset).limit(limit).all())
    total = db.query(_PH).filter(_PH.user_id == str(u.id)).count()
    return {'ok': True, 'user_id': str(u.id), 'total': total,
            'items': [{'track_id': str(ph.track_id), 'title': t.title,
                       'artist_name': t.artist_name, 'album_name': t.album_name,
                       'genre': t.genre,
                       'played_at': ph.played_at.isoformat() if ph.played_at else None}
                      for ph, t in rows]}


@router.get("/{user_id}/recent-events")
def recent_events(user_id: str, limit: int = 50, db: Session = Depends(get_db)):
    """Свежие события плеера для динамической волны клиента (play/skip/replay/...)."""
    from app.db.models import PlayEvent as _PE

    u = _require_user(db, user_id)
    limit = max(1, min(200, int(limit or 50)))
    rows = (db.query(_PE).filter(_PE.user_id == str(u.id))
            .order_by(_PE.created_at.desc()).limit(limit).all())
    return {'ok': True, 'user_id': str(u.id),
            'items': [{'track_id': str(r.track_id), 'action': r.action,
                       'position_sec': r.position_sec,
                       'created_at': r.created_at.isoformat() if r.created_at else None}
                      for r in rows]}


@router.delete("/{user_id}/history")
def clear_history(user_id: str, db: Session = Depends(get_db)):
    """Стереть память прослушиваний (лайки/дизлайки/баны не трогаем)."""
    from app.db.models import PlayEvent as _PE
    from app.db.models import PlayHistory as _PH

    u = _require_user(db, user_id)
    h = db.query(_PH).filter(_PH.user_id == str(u.id)).delete()
    e = db.query(_PE).filter(_PE.user_id == str(u.id)).delete()
    db.commit()
    return {'ok': True, 'cleared_history': h, 'cleared_events': e}


@router.delete("/history")
def clear_all_history(db: Session = Depends(get_db)):
    """Стереть ВСЮ историю прослушиваний и событий (кнопка «Очистить историю»). Лайки/плейлисты не трогаем."""
    from app.db.models import PlayEvent as _PE
    from app.db.models import PlayHistory as _PH

    h = db.query(_PH).delete()
    e = db.query(_PE).delete()
    db.commit()
    return {'ok': True, 'cleared_history': h, 'cleared_events': e}


@router.get("/{user_id}/wrapped")
def wrapped(user_id: str, year: int | None = None, month: int | None = None,
            db: Session = Depends(get_db)):
    """Итоги месяца (self-hosted Wrapped): топы, открытия, часы, лайки."""
    from datetime import datetime as _dt

    from app.services import wrapped as _w

    u = _require_user(db, user_id)
    months = _w.available_months(db, str(u.id))
    now = _dt.utcnow()
    y, m = int(year or 0), int(month or 0)
    if not (1 <= m <= 12 and 2000 <= y <= 2100):
        if months:
            y, m = int(months[0][:4]), int(months[0][5:7])
        else:
            y, m = now.year, now.month
    return {**_w.month_summary(db, str(u.id), y, m), "months": months,
            "user_id": str(u.id)}


@router.get("/{user_id}/drift")
def taste_drift(user_id: str, weeks_ago: int = 4, db: Session = Depends(get_db)):
    """Дрейф вкуса: текущий профиль vs снапшот N недель назад + фраза."""
    from app.services import drift as _drift

    u = _require_user(db, user_id)
    return {**_drift.compare(db, str(u.id), weeks_ago=max(1, min(12, int(weeks_ago or 4)))),
            "user_id": str(u.id)}


@router.post("/{user_id}/drift/snapshot")
def take_drift_snapshot(user_id: str, db: Session = Depends(get_db)):
    """Снять слепок вкуса вручную (иначе — крон по понедельникам)."""
    from app.services import drift as _drift

    u = _require_user(db, user_id)
    return _drift.take_snapshot(db, str(u.id))


@router.get("/{user_id}/activity")
def year_activity(user_id: str, year: int | None = None, db: Session = Depends(get_db)):
    """Активность за год для тепловой карты: {days: {'2026-09-01': n}, total}."""
    from collections import Counter
    from datetime import datetime as _dt

    from app.db.models import PlayHistory as _PH

    u = _require_user(db, user_id)
    y = int(year or 0) or _dt.utcnow().year
    rows = db.query(_PH.played_at).filter(
        _PH.user_id == str(u.id), _PH.played_at.is_not(None)).all()
    days: Counter = Counter()
    for (when,) in rows:
        if when and when.year == y:
            days[when.date().isoformat()] += 1
    return {"ok": True, "user_id": str(u.id), "year": y,
            "days": dict(days), "total": sum(days.values()),
            "active_days": len(days)}
