"""Выгрузка автоплейлистов в Navidrome (чтобы появились в родном клиенте).

Одна функция на всех: ручной Export из UI и авто-пуш из крона
(daily/smart/weekly) за тумблером automation.playlists_push_navidrome.
Повторный вызов пересоздаёт удалённый плейлист; id запоминаем в
Playlist.external_id. Треки с диска (disk:/demo-) пропускаются —
Navidrome их не знает.
"""
from __future__ import annotations

import asyncio


def push_playlist_to_navidrome(db, playlist_id: str) -> dict:
    """Синхронная выгрузка плейлиста. Возвращает dict ok/navidrome_id/exported/skipped/error."""
    import uuid as _uuid

    from app.db.models import Playlist, PlaylistTrack, Track

    try:
        _uuid.UUID(str(playlist_id))
    except ValueError:
        return {"ok": False, "error": "invalid id"}
    p = db.get(Playlist, str(playlist_id))
    if not p:
        return {"ok": False, "error": "not found"}
    items = (
        db.query(PlaylistTrack)
        .filter(PlaylistTrack.playlist_id == p.id)
        .order_by(PlaylistTrack.position.asc())
        .all()
    )
    nav_ids: list[str] = []
    skipped = 0
    for it in items:
        t = db.get(Track, it.track_id)
        ext = str(t.external_id or "") if t else ""
        if ext and not ext.startswith(("disk:", "dartist:", "dalbum:", "demo-")):
            nav_ids.append(ext)
        else:
            skipped += 1
    if not nav_ids:
        return {"ok": False, "error": "только файлы с диска — выгружать нечего"}
    from app.services.media_server import get_media_server_config
    from app.services.navidrome.client import SubsonicAuth, SubsonicClient

    try:
        cfg = get_media_server_config(db)
    except Exception:
        cfg = {}
    url, user, password = (cfg.get("url") or ""), (cfg.get("user") or ""), (cfg.get("password") or "")
    if not url or not user:
        return {"ok": False, "error": "Медиа-сервер не настроен"}

    async def _push() -> str:
        async with SubsonicClient(url, SubsonicAuth(user=user, password=password), timeout=60.0) as client:
            if p.external_id:
                try:
                    await client.delete_playlist(p.external_id)
                except Exception:
                    pass
            created = await client.create_playlist(name=p.name, song_ids=nav_ids)
            remote_id = str(created.get("id") or "")
            if not remote_id:
                raise RuntimeError("Navidrome не вернул id созданного плейлиста")
            return remote_id

    try:
        remote_id = asyncio.run(_push())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Navidrome: {str(e)[:300]}"}
    p.external_id = remote_id
    db.commit()
    return {"ok": True, "navidrome_id": remote_id, "exported": len(nav_ids), "skipped": skipped}


def maybe_auto_push(db, playlist_id: str) -> dict | None:
    """Пуш если включён тумблер playlists_push_navidrome. Иначе None. Не падает."""
    try:
        from app.services import automation as _auto

        if not _auto.playlists_push_enabled(db):
            return None
    except Exception:
        return None
    try:
        return push_playlist_to_navidrome(db, playlist_id)
    except Exception as e:  # noqa: BLE001
        try:
            from app.core.logging import get_logger

            get_logger("playlist_push").warning("auto-push {} failed: {}", playlist_id, e)
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:200]}
