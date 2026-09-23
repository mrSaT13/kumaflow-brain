"""Нормализация имён артистов для банов/дизлайков.

Проблема: в библиотеке артисты часто составные
(«Dark Polo Gang/GASHI/Capo Plaza», «Yxng Bane/Fredo»),
а в ArtistBan лежит то, что юзер забанил в UI — один токен
(«GASHI») или строка целиком. Точное сравнение `in` пропускает
такие треки мимо фильтра. Плюс расхождения регистра/пробелов.

Правило: забанен, если нормализованный бан совпадает с полным
именем ИЛИ с любым токеном (по разделителям / & , ; + |,
feat/ft-части отрезаем). Сравнение — lowercase + strip.
"""
from __future__ import annotations

import re

_SEP_RE = re.compile(r"\s*[/&;,+|]\s*")
_FEAT_RE = re.compile(r"\s+(feat\.?|ft\.?|featuring|with|vs\.?|versus|x)\s+.*$", re.IGNORECASE)


def norm_name(s: str | None) -> str:
    return (s or "").strip().lower()


def split_artists(artist_name: str | None) -> list[str]:
    """Токены артиста: «A/B feat. C» → [a, b, c]. Пустое → []."""
    text = norm_name(artist_name)
    if not text:
        return []
    # feat-часть — тоже артисты, режем отдельно и добавляем токенами
    m = _FEAT_RE.search(text)
    extra = m.group(0) if m else ""
    base = _FEAT_RE.sub("", text)
    parts = [p.strip() for p in _SEP_RE.split(base) if p.strip()]
    if extra:
        tail = re.sub(r"^\s*(feat\.?|ft\.?|featuring|with|vs\.?|versus|x)\s+", "", extra.strip(), flags=re.IGNORECASE)
        parts += [p.strip() for p in _SEP_RE.split(tail) if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def is_banned(artist_name: str | None, bans: set[str] | frozenset | list[str]) -> bool:
    """True, если артист (или любой его токен) в банах."""
    if not artist_name or not bans:
        return False
    full = norm_name(artist_name)
    normed = {norm_name(b) for b in bans}
    normed.discard("")
    if not normed:
        return False
    if full in normed:
        return True
    return any(t in normed for t in split_artists(artist_name))
