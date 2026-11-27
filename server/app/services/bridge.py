from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import AppSetting

logger = get_logger("bridge")


def _norm_url(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip().rstrip("/")
    if not u.startswith("http"):
        u = "http://" + u
    return u


def get_bridge_config(db: Session) -> dict[str, Any]:
    """Сначала AppSetting.bridge (сохранено из веб-UI), иначе fallback на env."""
    row = db.get(AppSetting, "bridge")
    if row and isinstance(row.value, dict):
        v = row.value
        return {
            "url": _norm_url(v.get("url")),
            "enabled": bool(v.get("enabled", False)),
        }
    s = get_settings()
    return {"url": _norm_url(s.bridge_url), "enabled": bool(s.bridge_enabled)}


def save_bridge_config(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    value = {
        "url": _norm_url(payload.get("url")),
        "enabled": bool(payload.get("enabled", False)),
    }
    row = db.get(AppSetting, "bridge")
    if row is None:
        row = AppSetting(key="bridge", value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return value


async def bridge_health(url: str) -> dict[str, Any]:
    """Пинг моста. Бросает исключение при недоступности — ловит вызывающий код."""
    target = f"{url.rstrip('/')}/api/bridge/health"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(target)
        r.raise_for_status()
        return r.json()


async def fetch_artist(db: Session, name: str) -> dict[str, Any] | None:
    """Метаданные артиста через мост. None = мост выключен/недоступен (не ошибка)."""
    cfg = get_bridge_config(db)
    if not cfg["enabled"] or not cfg["url"]:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{cfg['url']}/api/bridge/artist", params={"name": name})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001 — мост опционален, backend не должен падать
        logger.warning("bridge unavailable ({}), skipping enrichment", e)
        return None


# --- Метаданные артистов в БД (аддитивный источник) ---
#
# Мост — внешний сервис с лимитами MusicBrainz ~1 запрос/с, поэтому ответы
# кэшируются в artist_metadata на неделю и подмешиваются в рекомендации
# только как ДОПОЛНЕНИЕ к локальным сигналам. Мост выключен/недоступен —
# все новые функции просто возвращают None/пусто, текущая логика не меняется.

ENRICH_TTL_DAYS = 7


def artist_key(name: str | None) -> str:
    """Канонический ключ артиста: «AC/DC» == «ac dc» == «AC-DC»."""
    from app.services.library_sync import canonical_key

    return canonical_key(name)


def meta_to_dict(row) -> dict[str, Any]:
    """Строка БД → плоский словарь для API/UI. Источник и дата — всегда."""
    return {
        "name": str(row.name),
        "name_key": str(row.name_key),
        "source": str(row.source or "bridge"),
        "mbid": str(row.mbid or ""),
        "country": str(row.country or ""),
        "artist_type": str(row.artist_type or ""),
        "tags": list(row.tags or []),
        "mood_tags": list(row.mood_tags or []),
        "similar_artists": list(row.similar_artists or []),
        "bio_summary": str(row.bio_summary or ""),
        "lastfm_url": str(row.lastfm_url or ""),
        "cover_url": str(row.cover_url or ""),
        "synced_at": row.fetched_at.isoformat() + "Z" if row.fetched_at else None,
        "stale": bool(row.fail_count),
    }


def get_cached_meta(db: Session, name: str | None):
    """Строка artist_metadata по имени (без внешних вызовов)."""
    from app.db.models import ArtistMetadata

    key = artist_key(name)
    if not key:
        return None
    try:
        return db.get(ArtistMetadata, key)
    except Exception:
        return None


def cached_meta_map(db: Session, names: list[str] | set[str]) -> dict[str, dict[str, Any]]:
    """Пачка метаданных одним запросом: ключ → словарь. Для рекомендаций/API."""
    from app.db.models import ArtistMetadata

    keys = {artist_key(n) for n in names if n}
    if not keys:
        return {}
    try:
        rows = db.query(ArtistMetadata).filter(ArtistMetadata.name_key.in_(keys)).all()
        return {r.name_key: meta_to_dict(r) for r in rows}
    except Exception:
        return {}


def _fetch_sync(url: str, name: str) -> dict[str, Any] | None:
    """Синхронный GET к мосту (для воркеров/синхронных сервисов)."""
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{url.rstrip('/')}/api/bridge/artist", params={"name": name})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("bridge sync fetch failed for {}: {}", name, e)
        return None


def _similar_names(data: dict[str, Any]) -> list[str]:
    """similar_artists приходит списком словарей (Last.fm) или строк."""
    out: list[str] = []
    for a in data.get("similar_artists") or []:
        n = (a.get("name") if isinstance(a, dict) else a) or ""
        n = str(n).strip()
        if n and n not in out:
            out.append(n)
    return out[:30]


def enrich_artist(db: Session, name: str, force: bool = False) -> dict[str, Any] | None:
    """Подтянуть и сохранить метаданные одного артиста.

    Никогда не бросает исключение наружу: мост опционален. При ошибке
    обновляет fail_count/fetched_at (чтобы не долбить внешний API каждый
    запрос) и отдаёт старые данные, если они уже есть в базе.
    """
    from app.db.models import ArtistMetadata

    cfg = get_bridge_config(db)
    if not cfg["enabled"] or not cfg["url"]:
        return None
    key = artist_key(name)
    if not key:
        return None
    row = db.get(ArtistMetadata, key)
    if not force and row and row.fetched_at:
        age = datetime.utcnow() - row.fetched_at
        if age < timedelta(days=ENRICH_TTL_DAYS):
            return meta_to_dict(row)
    data = _fetch_sync(cfg["url"], name)
    try:
        if data is None:
            if row is not None:
                row.fail_count = int(row.fail_count or 0) + 1
                row.last_error = "artist not found in bridge (404)"
                row.fetched_at = datetime.utcnow()
                db.commit()
                return meta_to_dict(row)
            return None
        if row is None:
            row = ArtistMetadata(name_key=key, name=str(data.get("name") or name))
            db.add(row)
        row.name = str(data.get("name") or row.name or name)
        row.source = "bridge"
        row.mbid = str(data.get("mbid") or "") or None
        row.country = str(data.get("country") or "") or None
        row.artist_type = str(data.get("type") or data.get("artist_type") or "") or None
        row.tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()][:60]
        row.mood_tags = [str(t) for t in (data.get("mood_tags") or []) if str(t).strip()][:30]
        row.similar_artists = _similar_names(data)
        row.bio_summary = str(data.get("bio_summary") or data.get("bio") or "") or None
        row.lastfm_url = str(data.get("lastfm_url") or "") or None
        row.cover_url = str(data.get("cover_url") or "") or None
        row.fetched_at = datetime.utcnow()
        row.fail_count = 0
        row.last_error = None
        db.commit()
        return meta_to_dict(row)
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
            if row is not None:
                row.fail_count = int(row.fail_count or 0) + 1
                row.last_error = str(e)[:500]
                row.fetched_at = datetime.utcnow()
                db.commit()
                return meta_to_dict(row)
        except Exception:
            db.rollback()
        return None


def enrich_batch(db: Session, names: list[str], limit: int = 25) -> dict[str, Any]:
    """Обогатить до `limit` артистов (только тех, кто уже есть в библиотеке)."""
    cfg = get_bridge_config(db)
    if not cfg["enabled"] or not cfg["url"]:
        return {"ok": False, "error": "bridge disabled", "fetched": 0, "cached": 0,
                "failed": 0, "total_meta": _meta_count(db)}
    stats = {"ok": True, "fetched": 0, "cached": 0, "failed": 0}
    seen: set[str] = set()
    for name in names:
        key = artist_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        if len(seen) > max(1, min(int(limit or 25), 200)):
            break
        try:
            before = get_cached_meta(db, name)
            fresh = bool(before and before.fetched_at and
                         datetime.utcnow() - before.fetched_at < timedelta(days=ENRICH_TTL_DAYS))
            res = enrich_artist(db, name)
            if fresh:
                stats["cached"] += 1
            elif res:
                stats["fetched"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1
            try:
                db.rollback()
            except Exception:
                pass
    stats["total_meta"] = _meta_count(db)
    return stats


def _meta_count(db: Session) -> int:
    from app.db.models import ArtistMetadata

    try:
        return int(db.query(ArtistMetadata).count())
    except Exception:
        return 0


def library_artist_names(db: Session, limit: int | None = None) -> list[str]:
    """Имена артистов из библиотеки (по частоте треков) — для пакетного обогащения."""
    from sqlalchemy import func

    from app.db.models import Track

    q = (db.query(Track.artist_name, func.count(Track.id))
         .filter(Track.artist_name.isnot(None))
         .group_by(Track.artist_name)
         .order_by(func.count(Track.id).desc()))
    if limit:
        q = q.limit(max(1, int(limit)))
    return [str(n) for (n, _) in q.all() if n]


def bridge_enrich_job(limit: int = 25) -> dict[str, Any]:
    """Крон-задача: обновить метаданные артистов (маленькими пачками)."""
    from app.db.database import session_scope

    with session_scope() as db:
        cfg = get_bridge_config(db)
        if not cfg["enabled"] or not cfg["url"]:
            return {"ok": False, "error": "bridge disabled", "fetched": 0}
        # Берём больше имён, чем лимит: часть уже свежая и будет пропущена.
        names = library_artist_names(db, limit=max(50, int(limit) * 4))
        stats = enrich_batch(db, names, limit=limit)
        # Треки/альбомы — тоже аддитивно, малыми пачками. Last.fm разрешает
        # ~5 req/s, поэтому это недорого; при отсутствии /track,/album просто
        # ничего не сохранится (старый мост продолжает работать как раньше).
        try:
            stats["tracks"] = enrich_tracks_batch(db, limit=limit)
        except Exception as e:
            stats["tracks"] = {"error": str(e)[:200]}
        try:
            stats["albums"] = enrich_albums_batch(db, limit=max(5, limit // 2))
        except Exception as e:
            stats["albums"] = {"error": str(e)[:200]}
        logger.info("bridge enrich: {}", stats)
        return stats


# --- Треки и альбомы: та же корзина TrackMetadataEnrich (без миграций) ---
#
# source="bridge_track"  — теги/настроение трека из Last.fm;
# source="bridge_album"  — релиз MusicBrainz + обложка + теги альбома.
# Оба записаны рядом с уникальным треком-представителем и отдаются
# /api/tracks/{id} в уже существующем поле metadata.

TRACK_SRC = "bridge_track"
ALBUM_SRC = "bridge_album"

# Теги Last.fm → канонические коды настроений мозга (wave.MOOD_LABELS).
# Неизвестные слова отбрасываются: пилюля не должна появляться из мусора.
MOOD_TAG_TO_CODE = {
    "melancholic": "melancholic", "sad": "sad", "happy": "happy",
    "energetic": "energetic", "calm": "calm", "aggressive": "aggressive",
    "chill": "chill", "dreamy": "dreamy", "dark": "dark",
    "uplifting": "happy", "romantic": "romantic", "party": "dance",
    "summer": "happy", "winter": "calm", "acoustic": "warm",
    "electronic": "energetic", "mellow": "relaxed", "moody": "dark",
    "introspective": "melancholic", "euphoric": "happy",
    "relaxing": "relaxed", "sentimental": "romantic", "bright": "happy",
}


def mood_codes(tags: list[str] | None) -> list[str]:
    """Теги моста → валидные канонические коды настроений (без дублей)."""
    if not tags:
        return []
    try:
        from app.services.wave import norm_mood
    except Exception:
        return []
    out: list[str] = []
    for t in tags:
        code = MOOD_TAG_TO_CODE.get(str(t).strip().lower())
        if not code:
            continue
        try:
            code = norm_mood(code) or code
        except Exception:
            pass
        if code not in out:
            out.append(code)
    return out


def _get_enrich(db: Session, track_id: str, source: str):
    from app.db.models import TrackMetadataEnrich

    try:
        return db.get(TrackMetadataEnrich, (str(track_id), source))
    except Exception:
        return None


def _enrich_payload(data: dict[str, Any], source: str, ok: bool,
                    err: str = "") -> dict[str, Any]:
    """Единая форма записи: источник/дата/статус всегда присутствуют."""
    from datetime import datetime

    out = dict(data or {})
    out.update({
        "source": source,
        "synced_at": datetime.utcnow().isoformat() + "Z",
        "stale": not ok,
    })
    if err:
        out["error"] = str(err)[:300]
    return out


def _save_enrich(db: Session, track_id: str, source: str,
                 data: dict[str, Any], ok: bool, err: str = "") -> dict[str, Any]:
    from app.db.models import TrackMetadataEnrich

    row = _get_enrich(db, track_id, source)
    payload = _enrich_payload(data, source, ok, err)
    if row is None:
        row = TrackMetadataEnrich(track_id=str(track_id), source=source, data=payload)
        db.add(row)
    else:
        row.data = payload
        row.fetched_at = datetime.utcnow()
    db.commit()
    return payload


def _fresh(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    try:
        ts = str(payload.get("synced_at") or "").replace("Z", "")
        if not ts:
            return False
        from datetime import datetime

        return datetime.utcnow() - datetime.fromisoformat(ts) < timedelta(days=ENRICH_TTL_DAYS)
    except Exception:
        return False


def _fetch_entity_sync(url: str, endpoint: str, **params: str) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{url.rstrip('/')}/api/bridge/{endpoint}", params=params)
            if r.status_code in (404, 501):
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("bridge {}: недоступен/не реализован ({})", endpoint, e)
        return None


def enrich_track(db: Session, track, force: bool = False) -> dict[str, Any] | None:
    """Теги/настроение одного трека. Мост без /track → None (не ошибка)."""
    cfg = get_bridge_config(db)
    if not cfg["enabled"] or not cfg["url"] or not getattr(track, "artist_name", None):
        return None
    tid = str(track.id)
    old = _get_enrich(db, tid, TRACK_SRC)
    old_data = old.data if old else None
    if not force and _fresh(old_data):
        return old_data
    data = _fetch_entity_sync(cfg["url"], "track",
                              artist=str(track.artist_name), title=str(track.title or ""))
    if data is None:
        if old_data:
            return _save_enrich(db, tid, TRACK_SRC, old_data, False, "bridge unavailable")
        return None
    tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()][:40]
    payload = {
        "name": str(data.get("name") or track.title or ""),
        "artist": str(data.get("artist") or track.artist_name or ""),
        "mbid": str(data.get("mbid") or ""),
        "tags": tags,
        "mood_tags": [str(t) for t in (data.get("mood_tags") or []) if str(t).strip()][:20],
        "mood_codes": mood_codes(data.get("mood_tags") or tags),
        "similar": data.get("similar") or [],
        "summary": str(data.get("summary") or ""),
        "url": str(data.get("url") or ""),
    }
    return _save_enrich(db, tid, TRACK_SRC, payload, True)


def enrich_album(db: Session, album, representative_track_id: str,
                 force: bool = False) -> dict[str, Any] | None:
    """Релиз/обложка альбома. Мост без /album → None (не ошибка)."""
    cfg = get_bridge_config(db)
    if not cfg["enabled"] or not cfg["url"]:
        return None
    tid = str(representative_track_id)
    old = _get_enrich(db, tid, ALBUM_SRC)
    old_data = old.data if old else None
    if not force and _fresh(old_data):
        return old_data
    artist = str(getattr(album, "artist_name", "") or "")
    title = str(getattr(album, "title", "") or "")
    if not artist or not title:
        return None
    data = _fetch_entity_sync(cfg["url"], "album", artist=artist, album=title)
    if data is None:
        if old_data:
            return _save_enrich(db, tid, ALBUM_SRC, old_data, False, "bridge unavailable")
        return None
    tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()][:40]
    payload = {
        "name": str(data.get("name") or title),
        "artist": artist,
        "title": title,
        "mbid": str(data.get("mbid") or ""),
        "primary_type": str(data.get("primary_type") or ""),
        "first_release_date": str(data.get("first_release_date") or ""),
        "cover_url": str(data.get("cover_url") or ""),
        "tags": tags,
        "mood_tags": [str(t) for t in (data.get("mood_tags") or []) if str(t).strip()][:20],
        "mood_codes": mood_codes(data.get("mood_tags") or tags),
        "url": str(data.get("url") or ""),
    }
    return _save_enrich(db, tid, ALBUM_SRC, payload, True)


def country_map(db: Session, track_ids: list[str]) -> dict[str, str]:
    """track_id → страна артиста из моста (для фильтра/сортировки в плеере)."""
    if not track_ids:
        return {}
    from app.db.models import ArtistMetadata, Track

    try:
        meta = {
            artist_key(str(r.name or "")): str(r.country or "").upper()
            for r in db.query(ArtistMetadata).filter(
                ArtistMetadata.country.isnot(None), ArtistMetadata.country != "").all()
        }
    except Exception:
        return {}
    if not meta:
        return {}
    out: dict[str, str] = {}
    try:
        for i in range(0, len(track_ids), 500):
            for tid, aname in db.query(Track.id, Track.artist_name).filter(
                    Track.id.in_(track_ids[i:i + 500])).all():
                c = meta.get(artist_key(str(aname or "")))
                if c:
                    out[str(tid)] = c
    except Exception:
        pass
    return out


def countries_catalog(db: Session) -> list[dict[str, object]]:
    """Список стран из сохранённых метаданных артистов: [{code, artists}]."""
    from sqlalchemy import func

    from app.db.models import ArtistMetadata

    try:
        rows = (db.query(ArtistMetadata.country, func.count(ArtistMetadata.name_key))
                .filter(ArtistMetadata.country.isnot(None), ArtistMetadata.country != "")
                .group_by(ArtistMetadata.country)
                .order_by(func.count(ArtistMetadata.name_key).desc()).all())
        return [{"code": str(c).upper(), "artists": int(n)} for c, n in rows if c]
    except Exception:
        return []


def bridge_mood_map(db: Session, track_ids: list[str]) -> dict[str, list[str]]:
    """track_id → коды настроений из моста (для фильтра волны/cold-start)."""
    if not track_ids:
        return {}
    from app.db.models import TrackMetadataEnrich

    out: dict[str, list[str]] = {}
    try:
        for i in range(0, len(track_ids), 500):
            rows = (db.query(TrackMetadataEnrich)
                    .filter(TrackMetadataEnrich.source == TRACK_SRC,
                            TrackMetadataEnrich.track_id.in_(track_ids[i:i + 500]))
                    .all())
            for r in rows:
                codes = list((r.data or {}).get("mood_codes") or [])
                if codes and not (r.data or {}).get("stale"):
                    out[str(r.track_id)] = codes
    except Exception:
        pass
    return out


def enrich_tracks_batch(db: Session, limit: int = 25) -> dict[str, int]:
    """Сначала треки БЕЗ аудио-анализа — там фолбэк нужен больше всего."""
    from sqlalchemy import or_

    from app.db.models import Track, TrackFeatures

    n = max(1, min(int(limit or 25), 100))
    stats = {"fetched": 0, "cached": 0, "failed": 0}
    q = (db.query(Track)
         .outerjoin(TrackFeatures, TrackFeatures.track_id == Track.id)
         .filter(Track.artist_name.isnot(None),
                 or_(TrackFeatures.track_id.is_(None),
                     TrackFeatures.mood_labels.is_(None)))
         .order_by(Track.play_count.desc().nullslast()).limit(n * 3))
    tracks = q.all()
    for t in tracks:
        if stats["fetched"] + stats["failed"] >= n:
            break
        old = _get_enrich(db, str(t.id), TRACK_SRC)
        if _fresh(old.data if old else None):
            stats["cached"] += 1
            continue
        res = enrich_track(db, t)
        if res:
            stats["fetched"] += 1
        else:
            stats["failed"] += 1
    return stats


def enrich_albums_batch(db: Session, limit: int = 10) -> dict[str, int]:
    from sqlalchemy import func

    from app.db.models import Album, Track

    n = max(1, min(int(limit or 10), 50))
    stats = {"fetched": 0, "cached": 0, "failed": 0}
    albums = (db.query(Album)
              .filter(Album.artist_name.isnot(None), Album.title.isnot(None))
              .order_by(Album.title.asc()).limit(n * 4).all())
    for a in albums:
        if stats["fetched"] + stats["failed"] >= n:
            break
        rep = db.query(Track.id).filter(Track.album_external_id == a.external_id).first()
        if rep is None:
            continue
        old = _get_enrich(db, str(rep[0]), ALBUM_SRC)
        if _fresh(old.data if old else None):
            stats["cached"] += 1
            continue
        res = enrich_album(db, a, str(rep[0]))
        if res:
            stats["fetched"] += 1
        else:
            stats["failed"] += 1
    return stats
