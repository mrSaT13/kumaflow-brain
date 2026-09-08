"""Анализ текста песни через AI → набор настроений + теги + sentiment."""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.logging import get_logger
from app.services import ai

logger = get_logger("lyrics_ai")

_SYSTEM = (
    "Ты — музыкальный аналитик. Тебе дают текст песни. "
    "Ответь СТРОГО в JSON без пояснений и без markdown-блоков:\n"
    '{"sentiment": "positive|neutral|negative",'
    ' "valence": 0.0-1.0,'
    ' "arousal": 0.0-1.0,'
    ' "energy": 0.0-1.0,'
    ' "moods": ["3-5 short mood words in russian"],'
    ' "themes": ["2-4 short theme words in russian"],'
    ' "language": "ru|en|..."}'
)


def analyze(text: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    # обрежем до ~4000 символов — модели хватит
    snippet = text.strip()[:4000]
    try:
        raw = ai.chat(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Текст песни:\n{snippet}"},
            ],
            temperature=0.2,
            max_tokens=400,
        )
    except ai.AIUnavailable as e:
        logger.warning("AI unavailable: {}", e)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analyze failed: {}", e)
        return None

    return _parse(raw)


def _parse(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    # вырезаем json из возможной обёртки ```json ... ```
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    candidate = m.group(0) if m else raw
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        logger.warning("AI returned non-JSON: {}", raw[:200])
        return None
    if not isinstance(data, dict):
        return None

    out: dict[str, Any] = {}
    for k in ("sentiment", "language"):
        v = data.get(k)
        if isinstance(v, str):
            out[k] = v.strip().lower()
    for k in ("valence", "arousal", "energy"):
        v = data.get(k)
        try:
            if v is not None:
                out[k] = float(v)
        except (TypeError, ValueError):
            pass
    for k in ("moods", "themes"):
        v = data.get(k)
        if isinstance(v, list):
            out[k] = [str(x).strip().lower() for x in v if str(x).strip()][:8]
    return out
