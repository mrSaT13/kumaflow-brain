from __future__ import annotations

import hashlib
import os
import random
import re
import time
import uuid
from datetime import datetime, date
from pathlib import Path

from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import (
    ScanRun,
    ScanLog,
    Track,
    TrackFeatures,
    TrackCluster,
    Lyrics,
)
from app.services import lyrics as lyrics_svc
from app.services import lyrics_ai

logger = get_logger("workers.tasks")


def _append_log(run_id: str, level: str, message: str) -> None:
    with session_scope() as db:
        db.add(
            ScanLog(
                id=str(uuid.uuid4()),
                run_id=run_id,
                level=level,
                message=message,
            )
        )


def _finish_run(run_id: str, status: str = "success", error: str | None = None) -> None:
    with session_scope() as db:
        run = db.get(ScanRun, run_id)
        if not run:
            return
        run.status = status
        run.finished_at = datetime.utcnow()
        if error:
            run.error = error


def noop(*args, **kwargs):
    return {"ok": True}


def _disk_external_id(prefix: str, *parts: str) -> str:
    """Стабильный external_id для сущностей с диска (влезает в String(128))."""
    h = hashlib.sha1("\x00".join(parts).encode("utf-8", "ignore")).hexdigest()
    return f"{prefix}{h[:40]}"


def _split_disc_no(v: str | None) -> int | None:
    if not v:
        return None
    return _to_int(str(v).split("/")[0].strip())


def _year_from_tag(v: str | None) -> int | None:
    if not v:
        return None
    s = str(v).strip()[:4]
    return int(s) if s.isdigit() else None


def _read_disk_tags(path: Path) -> dict:
    """Теги файла через mutagen. Без тегов — пустые поля (дополнит парсинг имени)."""
    meta = {"title": None, "artist": None, "album": None, "genre": None,
            "year": None, "track_no": None, "disc_no": None,
            "duration_sec": None, "bitrate": None}
    try:
        from mutagen import File as _MutagenFile
        audio = _MutagenFile(str(path), easy=True)
    except Exception:
        return meta
    if audio is None:
        return meta

    def first(key: str) -> str | None:
        try:
            vals = audio.get(key)
        except Exception:
            return None
        if not vals:
            return None
        s = str(vals[0]).strip()
        return s or None

    meta["title"] = first("title")
    meta["artist"] = first("artist")
    meta["album"] = first("album")
    meta["genre"] = first("genre")
    meta["year"] = _year_from_tag(first("date"))
    meta["track_no"] = _split_disc_no(first("tracknumber"))
    meta["disc_no"] = _split_disc_no(first("discnumber"))
    try:
        if getattr(audio.info, "length", None):
            meta["duration_sec"] = int(float(audio.info.length))
        br = getattr(audio.info, "bitrate", None)
        if br:
            meta["bitrate"] = int(br // 1000)
    except Exception:
        pass
    return meta


def _fallback_from_filename(path: Path, rel_parts: list[str]) -> tuple[str, str, str]:
    """Artist/Album/Title когда тегов нет: папки + имя файла без номера."""
    stem = path.stem.strip()
    m = re.match(r"^\d{1,3}\s*[-._\s]+\s*(.+)$", stem)
    title = (m.group(1).strip() if m else stem) or stem
    artist = album = None
    if len(rel_parts) >= 2:
        artist, album = rel_parts[0], rel_parts[1]
    elif len(rel_parts) == 1:
        album = rel_parts[0]
    return artist or "Unknown Artist", album or "Unknown Album", title


_CONTENT_BY_SUFFIX = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
    "oga": "audio/ogg", "opus": "audio/ogg", "m4a": "audio/mp4",
    "wav": "audio/wav", "wma": "audio/x-ms-wma", "aac": "audio/aac",
}


def _scan_disk_music(run_id: str, server_id: str) -> dict:
    """Сканирование примонтированного тома MUSIC_DIR (вариант А из compose).

    Navidrome про эту папку ничего не знает — читаем теги напрямую через
    mutagen и кладём в те же таблицы (external_id с префиксом disk-,
    с данными Navidrome не пересекаются). Если папка не задана/пуста —
    возвращает нули без ошибок.
    """
    from app.core.config import get_settings
    from app.services import audio_analysis as aa

    try:
        music_dir = (get_settings().music_dir or os.getenv("MUSIC_DIR", "")).strip()
    except Exception:
        music_dir = os.getenv("MUSIC_DIR", "").strip()
    empty = {"files": 0, "tracks": 0, "artists": 0, "albums": 0, "music_dir": music_dir}
    if not music_dir:
        return empty
    base = Path(music_dir)
    if not base.is_dir():
        _append_log(run_id, "warn", f"MUSIC_DIR={music_dir} не виден внутри worker-контейнера — проверьте volumes (пример в docker-compose.yml: /путь/к/музыке:/music:ro и MUSIC_DIR=/music)")
        return empty
    suffixes = {s.lower() for s in (getattr(aa, "AUDIO_SUFFIXES", None) or {".mp3", ".flac", ".ogg", ".m4a", ".wav"})}
    files: list[Path] = []
    for p in sorted(base.rglob("*")):
        try:
            if p.is_file() and p.suffix.lower() in suffixes:
                files.append(p)
        except OSError:
            continue
    if not files:
        _append_log(run_id, "info", f"В {music_dir} аудиофайлов не найдено (ищу {sorted(suffixes)})")
        return dict(empty)
    _append_log(run_id, "info", f"На диске ({music_dir}) файлов: {len(files)} — читаю теги…")

    from app.db.models import Artist, Album

    artists_new = albums_new = tracks_cnt = skipped = 0
    # Сессия с autoflush=False: проверки .first() НЕ видят pending-объекты,
    # поэтому дубли внутри батча ловили UniqueViolation и откатывали всё.
    # Держим in-memory множества (заодно убираем 3 SELECT на файл).
    with session_scope() as db:
        seen_artists = {r[0] for r in db.query(Artist.external_id).filter_by(server_id=server_id).all()}
        seen_albums = {r[0] for r in db.query(Album.external_id).filter_by(server_id=server_id).all()}
        seen_tracks = {r[0] for r in db.query(Track.external_id).filter_by(server_id=server_id).all()}
        batch_seen: list[tuple[set, str]] = []  # что добавлено в текущем батче (для отката)
        for idx, path in enumerate(files):
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                rel = path.name
            rel_parts = rel.split("/")[:-1]
            tags = _read_disk_tags(path)
            fb_artist, fb_album, fb_title = _fallback_from_filename(path, rel_parts)
            artist_name = (tags["artist"] or fb_artist).strip() or "Unknown Artist"
            album_name = (tags["album"] or fb_album).strip() or "Unknown Album"
            title = (tags["title"] or fb_title).strip() or path.stem
            a_ext = _disk_external_id("dartist:", artist_name.lower())
            b_ext = _disk_external_id("dalbum:", artist_name.lower(), album_name.lower())
            t_ext = _disk_external_id("disk:", rel)
            if a_ext not in seen_artists:
                db.add(Artist(server_id=server_id, external_id=a_ext, name=artist_name[:512]))
                seen_artists.add(a_ext)
                batch_seen.append((seen_artists, a_ext))
                artists_new += 1
            if b_ext not in seen_albums:
                db.add(Album(server_id=server_id, external_id=b_ext, title=album_name[:512],
                             artist_external_id=a_ext, artist_name=artist_name[:512],
                             year=tags["year"], genre=(tags["genre"] or "")[:256] or None))
                seen_albums.add(b_ext)
                batch_seen.append((seen_albums, b_ext))
                albums_new += 1
            if t_ext in seen_tracks:
                skipped += 1
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None
                suffix = path.suffix.lower().lstrip(".")[:16] or None
                db.add(Track(id=str(uuid.uuid4()), server_id=server_id, external_id=t_ext,
                             title=title[:1024], artist_external_id=a_ext, artist_name=artist_name[:512],
                             album_external_id=b_ext, album_name=album_name[:512],
                             genre=(tags["genre"] or "")[:256] or None,
                             duration_sec=tags["duration_sec"], year=tags["year"],
                             track_no=tags["track_no"], disc_no=tags["disc_no"],
                             bitrate=tags["bitrate"], suffix=suffix, size_bytes=size,
                             content_type=_CONTENT_BY_SUFFIX.get(suffix or ""),
                             path=str(path), play_count=0))
                seen_tracks.add(t_ext)
                batch_seen.append((seen_tracks, t_ext))
                tracks_cnt += 1
            if (idx + 1) % 500 == 0:
                try:
                    db.commit()
                    batch_seen.clear()
                except Exception as e:
                    db.rollback()
                    for s, v in batch_seen:  # откаченные id снова станут кандидатами
                        s.discard(v)
                    batch_seen.clear()
                    _append_log(run_id, "warn", f"Батч {(idx + 1) // 500} откатился ({e}); продолжаю")
            if (idx + 1) % 2000 == 0:
                _append_log(run_id, "info", f"С диска обработано файлов: {idx + 1}/{len(files)} (новых треков: {tracks_cnt})")
                try:
                    with session_scope() as dbp:
                        rp = dbp.get(ScanRun, run_id)
                        if rp:
                            rp.total_items = len(files)
                            rp.processed_items = idx + 1
                except Exception:
                    pass
    _append_log(run_id, "info", f"С диска загружено: новых треков {tracks_cnt}, уже было {skipped}, новых альбомов {albums_new}, новых артистов {artists_new}")
    return {"files": len(files), "tracks": tracks_cnt, "artists": artists_new,
            "albums": albums_new, "music_dir": music_dir}


def _to_int(v, default=None):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).lower() in ("true", "1", "yes")


def _parse_dt(v):
    if not v:
        return None
    try:
        # Navidrome: 2024-03-20T12:34:56Z or 2024-03-20T12:34:56.000Z
        s = str(v).replace("Z", "")
        if "." in s:
            s = s.split(".")[0]
        return datetime.fromisoformat(s)
    except Exception:
        return None


def library_scan(run_id: str, *args, **kwargs) -> dict:
    _append_log(run_id, "info", "Сканирование библиотеки запущено")
    try:
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            if not run:
                return {"status": "no run"}
            from app.db.models import MediaServer

            server_row = db.get(MediaServer, run.server_id)
            if not server_row:
                _append_log(run_id, "error", "Сервер не найден")
                _finish_run(run_id, "failure", "server not found")
                return {"status": "failure", "error": "server not found"}

            # если это демо-сервер — сначала пробуем локальный диск, и только
            # если там пусто — считаем демо-треки
            if server_row.type == "demo" or server_row.url in ("http://localhost", "https://localhost"):
                disk = _scan_disk_music(run_id, run.server_id)
                if disk["tracks"] > 0:
                    with session_scope() as db2:
                        r2 = db2.get(ScanRun, run_id)
                        if r2:
                            r2.total_items = disk["files"]
                            r2.processed_items = disk["files"]
                    _finish_run(run_id, "success")
                    return {"status": "success", "tracks": disk["tracks"],
                            "artists": disk["artists"], "albums": disk["albums"],
                            "mode": "disk"}
                total = db.query(Track).filter_by(server_id=run.server_id).count()
                run.total_items = total
                run.processed_items = 0
                db.flush()
                _append_log(run_id, "info", f"Демо-режим: найдено треков: {total}")
                for i in range(total):
                    run.processed_items = i + 1
                _append_log(run_id, "info", "Сканирование библиотеки завершено (демо)")
                _finish_run(run_id, "success")
                return {"status": "success", "tracks": total, "mode": "demo"}

            # 0) Локальный диск (примонтированный том MUSIC_DIR): Navidrome про
            # него ничего не знает, поэтому читаем теги сами. Дополняет данные
            # из Navidrome (external_id с префиксом disk- не пересекаются).
            disk = _scan_disk_music(run_id, server_row.id)

            # реальный Navidrome — тянем через Subsonic API
            from app.db.models import Artist, Album

            cfg = None
            try:
                from app.services.media_server import get_media_server_config

                cfg = get_media_server_config(db)
            except Exception as e:
                logger.warning("get_media_server_config failed: {}", e)

            url = (cfg.get("url") if cfg else server_row.url) or server_row.url
            user = (cfg.get("user") if cfg else "") or ""
            password = (cfg.get("password") if cfg else "") or ""
            _append_log(run_id, "info", f"Подключаюсь к {url} как {user}")

            # пробуем ping синхронно через httpx чтобы быстро отловить ошибку
            import asyncio
            from app.services.navidrome.client import SubsonicClient, SubsonicAuth

            auth = SubsonicAuth(user=user, password=password)

            async def _do_fetch():
                artists_cnt = albums_cnt = tracks_cnt = 0
                # getArtists на большой библиотеке может отвечать дольше дефолтных
                # 30 секунд httpx — ставим 120 и даём понятную ошибку при таймауте.
                async with SubsonicClient(url, auth, timeout=120.0) as client:
                    ok = await client.ping()
                    if not ok:
                        raise RuntimeError("ping failed — проверьте URL/логин/пароль")
                    _append_log(run_id, "info", "ping ok, проверяю папки и статус Navidrome…")
                    try:
                        folders = await client.get_music_folders()
                        names = [f.get("name") or f.get("id") for f in folders] or ["(папок нет)"]
                        _append_log(run_id, "info", f"Папки музыки в Navidrome: {', '.join(str(n) for n in names)}")
                    except Exception as e:
                        _append_log(run_id, "warn", f"getMusicFolders failed: {e}")
                    try:
                        scan_status = await client.get_scan_status()
                        if scan_status and str(scan_status.get("scanning")).lower() == "true":
                            _append_log(run_id, "warn", "Navidrome сейчас сам сканирует папки — его ответы могут быть медленными. Дождитесь конца его сканирования и повторите.")
                    except Exception:
                        pass  # старые версии Navidrome не знают getScanStatus
                    seen_aids: set[str] = set()

                    async def _ensure_artist(aid, aname):
                        nonlocal artists_cnt
                        if not aid or aid in seen_aids:
                            return aid
                        with session_scope() as db2:
                            from sqlalchemy.exc import IntegrityError
                            try:
                                ex = db2.query(Artist).filter_by(server_id=server_row.id, external_id=aid).first()
                                if not ex:
                                    db2.add(Artist(server_id=server_row.id, external_id=aid, name=(aname or "Unknown")[:512]))
                                    artists_cnt += 1
                                elif aname:
                                    ex.name = aname
                            except IntegrityError:
                                db2.rollback()
                        seen_aids.add(aid)
                        return aid

                    async def _store_album(alid, alb, fb_aid=None, fb_aname=None):
                        nonlocal albums_cnt, tracks_cnt
                        alname = alb.get("name") or alb.get("title") or "Unknown Album"
                        if not alid:
                            return
                        base_aid = alb.get("artistId") or fb_aid
                        base_aname = alb.get("artist") or fb_aname or "Unknown"
                        with session_scope() as db2:
                            from sqlalchemy.exc import IntegrityError
                            try:
                                exa = db2.query(Album).filter_by(server_id=server_row.id, external_id=alid).first()
                                if not exa:
                                    exa = Album(
                                        server_id=server_row.id,
                                        external_id=alid,
                                        title=alname,
                                        artist_external_id=base_aid,
                                        artist_name=base_aname,
                                        year=_to_int(alb.get("year")),
                                        genre=alb.get("genre"),
                                        cover_art_id=alb.get("coverArt"),
                                    )
                                    db2.add(exa)
                                else:
                                    exa.title = alname
                                    exa.artist_name = base_aname
                                    exa.year = _to_int(alb.get("year")) or exa.year
                                    exa.genre = alb.get("genre") or exa.genre
                                    exa.cover_art_id = alb.get("coverArt") or exa.cover_art_id
                            except IntegrityError:
                                db2.rollback()
                        albums_cnt += 1
                        try:
                            alb_detail = await client.get_album(alid)
                        except Exception as e:
                            _append_log(run_id, "warn", f"getAlbum {alid} failed: {e}")
                            return
                        songs = alb_detail.get("song") or []
                        for s in songs:
                            sid = s.get("id")
                            if not sid:
                                continue
                            aid = s.get("artistId") or base_aid
                            aname = s.get("artist") or base_aname
                            await _ensure_artist(aid, aname)
                            with session_scope() as db2:
                                from sqlalchemy.exc import IntegrityError
                                try:
                                    ex_t = db2.query(Track).filter_by(server_id=server_row.id, external_id=sid).first()
                                    vals = dict(
                                        title=s.get("title") or "Unknown",
                                        artist_external_id=aid,
                                        artist_name=aname,
                                        album_external_id=s.get("albumId") or alid,
                                        album_name=s.get("album") or alname,
                                        genre=s.get("genre"),
                                        duration_sec=_to_int(s.get("duration")),
                                        year=_to_int(s.get("year")),
                                        track_no=_to_int(s.get("track")),
                                        disc_no=_to_int(s.get("discNumber")),
                                        bitrate=_to_int(s.get("bitRate")),
                                        suffix=s.get("suffix"),
                                        size_bytes=_to_int(s.get("size")),
                                        content_type=s.get("contentType"),
                                        path=s.get("path"),
                                        cover_art_id=s.get("coverArt"),
                                        starred=_to_bool(s.get("starred")),
                                        play_count=_to_int(s.get("playCount"), 0),
                                        rating=_to_int(s.get("userRating") or s.get("rating")),
                                        last_played_at=_parse_dt(s.get("played") or s.get("lastPlayed")),
                                    )
                                    if not ex_t:
                                        ex_t = Track(
                                            id=str(uuid.uuid4()),
                                            server_id=server_row.id,
                                            external_id=sid,
                                            **vals,
                                        )
                                        db2.add(ex_t)
                                    else:
                                        for k, v in vals.items():
                                            if v is not None:
                                                setattr(ex_t, k, v)
                                except IntegrityError:
                                    db2.rollback()
                                tracks_cnt += 1

                    # --- основной путь: альбомы постранично ---
                    # Каждый ответ маленький (500 шт), гигантского getArtists нет —
                    # таймаутов на больших библиотеках больше не будет.
                    _append_log(run_id, "info", "получаю список альбомов постранично (по 500)…")
                    _page_size = 500
                    _page_offset = 0
                    queued_albums: list = []
                    while True:
                        try:
                            _page = await client.get_album_list2(type_="alphabeticalByName", size=_page_size, offset=_page_offset)
                        except Exception as e:
                            _append_log(run_id, "warn", f"getAlbumList2 offset={_page_offset} failed: {e}")
                            break
                        if not _page:
                            break
                        queued_albums.extend(_page)
                        _page_offset += len(_page)
                        if len(_page) < _page_size:
                            break
                        if (_page_offset // _page_size) % 4 == 0:
                            _append_log(run_id, "info", f"Загружено альбомов: {len(queued_albums)}…")
                    total_albums = len(queued_albums)
                    artists: list = []
                    total_artists = 0
                    if total_albums == 0:
                        # fallback: старый путь через getArtists
                        _append_log(run_id, "info", "альбомный список пуст — пробую через артистов…")
                        try:
                            artists = await client.get_artists()
                        except Exception as e:
                            if "timeout" in type(e).__name__.lower() or "timeout" in str(e).lower():
                                raise RuntimeError(
                                    "Navidrome не отвечает дольше 120 секунд — дождитесь "
                                    "конца его собственного сканирования и запустите ещё раз."
                                ) from e
                            raise
                        total_artists = len(artists)
                        _append_log(run_id, "info", f"Артистов в библиотеке: {total_artists} — обрабатываю всех за один проход")
                    else:
                        _append_log(run_id, "info", f"Альбомов в библиотеке: {total_albums} — обрабатываю все за один проход")
                    # обновим total
                    with session_scope() as db2:
                        r2 = db2.get(ScanRun, run_id)
                        if r2:
                            r2.total_items = total_artists + total_albums
                            r2.processed_items = 0
                    for idx, a in enumerate(artists):
                        aid = a.get("id")
                        aname = a.get("name") or "Unknown"
                        if not aid:
                            continue
                        # upsert artist
                        with session_scope() as db2:
                            from sqlalchemy.exc import IntegrityError
                            try:
                                ex = db2.query(Artist).filter_by(server_id=server_row.id, external_id=aid).first()
                                if not ex:
                                    ex = Artist(server_id=server_row.id, external_id=aid, name=aname)
                                    db2.add(ex)
                                else:
                                    ex.name = aname
                                # прогресс
                                r2 = db2.get(ScanRun, run_id)
                                if r2:
                                    r2.processed_items = idx + 1
                            except IntegrityError:
                                db2.rollback()
                        # тянем альбомы артиста
                        try:
                            detail = await client.get_artist(aid)
                        except Exception as e:
                            _append_log(run_id, "warn", f"getArtist {aid} failed: {e}")
                            continue
                        albums = detail.get("album") or []
                        for alb in albums:
                            alid = alb.get("id")
                            alname = alb.get("name") or alb.get("title") or "Unknown Album"
                            if not alid:
                                continue
                            with session_scope() as db2:
                                from sqlalchemy.exc import IntegrityError
                                try:
                                    exa = db2.query(Album).filter_by(server_id=server_row.id, external_id=alid).first()
                                    if not exa:
                                        exa = Album(
                                            server_id=server_row.id,
                                            external_id=alid,
                                            title=alname,
                                            artist_external_id=aid,
                                            artist_name=aname,
                                            year=_to_int(alb.get("year")),
                                            genre=alb.get("genre"),
                                            cover_art_id=alb.get("coverArt"),
                                        )
                                        db2.add(exa)
                                    else:
                                        exa.title = alname
                                        exa.artist_name = aname
                                        exa.year = _to_int(alb.get("year")) or exa.year
                                        exa.genre = alb.get("genre") or exa.genre
                                        exa.cover_art_id = alb.get("coverArt") or exa.cover_art_id
                                except IntegrityError:
                                    db2.rollback()
                            albums_cnt += 1
                            # тянем треки альбома
                            try:
                                alb_detail = await client.get_album(alid)
                            except Exception as e:
                                _append_log(run_id, "warn", f"getAlbum {alid} failed: {e}")
                                continue
                            songs = alb_detail.get("song") or []
                            for s in songs:
                                sid = s.get("id")
                                if not sid:
                                    continue
                                with session_scope() as db2:
                                    from sqlalchemy.exc import IntegrityError
                                    try:
                                        ex_t = db2.query(Track).filter_by(server_id=server_row.id, external_id=sid).first()
                                        vals = dict(
                                            title=s.get("title") or "Unknown",
                                            artist_external_id=s.get("artistId") or aid,
                                            artist_name=s.get("artist") or aname,
                                            album_external_id=s.get("albumId") or alid,
                                            album_name=s.get("album") or alname,
                                            genre=s.get("genre"),
                                            duration_sec=_to_int(s.get("duration")),
                                            year=_to_int(s.get("year")),
                                            track_no=_to_int(s.get("track")),
                                            disc_no=_to_int(s.get("discNumber")),
                                            bitrate=_to_int(s.get("bitRate")),
                                            suffix=s.get("suffix"),
                                            size_bytes=_to_int(s.get("size")),
                                            content_type=s.get("contentType"),
                                            path=s.get("path"),
                                            cover_art_id=s.get("coverArt"),
                                            starred=_to_bool(s.get("starred")),
                                            play_count=_to_int(s.get("playCount"), 0),
                                            rating=_to_int(s.get("userRating") or s.get("rating")),
                                            last_played_at=_parse_dt(s.get("played") or s.get("lastPlayed")),
                                        )
                                        if not ex_t:
                                            ex_t = Track(
                                                id=str(uuid.uuid4()),
                                                server_id=server_row.id,
                                                external_id=sid,
                                                **vals,
                                            )
                                            db2.add(ex_t)
                                        else:
                                            for k, v in vals.items():
                                                if v is not None:
                                                    setattr(ex_t, k, v)
                                    except IntegrityError:
                                        db2.rollback()
                                        ex_t = db2.query(Track).filter_by(server_id=server_row.id, external_id=sid).first()
                                        if ex_t:
                                            for k, v in vals.items():
                                                if v is not None:
                                                    setattr(ex_t, k, v)
                                    tracks_cnt += 1
                        if (idx + 1) % 25 == 0:
                            _append_log(run_id, "info", f"Обработано артистов: {idx+1}/{total_artists}, альбомов: {albums_cnt}, треков: {tracks_cnt}")
                    for jdx, alb in enumerate(queued_albums):
                        await _store_album(alb.get("id"), alb)
                        if (jdx + 1) % 25 == 0:
                            with session_scope() as db2:
                                r2 = db2.get(ScanRun, run_id)
                                if r2:
                                    r2.processed_items = total_artists + jdx + 1
                            _append_log(run_id, "info", f"Обработано альбомов: {jdx+1}/{total_albums}, артистов: {artists_cnt}, треков: {tracks_cnt}")
                _append_log(run_id, "info", f"Готово — артистов: {artists_cnt}, альбомов: {albums_cnt}, треков: {tracks_cnt}")
                with session_scope() as db2:
                    r2 = db2.get(ScanRun, run_id)
                    if r2:
                        r2.total_items = total_artists + total_albums
                        r2.processed_items = total_artists + total_albums
                return {"artists": artists_cnt, "albums": albums_cnt, "tracks": tracks_cnt}

            try:
                result = asyncio.run(_do_fetch())
            except Exception as e_navi:
                # Navidrome лёг (таймаут getArtists и т.п.), но с диска уже
                # что-то загружено — не валим весь прогон, отдаём диск.
                logger.warning("navidrome fetch failed (disk tracks: {})", disk["tracks"])
                if disk["tracks"] > 0:
                    _append_log(run_id, "warn", f"Navidrome недоступен ({e_navi}), но с диска загружено треков: {disk['tracks']}")
                    with session_scope() as db2:
                        r2 = db2.get(ScanRun, run_id)
                        if r2:
                            r2.total_items = (r2.total_items or 0) + disk["files"]
                            r2.processed_items = (r2.processed_items or 0) + disk["files"]
                    _finish_run(run_id, "success")
                    return {"status": "success", "tracks": disk["tracks"],
                            "artists": disk["artists"], "albums": disk["albums"],
                            "mode": "disk", "navidrome_error": str(e_navi)}
                raise
            if disk["tracks"] > 0:
                _append_log(run_id, "info", f"Дополнительно с диска: треков {disk['tracks']}, альбомов {disk['albums']}")
            _finish_run(run_id, "success")
            combined = dict(result)
            combined["disk_tracks"] = disk["tracks"]
            if disk["tracks"] > 0:
                combined["mode"] = "navidrome+disk"
            return {"status": "success", **combined}
    except Exception as e:  # noqa: BLE001
        logger.exception("library_scan failed: {}", e)
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def sonic_analysis(run_id: str, *args, **kwargs) -> dict:
    """Настоящий sonic-анализ: читает реальные аудиофайлы и считает признаки.

    Источник: локальный файл (``track.path`` / ``MUSIC_DIR``) либо стрим
    из Navidrome через Subsonic ``download``. Движок — librosa; если её нет,
    задача честно падает с объяснением, а не пишет случайные числа.
    По умолчанию обрабатывает до ``analysis_max_tracks_per_run``
    непроанализированных треков (повторные запуски продолжают).
    Kwargs: ``force=True`` — пересчитать всё, ``limit=N`` — взять N треков.
    """
    from app.core.config import get_settings
    from app.services import audio_analysis as aa

    force = bool(kwargs.get("force", False))
    try:
        settings = get_settings()
        sample_seconds = int(settings.analysis_sample_seconds or 90)
        default_limit = int(settings.analysis_max_tracks_per_run or 0)
        music_dir = settings.music_dir or os.getenv("MUSIC_DIR", "")
    except Exception:
        sample_seconds, default_limit, music_dir = 90, 200, os.getenv("MUSIC_DIR", "")
    batch_limit = kwargs.get("limit", None)
    if batch_limit is None or int(batch_limit or 0) <= 0:
        batch_limit = default_limit

    try:
        import librosa  # noqa: F401
    except Exception:
        msg = (
            "librosa не установлена — sonic-анализ невозможен. "
            "Docker-образ backend уже включает ML-зависимости; "
            "для локального запуска: pip install -r requirements.txt"
        )
        _append_log(run_id, "error", msg)
        _finish_run(run_id, "failure", msg)
        return {"status": "failure", "error": msg}

    _append_log(
        run_id, "info",
        f"Sonic-анализ запущен (движок {aa.ANALYZER_VERSION}, "
        f"фрагмент {sample_seconds}с{', force' if force else ''})",
    )
    try:
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            if not run:
                return {"status": "no run"}
            server_id = run.server_id
            q = (
                db.query(Track)
                .outerjoin(TrackFeatures, TrackFeatures.track_id == Track.id)
                .filter(Track.server_id == server_id)
            )
            if not force:
                q = q.filter(TrackFeatures.track_id.is_(None))
            q = q.order_by(Track.created_at.asc())
            total_pending = q.count()
            batch = q.limit(batch_limit).all() if batch_limit and batch_limit > 0 else q.all()
            todo_data = [
                (t.id, t.external_id, t.path, t.suffix, t.duration_sec, t.title, t.artist_name)
                for t in batch
            ]
            run.total_items = len(todo_data)
            run.processed_items = 0

        with session_scope() as db:
            try:
                from app.services.media_server import get_media_server_config

                cfg: dict = dict(get_media_server_config(db))
            except Exception as e:
                logger.warning("get_media_server_config failed: {}", e)
                cfg = {}

        ok = fail = n_local = n_stream = 0
        for i, (tid, ext_id, tpath, suffix, dur, title, artist) in enumerate(todo_data):
            tmp_to_clean: Path | None = None
            try:
                local_path = aa.resolve_local_file(tpath, music_dir)
                if local_path is not None:
                    src = local_path
                    n_local += 1
                else:
                    if not ext_id:
                        raise RuntimeError("нет external_id и нет локального файла")
                    src = aa.download_from_navidrome(ext_id, cfg)
                    tmp_to_clean = Path(src)
                    n_stream += 1
                feats = aa.analyze_file(src, sample_seconds=sample_seconds, track_duration_sec=dur)
                with session_scope() as db:
                    f = db.get(TrackFeatures, tid)
                    if f is None:
                        f = TrackFeatures(track_id=tid)
                        db.add(f)
                    f.tempo_bpm = feats["tempo_bpm"]
                    f.key_name = feats["key_name"]
                    f.scale = feats["scale"]
                    f.energy = feats["energy"]
                    f.danceability = feats["danceability"]
                    f.valence = feats["valence"]
                    f.arousal = feats["arousal"]
                    f.loudness_db = feats["loudness_db"]
                    f.spectral_centroid = feats["spectral_centroid"]
                    f.spectral_rolloff = feats["spectral_rolloff"]
                    f.zero_crossing_rate = feats["zero_crossing_rate"]
                    f.mfcc_summary = feats["mfcc_summary"]
                    f.chroma_summary = feats["chroma_summary"]
                    f.mood_vector = feats["mood_vector"]
                    f.mood_labels = feats["mood_labels"]
                    f.analyzed_at = datetime.utcnow()
                    if feats.get("duration_sec") and not dur:
                        t = db.get(Track, tid)
                        if t is not None:
                            t.duration_sec = feats["duration_sec"]
                    run = db.get(ScanRun, run_id)
                    if run:
                        run.processed_items = i + 1
                ok += 1
            except Exception as e:  # noqa: BLE001 — один битый трек не валит весь прогон
                fail += 1
                _append_log(run_id, "warn", f"Не проанализирован: {artist} — {title}: {e}")
            finally:
                if tmp_to_clean is not None:
                    try:
                        tmp_to_clean.unlink(missing_ok=True)
                    except OSError:
                        pass
            if (i + 1) % 25 == 0:
                _append_log(run_id, "info", f"Обработано {i + 1}/{len(todo_data)} (ок: {ok}, ошибок: {fail})")

        remaining = total_pending - len(todo_data)
        summary = (
            f"Готово — ок: {ok}, ошибок: {fail} "
            f"(локальные файлы: {n_local}, стрим из Navidrome: {n_stream})."
        )
        if remaining > 0:
            summary += f" Осталось без анализа: {remaining} — запустите Sonic ещё раз."
        else:
            summary += " Все треки проанализированы."
        _append_log(run_id, "info", summary)
        _finish_run(run_id, "success")
        return {"status": "success", "ok": ok, "failed": fail, "remaining": remaining}
    except Exception as e:  # noqa: BLE001
        logger.exception("sonic_analysis failed: {}", e)
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def lyrics_fetch(run_id: str, *args, **kwargs) -> dict:
    """Тянет тексты с LRCLIB и анализирует настроение через AI.

    Берёт треки БЕЗ текстов (до 2000 за прогон), сеть — вне транзакций.
    Остаток показывается в итоге — повторные запуски продолжают.
    """
    _append_log(run_id, "info", "Загрузка текстов (LRCLIB) — беру треки без текстов")
    try:
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            # только треки без lrclib-текста
            have = db.query(Lyrics.track_id).filter(Lyrics.provider == "lrclib").subquery()
            todo_q = (
                db.query(Track)
                .filter(
                    Track.server_id == run.server_id,
                    Track.artist_name.isnot(None),
                    Track.title.isnot(None),
                    ~Track.id.in_(db.query(have.c.track_id)),
                )
                .order_by(Track.created_at.asc())
                .limit(2000)
            )
            todo_data = [
                (t.id, t.artist_name, t.title, t.album_name, t.duration_sec) for t in todo_q.all()
            ]
            run.total_items = len(todo_data)
            run.processed_items = 0
        if not todo_data:
            _append_log(run_id, "info", "Все треки уже с текстами — делать нечего")
            _finish_run(run_id, "success")
            return {"status": "success", "fetched": 0, "analyzed": 0, "skipped": 0, "remaining": 0}

        fetched = analyzed = skipped = 0
        for i, (tid, artist, title, album, dur) in enumerate(todo_data):
            # сеть вне транзакции
            result = lyrics_svc.fetch(artist=artist or "", title=title or "", album=album, duration_sec=dur)
            with session_scope() as db:
                run = db.get(ScanRun, run_id)
                if run:
                    run.processed_items = i + 1
                if not result or not result.get("text"):
                    _append_log(run_id, "warn", f"Текст не найден: {artist} — {title}")
                    skipped += 1
                    continue
                # защита от дубля (уникальный ключ track_id+provider)
                exists2 = db.query(Lyrics).filter(Lyrics.track_id == tid, Lyrics.provider == "lrclib").first()
                if exists2:
                    skipped += 1
                    continue
                try:
                    db.add(
                        Lyrics(
                            track_id=tid,
                            provider="lrclib",
                            text=result["text"],
                            synced=result.get("synced"),
                            language=result.get("language"),
                            source_url=result.get("source_url"),
                        )
                    )
                    db.flush()
                except Exception as e:
                    db.rollback()
                    _append_log(run_id, "warn", f"Дубль текста пропущен: {artist} — {title}: {e}")
                    skipped += 1
                    continue
                fetched += 1
                # AI анализ тоже вне долгой транзакции, но запись — внутри
                try:
                    ai_result = lyrics_ai.analyze(result["text"])
                except Exception:
                    ai_result = None
                if ai_result:
                    f = db.get(TrackFeatures, tid)
                    if f is None:
                        f = TrackFeatures(track_id=tid)
                        db.add(f)
                    if "valence" in ai_result:
                        try:
                            f.valence = float(ai_result["valence"])
                        except Exception:
                            pass
                    if "arousal" in ai_result:
                        try:
                            f.arousal = float(ai_result["arousal"])
                        except Exception:
                            pass
                    if "energy" in ai_result:
                        try:
                            f.energy = float(ai_result["energy"])
                        except Exception:
                            pass
                    moods = (f.mood_labels or []) + ai_result.get("moods", [])
                    f.mood_labels = list(dict.fromkeys([str(m).lower() for m in moods if str(m).strip()]))[:8]
                    mv = dict(f.mood_vector or {})
                    mv["ai_sentiment"] = ai_result.get("sentiment", "neutral")
                    mv["ai_language"] = ai_result.get("language", "")
                    mv["ai_themes"] = ai_result.get("themes", [])
                    f.mood_vector = mv
                    analyzed += 1
            time.sleep(0.02)

        _append_log(
            run_id,
            "info",
            f"Загружено: {fetched}, проанализировано AI: {analyzed}, пропущено: {skipped}",
        )
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            have2 = db.query(Lyrics.track_id).filter(Lyrics.provider == "lrclib").subquery()
            remaining = (
                db.query(Track)
                .filter(
                    Track.server_id == (run.server_id if run else ""),
                    ~Track.id.in_(db.query(have2.c.track_id)),
                )
                .count()
            )
        if remaining > 0:
            _append_log(run_id, "info", f"Осталось без текстов: {remaining} — запустите загрузку текстов ещё раз")
        _finish_run(run_id, "success")
        return {"status": "success", "fetched": fetched, "analyzed": analyzed, "skipped": skipped, "remaining": remaining}
    except Exception as e:  # noqa: BLE001
        logger.exception("lyrics_fetch failed: {}", e)
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def lyrics_fetch_one(track_id: str) -> dict:
    """Загрузка текста для одного трека (кнопка на странице трека)."""
    with session_scope() as db:
        t = db.get(Track, track_id)
        if not t:
            return {"status": "failure", "error": "track not found"}
        artist, title, album, dur = t.artist_name, t.title, t.album_name, t.duration_sec
    result = lyrics_svc.fetch(artist=artist or "", title=title or "", album=album, duration_sec=dur)
    if not result or not result.get("text"):
        return {"status": "failure", "error": "Текст не найден в LRCLIB"}
    with session_scope() as db:
        exists = db.query(Lyrics).filter(Lyrics.track_id == track_id, Lyrics.provider == "lrclib").first()
        if not exists:
            db.add(Lyrics(track_id=track_id, provider="lrclib", text=result["text"],
                          synced=result.get("synced"), language=result.get("language"),
                          source_url=result.get("source_url")))
        try:
            ai_result = lyrics_ai.analyze(result["text"])
        except Exception:
            ai_result = None
        if ai_result:
            f = db.get(TrackFeatures, track_id)
            if f is None:
                f = TrackFeatures(track_id=track_id)
                db.add(f)
            for k in ("valence", "arousal", "energy"):
                if k in ai_result:
                    try:
                        setattr(f, k, float(ai_result[k]))
                    except Exception:
                        pass
            moods = (f.mood_labels or []) + ai_result.get("moods", [])
            f.mood_labels = list(dict.fromkeys([str(m).lower() for m in moods if str(m).strip()]))[:8]
    return {"status": "success", "track_id": track_id, "ai_analyzed": bool(ai_result)}


def analyze_single(run_id: str, track_id: str) -> dict:
    """Sonic-анализ одного трека (кнопка на странице трека)."""
    from app.core.config import get_settings
    from app.services import audio_analysis as aa

    try:
        import librosa  # noqa: F401
    except Exception:
        msg = "librosa не установлена — пересоберите backend-образ"
        _append_log(run_id, "error", msg)
        _finish_run(run_id, "failure", msg)
        return {"status": "failure", "error": msg}
    try:
        settings = get_settings()
        sample_seconds = int(settings.analysis_sample_seconds or 90)
        music_dir = settings.music_dir or os.getenv("MUSIC_DIR", "")
    except Exception:
        sample_seconds, music_dir = 90, os.getenv("MUSIC_DIR", "")
    with session_scope() as db:
        run = db.get(ScanRun, run_id)
        if run:
            run.total_items = 1
            run.processed_items = 0
        t = db.get(Track, track_id)
        if not t:
            _finish_run(run_id, "failure", "track not found")
            return {"status": "failure", "error": "track not found"}
        ext_id, tpath, dur = t.external_id, t.path, t.duration_sec
        label = f"{t.artist_name} — {t.title}"
        try:
            from app.services.media_server import get_media_server_config

            cfg = dict(get_media_server_config(db))
        except Exception:
            cfg = {}
    _append_log(run_id, "info", f"Анализирую: {label}")
    tmp_to_clean: Path | None = None
    try:
        local_path = aa.resolve_local_file(tpath, music_dir)
        if local_path is not None:
            src = local_path
        else:
            if not ext_id or ext_id.startswith("disk:"):
                raise RuntimeError("Локальный файл не найден (проверьте MUSIC_DIR/volumes)")
            src = aa.download_from_navidrome(ext_id, cfg)
            tmp_to_clean = Path(src)
        feats = aa.analyze_file(src, sample_seconds=sample_seconds, track_duration_sec=dur)
        with session_scope() as db:
            f = db.get(TrackFeatures, track_id)
            if f is None:
                f = TrackFeatures(track_id=track_id)
                db.add(f)
            for k in ("tempo_bpm", "key_name", "scale", "energy", "danceability",
                      "valence", "arousal", "loudness_db", "spectral_centroid",
                      "spectral_rolloff", "zero_crossing_rate", "mfcc_summary",
                      "chroma_summary", "mood_vector", "mood_labels"):
                setattr(f, k, feats.get(k))
            f.analyzed_at = datetime.utcnow()
            run = db.get(ScanRun, run_id)
            if run:
                run.processed_items = 1
        _append_log(run_id, "info", f"Готово: {label} — tempo {feats.get('tempo_bpm')}, key {feats.get('key_name')}")
        _finish_run(run_id, "success")
        return {"status": "success", "track_id": track_id}
    except Exception as e:  # noqa: BLE001
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}
    finally:
        if tmp_to_clean is not None:
            try:
                tmp_to_clean.unlink(missing_ok=True)
            except OSError:
                pass


def daily_playlist(playlist_id: str, *args, **kwargs) -> dict:
    """Сгенерировать/пересоздать ежедневный плейлист.
    Если на сегодня уже есть — удалить и сделать заново.
    Источник кандидатов: избранные + recently played + кластеры + genre match.
    """
    from app.db.models import Playlist, PlaylistTrack, Favorite, PlayHistory, MediaUser

    today = date.today()
    log_prefix = f"[daily {today.isoformat()}]"
    logger.info("{} start playlist_id={}", log_prefix, playlist_id)

    with session_scope() as db:
        # удалить прошлые авто-плейлисты за сегодня (если есть)
        old = (
            db.query(Playlist)
            .filter(
                Playlist.is_auto_generated.is_(True),
                Playlist.generated_for_date >= datetime(today.year, today.month, today.day),
            )
            .all()
        )
        for p in old:
            if str(p.id) != str(playlist_id):
                db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).delete()
                db.delete(p)

        playlist = db.get(Playlist, playlist_id)
        if not playlist:
            return {"status": "no playlist"}
        # очистим старые треки в этом плейлисте
        db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist.id).delete()

        # кандидаты: все треки
        candidates = db.query(Track).all()
        if not candidates:
            logger.warning("{} no tracks in library", log_prefix)
            return {"status": "empty library"}

        # веса: favorite → boost; недавние plays → boost; хорошие признаки → boost
        scores: dict[str, float] = {}
        for t in candidates:
            s = 1.0
            if t.starred:
                s += 3.0
            s += min(t.play_count or 0, 50) * 0.05
            if t.rating:
                s += float(t.rating)
            f = db.get(TrackFeatures, t.id)
            if f:
                if f.valence is not None:
                    s += float(f.valence) * 0.5
                if f.energy is not None:
                    s += float(f.energy) * 0.3
            scores[str(t.id)] = s

        # добавим треки из плейлистов юзеров (коллаборативный сигнал)
        server_id = playlist.server_id
        for pl in (
            db.query(Playlist)
            .filter(Playlist.server_id == server_id, Playlist.is_auto_generated.is_(False))
            .all()
        ):
            for pt in db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == pl.id).all():
                key = str(pt.track_id)
                scores[key] = scores.get(key, 1.0) + 0.4

        # cold-start: если совсем пусто, подмешаем recently played
        if not scores:
            for t in candidates[:30]:
                scores[str(t.id)] = 1.0

        # итог: top-30 по score, перемешаем чтобы не было скучно
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top = [tid for tid, _ in ordered[:30]]
        random.shuffle(top)

        # запишем
        for pos, tid in enumerate(top):
            db.add(
                PlaylistTrack(
                    playlist_id=playlist.id,
                    track_id=tid,
                    position=pos,
                )
            )
        # пометим playlist дату
        playlist.generated_for_date = datetime.utcnow()
        playlist.is_auto_generated = True

    logger.info("{} playlist {} tracks={}", log_prefix, playlist_id, len(top))
    return {"status": "success", "playlist_id": playlist_id, "tracks": len(top)}


def cluster_build(*args, **kwargs):
    """KMeans-кластеризация по sonic-фичам (пересборка с нуля).

    k подбирается под размер библиотеки: min(50, max(4, tracks//250)).
    Без проанализированных треков — честная ошибка с подсказкой.
    """
    run_id = args[0] if args else None
    try:
        from app.services.ml import build_clusters

        with session_scope() as db:
            run = db.get(ScanRun, run_id) if run_id else None
            server_id = run.server_id if run else None
            if not server_id:
                # нет run — берём активный сервер
                from app.services.media_server import resolve_active_server

                server_id = resolve_active_server(db).id
                db.commit()
            n_tracks = db.query(Track).filter_by(server_id=server_id).count()
            n_feats = (
                db.query(TrackFeatures)
                .join(Track, TrackFeatures.track_id == Track.id)
                .filter(Track.server_id == server_id)
                .count()
            )
        if n_tracks == 0:
            msg = "Библиотека пуста — сначала запустите сканирование библиотеки"
            raise RuntimeError(msg)
        if n_feats < 4:
            msg = (
                f"Проанализировано треков: {n_feats} — мало для кластеризации. "
                "Запустите Sonic-анализ, затем пересоберите кластеры."
            )
            raise RuntimeError(msg)
        # адаптивное k: ~1 кластер на 250 треков, в пределах 4..50
        k = max(4, min(50, n_tracks // 250))
        _append_log(run_id, "info", f"Треков: {n_tracks}, с фичами: {n_feats} — строю {k} кластеров (KMeans)…") if run_id else None
        res = build_clusters(server_id, k=k)
        if res.get("status") != "ok":
            raise RuntimeError(str(res.get("status")))
        summary = f"Готово — кластеров: {res['clusters']}, треков в кластерах: {res['tracks_clustered']}"
        if run_id:
            _append_log(run_id, "info", summary)
            with session_scope() as db:
                run = db.get(ScanRun, run_id)
                if run:
                    run.total_items = res["tracks_clustered"]
                    run.processed_items = res["tracks_clustered"]
            _finish_run(run_id, "success")
        return {"status": "success", **res}
    except Exception as e:  # noqa: BLE001
        logger.exception("cluster_build failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def sync_navidrome_users(server_id: str) -> dict:
    """Синхронизация пользователей из Navidrome (getUsers) в media_users."""
    import asyncio

    from app.services.navidrome.client import SubsonicClient, SubsonicAuth

    with session_scope() as db:
        from app.db.models import MediaServer
        from app.services.media_server import get_media_server_config

        cfg = get_media_server_config(db)
        server_row = db.get(MediaServer, server_id)
        url = (cfg.get("url") if cfg else "") or (server_row.url if server_row else "")
        user = (cfg.get("user") if cfg else "") or ""
        password = (cfg.get("password") if cfg else "") or ""
        if not url or not user:
            return {"status": "failure", "error": "Navidrome не настроен (url/user) — укажите его в Настройках → Медиа-сервер"}

        async def _fetch():
            async with SubsonicClient(url, SubsonicAuth(user=user, password=password), timeout=60.0) as client:
                return await client.get_users()

        try:
            users = asyncio.run(_fetch())
        except Exception as e:
            err = str(e)
            # getUsers требует прав администратора в Navidrome
            if "50" in err or "admin" in err.lower() or "forbidden" in err.lower() or "401" in err or "403" in err:
                return {"status": "failure",
                        "error": "Navidrome отклонил getUsers — для синхронизации нужен пользователь с правами администратора (либо добавьте пользователей вручную на странице «Пользователи»)"}
            return {"status": "failure", "error": f"getUsers failed: {e}"}

        from app.db.models import MediaUser

        added = updated = 0
        for u in users:
            ext = str(u.get("id") or u.get("username") or "")
            name = u.get("username") or ext
            if not ext:
                continue
            ex = db.query(MediaUser).filter_by(server_id=server_id, external_id=ext).first()
            if ex is None:
                db.add(MediaUser(id=str(uuid.uuid4()), server_id=server_id,
                                 external_id=ext[:128], username=str(name)[:128],
                                 is_admin=bool(u.get("adminRole"))))
                added += 1
            else:
                ex.username = str(name)[:128]
                ex.is_admin = bool(u.get("adminRole"))
                updated += 1
        return {"status": "success", "added": added, "updated": updated, "total": len(users)}


def yandex_enrich(*args, **kwargs):
    run_id = args[0] if args else None
    msg = "Yandex-обогащение ещё не реализовано"
    if run_id:
        _append_log(run_id, "error", msg)
        _finish_run(run_id, "failure", msg)
    return {"status": "not_implemented", "error": msg}


def collab_build(*args, **kwargs):
    """Коллаборативный проход: синхронизация пользователей Navidrome.

    Пользователи — источник коллаборативных сигналов (избранное, плейлисты).
    После синка логирует, сколько пользователей доступно для рекомендаций.
    """
    run_id = args[0] if args else None
    try:
        with session_scope() as db:
            run = db.get(ScanRun, run_id) if run_id else None
            server_id = run.server_id if run else None
            if not server_id:
                from app.services.media_server import resolve_active_server

                server_id = resolve_active_server(db).id
                db.commit()
            from app.db.models import MediaUser

            before = db.query(MediaUser).filter_by(server_id=server_id).count()
        if run_id:
            _append_log(run_id, "info", "Синхронизирую пользователей из Navidrome…")
        res = sync_navidrome_users(server_id)
        if res.get("status") != "success":
            raise RuntimeError(res.get("error", "sync failed"))
        with session_scope() as db:
            from app.db.models import MediaUser, Playlist

            total_users = db.query(MediaUser).filter_by(server_id=server_id).count()
            user_playlists = db.query(Playlist).filter_by(server_id=server_id, is_auto_generated=False).count()
        summary = (f"Пользователей: {total_users} (было {before}, +{res['added']}, обновлено {res['updated']}), "
                   f"пользовательских плейлистов: {user_playlists} — коллаборативные сигналы готовы")
        if run_id:
            _append_log(run_id, "info", summary)
            with session_scope() as db:
                run = db.get(ScanRun, run_id)
                if run:
                    run.total_items = total_users
                    run.processed_items = total_users
            _finish_run(run_id, "success")
        return {"status": "success", **res, "user_playlists": user_playlists}
    except Exception as e:  # noqa: BLE001
        logger.exception("collab_build failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}