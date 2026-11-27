"""Провайдер OpenCode Zen: парсинг Free-моделей и чат через OpenAI-совместимый URL."""
import os

os.environ["REDIS_HOST"] = "localhost"

from types import SimpleNamespace

from app.services import ai


def _response(payload):
    return SimpleNamespace(
        status_code=200,
        json=lambda: payload,
        raise_for_status=lambda: None,
    )


def _eff():
    return {
        "provider": "OPENCODE",
        "model": "mimo-v2.6-flash-free",
        "opencode_server_url": "https://opencode.ai/zen/v1",
        "opencode_chat_url": "https://opencode.ai/inference/openai/v1/chat/completions",
        "opencode_api_key": "",
    }


def test_opencode_free_catalog_parsing(monkeypatch):
    monkeypatch.setattr(ai, "_eff", _eff)
    ai._opencode_catalog = None
    payload = {"data": [
        {"id": "mimo-v2.6-flash-free", "name": "MiMo Flash Free",
         "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}},
         "limit": {"context": 200000},
         "api": {"chat-completions": {"base_url": "https://opencode.ai/inference/openai/v1"}}},
        {"id": "claude-opus-5", "name": "Claude Opus 5",
         "cost": {"text": {"input": {"price": 5}, "output": {"price": 25}}},
         "api": {"messages": {"base_url": "https://opencode.ai/inference/anthropic/v1"}}},
        {"id": "free-zero-price", "name": "Zero price",
         "cost": {"text": {"input": "0", "output": "0"}}, "api": {}},
    ]}
    monkeypatch.setattr(ai.httpx, "get", lambda *a, **kw: _response(payload))
    free = ai.opencode_models(free_only=True)
    ids = [m["id"] for m in free]
    assert ids == ["mimo-v2.6-flash-free", "free-zero-price"]
    assert free[0]["free"] is True
    assert free[0]["chat_url"].endswith("/chat/completions")
    assert free[0]["context"] == 200000
    assert [m["id"] for m in ai.opencode_models(free_only=False)] == [
        "mimo-v2.6-flash-free", "claude-opus-5", "free-zero-price"]
    assert ai.available_models() == ids


def test_opencode_chat_uses_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setattr(ai, "_eff", _eff)
    calls = []

    def _fake(url, key, model, messages, temperature, max_tokens):
        calls.append((url, key, model, messages))
        return "ok"

    monkeypatch.setattr(ai, "_openai_compatible", _fake)
    assert ai.is_configured() is True
    assert ai.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert calls[0][0] == "https://opencode.ai/inference/openai/v1/chat/completions"
    assert calls[0][2] == "mimo-v2.6-flash-free"
