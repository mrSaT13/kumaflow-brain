"""Импорт вкусов пользователя из Navidrome (лайки/оценки/плейлисты).

Subsonic API отдаёт starred/ratings/playlists только в контексте конкретного
пользователя — поэтому импорт идёт под логином/паролем САМОГО пользователя
(как в Navidrome UI), а не под глобальным админом.

Что тянем:
  - getStarred2 → starred-треки = Favorite (лайки);
  - userRating в тех же треках: >=4 = лайк, <=2 = дизлайк (считаем, своей
    таблицы дизлайков нет — возвращаем счётчиком);
  - плейлисты пользователя → локальные Playlist (owner_user_id) + PlaylistTrack
    для треков, уже лежащих в нашей библиотеке (матчинг по external_id).
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import Favorite, MediaUser, Playlist, PlaylistTrack, Track

logger = get_logger("taste_import")


def _as_list(v: Any) -> list[dict]:
    if not v:
        return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


def import_user_tastes(server_id: str, username: str, password: str) -> dict:
    """Импорт вкусов под учёткой пользователя. Синхронная обёртка над async клиентом."""
    from app.services.navidrome.client import SubsonicAuth, SubsonicClient

    username = (username or "").strip()
    if not username or not password:
        return {"status": "failure", "error": "Укажите логин и пароль пользователя Navidrome"}

    with session_scope() as db:
        from app.db.models import MediaServer
        from app.services.media_server import get_media_server_config

        cfg = get_media_server_config(db)
        server_row = db.get(MediaServer, server_id)
        url = (cfg.get("url") if cfg else "") or (server_row.url if server_row else "")
        if not url:
            return {"status": "failure", "error": "Медиа-сервер не настроен (Настройки → Медиа-сервер)"}

    async def _fetch() -> dict:
        async with SubsonicClient(url, SubsonicAuth(user=username, password=password), timeout=60.0) as client:
            ok = await client.ping()
            if not ok:
                raise RuntimeError("ping failed — неверный логин/пароль или недоступный сервер")
            starred = await client.get_starred2()
            # плейлисты: пробуем с username и без (разные версии Navidrome)
            playlists: list[dict] = []
            try:
                playlists = await client.get_playlists(username=username)
            except Exception:
                playlists = []
            if not playlists:
                try:
                    playlists = await client.get_playlists()
                except Exception:
                    playlists = []
            details: list[dict] = []
            for pl in playlists:
                pid = pl.get("id")
                if not pid:
                    continue
                try:
                    details.append(await client.get_playlist(pid))
                except Exception as e:  # noqa: BLE001
                    logger.warning("getPlaylist {} failed: {}", pid, e)
            return {"starred": starred or {}, "playlists": details or []}

    try:
        data = asyncio.run(_fetch())
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "401" in msg or "403" in msg or "50" in msg or "unauthor" in msg.lower():
            return {"status": "failure", "error": f"Navidrome отклонил логин/пароль пользователя «{username}» ({msg[:200]})"}
        return {"status": "failure", "error": f"Navidrome: {msg[:300]}"}

    starred = data["starred"]
    songs = _as_list(starred.get("song"))
    # в некоторых версиях starred2 кладёт треки в entry
    songs += _as_list(starred.get("entry"))

    liked_ids: list[str] = []
    disliked = 0
    for s in songs:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        rating = None
        try:
            rating = int(s.get("userRating") or s.get("rating") or 0)
        except Exception:
            rating = 0
        if rating and rating <= 2:
            disliked += 1
            continue  # дизлайк — не добавляем в избранное, только считаем
        liked_ids.append(sid)

    with session_scope() as db:
        # ensure MediaUser
        user = db.query(MediaUser).filter_by(server_id=server_id, external_id=username).first()
        if user is None:
            user = MediaUser(
                id=str(uuid.uuid4()),
                server_id=server_id,
                external_id=username[:128],
                username=username[:128],
                is_admin=False,
            )
            db.add(user)
            db.flush()
        uid = str(user.id)

        # external_id -> Track.id для матчинга
        track_map: dict[str, str] = {}
        if liked_ids:
            rows = db.query(Track).filter(
                Track.server_id == server_id, Track.external_id.in_(liked_ids)
            ).all()
            track_map = {str(r.external_id): str(r.id) for r in rows}

        fav_added = 0
        for sid in liked_ids:
            tid = track_map.get(sid)
            if not tid:
                continue
            exists = db.query(Favorite).filter_by(user_id=uid, track_id=tid).first()
            if exists is None:
                db.add(Favorite(user_id=uid, track_id=tid))
                fav_added += 1

        # плейлисты пользователя
        pl_count = 0
        pl_tracks = 0
        pl_skipped = 0
        for pl in data["playlists"]:
            ext_pid = str(pl.get("id") or "")
            name = (pl.get("name") or "Без названия")[:512]
            entries = _as_list(pl.get("entry")) + _as_list(pl.get("song"))
            entry_ids = [str(e.get("id") or "") for e in entries if e.get("id")]
            if not ext_pid:
                continue
            local = db.query(Playlist).filter_by(server_id=server_id, external_id=ext_pid).first()
            if local is None:
                local = Playlist(
                    id=str(uuid.uuid4()),
                    server_id=server_id,
                    external_id=ext_pid,
                    owner_user_id=uid,
                    name=name,
                    is_public=bool(pl.get("public", False)),
                    is_auto_generated=False,
                )
                db.add(local)
                db.flush()
            else:
                local.name = name
                local.owner_user_id = uid
                db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == local.id).delete()
            # матчим треки плейлиста
            if entry_ids:
                rows = db.query(Track).filter(
                    Track.server_id == server_id, Track.external_id.in_(entry_ids)
                ).all()
                emap = {str(r.external_id): str(r.id) for r in rows}
                pos = 0
                for eid in entry_ids:
                    tid = emap.get(eid)
                    if not tid:
                        pl_skipped += 1
                        continue
                    db.add(PlaylistTrack(playlist_id=local.id, track_id=tid, position=pos))
                    pos += 1
                    pl_tracks += 1
            pl_count += 1

        fav_total = db.query(Favorite).filter_by(user_id=uid).count()

    return {
        "status": "success",
        "user_id": uid,
        "username": username,
        "starred_in_navidrome": len(liked_ids),
        "favorites_added": fav_added,
        "favorites_total": fav_total,
        "disliked": disliked,
        "playlists": pl_count,
        "playlist_tracks": pl_tracks,
        "playlist_tracks_skipped": pl_skipped,
        "note": "Треки, которых нет в локальной библиотеке, пропущены — запустите «Синхронизировать» в Библиотеке и повторите импорт.",
    }
