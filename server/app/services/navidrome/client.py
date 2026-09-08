from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import httpx

from app.core.logging import get_logger

logger = get_logger("subsonic")

SUBSONIC_API_VERSION = "1.16.1"
CLIENT_NAME = "KumaFlowBrain"


class SubsonicError(RuntimeError):
    def __init__(self, code: int | None, message: str, status: str = "failed") -> None:
        super().__init__(f"subsonic error {code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass
class SubsonicAuth:
    user: str
    password: str

    def params(self, extra: Mapping[str, Any] | None = None) -> dict[str, str]:
        params: dict[str, str] = {
            "u": self.user,
            "v": SUBSONIC_API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        salt = secrets.token_hex(6)
        token = hashlib.md5(
            f"{self.password}{salt}".encode("utf-8")
        ).hexdigest()
        params["t"] = token
        params["s"] = salt
        if extra:
            for k, v in extra.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    for item in v:
                        params[k] = str(item)
                else:
                    params[k] = str(v)
        return params


class SubsonicClient:
    """Minimal async Subsonic client (Navidrome-compatible, OpenSubsonic-aware)."""

    def __init__(self, base_url: str, auth: SubsonicAuth, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "SubsonicClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, endpoint: str, params: Mapping[str, Any] | None = None) -> str:
        merged = self.auth.params(params or {})
        return f"{self.base_url}/rest/{endpoint}?{urlencode(merged)}"

    async def _call(self, endpoint: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = self._url(endpoint, params)
        r = await self._client.get(url)
        r.raise_for_status()
        data = r.json()
        resp = data.get("subsonic-response") or {}
        if resp.get("status") != "ok":
            err = resp.get("error") or {}
            raise SubsonicError(err.get("code"), err.get("message", "unknown"), resp.get("status", "failed"))
        resp.pop("status", None)
        return resp

    @staticmethod
    def _unwrap(payload: dict[str, Any], *keys: str) -> Any:
        for k in keys:
            if k in payload:
                return payload[k]
        return None

    # ---------- System ----------
    async def ping(self) -> bool:
        try:
            await self._call("ping")
            return True
        except Exception as e:
            logger.warning("ping failed: {}", e)
            return False

    async def get_license(self) -> dict[str, Any] | None:
        r = await self._call("getLicense")
        return r.get("license")

    async def get_open_subsonic_extensions(self) -> list[str]:
        try:
            r = await self._call("getOpenSubsonicExtensions")
        except SubsonicError:
            return []
        ext = r.get("openSubsonicExtensions") or {}
        names: list[str] = []
        for entry in ext.get("openSubsonicExtension", []) or []:
            names.append(entry.get("name", ""))
        return [n for n in names if n]

    async def token_info(self) -> dict[str, Any] | None:
        try:
            r = await self._call("tokenInfo")
        except SubsonicError:
            return None
        return r.get("tokenInfo")

    # ---------- Browsing ----------
    async def get_music_folders(self) -> list[dict[str, Any]]:
        r = await self._call("getMusicFolders")
        return (r.get("musicFolders") or {}).get("musicFolder", []) or []

    async def get_artists(self, music_folder_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        r = await self._call("getArtists", params)
        index = r.get("artists") or {}
        result: list[dict[str, Any]] = []
        for letter in index.get("index", []) or []:
            result.extend(letter.get("artist", []) or [])
        return result

    async def get_artist(self, artist_id: str) -> dict[str, Any]:
        r = await self._call("getArtist", {"id": artist_id})
        return r.get("artist") or {}

    async def get_album(self, album_id: str) -> dict[str, Any]:
        r = await self._call("getAlbum", {"id": album_id})
        return r.get("album") or {}

    async def get_song(self, song_id: str) -> dict[str, Any]:
        r = await self._call("getSong", {"id": song_id})
        return r.get("song") or {}

    async def get_genres(self) -> list[dict[str, Any]]:
        r = await self._call("getGenres")
        return (r.get("genres") or {}).get("genre", []) or []

    # ---------- Lists ----------
    async def get_album_list2(
        self,
        type_: str = "newest",
        size: int = 100,
        offset: int = 0,
        genre: str | None = None,
        from_year: int | None = None,
        to_year: int | None = None,
        music_folder_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"type": type_, "size": size, "offset": offset}
        if genre:
            params["genre"] = genre
        if from_year is not None:
            params["fromYear"] = from_year
        if to_year is not None:
            params["toYear"] = to_year
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        r = await self._call("getAlbumList2", params)
        return (r.get("albumList2") or {}).get("album", []) or []

    async def get_random_songs(
        self,
        size: int = 50,
        genre: str | None = None,
        from_year: int | None = None,
        to_year: int | None = None,
        music_folder_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"size": size}
        if genre:
            params["genre"] = genre
        if from_year is not None:
            params["fromYear"] = from_year
        if to_year is not None:
            params["toYear"] = to_year
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        r = await self._call("getRandomSongs", params)
        return (r.get("randomSongs") or {}).get("song", []) or []

    async def get_songs_by_genre(self, genre: str, count: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        r = await self._call(
            "getSongsByGenre",
            {"genre": genre, "count": count, "offset": offset},
        )
        return (r.get("songsByGenre") or {}).get("song", []) or []

    async def get_starred2(self, music_folder_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        r = await self._call("getStarred2", params)
        return r.get("starred2") or {}

    async def get_now_playing(self) -> list[dict[str, Any]]:
        r = await self._call("getNowPlaying")
        entries = (r.get("nowPlaying") or {}).get("entry", []) or []
        return entries

    # ---------- Search ----------
    async def search3(
        self,
        query: str,
        artist_count: int = 20,
        album_count: int = 20,
        song_count: int = 50,
    ) -> dict[str, Any]:
        r = await self._call(
            "search3",
            {
                "query": query,
                "artistCount": artist_count,
                "albumCount": album_count,
                "songCount": song_count,
            },
        )
        return r.get("searchResult3") or {}

    # ---------- Playlists ----------
    async def get_playlists(self, username: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if username:
            params["username"] = username
        r = await self._call("getPlaylists", params)
        return (r.get("playlists") or {}).get("playlist", []) or []

    async def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        r = await self._call("getPlaylist", {"id": playlist_id})
        return r.get("playlist") or {}

    async def create_playlist(
        self,
        name: str,
        song_ids: Iterable[str] | None = None,
        playlist_id: str | None = None,
        public: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "public": "true" if public else "false"}
        if playlist_id:
            params["playlistId"] = playlist_id
        if song_ids:
            params["songId"] = list(song_ids)
        r = await self._call("createPlaylist", params)
        return r.get("playlist") or {}

    async def update_playlist(
        self,
        playlist_id: str,
        name: str | None = None,
        public: bool | None = None,
        add_ids: Iterable[str] | None = None,
        remove_indices: Iterable[int] | None = None,
        comment: str | None = None,
    ) -> None:
        params: dict[str, Any] = {"playlistId": playlist_id}
        if name is not None:
            params["name"] = name
        if public is not None:
            params["public"] = "true" if public else "false"
        if comment is not None:
            params["comment"] = comment
        if add_ids:
            params["songIdToAdd"] = list(add_ids)
        if remove_indices:
            params["songIndexToRemove"] = list(remove_indices)
        await self._call("updatePlaylist", params)

    async def delete_playlist(self, playlist_id: str) -> None:
        await self._call("deletePlaylist", {"id": playlist_id})

    # ---------- Lyrics ----------
    async def get_lyrics(self, artist: str, title: str) -> str | None:
        r = await self._call("getLyrics", {"artist": artist, "title": title})
        l = r.get("lyrics") or {}
        text = (l.get("value") or "").strip()
        return text or None

    async def get_lyrics_by_song_id(self, song_id: str) -> dict[str, Any] | None:
        try:
            r = await self._call("getLyricsBySongId", {"id": song_id})
        except SubsonicError:
            return None
        return r.get("lyricsBySongId")

    # ---------- Annotation ----------
    async def star(
        self,
        song_ids: Iterable[str] | None = None,
        album_ids: Iterable[str] | None = None,
        artist_ids: Iterable[str] | None = None,
    ) -> None:
        params: dict[str, Any] = {}
        if song_ids:
            params["id"] = list(song_ids)
        if album_ids:
            params["albumId"] = list(album_ids)
        if artist_ids:
            params["artistId"] = list(artist_ids)
        await self._call("star", params)

    async def unstar(
        self,
        song_ids: Iterable[str] | None = None,
        album_ids: Iterable[str] | None = None,
        artist_ids: Iterable[str] | None = None,
    ) -> None:
        params: dict[str, Any] = {}
        if song_ids:
            params["id"] = list(song_ids)
        if album_ids:
            params["albumId"] = list(album_ids)
        if artist_ids:
            params["artistId"] = list(artist_ids)
        await self._call("unstar", params)

    async def set_rating(self, song_id: str, rating: int) -> None:
        await self._call("setRating", {"id": song_id, "rating": max(0, min(5, int(rating)))})

    async def scrobble(self, song_id: str, time_ms: int | None = None, submission: bool = True) -> None:
        params: dict[str, Any] = {"id": song_id, "submission": "true" if submission else "false"}
        if time_ms is not None:
            params["time"] = int(time_ms)
        await self._call("scrobble", params)

    # ---------- Bookmarks / play queue ----------
    async def get_bookmarks(self) -> list[dict[str, Any]]:
        r = await self._call("getBookmarks")
        return (r.get("bookmarks") or {}).get("bookmark", []) or []

    async def get_play_queue(self) -> dict[str, Any] | None:
        r = await self._call("getPlayQueue")
        return r.get("playQueue")

    async def save_play_queue(self, ids: list[str], current: str | None = None, position_ms: int | None = None) -> None:
        params: dict[str, Any] = {"id": ids}
        if current:
            params["current"] = current
        if position_ms is not None:
            params["position"] = int(position_ms)
        await self._call("savePlayQueue", params)

    # ---------- Scanning ----------
    async def get_scan_status(self) -> dict[str, Any] | None:
        r = await self._call("getScanStatus")
        return r.get("scanStatus")

    async def start_scan(self, full: bool = False) -> dict[str, Any] | None:
        r = await self._call("startScan", {"fullScan": "true" if full else "false"})
        return r.get("scanStatus")

    # ---------- Users ----------
    async def get_user(self, username: str | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {}
        if username:
            params["username"] = username
        r = await self._call("getUser", params)
        return r.get("user")

    async def get_users(self) -> list[dict[str, Any]]:
        r = await self._call("getUsers")
        return (r.get("users") or {}).get("user", []) or []

    # ---------- OpenSubsonic playback report ----------
    async def report_playback(self, song_id: str, time_ms: int, status: str = "playing") -> None:
        try:
            await self._call(
                "reportPlayback",
                {"id": song_id, "time": int(time_ms), "status": status},
            )
        except SubsonicError as e:
            logger.debug("reportPlayback unsupported: {}", e)
