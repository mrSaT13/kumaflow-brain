from fastapi import APIRouter
from app.core.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/version")
def version() -> dict:
    from app.core.time import server_now, server_tz

    s = get_settings()
    now = server_now()
    try:
        off = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0
    except Exception:
        off = 0
    return {"name": s.app_name, "version": s.app_version, "env": s.env,
            "tz": str(getattr(server_tz(), "key", None) or s.app_timezone or "UTC"),
            "utc_offset_sec": off, "now": now.isoformat()}
