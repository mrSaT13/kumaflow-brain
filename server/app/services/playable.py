"""Какие треки имеет смысл отдавать плееру.

Проблема, из-за которой этот модуль существует. В мозге две библиотеки в одной
таблице `tracks`:

  * Navidrome — то, что реально играет (у трека есть Subsonic id в
    `external_id`, плеер его знает);
  * локальные файлы из примонтированного `MUSIC_DIR` — служебные, нужны ради
    аудио-фичей (librosa даёт energy/valence/tempo/тональность, которых у
    Navidrome нет). Их id начинается с `disk:`.

Пока обе лежат рядом и попадают в один пул кандидатов, получается ровно то,
что и происходит: плейлист формируется из 30 треков, а в плеер уезжает 23 —
`playlist_push` пропускает `disk:`-строки, потому что Navidrome не знает
таких id, и молча выкидывает их.

Здесь одно правило и одна функция-фильтр. Правило: **плеер — единственный
источник истины**. Трек без Navidrome-id не имеет права попасть в плейлист;
его роль — поставлять фичи, а не занимать место в очереди.

Демо-режим — исключение. Там Navidrome нет и нет вообще ничего, кроме диска,
поэтому фильтр возвращает `None` (не ограничивать) и всё работает как раньше.
"""
from __future__ import annotations

# Префиксы external_id, означающие «это не трек Navidrome».
LOCAL_PREFIXES = ("disk:", "demo-")

# Локальный адрес в поле url = демо, а не настоящий медиасервер.
_DEMO_URLS = ("http://localhost", "https://localhost")


def is_local_source(external_id: str | None) -> bool:
    """True, если id принадлежит локальному файлу, а не Navidrome."""
    return str(external_id or "").startswith(LOCAL_PREFIXES)


def is_playable(external_id: str | None) -> bool:
    """Обратная проверка: True, если трек теоретически сыграет плеер."""
    return not is_local_source(external_id)


def has_navidrome(db) -> bool:
    """Настроен ли реальный Navidrome (не демо).

    От этого зависит, нужен ли фильтр вообще: без медиасервера локальный диск —
    единственная библиотека, и отсекать его нельзя.
    """
    try:
        from app.services.media_server import get_media_server_config

        cfg = get_media_server_config(db) or {}
    except Exception:  # noqa: BLE001 — нет настроек, считаем что Navidrome нет
        return False
    url = str(cfg.get("url") or "").strip().lower()
    user = str(cfg.get("user") or "").strip()
    if not url or not user:
        return False
    if url in _DEMO_URLS or "demo" in url:
        return False
    return True


def playable_filter(db):
    """SQL-фильтр «только то, что сыграет плеер». None = фильтр не нужен.

    Возвращает выражение для `.filter(...)`. Удобно, потому что не тащит в
    память лишние строки: в библиотеке на десятки тысяч треков разница между
    «выбрать всё и отсечь в питоне» и «не выбирать» заметна.
    """
    if not has_navidrome(db):
        return None
    # Импорт внутри: сам модуль должен грузиться и без SQLAlchemy — префиксы
    # и has_navidrome используются в местах, где ORM не нужен вовсе.
    from sqlalchemy import and_, or_

    from app.db.models import Track

    return or_(
        Track.external_id.is_(None),
        and_(
            ~Track.external_id.like("disk:%"),
            ~Track.external_id.like("demo-%"),
        ),
    )


def apply(query, db):
    """Навесить фильтр на запрос, если он нужен. Возвращает тот же query."""
    f = playable_filter(db)
    return query.filter(f) if f is not None else query


def playable_ids(db, ids: list[str]) -> tuple[list[str], int]:
    """Разделить список id на «плеер сможет» и «в плеер не уедет».

    Нужно генераторам плейлистов: если попросили 30, а 7 из них локальные,
    молча отдать 23 — это плохо. Правильно либо добрать до 30 другими треками,
    либо честно сказать, сколько не доедет.
    """
    if not ids or not has_navidrome(db):
        return list(ids), 0
    from app.db.models import Track

    rows = db.query(Track.id, Track.external_id).filter(Track.id.in_(list(ids))).all()
    good: list[str] = []
    bad = 0
    for rid, ext in rows:
        if is_playable(ext):
            good.append(str(rid))
        else:
            bad += 1
    # Сохраняем исходный порядок (важно для энергетической волны).
    order = {str(t): i for i, t in enumerate(ids)}
    good.sort(key=lambda t: order.get(t, 0))
    return good, bad


__all__ = [
    "LOCAL_PREFIXES",
    "apply",
    "has_navidrome",
    "is_local_source",
    "is_playable",
    "playable_filter",
    "playable_ids",
]
