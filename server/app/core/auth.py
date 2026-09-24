"""Bearer-авторизация мозг <-> плеер.

Источники токена (порядок):
1. BRAIN_API_TOKEN из env (legacy) — полный доступ, как раньше.
2. Токены из БД (создаются в веб-UI Настройки → Токены, per-user + скоупы).

Если ни env-токена, ни DB-токенов нет — auth выключен (доверенная LAN).
Если хоть один токен существует — клиент обязан слать
`Authorization: Bearer <token>` (или `?token=` для <img> обложек).

Пароль Navidrome НЕ храним на мозге постоянно: мобила уже залогинена
в Navidrome (SubsonicService хранит логин/пароль), мозгу она отдаёт только
username для маппинга user_id + external_id треков. Vault (opt-in) остаётся
единственным местом где пароль шифруется Fernet.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

_bearer = HTTPBearer(auto_error=False)

# Пути, всегда открытые (health/docs + логин из LAN-веба; сами токены
# защищают API, а не веб-морду). tokens/meta открыт чтобы UI мог показать
# состояние; CRUD токенов требует admin (первое создание при пустой базе
# открыто — иначе из начальной LAN-настройки было бы не выйти).
OPEN_PATHS = (
    "/api/health",
    "/api/docs",
    "/api/openapi.json",
    "/api/settings/tokens/meta",
    "/api/settings/login",
    "/api/settings/whoami",
)


def _extract_token(request: Request, creds: HTTPAuthorizationCredentials | None) -> str:
    got = (creds.credentials if creds else "").strip()
    if not got:
        try:
            got = (request.query_params.get("token") or "").strip()
        except Exception:
            got = ""
    return got


def _auth_state(request: Request, creds: HTTPAuthorizationCredentials | None) -> dict | None:
    """None = auth не требуется (токенов нет) или legacy env. Иначе info токена."""
    settings = get_settings()
    path = request.url.path or ""
    for p in OPEN_PATHS:
        if path == p or path.startswith(p + "/"):
            return None
    env_token = (settings.brain_api_token or "").strip()
    got = _extract_token(request, creds)
    # 1) legacy env — полный доступ
    if env_token and got == env_token:
        return {"is_admin": True, "scopes": ["admin"], "owner_user_id": None,
                "source": "env"}
    # 2) есть ли вообще DB-токены? нет — auth выключен
    try:
        from app.db.database import session_scope
        from app.services import api_tokens as _tokens

        with session_scope() as db:
            if _tokens.count_tokens(db) == 0:
                if not env_token:
                    return None  # токенов нет — доверенная LAN
                # env задан, но прислали чужое
                raise HTTPException(401, "invalid brain token")
            info = _tokens.verify(db, got)
    except HTTPException:
        raise
    except Exception:
        return None  # БД недоступна — не валим запрос
    if info is None:
        raise HTTPException(401, "invalid brain token")
    return info


def require_brain_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    """Базовая защита: токен нужен, скоуп любой."""
    info = _auth_state(request, creds)
    if info is None:
        return None
    request.state.brain_token = info
    return None


def require_scope(*allowed: str):
    """Защита со скоупом: хотя бы один из allowed (admin проходит везде)."""

    def _dep(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ):
        info = _auth_state(request, creds)
        if info is None:
            return None
        scopes = set(info.get("scopes") or [])
        if "admin" in scopes:
            request.state.brain_token = info
            return None
        if scopes & set(allowed):
            request.state.brain_token = info
            return None
        raise HTTPException(403, f"token lacks scope (need: {'|'.join(allowed)})")

    return _dep


def require_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    """Только админ: info None (открытая LAN без токенов) или admin-скоуп.

    Обычный per-user токен (wave/sync/...) сюда не проходит — так обычный
    пользователь не может дёргать сканы, настройки, токены и чужие данные.
    """
    info = _auth_state(request, creds)
    if info is None:
        return None
    if info.get("is_admin"):
        request.state.brain_token = info
        return None
    raise HTTPException(403, "admin token required")


def enforce_user_binding(request: Request):
    """Привязка токена к юзеру: не-админ ходит только под своим user_id.

    Смотрит path user_id (users/{user_id}/*). Для body user_id (waveContinue,
    weekly-discovery) проверка внутри эндпоинтов.
    """
    info = getattr(request.state, "brain_token", None)
    if info is None or info.get("is_admin"):
        return None
    owner = info.get("owner_user_id")
    if not owner:
        return None  # токен без юзера — только скоупы
    try:
        path_uid = (request.path_params or {}).get("user_id")
    except Exception:
        path_uid = None
    if path_uid and str(path_uid) != str(owner):
        # разрешаем external_id того же юзера
        try:
            from app.db.database import session_scope
            from app.services.track_resolve import get_user as _gu

            with session_scope() as db:
                u = _gu(db, str(path_uid))
                if u is None or str(u.id) != str(owner):
                    raise HTTPException(403, "token bound to another user")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(403, "token bound to another user")
    return None


def check_body_user(info: dict | None, user_id: str | None) -> None:
    """Проверка body user_id для waveContinue/weekly-discovery."""
    if info is None or info.get("is_admin"):
        return
    owner = (info or {}).get("owner_user_id")
    if not owner or not user_id:
        return
    if str(user_id) == str(owner):
        return
    try:
        from app.db.database import session_scope
        from app.services.track_resolve import get_user as _gu

        with session_scope() as db:
            u = _gu(db, str(user_id))
            if u is not None and str(u.id) == str(owner):
                return
    except Exception:
        pass
    raise HTTPException(403, "token bound to another user")
