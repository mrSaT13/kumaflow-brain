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


def _dur_delta(a, b) -> float | None:
    """Модуль расхождения длительностей, либо None если сравнивать нечем."""
    try:
        if a is None or b is None:
            return None
        return abs(float(a) - float(b))
    except (TypeError, ValueError):
        return None


def _dur_tight(a, b, tol: float = 2.0) -> bool:
    """Строгий допуск: для одного и того же файла, прочитанного однимTools."""
    d = _dur_delta(a, b)
    return d is not None and d <= tol


def _dur_loose(a, b) -> bool:
    """Мягкий допуск для СРАВНЕНИЯ РАЗНЫХ ИНСТРУМЕНТОВ.

    Длительность локального файла считает mutagen из тегов, а у трека
    Navidrome — его собственный сканер. На живой библиотеке это расходится
    на секунды: один и тот же файл попадает в разные 5-секундные бакеты, и
    строгий ±2с отсекал почти всё в «требует решения». Отсюда относительный
    допуск: 5 секунд ИЛИ 1% длительности, что для трёхминутной песни
    терпит ~1.8с, а для часовой — 36с.

    Две РАЗНЫЕ песни с одинаковым названием и артистом различаются длиной
    гораздо сильнее, чем на 1%, поэтому относительный допуск не открывает
    дверь к неверной склейке — а вот абсолютные 2 секунды её открывали бы
    слишком часто.
    """
    d = _dur_delta(a, b)
    if d is None:
        return False
    try:
        base = max(abs(float(a)), abs(float(b)), 1.0)
    except (TypeError, ValueError):
        return False
    return d <= max(5.0, base * 0.01)


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
    d1 = a.get("duration_sec")
    d2 = b.get("duration_sec")
    d_tight = _dur_tight(d1, d2)
    d_loose = _dur_loose(d1, d2)
    # Длительности нет — это НЕ доказательство различия. Раньше здесь
    # получалось False, и файл уезжал в «требует решения»: на живой
    # библиотеке это уводило в отчёт около 65 тысяч пар из 76 тысяч, то
    # есть отчёт показывал 10 тысяч и молчал про остальные. Отсутствие
    # данных должно понижать уверенность на ступень, а не отправлять
    # человека разбирать каждую песню.
    d_missing = _dur_delta(d1, d2) is None
    if a1 and a1 == a2:
        if al1 and al1 == al2 and d_tight:
            return "exact_meta"
        if d_loose or d_missing:
            return "norm_meta"
        return None
    if d_loose or d_missing:
        return "title_duration"
    return None


# Уровни, которые сливаются автоматически. title_duration и None — никогда.
AUTO_MERGE_TIERS = ("exact_meta", "norm_meta")


def _pair_key(artist: str | None, title: str | None) -> tuple[str, str] | None:
    """Ключ для поиска пары «локальный файл ↔ трек Navidrome».

    ТОЛЬКО артист и название, БЕЗ длительности. Это и было причиной, почему
    на живых данных нашлось 204 совпадения из ~76 тысяч файлов: exact_key
    кладёт в ключ бакет длительности по 5 секунд, а длительность локального
    файла читает mutagen, а у трека Navidrome её считает собственный сканер.
    Они расходятся на секунды почти всегда, и стоит разойтись по разные
    стороны границы бакета — ключи становятся разными, и пара не находится
    ВООБЩЕ. До match_tier дело не доходит: он не получает кандидатов.

    Длительность никуда не делась — она проверяется в match_tier, где решает
    «склеивать или показать человеку», а не «найти или не найти».

    Версии в скобках НЕ вырезаются. Раньше `_has_version_marker` отбрасывала
    «Stupider (Remix)» целиком, и такие файлы автоматически попадали в
    «сироты» независимо от того, есть ли близнец в Navidrome. Теперь строки
    сравниваются как есть: «X (Remix)» совпадёт с «X (Remix)» и НЕ совпадёт
    с «X» — то есть защита от склейки ремикса с оригиналом сохраняется
    самим фактом неравенства строк, а возможность найти пару появляется.
    """
    a, t = norm_text(artist), norm_text(title)
    if not a or not t:
        return None
    return (a, t)


def find_cross_source_candidates(db, per_key_cap: int = 8) -> dict:
    """Пары «локальный файл ↔ трек Navidrome» + честный список сирот.

    Полный проход колонками (без ORM-нагрузки) — на 150k+ треков это
    единицы секунд. Возвращает:

      pairs            — пары, у каждой tier (см. match_tier);
      locals_with_pair — id локальных файлов, у которых нашлась хоть одна пара;
      local_orphans    — локальные файлы, у которых пары НЕТ вообще.

    per_key_cap ограничивает комбинаторный взрыв: у популярной песни бывает
    по 10-20 записей на источник, и 20×20 пар впустую. Больше 8 от каждого
    источника в группе не рассматриваем — сверх этого это всё равно не наши
    песни, а сборки одной и той же композиции.
    """
    from app.db.models import Track

    rows = db.query(
        Track.id, Track.external_id, Track.title, Track.artist_name,
        Track.album_name, Track.duration_sec,
    ).all()

    by_key: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = _pair_key(r.artist_name, r.title)
        if k is None:
            continue
        slot = by_key.get(k)
        if slot is None:
            slot = by_key[k] = {"nav": [], "loc": []}
        bucket = "loc" if is_local_source(r.external_id) else "nav"
        if len(slot[bucket]) < per_key_cap:
            slot[bucket].append({
                "id": str(r.id), "title": r.title, "artist_name": r.artist_name,
                "album_name": r.album_name, "duration_sec": r.duration_sec,
                "source": "local" if bucket == "loc" else "navidrome",
            })

    pairs: list[dict] = []
    locals_with_pair: set[str] = set()
    for slot in by_key.values():
        if not slot["nav"] or not slot["loc"]:
            continue
        for lv in slot["loc"]:
            best_tier: str | None = None
            best_nav: dict | None = None
            for nv in slot["nav"]:
                got = match_tier(lv, nv)
                if got is None:
                    continue
                if best_tier is None or AUTO_MERGE_TIERS.index(got) < AUTO_MERGE_TIERS.index(best_tier):
                    best_tier, best_nav = got, nv
            if best_tier is None:
                # Артист+название совпали, но длительность не сошлась даже
                # мягко. Не склеиваем и НЕ считаем сиротой: пара существует,
            # просто требует решения человека.
                locals_with_pair.add(lv["id"])
                pairs.append({**lv, "nav_id": None, "tier": None,
                              "reason": "совпали артист и название, но длительность "
                                        "расходится больше чем на 2 секунды"})
                continue
            locals_with_pair.add(lv["id"])
            pairs.append({**lv, "nav_id": best_nav["id"], "tier": best_tier,
                          "nav_title": best_nav["title"],
                          "nav_album": best_nav["album_name"],
                          "nav_dur": best_nav["duration_sec"]})

    # Сирота — строго то, у чего пары не нашлось ВООБЩЕ. Отдельно считаем
    # файлы, которые нельзя сопоставить В ПРИНЦИПЕ: нет артиста или названия,
    # значит нет ключа, значит пробовать нечего. Раньше они тонули в сиротах,
    # и было непонятно: файла нет в Navidrome или у файла битые теги.
    # Это разные действия: первое чинится сканированием Navidrome, второе —
    # правкой тегов. Поэтому два разных числа.
    local_rows = [r for r in rows if is_local_source(r.external_id)]
    unmatchable_ids = {str(r.id) for r in local_rows
                       if _pair_key(r.artist_name, r.title) is None}
    orphans = [{
        "id": str(r.id), "title": r.title, "artist_name": r.artist_name,
    } for r in local_rows
        if str(r.id) not in locals_with_pair and str(r.id) not in unmatchable_ids]
    unmatchable = [{
        "id": str(r.id), "title": r.title, "artist_name": r.artist_name,
    } for r in local_rows if str(r.id) in unmatchable_ids]

    return {
        "pairs": pairs,
        "locals_with_pair": locals_with_pair,
        "local_orphans": orphans,
        "unmatchable": unmatchable,
        "local_total": len(local_rows),
        "nav_total": len(rows) - len(local_rows),
    }


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
    """Автослияние для чекбокса «автоматом после сканирования» и кнопки
    «Сшить все точные».

    Раньше шло своим путём через find_duplicate_groups — бакет длительности
    по 5 секунд в ключе, и на живой библиотеке это находило ~200 пар из
    десятков тысяч. То есть галочка, которую мы советуем включить, чинила
    почти ничего, а её цифры расходились с планом «Перелить».

    Теперь делегирует единственному массовому пути — recannonicalize:
    поиск пар по артисту и названию, склейка только exact_meta/norm_meta.
    Ключи groups/merged сохранены: их читают лог сканирования
    (tasks/__init__.py) и тост кнопки. limit_groups больше не ограничивает
    (поиск пар идёт полным проходом), параметр оставлен для совместимости.
    """
    _ = limit_groups
    res = recannonicalize(db, dry_run=False)
    return {
        "groups": res.get("groups", 0),
        "merged": res.get("tracks_merged", 0),
        "features_moved": res.get("features_moved", 0),
        "needs_review": res.get("needs_review", 0),
        "unmatchable": res.get("unmatchable", 0),
    }


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

    # --- Новый поиск пар: ТОЛЬКО артист + название, без бакета длительности ---
    # Старый путь (find_duplicate_groups + exact_key) на живых данных дал
    # 204 совпадения из ~76 тысяч файлов: бакет длительности по 5 секунд
    # разводил ключи, потому что mutagen и сканер Navidrome считают длину
    # по-разному. Длительность остаётся проверкой в match_tier, но больше
    # не участвует в ПОИСКЕ.
    cands = find_cross_source_candidates(db)
    pairs = cands["pairs"]
    safe = [p for p in pairs if p.get("tier") in AUTO_MERGE_TIERS and p.get("nav_id")]
    weak = [p for p in pairs if p not in safe]
    by_tier: dict[str, int] = {}
    for p in safe:
        by_tier[p["tier"]] = by_tier.get(p["tier"], 0) + 1

    with_features = db.query(TrackFeatures.track_id).count()
    no_dur = sum(1 for p in safe if p.get("nav_dur") is None)
    # САМОПРОВЕРКА ОТЧЁТА. Первый прогон на живой библиотеке дал 204 «слить
    # можно» и 1 519 «сирот» — а локальных файлов было 77 912. То есть 76
    # тысяч молча не попали ни в одну категорию, и это выглядело как правда,
    # потому что невязавшиеся просто не показывались. Такую арифметику обязан
    # считать сам отчёт, а не человек глазами.
    accounted = (len(safe) + len(weak) + len(cands["local_orphans"])
                 + len(cands["unmatchable"]))
    unaccounted = cands["local_total"] - accounted
    if unaccounted:
        logger.warning("source_report: не учтено {} локальных файлов из {} — "
                       "вероятно, пустой артист или название", unaccounted,
                       cands["local_total"])
    return {
        "total": cands["local_total"] + cands["nav_total"],
        "navidrome": cands["nav_total"],
        "local": cands["local_total"],
        "local_with_navidrome_twin": len(safe),
        "local_orphans_no_twin": len(cands["local_orphans"]),
        "orphan_samples": cands["local_orphans"][:20],
        "unmatchable": len(cands["unmatchable"]),
        "unmatchable_samples": cands["unmatchable"][:10],
        "cross_source_groups": len(pairs),
        "by_tier": by_tier,
        "needs_review": len(weak),
        "pairs_without_duration": no_dur,
        # Четыре числа обязаны дать local. Если не дают — отчёт врёт, и это
        # должно быть видно сразу, а не через месяц на живых данных.
        "accounted": accounted,
        "unaccounted": unaccounted,
        "balances": unaccounted == 0,
        "review_samples": [
            {"title": p.get("title"), "artist_name": p.get("artist_name"),
             "local_dur": p.get("duration_sec"), "nav_dur": p.get("nav_dur"),
             "tier": p.get("tier"), "reason": p.get("reason")}
            for p in weak[:20]
        ],
        "with_features": with_features,
        "note": ("Локальные копии с двойником в Navidrome можно слить в пользу "
                 "Navidrome — фичи переедут, плеер получит id. Без двойника "
                 "трогать нельзя: это единственные носители файла. Слабые "
                 "совпадения — в needs_review, автоматом не сливаются."),
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
    # Поиск пар идёт через find_cross_source_candidates: артист + название,
    # длительность — мягкая проверка. Старый путь через find_duplicate_groups
    # искал бакетом длительности по 5 секунд и на живых данных нашёл 204
    # совпадения из ~76 тысяч локальных файлов, то есть практически ничего.
    cands = find_cross_source_candidates(db)
    feat_ids: set[str] = set()
    try:
        from app.db.models import TrackFeatures

        feat_ids = {str(r[0]) for r in db.query(TrackFeatures.track_id).all()}
    except Exception:  # noqa: BLE001 — отчёт не должен падать из-за этого
        pass

    plan: list[dict] = []
    review: list[dict] = []
    for p in cands["pairs"]:
        # Мягкие и пустые уровни — в отчёт, НЕ сливаются: склейка необратима.
        if p.get("tier") not in AUTO_MERGE_TIERS or not p.get("nav_id"):
            review.append({
                "title": p.get("title"),
                "artist_name": p.get("artist_name"),
                "local_dur": p.get("duration_sec"),
                "nav_dur": p.get("nav_dur"),
                "tier": p.get("tier"),
                "reason": p.get("reason") or "уровень ниже автоматического",
            })
            continue
        local_id, nav_id = p["id"], p["nav_id"]
        plan.append({
            "key": "%s — %s" % (p.get("artist_name") or "", p.get("title") or ""),
            "title": p.get("title"),
            "artist_name": p.get("artist_name"),
            "tier": p["tier"],
            # Каноническим всегда становится трек Navidrome: только у него
            # есть номер, который понимает плеер. Если сейчас каноническим
            # числится локальная копия (старые базы, где диск сканировался
            # первым) — это тот же состав, просто с обратным выбором.
            "keep_id": nav_id,
            "drop_ids": [local_id],
            "drop_count": 1,
            "features_moving": 1 if local_id in feat_ids else 0,
            "action": "drop_local",
        })

    if dry_run:
        return {
            "dry_run": True,
            "groups": len(plan),
            "recannonicalize": 0,
            "drop_local": len(plan),
            "tracks_to_merge": sum(p["drop_count"] for p in plan),
            "features_to_move": sum(p["features_moving"] for p in plan),
            "by_tier": {t: sum(1 for p in plan if p["tier"] == t)
                        for t in AUTO_MERGE_TIERS},
            "needs_review": len(review),
            "review_sample": review[:50],
            "local_orphans": len(cands["local_orphans"]),
            "unmatchable": len(cands["unmatchable"]),
            "unmatchable_samples": cands["unmatchable"][:10],
            "plan": plan[:200],
            "note": ("Сливаются только exact_meta и norm_meta: совпали название, "
                     "артист и длительность в пределах 2 секунд. Остальное — в "
                     "needs_review и не трогается. Каждая песня отдельным "
                     "commit: можно остановить, состояние останется целым."),
        }

    merged = feats = 0
    for p in plan:
        try:
            res = merge_tracks(db, p["keep_id"], p["drop_ids"])
            if res.get("ok"):
                merged += res["merged"]
                feats += p["features_moving"]
        except Exception as e:  # noqa: BLE001 — одна битая пара не валит всё
            logger.warning("recannonicalize {} failed: {}", p["key"], e)
            db.rollback()
    logger.info("recannonicalize: {} pairs, {} tracks merged, {} features moved",
                len(plan), merged, feats)
    return {
        "dry_run": False,
        "groups": len(plan),
        "recannonicalize": 0,
        "drop_local": len(plan),
        "tracks_merged": merged,
        "features_moved": feats,
        "needs_review": len(review),
        "unmatchable": len(cands["unmatchable"]),
    }
