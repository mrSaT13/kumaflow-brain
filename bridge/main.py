"""KumaFlow Bridge — кэширующий шлюз к MusicBrainz и Last.fm.

Зачем: внешние API имеют жёсткие лимиты (MusicBrainz ~1 req/s, Last.fm ~5 req/s).
Мост держит лимиты сам, кэширует ответы в памяти с TTL и отдаёт серверу
генерации единый нормализованный формат, изолируя его от изменений внешних API.

Статус: заготовка v0. Хранилище — in-memory (переживает только рестарт).
Позже: Redis-кэш + таблица artist_metadata на стороне backend.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query

APP_VERSION = "0.1.0"

# --- env ---
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()
USER_AGENT = os.getenv(
    "BRIDGE_USER_AGENT", "KumaFlowBridge/0.1 ( https://github.com/mrSaT13/kumaflow-brain )"
).strip()
CACHE_TTL_SEC = int(os.getenv("BRIDGE_CACHE_TTL_HOURS", "24")) * 3600
MB_MIN_INTERVAL = 1.1   # MusicBrainz: не чаще ~1 req/s
LFM_MIN_INTERVAL = 0.25  # Last.fm: не чаще ~5 req/s

MB_API = "https://musicbrainz.org/ws/2"
LFM_API = "http://ws.audioscrobbler.com/2.0/"

app = FastAPI(title="KumaFlow Bridge", version=APP_VERSION)

_cache: dict[str, tuple[float, Any]] = {}
_mb_lock = asyncio.Lock()
_lfm_lock = asyncio.Lock()
_mb_last = 0.0
_lfm_last = 0.0


def _get_cache(key: str) -> Any | None:
    hit = _cache.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > CACHE_TTL_SEC:
        _cache.pop(key, None)
        return None
    return value


def _set_cache(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


async def _throttle(which: str) -> None:
    """Простой rate-limiter: минимальный интервал между запросами к провайдеру."""
    global _mb_last, _lfm_last
    if which == "mb":
        async with _mb_lock:
            wait = MB_MIN_INTERVAL - (time.time() - _mb_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _mb_last = time.time()
    else:
        async with _lfm_lock:
            wait = LFM_MIN_INTERVAL - (time.time() - _lfm_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _lfm_last = time.time()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


async def _mb_search_artist(name: str) -> dict[str, Any] | None:
    await _throttle("mb")
    async with _client() as c:
        r = await c.get(f"{MB_API}/artist", params={"query": f'artist:"{name}"', "fmt": "json", "limit": 1})
        r.raise_for_status()
        artists = r.json().get("artists", [])
        return artists[0] if artists else None


async def _lfm_artist_info(name: str, mbid: str = "") -> dict[str, Any]:
    """Теги, похожие артисты, био. Без ключа возвращает пустую структуру (не ошибка)."""
    if not LASTFM_API_KEY:
        return {"tags": [], "similar": [], "bio": "", "url": ""}
    params: dict[str, str] = {
        "method": "artist.getinfo",
        "artist": name,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "autocorrect": "1",
    }
    if mbid:
        params["mbid"] = mbid
    await _throttle("lfm")
    async with _client() as c:
        r = await c.get(LFM_API, params=params)
        r.raise_for_status()
        info = r.json().get("artist", {})
        tags = [t["name"] for t in info.get("tags", {}).get("tag", []) if t.get("name")]
        similar = [
            {"name": a.get("name", ""), "url": a.get("url", "")}
            for a in info.get("similar", {}).get("artist", [])
        ]
        return {
            "tags": tags,
            "similar": similar,
            "bio": (info.get("bio", {}) or {}).get("summary", ""),
            "url": info.get("url", ""),
        }


@app.get("/api/bridge/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "providers": {
            "musicbrainz": True,
            "lastfm": bool(LASTFM_API_KEY),
        },
        "cache_entries": len(_cache),
    }


@app.get("/api/bridge/artist")
async def artist(name: str = Query(..., min_length=1, max_length=256)) -> dict[str, Any]:
    """Нормализованные метаданные артиста. Единый формат для сервера генерации."""
    key = f"artist:{name.strip().lower()}"
    hit = _get_cache(key)
    if hit is not None:
        return {"cached": True, **hit}
    try:
        mb = await _mb_search_artist(name.strip())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"MusicBrainz unavailable: {e}")
    if not mb:
        raise HTTPException(status_code=404, detail="artist not found")
    mbid = mb.get("id", "")
    try:
        lfm = await _lfm_artist_info(mb.get("name", name), mbid)
    except httpx.HTTPError:
        lfm = {"tags": [], "similar": [], "bio": "", "url": ""}
    result = {
        "name": mb.get("name", name),
        "mbid": mbid,
        "country": mb.get("country", ""),
        "type": mb.get("type", ""),
        "tags": lfm["tags"],
        "similar_artists": lfm["similar"],
        "bio_summary": lfm["bio"],
        "lastfm_url": lfm["url"],
    }
    _set_cache(key, result)
    return {"cached": False, **result}


@app.get("/api/bridge/release")
async def release(
    artist: str = Query(..., min_length=1, max_length=256),
    album: str = Query(..., min_length=1, max_length=256),
) -> dict[str, Any]:
    """Поиск релиза в MusicBrainz, первый лучший матч."""
    key = f"release:{artist.strip().lower()}:{album.strip().lower()}"
    hit = _get_cache(key)
    if hit is not None:
        return {"cached": True, **hit}
    try:
        await _throttle("mb")
        async with _client() as c:
            r = await c.get(
                f"{MB_API}/release-group",
                params={"query": f'artist:"{artist}" AND release:"{album}"', "fmt": "json", "limit": 1},
            )
            r.raise_for_status()
            groups = r.json().get("release-groups", [])
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"MusicBrainz unavailable: {e}")
    if not groups:
        raise HTTPException(status_code=404, detail="release not found")
    g = groups[0]
    result = {
        "title": g.get("title", album),
        "mbid": g.get("id", ""),
        "primary_type": g.get("primary-type", ""),
        "first_release_date": g.get("first-release-date", ""),
    }
    _set_cache(key, result)
    return {"cached": False, **result}
