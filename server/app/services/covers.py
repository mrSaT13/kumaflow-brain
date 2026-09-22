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


def _path_for(cover_id: str, size: int | None = None) -> Path:
    h = hashlib.sha1(cover_id.encode("utf-8")).hexdigest()
    if size:
        return CACHE_DIR / f"{h}_{size}.bin"
    return CACHE_DIR / f"{h}.bin"


def _ext_path_for(cover_id: str, size: int | None = None) -> Path:
    h = hashlib.sha1(cover_id.encode("utf-8")).hexdigest()
    if size:
        return CACHE_DIR / f"{h}_{size}.ext"
    return CACHE_DIR / f"{h}.ext"


def _is_image_bytes(data: bytes) -> bool:
    if not data or len(data) < 8:
        return False
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return True
    if data.startswith(b"RIFF") and b"WEBP" in data[:12]:
        return True
    if data.startswith(b"BM"):
        return True
    return False


def clear_expired(ttl_days: int = 7, max_mb: int = 2048) -> dict:
    """Очистка старых/лишних как на mobile SafeManager 400/7d."""
    import time

    now = time.time()
    ttl = ttl_days * 86400
    files = [p for p in CACHE_DIR.glob("*.bin")]
    removed = 0
    # TTL
    for p in files:
        try:
            if now - p.stat().st_mtime > ttl:
                p.unlink(missing_ok=True)
                (CACHE_DIR / f"{p.stem}.ext").unlink(missing_ok=True)
                removed += 1
        except Exception:
            pass
    # size cap LRU
    files = [p for p in CACHE_DIR.glob("*.bin")]
    total = sum(p.stat().st_size for p in files if p.exists())
    if total > max_mb * 1024 * 1024:
        files.sort(key=lambda pp: pp.stat().st_mtime)
        for p in files:
            if total <= max_mb * 1024 * 1024:
                break
            try:
                sz = p.stat().st_size
                p.unlink(missing_ok=True)
                (CACHE_DIR / f"{p.stem}.ext").unlink(missing_ok=True)
                total -= sz
                removed += 1
            except Exception:
                pass
    return {"removed": removed, "remaining": len(list(CACHE_DIR.glob("*.bin")))}


def cached_path(cover_id: str) -> Optional[Path]:
    p = _path_for(cover_id)
    if p.exists():
        return p
    return None


def _auth_params(user: str, password: str) -> dict[str, str]:
    salt = __import__("secrets").token_hex(6)
    token = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
    return {
        "u": user,
        "v": "1.16.1",
        "c": "KumaFlowBrain",
        "f": "json",
        "t": token,
        "s": salt,
    }


def get_cover(cover_id: str, size: int = 300, cfg: dict | None = None) -> bytes | None:
    """Синхронная версия (для API proxy). Кэширует per-size, валидирует magic bytes."""
    # пробуем size-specific, затем legacy без size
    for cand in (_path_for(cover_id, size), _path_for(cover_id)):
        if cand.exists():
            try:
                data = cand.read_bytes()
                if not _is_image_bytes(data):
                    cand.unlink(missing_ok=True)
                    continue
                return data
            except Exception:
                pass
    if cfg is None:
        cfg = _load_cfg()
    url = (cfg.get("url") or "").rstrip("/")
    user = (cfg.get("user") or "").strip()
    password = cfg.get("password") or ""
    if not url or not user:
        return None
    params = _auth_params(user, password)
    params.update({"id": cover_id, "size": str(size)})
    try:
        r = httpx.get(f"{url}/rest/getCoverArt", params=params, timeout=20.0)
    except httpx.HTTPError as e:
        logger.warning("cover fetch error: {}", e)
        return None
    if r.status_code != 200 or not r.content:
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if "json" in ctype or "xml" in ctype:
        return None
    if not _is_image_bytes(r.content):
        return None
    p = _path_for(cover_id, size)
    p.write_bytes(r.content)
    _ext_path_for(cover_id, size).write_text(_ext_from(r.headers.get("content-type")))
    # также кладём legacy для совместимости
    legacy = _path_for(cover_id)
    if not legacy.exists():
        try:
            legacy.write_bytes(r.content)
            _ext_path_for(cover_id).write_text(_ext_from(r.headers.get("content-type")))
        except Exception:
            pass
    return r.content


def _load_cfg() -> dict:
    """Конфиг медиа-сервера: сначала БД (веб-UI), иначе env."""
    try:
        from app.db.database import session_scope
        from app.services.media_server import get_media_server_config

        with session_scope() as db:
            return dict(get_media_server_config(db))
    except Exception:
        pass
    s = get_settings()
    return {
        "type": s.media_server_type,
        "url": s.navidrome_url,
        "user": s.navidrome_user,
        "password": s.navidrome_password,
        "token": "",
    }


def get_disk_cover(track_path: str | None) -> bytes | None:
    """Встроенная обложка из аудиофайла (для треков с диска)."""
    if not track_path or not os.path.isfile(track_path):
        return None
    try:
        from mutagen import File as _MutagenFile

        audio = _MutagenFile(track_path)
    except Exception:
        return None
    if audio is None:
        return None
    try:
        # MP3/APIC, FLAC/MP4 covr, Vorbis METADATA_BLOCK_PICTURE
        if hasattr(audio, "tags") and audio.tags:
            for key in ("APIC:", "APIC:Cover", "covr", "METADATA_BLOCK_PICTURE"):
                for k in list(audio.tags.keys()):
                    if k == key or k.startswith("APIC"):
                        v = audio.tags[k]
                        vals = v if isinstance(v, list) else [v]
                        for item in vals:
                            data = getattr(item, "data", None)
                            if data:
                                return bytes(data)
                            if isinstance(item, (bytes, bytearray)):
                                # base64 picture block (vorbis)
                                import base64

                                try:
                                    raw = base64.b64decode(bytes(item))
                                    # FLAC picture: skip header (32+len bytes); ищем JPEG/PNG сигнатуру
                                    for sig in (b"\xff\xd8\xff", b"\x89PNG"):
                                        i = raw.find(sig)
                                        if i > 0:
                                            return raw[i:]
                                    return raw
                                except Exception:
                                    continue
    except Exception:
        return None
    return None


def cached_content_type(cover_id: str, size: int | None = None) -> str:
    for cand in (_ext_path_for(cover_id, size), _ext_path_for(cover_id)):
        if cand.exists():
            ct = cand.read_text().strip()
            return {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ct, "image/jpeg")
    return "image/jpeg"


async def prefetch_artist_covers(artist_ids: list[str]) -> int:
    """Параллельно дёргает обложки артистов из Navidrome и кэширует."""
    cfg = _load_cfg()
    url = (cfg.get("url") or "").rstrip("/")
    user = (cfg.get("user") or "").strip()
    password = cfg.get("password") or ""
    if not url or not user:
        return 0
    auth = SubsonicAuth(user=user, password=password)

    async with SubsonicClient(url, auth) as client:
        sem = asyncio.Semaphore(4)

        async def one(aid: str) -> bool:
            async with sem:
                try:
                    # нет прямого бинарного метода в нашем клиенте — используем httpx
                    params = _auth_params(user, password)
                    params.update({"id": aid, "size": "300"})
                    r = await client._client.get(
                        f"{url}/rest/getCoverArt",
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
