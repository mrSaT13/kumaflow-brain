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
        # Не перезаписываем отмену пользователем
        if run.status == "failure" and (run.error or "") == "Отменено пользователем":
            return
        run.status = status
        run.finished_at = datetime.utcnow()
        if error:
            run.error = error


def _write_mood_tags(path: Path, feats: dict, backup: bool = False) -> None:
    """Записать mood/genre/key/bpm в теги файла (mp3/flac/ogg/m4a). Тихо, без падения задачи."""
    if not path.is_file():
        return
    # не трогаем если файл read-only
    try:
        if not os.access(str(path), os.W_OK):
            return
    except Exception:
        return
    if backup:
        try:
            import shutil

            shutil.copy2(str(path), str(path) + ".bak")
        except Exception:
            pass
    try:
        from mutagen import File as _MF  # type: ignore
        from mutagen.id3 import ID3, TCON, TXXX, TBPM, TKEY  # type: ignore
    except Exception:
        return
    moods = ", ".join(feats.get("mood_labels") or [])[:200]
    key = f"{feats.get('key_name') or ''} {feats.get('scale') or ''}".strip()
    bpm = str(feats.get("tempo_bpm") or "")
    energy = feats.get("energy")
    try:
        audio = _MF(str(path), easy=False)
        if audio is None:
            return
        # MP3 — ID3
        if path.suffix.lower() == ".mp3":
            try:
                id3 = ID3(str(path))
            except Exception:
                id3 = ID3()
            if moods:
                id3.delall("TXXX")
                # сохраняем существующие TXXX кроме наших
                id3.add(TXXX(encoding=3, desc="MOOD", text=moods))
                if energy is not None:
                    id3.add(TXXX(encoding=3, desc="ENERGY", text=str(energy)))
            if key:
                id3.add(TKEY(encoding=3, text=key))
            if bpm:
                id3.add(TBPM(encoding=3, text=bpm))
            id3.save(str(path))
            return
        # FLAC/Ogg/Opus — VorbisComment; M4A — MP4
        if hasattr(audio, "tags") and audio.tags is not None:
            try:
                # общий путь для easy-совместимых
                audio.tags["mood"] = moods
                if key:
                    audio.tags["key"] = key
                if bpm:
                    audio.tags["bpm"] = bpm
                if energy is not None:
                    audio.tags["energy"] = str(energy)
                audio.save()
            except Exception:
                pass
    except Exception:
        pass


def _is_cancelled(run_id: str | None) -> bool:
    """Кооперативная отмена: пользователь нажал «Отменить» (статус failure)."""
    if not run_id:
        return False
    try:
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            if not run:
                return False
            return run.status == "failure" and (run.error or "") == "Отменено пользователем"
    except Exception:
        return False


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
                        if idx % 10 == 0 and _is_cancelled(run_id):
                            raise RuntimeError("Отменено пользователем")
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
                        if jdx % 10 == 0 and _is_cancelled(run_id):
                            raise RuntimeError("Отменено пользователем")
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
            # автослияние точных дублей (если включено в Библиотеке → чекбокс)
            try:
                from app.db.models import AppSetting as _AS

                with session_scope() as db3:
                    _row = db3.get(_AS, "dedup")
                    _auto = bool(dict(_row.value).get("auto_merge")) if _row and isinstance(_row.value, dict) else False
                if _auto:
                    from app.services import dedup as _dd

                    with session_scope() as db3:
                        _res = _dd.auto_merge_exact(db3)
                    _append_log(run_id, "info",
                                f"Автослияние дублей: групп {_res['groups']}, сшито треков {_res['merged']}")
            except Exception as e_dd:  # noqa: BLE001 — не валим сканирование
                _append_log(run_id, "warn", f"Автослияние дублей пропущено: {e_dd}")
            _finish_run(run_id, "success")
            combined = dict(result)
            combined["disk_tracks"] = disk["tracks"]
            if disk["tracks"] > 0:
                combined["mode"] = "navidrome+disk"
            return {"status": "success", **combined}
    except Exception as e:  # noqa: BLE001
        if "Отменено пользователем" in str(e) or _is_cancelled(run_id):
            logger.info("library_scan cancelled by user: {}", run_id)
            # Статус failure+«Отменено пользователем» уже выставил cancel_run —
            # не перезаписываем его.
            return {"status": "failure", "error": "Отменено пользователем"}
        logger.exception("library_scan failed: {}", e)
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        try:
            from app.services import notify as _notify

            with session_scope() as db:
                _notify.notify(db, "error", "Сканирование библиотеки упало",
                               str(e)[:300], link="/scans")
        except Exception:
            pass
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
        per_track_timeout = int(getattr(settings, "analysis_per_track_timeout_sec", 300) or 300)
        auto_continue = bool(getattr(settings, "analysis_auto_continue", True))
    except Exception:
        sample_seconds, default_limit, music_dir = 90, 0, os.getenv("MUSIC_DIR", "")
        per_track_timeout, auto_continue = 300, True
    # limit из API: 0 = «пачками, но всю библиотеку». chunk — размер одной пачки.
    requested = kwargs.get("limit", None)
    if requested is not None and int(requested or 0) > 0:
        chunk_size = int(requested)
        do_all = False  # явный limit — только N штук
    else:
        # 0/None: берём из настроек; 0 там значит «вся библиотека», пачками по 200
        if default_limit and int(default_limit) > 0:
            chunk_size = int(default_limit)
        else:
            chunk_size = 200
        do_all = True

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
        # общий счётчик для всего прогона
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            if not run:
                return {"status": "no run"}
            q0 = db.query(Track).outerjoin(TrackFeatures, TrackFeatures.track_id == Track.id)
            if not force:
                q0 = q0.filter(TrackFeatures.track_id.is_(None))
            total_pending_initial = q0.count()
            run.total_items = total_pending_initial if do_all else min(total_pending_initial, chunk_size)
            run.processed_items = 0

        with session_scope() as db:
            try:
                from app.services.media_server import get_media_server_config

                cfg: dict = dict(get_media_server_config(db))
            except Exception as e:
                logger.warning("get_media_server_config failed: {}", e)
                cfg = {}

        ok = fail = n_local = n_stream = total_processed = 0
        while True:
            # берём следующую пачку непроанализированных
            with session_scope() as db:
                q = db.query(Track).outerjoin(TrackFeatures, TrackFeatures.track_id == Track.id)
                if not force:
                    q = q.filter(TrackFeatures.track_id.is_(None))
                q = q.order_by(Track.created_at.asc())
                batch = q.limit(chunk_size).all()
                todo_data = [
                    (t.id, t.external_id, t.path, t.suffix, t.duration_sec, t.title, t.artist_name)
                    for t in batch
                ]
                if not todo_data:
                    break
                # если делаем всю библиотеку — total остаётся исходным, иначе это один chunk
                if do_all:
                    run2 = db.get(ScanRun, run_id)
                    if run2 and run2.total_items < total_pending_initial:
                        run2.total_items = total_pending_initial

            for tid, ext_id, tpath, suffix, dur, title, artist in todo_data:
                if _is_cancelled(run_id):
                    _append_log(run_id, "warn", f"Остановлено пользователем на {total_processed}/{total_pending_initial}")
                    return {"status": "failure", "error": "Отменено пользователем", "ok": ok, "failed": fail}
                tmp_to_clean: Path | None = None
                try:
                    # Видно в docker logs даже если трек зависнет (в БД пишем только итог/ошибки, чтобы не спамить 150k строк)
                    logger.info("Анализ: {} — {} ({})", artist, title, tid)
                    t0 = time.monotonic()

                    def _do_one():
                        _tmp: Path | None = None
                        _local = aa.resolve_local_file(tpath, music_dir)
                        if _local is not None:
                            return ("local", _local, None, aa.analyze_file(_local, sample_seconds=sample_seconds, track_duration_sec=dur))
                        if not ext_id:
                            raise RuntimeError("нет external_id и нет локального файла")
                        if str(ext_id).startswith("disk:"):
                            raise RuntimeError(f"локальный файл не найден: {tpath} (проверьте MUSIC_DIR/volumes)")
                        _src = aa.download_from_navidrome(ext_id, cfg)
                        _tmp = Path(_src)
                        try:
                            _feats = aa.analyze_file(_src, sample_seconds=sample_seconds, track_duration_sec=dur)
                        except Exception:
                            try:
                                _tmp.unlink(missing_ok=True)
                            except OSError:
                                pass
                            raise
                        return ("stream", _src, _tmp, _feats)

                    import concurrent.futures as _fut

                    _ex = _fut.ThreadPoolExecutor(max_workers=1)
                    try:
                        _fut_obj = _ex.submit(_do_one)
                        try:
                            kind, src, _tmp_got, feats = _fut_obj.result(timeout=per_track_timeout)
                        except _fut.TimeoutError:
                            fail += 1
                            _append_log(run_id, "warn", f"Пропущен зависший (>{per_track_timeout}с): {artist} — {title}")
                            logger.warning("Skip slow track >{}s: {} — {}", per_track_timeout, artist, title)
                            try:
                                _ex.shutdown(wait=False, cancel_futures=True)
                            except Exception:
                                pass
                            continue
                    finally:
                        # зависший поток librosa может ещё жить в фоне — следующий трек всё равно пойдёт;
                        # shutdown(wait=False) чтобы не ждать его здесь
                        try:
                            _ex.shutdown(wait=False, cancel_futures=True)
                        except Exception:
                            pass
                    if _tmp_got is not None:
                        tmp_to_clean = _tmp_got
                    if kind == "local":
                        n_local += 1
                    else:
                        n_stream += 1
                        tmp_to_clean = _tmp_got
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
                            run.processed_items = total_processed + 1
                    # --- MUTAGEN_WRITEBACK: тихо пишем mood/genre/key/bpm в файл, если включен ---
                    try:
                        from app.core.config import get_settings as _gs

                        s = _gs()
                        _local_for_tags = src if kind == "local" else None
                        if s.mutagen_writeback and _local_for_tags is not None:
                            # только локальные файлы, только если rw
                            _write_mood_tags(Path(_local_for_tags), feats, backup=s.mutagen_writeback_backup)
                    except Exception as _e:
                        logger.warning("mutagen writeback failed for {}: {}", src, _e)
                    ok += 1
                    try:
                        _dt = time.monotonic() - t0
                        if _dt > 30:
                            logger.info("Долго: {} — {} за {:.0f}с", artist, title, _dt)
                    except Exception:
                        pass
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    _append_log(run_id, "warn", f"Не проанализирован: {artist} — {title}: {e}")
                finally:
                    if tmp_to_clean is not None:
                        try:
                            tmp_to_clean.unlink(missing_ok=True)
                        except OSError:
                            pass
                total_processed += 1
                if total_processed % 25 == 0:
                    _append_log(run_id, "info", f"Обработано {total_processed}/{total_pending_initial} (ок: {ok}, ошибок: {fail}, пачка {chunk_size})")
            # если запрошен только один chunk — выходим
            if not do_all:
                break
            # иначе цикл возьмёт следующую пачку, пока не кончатся

        with session_scope() as db:
            q = db.query(Track).outerjoin(TrackFeatures, TrackFeatures.track_id == Track.id)
            if not force:
                q = q.filter(TrackFeatures.track_id.is_(None))
            remaining = q.count()
        summary = f"Готово — ок: {ok}, ошибок: {fail} (локальные файлы: {n_local}, стрим из Navidrome: {n_stream})."
        if remaining > 0:
            summary += f" Осталось без анализа: {remaining} (прервано/ошибка?)."
        else:
            summary += " Вся библиотека проанализирована."
        _append_log(run_id, "info", summary)
        _finish_run(run_id, "success")
        # Автопродолжение: один job = один чанк, дальше ставим следующий сам (ночь переживает таймауты)
        if remaining > 0 and do_all and auto_continue:
            try:
                from app.services.queue import enqueue as _enq

                with session_scope() as db:
                    _run = db.get(ScanRun, run_id)
                    _srv_id = str(_run.server_id) if _run and _run.server_id else None
                    _new_id = str(uuid.uuid4())
                    if _srv_id:
                        db.add(ScanRun(id=_new_id, server_id=_srv_id, phase="analysis",
                                       status="running", total_items=remaining, processed_items=0,
                                       started_at=datetime.utcnow()))
                _job = _enq(sonic_analysis, _new_id, job_timeout=7200, force=force, limit=chunk_size)
                with session_scope() as db:
                    _nr = db.get(ScanRun, _new_id)
                    if _nr is not None:
                        try:
                            _nr.metadata_extra = dict(_nr.metadata_extra or {}) | {"job_id": _job}
                        except Exception:
                            _nr.metadata_extra = {"job_id": _job}
                        db.add(ScanLog(id=str(uuid.uuid4()), run_id=_new_id, level="info",
                                       message=f"Автопродолжение анализа: осталось {remaining} (job {_job})"))
                _append_log(run_id, "info", f"Поставлен следующий чанк: осталось {remaining} (run {_new_id[:8]})")
            except Exception as _ce:  # noqa: BLE001
                logger.warning("analysis auto-continue failed: {}", _ce)
                _append_log(run_id, "warn", f"Автопродолжение не встало: {_ce}")
        return {"status": "success", "ok": ok, "failed": fail, "remaining": remaining, "total": total_pending_initial}
    except Exception as e:  # noqa: BLE001
        logger.exception("sonic_analysis failed: {}", e)
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        # Даже если RQ убил job по timeout — пробуем продолжить остаток новым job'ом
        try:
            _msg = str(e)
            if do_all and auto_continue and ("timeout" in _msg.lower() or "exceeded" in _msg.lower()):
                from app.services.queue import enqueue as _enq2

                with session_scope() as db:
                    _run = db.get(ScanRun, run_id)
                    _srv_id = str(_run.server_id) if _run and _run.server_id else None
                    _new_id = str(uuid.uuid4())
                    if _srv_id:
                        db.add(ScanRun(id=_new_id, server_id=_srv_id, phase="analysis",
                                       status="running", total_items=0, processed_items=0,
                                       started_at=datetime.utcnow()))
                _job = _enq2(sonic_analysis, _new_id, job_timeout=7200, force=force, limit=chunk_size)
                _append_log(_new_id, "info", f"Перезапуск после таймаута (прошлый run {run_id[:8]}, job {_job})")
        except Exception as _ce2:  # noqa: BLE001
            logger.warning("analysis resume-after-timeout failed: {}", _ce2)
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
            # Охват — вся база (раньше только run.server_id, диск выпадал).
            todo_q = (
                db.query(Track)
                .filter(
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
            if _is_cancelled(run_id):
                _append_log(run_id, "warn", f"Остановлено пользователем на {i}/{len(todo_data)}")
                return {"status": "failure", "error": "Отменено пользователем",
                        "fetched": fetched, "analyzed": analyzed, "skipped": skipped}
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
            have2 = db.query(Lyrics.track_id).filter(Lyrics.provider == "lrclib").subquery()
            remaining = (
                db.query(Track)
                .filter(
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
            # Кластеризуем ВСЮ базу (Navidrome + диск). server_id оставляем
            # только для совместимости сигнатуры build_clusters.
            n_tracks = db.query(Track).count()
            n_feats = (
                db.query(TrackFeatures)
                .join(Track, TrackFeatures.track_id == Track.id)
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
    """Обогащение метаданных через Yandex Music (stealth, кэш, throttle).

    Берёт треки без yandex-enrich (пачками по 20), ищет через api.music.yandex.net
    и пишет в TrackMetadataEnrich source='yandex'. Пауза 1.2s между запросами,
    чтобы не палить токен (429 → backoff).
    """
    run_id = args[0] if args else None
    try:
        from app.db.models import TrackMetadataEnrich

        with session_scope() as db:
            run = db.get(ScanRun, run_id) if run_id else None
            total = run.total_items if run else 0
        _append_log(run_id, "info", f"Yandex enrich: до {total} треков (throttle 1.2s)")
        ok = fail = 0
        with session_scope() as db:
            have = db.query(TrackMetadataEnrich.track_id).filter(TrackMetadataEnrich.source == "yandex").subquery()
            todos = db.query(Track).filter(~Track.id.in_(db.query(have.c.track_id))).limit(total or 20).all()
            todo_data = [(t.id, t.artist_name or "", t.title or "", t.album_name or "") for t in todos]
        import asyncio as _aio

        from app.services.yandex_music.client import fetch_metadata_sync

        for idx, (tid, artist, title, album) in enumerate(todo_data):
            if _is_cancelled(run_id):
                _append_log(run_id, "warn", f"Остановлено на {idx}/{len(todo_data)}")
                break
            if not artist or not title:
                fail += 1
                continue
            try:
                with session_scope() as db:
                    res = fetch_metadata_sync(db, artist, title, album)
                    if res:
                        # защита от дубля
                        ex = db.query(TrackMetadataEnrich).filter_by(track_id=tid, source="yandex").first()
                        if ex is None:
                            db.add(TrackMetadataEnrich(track_id=tid, source="yandex", data=res))
                        else:
                            ex.data = res
                        ok += 1
                    else:
                        fail += 1
                        _append_log(run_id, "warn", f"Yandex не нашёл: {artist} — {title}")
                    run = db.get(ScanRun, run_id) if run_id else None
                    if run:
                        run.processed_items = idx + 1
                time.sleep(0.1)  # доп. пауза внутри пачки, основная — в client _throttle
            except Exception as e:  # noqa: BLE001
                fail += 1
                _append_log(run_id, "warn", f"Yandex err {artist} — {title}: {e}")
            if (idx + 1) % 10 == 0:
                _append_log(run_id, "info", f"Yandex {idx+1}/{len(todo_data)} ok:{ok} fail:{fail}")
        _append_log(run_id, "info", f"Yandex готово ok:{ok} fail:{fail}")
        _finish_run(run_id, "success")
        return {"status": "success", "ok": ok, "fail": fail}
    except Exception as e:  # noqa: BLE001
        logger.exception("yandex_enrich failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def clap_embed(*args, **kwargs):
    """Backfill CLAP эмбеддингов: текст -> TrackEmbedding (для search-by-text).

    Если модели нет — тихо skip (keyword fallback остаётся). Пишет 512-дим vec в TrackEmbedding.
    """
    run_id = args[0] if args else None
    try:
        from app.db.models import TrackEmbedding
        from app.services.clap import get_text_embedding, is_available

        if not is_available():
            msg = "CLAP модель не найдена (server/ml/models/*.onnx) — пропуск, keyword поиск работает. Запустите python ml/download_clap.py"
            _append_log(run_id, "warn", msg)
            _finish_run(run_id, "success")
            return {"status": "success", "skipped": True, "reason": msg}
        with session_scope() as db:
            run = db.get(ScanRun, run_id) if run_id else None
            total = db.query(Track).count()
            if run:
                run.total_items = total
                run.processed_items = 0
            have = db.query(TrackEmbedding.track_id).filter(TrackEmbedding.model == "clap_text").subquery()
            todos = db.query(Track).filter(~Track.id.in_(db.query(have.c.track_id))).all()
            _append_log(run_id, "info", f"CLAP embed: {len(todos)}/{total} без эмбеддингов (512-dim)")
            ok = 0
            for idx, t in enumerate(todos):
                if _is_cancelled(run_id):
                    break
                text = f"{t.artist_name or ''} {t.title or ''} {t.genre or ''}".strip()
                vec = get_text_embedding(text)
                if vec:
                    import struct

                    blob = np.array(vec, dtype=np.float32).tobytes()  # type: ignore
                    db.add(TrackEmbedding(track_id=t.id, model="clap_text", dim=len(vec), vector=blob))
                    ok += 1
                if run:
                    run.processed_items = idx + 1
                if (idx + 1) % 100 == 0:
                    _append_log(run_id, "info", f"CLAP {idx+1}/{len(todos)} ok:{ok}")
                    db.commit()
            db.commit()
        _append_log(run_id, "info", f"CLAP готово ok:{ok}")
        _finish_run(run_id, "success")
        return {"status": "success", "ok": ok}
    except Exception as e:  # noqa: BLE001
        logger.exception("clap_embed failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def daily_per_user(*args, **kwargs):
    """Крон 03:00 per-user: генерирует KumaFlow Daily для каждого MediaUser (копия mobile daily)."""
    run_id = args[0] if args else None
    try:
        from app.db.models import MediaUser, Playlist, PlaylistTrack
        from app.services.ml import cold_start_playlist
        from app.services.orchestrator import create_energy_wave
        from app.services.media_server import resolve_active_server

        with session_scope() as db:
            server = resolve_active_server(db)
            users = db.query(MediaUser).filter_by(server_id=server.id).all()
            db.commit()
        total = max(1, len(users))
        with session_scope() as db:
            run = db.get(ScanRun, run_id) if run_id else None
            if run:
                run.total_items = total
                run.processed_items = 0
        ok = 0
        for idx, u in enumerate(users):
            if _is_cancelled(run_id):
                break
            try:
                # используем API логику generate-daily per-user
                from datetime import date, datetime
                import uuid as _uuid

                today = date.today()
                with session_scope() as db:
                    # удалить старый daily этого юзера
                    old = db.query(Playlist).filter(Playlist.is_auto_generated.is_(True), Playlist.server_id == u.server_id, Playlist.owner_user_id == u.id).all()
                    for p in old:
                        if p.generated_for_date and p.generated_for_date.date() >= today:
                            db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).delete()
                            db.delete(p)
                    db.flush()
                    p = Playlist(id=str(_uuid.uuid4()), server_id=u.server_id, owner_user_id=u.id, name=f"KumaFlow Daily · {today.isoformat()}", is_auto_generated=True, generated_for_date=datetime.combine(today, datetime.min.time()))
                    db.add(p)
                    db.flush()
                    res = cold_start_playlist(str(u.server_id), n=30, user_id=str(u.id))
                    tids = res.get("tracks") or []
                    # волна
                    try:
                        tracks = [db.get(Track, tid) for tid in tids]
                        tracks = [t for t in tracks if t]
                        waved = create_energy_wave(tracks)
                        order = {str(t.id): i for i, t in enumerate(waved)}
                        tids = sorted(tids, key=lambda tid: order.get(tid, 999))
                    except Exception:
                        pass
                    for pos, tid in enumerate(tids):
                        db.add(PlaylistTrack(playlist_id=p.id, track_id=tid, position=pos))
                    ok += 1
                _append_log(run_id, "info", f"Daily {u.username}: {len(tids)} треков")
            except Exception as e:
                _append_log(run_id, "warn", f"Daily {u.username} fail: {e}")
            with session_scope() as db:
                run = db.get(ScanRun, run_id) if run_id else None
                if run:
                    run.processed_items = idx + 1
        # fallback если юзеров нет — один глобальный daily
        if not users:
            with session_scope() as db:
                server = resolve_active_server(db)
                from datetime import date, datetime
                import uuid as _uuid

                today = date.today()
                p = Playlist(id=str(_uuid.uuid4()), server_id=server.id, name=f"KumaFlow Daily · {today.isoformat()}", is_auto_generated=True, generated_for_date=datetime.combine(today, datetime.min.time()))
                db.add(p)
                db.flush()
                res = cold_start_playlist(str(server.id), n=30)
                for pos, tid in enumerate(res.get("tracks") or []):
                    db.add(PlaylistTrack(playlist_id=p.id, track_id=tid, position=pos))
                ok = 1
        _append_log(run_id, "info", f"Daily per-user готово: {ok}/{total}")
        _finish_run(run_id, "success")
        return {"status": "success", "users": total, "ok": ok}
    except Exception as e:  # noqa: BLE001
        logger.exception("daily_per_user failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def smart_playlists(*args, **kwargs):
    """Умные автоплейлисты для КАЖДОГО пользователя (открытия/забытые/ночь/спорт).

    Старый плейлист того же вида у пользователя заменяется (не копим).
    Каждому пользователю — уведомление в колокол со ссылкой на плейлисты.
    """
    run_id = args[0] if args else None
    try:
        from datetime import date, datetime
        import uuid as _uuid

        from app.db.models import MediaUser, Playlist, PlaylistTrack, Track
        from app.services import smart as _smart
        from app.services.media_server import resolve_active_server
        from app.services.orchestrator import create_energy_wave

        with session_scope() as db:
            server = resolve_active_server(db)
            users = db.query(MediaUser).filter_by(server_id=server.id).all()
            db.commit()
        total = max(1, len(users))
        with session_scope() as db:
            run = db.get(ScanRun, run_id) if run_id else None
            if run:
                run.total_items = total * len(_smart.KINDS)
                run.processed_items = 0
        done = 0
        today = date.today()
        for idx, u in enumerate(users):
            if _is_cancelled(run_id):
                break
            made: list[str] = []
            for kind, label in _smart.KINDS.items():
                try:
                    with session_scope() as db:
                        old = db.query(Playlist).filter(
                            Playlist.is_auto_generated.is_(True),
                            Playlist.server_id == u.server_id,
                            Playlist.owner_user_id == u.id,
                            Playlist.name.like(f"{label} %")).all()
                        for p in old:
                            db.query(PlaylistTrack).filter(
                                PlaylistTrack.playlist_id == p.id).delete()
                            db.delete(p)
                        db.flush()
                        tids = _smart.GENERATORS[kind](db, str(u.id), n=30)
                        if not tids:
                            continue
                        try:
                            tracks = [t for t in
                                      (db.get(Track, tid) for tid in tids) if t]
                            waved = create_energy_wave(tracks)
                            order = {str(t.id): i for i, t in enumerate(waved)}
                            tids = sorted(tids, key=lambda tid: order.get(tid, 999))
                        except Exception:
                            pass
                        p = Playlist(
                            id=str(_uuid.uuid4()), server_id=u.server_id,
                            owner_user_id=u.id,
                            name=f"{label} · {today.isoformat()}",
                            is_auto_generated=True,
                            generated_for_date=datetime.combine(
                                today, datetime.min.time()))
                        db.add(p)
                        db.flush()
                        for pos, tid in enumerate(tids):
                            db.add(PlaylistTrack(playlist_id=p.id, track_id=tid,
                                                 position=pos))
                        made.append(f"{label} ({len(tids)})")
                except Exception as e:  # noqa: BLE001 — один вид не валит остальных
                    if run_id:
                        _append_log(run_id, "warn", f"Smart {u.username}/{kind} fail: {e}")
                with session_scope() as db:
                    run = db.get(ScanRun, run_id) if run_id else None
                    if run:
                        run.processed_items = (run.processed_items or 0) + 1
            if made:
                done += 1
                if run_id:
                    _append_log(run_id, "info", f"Smart {u.username}: {', '.join(made)}")
                try:
                    from app.services import notify as _notify

                    with session_scope() as db:
                        _notify.notify(db, "success", "Умные плейлисты готовы",
                                       f"{u.username}: {', '.join(made)}",
                                       user_id=str(u.id), link="/playlists")
                except Exception:
                    pass
        summary = f"Smart-плейлисты: пользователей {done}/{total}"
        if run_id:
            _append_log(run_id, "info", summary)
            _finish_run(run_id, "success")
        try:
            from app.services import notify as _notify

            with session_scope() as db:
                _notify.notify(db, "success", "Умные плейлисты обновлены",
                               summary, link="/playlists")
                _notify.prune(db)
        except Exception:
            pass
        return {"status": "success", "users": total, "ok": done}
    except Exception as e:  # noqa: BLE001
        logger.exception("smart_playlists failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def taste_snapshots(*args, **kwargs):
    """Недельные слепки вкуса (крон по понедельникам): тихо, без уведомлений."""
    run_id = args[0] if args else None
    try:
        from app.db.models import MediaUser
        from app.services import drift as _drift
        from app.services.media_server import resolve_active_server

        with session_scope() as db:
            server = resolve_active_server(db)
            users = db.query(MediaUser).filter_by(server_id=server.id).all()
            db.commit()
        ok = 0
        drift_sent = 0
        for u in users:
            try:
                with session_scope() as db:
                    res = _drift.take_snapshot(db, str(u.id))
                    if res.get("ok"):
                        ok += 1
                    # Drift radar: сравнить с 4-недельным снапшотом и пушнуть в колокол при сильном сдвиге
                    try:
                        cmp = _drift.compare(db, str(u.id), weeks_ago=4)
                        if cmp.get("ok") and cmp.get("summary"):
                            up = (cmp.get("genres_up") or [{}])[0] or {}
                            dn = (cmp.get("genres_down") or [{}])[0] or {}
                            try:
                                du = abs(float(up.get("delta") or 0))
                            except Exception:
                                du = 0
                            try:
                                dd = abs(float(dn.get("delta") or 0))
                            except Exception:
                                dd = 0
                            if du >= 1.0 or dd >= 1.0:
                                from app.db.models import Notification as _N
                                from app.services import notify as _notify

                                exists = db.query(_N).filter(
                                    _N.user_id == str(u.id), _N.kind == "drift",
                                    _N.read_at.is_(None)).first()
                                if not exists:
                                    _notify.notify(
                                        db, "drift", f"Дрейф вкуса: {cmp['summary']}",
                                        f"{up.get('name','')} {up.get('old','')}→{up.get('new','')}; "
                                        f"{dn.get('name','')} {dn.get('old','')}→{dn.get('new','')} "
                                        f"(с {cmp.get('snapshot_week','')})",
                                        user_id=str(u.id), link=f"/users/{u.id}")
                                    drift_sent += 1
                    except Exception:
                        pass
            except Exception as e:  # noqa: BLE001
                if run_id:
                    _append_log(run_id, "warn", f"Snapshot {u.username} fail: {e}")
        if run_id:
            _append_log(run_id, "info", f"Слепки вкуса: {ok}/{len(users)}, дрейф-уведомлений: {drift_sent}")
            _finish_run(run_id, "success")
        return {"status": "success", "users": len(users), "ok": ok, "drift": drift_sent}
    except Exception as e:  # noqa: BLE001
        logger.exception("taste_snapshots failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


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


def refresh_tastes(*args, **kwargs):
    """Ночное автообновление вкусов по запомненным паролям (opt-in vault).

    Для каждого UserCredential: расшифровать (ключ TASTE_VAULT_KEY) и прогнать
    import_user_tastes. Без ключа — мягкий успех с пометкой (фича выключена).
    """
    run_id = args[0] if args else None
    only_user = kwargs.get("user_id") or (args[1] if len(args) > 1 else None)
    try:
        from app.db.models import MediaUser, UserCredential
        from app.services import vault as _vault
        from app.services.taste_import import import_user_tastes

        with session_scope() as db:
            q = db.query(UserCredential)
            if only_user:
                q = q.filter_by(user_id=str(only_user))
            pairs = [(str(r.user_id), bytes(r.enc_password))
                     for r in q.all()]
        if run_id:
            with session_scope() as db:
                run = db.get(ScanRun, run_id)
                if run:
                    run.total_items = max(1, len(pairs))
                    run.processed_items = 0
        if not pairs:
            msg = "Нет запомненных паролей — автообновление нечего делать (opt-in в карточке пользователя)"
            if run_id:
                _append_log(run_id, "info", msg)
                _finish_run(run_id, "success")
            return {"status": "success", "users": 0, "ok": 0, "note": msg}
        if not _vault.vault_available():
            msg = "TASTE_VAULT_KEY не задан — автообновление пропущено (пароли не расшифровать)"
            if run_id:
                _append_log(run_id, "warn", msg)
                _finish_run(run_id, "success")
            return {"status": "success", "users": len(pairs), "ok": 0, "note": msg}
        ok = fail = 0
        for idx, (uid, blob) in enumerate(pairs):
            if _is_cancelled(run_id):
                break
            try:
                password = _vault.decrypt_password(blob)
            except Exception as e:
                if run_id:
                    _append_log(run_id, "warn", f"Сейф {uid[:8]} не открылся ({e}) — пропустите/перезапомните пароль")
                fail += 1
                continue
            with session_scope() as db:
                u = db.get(MediaUser, uid)
                if not u:
                    fail += 1
                    continue
                uname, sid = u.external_id, str(u.server_id)
            try:
                res = import_user_tastes(sid, uname, password)
            except Exception as e:  # noqa: BLE001
                res = {"status": "failure", "error": str(e)}
            if res.get("status") == "success":
                ok += 1
                if run_id:
                    _append_log(run_id, "info",
                                f"{uname}: ★ всего {res.get('favorites_total')}, плейлистов {res.get('playlists')}")
            else:
                fail += 1
                if run_id:
                    _append_log(run_id, "warn", f"{uname}: {res.get('error')}")
            if run_id:
                with session_scope() as db:
                    run = db.get(ScanRun, run_id)
                    if run:
                        run.processed_items = idx + 1
        summary = f"Автообновление вкусов: ок {ok}, ошибок {fail} (пользователей {len(pairs)})"
        if run_id:
            _append_log(run_id, "info", summary)
            _finish_run(run_id, "success")
        try:
            from app.services import notify as _notify

            with session_scope() as db:
                _notify.notify(db, "success" if fail == 0 else "warn",
                               "Ночное обновление вкусов",
                               summary, link="/scans")
                _notify.prune(db)
        except Exception:
            pass
        return {"status": "success", "users": len(pairs), "ok": ok, "failed": fail}
    except Exception as e:  # noqa: BLE001
        logger.exception("refresh_tastes failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        try:
            from app.services import notify as _notify

            with session_scope() as db:
                _notify.notify(db, "error", "Ночное обновление вкусов упало",
                               str(e)[:300], link="/scans")
        except Exception:
            pass
        return {"status": "failure", "error": str(e)}


def weekly_discovery_all(*args, **kwargs):
    """Крон по понедельникам: Открытия недели (CLAP) для каждого пользователя."""
    run_id = args[0] if args else None
    try:
        from datetime import date, datetime

        from app.db.models import MediaUser, Playlist, PlaylistTrack, Track
        from app.services.discovery import weekly_discovery as _wd
        from app.services.media_server import resolve_active_server
        from app.services.orchestrator import create_energy_wave

        with session_scope() as db:
            server = resolve_active_server(db)
            users = db.query(MediaUser).filter_by(server_id=server.id).all()
            db.commit()
        ok = 0
        for idx, u in enumerate(users):
            if _is_cancelled(run_id):
                break
            try:
                with session_scope() as db:
                    res = _wd(db, str(u.id), n=30)
                    tids = res.get("tracks") or []
                    try:
                        tracks = [db.get(Track, tid) for tid in tids]
                        tracks = [t for t in tracks if t]
                        waved = create_energy_wave(tracks)
                        order = {str(t.id): i for i, t in enumerate(waved)}
                        tids = sorted(tids, key=lambda tid: order.get(tid, 999))
                    except Exception:
                        pass
                    for p in db.query(Playlist).filter(
                            Playlist.server_id == u.server_id, Playlist.owner_user_id == u.id,
                            Playlist.is_auto_generated.is_(True),
                            Playlist.name.like("Открытия недели%")).all():
                        db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == p.id).delete()
                        db.delete(p)
                    db.flush()
                    today = date.today()
                    import uuid as _uuid

                    p = Playlist(id=str(_uuid.uuid4()), server_id=u.server_id, owner_user_id=u.id,
                                 name=f"Открытия недели · {today.isoformat()}", is_auto_generated=True,
                                 generated_for_date=datetime.combine(today, datetime.min.time()))
                    db.add(p)
                    db.flush()
                    for pos, tid in enumerate(tids):
                        db.add(PlaylistTrack(playlist_id=p.id, track_id=tid, position=pos))
                    try:
                        from app.services import notify as _notify

                        _notify.notify(db, "discovery", "Открытия недели готовы",
                                       f"{u.username}: {len(tids)} треков ({res.get('mode')})",
                                       user_id=str(u.id), link=f"/playlists/{p.id}")
                    except Exception:
                        pass
                    ok += 1
                if run_id:
                    _append_log(run_id, "info", f"Weekly {u.username}: {len(tids)} ({res.get('mode')})")
            except Exception as e:  # noqa: BLE001
                if run_id:
                    _append_log(run_id, "warn", f"Weekly {u.username} fail: {e}")
            if run_id:
                with session_scope() as db:
                    run = db.get(ScanRun, run_id) if run_id else None
                    if run:
                        run.processed_items = idx + 1
        if run_id:
            _append_log(run_id, "info", f"Открытия недели: {ok}/{len(users)}")
            _finish_run(run_id, "success")
        return {"status": "success", "users": len(users), "ok": ok}
    except Exception as e:  # noqa: BLE001
        logger.exception("weekly_discovery_all failed: {}", e)
        if run_id:
            _append_log(run_id, "error", str(e))
            _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}