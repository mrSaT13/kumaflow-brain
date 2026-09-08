from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.queue import enqueue
from app.workers.tasks import noop as _placeholder

router = APIRouter()


@router.post("/enrich")
async def enrich(db: Session = Depends(get_db)):
    return {"queued": True, "job_id": enqueue(_placeholder)}
