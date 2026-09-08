"""LRCLIB lyrics fetcher — бесплатный, без токена, отдает synced + plain текст."""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("lyrics")

BASE = "https://lrclib.net"


class LyricsResult(dict):
    pass


def _normalize(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().lower()
    return " ".join(s.split())


def fetch(artist: str, title: str, album: str | None = None, duration_sec: int | None = None) -> dict[str, Any] | None:
    """Возвращает dict с полями provider/text/synced/language/source_url или None."""
    s = get_settings()
    headers = {"user-agent": s.lyrics_user_agent}
    a, t = _normalize(artist) or "", _normalize(title) or ""
    if not a or not t:
        return None

    # сначала точное совпадение
    try:
        r = httpx.get(
            f"{BASE}/api/get",
            params={"artist_name": artist, "track_name": title, "album_name": album} if album else {"artist_name": artist, "track_name": title},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            return _to_result(r.json())
    except httpx.HTTPError as e:
        logger.debug("lrclib /get failed: {}", e)

    # затем поиск по каталогу
    try:
        r = httpx.get(
            f"{BASE}/api/search",
            params={"q": f"{artist} {title}", "limit": 5},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            for item in r.json() or []:
                if _normalize(item.get("artistName")) == a and _normalize(item.get("trackName")) == t:
                    return _to_result(item)
                # пробуем по длительности ±3 сек
                if duration_sec and item.get("duration"):
                    try:
                        if abs(float(item["duration"]) - float(duration_sec)) < 3:
                            return _to_result(item)
                    except (TypeError, ValueError):
                        continue
    except httpx.HTTPError as e:
        logger.debug("lrclib /search failed: {}", e)

    return None


def _to_result(item: dict[str, Any]) -> dict[str, Any]:
    plain = (item.get("plainLyrics") or "").strip()
    synced = (item.get("syncedLyrics") or "").strip()
    return {
        "provider": "lrclib",
        "text": plain or _strip_lrc(synced),
        "synced": _parse_lrc(synced) if synced else None,
        "language": None,
        "source_url": item.get("url") or f"{BASE}/api/get?artist_name={item.get('artistName','')}&track_name={item.get('trackName','')}",
        "fetched_at": int(time.time()),
    }


def _strip_lrc(synced: str) -> str:
    out = []
    for line in synced.splitlines():
        if "]" in line:
            idx = line.find("]")
            text = line[idx + 1:].strip()
            if text:
                out.append(text)
    return "\n".join(out)


def _parse_lrc(synced: str) -> list[dict[str, Any]]:
    """[mm:ss.xx] text  -> [{t: ms, text: ...}]"""
    result: list[dict[str, Any]] = []
    for line in synced.splitlines():
        if "]" not in line:
            continue
        ts_part, _, text = line.partition("]")
        ts_part = ts_part.lstrip("[").strip()
        try:
            mm, rest = ts_part.split(":", 1)
            ss, _ = rest.split(".", 1)
            ms = int(mm) * 60_000 + int(ss) * 1000
            result.append({"t": ms, "text": text.strip()})
        except (ValueError, AttributeError):
            continue
    return result
