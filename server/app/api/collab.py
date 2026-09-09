from __future__ import annotations

from fastapi import APIRouter, Depends
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
def similar_users(user_id: str):
    return {"users": []}


@router.get("/recommend/{user_id}")
def recommend(user_id: str):
    return {"tracks": []}
