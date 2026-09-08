from fastapi import APIRouter
from app.core.config import get_settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict:
    s = get_settings()
    return {"name": s.app_name, "version": s.app_version, "env": s.env}
