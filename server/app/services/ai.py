from __future__ import annotations

import httpx

from app.core.logging import get_logger

logger = get_logger("ai")


class AIUnavailable(RuntimeError):
    pass


def _eff() -> dict:
    from app.services import ai_config as _cfg

    return _cfg.effective_ai()


def _provider() -> str:
    return (_eff().get("provider") or "NONE").upper()


def _cloud_base(e: dict) -> str:
    """Базовый URL для Ollama Cloud. Важно: дефолт env — http://localhost:11434,
    он НЕ должен утекать в CLOUD-ветку (иначе Connection refused в docker)."""
    base = (e.get("ollama_server_url") or "").strip().rstrip("/")
    if not base or "localhost" in base or "127.0.0.1" in base or base.startswith("http://host.docker.internal"):
        return "https://ollama.com"
    return base


def is_configured() -> bool:
    e = _eff()
    p = (e.get("provider") or "NONE").upper()
    if p == "OLLAMA":
        return bool(e.get("ollama_server_url") and (e.get("model") or e.get("ollama_model")))
    if p == "OLLAMA_CLOUD":
        return bool(e.get("ollama_cloud_api_key") and (e.get("model") or e.get("ollama_cloud_model")))
    if p == "OPENAI":
        return bool(e.get("openai_api_key"))
    if p == "GEMINI":
        return bool(e.get("gemini_api_key"))
    if p == "MISTRAL":
        return bool(e.get("mistral_api_key"))
    return False


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.4,
    max_tokens: int = 1024,
) -> str:
    """Универсальный вызов чата. Возвращает текст ответа."""
    e = _eff()
    provider = (e.get("provider") or "NONE").upper()
    if provider == "NONE":
        raise AIUnavailable("AI provider not configured (AI_PROVIDER=NONE)")

    if provider == "OLLAMA":
        return _ollama_local(e.get("ollama_server_url") or "", model or e.get("model") or e.get("ollama_model") or "", messages, temperature, max_tokens)
    if provider == "OLLAMA_CLOUD":
        # как на мобиле: https://ollama.com/api/chat с Bearer, options num_predict
        return _ollama_local(
            _cloud_base(e),
            model or e.get("model") or e.get("ollama_cloud_model") or "gpt-oss:20b",
            messages,
            temperature,
            max_tokens,
            api_key=e.get("ollama_cloud_api_key") or "",
        )
    if provider == "OPENAI":
        return _openai_compatible(
            e.get("openai_server_url") or "https://api.openai.com/v1/chat/completions",
            e.get("openai_api_key") or "",
            model or e.get("model") or "gpt-4o-mini",
            messages,
            temperature,
            max_tokens,
        )
    if provider == "GEMINI":
        # Gemini имеет OpenAI-совместимый эндпоинт; ключ тот же.
        return _openai_compatible(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            e.get("gemini_api_key") or "",
            model or e.get("model") or "gemini-2.0-flash",
            messages,
            temperature,
            max_tokens,
        )
    if provider == "MISTRAL":
        return _openai_compatible(
            "https://api.mistral.ai/v1/chat/completions",
            e.get("mistral_api_key") or "",
            model or e.get("model") or "mistral-small-latest",
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
    api_key: str = "",
) -> str:
    base = (base or "http://localhost:11434").rstrip("/")
    url = base + "/api/chat"
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key.strip()}"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=120.0)
    except httpx.HTTPError as e:
        raise AIUnavailable(f"network error: {e}") from e
    if r.status_code == 401:
        raise AIUnavailable(f"401 Unauthorized — проверьте токен/baseUrl ({base})")
    if r.status_code >= 400:
        # fallback на /api/generate как на мобиле (только для local без токена пробуем)
        if not api_key:
            try:
                gen_url = base + "/api/generate"
                gen_payload = {"model": model, "prompt": "\n".join(m.get("content","") for m in messages), "stream": False, "options": {"temperature": temperature, "num_predict": max_tokens}}
                rg = httpx.post(gen_url, headers=headers, json=gen_payload, timeout=120.0)
                if rg.status_code == 200:
                    return (rg.json().get("response") or "").strip()
            except Exception:
                pass
        raise AIUnavailable(f"{r.status_code}: {r.text[:300]}")
    data = r.json()
    # /api/chat -> message.content, /api/generate -> response
    if "message" in data:
        return (data.get("message") or {}).get("content", "").strip()
    return (data.get("response") or "").strip()


def available_models() -> list[str]:
    e = _eff()
    provider = (e.get("provider") or "NONE").upper()
    out: list[str] = []
    if provider == "OLLAMA_CLOUD":
        # как на мобиле: /api/tags с Bearer
        bases = [_cloud_base(e)]
        for b in bases:
            try:
                base = b.rstrip("/")
                r = httpx.get(
                    f"{base}/api/tags",
                    headers={"authorization": f"Bearer {e.get('ollama_cloud_api_key')}"} if e.get("ollama_cloud_api_key") else {},
                    timeout=30,
                )
                if r.status_code == 200:
                    data = r.json()
                    for m in data.get("models", []) or []:
                        name = m.get("name") if isinstance(m, dict) else None
                        if name:
                            out.append(name)
                    if out:
                        break
                # fallback OpenAI-compat
                r2 = httpx.get(
                    "https://ollama.com/v1/models",
                    headers={"authorization": f"Bearer {e.get('ollama_cloud_api_key')}"},
                    timeout=30,
                )
                if r2.status_code == 200:
                    data = r2.json()
                    for m in data.get("data", []):
                        if isinstance(m, dict) and m.get("id"):
                            out.append(m["id"])
                    if out:
                        break
            except httpx.HTTPError as exc:
                logger.warning("ollama cloud models fetch failed: {}", exc)
    elif provider == "OLLAMA":
        # локальная Ollama: список установленных моделей
        try:
            base = (e.get("ollama_server_url") or "").rstrip("/")
            r = httpx.get(f"{base}/api/tags", timeout=15)
            if r.status_code == 200:
                for m in r.json().get("models", []) or []:
                    name = m.get("name") if isinstance(m, dict) else None
                    if name:
                        out.append(name)
        except httpx.HTTPError as exc:
            logger.warning("ollama local models fetch failed: {}", exc)
    elif provider == "OPENAI":
        # OpenAI-совместимый /models (работает и для многих провайдеров)
        try:
            base = (e.get("openai_server_url") or "").rsplit("/chat/completions", 1)[0]
            r = httpx.get(
                f"{base}/models",
                headers={"authorization": f"Bearer {e.get('openai_api_key')}"},
                timeout=15,
            )
            if r.status_code == 200:
                for m in r.json().get("data", []) or []:
                    mid = m.get("id") if isinstance(m, dict) else None
                    if mid:
                        out.append(mid)
        except httpx.HTTPError as exc:
            logger.warning("openai models fetch failed: {}", exc)
    return out
