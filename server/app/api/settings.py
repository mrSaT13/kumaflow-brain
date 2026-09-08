from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_db
from app.db.models import AppSetting
from app.services import ai

router = APIRouter()


@router.get("/")
async def list_settings(db: Session = Depends(get_db)):
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
    return {"runtime": runtime, "db": db_settings}


@router.put("/")
async def upsert_setting(payload: dict, db: Session = Depends(get_db)):
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


@router.post("/ai/test")
async def ai_test(payload: dict | None = None):
    """Пинг AI-провайдера. Если передан prompt — ответит, иначе просто 'ok'."""
    if not ai.is_configured():
        return {"ok": False, "error": "AI не настроен"}
    prompt = (payload or {}).get("prompt", "Скажи 'ok' одним словом")
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
async def ai_models():
    """Список моделей (если поддерживается провайдером)."""
    s = get_settings()
    models = ai.available_models()
    return {
        "provider": s.ai_provider,
        "configured_model": (
            s.ollama_cloud_model if s.ai_provider == "OLLAMA_CLOUD"
            else s.ollama_model_name if s.ai_provider == "OLLAMA"
            else s.openai_model_name if s.ai_provider == "OPENAI"
            else ""
        ),
        "available": models,
    }


@router.post("/media-server")
async def save_media_server(payload: dict, db: Session = Depends(get_db)):
    from app.services.media_server import save_media_server_config

    if not (payload.get("url") or "").strip():
        return {"ok": False, "error": "URL обязателен"}
    value = save_media_server_config(db, payload)
    return {"ok": True, "saved": value}


@router.get("/media-server")
async def get_media_server(db: Session = Depends(get_db)):
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
async def get_bridge(db: Session = Depends(get_db)):
    """Настройки моста из БД (сохранены из веб-UI) с фолбэком на env."""
    from app.services.bridge import get_bridge_config

    return get_bridge_config(db)


@router.post("/bridge")
async def save_bridge(payload: dict, db: Session = Depends(get_db)):
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
