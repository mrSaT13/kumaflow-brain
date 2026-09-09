from fastapi import APIRouter
from app.core.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/version")
def version() -> dict:
    s = get_settings()
    return {"name": s.app_name, "version": s.app_version, "env": s.env}
