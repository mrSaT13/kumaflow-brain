"""Дедупликация треков: поиск и аккуратное сшивание дублей.

Точный дубль (кандидат на авто-слияние): одинаковые нормализованные
артист + название + длительность (±2с). Несколько проверок перед сшиванием:
  1) группы только из >=2 треков с одинаковым ключом;
  2) live/remix/edit/instrumental-маркеры в скобках — разные версии, не трогаем;
  3) сшиваем в канонический (больше прослушиваний/starred/старше), FK переносим.
Похожие (тот же артист+базовое название, но длительность/версия различаются) —
только на ручное решение, автоматом никогда.
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict

from app.core.logging import get_logger
from app.db.models import (
    Favorite,
    Lyrics,
    LocalFileLink,
    PlayEvent,
    PlayHistory,
    PlaylistTrack,
    RecommendationFeedback,
    Track,
    TrackCluster,
    TrackDislike,
    TrackEmbedding,
    TrackFeatures,
    TrackMetadataEnrich,
    TrackStat,
    TrackTimeStat,
)

logger = get_logger("dedup")

# маркеры версий — такие треки дублями НЕ считаем
_VERSION_RE = re.compile(
    r"\b(live|remix|remaster|edit|extended|instrumental|acoustic|demo|version|mix|cover|karaoke|sped up|slowed|rework|remake)\b",
    re.IGNORECASE,
)

_DURATION_TOLERANCE = 2  # секунды

# Префиксы external_id, которые НЕ играют в плеере.
# Единый источник — services/playable.py (там же SQL-фильтр и has_navidrome).
from app.services.playable import LOCAL_PREFIXES as _LOCAL_PREFIXES
from app.services.playable import is_local_source


def _source_rank(external_id: str | None) -> int:
    """Приоритет источника при выборе канонического трека.

    Navidrome важнее локального файла, и это не вопрос вкуса, а вопрос того,
    что происходит с результатом. `services/playlist_push.py:42` выкидывает из
    выгрузки всё с префиксом disk:/demo-, потому что плеер таких треков не
    знает. Если каноническим станет локальный трек, то трек уедет в Navidrome
    уже без физического id — и из плейлиста выпадет навсегда, а не на один
    раз. Раньше приоритета источника не было вовсе: при равных play_count и
    starred (а у дисковых они всегда 0/False, см. _scan_disk_music) выбор
    решал created_at, а `library_scan` сканирует диск ПЕРВЫМ
    (`workers/tasks/__init__.py:422`) — то есть каноническим становился именно
    локальный дубль. Отсюда «создал плейлист на 30, плеер получил 23».
    """
    return 1 if is_local_source(external_id) else 0


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[\u200b-\u200f\ufeff]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _has_version_marker(title: str) -> bool:
    m = re.search(r"[\(\[]([^\)\]]+)[\)\]]", title or "")
    return bool(m and _VERSION_RE.search(m.group(1)))


def exact_key(artist: str | None, title: str | None, duration: int | None) -> tuple | None:
    """Ключ точного дубля. None — не кандидат (нет данных / версия)."""
    a, t = norm_text(artist), norm_text(title)
    if not a or not t:
        return None
    if _has_version_marker(title or ""):
        return None
    if duration is None:
        return ("notime", a, t)
    return ("dur", a, t, int(duration // 5))  # бакет 5с, точная проверка — по ±2с


def _path_exists(path: str | None) -> bool:
    """Файл реально лежит по этому пути на машине, где работает воркер.

    Нужна, чтобы при слиянии перенести ПРАВИЛЬНЫЙ путь. У строки Navidrome
    путь от имени чужой машины (Subsonic отдаёт путь сервера, а не воркера),
    и воркер такого пути не видел. Критерий «какой путь непустой» был бы
    неверным: импорт Navidrome заполняет path всегда, и выигрывал бы именно
    чужой путь.

    Одна проверка на трек и только в момент слияния — не горячий путь.
    """
    if not path or not str(path).strip():
        return False
    try:
        return os.path.isfile(str(path))
    except (OSError, ValueError):
        return False


def _dur_ok(a, b, tol: float = 2.0) -> bool:
    """Длительности сходятся с точностью до tol секунд."""
    try:
        if a is None or b is None:
            return False
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def match_tier(a: dict, b: dict) -> str | None:
    """Насколько уверенно две строки — одна и та же песня. None = не уверены.

    Идея взята из AudioMuse-AI (`tasks/mediaserver/registry.py`,
    `match_tier_rank_sql`): уверенность записывается в данные, а не
    подразумевается. Там же у них слой «название + артист» есть, но
    ВЫКЛЮЧЕН по умолчанию — и это правильно: два разных трека с одинаковым
    названием склеиваются в один навсегда, и это необратимо.

    Здесь та же логика, но под нашу пару «файл с диска ↔ трек Navidrome»:

      exact_meta     — название + артист + альбом совпали И длительность ±2с.
                       Это наш основной случай: теги одного файла против
                       метаданных того же файла из Navidrome.
      norm_meta      — название + артист совпали, длительность ±2с, альбом
                       не учитываем (Navidrome мог назвать сборку иначе).
      title_duration — совпало только название и длительность. НЕ
                       склеиваем автоматически: артиста нет, а значит
                       «Yellow» двух разных групп склеился бы в один.
      None           — не уверены, в отчёт как «требует решения».

    Чего тут нет и появится позже: уровня `fingerprint` (совпадение по
    звуковому отпечатку). Он сильнее всех, но требует, чтобы ОБА трека были
    проанализированы. Пока покрытие анализа неполное, его рано применять
    автоматически — сначала он должен просто показывать кандидатов.
    """
    # Ключи в данных find_duplicate_groups — artist_name/album_name, но
    # match_tier берётся и из других вызывающих мест, где может быть
    # artist/album. Раньше читался только короткий вариант, и артист всегда
    # выходил пустым: ЛЮБАЯ пара падала в title_duration, то есть в review,
    # и автоматическое слияние не работало бы вообще. Тест это показал.
    def _f(row: dict, *keys) -> str:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip():
                return str(v)
        return ""

    t1, a1 = norm_text(a.get("title")), norm_text(_f(a, "artist_name", "artist"))
    al1 = norm_text(_f(a, "album_name", "album"))
    t2, a2 = norm_text(b.get("title")), norm_text(_f(b, "artist_name", "artist"))
    al2 = norm_text(_f(b, "album_name", "album"))
    if not t1 or not t2 or t1 != t2:
        return None
    d_ok = _dur_ok(a.get("duration_sec"), b.get("duration_sec"))
    if a1 and a1 == a2:
        if al1 and al1 == al2 and d_ok:
            return "exact_meta"
        if d_ok:
            return "norm_meta"
        return None
    if d_ok:
        return "title_duration"
    return None


# Уровни, которые сливаются автоматически. title_duration и None — никогда.
AUTO_MERGE_TIERS = ("exact_meta", "norm_meta")


def find_duplicate_groups(db, limit_groups: int = 500) -> list[dict]:
    """Группы точных дублей. Лёгкий проход колонками (без ORM-нагрузки)."""
    rows = db.query(
        Track.id, Track.title, Track.artist_name, Track.album_name,
        Track.genre, Track.duration_sec, Track.play_count, Track.starred,
        Track.external_id, Track.created_at,
    ).all()
    buckets: dict[tuple, list] = defaultdict(list)
    for r in rows:
        k = exact_key(r.artist_name, r.title, r.duration_sec)
        if k:
            buckets[k].append(r)
    groups = []
    for k, members in buckets.items():
        if len(members) < 2:
            continue
        # точная проверка длительности внутри бакета (для "dur")
        if k[0] == "dur":
            durs = [m.duration_sec for m in members if m.duration_sec is not None]
            if durs and (max(durs) - min(durs) > _DURATION_TOLERANCE):
                continue
        members = sorted(members, key=lambda m: (
            # Сначала источник: Navidrome должен выигрывать всегда, независимо
            # от статистики. Иначе каноническим станет disk:-трек, который
            # плеер не воспроизведёт (см. _source_rank).
            _source_rank(m.external_id),
            -(m.play_count or 0), not bool(m.starred), str(m.created_at or "")))
        groups.append({
            "key": f"{k[1]} — {k[2]}",
            "keep_id": str(members[0].id),
            "tracks": [
                {"id": str(m.id), "title": m.title, "artist_name": m.artist_name,
                 "album_name": m.album_name, "genre": m.genre,
                 "duration_sec": m.duration_sec, "play_count": m.play_count or 0,
                 "starred": bool(m.starred),
                 "source": ("disk" if (m.external_id or "").startswith("disk:")
                            else "demo" if (m.external_id or "").startswith("demo-")
                            else "navidrome")}
                for m in members
            ],
        })
        if len(groups) >= limit_groups:
            break
    groups.sort(key=lambda g: len(g["tracks"]), reverse=True)
    return groups


def _move_fk(db, model, col: str, drop_ids: list[str], keep_id: str,
             extra_skip_check=None) -> int:
    """Перенести ссылки drop→keep. Возвращает число перенесённых строк."""
    moved = 0
    q = db.query(model).filter(getattr(model, col).in_(drop_ids))
    for row in q.all():
        if extra_skip_check is not None and extra_skip_check(row):
            db.delete(row)
            continue
        setattr(row, col, keep_id)
        moved += 1
    return moved


def merge_tracks(db, keep_id: str, drop_ids: list[str]) -> dict:
    """Сшить дубли в канонический: статистика суммируется, ссылки переносятся.

    Защита от дублей PK (Favorite/Dislike/Lyrics/...): если у keep уже есть
    такая же строка — строка дубля удаляется. Счётчики TrackStat/TrackTimeStat
    суммируются (user[,hour]), показы RecommendationFeedback переносятся.
    """
    drop_ids = [d for d in drop_ids if d != keep_id]
    if not drop_ids:
        return {"ok": False, "error": "nothing to merge"}
    keep = db.get(Track, keep_id)
    drops = [db.get(Track, d) for d in drop_ids]
    drops = [d for d in drops if d is not None]
    if not keep or not drops:
        return {"ok": False, "error": "track not found"}

    # --- Защита от необратимой потери -------------------------------------
    # external_id — это то, чем Navidrome-строка связана с плеером, и
    # merge_tracks его НЕ переносит: строка просто удаляется. Значит если
    # канонической окажется локальная копия, номер Navidrome исчезнет из базы
    # навсегда, трек станет неиграбельным, и восстановить его можно будет
    # только повторным импортом библиотеки. Раньше выбор канонического
    # решался сортировкой (см. _source_rank), но на живых базах с уже
    # слитыми неправильно строками и при автослиянии по чекбоксу повтор —
    # это тихая и необратимая потеря. Поэтому проверяем здесь, независимо от
    # того, кто вызвал слияние.
    if is_local_source(keep.external_id):
        _adopt = next((d for d in drops if not is_local_source(d.external_id)), None)
        if _adopt is not None:
            # Кто-то отдал слияние «не тем». Не спорим: меняем канонического
            # местами. Все ссылки переедут на нового keep, а локальная строка
            # станет дублем — ровно то, что нужно.
            drops.append(keep)
            keep = _adopt
            logger.info("merge: каноническим сделан трек Navidrome {} вместо "
                        "локальной копии", str(keep.id)[:8])
        else:
            # Локальных и играбельных в группе нет — сливать нечего опасного.
            pass

    # Страховка второго уровня: если канонической всё же осталась строка без
    # номера Navidrome, а среди дублей номер есть —adoptим его. Тогда трек
    # останется играбельным независимо от того, в каком порядке пришли id.
    if is_local_source(keep.external_id) or not str(keep.external_id or "").strip():
        _ext = next((str(d.external_id or "") for d in drops
                     if not is_local_source(d.external_id)), None)
        if _ext:
            logger.info("merge {}: подставляем external_id Navidrome из дубля",
                        str(keep.id)[:8])
            keep.external_id = _ext[:128]

    total_plays = keep.play_count or 0
    # Помечаем файлы с диска, чьи строки сейчас исчезнут, как «уже учтённые».
    # Без этого стройка не держится: external_id локального трека — хэш пути, и
    # сканер создаёт строку заново, если её нет в tracks (tasks/__init__.py,
    # _scan_disk_music). Слил дубли — крон ночью вернул их, и через неделю всё
    # выглядело бы так, будто чинить бесполезно. Здесь факт живёт на строке
    # Navidrome и переживает удаление локальной копии.
    _linked = 0
    try:
        for d in drops:
            _ph = str(d.external_id or "")
            if not is_local_source(_ph):
                continue
            try:
                db.merge(LocalFileLink(path_hash=_ph, track_id=keep_id))
                _linked += 1
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001 — слияние важнее пометки
        logger.warning("merge {}: не удалось пометить файлы учтёнными: {}",
                       keep_id[:8], e)
    for d in drops:
        total_plays += d.play_count or 0
        if d.starred:
            keep.starred = True
        if d.rating and (not keep.rating or d.rating > keep.rating):
            keep.rating = d.rating
        if d.last_played_at and (not keep.last_played_at or d.last_played_at > keep.last_played_at):
            keep.last_played_at = d.last_played_at
        # обложка: добираем если у keep нет
        if not keep.cover_art_id and d.cover_art_id:
            keep.cover_art_id = d.cover_art_id
        # Путь к файлу. У Navidrome-строки он ЧУЖОЙ: Subsonic отдаёт путь от
        # имени своей машины, воркер такого пути не видел. Локальная строка —
        # единственная, кто знает реальный путь на смонтированной папке, и без
        # переноса мозг его терял. Нужно для пересчёта анализа (force) и для
        # CLAP-эмбеддинга по аудио; при отсутствии файла их спасёт скачивание
        # из Navidrome, но это медленно.
        #
        # Критерий — «какой путь существует», а не «какой непустой»: правило
        # «если у keep пусто» почти не срабатывало, потому что импорт
        # Navidrome заполняет path всегда, и выигрывал бы как раз чужой путь.
        # Проверка одноразовая на трек при слиянии, не в горячем пути.
        if d.path and not _path_exists(keep.path):
            if _path_exists(d.path):
                keep.path = d.path
            elif not str(keep.path or "").strip():
                keep.path = d.path
        if not keep.genre and d.genre:
            keep.genre = d.genre
        if not keep.year and d.year:
            keep.year = d.year
    keep.play_count = total_plays

    moved: dict[str, int] = {}

    def _pk_skip(model, extra_cols: tuple):
        def check(row):
            filt = [getattr(model, c) == getattr(row, c) for c in extra_cols]
            filt.append(getattr(model, [c for c in ("track_id",) if hasattr(model, c)][0]) == keep_id)
            return db.query(model).filter(*filt).first() is not None
        return check

    # Favorite(user,track), TrackDislike(user,track): скип если у keep уже есть
    for model in (Favorite, TrackDislike):
        n = 0
        for row in db.query(model).filter(model.track_id.in_(drop_ids)).all():
            if db.query(model).filter_by(user_id=row.user_id, track_id=keep_id).first():
                db.delete(row)
            else:
                row.track_id = keep_id
                n += 1
        moved[model.__tablename__] = n
    # PlayHistory / PlayEvent: просто переносим (PK по id)
    for model in (PlayHistory, PlayEvent):
        moved[model.__tablename__] = _move_fk(db, model, "track_id", drop_ids, keep_id)
    # PlaylistTrack(playlist,track): скип-дубли позиций
    n = 0
    for row in db.query(PlaylistTrack).filter(PlaylistTrack.track_id.in_(drop_ids)).all():
        if db.query(PlaylistTrack).filter_by(playlist_id=row.playlist_id, track_id=keep_id).first():
            db.delete(row)
        else:
            row.track_id = keep_id
            n += 1
    moved["playlist_tracks"] = n
    # Lyrics / Enrich / Features / Embedding / Cluster (PK track_id[+...]): перенос или удаление
    for model in (Lyrics, TrackMetadataEnrich, TrackFeatures, TrackEmbedding, TrackCluster):
        n = 0
        for row in db.query(model).filter(model.track_id.in_(drop_ids)).all():
            exists_q = db.query(model).filter(model.track_id == keep_id)
            if hasattr(model, "provider"):
                exists_q = exists_q.filter(model.provider == row.provider)
            if hasattr(model, "source"):
                exists_q = exists_q.filter(model.source == row.source)
            if hasattr(model, "model"):
                exists_q = exists_q.filter(model.model == row.model)
            if hasattr(model, "algorithm"):
                exists_q = exists_q.filter(model.algorithm == row.algorithm)
            if exists_q.first() is not None:
                db.delete(row)
            else:
                row.track_id = keep_id
                n += 1
        moved[model.__tablename__] = n
    # TrackStat / TrackTimeStat (PK user[,hour],track): счётчики СУММИРУЕМ,
    # иначе статистика волны («часто в этот час», скоры вкуса) теряется вместе
    # с удалённым дублем. last_played — максимум из двух.
    for model, _sum_cols in (
        (TrackStat, ("plays", "skips", "early_skips", "replays",
                     "seek_backs", "abandons", "completes", "mobile_score")),
        (TrackTimeStat, ("plays", "likes", "skips")),
    ):
        n = 0
        for row in db.query(model).filter(model.track_id.in_(drop_ids)).all():
            filt = [model.user_id == row.user_id, model.track_id == keep_id]
            if hasattr(model, "hour"):
                filt.append(model.hour == row.hour)
            keep_row = db.query(model).filter(*filt).first()
            if keep_row is None:
                row.track_id = keep_id
            else:
                for c in _sum_cols:
                    setattr(keep_row, c, (getattr(keep_row, c) or 0) + (getattr(row, c) or 0))
                if getattr(row, "last_played", None) and (
                        not getattr(keep_row, "last_played", None)
                        or row.last_played > keep_row.last_played):
                    keep_row.last_played = row.last_played
                db.delete(row)
            n += 1
        moved[model.__tablename__] = n
    # RecommendationFeedback (PK id): просто переносим показы на keep —
    # иначе метрики волны повиснут на удалённом track_id.
    moved[RecommendationFeedback.__tablename__] = _move_fk(
        db, RecommendationFeedback, "track_id", drop_ids, keep_id)

    for d in drops:
        db.delete(d)
    db.commit()
    logger.info("merged {} dupes into {}: {}, files_marked={}", len(drops),
                keep_id[:8], moved, _linked)
    return {"ok": True, "keep_id": keep_id, "merged": len(drops), "moved": moved,
            "files_marked": _linked}


def auto_merge_exact(db, limit_groups: int = 2000) -> dict:
    """Автослияние только точных групп (для чекбокса/крона)."""
    groups = find_duplicate_groups(db, limit_groups=limit_groups)
    merged = 0
    for g in groups:
        try:
            res = merge_tracks(db, g["keep_id"], [t["id"] for t in g["tracks"] if t["id"] != g["keep_id"]])
            if res.get("ok"):
                merged += res["merged"]
        except Exception as e:  # noqa: BLE001 — одна битая группа не валит всё
            logger.warning("auto-merge group failed: {}", e)
            db.rollback()
    return {"groups": len(groups), "merged": merged}


def source_report(db) -> dict:
    """Разбор библиотеки по источнику. Только чтение — ничего не меняет.

    Нужен, чтобы перед любым массовым слиянием понимать масштаб: сколько
    треков реально играет в плеере (Navidrome), сколько существует только как
    локальный файл, и сколько локальных копий можно безопасно убрать, потому
    что у них есть Navidrome-двойник.
    """
    from app.db.models import TrackFeatures, Track

    local_prefixes = _LOCAL_PREFIXES
    rows = db.query(Track.id, Track.external_id, Track.duration_sec,
                    Track.title, Track.artist_name, Track.play_count, Track.starred).all()
    nav = loc = 0
    orphan_samples: list[dict] = []
    for r in rows:
        if is_local_source(r.external_id):
            loc += 1
        else:
            nav += 1

    # Группы, где смешаны оба источника: локальная копия + её Navidrome-двойник.
    # Именно их безопасно сливать (в пользу Navidrome), потому что плеер знает
    # только Navidrome-часть, а фичи/фичерпринты переезжают на неё целиком.
    groups = find_duplicate_groups(db, limit_groups=10000)
    cross: list[dict] = []
    removable = 0
    for g in groups:
        srcs = [t["source"] for t in g["tracks"]]
        has_nav = "navidrome" in srcs
        has_loc = "disk" in srcs or "demo" in srcs
        if not (has_nav and has_loc):
            continue
        keep = next((t for t in g["tracks"] if t["id"] == g["keep_id"]), None)
        drops_local = [t for t in g["tracks"] if t["id"] != g["keep_id"]
                       and t["source"] in ("disk", "demo")]
        removable += len(drops_local)
        if keep is not None and keep["source"] == "navidrome":
            cross.append({
                "key": g["key"],
                "keep_id": g["keep_id"],
                "drop_ids": [t["id"] for t in drops_local],
                "drop_count": len(drops_local),
            })
        else:
            # Каноническим сейчас локальный трек (уже слитый раньше или
            # созданный диском раньше Navidrome). Для отчёта — план починки.
            cross.append({
                "key": g["key"],
                "keep_id": g["keep_id"],
                "keep_source": keep["source"] if keep else None,
                "drop_ids": [t["id"] for t in drops_local],
                "drop_count": len(drops_local),
                "needs_recannonicalize": True,
            })

    # Локальные треки БЕЗ двойника в Navidrome — их нельзя сливать: это
    # единственные носители файла (Navidrome про них не знает). Их надо
    # оставить, иначе песня исчезнет из мозга вообще.
    nav_keys = set()
    for r in rows:
        if not is_local_source(r.external_id):
            k = exact_key(r.artist_name, r.title, r.duration_sec)
            if k:
                nav_keys.add(k)
    orphans = 0
    for r in rows:
        if is_local_source(r.external_id):
            k = exact_key(r.artist_name, r.title, r.duration_sec)
            if k not in nav_keys:
                orphans += 1
                if len(orphan_samples) < 20:
                    orphan_samples.append({"id": str(r.id), "title": r.title,
                                           "artist_name": r.artist_name})

    with_features = db.query(TrackFeatures.track_id).count()
    return {
        "total": len(rows),
        "navidrome": nav,
        "local": loc,
        "local_with_navidrome_twin": removable,
        "local_orphans_no_twin": orphans,
        "orphan_samples": orphan_samples,
        "cross_source_groups": len(cross),
        "needs_recannonicalize": sum(1 for c in cross if c.get("needs_recannonicalize")),
        "with_features": with_features,
        "note": ("Локальные копии с двойником в Navidrome можно слить в пользу "
                 "Navidrome — фичи переедут, плеер получит id. Локальные без "
                 "двойника трогать нельзя: это единственные носители файла."),
    }


def recannonicalize(db, dry_run: bool = True, limit_groups: int = 10000) -> dict:
    """Пересобрать канонические треки в пользу Navidrome (repair, не только plan).

    Что делает. Находит группы, где смешаны оба источника, и если каноническим
    сейчас локальный (disk:/demo-) трек — переливает всё в Navidrome-член
    группы: TrackFeatures, эмбеддинги, лайки, история, плейлисты.

    Зачем. `library_scan` historically сканировал диск ПЕРВЫМ, поэтому у
    никогда неигранных треков канонической строкой становилась локальная копия
    (у disk-строк play_count всегда 0 и starred False, так что статистика не
    могла перевесить). Плейлист из таких треков при выгрузке в Navidrome терял
    их: `playlist_push` пропускает disk:-префиксы, потому что плеер такого id
    не знает. Слить их «правильно» — значит сделать Navidrome-строку
    канонической, и тогда трек уедет в плеер, а его аудио-фичи (единственная
    причина вообще сканировать диск) переедут на неё.

    dry_run=True (по умолчанию) — только план, ничего не меняет.
    """
    groups = find_duplicate_groups(db, limit_groups=limit_groups)
    # Кто из локальных копий уже посчитан: именно их фичи поедут на
    # Navidrome-строку. Считаем заранее, чтобы в отчёте было видно, сколько
    # работы переедет, а не сколько строк вообще исчезнет.
    feat_ids: set[str] = set()
    try:
        from app.db.models import TrackFeatures

        feat_ids = {str(r[0]) for r in db.query(TrackFeatures.track_id).all()}
    except Exception:  # noqa: BLE001 — отчёт не должен падать из-за этого
        pass

    plan: list[dict] = []
    review: list[dict] = []
    for g in groups:
        members = g["tracks"]
        nav_members = [t for t in members if t["source"] == "navidrome"]
        if not nav_members:
            continue  # нет Navidrome-двойника — сливать не с кем, файл пропадёт
        local_members = [t for t in members if t["source"] in ("disk", "demo")]
        if not local_members:
            continue
        # Уверенность: сверяем КАЖДУЮ локальную копию с КАЖДЫМ треком
        # Navidrome в группе и берём лучший уровень. Если ни одна пара не
        # убедила — группа уходит в «требует решения» и НЕ сливается.
        best_tier: str | None = None
        best_pair: tuple[dict, dict] | None = None
        for lv in sorted((t for t in local_members), key=lambda t: len(t["title"] or ""), reverse=True):
            for nv in nav_members:
                got = match_tier(lv, nv)
                if got is None:
                    continue
                if best_tier is None or AUTO_MERGE_TIERS.index(got) < AUTO_MERGE_TIERS.index(best_tier):
                    best_tier, best_pair = got, (lv, nv)
            if best_tier == "exact_meta":
                break
        local_row, nav_row = best_pair if best_pair else (local_members[0], nav_members[0])
        entry = {
            "key": g["key"],
            "title": nav_row.get("title") or local_row.get("title"),
            "artist_name": nav_row.get("artist_name") or local_row.get("artist_name"),
            "local_title": local_row.get("title"),
            "local_artist": local_row.get("artist_name"),
            "local_album": local_row.get("album_name"),
            "nav_title": nav_row.get("title"),
            "nav_album": nav_row.get("album_name"),
            "local_dur": local_row.get("duration_sec"),
            "nav_dur": nav_row.get("duration_sec"),
            "tier": best_tier,
        }
        if best_tier not in AUTO_MERGE_TIERS:
            # Сомнительно. Показываем, но НЕ трогаем: склейка необратима.
            entry["reason"] = ("только название сошлось, артиста нет — такие "
                               "не склеиваем автоматически")
            review.append(entry)
            continue
        keep = next((t for t in members if t["id"] == g["keep_id"]), None)
        if keep is not None and keep["source"] == "navidrome":
            # Уже правильно: Navidrome канонический, локальные — лишние.
            entry.update({
                "keep_id": keep["id"],
                "drop_ids": [t["id"] for t in local_members],
                "drop_count": len(local_members),
                "features_moving": sum(1 for t in local_members if t["id"] in feat_ids),
                "action": "drop_local",
            })
        else:
            # Канонический — локальный. Чиним: Navidrome-член становится keep,
            # локальный уходит в drop вместе с остальными дублями.
            drops = [t for t in members if t["id"] != nav_row["id"]]
            entry.update({
                "keep_id": nav_row["id"],
                "old_keep_id": g["keep_id"],
                "drop_ids": [t["id"] for t in drops],
                "drop_count": len(drops),
                "features_moving": sum(1 for t in drops if t["id"] in feat_ids),
                "action": "recannonicalize",
            })
        plan.append(entry)

    if dry_run:
        return {
            "dry_run": True,
            "groups": len(plan),
            "recannonicalize": sum(1 for p in plan if p["action"] == "recannonicalize"),
            "drop_local": sum(1 for p in plan if p["action"] == "drop_local"),
            "tracks_to_merge": sum(p["drop_count"] for p in plan),
            "features_to_move": sum(p["features_moving"] for p in plan),
            "by_tier": {t: sum(1 for p in plan if p["tier"] == t)
                        for t in ("exact_meta", "norm_meta")},
            "needs_review": len(review),
            "review_sample": review[:50],
            "plan": plan[:200],
            "note": ("Сливаются только exact_meta и norm_meta — там совпали "
                     "название, артист и длительность. Совпадения только по "
                     "названию — в needs_review и не трогаются. Крупные слияния "
                     "идут по одной песне с отдельным commit: можно "
                     "остановить, состояние останется целым."),
        }

    fixed = merged = feats = 0
    for p in plan:
        try:
            res = merge_tracks(db, p["keep_id"], p["drop_ids"])
            if res.get("ok"):
                merged += res["merged"]
                feats += p["features_moving"]
                if p["action"] == "recannonicalize":
                    fixed += 1
        except Exception as e:  # noqa: BLE001 — одна битая группа не валит всё
            logger.warning("recannonicalize group failed: {}", e)
            db.rollback()
    return {
        "dry_run": False,
        "groups": len(plan),
        "recannonicalize": fixed,
        "drop_local": sum(1 for p in plan if p["action"] == "drop_local"),
        "tracks_merged": merged,
        "features_moved": feats,
        "needs_review": len(review),
    }
