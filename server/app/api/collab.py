from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.demo import ensure_demo_server
from app.services.queue import enqueue
from app.workers.tasks import collab_build

router = APIRouter()


@router.post("/rebuild")
def rebuild():
    return {"queued": True, "job_id": enqueue(collab_build)}


@router.get("/similar-users/{user_id}")
def similar_users(user_id: str, limit: int = 5, db: Session = Depends(get_db)):
    """Похожие по вкусу пользователи (косинус по лайкам). Реально считается."""
    from app.db.models import MediaUser
    from app.services import collab as _cb

    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    if not db.get(MediaUser, user_id):
        raise HTTPException(404, "not found")
    return {"users": _cb.similar_users(db, user_id, limit=max(1, min(10, limit)))}


@router.get("/recommend/{user_id}")
def recommend(user_id: str, n: int = 30, db: Session = Depends(get_db)):
    """Коллаборативные рекомендации: лайкнули похожие — нет у тебя.

    Работает уже на 2+ пользователях с пересекающимися лайками.
    """
    from app.db.models import MediaUser
    from app.services import collab as _cb

    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    if not db.get(MediaUser, user_id):
        raise HTTPException(404, "not found")
    return _cb.recommend_for_user(db, user_id, n=max(1, min(100, n)))
