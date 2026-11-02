"""ИИ-подсказки для «спасателя запроса».

Обычные проверки (транслит, «звучит одинаково», похожесть написания) —
это арифметика. Они не понимают, что «пиано гайс» — это «The Piano Guys»,
а «джем» — «Jam», если в каталоге такого исполнителя нет.

Этот модуль делает вторую вещь: даёт модели список имён из твоей библиотеки
и просит сказать, что ты имел в виду. Модель не выдумывает идентификаторы —
она предлагает НАЗВАНИЯ, а мы ищем их в базе сами.

Два режима:
- suggest(): разовый запрос «что это могло быть» (из веб-морды или с клиента);
- в кроне: накопленные промахи разбираются пачкой, результат кладётся в
  таблицу ai_suggestions — юзер видит их в веб-морде и подтверждает
  («да, это оно») или отклоняет.

Модель зовётся только когда дешёвые способы не дали уверенного ответа:
лишние вызовы токенов не делаем.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.logging import get_logger
from app.services.text_tools import norm_key

logger = get_logger("query_ai")

# Сколько имён отдаём модели: контекст не резиновый, а самые частые треки
# обычно и нужны.
MAX_NAMES = 600
# Модель возвращает JSON; если она обернула его в текст — вытащим объект.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        match = _JSON_RE.search(raw or "")
        if match:
            return json.loads(match.group(0))
        return json.loads(raw or "{}")
    except Exception:
        return {}


def library_names(db, kind: str = "artist", limit: int = MAX_NAMES) -> list[str]:
    """Список имён из библиотеки для подсказки модели."""
    try:
        from sqlalchemy import func

        from app.db.models import Album, Artist, Track

        out: list[str] = []
        if kind in ("artist", "any"):
            rows = db.query(Artist.name, func.count(Artist.id)).group_by(Artist.name).all()
            out.extend(str(name) for name, _ in rows if name)
        if kind in ("track", "any"):
            rows = db.query(Track.title, func.count(Track.id)).group_by(Track.title).all()
            out.extend(f"{title}" for title, _ in rows if title)
        if kind in ("album", "any"):
            rows = db.query(Album.title, func.count(Album.id)).group_by(Album.title).all()
            out.extend(f"{title}" for title, _ in rows if title)
    except Exception as e:
        logger.warning("не удалось собрать имена для ИИ: {}", e)
        return []
    seen: set[str] = set()
    unique: list[str] = []
    for name in out:
        key = norm_key(name)
        if key and key not in seen:
            seen.add(key)
            unique.append(name)
    return unique[:limit]


def _prompt(query: str, names: list[str], kind: str) -> str:
    kind_ru = {
        "artist": "исполнителя",
        "track": "название трека",
        "album": "название альбома",
    }.get(kind, "исполнителя")
    joined = "\n".join(f"- {name}" for name in names)
    return (
        "Ты — помощник музыкального поиска. Пользователь голосом или с ошибкой "
        f"назвал {kind_ru}.\n"
        f'Что он сказал: "{query}"\n'
        "Список названий из его библиотеки (выбирай ТОЛЬКО из него):\n"
        f"{joined}\n"
        "Верни СТРОГО JSON без markdown: "
        '{"suggestions": [{"name": "название из списка", "confidence": 0.0-1.0, '
        '"why": "почему это похоже"}]}.\n'
        "Правила: до 3 вариантов, от лучшего к худшему; confidence — честная "
        "уверенность; если ничего не подходит — пустой список []; не выдумывай "
        "названий, которых нет в списке."
    )


def suggest(db, query: str, *, kind: str = "artist", top_k: int = 3,
            user_id: str | None = None) -> list[dict[str, Any]]:
    """Что модель думает, что ты сказал. Пустой список — если не настроена
    или нечего предложить."""
    raw_query = (query or "").strip()
    if not raw_query:
        return []
    try:
        from app.services.ai import AIUnavailable, chat, is_configured
    except Exception as e:
        logger.warning("ИИ-модуль недоступен: {}", e)
        return []
    if not is_configured():
        return []
    names = library_names(db, kind=kind)
    if not names:
        return []
    try:
        answer = chat(messages=[{"role": "user", "content": _prompt(raw_query, names, kind)}],
                      temperature=0.1, max_tokens=500)
    except AIUnavailable:
        return []
    except Exception as e:
        logger.warning("ИИ не ответил по запросу {!r}: {}", raw_query, e)
        return []

    data = _parse_json(answer)
    out: list[dict[str, Any]] = []
    for item in (data.get("suggestions") or [])[: max(1, min(5, int(top_k or 3)))]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            confidence = float(item.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        out.append({
            "name": name,
            "score": round(max(0.0, min(1.0, confidence)), 4),
            "why": str(item.get("why") or "").strip()[:200],
            "kind": kind,
            "source": "ai",
        })
    logger.info("ИИ предложил {} вариантов для {!r}", len(out), raw_query)
    return out


def suggest_for_misses(db, limit: int = 10) -> list[dict[str, Any]]:
    """Разобрать накопленные промахи: для каждого — спросить ИИ.

    Результат кладём в ai_suggestions, чтобы юзер подтвердил в веб-морде.
    """
    from app.db.models import AiSuggestion, QueryMiss

    try:
        misses = (db.query(QueryMiss)
                  .filter(QueryMiss.resolved.is_(False))
                  .order_by(QueryMiss.created_at.desc())
                  .limit(max(1, min(50, int(limit or 10))))
                  .all())
    except Exception as e:
        logger.warning("не удалось прочитать промахи для ИИ: {}", e)
        return []
    produced: list[dict[str, Any]] = []
    for miss in misses:
        suggestions = suggest(db, miss.query, kind=miss.kind or "artist",
                              user_id=miss.user_id)
        if not suggestions:
            continue
        try:
            db.add(AiSuggestion(
                user_id=miss.user_id,
                query=miss.query,
                kind=miss.kind or "artist",
                suggestions=suggestions,
                source="ai",
            ))
            db.flush()
        except Exception as e:
            logger.warning("не удалось сохранить предложение ИИ: {}", e)
        produced.append({"query": miss.query, "suggestions": suggestions})
    try:
        db.commit()
    except Exception:
        pass
    return produced
