"""Канон жанров: рваные теги приводятся к одному виду НА ВХОДЕ.

Зачем. Один и тот же жанр приезжал десятком написаний («numetal»,
«Nu Metal», «nu-metal», «NU_METAL»), и весь downstream сравнивал строки:
похожие артисты, кластерные подписи, вкусовой профиль. Сабстринг-матч в
радио чинит сравнение, но корень — грязь в данных. Канон пишется один раз
при скане/импорте, старые строки правит backfill при старте бэкенда.

Ключ — только буквы+цифры нижнего регистра: дефис/пробел/подчёркивание
схлопываются сами («nu-metal» == «nu metal» == «numetal» по ключу).
Неизвестный жанр возвращается почищенным (strip, нижний регистр, один
пробел), а не выкидывается — лучше «etan» как есть, чем пусто.
"""

from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")
_ALNUM_RE = re.compile(r"[^a-z0-9]")

# Ключ (без разделителей) -> канон. Только уверенные слияния:
# разные написания одного и того же, плюс rap==hip-hop (одно семейство).
_ALIASES: dict[str, str] = {
    "numetal": "nu metal",
    "hiphop": "hip-hop",
    "rap": "hip-hop",
    "randb": "r&b",
    "rnb": "r&b",
    "synthpop": "synth-pop",
    "drumnbass": "drum'n'bass",
    "dnb": "drum'n'bass",
    "rocknroll": "rock'n'roll",
    "heavymetal": "heavy metal",
    "deathmetal": "death metal",
    "blackmetal": "black metal",
    "thrashmetal": "thrash metal",
    "speedmetal": "speed metal",
    "powermetal": "power metal",
    "doommetal": "doom metal",
    "alternativemetal": "alternative metal",
    "altmetal": "alternative metal",
    "hardrock": "hard rock",
    "punkrock": "punk rock",
    "poppunk": "pop punk",
    "indierock": "indie rock",
    "postpunk": "post-punk",
    "newwave": "new wave",
    "triphop": "trip-hop",
    "altrock": "alternative rock",
}


def canon_genre(value: str | None) -> str | None:
    """Канонический жанр или None (пусто на входе — пусто на выходе)."""
    if value is None:
        return None
    s = _WS_RE.sub(" ", str(value).strip().lower())
    if not s:
        return None
    key = _ALNUM_RE.sub("", s)
    if not key:
        return None
    hit = _ALIASES.get(key)
    if hit:
        return hit
    return s[:64] or None


def backfill_genres(db) -> int:
    """Переписать отличающиеся жанры треков/альбомов каноном. Возвращает число правок.

    Идемпотентно и дёшево: читаем distinct-жанры (их сотни), UPDATE только
    по изменившимся (десятки). Зовётся при старте бэкенда (init_db),
    best-effort — ошибка не валит старт.
    """
    try:
        from app.db.models import Album as _A
        from app.db.models import Track as _T
    except Exception:
        return 0
    fixed = 0
    try:
        track_genres = [r[0] for r in db.query(_T.genre).distinct().all()]
    except Exception:
        return 0
    try:
        album_genres = [r[0] for r in db.query(_A.genre).distinct().all()]
    except Exception:
        album_genres = []
    mapping: dict[str, str] = {}
    for g in track_genres + album_genres:
        if not g:
            continue
        c = canon_genre(str(g))
        if c and c != str(g):
            mapping[str(g)] = c
    if not mapping:
        return 0
    try:
        for old, new in mapping.items():
            try:
                n1 = db.query(_T).filter(_T.genre == old).update(
                    {_T.genre: new}, synchronize_session=False)
                fixed += int(n1 or 0)
            except Exception:
                pass
            try:
                n2 = db.query(_A).filter(_A.genre == old).update(
                    {_A.genre: new}, synchronize_session=False)
                fixed += int(n2 or 0)
            except Exception:
                pass
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    return fixed
