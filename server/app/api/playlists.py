from __future__ import annotations

import uuid
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.db.models import Playlist, PlaylistTrack, Track
from app.services.demo import ensure_demo_server as _ensure_demo  # noqa: F401 (реэкспорт для совместимости)
from app.services.media_server import resolve_active_server
from app.services.queue import enqueue
from app.workers.tasks import daily_playlist, lyrics_fetch
from app.services.ml import cold_start_playlist

router = APIRouter()


class GenerateIn(BaseModel):
    n: int = 30
    user_id: str | None = None
    query: str | None = None  # если указан — AI генератор (копия mobile ai_mix_service)


def _hidden_ids(db: Session) -> set[str]:
    """Скрытые плейлисты (AppSetting, без миграции — id списком)."""
    from app.db.models import AppSetting

    row = db.get(AppSetting, "hidden_playlists")
    val = row.value if row and isinstance(row.value, dict) else {}
    ids = val.get("ids") or []
    return {str(x) for x in ids if x}


def _to_dict(p: Playlist, db: Session) -> dict:
    count = (
        db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).count()
    )
    return {
        "id": str(p.id),
        "name": p.name,
        "external_id": p.external_id,
        "in_navidrome": bool(p.external_id),
        "is_public": p.is_public,
        "is_auto_generated": p.is_auto_generated,
        "generated_for_date": (
            p.generated_for_date.isoformat() if p.generated_for_date else None
        ),
        "track_count": count,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "is_hidden": str(p.id) in _hidden_ids(db),
    }


@router.get("", include_in_schema=False)
@router.get("/")
def list_playlists(show_hidden: bool = False, db: Session = Depends(get_db)):
    rows = db.query(Playlist).order_by(Playlist.created_at.desc()).limit(200).all()
    hidden = _hidden_ids(db)
    items = [_to_dict(p, db) for p in rows]
    if not show_hidden:
        items = [it for it in items if it["id"] not in hidden]
    return {"playlists": items, "hidden_count": len(hidden)}


@router.post("/{playlist_id}/hide")
def hide_playlist(playlist_id: str, db: Session = Depends(get_db)):
    """Скрыть из списка (не удаляет, открывает по прямой ссылке)."""
    from app.db.models import AppSetting

    try:
        uuid.UUID(playlist_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    if not db.get(Playlist, playlist_id):
        raise HTTPException(404, "not found")
    row = db.get(AppSetting, "hidden_playlists")
    ids = {str(playlist_id)} | _hidden_ids(db)
    value = {"ids": sorted(ids)}
    if row is None:
        db.add(AppSetting(key="hidden_playlists", value=value))
    else:
        row.value = value
    db.commit()
    return {"ok": True, "is_hidden": True}


@router.post("/{playlist_id}/unhide")
def unhide_playlist(playlist_id: str, db: Session = Depends(get_db)):
    from app.db.models import AppSetting

    try:
        uuid.UUID(playlist_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    row = db.get(AppSetting, "hidden_playlists")
    ids = _hidden_ids(db) - {str(playlist_id)}
    if row is None:
        db.add(AppSetting(key="hidden_playlists", value={"ids": sorted(ids)}))
    else:
        row.value = {"ids": sorted(ids)}
    db.commit()
    return {"ok": True, "is_hidden": False}


@router.get("/{playlist_id}")
def get_playlist(playlist_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(playlist_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    p = db.get(Playlist, playlist_id)
    if not p:
        raise HTTPException(404, "not found")
    items = (
        db.query(PlaylistTrack)
        .filter(PlaylistTrack.playlist_id == p.id)
        .order_by(PlaylistTrack.position.asc())
        .all()
    )
    tracks = []
    # bulk фичей одним запросом: настроение/тональность + схожесть с миксом
    tids = [str(it.track_id) for it in items]
    feat_map: dict = {}
    if tids:
        from app.db.models import TrackFeatures

        for f in db.query(TrackFeatures).filter(TrackFeatures.track_id.in_(tids)).all():
            feat_map[str(f.track_id)] = f
    # схожесть: косинус вектора трека к центроиду плейлиста (как MMR-скор)
    sim_map: dict[str, float] = {}
    try:
        from app.services.ml import _cosine, _feature_vector

        vecs = []
        for tid in tids:
            f = feat_map.get(tid)
            if f is not None:
                v = _feature_vector(None, f)  # type: ignore[arg-type]
                if v is not None:
                    vecs.append((tid, v))
        if len(vecs) >= 2:
            import numpy as _np

            centroid = _np.mean(_np.vstack([v for _, v in vecs]), axis=0)
            for tid, v in vecs:
                sim_map[tid] = round(float(_cosine(v, centroid)), 4)
    except Exception:
        sim_map = {}
    for it in items:
        t = db.get(Track, it.track_id)
        if t:
            f = feat_map.get(str(t.id))
            key = None
            if f and (f.key_name or f.scale):
                key = f"{f.key_name or ''} {f.scale or ''}".strip()
            tracks.append(
                {
                    "position": it.position,
                    "id": str(t.id),
                    "title": t.title,
                    "artist_name": t.artist_name,
                    "album_name": t.album_name,
                    "genre": t.genre,
                    "duration_sec": t.duration_sec,
                    "mood": list(f.mood_labels[:4]) if f and f.mood_labels else [],
                    "musical_key": key,
                    "energy": float(f.energy) if f and f.energy is not None else None,
                    "similarity": sim_map.get(str(t.id)),
                    "added_at": it.added_at.isoformat() if it.added_at else None,
                }
            )
    return {**_to_dict(p, db), "tracks": tracks}


@router.post("/generate-daily")
def generate_daily(payload: GenerateIn | None = None, db: Session = Depends(get_db)):
    """Ежедневный: per-user cold-start + оркестратор волной. Если query указан — AI генератор."""
    n = (payload.n if payload else 30) or 30
    server = resolve_active_server(db)
    db.commit()

    today = date.today()
    # user_id резолв
    user_id = (payload.user_id if payload else None)
    resolved_user: str | None = None
    if user_id:
        try:
            from app.db.models import MediaUser

            u = db.get(MediaUser, user_id)
            if u:
                resolved_user = str(u.id)
            else:
                q = db.query(MediaUser).filter(MediaUser.external_id == user_id).first()
                resolved_user = str(q.id) if q else user_id
        except Exception:
            resolved_user = user_id

    # удалить прошлые авто-плейлисты за сегодня (per-user если user указан)
    stale = (
        db.query(Playlist)
        .filter(
            Playlist.is_auto_generated.is_(True),
            Playlist.server_id == server.id,
            Playlist.generated_for_date.isnot(None),
        )
        .all()
    )
    for p in stale:
        same_user = (p.owner_user_id == resolved_user) if resolved_user else (p.owner_user_id is None)
        if same_user and p.generated_for_date and p.generated_for_date.date() >= today:
            db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).delete()
            db.delete(p)
    db.flush()

    # имя с пользователем
    suffix = f" · {resolved_user[:8]}" if resolved_user else ""
    p = Playlist(
        id=str(uuid.uuid4()),
        server_id=server.id,
        owner_user_id=resolved_user,
        name=f"KumaFlow Daily{suffix} · {today.isoformat()}",
        is_auto_generated=True,
        generated_for_date=datetime.combine(today, datetime.min.time()),
    )
    db.add(p)
    db.flush()

    # если query — AI генератор (копия mobile ai_mix_service)
    if payload and payload.query and payload.query.strip():
        from app.services.playlist_ai import generate_from_prompt

        res = generate_from_prompt(db, query=payload.query.strip(), desired=n)
        # оркестрация волной
        try:
            from app.services.orchestrator import create_energy_wave

            tracks = res.get("songs") or []
            waved = create_energy_wave(tracks)
            order = {str(t.id): idx for idx, t in enumerate(waved)} if waved else {}
            ids = res.get("ids") or []
            ids = sorted(ids, key=lambda tid: order.get(tid, 999))
        except Exception:
            ids = res.get("ids") or []
        for pos, tid in enumerate(ids):
            db.add(PlaylistTrack(playlist_id=p.id, track_id=tid, position=pos))
        db.commit()
        return {"queued": True, "playlist_id": str(p.id), "tracks": len(ids), "name": res.get("name"), "comment": res.get("comment"), "from_fallback": res.get("from_fallback"), "mode": "ai"}
    # иначе cold-start per-user + оркестратор
    try:
        result = cold_start_playlist(str(server.id), n=n, user_id=resolved_user)
    except Exception as e:  # noqa: BLE001 — отдаём текст, а не голый 500
        from app.core.logging import get_logger as _gl

        _gl("api.playlists").exception("generate-daily cold-start failed")
        db.rollback()
        raise HTTPException(500, f"cold-start не удался: {str(e)[:400]}")
    # волной оркестрируем
    try:
        from app.services.orchestrator import create_energy_wave

        # резолв треков для волны
        tids = result["tracks"]
        tracks = [db.get(Track, tid) for tid in tids]
        tracks = [t for t in tracks if t]
        waved = create_energy_wave(tracks)
        order = {str(t.id): i for i, t in enumerate(waved)}
        tids = sorted(tids, key=lambda tid: order.get(tid, 999))
    except Exception:
        tids = result["tracks"]
    for pos, tid in enumerate(tids):
        db.add(PlaylistTrack(playlist_id=p.id, track_id=tid, position=pos))
    db.commit()
    return {
        "queued": True,
        "playlist_id": str(p.id),
        "tracks": len(tids),
        "steps": result["steps"],
        "mode": "cold_start",
        "user_id": resolved_user,
    }


@router.post("/my-wave")
def my_wave(payload: dict, db: Session = Depends(get_db)):
    """«Моя волна» как у Яндекс Музыки: бесконечный персональный поток вкуса.

    Body: {user_id (обязательно), n=30, seed_track_id?, mood?}.
    Новизна mobile (недавнее реже), дизлайки/баны исключены, сид — волна
    от трека, mood — волна по настроению. Сохраняется обычным (не авто)
    плейлистом «Моя волна», прошлая волна пользователя заменяется.
    """
    from app.services.ml import cold_start_playlist

    user_id = str((payload or {}).get("user_id") or "")
    n = int((payload or {}).get("n") or 30)
    n = max(5, min(100, n))
    seed = (payload or {}).get("seed_track_id")
    mood = ((payload or {}).get("mood") or "").strip() or None
    if not user_id:
        raise HTTPException(400, "user_id required")
    from app.db.models import MediaUser

    u = db.get(MediaUser, user_id)
    if not u:
        try:
            u = db.query(MediaUser).filter(MediaUser.external_id == user_id).first()
        except Exception:
            u = None
    if not u:
        raise HTTPException(404, "user not found")
    server = resolve_active_server(db)
    db.commit()
    # прошлую волну пользователя заменяем (не копим)
    for p in db.query(Playlist).filter(
            Playlist.server_id == server.id, Playlist.owner_user_id == u.id,
            Playlist.is_auto_generated.is_(False),
            Playlist.name.like("Моя волна%")).all():
        db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).delete()
        db.delete(p)
    db.flush()
    result = cold_start_playlist(str(server.id), n=n, user_id=str(u.id),
                                 seed_track_id=seed, mood=mood, novelty=True)
    tids = result["tracks"]
    try:
        from app.services.orchestrator import create_energy_wave

        tracks = [db.get(Track, tid) for tid in tids]
        tracks = [t for t in tracks if t]
        waved = create_energy_wave(tracks)
        order = {str(t.id): i for i, t in enumerate(waved)}
        tids = sorted(tids, key=lambda tid: order.get(tid, 999))
    except Exception:
        pass
    today = date.today()
    p = Playlist(id=str(uuid.uuid4()), server_id=server.id, owner_user_id=u.id,
                 name=f"Моя волна · {today.isoformat()}", is_auto_generated=False)
    db.add(p)
    db.flush()
    for pos, tid in enumerate(tids):
        db.add(PlaylistTrack(playlist_id=p.id, track_id=tid, position=pos))
    db.commit()
    return {"playlist_id": str(p.id), "tracks": len(tids), "steps": result["steps"],
            "excluded_disliked": result.get("excluded_disliked", 0),
            "excluded_banned": result.get("excluded_banned", 0), "mode": "my_wave"}


@router.post("/ai-generate")
def ai_generate(payload: dict, db: Session = Depends(get_db)):
    """AI генератор как на мобиле: query -> candidates -> LLM -> playlist."""
    q = (payload.get("query") or payload.get("q") or "").strip()
    n = int(payload.get("n") or payload.get("desiredCount") or 30)
    user_id = payload.get("user_id")
    if not q:
        raise HTTPException(400, "query required")
    server = resolve_active_server(db)
    db.commit()
    from app.services.playlist_ai import generate_from_prompt

    res = generate_from_prompt(db, query=q, desired=n)
    # создаём плейлист
    p = Playlist(
        id=str(uuid.uuid4()),
        server_id=server.id,
        owner_user_id=user_id,
        name=res.get("name") or f"AI Mix · {q[:24]}",
        is_auto_generated=False,
        generated_for_date=None,
    )
    db.add(p)
    db.flush()
    try:
        from app.services.orchestrator import create_energy_wave

        tracks = res.get("songs") or []
        waved = create_energy_wave(tracks)
        order = {str(t.id): idx for idx, t in enumerate(waved)} if waved else {}
        ids = res.get("ids") or []
        ids = sorted(ids, key=lambda tid: order.get(tid, 999))
    except Exception:
        ids = res.get("ids") or []
    for pos, tid in enumerate(ids):
        db.add(PlaylistTrack(playlist_id=p.id, track_id=tid, position=pos))
    db.commit()
    return {"playlist_id": str(p.id), "name": res.get("name"), "comment": res.get("comment"), "tracks": len(ids), "from_fallback": res.get("from_fallback"), "ids": ids}


@router.post("/{playlist_id}/export")
def export_playlist(playlist_id: str, db: Session = Depends(get_db)):
    """Выгрузить плейлист в Navidrome (чтобы появился в родном клиенте).

    Создаёт плейлист через Subsonic createPlaylist из треков Navidrome
    (файлы с диска пропускаются — Navidrome их не знает). Повторный вызов
    пересоздаёт удалённый плейлист заново. ID в Navidrome запоминаем.
    """
    import asyncio

    try:
        uuid.UUID(playlist_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    p = db.get(Playlist, playlist_id)
    if not p:
        raise HTTPException(404, "not found")
    items = (
        db.query(PlaylistTrack)
        .filter(PlaylistTrack.playlist_id == p.id)
        .order_by(PlaylistTrack.position.asc())
        .all()
    )
    nav_ids: list[str] = []
    skipped = 0
    for it in items:
        t = db.get(Track, it.track_id)
        ext = str(t.external_id or "") if t else ""
        if ext and not ext.startswith(("disk:", "dartist:", "dalbum:", "demo-")):
            nav_ids.append(ext)
        else:
            skipped += 1
    if not nav_ids:
        return {"ok": False, "error": "В плейлисте нет треков из Navidrome (только файлы с диска) — выгружать нечего"}
    from app.services.media_server import get_media_server_config
    from app.services.navidrome.client import SubsonicAuth, SubsonicClient

    try:
        cfg = get_media_server_config(db)
    except Exception:
        cfg = {}
    url, user, password = (cfg.get("url") or ""), (cfg.get("user") or ""), (cfg.get("password") or "")
    if not url or not user:
        return {"ok": False, "error": "Медиа-сервер не настроен (Настройки → Медиа-сервер)"}

    async def _push() -> str:
        async with SubsonicClient(url, SubsonicAuth(user=user, password=password), timeout=60.0) as client:
            if p.external_id:
                try:
                    await client.delete_playlist(p.external_id)
                except Exception:
                    pass
            created = await client.create_playlist(name=p.name, song_ids=nav_ids)
            remote_id = str(created.get("id") or "")
            if not remote_id:
                raise RuntimeError("Navidrome не вернул id созданного плейлиста")
            return remote_id

    try:
        remote_id = asyncio.run(_push())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Navidrome: {str(e)[:300]}"}
    p.external_id = remote_id
    db.commit()
    return {"ok": True, "navidrome_id": remote_id, "exported": len(nav_ids), "skipped": skipped}


@router.delete("/{playlist_id}")
def delete_playlist(playlist_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(playlist_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    p = db.get(Playlist, playlist_id)
    if not p:
        raise HTTPException(404, "not found")
    remote_id = p.external_id
    db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).delete()
    db.delete(p)
    db.commit()
    # заодно убрать копию в Navidrome (тихо — локальное удаление главнее)
    if remote_id:
        try:
            import asyncio

            from app.services.media_server import get_media_server_config
            from app.services.navidrome.client import SubsonicAuth, SubsonicClient

            try:
                cfg = get_media_server_config(db)
            except Exception:
                cfg = {}

            async def _rm() -> None:
                async with SubsonicClient(
                    cfg.get("url") or "", SubsonicAuth(cfg.get("user") or "", cfg.get("password") or "")
                ) as client:
                    await client.delete_playlist(remote_id)

            if cfg.get("url") and cfg.get("user"):
                asyncio.run(_rm())
        except Exception:
            pass
    return {"ok": True}


@router.post("/fetch-lyrics")
def fetch_lyrics_now(db: Session = Depends(get_db)):
    """Запустить загрузку текстов + AI-анализ настроения прямо сейчас (как scan)."""
    server = resolve_active_server(db)
    db.commit()
    from app.db.models import ScanRun

    total = db.query(Track).filter_by(server_id=server.id).count()
    from app.db.models import ScanLog
    import uuid as _uuid

    run = ScanRun(
        id=str(_uuid.uuid4()),
        server_id=server.id,
        phase="lyrics",
        status="running",
        total_items=total,
        processed_items=0,
    )
    db.add(run)
    db.flush()
    job_id = enqueue(lyrics_fetch, str(run.id))
    db.add(
        ScanLog(
            id=str(_uuid.uuid4()),
            run_id=run.id,
            level="info",
            message=f"Запущена загрузка текстов (job {job_id})",
        )
    )
    db.commit()
    return {"queued": True, "run_id": str(run.id), "job_id": job_id}
