"""«Спасатель запроса»: превращает кривую распознанную строку в кандидатов.

Сценарий: голос (ASR) выдал «Эминем» кириллицей, а в каталоге артист —
«Eminem» латиницей. Обычный поиск ищет буквально и возвращает пустоту.

Как ищем (по убыванию уверенности):
  1) точное совпадение;
  2) кириллица → латиница («Эминем» → «Eminem»);
  3) «звуковой» ключ (гласные убраны: «eminem»/«еминем» → «mnm»);
  4) похожесть написания (Левенштейн) — опечатки и оговорения голоса;
  5) совпадение по отдельному слову запроса («включи eminem»);
  6) выученные алиасы (query_aliases) — то, что мозг уже понял сам.

Источники кандидатов: библиотека (artists + tracks) и, по желанию, внешний
список (Яндекс.Музыка) — он приходит снаружи как extra_names.

Модуль ничего не знает про колонки и конкретных клиентов: это просто
«исправь строку и дай варианты».
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from app.core.logging import get_logger
from app.services.text_tools import (
    levenshtein,
    norm_key,
    phonetic_key,
    similarity,
    translit_ru_en,
)

logger = get_logger("query_resolver")

# Пороги уверенности.
SCORE_EXACT = 1.0
SCORE_TRANSLIT = 0.95
SCORE_PHONETIC = 0.90
SCORE_WORD = 0.80
GOOD_ENOUGH = 0.85
# Насколько похожее написание слова ещё считаем «тем же самым» при поиске
# выученного алиаса: голос пишет одно слово по-разному.
ALIAS_FUZZY_MIN = 0.88

# Кэш имён библиотеки: пересобирать на каждый запрос — лишняя работа,
# библиотека меняется редко.
_LIB_CACHE_TTL = 120.0
_lib_cache: dict[str, Any] = {"ts": 0.0, "names": {}, "ids": {}}


def score_pair(query: str | None, name: str | None) -> tuple[float, str]:
    """Оценка «похожести» запроса на имя. Возвращает (score, причина).

    score >= GOOD_ENOUGH — можно принимать без уточнений.
    """
    q = norm_key(query)
    n = norm_key(name)
    if not q or not n:
        return 0.0, ""

    if q == n:
        return SCORE_EXACT, "точное совпадение"

    qt, nt = translit_ru_en(q), translit_ru_en(n)
    if qt and qt == nt:
        return SCORE_TRANSLIT, "транслитерация"

    pq, pn = phonetic_key(q), phonetic_key(n)
    if pq and pq == pn:
        return SCORE_PHONETIC, "звучит одинаково"

    # Похожесть написания — по транслиту (чтобы «еминем» и «eminem» были рядом).
    sim = similarity(qt, nt)
    if sim >= 0.85:
        return 0.60 + 0.35 * sim, "похоже по написанию"
    if sim >= 0.70:
        return 0.45 + 0.30 * sim, "похоже по написанию"

    # Многословный запрос: «включи eminem» — нужное слово внутри.
    if " " in q:
        for tok in q.split():
            if len(tok) < 3:
                continue
            tt = translit_ru_en(tok)
            if tt == nt or (tt and tt == translit_ru_en(tok) and phonetic_key(tok) == pn):
                return SCORE_WORD, "совпадение по слову"
            if tt and nt.startswith(tt) and len(tt) >= 4:
                return SCORE_WORD - 0.05, "совпадение по слову"

    return 0.0, ""


def _library_names(db) -> tuple[dict[str, str], dict[str, str]]:
    """(имя → external_id, имя → '') для артистов библиотеки.

    Берём и таблицу artists, и artist_name треков: часть библиотек
    отдаёт артистов только через треки.
    """
    now = time.time()
    if now - float(_lib_cache.get("ts") or 0.0) < _LIB_CACHE_TTL:
        return _lib_cache["names"], _lib_cache["ids"]
    names: dict[str, str] = {}
    ids: dict[str, str] = {}
    try:
        from sqlalchemy import func

        from app.db.models import Artist, Track

        for nm, ext in db.query(Artist.name, Artist.external_id).distinct().all():
            key = norm_key(nm)
            if key and key not in names:
                names[key] = nm
                if ext:
                    ids[key] = str(ext)
        for nm, ext in db.query(Track.artist_name, Track.artist_external_id).distinct().all():
            key = norm_key(nm)
            if key and key not in names:
                names[key] = nm
                if ext:
                    ids[key] = str(ext)
    except Exception as e:
        logger.warning("не удалось собрать имена артистов: {}", e)
        return _lib_cache["names"], _lib_cache["ids"]
    _lib_cache["ts"] = now
    _lib_cache["names"] = names
    _lib_cache["ids"] = ids
    return names, ids


def invalidate_cache() -> None:
    """Сбросить кэш имён (после скана библиотеки)."""
    _lib_cache["ts"] = 0.0


def lookup_alias(db, query: str, user_id: str | None = None,
                 kind: str = "artist") -> dict[str, Any] | None:
    """Выученный алиас: «эминем» → «Eminem». user_id=None — общие алиасы.

    Сначала ищем точное совпадение. Если его нет — берём ближайший похожий
    алиас: голос пишет одно и то же слово по-разному («имейджен драгонс» и
    «имайджен драгонс»), и учить каждое написание отдельно не нужно.
    """
    key = norm_key(query)
    if not key:
        return None
    try:
        from app.db.models import QueryAlias

        q = db.query(QueryAlias)
        if user_id:
            rows = q.filter((QueryAlias.user_id == user_id)
                            | (QueryAlias.user_id.is_(None))).all()
        else:
            rows = q.filter(QueryAlias.user_id.is_(None)).all()
        exact = None
        best: tuple[float, Any] | None = None
        for row in rows:
            if kind and row.kind and row.kind != kind:
                continue
            if norm_key(row.alias) == key:
                exact = row
                break
            score, _ = score_pair(query, row.alias)
            if score >= ALIAS_FUZZY_MIN and (best is None or score > best[0]):
                best = (score, row)
        row = exact
        why = "выучено"
        if row is None and best is not None:
            row = best[1]
            why = "выучено (похожее написание)"
        if row is None:
            return None
        return {
            "name": row.target_name,
            "external_id": row.target_external_id or "",
            "score": 1.0,
            "why": why,
            "kind": row.kind or kind,
            "source": "alias",
            "learned_from": row.source or "",
        }
    except Exception as e:
        logger.warning("не удалось прочитать алиасы: {}", e)
    return None


def resolve(query: str | None, db, *, user_id: str | None = None,
            kind: str = "artist", top_k: int = 5,
            extra_names: Iterable[str] | None = None) -> dict[str, Any]:
    """Основная точка входа: строка → кандидаты.

    Возвращает:
      {"ok": True, "query": ..., "best": {...} | None, "candidates": [...],
       "reason": "..."}
    """
    raw = (query or "").strip()
    top_k = max(1, min(20, int(top_k or 5)))
    if not raw:
        return {"ok": True, "query": "", "best": None, "candidates": [], "reason": "пусто"}

    cands: list[dict[str, Any]] = []
    seen: set[str] = set()

    alias = lookup_alias(db, raw, user_id=user_id, kind=kind)
    if alias:
        cands.append(alias)
        seen.add(norm_key(alias["name"]))

    names, ids = _library_names(db)
    for key, disp in names.items():
        if key in seen:
            continue
        sc, why = score_pair(raw, disp)
        if sc <= 0.0:
            continue
        cands.append({
            "name": disp,
            "external_id": ids.get(key, ""),
            "score": round(sc, 4),
            "why": why,
            "kind": kind,
            "source": "library",
        })
        seen.add(key)

    for extra in (extra_names or []):
        disp = str(extra or "").strip()
        if not disp:
            continue
        key = norm_key(disp)
        if key in seen:
            continue
        sc, why = score_pair(raw, disp)
        if sc <= 0.0:
            continue
        cands.append({
            "name": disp,
            "external_id": "",
            "score": round(sc, 4),
            "why": why,
            "kind": kind,
            "source": "external",
        })
        seen.add(key)

    cands.sort(key=lambda c: (-float(c.get("score") or 0.0), len(str(c.get("name") or ""))))
    cands = cands[:top_k]
    best = cands[0] if cands else None
    reason = "нашлось" if best else "ничего не нашлось"
    if best and float(best.get("score") or 0.0) < GOOD_ENOUGH:
        reason = "нашлось, но неуверенно"
    return {
        "ok": True,
        "query": raw,
        "best": best,
        "candidates": cands,
        "reason": reason,
    }


def is_confident(result: dict[str, Any]) -> bool:
    """True, если найденный кандидат достаточно уверенный, чтобы взять его."""
    best = (result or {}).get("best") or {}
    try:
        return float(best.get("score") or 0.0) >= GOOD_ENOUGH
    except Exception:
        return False


def near_misses(query: str | None, names: Iterable[str], limit: int = 3) -> list[str]:
    """Имена, которые «почти» подошли — показываем как подсказки."""
    out: list[tuple[float, str]] = []
    for nm in names or []:
        sc, _ = score_pair(query, nm)
        if sc > 0:
            out.append((sc, str(nm)))
    out.sort(key=lambda x: -x[0])
    return [n for _, n in out[:limit]]


__all__ = [
    "GOOD_ENOUGH",
    "invalidate_cache",
    "is_confident",
    "levenshtein",
    "lookup_alias",
    "near_misses",
    "resolve",
    "score_pair",
]
