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

    artists_new = albums_new = tracks_cnt = 0
    with session_scope() as db:
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
            if not db.query(Artist).filter_by(server_id=server_id, external_id=a_ext).first():
                db.add(Artist(server_id=server_id, external_id=a_ext, name=artist_name[:512]))
                artists_new += 1
            ex_b = db.query(Album).filter_by(server_id=server_id, external_id=b_ext).first()
            if not ex_b:
                db.add(Album(server_id=server_id, external_id=b_ext, title=album_name[:512],
                             artist_external_id=a_ext, artist_name=artist_name[:512],
                             year=tags["year"], genre=(tags["genre"] or "")[:256] or None))
                albums_new += 1
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            suffix = path.suffix.lower().lstrip(".")[:16] or None
            vals = dict(title=title[:1024], artist_external_id=a_ext, artist_name=artist_name[:512],
                        album_external_id=b_ext, album_name=album_name[:512],
                        genre=(tags["genre"] or "")[:256] or None,
                        duration_sec=tags["duration_sec"], year=tags["year"],
                        track_no=tags["track_no"], disc_no=tags["disc_no"],
                        bitrate=tags["bitrate"], suffix=suffix, size_bytes=size,
                        content_type=_CONTENT_BY_SUFFIX.get(suffix or ""),
                        path=str(path), play_count=0)
            ex_t = db.query(Track).filter_by(server_id=server_id, external_id=t_ext).first()
            if not ex_t:
                db.add(Track(id=str(uuid.uuid4()), server_id=server_id, external_id=t_ext, **vals))
            else:
                for k, v in vals.items():
                    if v is not None:
                        setattr(ex_t, k, v)
                ex_t.path = str(path)
            tracks_cnt += 1
            if (idx + 1) % 200 == 0:
                db.commit()
            if (idx + 1) % 500 == 0:
                _append_log(run_id, "info", f"С диска обработано файлов: {idx + 1}/{len(files)}")
    _append_log(run_id, "info", f"С диска загружено: треков {tracks_cnt}, новых альбомов {albums_new}, новых артистов {artists_new}")
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
                    _append_log(run_id, "info", "получаю список артистов…")
                    try:
                        artists = await client.get_artists()
                    except Exception as e:
                        if "timeout" in type(e).__name__.lower() or "timeout" in str(e).lower():
                            raise RuntimeError(
                                "Navidrome не отдал список артистов за 120 секунд. "
                                "Обычно это значит: библиотека очень большая или Navidrome "
                                "занят собственным сканированием папок — дождитесь его "
                                "завершения в интерфейсе Navidrome и запустите скан ещё раз."
                            ) from e
                        raise
                    total_artists = len(artists)
                    # для первого прогона берём только 100 артистов чтобы быстро показать результат
                    # повторный запуск догрузит следующих 100 (инкрементально)
                    limit = 100
                    with session_scope() as db2:
                        existing = db2.query(Artist).filter_by(server_id=server_row.id).count()
                    offset = existing  # уже загруженные пропускаем
                    # если уже всё загружено — начнём сначала (обновим)
                    if offset >= total_artists:
                        offset = 0
                        _append_log(run_id, "info", f"Все {total_artists} артистов уже загружены — обновляю")
                    remaining = total_artists - offset
                    display_total = min(remaining, limit)
                    _append_log(run_id, "info", f"Артистов в библиотеке: {total_artists}, уже в БД: {existing}, загружаю {display_total} начиная с {offset}")
                    # обновим total
                    with session_scope() as db2:
                        r2 = db2.get(ScanRun, run_id)
                        if r2:
                            r2.total_items = display_total
                            r2.processed_items = 0
                    for idx, a in enumerate(artists[offset:offset+limit]):
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
                        if (idx + 1) % 10 == 0:
                            _append_log(run_id, "info", f"Обработано артистов: {idx+1}/{display_total}, альбомов: {albums_cnt}, треков: {tracks_cnt}")
                _append_log(run_id, "info", f"Готово — артистов: {display_total}/{total_artists}, альбомов: {albums_cnt}, треков: {tracks_cnt}")
                if total_artists > display_total:
                    _append_log(run_id, "info", f"Быстрый старт завершён. Для полной загрузки {total_artists} артистов запустите сканирование ещё раз — догрузит остальных.")
                with session_scope() as db2:
                    r2 = db2.get(ScanRun, run_id)
                    if r2:
                        r2.total_items = display_total
                        r2.processed_items = display_total
                return {"artists": len(artists), "albums": albums_cnt, "tracks": tracks_cnt}

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
    """Тянет тексты с LRCLIB и анализирует настроение через AI. Берёт только 200 за раз, без удержания транзакции."""
    _append_log(run_id, "info", "Загрузка текстов (LRCLIB) — быстрый старт 200 треков")
    try:
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            tracks = (
                db.query(Track)
                .filter(
                    Track.server_id == run.server_id,
                    Track.artist_name.isnot(None),
                    Track.title.isnot(None),
                )
                .limit(200)
                .all()
            )
            # фильтруем уже имеющиеся
            todo = []
            for t in tracks:
                exists = db.query(Lyrics).filter(Lyrics.track_id == t.id, Lyrics.provider == "lrclib").first()
                if not exists:
                    todo.append(t)
            run.total_items = len(todo)
            run.processed_items = 0
            # копируем нужные поля чтобы не держать сессию
            todo_data = [
                (t.id, t.artist_name, t.title, t.album_name, t.duration_sec) for t in todo
            ]

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
        _finish_run(run_id, "success")
        return {"status": "success", "fetched": fetched, "analyzed": analyzed, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        logger.exception("lyrics_fetch failed: {}", e)
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


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
    run_id = args[0] if args else None
    msg = "Кластеризация ещё не реализована (следующий шаг после sonic-анализа)"
    if run_id:
        _append_log(run_id, "error", msg)
        _finish_run(run_id, "failure", msg)
    return {"status": "not_implemented", "error": msg}


def yandex_enrich(*args, **kwargs):
    run_id = args[0] if args else None
    msg = "Yandex-обогащение ещё не реализовано"
    if run_id:
        _append_log(run_id, "error", msg)
        _finish_run(run_id, "failure", msg)
    return {"status": "not_implemented", "error": msg}


def collab_build(*args, **kwargs):
    run_id = args[0] if args else None
    msg = "Коллаборативная фильтрация ещё не реализована"
    if run_id:
        _append_log(run_id, "error", msg)
        _finish_run(run_id, "failure", msg)
    return {"status": "not_implemented", "error": msg}