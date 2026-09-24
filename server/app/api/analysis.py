from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.core.auth import require_admin, require_brain_auth
from app.services.demo import ensure_demo_server as _ensure_demo  # noqa: F401 (реэкспорт для совместимости)
from app.services.media_server import resolve_active_server
from app.services.ml import (
    build_clusters,
    cold_start_playlist,
    recommend_by_track,
    search_by_embedding,
)
from app.services.queue import enqueue

router = APIRouter(dependencies=[Depends(require_brain_auth)])


@router.get("/clap-status")
def clap_status(db: Session = Depends(get_db)):
    """Есть ли CLAP-модели в образе и сколько эмбеддингов в базе.

    available — текст-модель (поиск по смыслу); audio_* — аудио-ветка
    (гибрид в похожих). Без аудио-модели/флага — тихий фолбек на librosa.
    """
    from sqlalchemy import func as _func

    from app.db.models import TrackEmbedding

    available = False
    audio_available = False
    audio_enabled = False
    audio_weight = 0.0
    files: list[dict] = []
    flag_enabled = False
    models_dir = ""
    text_file: str | None = None
    audio_file: str | None = None
    try:
        from app.services import clap as _clap

        available = bool(_clap.is_available())
        audio_available = bool(_clap.is_audio_available())
        try:
            from app.core.config import get_settings as _gs

            _s = _gs()
            flag_enabled = bool(getattr(_s, "clap_enabled", False))
            audio_enabled = bool(getattr(_s, "clap_audio_enabled", False))
            audio_weight = float(getattr(_s, "clap_audio_weight", 0.7) or 0.0)
        except Exception:
            pass
        for p in sorted(_clap.MODELS_DIR.rglob("*.onnx")):
            if p.is_file():
                try:
                    files.append({"name": p.name, "bytes": int(p.stat().st_size)})
                except OSError:
                    files.append({"name": p.name, "bytes": 0})
        try:
            _tp = _clap._find_text_onnx()
            text_file = _tp.name if _tp else None
        except Exception:
            text_file = None
        try:
            _ap = _clap._find_audio_onnx()
            audio_file = _ap.name if _ap else None
        except Exception:
            audio_file = None
        try:
            models_dir = str(_clap.MODELS_DIR)
        except Exception:
            models_dir = ""
    except Exception:
        pass
    counts: dict[str, int] = {}
    try:
        for model, n in db.query(TrackEmbedding.model, _func.count()).group_by(TrackEmbedding.model).all():
            counts[str(model)] = int(n)
    except Exception:
        pass
    return {"available": available, "files": files, "embeddings": counts,
            "audio_available": audio_available, "audio_enabled": audio_enabled,
            "audio_weight": audio_weight,
            "audio_stub": not audio_available,
            "flag_enabled": flag_enabled, "models_dir": models_dir,
            "text_file": text_file, "audio_file": audio_file}


@router.post("/clusters/build", dependencies=[Depends(require_admin)])
def clusters_build(db: Session = Depends(get_db)):
    server = resolve_active_server(db)
    db.commit()
    res = build_clusters(str(server.id))
    return res


@router.get("/recommend/by-track/{track_id}")
def recommend_by_track_endpoint(track_id: str):
    return {"items": recommend_by_track(track_id, top_k=20)}


@router.get("/cold-start")
def cold_start(db: Session = Depends(get_db), n: int = 30, user_id: str | None = None):
    """Холодный старт: per-user если user_id указан (копия mobile seedTaste), иначе глобально."""
    server = resolve_active_server(db)
    db.commit()
    # user_id может быть uuid или external_id — пробуем резолв
    resolved: str | None = None
    if user_id:
        try:
            from app.db.models import MediaUser

            u = db.get(MediaUser, user_id)
            if u:
                resolved = str(u.id)
            else:
                # поиск по external_id
                q = db.query(MediaUser).filter(MediaUser.external_id == user_id).first()
                if q:
                    resolved = str(q.id)
                else:
                    resolved = user_id
        except Exception:
            resolved = user_id
    return cold_start_playlist(str(server.id), n=n, user_id=resolved)


@router.post("/search-by-text")
def search_by_text(payload: dict, db: Session = Depends(get_db)):
    """Текстовый поиск: CLAP-эмбеддинг если есть, иначе TF-IDF/keyword fallback.

    Payload: {"q": "грустный инди", "top_k": 20}
    """
    q = (payload.get("q") or payload.get("query") or "").strip() if isinstance(payload, dict) else ""
    top_k = int(payload.get("top_k") or payload.get("n") or 20) if isinstance(payload, dict) else 20
    top_k = max(1, min(50, top_k))
    if not q:
        return {"items": [], "mode": "empty", "query": q}

    # 1) CLAP — если модель скачана (ml/download_clap.py) и CLAP_ENABLED
    try:
        from app.services.clap import is_available as clap_ready, get_text_embedding
        from app.services.ml import search_by_embedding as _search_emb

        if clap_ready():
            vec = get_text_embedding(q)
            if vec:
                # есть ли эмбеддинги в БД? если нет — тихо fallback
                from app.db.models import TrackEmbedding

                cnt = db.query(TrackEmbedding).count()
                if cnt > 0:
                    items = _search_emb(vec, top_k=top_k)
                    if items:
                        return {"items": items, "mode": "embedding", "query": q}
                else:
                    # нет аудио-эмбеддингов — используем text эмбеддинг для ранжирования keyword-кандидатов
                    # (хеш-фолбэк всё равно даёт детерминированный скор)
                    pass
    except Exception as e:
        # не ломаем fallback
        from app.core.logging import get_logger

        get_logger("analysis").warning("clap search failed, fallback keyword: {}", e)

    # 2) TF-IDF fallback по title/artist/album/lyrics/mood (работает без ML)
    from sqlalchemy import or_

    from app.db.models import Lyrics, Track, TrackFeatures

    like = f"%{q.lower()}%"
    # прямой keyword-поиск (ILIKE через lower -> работает и на sqlite и на postgres)
    from sqlalchemy import func as _func

    try:
        base_q = db.query(Track).filter(
            or_(
                _func.lower(Track.title).like(like),
                _func.lower(Track.artist_name).like(like),
                _func.lower(Track.album_name).like(like),
                _func.lower(Track.genre).like(like),
            )
        )
        rows = base_q.order_by(Track.play_count.desc()).limit(top_k * 2).all()
    except Exception:
        rows = []
    # если есть тексты — дополняем поиском по lyrics
    lyric_ids: list[str] = []
    try:
        lyric_rows = (
            db.query(Lyrics.track_id)
            .filter(_func.lower(Lyrics.text).like(like))
            .limit(top_k)
            .all()
        )
        lyric_ids = [str(r[0]) for r in lyric_rows]
    except Exception:
        pass
    # mood fallback: ищем по TrackFeatures.mood_labels (json LIKE)
    mood_rows: list[Track] = []
    try:
        # SQLite: json_each, Postgres: @> — упрощаем через Python фильтрацию
        cand = db.query(Track).join(TrackFeatures, TrackFeatures.track_id == Track.id).all()
        q_low = q.lower()
        for t in cand:
            f = db.get(TrackFeatures, t.id)
            if f and f.mood_labels and any(q_low in str(m).lower() for m in f.mood_labels):
                mood_rows.append(t)
                if len(mood_rows) >= top_k:
                    break
    except Exception:
        pass

    seen: set[str] = set()
    items: list[dict] = []
    for t in list(rows) + mood_rows:
        tid = str(t.id)
        if tid in seen:
            continue
        seen.add(tid)
        items.append({"track_id": tid, "title": t.title, "artist_name": t.artist_name, "album_name": t.album_name, "genre": t.genre, "score": 1.0})
        if len(items) >= top_k and not lyric_ids:
            break
    # добавим lyric-хиты если не хватает
    if len(items) < top_k and lyric_ids:
        for tid in lyric_ids:
            if tid in seen:
                continue
            t = db.get(Track, tid)
            if not t:
                continue
            items.append({"track_id": tid, "title": t.title, "artist_name": t.artist_name, "album_name": t.album_name, "genre": t.genre, "score": 0.9})
            seen.add(tid)
            if len(items) >= top_k:
                break
    return {"items": items[:top_k], "mode": "keyword", "query": q}


@router.post("/lyrics/analyze-all")
def analyze_all(db: Session = Depends(get_db)):
    server = resolve_active_server(db)
    db.commit()
    from app.db.models import ScanRun, ScanLog, Track
    import uuid
    from app.workers.tasks import lyrics_fetch

    total = db.query(Track).filter_by(server_id=server.id).count()
    run = ScanRun(
        id=str(uuid.uuid4()),
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
            id=str(uuid.uuid4()),
            run_id=run.id,
            level="info",
            message=f"Запущен AI-анализ текстов (job {job_id})",
        )
    )
    db.commit()
    return {"queued": True, "run_id": str(run.id)}
