from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter()


def _to_dict(n) -> dict:
    return {
        "id": str(n.id),
        "user_id": str(n.user_id) if n.user_id else None,
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "read_at": n.read_at.isoformat() if n.read_at else None,
    }


def _scope(db, user_id: str | None):
    """Глобальные + свои (чужие не отдаём)."""
    from app.db.models import Notification

    q = db.query(Notification)
    if user_id:
        try:
            uuid.UUID(str(user_id))
        except ValueError:
            raise HTTPException(400, "invalid user_id")
        q = q.filter(or_(Notification.user_id.is_(None),
                         Notification.user_id == str(user_id)))
    else:
        q = q.filter(Notification.user_id.is_(None))
    return q


@router.get("/")
def list_notifications(user_id: str | None = None, limit: int = 50,
                       db: Session = Depends(get_db)):
    """Свежие первые, непрочитанные выше (сортировка в вебе по read_at)."""
    from app.db.models import Notification

    q = _scope(db, user_id).order_by(Notification.created_at.desc())
    rows = q.limit(max(1, min(200, int(limit or 50)))).all()
    return {"notifications": [_to_dict(n) for n in rows]}


@router.get("/unread-count")
def unread_count(user_id: str | None = None, db: Session = Depends(get_db)):
    from app.db.models import Notification

    n = _scope(db, user_id).filter(Notification.read_at.is_(None)).count()
    return {"unread": n}


@router.post("/{note_id}/read")
def mark_read(note_id: str, db: Session = Depends(get_db)):
    from app.db.models import Notification

    try:
        uuid.UUID(note_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    n = db.get(Notification, note_id)
    if not n:
        raise HTTPException(404, "not found")
    n.read_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(user_id: str | None = None, db: Session = Depends(get_db)):
    from app.db.models import Notification

    n = _scope(db, user_id).filter(Notification.read_at.is_(None)) \
        .update({"read_at": datetime.utcnow()}, synchronize_session=False)
    db.commit()
    return {"ok": True, "marked": n}
