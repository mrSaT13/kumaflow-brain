from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db import get_db, models
from app.db.models import MediaServer, Track, Album, Artist, MediaUser, TrackFeatures

logger = get_logger("api.library")

# Негативный кэш серверного similar (как mobile isSimilarSupported):
# без Last.fm Navidrome висит/пусто — не жжём таймаут на каждый тап.
_SIMILAR_DEADLINE: datetime | None = None


def _server_similar_songs(seed_external_id: str, count: int = 20) -> list[dict] | None:
    """getSimilarSongs2 с Navidrome. None — пропустить (негативный кэш/нет конфига).

    Таймаут 6с как в мобильном cold-start; пусто/ошибка → негатив на час.
    """
    global _SIMILAR_DEADLINE
    from app.db.database import session_scope
    from app.services.media_server import get_media_server_config

    if _SIMILAR_DEADLINE is not None and datetime.utcnow() < _SIMILAR_DEADLINE:
        return None
    with session_scope() as db:
        try:
            cfg = get_media_server_config(db)
        except Exception:
            cfg = {}
    url, user, password = (cfg.get("url") or ""), (cfg.get("user") or ""), (cfg.get("password") or "")
    if not url or not user or seed_external_id.startswith(("disk:", "demo-")):
        return None
    import asyncio

    from app.services.navidrome.client import SubsonicAuth, SubsonicClient

    async def _go() -> list[dict]:
        async with SubsonicClient(url, SubsonicAuth(user=user, password=password), timeout=8.0) as client:
            return await client.get_similar_songs2(seed_external_id, count=count)

    try:
        songs = asyncio.run(asyncio.wait_for(_go(), timeout=6.0))
    except Exception as e:  # noqa: BLE001 (таймаут/Last.fm/нет сети — тихо локально)
        logger.warning("getSimilarSongs2 failed, local-only: {}", e)
        _SIMILAR_DEADLINE = datetime.utcnow() + timedelta(hours=1)
        return None
    if not songs:
        _SIMILAR_DEADLINE = datetime.utcnow() + timedelta(hours=1)
        return None
    _SIMILAR_DEADLINE = None
    return songs

router = APIRouter()


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    from app.services.media_server import get_media_server_config

    cfg = get_media_server_config(db)
    real_url = cfg.get("url") if cfg.get("url") not in ("", "http://localhost", "https://localhost") else None
    active = None
    if real_url:
        srv = db.query(MediaServer).filter(MediaServer.url == real_url).first()
        if srv:
            active = srv.url
    # Итоги — по ВСЕЙ базе (Navidrome + диск + демо), иначе файлы с диска
    # на другой строке media_servers пропадали из счётчиков.
    total_tracks = db.query(Track).count()
    disk_tracks = db.query(Track).filter(Track.external_id.like("disk:%")).count()
    navidrome_tracks = db.query(Track).filter(
        ~Track.external_id.like("disk:%"), ~Track.external_id.like("demo-%")
    ).count()
    return {
        "tracks": total_tracks,
        "albums": db.query(Album).count(),
        "artists": db.query(Artist).count(),
        "users": db.query(MediaUser).count(),
        "servers": db.query(MediaServer).count(),
        "active_server": active,
        "disk_tracks": disk_tracks,
        "navidrome_tracks": navidrome_tracks,
        "analyzed_tracks": db.query(TrackFeatures).count(),
    }


@router.get("/health")
def library_health(db: Session = Depends(get_db)):
    """Здоровье библиотеки: что чинить (битрейт, обложки, тексты, анализ, дубли, жанры)."""
    from sqlalchemy import or_

    from app.db.models import Lyrics
    from app.services import dedup as _dd

    total = db.query(Track).count()
    low_bitrate = db.query(Track).filter(
        Track.bitrate.is_not(None), Track.bitrate < 192).count()
    no_cover = db.query(Track).filter(
        or_(Track.cover_art_id.is_(None), Track.cover_art_id == "")).count()
    with_lyrics = db.query(Lyrics.track_id).distinct().count()
    analyzed = db.query(TrackFeatures).count()
    no_genre = db.query(Track).filter(
        or_(Track.genre.is_(None), Track.genre == "")).count()
    groups = _dd.find_duplicate_groups(db, limit_groups=10000)
    dup_tracks = sum(len(g["tracks"]) - 1 for g in groups)

    def _brief(rows):
        return [{"track_id": str(t.id), "title": t.title,
                 "artist_name": t.artist_name} for t in rows]

    low_br_sample = _brief(db.query(Track).filter(
        Track.bitrate.is_not(None), Track.bitrate < 192).limit(5).all())
    no_lyr_sample = _brief(db.query(Track).outerjoin(
        Lyrics, Lyrics.track_id == Track.id).filter(
        Lyrics.track_id.is_(None)).limit(5).all())

    return {
        "ok": True, "total": total,
        "low_bitrate": low_bitrate,
        "no_cover": no_cover,
        "no_lyrics": max(0, total - with_lyrics),
        "not_analyzed": max(0, total - analyzed),
        "no_genre": no_genre,
        "duplicate_groups": len(groups),
        "duplicate_tracks": dup_tracks,
        "samples": {
            "low_bitrate": low_br_sample,
            "no_lyrics": no_lyr_sample,
        },
    }


@router.get("/servers")
def list_servers(db: Session = Depends(get_db)):
    rows = db.query(MediaServer).all()
    return {
        "servers": [
            {
                "id": str(s.id),
                "type": s.type,
                "name": s.name,
                "url": s.url,
                "enabled": s.enabled,
            }
            for s in rows
        ]
    }


@router.get("/genres")
def list_genres(source: str | None = None, db: Session = Depends(get_db)):
    # Жанры по всей базе (опционально source=disk/navidrome/demo).
    q = db.query(Track.genre).filter(Track.genre.isnot(None))
    if source == "disk":
        q = q.filter(Track.external_id.like("disk:%"))
    elif source == "navidrome":
        q = q.filter(~Track.external_id.like("disk:%"), ~Track.external_id.like("demo-%"))
    elif source == "demo":
        q = q.filter(Track.external_id.like("demo-%"))
    rows = q.distinct().all()
    return {"genres": sorted([r[0] for r in rows if r[0]])}


def _artist_cover_map(db: Session, names: list[str]) -> dict[str, str]:
    """Для каждого имени артиста — id трека для обложки.

    Предпочитаем трек с cover_art_id (Navidrome), иначе топ по play_count
    (у него может быть встроенная обложка в файле — /api/covers/track
    её отдаст, иначе вернёт плейсхолдер).
    """
    out: dict[str, str] = {}
    if not names:
        return out
    rows = (
        db.query(Track.artist_name, Track.id, Track.cover_art_id, Track.play_count)
        .filter(Track.artist_name.in_(names))
        .order_by(Track.play_count.desc().nullslast())
        .limit(max(2000, len(names) * 4))
        .all()
    )
    best: dict[str, tuple] = {}
    for aname, tid, cover, pc in rows:
        if not aname:
            continue
        cur = best.get(aname)
        if cur is None:
            best[aname] = (tid, cover)
        elif not cur[1] and cover:
            best[aname] = (tid, cover)
    return {k: str(v[0]) for k, v in best.items()}


def _artist_entries(db: Session, names: list[str]) -> list[dict]:
    """Собрать карточки артистов: счётчики, топ-жанры, обложка."""
    if not names:
        return []
    counts = dict(
        db.query(Track.artist_name, func.count(Track.id))
        .filter(Track.artist_name.in_(names))
        .group_by(Track.artist_name)
        .all()
    )
    genre_rows = (
        db.query(Track.artist_name, Track.genre, func.count(Track.id))
        .filter(Track.artist_name.in_(names), Track.genre.isnot(None))
        .group_by(Track.artist_name, Track.genre)
        .all()
    )
    top_genres: dict[str, list[str]] = {}
    for aname, genre, cnt in genre_rows:
        top_genres.setdefault(aname or "", []).append((genre, cnt))
    covers = _artist_cover_map(db, names)
    out = []
    for n in names:
        gs = sorted(top_genres.get(n, []), key=lambda kv: kv[1], reverse=True)
        out.append(
            {
                "name": n,
                "track_count": counts.get(n, 0),
                "top_genres": [g for g, _ in gs[:3]],
                "cover_track_id": covers.get(n),
            }
        )
    return out


@router.get("/artists")
def list_artists(
    q: str | None = None,
    genre: list[str] | None = Query(default=None),
    limit: int = Query(300, le=1000),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Артисты для визарда холодного старта (как в мобильном): поиск,
    фильтр по жанрам, счётчики треков и обложка для кружков.

    Группируем по Track.artist_name — это имя видит пользователь и оно же
    используется в seed-taste/cold-start (без привязки к Artist.id).
    """
    # базовый список имён с числом треков
    tq = db.query(Track.artist_name, func.count(Track.id)).filter(Track.artist_name.isnot(None))
    if q:
        like = f"%{q.lower()}%"
        tq = tq.filter(Track.artist_name.ilike(like))
    if genre:
        sub = db.query(Track.artist_name).filter(Track.genre.in_(genre)).distinct().subquery()
        tq = tq.filter(Track.artist_name.in_(db.query(sub.c.artist_name)))
    tq = tq.group_by(Track.artist_name).order_by(func.count(Track.id).desc())
    total = tq.count()
    rows = tq.offset(offset).limit(limit).all()
    names = [r[0] for r in rows if r[0]]
    return {"items": _artist_entries(db, names), "total": total, "limit": limit, "offset": offset}


@router.get("/artists/similar")
def similar_artists(name: str, limit: int = Query(6, le=12), db: Session = Depends(get_db)):
    """Похожие артисты (раскрытие под кружком как в мобильном).

    Порядок как в mobile _fetchSimilarArtists:
    1) локально по жанрам;
    2) добивка серверным getSimilarSongs2 (сид — топ-трек артиста);
    3) fallback — топ по числу треков.
    Серверный хит по артисту из нашей библиотеки даёт +6, артисты
    только с сервера прикладываются с cover_art_id (обложка через /api/covers).
    """
    from app.db.models import TrackCluster

    if not name:
        return {"items": [], "server_used": False}
    # топ-жанры артиста
    grows = (
        db.query(Track.genre, func.count(Track.id))
        .filter(Track.artist_name == name, Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc())
        .limit(3)
        .all()
    )
    genres = [g for g, _ in grows]
    # сид для серверного similar: топ-трек артиста из Navidrome (не disk/demo)
    seed_track = (
        db.query(Track)
        .filter(Track.artist_name == name)
        .order_by(Track.play_count.desc().nullslast())
        .limit(20)
        .all()
    )
    seed_ext: str | None = None
    for t in seed_track:
        ext = str(t.external_id or "")
        if ext and not ext.startswith(("disk:", "demo-")):
            seed_ext = ext
            break
    # частые кластеры артиста
    track_ids = [str(t.id) for t in seed_track]
    if not track_ids:
        track_ids = [r[0] for r in db.query(Track.id).filter(Track.artist_name == name).limit(2000).all()]
        track_ids = [str(x) for x in track_ids]
    cluster_ids: list[int] = []
    if track_ids:
        crows = (
            db.query(TrackCluster.cluster_id, func.count(TrackCluster.track_id))
            .filter(TrackCluster.track_id.in_(track_ids))
            .group_by(TrackCluster.cluster_id)
            .order_by(func.count(TrackCluster.track_id).desc())
            .limit(2)
            .all()
        )
        cluster_ids = [c for c, _ in crows]
    cluster_track_ids: set[str] = set()
    if cluster_ids:
        cluster_track_ids = {
            str(r[0])
            for r in db.query(TrackCluster.track_id)
            .filter(TrackCluster.cluster_id.in_(cluster_ids))
            .limit(5000)
            .all()
        }
    # кандидаты: артисты тех же жанров
    cand_q = db.query(Track.artist_name, Track.genre, Track.id).filter(
        Track.artist_name.isnot(None), Track.artist_name != name
    )
    if genres:
        cand_q = cand_q.filter(Track.genre.in_(genres))
    cand_rows = cand_q.limit(5000).all()
    from collections import Counter

    scores: Counter = Counter()
    for aname, g, tid in cand_rows:
        if not aname:
            continue
        if g in genres:
            scores[aname] += 2
        if str(tid) in cluster_track_ids:
            scores[aname] += 3

    # 2) серверный getSimilarSongs2 (как mobile: сид — первый трек жанра)
    server_used = False
    server_only: list[dict] = []
    if seed_ext:
        songs = _server_similar_songs(seed_ext, count=20)
        if songs:
            server_used = True
            # external_id песен → наши артисты (буст), остальные — с coverArt сервера
            ext_ids = [str(s.get("id") or "") for s in songs if s.get("id")]
            local_by_ext: dict[str, str] = {}
            if ext_ids:
                for t in db.query(Track).filter(Track.external_id.in_(ext_ids)).all():
                    local_by_ext[str(t.external_id)] = str(t.artist_name or "")
            lib_names_lower = {
                str(r[0]).lower(): str(r[0])
                for r in db.query(Track.artist_name).distinct().all()
                if r[0]
            }
            seen_server: set[str] = set()
            for s in songs:
                sid = str(s.get("id") or "")
                sartist = str(s.get("artist") or "").strip()
                if not sartist or sartist.lower() == name.lower():
                    continue
                hit = local_by_ext.get(sid) or lib_names_lower.get(sartist.lower())
                if hit:
                    scores[hit] += 6
                elif sartist.lower() not in seen_server:
                    seen_server.add(sartist.lower())
                    server_only.append(
                        {
                            "name": sartist,
                            "track_count": 0,
                            "top_genres": [],
                            "cover_track_id": None,
                            "cover_art_id": s.get("coverArt"),
                        }
                    )

    scored = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not scored and not server_only:
        # 3) fallback: топ по числу треков
        fb = (
            db.query(Track.artist_name, func.count(Track.id))
            .filter(Track.artist_name.isnot(None), Track.artist_name != name)
            .group_by(Track.artist_name)
            .order_by(func.count(Track.id).desc())
            .limit(limit)
            .all()
        )
        names = [r[0] for r in fb if r[0]]
        return {"items": _artist_entries(db, names), "server_used": False}
    names = [n for n, _ in scored[:limit]]
    items = _artist_entries(db, names)
    # добить серверными (которых нет у нас) до limit
    have = {str(i["name"]).lower() for i in items} | {name.lower()}
    for s in server_only:
        if len(items) >= limit:
            break
        if str(s["name"]).lower() not in have:
            items.append(s)
            have.add(str(s["name"]).lower())
    return {"items": items[:limit], "server_used": server_used}


@router.get("/duplicates")
def list_duplicates(limit: int = Query(200, le=1000), db: Session = Depends(get_db)):
    """Группы точных дублей (артист+название+длительность±2с, без live/remix-версий).

    keep_id — кого оставить (больше прослушиваний/starred/старше).
    """
    from app.services import dedup as _dd

    groups = _dd.find_duplicate_groups(db, limit_groups=limit)
    dup_tracks = sum(len(g["tracks"]) - 1 for g in groups)
    return {"groups": groups, "group_count": len(groups), "duplicate_tracks": dup_tracks}


@router.post("/duplicates/merge")
def merge_duplicates(payload: dict, db: Session = Depends(get_db)):
    """Сшить дубли вручную: {keep_id, drop_ids[]} — статистика суммируется,
    лайки/история/плейлисты/фичи переезжают на keep."""
    import uuid as _uuid

    from fastapi import HTTPException as _HE

    from app.services import dedup as _dd

    try:
        keep_id = str(payload.get("keep_id") or "")
        _uuid.UUID(keep_id)
        drop_ids = [str(x) for x in (payload.get("drop_ids") or []) if x]
        for d in drop_ids:
            _uuid.UUID(d)
    except (ValueError, AttributeError):
        raise _HE(400, "invalid ids")
    if not drop_ids:
        raise _HE(400, "drop_ids required")
    return _dd.merge_tracks(db, keep_id, drop_ids)


@router.post("/duplicates/auto")
def auto_merge_duplicates(db: Session = Depends(get_db)):
    """Автослияние ВСЕХ точных групп (то же, что чекбокс «автоматом»).

    Трогает только строгие совпадения; live/remix/edit-версии не сливаются никогда.
    """
    from app.services import dedup as _dd

    return {"ok": True, **_dd.auto_merge_exact(db)}


@router.get("/dedup-settings")
def get_dedup_settings(db: Session = Depends(get_db)):
    """Настройка автослияния дублей после сканирования."""
    from app.db.models import AppSetting

    row = db.get(AppSetting, "dedup")
    val = dict(row.value) if row and isinstance(row.value, dict) else {}
    return {"auto_merge": bool(val.get("auto_merge", False))}


@router.post("/dedup-settings")
def save_dedup_settings(payload: dict, db: Session = Depends(get_db)):
    from app.db.models import AppSetting

    value = {"auto_merge": bool((payload or {}).get("auto_merge", False))}
    row = db.get(AppSetting, "dedup")
    if row is None:
        db.add(AppSetting(key="dedup", value=value))
    else:
        row.value = value
    db.commit()
    return {"ok": True, **value}
