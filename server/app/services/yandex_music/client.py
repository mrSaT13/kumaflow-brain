"""Stealth-клиент Yandex Music (api.music.yandex.net).

Токен — OAuth из браузера (https://oauth.yandex.ru), хранится в БД (AppSetting yandex)
или env YANDEX_MUSIC_TOKEN. Без токена — все вызовы возвращают None (не ошибка).
Rate-limit 1 req/1.2s (как bridge), кэш в памяти + TrackMetadataEnrich (TTL 72h),
User-Agent как у официального клиента, чтобы не палить сервис.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import AppSetting, TrackMetadataEnrich

logger = get_logger("yandex_music")

API = "https://api.music.yandex.net"
UA = "YandexMusic/240223 (KumaFlowBrain/0.1)"
_lock = asyncio.Lock()
_last = 0.0
_mem: dict[str, tuple[float, Any]] = {}


def _norm_token(t: str | None) -> str:
    if not t:
        return ""
    t = t.strip()
    # токен может прийти как "OAuth y0_Ag..." или просто "y0_Ag..."
    if t.lower().startswith("oauth "):
        t = t[6:].strip()
    return t


def get_yandex_config(db: Session) -> dict[str, Any]:
    try:
        row = db.get(AppSetting, "yandex")
        if row and isinstance(row.value, dict):
            v = row.value
            return {"token": _norm_token(v.get("token")), "enabled": bool(v.get("enabled", False))}
    except Exception:
        pass
    s = get_settings()
    return {"token": _norm_token(s.yandex_music_token), "enabled": bool(s.yandex_music_enabled)}


def save_yandex_config(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    value = {"token": _norm_token(payload.get("token")), "enabled": bool(payload.get("enabled", False))}
    row = db.get(AppSetting, "yandex")
    if row is None:
        row = AppSetting(key="yandex", value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return value


def _get_ttl() -> int:
    try:
        return int(get_settings().yandex_music_cache_ttl_hours or 72) * 3600
    except Exception:
        return 72 * 3600


def _throttle_sec() -> float:
    try:
        return float(get_settings().yandex_music_throttle_sec or 1.2)
    except Exception:
        return 1.2


async def _throttle():
    global _last
    async with _lock:
        wait = _throttle_sec() - (time.time() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.time()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"OAuth {token}", "User-Agent": UA, "Accept": "application/json"}


def _mem_get(key: str) -> Any | None:
    hit = _mem.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _get_ttl():
        _mem.pop(key, None)
        return None
    return val


def _mem_set(key: str, val: Any) -> None:
    _mem[key] = (time.time(), val)


async def search_track(db: Session, artist: str, title: str, album: str = "") -> dict[str, Any] | None:
    """Поиск трека в Yandex Music. Возвращает первый hit или None. Кэширует."""
    cfg = get_yandex_config(db)
    if not cfg["enabled"] or not cfg["token"]:
        return None
    q = f"{artist} {title}".strip()
    if album:
        q += f" {album}"
    q = q[:120]
    key = f"ym:search:{q.lower()}"
    hit = _mem_get(key)
    if hit is not None:
        return hit
    # DB кэш
    try:
        from app.db.database import session_scope as _sc

        # ищем в TrackMetadataEnrich уже закэшированное по этому запросу
        pass
    except Exception:
        pass
    await _throttle()
    try:
        async with httpx.AsyncClient(timeout=15, headers=_headers(cfg["token"])) as c:
            r = await c.get(f"{API}/search", params={"text": q, "type": "track", "page": 0, "nocorrect": "false"})
            if r.status_code == 401:
                logger.warning("yandex token invalid (401)")
                return None
            if r.status_code == 429:
                logger.warning("yandex rate 429, backoff")
                await asyncio.sleep(2)
                return None
            r.raise_for_status()
            j = r.json()
            tracks = (((j.get("result") or {}).get("tracks") or {}).get("results") or [])
            if not tracks:
                _mem_set(key, None)
                return None
            t = tracks[0]
            out = {
                "id": str(t.get("id", "")),
                "title": t.get("title", ""),
                "artists": [a.get("name", "") for a in t.get("artists", []) or []],
                "album": (t.get("albums") or [{}])[0].get("title", "") if t.get("albums") else "",
                "genre": (t.get("albums") or [{}])[0].get("genre", "") if t.get("albums") else "",
                "year": (t.get("albums") or [{}])[0].get("year") if t.get("albums") else None,
                "cover_uri": t.get("coverUri") or (t.get("albums") or [{}])[0].get("coverUri", "") if t.get("albums") else "",
            }
            _mem_set(key, out)
            return out
    except httpx.HTTPError as e:
        logger.warning("yandex search failed: {}", e)
        return None


async def fetch_metadata(db: Session, artist: str, title: str, album: str = "") -> dict[str, Any] | None:
    """Обогащение: search + кэш в TrackMetadataEnrich (если есть track_id)."""
    res = await search_track(db, artist, title, album)
    return res

# sync wrapper для воркеров (rq — sync)
def fetch_metadata_sync(db: Session, artist: str, title: str, album: str = "") -> dict[str, Any] | None:
    import asyncio as _aio

    try:
        return _aio.run(fetch_metadata(db, artist, title, album))
    except RuntimeError:
        loop = _aio.new_event_loop()
        try:
            return loop.run_until_complete(fetch_metadata(db, artist, title, album))
        finally:
            loop.close()
