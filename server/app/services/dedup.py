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

# Префиксы external_id, которые НЕ играют в плеере.
_LOCAL_PREFIXES = ("disk:", "demo-")


def is_local_source(external_id: str | None) -> bool:
    """True, если трек — локальная копия файла, а не трек Navidrome."""
    return str(external_id or "").startswith(_LOCAL_PREFIXES)


def _source_rank(external_id: str | None) -> int:
    """Приоритет источника при выборе канонического трека.

    Navidrome важнее локального файла, и это не вопрос вкуса, а вопрос того,
    что происходит с результатом. `services/playlist_push.py:42` выкидывает из
    выгрузки всё с префиксом disk:/demo-, потому что плеер таких треков не
    знает. Если каноническим станет локальный трек, то трек уедет в Navidrome
    уже без физического id — и из плейлиста выпадет навсегда, а не на один
    раз. Раньше приоритета источника не было вовсе: при равных play_count и
    starred (а у дисковых они всегда 0/False, см. _scan_disk_music) выбор
    решал created_at, а `library_scan` сканирует диск ПЕРВЫМ
    (`workers/tasks/__init__.py:422`) — то есть каноническим становился именно
    локальный дубль. Отсюда «создал плейлист на 30, плеер получил 23».
    """
    return 1 if is_local_source(external_id) else 0


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
            # Сначала источник: Navidrome должен выигрывать всегда, независимо
            # от статистики. Иначе каноническим станет disk:-трек, который
            # плеер не воспроизведёт (см. _source_rank).
            _source_rank(m.external_id),
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


def source_report(db) -> dict:
    """Разбор библиотеки по источнику. Только чтение — ничего не меняет.

    Нужен, чтобы перед любым массовым слиянием понимать масштаб: сколько
    треков реально играет в плеере (Navidrome), сколько существует только как
    локальный файл, и сколько локальных копий можно безопасно убрать, потому
    что у них есть Navidrome-двойник.
    """
    from app.db.models import TrackFeatures, Track

    local_prefixes = _LOCAL_PREFIXES
    rows = db.query(Track.id, Track.external_id, Track.duration_sec,
                    Track.title, Track.artist_name, Track.play_count, Track.starred).all()
    nav = loc = 0
    orphan_samples: list[dict] = []
    for r in rows:
        if is_local_source(r.external_id):
            loc += 1
        else:
            nav += 1

    # Группы, где смешаны оба источника: локальная копия + её Navidrome-двойник.
    # Именно их безопасно сливать (в пользу Navidrome), потому что плеер знает
    # только Navidrome-часть, а фичи/фичерпринты переезжают на неё целиком.
    groups = find_duplicate_groups(db, limit_groups=10000)
    cross: list[dict] = []
    removable = 0
    for g in groups:
        srcs = [t["source"] for t in g["tracks"]]
        has_nav = "navidrome" in srcs
        has_loc = "disk" in srcs or "demo" in srcs
        if not (has_nav and has_loc):
            continue
        keep = next((t for t in g["tracks"] if t["id"] == g["keep_id"]), None)
        drops_local = [t for t in g["tracks"] if t["id"] != g["keep_id"]
                       and t["source"] in ("disk", "demo")]
        removable += len(drops_local)
        if keep is not None and keep["source"] == "navidrome":
            cross.append({
                "key": g["key"],
                "keep_id": g["keep_id"],
                "drop_ids": [t["id"] for t in drops_local],
                "drop_count": len(drops_local),
            })
        else:
            # Каноническим сейчас локальный трек (уже слитый раньше или
            # созданный диском раньше Navidrome). Для отчёта — план починки.
            cross.append({
                "key": g["key"],
                "keep_id": g["keep_id"],
                "keep_source": keep["source"] if keep else None,
                "drop_ids": [t["id"] for t in drops_local],
                "drop_count": len(drops_local),
                "needs_recannonicalize": True,
            })

    # Локальные треки БЕЗ двойника в Navidrome — их нельзя сливать: это
    # единственные носители файла (Navidrome про них не знает). Их надо
    # оставить, иначе песня исчезнет из мозга вообще.
    nav_keys = set()
    for r in rows:
        if not is_local_source(r.external_id):
            k = exact_key(r.artist_name, r.title, r.duration_sec)
            if k:
                nav_keys.add(k)
    orphans = 0
    for r in rows:
        if is_local_source(r.external_id):
            k = exact_key(r.artist_name, r.title, r.duration_sec)
            if k not in nav_keys:
                orphans += 1
                if len(orphan_samples) < 20:
                    orphan_samples.append({"id": str(r.id), "title": r.title,
                                           "artist_name": r.artist_name})

    with_features = db.query(TrackFeatures.track_id).count()
    return {
        "total": len(rows),
        "navidrome": nav,
        "local": loc,
        "local_with_navidrome_twin": removable,
        "local_orphans_no_twin": orphans,
        "orphan_samples": orphan_samples,
        "cross_source_groups": len(cross),
        "needs_recannonicalize": sum(1 for c in cross if c.get("needs_recannonicalize")),
        "with_features": with_features,
        "note": ("Локальные копии с двойником в Navidrome можно слить в пользу "
                 "Navidrome — фичи переедут, плеер получит id. Локальные без "
                 "двойника трогать нельзя: это единственные носители файла."),
    }


def recannonicalize(db, dry_run: bool = True, limit_groups: int = 10000) -> dict:
    """Пересобрать канонические треки в пользу Navidrome (repair, не только plan).

    Что делает. Находит группы, где смешаны оба источника, и если каноническим
    сейчас локальный (disk:/demo-) трек — переливает всё в Navidrome-член
    группы: TrackFeatures, эмбеддинги, лайки, история, плейлисты.

    Зачем. `library_scan` historically сканировал диск ПЕРВЫМ, поэтому у
    никогда неигранных треков канонической строкой становилась локальная копия
    (у disk-строк play_count всегда 0 и starred False, так что статистика не
    могла перевесить). Плейлист из таких треков при выгрузке в Navidrome терял
    их: `playlist_push` пропускает disk:-префиксы, потому что плеер такого id
    не знает. Слить их «правильно» — значит сделать Navidrome-строку
    канонической, и тогда трек уедет в плеер, а его аудио-фичи (единственная
    причина вообще сканировать диск) переедут на неё.

    dry_run=True (по умолчанию) — только план, ничего не меняет.
    """
    groups = find_duplicate_groups(db, limit_groups=limit_groups)
    plan: list[dict] = []
    for g in groups:
        members = g["tracks"]
        nav_members = [t for t in members if t["source"] == "navidrome"]
        if not nav_members:
            continue  # нет Navidrome-двойника — сливать не с кем, файл пропадёт
        local_members = [t for t in members if t["source"] in ("disk", "demo")]
        if not local_members:
            continue
        keep = next((t for t in members if t["id"] == g["keep_id"]), None)
        if keep is not None and keep["source"] == "navidrome":
            # Уже правильно: Navidrome канонический, локальные — лишние.
            plan.append({
                "key": g["key"],
                "keep_id": keep["id"],
                "drop_ids": [t["id"] for t in local_members],
                "drop_count": len(local_members),
                "action": "drop_local",
            })
        else:
            # Канонический — локальный. Чиним: Navidrome-член становится keep,
            # локальный уходит в drop вместе с остальными дублями.
            plan.append({
                "key": g["key"],
                "keep_id": nav_members[0]["id"],
                "old_keep_id": g["keep_id"],
                "drop_ids": [t["id"] for t in members if t["id"] != nav_members[0]["id"]],
                "drop_count": len(members) - 1,
                "action": "recannonicalize",
            })

    if dry_run:
        return {
            "dry_run": True,
            "groups": len(plan),
            "recannonicalize": sum(1 for p in plan if p["action"] == "recannonicalize"),
            "drop_local": sum(1 for p in plan if p["action"] == "drop_local"),
            "tracks_to_merge": sum(p["drop_count"] for p in plan),
            "plan": plan[:200],
        }

    fixed = merged = 0
    for p in plan:
        try:
            res = merge_tracks(db, p["keep_id"], p["drop_ids"])
            if res.get("ok"):
                merged += res["merged"]
                if p["action"] == "recannonicalize":
                    fixed += 1
        except Exception as e:  # noqa: BLE001 — одна битая группа не валит всё
            logger.warning("recannonicalize group failed: {}", e)
            db.rollback()
    return {
        "dry_run": False,
        "groups": len(plan),
        "recannonicalize": fixed,
        "drop_local": sum(1 for p in plan if p["action"] == "drop_local"),
        "tracks_merged": merged,
    }
