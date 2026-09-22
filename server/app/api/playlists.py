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


def _to_dict(p: Playlist, db: Session) -> dict:
    count = (
        db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).count()
    )
    return {
        "id": str(p.id),
        "name": p.name,
        "is_public": p.is_public,
        "is_auto_generated": p.is_auto_generated,
        "generated_for_date": (
            p.generated_for_date.isoformat() if p.generated_for_date else None
        ),
        "track_count": count,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("", include_in_schema=False)
@router.get("/")
def list_playlists(db: Session = Depends(get_db)):
    rows = db.query(Playlist).order_by(Playlist.created_at.desc()).limit(100).all()
    return {"playlists": [_to_dict(p, db) for p in rows]}


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
    for it in items:
        t = db.get(Track, it.track_id)
        if t:
            tracks.append(
                {
                    "position": it.position,
                    "id": str(t.id),
                    "title": t.title,
                    "artist_name": t.artist_name,
                    "album_name": t.album_name,
                    "genre": t.genre,
                    "duration_sec": t.duration_sec,
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
    result = cold_start_playlist(str(server.id), n=n, user_id=resolved_user)
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


@router.delete("/{playlist_id}")
def delete_playlist(playlist_id: str, db: Session = Depends(get_db)):
    try:
        uuid.UUID(playlist_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    p = db.get(Playlist, playlist_id)
    if not p:
        raise HTTPException(404, "not found")
    db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).delete()
    db.delete(p)
    db.commit()
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
