"""API «спасателя запроса»: исправляем то, как голос услышали.

Сценарий: колонка/микрофон услышал «Эминем», ASR написал кириллицей, обычный
поиск не нашёл. Клиент шлёт сюда то, что распознал, — мозг отдаёт кандидатов
и, если уверен, сразу «правильное» имя.

Контракт:
  POST /api/resolve/query
      {"q": "Эминем", "kind": "artist", "top_k": 5, "source": "voice",
       "learn": true}
    → {"ok": true, "query", "best": {...}|null, "candidates": [...],
       "reason", "confident": bool}

  POST /api/resolve/confirm
      {"q": "Эминем", "name": "Eminem", "kind": "artist"}
    → мозг запоминает соответствие навсегда (подтверждение юзера).

  GET /api/resolve/aliases  → что мозг уже выучил.
  GET /api/resolve/misses   → где мозг пока спотыкается (неразобранные промахи).

Авторизация — общая мозговая (require_brain_auth), как у остальных клиентских
эндпоинтов. user_id берётся из токена; для открытой LAN можно передать явно.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.auth import require_brain_auth
from app.db import get_db

router = APIRouter(dependencies=[Depends(require_brain_auth)])


def _owner(request: Request, explicit: str | None = None) -> str | None:
    """Владелец запроса: из токена, иначе явный user_id (открытая LAN)."""
    info = getattr(request.state, "brain_token", None) or {}
    owner = info.get("owner_user_id")
    if owner:
        return str(owner)
    if explicit:
        return str(explicit)
    return None


@router.post("/query")
async def resolve_query(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Строка на входе → кандидаты на выходе.

    kind: artist | track | any (any — ищем и там, и там, но score один и тот же).
    learn: true (по умолчанию) — если ничего не нашлось, мозг запомнит промах
    и позже попробует догадаться сам.
    """
    from app.services import query_learning as _ql
    from app.services.query_resolver import is_confident, resolve as _resolve

    if not isinstance(payload, dict):
        payload = {}
    q = str(payload.get("q") or payload.get("query") or "").strip()
    kind = str(payload.get("kind") or "artist").strip().lower()
    if kind not in ("artist", "track", "any"):
        kind = "artist"
    try:
        top_k = int(payload.get("top_k") or payload.get("n") or 5)
    except Exception:
        top_k = 5
    source = str(payload.get("source") or "voice").strip() or "voice"
    learn = payload.get("learn", True)
    uid = _owner(request, payload.get("user_id"))

    if not q:
        return {"ok": True, "query": "", "best": None, "candidates": [],
                "reason": "пусто", "confident": False}

    res = _resolve(q, db, user_id=uid, kind=kind, top_k=top_k)

    # Не нашли уверенно — спросим ещё и Яндекс.Музыку (внешний каталог).
    if not is_confident(res):
        try:
            from app.services.yandex_music.client import search_artists

            ym = await search_artists(db, q, limit=top_k)
            names = [str(a.get("name") or "") for a in ym if a.get("name")]
            if names:
                res = _resolve(q, db, user_id=uid, kind=kind, top_k=top_k,
                               extra_names=names)
        except Exception:
            pass

    confident = is_confident(res)
    res["confident"] = confident

    # Промах — сырьё для самообучения: мозг сам попробует догадаться позже.
    if not confident and learn:
        try:
            _ql.note_miss(db, q, user_id=uid, kind=kind, source=source)
            db.commit()
        except Exception:
            pass
    return res


@router.post("/confirm")
def resolve_confirm(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Юзер подтвердил: «да, это Eminem» — запоминаем навсегда."""
    from app.services.query_learning import confirm_alias

    if not isinstance(payload, dict):
        payload = {}
    q = str(payload.get("q") or payload.get("query") or "").strip()
    name = str(payload.get("name") or payload.get("target") or "").strip()
    kind = str(payload.get("kind") or "artist").strip().lower() or "artist"
    uid = _owner(request, payload.get("user_id"))
    if not q or not name:
        return {"ok": False, "error": "нужны q и name"}
    out = confirm_alias(db, q, name, user_id=uid, kind=kind,
                        external_id=payload.get("external_id"))
    return out


@router.get("/aliases")
def resolve_aliases(request: Request, db: Session = Depends(get_db),
                    user_id: str | None = None, limit: int = 100):
    """Что мозг уже выучил (для UI/отладки)."""
    from app.services.query_learning import list_aliases

    uid = _owner(request, user_id)
    return {"ok": True, "aliases": list_aliases(db, user_id=uid, limit=limit)}


@router.delete("/aliases")
def resolve_delete_alias(alias: str, request: Request, db: Session = Depends(get_db),
                         user_id: str | None = None, kind: str = "artist"):
    """Забыть соответствие: мозг понял неверно — юзер поправил."""
    from app.services.query_learning import delete_alias

    uid = _owner(request, user_id)
    ok = delete_alias(db, alias, user_id=uid, kind=kind)
    try:
        db.commit()
    except Exception:
        pass
    return {"ok": bool(ok), "alias": alias}


@router.post("/import")
def resolve_import(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Массово добавить соответствия из текста: «слово = артист» по строке.

    Тот же формат, что и экспорт, — файл можно скопировать туда-обратно.
    """
    from app.services.query_learning import import_aliases

    if not isinstance(payload, dict):
        payload = {}
    text = str(payload.get("text") or "")
    kind = str(payload.get("kind") or "artist").strip().lower() or "artist"
    uid = _owner(request, payload.get("user_id"))
    return import_aliases(db, text, user_id=uid, kind=kind)


@router.get("/misses")
def resolve_misses(request: Request, db: Session = Depends(get_db),
                   user_id: str | None = None, limit: int = 50):
    """Нерасшифрованные промахи — видно, где мозг спотыкается."""
    from app.services.query_learning import open_misses

    uid = _owner(request, user_id)
    return {"ok": True, "misses": open_misses(db, limit=limit)}
