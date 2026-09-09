"""Настройки ИИ, хранимые в БД (редактируются из веб-UI).

Приоритет: значения из БД (AppSetting.ai) перекрывают env-переменные.
Так провайдера/модель/ключи можно менять без перезапуска и без правок compose.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AppSetting


_DEFAULTS: dict[str, Any] = {
    "provider": "",
    "model": "",
    "openai_api_key": "",
    "openai_server_url": "",
    "ollama_server_url": "",
    "ollama_cloud_api_key": "",
    "gemini_api_key": "",
    "mistral_api_key": "",
}


def get_ai_config(db: Session) -> dict[str, Any]:
    """Сохранённые в БД настройки ИИ (пусто — значит используются env)."""
    row = db.get(AppSetting, "ai")
    saved = dict(row.value) if row and isinstance(row.value, dict) else {}
    out = dict(_DEFAULTS)
    for k in out:
        v = saved.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def save_ai_config(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    value = {k: str(payload.get(k) or "").strip() for k in _DEFAULTS}
    # provider нормализуем к верхнему регистру
    value["provider"] = value["provider"].upper()
    row = db.get(AppSetting, "ai")
    if row is None:
        row = AppSetting(key="ai", value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return value


def effective_ai() -> dict[str, Any]:
    """Итоговый конфиг ИИ: БД поверх env. Не падает без БД (fallback на env)."""
    s = get_settings()
    eff = {
        "provider": (s.ai_provider or "NONE").upper(),
        "model": "",
        "openai_api_key": s.openai_api_key or "",
        "openai_server_url": s.openai_server_url or "",
        "ollama_server_url": s.ollama_server_url or "",
        "ollama_model": s.ollama_model_name or "",
        "ollama_cloud_api_key": s.ollama_cloud_api_key or "",
        "ollama_cloud_model": s.ollama_cloud_model or "",
        "gemini_api_key": s.gemini_api_key or "",
        "mistral_api_key": s.mistral_api_key or "",
    }
    try:
        from app.db.database import session_scope

        with session_scope() as db:
            saved = get_ai_config(db)
        if saved.get("provider"):
            eff["provider"] = saved["provider"]
        if saved.get("model"):
            # явная модель из UI перекрывает модель провайдера по умолчанию
            eff["model"] = saved["model"]
            eff["ollama_model"] = saved["model"]
            eff["ollama_cloud_model"] = saved["model"]
        for k in ("openai_api_key", "openai_server_url", "ollama_server_url",
                  "ollama_cloud_api_key", "gemini_api_key", "mistral_api_key"):
            if saved.get(k):
                eff[k] = saved[k]
        if not eff["model"]:
            eff["model"] = (
                eff["ollama_cloud_model"] if eff["provider"] == "OLLAMA_CLOUD"
                else eff["ollama_model"] if eff["provider"] == "OLLAMA"
                else ""
            )
    except Exception:
        pass
    return eff
