from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.queue import enqueue
from app.workers.tasks import cluster_build

router = APIRouter()


@router.post("/build")
async def build():
    return {"queued": True, "job_id": enqueue(cluster_build)}


@router.get("/")
async def list_clusters():
    return {"clusters": []}
