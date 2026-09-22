"""Дедупликация треков: поиск и аккуратное сшивание дублей.

Точный дубль (кандидат на авто-слияние): одинаковые нормализованные
артист + название + длительность (±2с). Несколько проверок перед сшиванием:
  1) группы только из >=2 треков с одинаковым ключом;
  2) live/remix/edit/instrumental-маркеры в скобках — разные версии, не трогаем;
  3) сшиваем в канонический (больше прослушиваний/starred/старше), FK переносим.
Похожие (тот же артист+базовое название, но длительность/версия различаются) —
только на ручное решение, автоматом никогда.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from app.core.logging import get_logger
from app.db.models import (
    Favorite,
    Lyrics,
    PlayEvent,
    PlayHistory,
    PlaylistTrack,
    Track,
    TrackCluster,
    TrackDislike,
    TrackEmbedding,
    TrackFeatures,
    TrackMetadataEnrich,
)

logger = get_logger("dedup")

# маркеры версий — такие треки дублями НЕ считаем
_VERSION_RE = re.compile(
    r"\b(live|remix|remaster|edit|extended|instrumental|acoustic|demo|version|mix|cover|karaoke|sped up|slowed|rework|remake)\b",
    re.IGNORECASE,
)

_DURATION_TOLERANCE = 2  # секунды


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[\u200b-\u200f\ufeff]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _has_version_marker(title: str) -> bool:
    m = re.search(r"[\(\[]([^\)\]]+)[\)\]]", title or "")
    return bool(m and _VERSION_RE.search(m.group(1)))


def exact_key(artist: str | None, title: str | None, duration: int | None) -> tuple | None:
    """Ключ точного дубля. None — не кандидат (нет данных / версия)."""
    a, t = norm_text(artist), norm_text(title)
    if not a or not t:
        return None
    if _has_version_marker(title or ""):
        return None
    if duration is None:
        return ("notime", a, t)
    return ("dur", a, t, int(duration // 5))  # бакет 5с, точная проверка — по ±2с


def find_duplicate_groups(db, limit_groups: int = 500) -> list[dict]:
    """Группы точных дублей. Лёгкий проход колонками (без ORM-нагрузки)."""
    rows = db.query(
        Track.id, Track.title, Track.artist_name, Track.album_name,
        Track.genre, Track.duration_sec, Track.play_count, Track.starred,
        Track.external_id, Track.created_at,
    ).all()
    buckets: dict[tuple, list] = defaultdict(list)
    for r in rows:
        k = exact_key(r.artist_name, r.title, r.duration_sec)
        if k:
            buckets[k].append(r)
    groups = []
    for k, members in buckets.items():
        if len(members) < 2:
            continue
        # точная проверка длительности внутри бакета (для "dur")
        if k[0] == "dur":
            durs = [m.duration_sec for m in members if m.duration_sec is not None]
            if durs and (max(durs) - min(durs) > _DURATION_TOLERANCE):
                continue
        members = sorted(members, key=lambda m: (
            -(m.play_count or 0), not bool(m.starred), str(m.created_at or "")))
        groups.append({
            "key": f"{k[1]} — {k[2]}",
            "keep_id": str(members[0].id),
            "tracks": [
                {"id": str(m.id), "title": m.title, "artist_name": m.artist_name,
                 "album_name": m.album_name, "genre": m.genre,
                 "duration_sec": m.duration_sec, "play_count": m.play_count or 0,
                 "starred": bool(m.starred),
                 "source": ("disk" if (m.external_id or "").startswith("disk:")
                            else "demo" if (m.external_id or "").startswith("demo-")
                            else "navidrome")}
                for m in members
            ],
        })
        if len(groups) >= limit_groups:
            break
    groups.sort(key=lambda g: len(g["tracks"]), reverse=True)
    return groups


def _move_fk(db, model, col: str, drop_ids: list[str], keep_id: str,
             extra_skip_check=None) -> int:
    """Перенести ссылки drop→keep. Возвращает число перенесённых строк."""
    moved = 0
    q = db.query(model).filter(getattr(model, col).in_(drop_ids))
    for row in q.all():
        if extra_skip_check is not None and extra_skip_check(row):
            db.delete(row)
            continue
        setattr(row, col, keep_id)
        moved += 1
    return moved


def merge_tracks(db, keep_id: str, drop_ids: list[str]) -> dict:
    """Сшить дубли в канонический: статистика суммируется, ссылки переносятся.

    Защита от дублей PK (Favorite/Dislike/Lyrics/...): если у keep уже есть
    такая же строка — строка дубля удаляется.
    """
    drop_ids = [d for d in drop_ids if d != keep_id]
    if not drop_ids:
        return {"ok": False, "error": "nothing to merge"}
    keep = db.get(Track, keep_id)
    drops = [db.get(Track, d) for d in drop_ids]
    drops = [d for d in drops if d is not None]
    if not keep or not drops:
        return {"ok": False, "error": "track not found"}

    total_plays = keep.play_count or 0
    for d in drops:
        total_plays += d.play_count or 0
        if d.starred:
            keep.starred = True
        if d.rating and (not keep.rating or d.rating > keep.rating):
            keep.rating = d.rating
        if d.last_played_at and (not keep.last_played_at or d.last_played_at > keep.last_played_at):
            keep.last_played_at = d.last_played_at
        # обложка: добираем если у keep нет
        if not keep.cover_art_id and d.cover_art_id:
            keep.cover_art_id = d.cover_art_id
        if not keep.genre and d.genre:
            keep.genre = d.genre
        if not keep.year and d.year:
            keep.year = d.year
    keep.play_count = total_plays

    moved: dict[str, int] = {}

    def _pk_skip(model, extra_cols: tuple):
        def check(row):
            filt = [getattr(model, c) == getattr(row, c) for c in extra_cols]
            filt.append(getattr(model, [c for c in ("track_id",) if hasattr(model, c)][0]) == keep_id)
            return db.query(model).filter(*filt).first() is not None
        return check

    # Favorite(user,track), TrackDislike(user,track): скип если у keep уже есть
    for model in (Favorite, TrackDislike):
        n = 0
        for row in db.query(model).filter(model.track_id.in_(drop_ids)).all():
            if db.query(model).filter_by(user_id=row.user_id, track_id=keep_id).first():
                db.delete(row)
            else:
                row.track_id = keep_id
                n += 1
        moved[model.__tablename__] = n
    # PlayHistory / PlayEvent: просто переносим (PK по id)
    for model in (PlayHistory, PlayEvent):
        moved[model.__tablename__] = _move_fk(db, model, "track_id", drop_ids, keep_id)
    # PlaylistTrack(playlist,track): скип-дубли позиций
    n = 0
    for row in db.query(PlaylistTrack).filter(PlaylistTrack.track_id.in_(drop_ids)).all():
        if db.query(PlaylistTrack).filter_by(playlist_id=row.playlist_id, track_id=keep_id).first():
            db.delete(row)
        else:
            row.track_id = keep_id
            n += 1
    moved["playlist_tracks"] = n
    # Lyrics / Enrich / Features / Embedding / Cluster (PK track_id[+...]): перенос или удаление
    for model in (Lyrics, TrackMetadataEnrich, TrackFeatures, TrackEmbedding, TrackCluster):
        n = 0
        for row in db.query(model).filter(model.track_id.in_(drop_ids)).all():
            exists_q = db.query(model).filter(model.track_id == keep_id)
            if hasattr(model, "provider"):
                exists_q = exists_q.filter(model.provider == row.provider)
            if hasattr(model, "source"):
                exists_q = exists_q.filter(model.source == row.source)
            if hasattr(model, "model"):
                exists_q = exists_q.filter(model.model == row.model)
            if hasattr(model, "algorithm"):
                exists_q = exists_q.filter(model.algorithm == row.algorithm)
            if exists_q.first() is not None:
                db.delete(row)
            else:
                row.track_id = keep_id
                n += 1
        moved[model.__tablename__] = n

    for d in drops:
        db.delete(d)
    db.commit()
    logger.info("merged {} dupes into {}: {}", len(drops), keep_id[:8], moved)
    return {"ok": True, "keep_id": keep_id, "merged": len(drops), "moved": moved}


def auto_merge_exact(db, limit_groups: int = 2000) -> dict:
    """Автослияние только точных групп (для чекбокса/крона)."""
    groups = find_duplicate_groups(db, limit_groups=limit_groups)
    merged = 0
    for g in groups:
        try:
            res = merge_tracks(db, g["keep_id"], [t["id"] for t in g["tracks"] if t["id"] != g["keep_id"]])
            if res.get("ok"):
                merged += res["merged"]
        except Exception as e:  # noqa: BLE001 — одна битая группа не валит всё
            logger.warning("auto-merge group failed: {}", e)
            db.rollback()
    return {"groups": len(groups), "merged": merged}
