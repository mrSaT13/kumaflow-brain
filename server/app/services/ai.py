from __future__ import annotations

import os
from typing import Iterable

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("ai")


class AIUnavailable(RuntimeError):
    pass


def _provider() -> str:
    return (get_settings().ai_provider or "NONE").upper()


def is_configured() -> bool:
    s = get_settings()
    if _provider() == "OLLAMA":
        return bool(s.ollama_server_url and s.ollama_model_name)
    if _provider() == "OLLAMA_CLOUD":
        return bool(s.ollama_cloud_api_key and s.ollama_cloud_model)
    if _provider() in ("OPENAI",):
        return bool(s.openai_api_key and s.openai_server_url and s.openai_model_name)
    if _provider() == "GEMINI":
        return bool(s.gemini_api_key)
    if _provider() == "MISTRAL":
        return bool(s.mistral_api_key)
    return False


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.4,
    max_tokens: int = 1024,
) -> str:
    """Универсальный вызов чата. Возвращает текст ответа."""
    s = get_settings()
    provider = _provider()
    if provider == "NONE":
        raise AIUnavailable("AI provider not configured (AI_PROVIDER=NONE)")

    if provider == "OLLAMA":
        return _ollama_local(s.ollama_server_url, model or s.ollama_model_name, messages, temperature, max_tokens)
    if provider == "OLLAMA_CLOUD":
        return _openai_compatible(
            "https://ollama.com/v1/chat/completions",
            s.ollama_cloud_api_key,
            model or s.ollama_cloud_model,
            messages,
            temperature,
            max_tokens,
        )
    if provider == "OPENAI":
        return _openai_compatible(
            s.openai_server_url,
            s.openai_api_key,
            model or s.openai_model_name,
            messages,
            temperature,
            max_tokens,
        )
    raise AIUnavailable(f"Provider {provider} not implemented")


def _openai_compatible(
    url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> str:
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=60.0)
    except httpx.HTTPError as e:
        raise AIUnavailable(f"network error: {e}") from e
    if r.status_code >= 400:
        raise AIUnavailable(f"{r.status_code}: {r.text[:200]}")
    data = r.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        raise AIUnavailable(f"bad response shape: {e}: {data}") from e


def _ollama_local(
    base: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> str:
    url = base.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    try:
        r = httpx.post(url, json=payload, timeout=120.0)
    except httpx.HTTPError as e:
        raise AIUnavailable(f"network error: {e}") from e
    if r.status_code >= 400:
        raise AIUnavailable(f"{r.status_code}: {r.text[:200]}")
    data = r.json()
    return (data.get("message") or {}).get("content", "").strip()


def available_models() -> list[str]:
    s = get_settings()
    provider = _provider()
    out: list[str] = []
    if provider == "OLLAMA_CLOUD":
        try:
            r = httpx.get(
                "https://ollama.com/v1/models",
                headers={"authorization": f"Bearer {s.ollama_cloud_api_key}"},
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                for m in data.get("data", []):
                    if isinstance(m, dict) and m.get("id"):
                        out.append(m["id"])
        except httpx.HTTPError as e:
            logger.warning("ollama cloud models fetch failed: {}", e)
    return out
