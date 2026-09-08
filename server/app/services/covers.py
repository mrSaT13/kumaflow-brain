"""Кэш обложек артистов/альбомов из Navidrome.

Subsonic API: /rest/getCoverArt?id={artistId|albumId|songId}&size=300
Возвращает бинарные данные (jpg/png). Мы проксируем через наш бэк и кэшируем
на диск: /server/data/covers/{id}.{ext}
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.navidrome.client import SubsonicClient, SubsonicAuth

logger = get_logger("covers")


CACHE_DIR = Path(os.environ.get("KUMAFLOW_COVER_CACHE", "./data/covers"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _ext_from(content_type: str | None) -> str:
    if not content_type:
        return "jpg"
    ct = content_type.lower()
    if "png" in ct:
        return "png"
    if "webp" in ct:
        return "webp"
    return "jpg"


def _path_for(cover_id: str) -> Path:
    h = hashlib.sha1(cover_id.encode("utf-8")).hexdigest()
    p = CACHE_DIR / f"{h}.bin"
    return p


def _ext_path_for(cover_id: str) -> Path:
    h = hashlib.sha1(cover_id.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.ext"


def cached_path(cover_id: str) -> Optional[Path]:
    p = _path_for(cover_id)
    if p.exists():
        return p
    return None


def get_cover(cover_id: str, size: int = 300) -> bytes | None:
    """Синхронная версия (для API proxy). Кэширует."""
    cached = cached_path(cover_id)
    if cached is not None:
        return cached.read_bytes()
    s = get_settings()
    if s.media_server_type != "navidrome" or not s.navidrome_url:
        return None
    auth = SubsonicAuth(user=s.navidrome_user, password=s.navidrome_password)
    url = f"{s.navidrome_url.rstrip('/')}/rest/getCoverArt"
    salt = __import__("secrets").token_hex(6)
    token = hashlib.md5(f"{s.navidrome_password}{salt}".encode()).hexdigest()
    params = {
        "u": s.navidrome_user,
        "v": "1.16.1",
        "c": "KumaFlowBrain",
        "f": "json",
        "t": token,
        "s": salt,
        "id": cover_id,
        "size": str(size),
    }
    try:
        r = httpx.get(url, params=params, timeout=20.0)
    except httpx.HTTPError as e:
        logger.warning("cover fetch error: {}", e)
        return None
    if r.status_code != 200 or not r.content:
        return None
    p = _path_for(cover_id)
    p.write_bytes(r.content)
    _ext_path_for(cover_id).write_text(_ext_from(r.headers.get("content-type")))
    return r.content


def cached_content_type(cover_id: str) -> str:
    p = _ext_path_for(cover_id)
    if p.exists():
        ct = p.read_text().strip()
        return {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ct, "image/jpeg")
    return "image/jpeg"


async def prefetch_artist_covers(artist_ids: list[str]) -> int:
    """Параллельно дёргает обложки артистов из Navidrome и кэширует."""
    s = get_settings()
    if s.media_server_type != "navidrome" or not s.navidrome_url:
        return 0
    auth = SubsonicAuth(user=s.navidrome_user, password=s.navidrome_password)

    async with SubsonicClient(s.navidrome_url, auth) as client:
        sem = asyncio.Semaphore(4)

        async def one(aid: str) -> bool:
            async with sem:
                try:
                    # нет прямого бинарного метода в нашем клиенте — используем httpx
                    salt = __import__("secrets").token_hex(6)
                    token = hashlib.md5(
                        f"{s.navidrome_password}{salt}".encode()
                    ).hexdigest()
                    params = {
                        "u": s.navidrome_user,
                        "v": "1.16.1",
                        "c": "KumaFlowBrain",
                        "f": "json",
                        "t": token,
                        "s": salt,
                        "id": aid,
                        "size": "300",
                    }
                    r = await client._client.get(
                        f"{s.navidrome_url.rstrip('/')}/rest/getCoverArt",
                        params=params,
                    )
                    if r.status_code == 200 and r.content:
                        p = _path_for(aid)
                        p.write_bytes(r.content)
                        _ext_path_for(aid).write_text(_ext_from(r.headers.get("content-type")))
                        return True
                except Exception as e:  # noqa: BLE001
                    logger.debug("prefetch {} failed: {}", aid, e)
            return False

        results = await asyncio.gather(*[one(a) for a in artist_ids])
        return sum(1 for x in results if x)
