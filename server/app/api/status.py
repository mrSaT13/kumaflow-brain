from fastapi import APIRouter
from app.core.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/version")
def version() -> dict:
    from app.core.time import resolve_tz_name, server_now, server_tz

    s = get_settings()
    now = server_now()
    try:
        off = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0
    except Exception:
        off = 0
    try:
        tz = server_tz()
        key = getattr(tz, "key", None) or resolve_tz_name()
    except Exception:
        key = resolve_tz_name()
    return {"name": s.app_name, "version": s.app_version, "env": s.env,
            "tz": str(key), "utc_offset_sec": off, "now": now.isoformat()}


@router.get("/updates")
def updates() -> dict:
    """О программе: своя версия + чейнджлог из CHANGELOG.md."""
    from app.services import updates as _up

    return {"ok": True, "current": _up.current_version(),
            "changelog": _up.local_changelog()}


@router.post("/updates/check")
def updates_check() -> dict:
    """Ручная проверка обновлений с GitHub (кнопка в О программе).

    Та же проверка, что ежедневный крон: есть новее — уведомит
    в колокольчик один раз на версию (дедуп по AppSetting).
    """
    from app.db.database import session_scope
    from app.services import updates as _up

    with session_scope() as db:
        res = _up.check_and_notify(db)
    out = {"ok": True, **res}
    if res.get("latest") is None:
        out["note"] = "GitHub недоступен — попробуй позже"
    return out


@router.get("/debug/pool")
def debug_pool() -> dict:
    """Живое состояние пула БД: кто держит слоты. Открыт как /health —
    счётчики не чувствительны, а при QueuePool timeout это первый вопрос.
    """
    out: dict = {"ok": False}
    try:
        from app.db.database import engine

        pool = engine.pool
        try:
            out["checkedout"] = pool.checkedout()
        except Exception:
            out["checkedout"] = None
        try:
            out["checkedin"] = pool.checkedin()
        except Exception:
            out["checkedin"] = None
        try:
            out["overflow"] = pool.overflow()
        except Exception:
            out["overflow"] = None
        try:
            out["size"] = pool.size()
        except Exception:
            out["size"] = None
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
    return out
