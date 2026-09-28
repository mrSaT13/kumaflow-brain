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


def is_local_source(external_id: str | None) -> bool:
    """True, если id принадлежит локальному файлу, а не Navidrome."""
    return str(external_id or "").startswith(LOCAL_PREFIXES)


def is_playable(external_id: str | None) -> bool:
    """Обратная проверка: True, если трек теоретически сыграет плеер."""
    return not is_local_source(external_id)


# TTL проверки «есть ли играбельные треки», секунды. Проверка — это запрос
# с LIKE по неиндексированному external_id, на 150k треков она не бесплатна,
# а ответ меняется только при сканировании библиотеки.
_EXIST_TTL_SEC = 60.0
_exist_cache: dict = {"at": 0.0, "value": None}


def has_playable_tracks(db) -> bool:
    """Есть ли в базе хоть один трек, который Navidrome сможет отдать плееру.

    Страховка для has_navidrome, а не самостоятельное условие. Смысл такой: если
    играбельных треков нет, фильтр включать незачем — отбирать всё равно не из
    чего, и плейлист вышел бы пустым. Благодаря этой проверке любое расхождение
    между настройками и содержимым базы деградирует в «фильтр выключен», а не в
    «пустая очередь».
    """
    import time as _t

    now = _t.time()
    if _exist_cache["value"] is not None and (now - _exist_cache["at"]) < _EXIST_TTL_SEC:
        return bool(_exist_cache["value"])
    try:
        from sqlalchemy import and_, or_

        from app.db.models import Track

        playable_row = (
            db.query(Track.id)
            .filter(or_(
                Track.external_id.is_(None),
                and_(
                    ~Track.external_id.like("disk:%"),
                    ~Track.external_id.like("demo-%"),
                ),
            ))
            .limit(1)
            .first()
        )
        value = playable_row is not None
    except Exception:  # noqa: BLE001 — не смогли проверить, не кэшируем
        # Возвращаем True: решение принимает has_navidrome, а если БД лежит,
        # запрос всё равно упадёт целиком. Кэшировать нельзя — иначе одна
        # transient-ошибка зафиксировала бы неверное значение на минуту.
        return True
    _exist_cache["value"] = value
    _exist_cache["at"] = now
    return value


def has_navidrome(db) -> bool:
    """Настроен ли реальный Navidrome (не демо) И есть что отдавать.

    От этого зависит, нужен ли фильтр вообще: без медиасервера локальный диск —
    единственная библиотека, и отсекать его нельзя.

    Решение о «демо или реальный сервер» НЕ дублируется, а берётся из
    media_server.is_real_config — того же, что использует resolve_active_server.
    Своя проверка означала второе определение одного и того же понятия, и они
    расходились: здесь дополнительно требовался непустой логин, а там нет.
    Следствие было тихим — при реальном Navidrome с пустым логином фильтр
    выключался, локальные файлы снова попадали в плейлист, и счёт 30 -> 23
    возвращался без единой ошибки в логах.

    Логин (`user`) здесь НЕ проверяется намеренно. Он нужен тем, кто ходит в
    Navidrome по API (см. api/now_playing.py), потому что им нужны креды. Этот
    модуль в Navidrome не ходит ни разу: он только решает, у трека есть номер
    Navidrome или нет, и для такого решения логин не требуется.
    """
    try:
        from app.services.media_server import get_media_server_config, is_real_config

        cfg = get_media_server_config(db) or {}
    except Exception:  # noqa: BLE001 — нет настроек, считаем что Navidrome нет
        return False
    if not is_real_config(cfg):
        return False
    return has_playable_tracks(db)


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
