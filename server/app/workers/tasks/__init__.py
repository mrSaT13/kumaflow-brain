from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, date

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

            # если это демо-сервер — просто считаем
            if server_row.type == "demo" or server_row.url in ("http://localhost", "https://localhost"):
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
                async with SubsonicClient(url, auth) as client:
                    ok = await client.ping()
                    if not ok:
                        raise RuntimeError("ping failed — проверьте URL/логин/пароль")
                    _append_log(run_id, "info", "ping ok, получаю список артистов…")
                    artists = await client.get_artists()
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

            result = asyncio.run(_do_fetch())
            _finish_run(run_id, "success")
            return {"status": "success", **result, "mode": "navidrome"}
    except Exception as e:  # noqa: BLE001
        logger.exception("library_scan failed: {}", e)
        _append_log(run_id, "error", str(e))
        _finish_run(run_id, "failure", str(e))
        return {"status": "failure", "error": str(e)}


def sonic_analysis(run_id: str, *args, **kwargs) -> dict:
    _append_log(run_id, "info", "Sonic-анализ запущен (демо: синтетические признаки)")
    try:
        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            tracks = db.query(Track).filter_by(server_id=run.server_id).all()
            run.total_items = len(tracks)
            run.processed_items = 0

        keys = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        scales = ["major", "minor"]
        moods_pool = ["энергичный", "меланхоличный", "спокойный", "агрессивный", "мечтательный", "тёплый", "тёмный", "лёгкий"]

        with session_scope() as db:
            run = db.get(ScanRun, run_id)
            for i, t in enumerate(tracks):
                f = db.get(TrackFeatures, t.id)
                if f is None:
                    f = TrackFeatures(track_id=t.id)
                    db.add(f)
                rnd = random.Random(t.id)
                f.tempo_bpm = round(rnd.uniform(70, 160), 1)
                f.key_name = rnd.choice(keys)
                f.scale = rnd.choice(scales)
                f.energy = round(rnd.uniform(0.1, 0.95), 3)
                f.danceability = round(rnd.uniform(0.1, 0.95), 3)
                f.valence = round(rnd.uniform(0.1, 0.95), 3)
                f.arousal = round(rnd.uniform(0.1, 0.95), 3)
                f.loudness_db = round(rnd.uniform(-30, -6), 2)
                f.spectral_centroid = round(rnd.uniform(500, 4000), 1)
                f.spectral_rolloff = round(rnd.uniform(1500, 7000), 1)
                f.zero_crossing_rate = round(rnd.uniform(0.01, 0.15), 4)
                f.mood_vector = {
                    "energetic": f.energy,
                    "valence": f.valence,
                    "arousal": f.arousal,
                    "calm": 1.0 - f.energy,
                }
                f.mood_labels = rnd.sample(moods_pool, k=3)
                f.analyzed_at = datetime.utcnow()

                cluster_id = (hash(t.id) & 0x7) + 1
                existing = db.query(TrackCluster).filter_by(track_id=t.id, algorithm="kmeans-demo").first()
                if existing:
                    existing.cluster_id = cluster_id
                    existing.distance_to_center = rnd.uniform(0.1, 1.0)
                else:
                    db.add(
                        TrackCluster(
                            track_id=t.id,
                            algorithm="kmeans-demo",
                            cluster_id=cluster_id,
                            distance_to_center=rnd.uniform(0.1, 1.0),
                        )
                    )
                run.processed_items = i + 1
                time.sleep(0.05)
        _append_log(run_id, "info", f"Проанализировано треков: {len(tracks)}")
        _finish_run(run_id, "success")
        return {"status": "success", "tracks": len(tracks)}
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
    return {"status": "stub"}


def yandex_enrich(*args, **kwargs):
    return {"status": "stub"}


def collab_build(*args, **kwargs):
    return {"status": "stub"}