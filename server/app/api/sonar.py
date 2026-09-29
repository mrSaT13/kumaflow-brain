"""«Сонар»: recognize для плееров, enroll/dedup/enrich для админа.

Контракт для клиентов (плееры/приложения):
  POST /api/sonar/recognize (multipart, поле ``audio``, до ~10МБ, 5-20с записи)
  -> {ok, match: {track_id, external_id, title, artist_name, ...} | None,
      query_hashes, took_ms}
Распознавание работает, только когда админ включил тумблер
(Настройки → Автоматизация → Сонар) и снял отпечатки (enroll).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.auth import require_admin, require_brain_auth
from app.db import get_db

router = APIRouter(dependencies=[Depends(require_brain_auth)])

MAX_UPLOAD_MB = 10


def _ensure_enabled(db) -> None:
    from app.services import automation as _auto
    if not _auto.sonar_enabled(db):
        raise HTTPException(403, "Сонар выключен тумблером "
                                 "(админ: Настройки → Автоматизация → Сонар)")


@router.get("", include_in_schema=False)
@router.get("/")
def sonar_status(db: Session = Depends(get_db)):
    from app.services import automation as _auto
    from app.services import sonar as _sonar
    cov = _sonar.coverage(db)
    return {"ok": True, "enabled": bool(_auto.sonar_enabled(db)),
            "coverage": cov}


@router.post("/enroll", dependencies=[Depends(require_admin)])
def sonar_enroll(limit: int = 500, force: bool = False,
                 db: Session = Depends(get_db)):
    """Снять отпечатки: чанками по limit (resume — готовые пропускаем)."""
    from app.db.models import ScanRun, ScanLog
    from app.services.media_server import resolve_active_server
    from app.services.queue import enqueue
    from app.workers.tasks import sonar_enroll as _task
    import uuid as _uuid

    _ensure_enabled(db)
    server = resolve_active_server(db)
    db.commit()
    run = ScanRun(id=str(_uuid.uuid4()), server_id=server.id, phase="sonar",
                  status="running", total_items=0, processed_items=0)
    db.add(run)
    db.flush()
    job_id = enqueue(_task, str(run.id), job_timeout=7200,
                     limit=max(0, int(limit or 0)), force=bool(force))
    db.add(ScanLog(id=str(_uuid.uuid4()), run_id=run.id, level="info",
                   message=f"Sonar enroll queued (job {job_id}, limit {limit})"))
    db.commit()
    return {"queued": True, "job_id": job_id, "run_id": str(run.id)}


@router.post("/recognize")
async def sonar_recognize(audio: UploadFile = File(...),
                          db: Session = Depends(get_db)):
    """Что сейчас играет? Прими запись с клиента, верни трек библиотеки."""
    from app.services import sonar as _sonar
    _ensure_enabled(db)
    t0 = time.monotonic()
    try:
        blob = await audio.read()
    except Exception:
        raise HTTPException(400, "не прочитал загрузку")
    if not blob:
        raise HTTPException(400, "пустая запись")
    if len(blob) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"запись больше {MAX_UPLOAD_MB} МБ")
    import tempfile as _tf
    suffix = ".wav"
    try:
        fn = (audio.filename or "").lower()
        if "." in fn:
            suffix = "." + fn.rsplit(".", 1)[-1][:8]
    except Exception:
        pass
    try:
        with _tf.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(blob)
            tmp = f.name
    except Exception:
        raise HTTPException(400, "не сохранил загрузку")
    try:
        try:
            y, f_sr = _sonar.load_mono(tmp, duration=_sonar.QUERY_SECONDS + 5.0)
            qhashes = _sonar.fingerprint_samples(y, f_sr,
                                                 max_seconds=_sonar.QUERY_SECONDS)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"не декодировал аудио: {str(e)[:200]}")
        if not qhashes:
            return {"ok": True, "match": None,
                    "query_hashes": 0, "took_ms": int((time.monotonic() - t0) * 1000),
                    "note": "в записи нет устойчивых пиков (тишина/шум)"}
        m = _sonar.recognize_track(db, qhashes)
        return {"ok": True, "match": m, "query_hashes": len(qhashes),
                "took_ms": int((time.monotonic() - t0) * 1000)}
    finally:
        try:
            import os as _os
            _os.unlink(tmp)
        except OSError:
            pass


@router.get("/duplicates", dependencies=[Depends(require_admin)])
def sonar_duplicates(min_shared: int = 25, db: Session = Depends(get_db)):
    """Пары с общими хэшами — кандидаты на сшивку (проверять вручную)."""
    from app.services import sonar as _sonar
    _ensure_enabled(db)
    return {"ok": True,
            "groups": _sonar.duplicates(db, min_shared=max(5, int(min_shared or 25)))}


@router.post("/enrich", dependencies=[Depends(require_admin)])
def sonar_enrich(db: Session = Depends(get_db)):
    """Добить пустые genre/year/album из фингерпринт-двойника."""
    from app.services import sonar as _sonar
    _ensure_enabled(db)
    return _sonar.enrich(db)
