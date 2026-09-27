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

import hmac

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
    # 1) legacy env — полный доступ. Сравнение константно-временное.
    if env_token and got and hmac.compare_digest(got, env_token):
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
    except Exception as e:
        # Раньше здесь был `return None` — то есть при ЛЮБОЙ ошибке БД запрос
        # считался авторизованным. Кратковременный hiccup Postgres (рестарт,
        # переподключение) на пару секунд снимал авторизацию со всего API,
        # включая require_admin. Теперь, если env-токен настроен (значит auth
        # должен работать), при недоступной БД пропускаем только env-токен,
        # а остальным отвечаем 503: временная недоступность ≠ «всем можно».
        _warn_auth_degraded(e)
        if env_token:
            raise HTTPException(503, "auth temporarily unavailable (database unreachable)")
        # Без env-токена система живёт в режиме «доверенной LAN»: auth и так
        # выключен, отказывать тут не в чем.
        return None
    if info is None:
        raise HTTPException(401, "invalid brain token")
    return info


# Чтобы не засорять лог на каждый запрос, пока БД лежит.
_auth_degraded_at: float = 0.0


# Что можно делать без токена, чтобы вообще настроить систему: подключить
# медиа-сервер и выпустить ПЕРВЫЙ admin-токен. Всё остальное (сканы, крон,
# пользователи, настройки) — закрыто, пока не появится токен.
_BOOTSTRAP_EXACT: dict[str, set[str]] = {
    "/api/settings": {"GET", "PUT"},
    "/api/settings/media-server": {"GET", "POST"},
    "/api/settings/media-server/test": {"POST"},
    "/api/settings/tokens/meta": {"GET"},
    "/api/settings/tokens": {"GET", "POST"},
    # Переключатель «доверять сети» должен быть доступен и до первого токена,
    # иначе из закрытого режима не выйти. Дополнительного риска нет: выпустить
    # admin-токен (POST /api/settings/tokens) и так можно без авторизации.
    "/api/settings/security": {"GET", "PUT"},
    # whoami обязателен: по нему LoginGate решает, показывать ли окно входа.
    # Без него гейт получал 403, SWR долбил endpoint по своему retry и
    # страница выглядела «постоянно перезагружающейся», а кнопка выхода
    # ничего не делала — редиректить было не на что.
    "/api/settings/whoami": {"GET"},
    # Первоначальная настройка, иначе до выпуска токена нельзя задать
    # медиа-сервер/мост/ИИ/часовой пояс — то есть не настроить систему.
    "/api/settings/timezone": {"GET", "PUT"},
    "/api/settings/ai": {"GET", "POST"},
    "/api/settings/ai/test": {"POST"},
    "/api/settings/ai/models": {"GET"},
    "/api/settings/bridge": {"GET", "POST"},
    "/api/settings/bridge/test": {"POST"},
    # Управление уже выпущенными токенами (выключить/удалить свой).
    "/api/settings/automation": {"GET"},
}

# Префиксы: (начало пути, разрешённые методы). Нужны для путей с id в конце.
# Пока auth выключен, токенов в БД не существует по определению (иначе auth
# был бы включён), поэтому управлять ими без токена безопасно.
_BOOTSTRAP_PREFIXES: list[tuple[str, set[str]]] = [
    ("/api/settings/tokens/", {"PATCH", "DELETE"}),
]


def _is_bootstrap_path(method: str, path: str) -> bool:
    m = (method or "GET").upper()
    if path in _BOOTSTRAP_EXACT:
        return m in _BOOTSTRAP_EXACT[path]
    for prefix, methods in _BOOTSTRAP_PREFIXES:
        if path.startswith(prefix):
            return m in methods
    return False


def _lan_trust_enabled() -> bool:
    """Явно разрешён ли открытый доступ из локальной сети (AppSetting)."""
    try:
        from app.db.database import session_scope
        from app.db.models import AppSetting

        with session_scope() as db:
            row = db.get(AppSetting, "security")
            val = row.value if row and isinstance(row.value, dict) else {}
            return bool(val.get("lan_admin_enabled", False))
    except Exception:
        return False


def _warn_auth_degraded(exc: Exception) -> None:
    """Предупреждение не чаще раза в минуту."""
    global _auth_degraded_at
    import time as _time

    now = _time.time()
    if now - _auth_degraded_at < 60:
        return
    _auth_degraded_at = now
    try:
        from app.core.logging import get_logger

        get_logger("kumaflow.auth").warning(
            "БД недоступна при проверке токена, авторизация деградировала: {}", exc
        )
    except Exception:
        pass


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
    """Только админ.

    С ADMIN-ТОКЕНОМ всё как раньше: обычный per-user токен (wave/sync/...) сюда
    не проходит — так обычный пользователь не может дёргать сканы, настройки,
    токены и чужие данные. Ответ на «может ли обычный пользователь выдать себе
    права админа» — нет: смена is_admin и создание admin-токена только здесь.

    БЕЗ токенов (режим «доверенная LAN») раньше пропускал вообще всех, а значит
    любой из сети мог выполнить POST /api/settings/tokens и выдать себе
    admin-токен. Теперь по умолчанию открыт только стартовый набор путей
    (настроить медиа-сервер, выпустить первый токен, войти), а всё остальное —
    403. Старое поведение включается явно: Настройки → Диагностика →
    «Доверять локальной сети».
    """
    info = _auth_state(request, creds)
    if info is not None:
        if info.get("is_admin"):
            request.state.brain_token = info
            return None
        raise HTTPException(403, "admin token required")
    # auth выключен — решаем по пути и по явному разрешению доверия сети
    if _lan_trust_enabled():
        return None
    path = request.url.path or ""
    if not _is_bootstrap_path(request.method, path):
        raise HTTPException(
            403,
            "API работает без авторизации, поэтому административные операции закрыты. "
            "Выпустите себе admin-токен (Настройки → Токены) — после этого доступ откроется. "
            "Если доверяете сети целиком, включите «Доверять локальной сети» в Настройках → Диагностика.",
        )
    return None


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
