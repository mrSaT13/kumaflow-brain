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
from fastapi.responses import RedirectResponse

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
        "mood_tags": [t for t in lfm["tags"] if t.strip().lower() in MOOD_WORDS],
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


# --- дополнительные эндпоинты (как на VPS-патче + новые для треков/альбомов) ---

MOOD_WORDS = {
    "melancholic", "sad", "happy", "energetic", "calm", "aggressive", "chill",
    "dreamy", "dark", "uplifting", "romantic", "party", "summer", "winter",
    "acoustic", "electronic", "mellow", "moody", "introspective",
    "euphoric", "relaxing", "sentimental", "bright",
}


def _normalize_lfm_entity(payload: dict[str, Any]) -> dict[str, Any]:
    """Привести ответ track.getInfo/album.getInfo к единому виду."""
    ent = payload.get("track") or payload.get("album") or {}
    if not isinstance(ent, dict):
        ent = {}
    tags = ent.get("tags", {}).get("tag", []) if isinstance(ent.get("tags"), dict) else []
    if isinstance(tags, dict):
        tags = [tags]
    tag_names = [str(t.get("name", "")) for t in tags if isinstance(t, dict) and t.get("name")]
    similar = ent.get("similar", {}).get("track", []) if isinstance(ent.get("similar"), dict) else []
    if isinstance(similar, dict):
        similar = [similar]
    sim_names = [
        {"name": str(t.get("name", "")),
         "artist": str((t.get("artist") or {}).get("name", ""))
         if isinstance(t.get("artist"), dict) else str(t.get("artist") or "")}
        for t in similar if isinstance(t, dict) and t.get("name")
    ]
    wiki = ent.get("wiki") or ent.get("bio") or {}
    artist_val = ent.get("artist") or ""
    return {
        "name": str(ent.get("name", "")),
        "artist": str(artist_val.get("name", "")) if isinstance(artist_val, dict) else str(artist_val),
        "mbid": str(ent.get("mbid", "")),
        "tags": tag_names,
        "mood_tags": [t for t in tag_names if t.strip().lower() in MOOD_WORDS],
        "similar": sim_names[:30],
        "summary": str(wiki.get("summary", "")) if isinstance(wiki, dict) else "",
        "url": str(ent.get("url", "")),
    }


async def _lfm_entity_info(method: str, **params: str) -> dict[str, Any]:
    """Универсальный Last.fm-запрос (track.getInfo / album.getInfo)."""
    if not LASTFM_API_KEY:
        return {"tags": [], "mood_tags": [], "similar": [], "summary": "", "url": ""}
    params.update({"method": method, "api_key": LASTFM_API_KEY,
                   "format": "json", "autocorrect": "1"})
    await _throttle("lfm")
    async with _client() as c:
        r = await c.get(LFM_API, params=params)
        r.raise_for_status()
        return _normalize_lfm_entity(r.json())


@app.get("/api/bridge/cover")
async def cover(mbid: str = Query(..., min_length=1), size: int = Query(500, ge=100, le=1000)):
    """Обложка из Cover Art Archive по MusicBrainz release-group MBID."""
    return RedirectResponse(
        url=f"https://coverartarchive.org/release-group/{mbid}/front-{size}",
        status_code=302,
    )


@app.get("/api/bridge/mood")
async def mood(name: str = Query(..., min_length=1, max_length=256)):
    """Настроение артиста: фильтр тегов Last.fm по словарю MOOD_WORDS."""
    lfm = await _lfm_artist_info(name.strip())
    return {
        "artist": name,
        "mood_tags": [t for t in lfm["tags"] if t.strip().lower() in MOOD_WORDS],
        "all_tags": lfm["tags"],
        "bio_summary": lfm["bio"],
    }


@app.get("/api/bridge/tags")
async def tags(name: str = Query(..., min_length=1, max_length=256)):
    """Все теги артиста + похожие + био (без MusicBrainz)."""
    return await _lfm_artist_info(name.strip())


@app.get("/api/bridge/track")
async def track(
    artist: str = Query(..., min_length=1, max_length=256),
    title: str = Query(..., min_length=1, max_length=256),
):
    """Теги/настроение/похожие трека из Last.fm (аддитивный источник)."""
    key = f"track:{artist.strip().lower()}:{title.strip().lower()}"
    hit = _get_cache(key)
    if hit is not None:
        return {"cached": True, **hit}
    try:
        info = await _lfm_entity_info("track.getInfo", artist=artist.strip(), track=title.strip())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Last.fm unavailable: {e}")
    if not info.get("name"):
        raise HTTPException(status_code=404, detail="track not found")
    _set_cache(key, info)
    return {"cached": False, **info}


@app.get("/api/bridge/album")
async def album(
    album_name: str = Query(..., min_length=1, max_length=256, alias="album"),
    artist: str = Query(..., min_length=1, max_length=256),
):
    """Альбом: теги Last.fm + MBID + ссылка на обложку."""
    key = f"album:{artist.strip().lower()}:{album_name.strip().lower()}"
    hit = _get_cache(key)
    if hit is not None:
        return {"cached": True, **hit}
    try:
        info = await _lfm_entity_info("album.getInfo", artist=artist.strip(), album=album_name.strip())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Last.fm unavailable: {e}")
    if not info.get("name"):
        raise HTTPException(status_code=404, detail="album not found")
    info["cover_url"] = (
        f"https://coverartarchive.org/release-group/{info['mbid']}/front-500"
        if info.get("mbid") else ""
    )
    _set_cache(key, info)
    return {"cached": False, **info}
