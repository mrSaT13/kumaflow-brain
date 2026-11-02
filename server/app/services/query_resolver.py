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
_lib_cache: dict[str, Any] = {"ts": 0.0, "index": {}}

# Слова-паразиты ассистента: ASR их шлёт вместе с названием («включи лусе
# юрселф»), и без чистки они ломают сравнение слово к слову.
FILLER_WORDS = frozenset({
    "включи", "поставь", "запусти", "послушай", "зацени", "накинь",
    "давай", "ну", "пожалуйста", "эй", "привет", "трек", "песню",
    "композицию", "play", "please",
})


def _word_score(qt: str, ntw: str) -> float:
    """Оценка одного слова запроса против одного слова названия (0..1)."""
    if qt == ntw:
        return SCORE_WORD
    qf, nf = phonetic_key(qt), phonetic_key(ntw)
    if qf and qf == nf:
        return SCORE_PHONETIC
    # утратив пробел: «сэлф» внутри «yourself»
    if qf and len(qf) >= 3 and len(qf) < len(nf) and qf in nf:
        return 0.85
    sim = similarity(qt, ntw)
    if sim >= 0.85:
        return 0.60 + 0.35 * sim
    if sim >= 0.70:
        return 0.45 + 0.30 * sim
    return 0.0


def _tokens_score(query: str, name: str) -> tuple[float, str]:
    """Слово к слову: квадрант запроса против квадранта названия.

    ASR теряет и добавляет пробелы («луз юр сэлф», «включи лусе юрселф»),
    поэтому целую фразу такому запросу не спасти. Сравниваем каждое слово
    запроса с каждым словом названия (после транслита), паразитов — выкидываем.
    """
    qwords = [tok for tok in query.split()
              if tok not in FILLER_WORDS and len(tok) >= 3]
    nwords = [word for word in name.split() if word]
    if not qwords or not nwords:
        return 0.0, ""
    total = 0.0
    for tok in qwords:
        tt = translit_ru_en(tok)
        best = 0.0
        for word in nwords:
            best = max(best, _word_score(tt, translit_ru_en(word)))
        total += best
    avg = total / len(qwords)
    if avg >= 0.70:
        return round(avg, 4), ("совпадение по словам" if avg >= 0.85
                               else "слова похожи")
    return 0.0, ""


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

    # ASR редко шлёт название ровно: «включи лусе юрселф» — меж морфема затесался
    # глагол, и пробелы потерялись («луз юр сэлф»). Сравниваем слово к слову:
    # в каком запросе — титульные слова, и наоборот.
    score, why = _tokens_score(q, nt)
    if score:
        return score, why

    return 0.0, ""


def _library_index(db) -> dict[str, dict[str, dict[str, str]]]:
    """Индекс библиотеки по видам: артист / трек / альбом.

    kind → {norm_имя: {"name": название, "id": external_id, "sub": исполнитель}}

    Артисты берём и из таблицы artists, и из artist_name треков: часть
    библиотек отдаёт артистов только через треки. Для треков и альбомов
    храним исполнителя отдельно — запрос вида «Eminem Lose Yourself»
    должен находиться по названию + исполнителю.
    """
    now = time.time()
    if now - float(_lib_cache.get("ts") or 0.0) < _LIB_CACHE_TTL:
        return _lib_cache["index"]
    index: dict[str, dict[str, dict[str, str]]] = {
        "artist": {}, "track": {}, "album": {},
    }
    try:
        from app.db.models import Album, Artist, Track

        for nm, ext in db.query(Artist.name, Artist.external_id).distinct().all():
            key = norm_key(nm)
            if key and key not in index["artist"]:
                index["artist"][key] = {"name": str(nm), "id": str(ext or ""), "sub": ""}
        for nm, ext in db.query(Track.artist_name, Track.artist_external_id).distinct().all():
            key = norm_key(nm)
            if key and key not in index["artist"]:
                index["artist"][key] = {"name": str(nm), "id": str(ext or ""), "sub": ""}
        for title, artist, ext in db.query(
                Track.title, Track.artist_name, Track.external_id).distinct().all():
            key = norm_key(title)
            if key and key not in index["track"]:
                index["track"][key] = {"name": str(title), "id": str(ext or ""),
                                       "sub": str(artist or "")}
        for title, artist, ext in db.query(
                Album.title, Album.artist_name, Album.external_id).distinct().all():
            key = norm_key(title)
            if key and key not in index["album"]:
                index["album"][key] = {"name": str(title), "id": str(ext or ""),
                                       "sub": str(artist or "")}
    except Exception as e:
        logger.warning("не удалось собрать индекс библиотеки: {}", e)
        return _lib_cache.get("index") or index
    _lib_cache["ts"] = now
    _lib_cache["index"] = index
    return index


def _score_candidate(query: str, kind: str, entry: dict[str, str]) -> tuple[float, str]:
    """Оценка кандидата с учётом вида: трек/альбом ищутся и по названию,
    и по паре «название + исполнитель»."""
    score, why = score_pair(query, entry.get("name") or "")
    sub = (entry.get("sub") or "").strip()
    if sub:
        full = f"{entry.get('name') or ''} {sub}"
        score2, why2 = score_pair(query, full)
        if score2 > score:
            return min(score2, 0.97), ("название с исполнителем" if why2 == "точное совпадение" else why2)
    return score, why


def _display(kind: str, entry: dict[str, str]) -> str:
    """Как показывать кандидата человеку: «Lose Yourself — Eminem»."""
    name = entry.get("name") or ""
    sub = (entry.get("sub") or "").strip()
    if kind != "artist" and sub and sub.lower() != name.lower():
        return f"{name} — {sub}"
    return name


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
            # У алиасов название и «чистое» имя совпадают; sub у них нет.
            "title": row.target_name,
            "external_id": row.target_external_id or "",
            "score": 1.0,
            "why": why,
            "kind": row.kind or kind,
            "source": "alias",
            "sub": "",
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

    kinds = [kind] if kind in ("artist", "track", "album") else ["artist", "track", "album"]

    for one_kind in kinds:
        alias = lookup_alias(db, raw, user_id=user_id, kind=one_kind)
        if alias:
            alias = dict(alias)
            alias["kind"] = one_kind
            cands.append(alias)
            seen.add(norm_key(alias["name"]))

    index = _library_index(db)
    for one_kind in kinds:
        for key, entry in index.get(one_kind, {}).items():
            if key in seen:
                continue
            sc, why = _score_candidate(raw, one_kind, entry)
            if sc <= 0.0:
                continue
            cands.append({
                "name": _display(one_kind, entry),
                # Чистое название без «— Исполнитель»: клиентам, которые потом
                # ищут по имени в каталоге, нужно именно оно.
                "title": entry.get("name") or "",
                "external_id": entry.get("id", ""),
                "score": round(sc, 4),
                "why": why,
                "kind": one_kind,
                "source": "library",
                "sub": (entry.get("sub") or "").strip(),
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
            "title": disp,
            "external_id": "",
            "score": round(sc, 4),
            "why": why,
            "kind": kind,
            "source": "external",
            "sub": "",
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
