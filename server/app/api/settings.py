from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_db
from app.db.models import AppSetting
from app.services import ai

router = APIRouter()


@router.get("", include_in_schema=False)
@router.get("/")
def list_settings(db: Session = Depends(get_db)):
    s = get_settings()
    runtime = {
        "app_name": s.app_name,
        "version": s.app_version,
        "env": s.env,
        "media_server_type": s.media_server_type,
        "navidrome_url": s.navidrome_url,
        "navidrome_user": s.navidrome_user,
        "yandex_music_enabled": s.yandex_music_enabled,
        "bridge_enabled": s.bridge_enabled,
        "bridge_url": s.bridge_url,
        "clap_enabled": s.clap_enabled,
        "lyrics_providers": s.lyrics_providers,
        "ai_provider": s.ai_provider,
        "ai_configured": ai.is_configured(),
        "ollama_cloud_model": s.ollama_cloud_model,
        "openai_model_name": s.openai_model_name,
        "ollama_model_name": s.ollama_model_name,
        "database_url": (s.database_url.split("@")[-1] if "@" in s.database_url else s.database_url),
    }
    db_settings = {row.key: row.value for row in db.query(AppSetting).all()}
    # секрет сейфа наружу не отдаём — только факт наличия
    if isinstance(db_settings.get("taste_vault_key"), dict):
        _v = db_settings["taste_vault_key"]
        db_settings["taste_vault_key"] = {"configured": bool(_v.get("key")), "auto": bool(_v.get("auto"))}
    return {"runtime": runtime, "db": db_settings}


@router.put("/")
def upsert_setting(payload: dict, db: Session = Depends(get_db)):
    key = payload.get("key")
    value = payload.get("value")
    if not key:
        return {"ok": False, "error": "key required"}
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return {"ok": True, "key": key}


@router.post("/ai/pull")
def ai_pull(payload: dict | None = None, db: Session = Depends(get_db)):
    """Скачать модель в локальную Ollama (как на мобиле — выгрузка модели). Только для OLLAMA."""
    from app.services.ai_config import effective_ai

    eff = effective_ai()
    if (eff.get("provider") or "").upper() != "OLLAMA":
        return {"ok": False, "error": "Только для OLLAMA local (для CLOUD модель в облаке)"}
    base = (eff.get("ollama_server_url") or (payload or {}).get("ollama_server_url") or "").strip().rstrip("/")
    model = (eff.get("model") or eff.get("ollama_model") or (payload or {}).get("model") or "llama3.1").strip()
    if not base:
        return {"ok": False, "error": "Ollama URL не задан"}
    import httpx

    try:
        # Ollama pull — стримим логи, но ждём завершения
        r = httpx.post(f"{base}/api/pull", json={"name": model}, timeout=600.0)
        if r.status_code >= 400:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:500]}"}
        return {"ok": True, "model": model, "response": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/ai/test")
def ai_test(payload: dict | None = None):
    """Пинг AI-провайдера. Если передан prompt — ответит, иначе просто 'ok'."""
    if not ai.is_configured():
        return {"ok": False, "error": "AI не настроен (проверьте провайдера/ключ/модель в настройках)"}
    prompt = (payload or {}).get("prompt", "Скажи ok одним словом")
    try:
        text = ai.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=64,
        )
        return {"ok": True, "response": text}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/ai/models")
def ai_models():
    """Список моделей (если поддерживается провайдером)."""
    from app.services.ai_config import effective_ai

    eff = effective_ai()
    models = ai.available_models()
    provider = (eff.get("provider") or "NONE").upper()
    return {
        "provider": provider,
        "configured_model": eff.get("model") or "",
        "available": models,
    }


@router.get("/ai")
def get_ai(db: Session = Depends(get_db)):
    """Настройки ИИ из БД (сохранены из веб-UI) + effective-итог и статус."""
    from app.services import ai as _ai
    from app.services.ai_config import effective_ai, get_ai_config

    return {
        "saved": get_ai_config(db),
        "effective": {k: (v if "key" not in k else ("***" if v else "")) for k, v in effective_ai().items()},
        "configured": _ai.is_configured(),
    }


@router.post("/ai")
def save_ai(payload: dict, db: Session = Depends(get_db)):
    """Сохранить провайдера/модель/ключи ИИ. Без перезапуска и правок файлов."""
    from app.services.ai_config import save_ai_config

    value = save_ai_config(db, payload or {})
    return {"ok": True, "saved": {k: (v if "key" not in k else ("***" if v else "")) for k, v in value.items()}}


@router.post("/media-server")
def save_media_server(payload: dict, db: Session = Depends(get_db)):
    from app.services.media_server import save_media_server_config

    if not (payload.get("url") or "").strip():
        return {"ok": False, "error": "URL обязателен"}
    value = save_media_server_config(db, payload)
    return {"ok": True, "saved": value}


@router.get("/media-server")
def get_media_server(db: Session = Depends(get_db)):
    from app.services.media_server import get_media_server_config

    return get_media_server_config(db)


@router.post("/media-server/test")
async def test_media_server(payload: dict | None = None, db: Session = Depends(get_db)):
    """Проверить подключение к Navidrome/Subsonic. Если body пустой — берёт сохранённые настройки."""
    from app.services.media_server import get_media_server_config
    import httpx
    import hashlib, secrets

    cfg = payload if payload and payload.get("url") else get_media_server_config(db)
    url = (cfg.get("url") or "").strip().rstrip("/")
    user = (cfg.get("user") or "").strip()
    password = cfg.get("password") or ""
    token = (cfg.get("token") or "").strip()

    if not url or not user:
        return {"ok": False, "error": "Укажите URL и пользователя"}

    # пробуем ping
    try:
        # Navidrome принимает как token так и пароль; пробуем оба варианта
        salt = secrets.token_hex(6)
        tok = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
        params = {
            "u": user,
            "v": "1.16.1",
            "c": "KumaFlowBrain",
            "f": "json",
        }
        if token:
            # если передан готовый токен — используем его как t, иначе считаем из пароля
            params["t"] = token
            params["s"] = salt
        else:
            params["t"] = tok
            params["s"] = salt

        target = f"{url}/rest/ping.view"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(target, params=params)
            # Navidrome возвращает xml или json в зависимости от f
            text = r.text
            if r.status_code == 200 and ("ok" in text.lower() or "ping" in text.lower()):
                return {"ok": True, "status": r.status_code, "body": text[:500]}
            # попробуем json путь
            try:
                j = r.json()
                sr = j.get("subsonic-response", {})
                if sr.get("status") == "ok":
                    return {"ok": True, "body": j}
                return {"ok": False, "error": sr.get("error", {}).get("message", text[:500]), "status": r.status_code}
            except Exception:
                return {"ok": r.status_code == 200, "status": r.status_code, "body": text[:500]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/bridge")
def get_bridge(db: Session = Depends(get_db)):
    """Настройки моста из БД (сохранены из веб-UI) с фолбэком на env."""
    from app.services.bridge import get_bridge_config

    return get_bridge_config(db)


@router.post("/bridge")
def save_bridge(payload: dict, db: Session = Depends(get_db)):
    """Сохранить адрес моста и вкл/выкл. Ничего в файлах править не нужно."""
    from app.services.bridge import save_bridge_config

    value = save_bridge_config(db, payload or {})
    return {"ok": True, "saved": value}


@router.post("/bridge/test")
async def test_bridge(payload: dict | None = None, db: Session = Depends(get_db)):
    """Проверить мост. Если body пустой — пингует сохранённые настройки."""
    from app.services.bridge import bridge_health, get_bridge_config

    cfg = payload if payload and payload.get("url") else get_bridge_config(db)
    url = (cfg.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "Укажите URL моста"}
    if not url.startswith("http"):
        url = "http://" + url
    try:
        health = await bridge_health(url)
        return {"ok": True, "status": 200, "body": health}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/automation")
def get_automation(db: Session = Depends(get_db)):
    """Флаги автоматизации (БД, без перезапуска)."""
    from app.services import automation as _auto

    return {"ok": True, "flags": _auto.get_flags(db)}


@router.put("/automation")
def save_automation(payload: dict, db: Session = Depends(get_db)):
    """Сохранить флаги автоматизации. Принимает только известные ключи."""
    from app.services import automation as _auto

    flags = _auto.set_flags(dict(payload or {}), db)
    return {"ok": True, "flags": flags}


@router.get("/tokens/meta")
def tokens_meta(db: Session = Depends(get_db)):
    """Мета для UI: скоупы, пресеты, задан ли env-токен, сколько токенов."""
    from app.core.config import get_settings as _gs
    from app.services import api_tokens as _tokens

    return {
        "scopes": dict(_tokens.SCOPES),
        "presets": {k: {"label": v["label"], "desc": v["desc"], "scopes": v["scopes"]}
                    for k, v in _tokens.PRESETS.items()},
        "env_token_configured": bool((_gs().brain_api_token or "").strip()),
        "count": _tokens.count_tokens(db),
    }


@router.get("/tokens")
def tokens_list(owner_user_id: str | None = None, db: Session = Depends(get_db)):
    """Список токенов (без секретов). Фильтр по юзеру — для вкладки per-user."""
    from app.services import api_tokens as _tokens

    return {"tokens": _tokens.list_tokens(db, owner_user_id)}


@router.post("/tokens")
def tokens_create(payload: dict, db: Session = Depends(get_db)):
    """Создать токен. Ответ содержит plaintext ОДИН раз — показать и скопировать."""
    from app.services import api_tokens as _tokens

    name = str((payload or {}).get("name") or "mobile")
    scopes = list((payload or {}).get("scopes") or [])
    owner = (payload or {}).get("owner_user_id") or (payload or {}).get("user_id")
    try:
        res = _tokens.create_token(db, str(owner) if owner else None, name, scopes)
    except ValueError as e:
        from fastapi import HTTPException as _HE

        raise _HE(404, str(e))
    return {"ok": True, **res}


@router.patch("/tokens/{token_id}")
def tokens_toggle(token_id: str, payload: dict, db: Session = Depends(get_db)):
    from app.services import api_tokens as _tokens

    enabled = bool((payload or {}).get("enabled", True))
    res = _tokens.set_enabled(db, token_id, enabled)
    if res is None:
        from fastapi import HTTPException as _HE

        raise _HE(404, "not found")
    return {"ok": True, "token": res}


@router.delete("/tokens/{token_id}")
def tokens_delete(token_id: str, db: Session = Depends(get_db)):
    from app.services import api_tokens as _tokens

    if not _tokens.delete_token(db, token_id):
        from fastapi import HTTPException as _HE

        raise _HE(404, "not found")
    return {"ok": True}


def _subsonic_auth_params(user: str, password: str) -> dict:
    import hashlib as _hl
    import secrets as _sec

    salt = _sec.token_hex(6)
    return {
        "u": user,
        "t": _hl.md5(f"{password}{salt}".encode()).hexdigest(),
        "s": salt,
        "v": "1.16.1",
        "c": "KumaFlowBrain",
        "f": "json",
    }


async def _navidrome_check(url: str, username: str, password: str) -> dict:
    """Проверка логина/пароля в Navidrome + флаг админа.

    Возвращает {ok, is_admin, error}. Пароль используется один раз и забывается.
    Админ определяется через getUser (доступен только админам): получилось
    и adminRole true — админ, иначе обычный юзер.
    """
    import httpx

    base = (url or "").strip().rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base
    params = _subsonic_auth_params(username, password)
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(f"{base}/rest/ping.view", params=params)
            try:
                sr = r.json().get("subsonic-response", {})
            except Exception:
                return {"ok": False, "is_admin": False, "error": f"HTTP {r.status_code}"}
            if sr.get("status") != "ok":
                err = (sr.get("error") or {}).get("message") or "неверный логин или пароль"
                return {"ok": False, "is_admin": False, "error": str(err)[:200]}
            # пинг ок — пробуем getUser для флага админа
            is_admin = False
            try:
                r2 = await client.get(f"{base}/rest/getUser.view",
                                      params={**params, "username": username})
                u = r2.json().get("subsonic-response", {}).get("user", {})
                is_admin = bool(u.get("adminRole", False))
            except Exception:
                is_admin = False
            return {"ok": True, "is_admin": is_admin, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "is_admin": False, "error": str(e)[:200]}


@router.post("/login")
async def login(payload: dict, db: Session = Depends(get_db)):
    """Вход по логину/паролю Navidrome — в обмен выдаём API-токен.

    Пароль используется один раз для проверки и НЕ хранится.
    Админы Navidrome получают admin-токен, остальные — мобильный набор
    (волна + синк + обложки + плейлисты), привязанный к их юзеру.
    """
    import uuid as _uuid

    from app.db.models import MediaUser
    from app.services import api_tokens as _tokens
    from app.services.media_server import get_media_server_config

    username = str((payload or {}).get("username") or "").strip()
    password = str((payload or {}).get("password") or "")
    device = str((payload or {}).get("device") or "web").strip()[:64] or "web"
    if not username or not password:
        return {"ok": False, "error": "Укажите логин и пароль"}
    try:
        cfg = get_media_server_config(db)
    except Exception:
        cfg = {}
    url = (cfg.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "Медиа-сервер не настроен (сначала Настройки → Подключения)"}
    chk = await _navidrome_check(url, username, password)
    if not chk.get("ok"):
        return {"ok": False, "error": chk.get("error") or "Navidrome отклонил логин/пароль"}
    is_admin = bool(chk.get("is_admin"))
    # find-or-create MediaUser без импорта вкусов (быстро)
    from app.services.media_server import resolve_active_server

    server = resolve_active_server(db)
    db.commit()
    u = db.query(MediaUser).filter_by(server_id=server.id, external_id=username[:128]).first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=server.id,
                      external_id=username[:128], username=username[:128],
                      is_admin=is_admin)
        db.add(u)
        db.commit()
        db.refresh(u)
    scopes = ["admin"] if is_admin else list(_tokens.PRESETS["mobile"]["scopes"])
    created = _tokens.create_token(db, str(u.id), f"{device} · {username}"[:128], scopes)
    return {"ok": True, "token": created["token"], "user": {"id": str(u.id), "username": u.username},
            "is_admin": is_admin, "scopes": created["scopes"]}


@router.get("/whoami")
def whoami(request: Request, db: Session = Depends(get_db)):
    """Кто этот браузер: валиден ли сохранённый токен, закрыт ли API.

    Всегда 200 (без 401) — gate в вебе решает, показывать ли окно логина.
    """
    from app.core.config import get_settings as _gs
    from app.services import api_tokens as _tokens

    env_token = (_gs().brain_api_token or "").strip()
    total = _tokens.count_tokens(db)
    locked = bool(env_token) or total > 0
    got = ""
    try:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    except Exception:
        got = ""
    if got and env_token and got == env_token:
        return {"ok": True, "logged_in": True, "is_admin": True,
                "owner_user_id": None, "prefix": "env",
                "locked": True, "tokens_exist": total > 0, "env_configured": True}
    info = _tokens.verify(db, got) if got else None
    if info is None:
        return {"ok": True, "logged_in": False, "is_admin": False,
                "owner_user_id": None, "prefix": "",
                "locked": locked, "tokens_exist": total > 0,
                "env_configured": bool(env_token)}
    prefix = ""
    try:
        from app.db.models import ApiToken

        r = db.get(ApiToken, info["token_id"])
        prefix = (r.prefix or "") if r else ""
    except Exception:
        pass
    return {"ok": True, "logged_in": True, "is_admin": bool(info.get("is_admin")),
            "owner_user_id": info.get("owner_user_id"), "prefix": prefix,
            "locked": locked, "tokens_exist": total > 0,
            "env_configured": bool(env_token)}
