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


def is_banned_either(artist_name: str | None, artist_external_id: str | None,
                     ban_names: set[str] | frozenset | list[str],
                     ban_ids: set[str] | frozenset | list[str]) -> bool:
    """Двойной матч бана: имя (с токенами, как раньше) ИЛИ Navidrome-ID.

    Чисто аддитивно к is_banned: всё, что ловилось по имени, ловится так же;
    сверху — точные попадания по ID (десктоп шлёт пары {id, name}, у треков
    с диска ID нет — их по-прежнему крыет только имя).
    """
    if artist_external_id and ban_ids:
        try:
            if str(artist_external_id) in set(map(str, ban_ids)):
                return True
        except Exception:
            pass
    return is_banned(artist_name, ban_names)


def load_bans(db, user_id: str) -> tuple[set[str], set[str]]:
    """Баны юзера парой (имена, external_ids). Не падает никогда.

    Имена — как раньше (для is_banned с токенами), ids — для точного матча
    Navidrome-треков. Пустые значения отбрасываются с обеих сторон.
    """
    names: set[str] = set()
    ids: set[str] = set()
    try:
        from app.db.models import ArtistBan as _AB

        rows = db.query(_AB).filter_by(user_id=user_id).all()
    except Exception:
        return names, ids
    for r in rows or []:
        try:
            n = str(getattr(r, "artist_name", "") or "")
            if n:
                names.add(n)
            i = str(getattr(r, "artist_external_id", "") or "")
            if i:
                ids.add(i)
        except Exception:
            continue
    return names, ids


def backfill_ban_ids(db, user_id: str) -> int:
    """Дозаполнить пустые artist_external_id у банов юзера. Best-effort.

    Два точных прохода, без эвристик:
    1) «имя» бана равно чьему-то artist_external_id (десктоп слал голые ID
       до обновления плеера) — это ID;
    2) точное совпадение имени (lower+strip) с Navidrome-треком (не disk:/demo-)
       — забираем его artist_external_id.
    Возвращает число заполненных строк. Не коммитит — коммитит вызывающий.
    """
    filled = 0
    try:
        from app.db.models import ArtistBan as _AB
        from app.db.models import Track as _T

        try:
            # Сессия sync_from_mobile — autoflush=False: без flush только что
            # добавленная строка не видна SELECT. Flush сбрасывает pending
            # INSERT, коммитит по-прежнему вызывающий.
            db.flush()
        except Exception:
            pass
        rows = db.query(_AB).filter_by(user_id=user_id).all()
        need = [r for r in (rows or [])
                if not (str(getattr(r, "artist_external_id", "") or ""))]
        if not need:
            return 0
        # Карта всех артистов библиотеки: external_id -> имя, имя -> external_id.
        # Только Navidrome-строки дают настоящие ID; disk:/demo- пропускаем
        # во втором проходе (их synthetic dartists: нам не нужны).
        id_to_name: dict[str, str] = {}
        name_to_id: dict[str, str] = {}
        try:
            for aid, aname, ext in db.query(
                    _T.artist_external_id, _T.artist_name, _T.external_id).all():
                aid_s, aname_s = str(aid or ""), str(aname or "")
                if aid_s and aname_s and aid_s not in id_to_name:
                    id_to_name[aid_s] = aname_s
                if aname_s and ext and not str(ext).startswith(("disk:", "demo-", "dartist:", "dalbum:")):
                    k = aname_s.strip().lower()
                    if k and k not in name_to_id and aid_s:
                        name_to_id[k] = aid_s
        except Exception:
            pass
        for r in need:
            try:
                cur = str(r.artist_name or "")
                if not cur:
                    continue
                if cur in id_to_name:
                    r.artist_external_id = cur[:128]
                    filled += 1
                else:
                    hit = name_to_id.get(cur.strip().lower())
                    if hit:
                        r.artist_external_id = hit[:128]
                        filled += 1
            except Exception:
                continue
        if filled:
            try:
                db.flush()
            except Exception:
                pass
    except Exception:
        pass
    return filled
