from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.db.models import MediaUser, MediaServer
from app.services.demo import ensure_demo_server
from app.services.queue import enqueue
from app.workers.tasks import collab_build

router = APIRouter()


class UserIn(BaseModel):
    external_id: str
    username: str
    is_admin: bool = False


class UserPatch(BaseModel):
    username: str | None = None
    is_admin: bool | None = None


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
async def list_users(db: Session = Depends(get_db)):
    rows = db.query(MediaUser).order_by(MediaUser.username.asc()).all()
    return {"users": [_to_dict(u) for u in rows]}


@router.post("", include_in_schema=False)
@router.post("/")
async def create_user(payload: UserIn, db: Session = Depends(get_db)):
    server = ensure_demo_server(db)
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
async def update_user(user_id: str, payload: UserPatch, db: Session = Depends(get_db)):
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
async def delete_user(user_id: str, db: Session = Depends(get_db)):
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
async def sync_users(db: Session = Depends(get_db)):
    server = ensure_demo_server(db)
    db.commit()
    job_id = enqueue(collab_build)
    return {"queued": True, "job_id": job_id}
