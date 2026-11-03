"""«Спасатель запроса»: превращает кривую распознанную строку в кандидатов.

Сценарий: голос (ASR) выдал «Эминем» кириллицей, а в каталоге артист —
«Eminem» латиницей. Обычный поиск ищет буквально и возвращает пустоту.

Как ищем (по убыванию уверенности):
  1) точное совпадение;
  2) кириллица → латиница («Эминем» → «Eminem»);
  3) «звуковой» ключ (гласные убраны: «eminem»/«еминем» → «mnm»);
  4) похожесть написания (Левенштейн) — опечатки и оговорения голоса;
  5) совпадение по отдельному слову запроса («включи eminem»);
  6) выученные алиасы (query_aliases) — то, что мозг уже понял сам.

Источники кандидатов: библиотека (artists + tracks) и, по желанию, внешний
список (Яндекс.Музыка) — он приходит снаружи как extra_names.

Модуль ничего не знает про колонки и конкретных клиентов: это просто
«исправь строку и дай варианты».
"""
from __future__ import annotations

import bisect
import time
from typing import Any, Iterable

from app.core.logging import get_logger
from app.services.text_tools import (
    levenshtein,
    norm_key,
    phonetic_key,
    similarity,
    translit_ru_en,
)

logger = get_logger("query_resolver")

# Пороги уверенности.
SCORE_EXACT = 1.0
SCORE_TRANSLIT = 0.95
SCORE_PHONETIC = 0.90
SCORE_WORD = 0.80
GOOD_ENOUGH = 0.85
# Насколько похожее написание слова ещё считаем «тем же самым» при поиске
# выученного алиаса: голос пишет одно слово по-разному.
ALIAS_FUZZY_MIN = 0.88

# Кэш имён библиотеки: пересобирать на каждый запрос — лишняя работа,
# библиотека меняется редко. На больших коллекциях (80к+ треков) полный
# перебор со скорингом каждой строки занял ~10 с и ломал таймаут станции.
#
# Поэтому поиск работает как у больших сервисов (Яндекс/ВК/Spotify):
#  * инвертированные бакеты — по нормализованному ключу, транслиту,
#    «звуковому» ключу (целому и по словам) — кандидаты ГЕНЕРИРУЮТСЯ, а не
#    перебираются;
#  * дорогая метрика (Левенштейн) считается только на этих кандидатах;
#  * фон-фолбэк похожести ограничен бюджетом времени;
#  * перестройка индекса идёт в фоне (stale-while-revalidate), голосовой
#    запрос никогда не ждёт пересбор.
_LIB_CACHE_TTL = 120.0

# Максимум кандидатов из одного бакета — защита от вырожденных запросов
# («a») на гигантских библиотеках.
_SEARCH_BUCKET_CAP = 64
# Бюджет фон-фолбэка (перебор «похожих по написанию») — только когда
# бакеты ничего не дали или дали неуверенного кандидата.
_FUZZ_BUDGET_SEC = 0.40

_lib_cache: dict[str, Any] = {"ts": 0.0, "index": None, "search": None,
                              "building": False, "force": False}
_EMPTY_SEARCH: dict[str, Any] = {"translit": {}, "phon": {}, "wphon": {},
                                 "tkeys": {}, "bix": {}, "lists": {}}

# Слова-паразиты ассистента: ASR их шлёт вместе с названием («включи лусе
# юрселф»), и без чистки они ломают сравнение слово к слову.
FILLER_WORDS = frozenset({
    "включи", "поставь", "запусти", "послушай", "зацени", "накинь",
    "давай", "ну", "пожалуйста", "эй", "привет", "трек", "песню",
    "композицию", "play", "please",
})


def _word_score(qt: str, ntw: str) -> float:
    """Оценка одного слова запроса против одного слова названия (0..1)."""
    if qt == ntw:
        return SCORE_WORD
    qf, nf = phonetic_key(qt), phonetic_key(ntw)
    if qf and qf == nf:
        return SCORE_PHONETIC
    # утратив пробел: «сэлф» внутри «yourself»
    if qf and len(qf) >= 3 and len(qf) < len(nf) and qf in nf:
        return 0.85
    sim = similarity(qt, ntw)
    if sim >= 0.85:
        return 0.60 + 0.35 * sim
    if sim >= 0.70:
        return 0.45 + 0.30 * sim
    return 0.0


def _tokens_score(query: str, name: str) -> tuple[float, str]:
    """Слово к слову: квадрант запроса против квадранта названия.

    ASR теряет и добавляет пробелы («луз юр сэлф», «включи лусе юрселф»),
    поэтому целую фразу такому запросу не спасти. Сравниваем каждое слово
    запроса с каждым словом названия (после транслита), паразитов — выкидываем.
    """
    qwords = [tok for tok in query.split()
              if tok not in FILLER_WORDS and len(tok) >= 3]
    nwords = [word for word in name.split() if word]
    if not qwords or not nwords:
        return 0.0, ""
    total = 0.0
    for tok in qwords:
        tt = translit_ru_en(tok)
        best = 0.0
        for word in nwords:
            best = max(best, _word_score(tt, translit_ru_en(word)))
        total += best
    avg = total / len(qwords)
    if avg >= 0.70:
        return round(avg, 4), ("совпадение по словам" if avg >= 0.85
                               else "слова похожи")
    return 0.0, ""


def score_pair(query: str | None, name: str | None) -> tuple[float, str]:
    """Оценка «похожести» запроса на имя. Возвращает (score, причина).

    score >= GOOD_ENOUGH — можно принимать без уточнений.
    """
    q = norm_key(query)
    n = norm_key(name)
    if not q or not n:
        return 0.0, ""

    if q == n:
        return SCORE_EXACT, "точное совпадение"

    qt, nt = translit_ru_en(q), translit_ru_en(n)
    if qt and qt == nt:
        return SCORE_TRANSLIT, "транслитерация"

    pq, pn = phonetic_key(q), phonetic_key(n)
    if pq and pq == pn:
        return SCORE_PHONETIC, "звучит одинаково"

    # Похожесть написания — по транслиту (чтобы «еминем» и «eminem» были рядом).
    sim = similarity(qt, nt)
    if sim >= 0.85:
        return 0.60 + 0.35 * sim, "похоже по написанию"
    if sim >= 0.70:
        return 0.45 + 0.30 * sim, "похоже по написанию"

    # Многословный запрос: «включи eminem» — нужное слово внутри.
    if " " in q:
        for tok in q.split():
            if len(tok) < 3:
                continue
            tt = translit_ru_en(tok)
            if tt == nt or (tt and tt == translit_ru_en(tok) and phonetic_key(tok) == pn):
                return SCORE_WORD, "совпадение по слову"
            if tt and nt.startswith(tt) and len(tt) >= 4:
                return SCORE_WORD - 0.05, "совпадение по слову"

    # ASR редко шлёт название ровно: «включи лусе юрселф» — меж морфема затесался
    # глагол, и пробелы потерялись («луз юр сэлф»). Сравниваем слово к слову:
    # в каком запросе — титульные слова, и наоборот.
    score, why = _tokens_score(q, nt)
    if score:
        return score, why

    return 0.0, ""


def _rebuild_index(db) -> None:
    """Пересобрать индекс библиотеки + поисковые структуры.

    Вызывается фон-потоком (stale-while-revalidate) или синхронно на
    холодном старте / после явного invalidate_cache().
    """
    index: dict[str, dict[str, dict[str, str]]] = {
        "artist": {}, "track": {}, "album": {},
    }
    from app.db.models import Album, Artist, Track

    for nm, ext in db.query(Artist.name, Artist.external_id).distinct().all():
        key = norm_key(nm)
        if key and key not in index["artist"]:
            index["artist"][key] = {"name": str(nm), "id": str(ext or ""), "sub": ""}
    for nm, ext in db.query(Track.artist_name, Track.artist_external_id).distinct().all():
        key = norm_key(nm)
        if key and key not in index["artist"]:
            index["artist"][key] = {"name": str(nm), "id": str(ext or ""), "sub": ""}
    for title, artist, ext, dur in db.query(
            Track.title, Track.artist_name, Track.external_id,
            Track.duration_sec).distinct().all():
        key = norm_key(title)
        if key and key not in index["track"]:
            index["track"][key] = {"name": str(title), "id": str(ext or ""),
                                   "sub": str(artist or ""), "dur": dur}
    for title, artist, ext in db.query(
            Album.title, Album.artist_name, Album.external_id).distinct().all():
        key = norm_key(title)
        if key and key not in index["album"]:
            index["album"][key] = {"name": str(title), "id": str(ext or ""),
                                   "sub": str(artist or "")}

    search = _build_search(index)
    _lib_cache["index"] = index
    _lib_cache["search"] = search
    _lib_cache["ts"] = time.time()


def _build_search(index: dict[str, dict[str, dict[str, str]]]) -> dict[str, Any]:
    """Инвертированные бакеты по видам (как у больших сервисов).

    Для каждой записи раз прекомпьютим: транслит, фонетический ключ,
    фонетические ключи слов (название + исполнитель) и 2-граммы — дальше
    на каждый запрос работаем только с бакетами, без перебора библиотеки.
    """
    search: dict[str, Any] = {"translit": {}, "phon": {}, "wphon": {},
                              "tkeys": {}, "bix": {}, "lists": {}}
    for kind, entries in index.items():
        tmap: dict[str, list] = {}
        pmap: dict[str, list] = {}
        wmap: dict[str, list] = {}
        bmap: dict[str, list] = {}
        elems: list[str] = []  # ключи транслита, у которых есть имя
        for e in entries.values():
            if "_tr" not in e:
                n = str(e.get("name") or "")
                e["_tr"] = translit_ru_en(n)
                e["_ph"] = phonetic_key(n)
                sub = str(e.get("sub") or "")
                words = [w for w in (sub + " " + n).split() if len(w) >= 3]
                e["_wph"] = {phonetic_key(w) for w in words} - {""}
            tr = e.get("_tr") or ""
            if tr:
                tmap.setdefault(tr, []).append(e)
                elems.append(tr)
            if e.get("_ph"):
                pmap.setdefault(e["_ph"], []).append(e)
            for wp in e.get("_wph") or ():
                wmap.setdefault(wp, []).append(e)
        # bigram-индекс — классика спеллчекеров (bigram → транслит-ключи):
        # запрос берёт «свои» бакеты словарём и считает пересечения, это
        # O(бакеты запроса), а не O(библиотека). Дубли tr_key в разных
        # бакетах не страшны — хиты считаются и идёт пороговая фильтрация.
        for tr_key in tmap:
            for bgg in _bigrams(tr_key):
                bmap.setdefault(bgg, []).append(tr_key)
        search["translit"][kind] = tmap
        search["phon"][kind] = pmap
        search["wphon"][kind] = wmap
        search["tkeys"][kind] = sorted(tmap)
        search["bix"][kind] = bmap
        search["lists"][kind] = sorted(
            tmap.values(), key=lambda lst: lst[0].get("name") or "")
    return search


def _rebuild_thread() -> None:
    """Фоновая перестройка: свой DB-коннект, запрос пользователя не ждёт."""
    try:
        from app.db.database import session_scope

        with session_scope() as db:
            _rebuild_index(db)
    except Exception as e:
        logger.warning("фоновая перестройка индекса не удалась: {}", e)
    finally:
        _lib_cache["building"] = False


def _library_data(db) -> tuple[dict, dict]:
    """Актуальные или чуть протухшие (index, search), без ожидания.

    Холодный старт и явный invalidate_cache() — САМЫЙ МЕДЛЕННЫЙ РЕЖИМ
    синхронной сборки; вся остальная жизнь протухший индекс остаётся
    у клиентов добрых 20 минут — и перестройка идёт в фоне.
    """
    now = time.time()
    have = _lib_cache.get("search") is not None
    fresh = now - float(_lib_cache.get("ts") or 0.0) < _LIB_CACHE_TTL
    if not fresh:
        forced = bool(_lib_cache.get("force"))
        if have and not forced:
            # Протухшее — вкусно для пользователя: отдаём сразу и
            # перечитываем в фоне.
            if not _lib_cache.get("building"):
                from threading import Thread

                _lib_cache["building"] = True
                Thread(target=_rebuild_thread, daemon=True,
                       name="query-index-rebuild").start()
            return _lib_cache.get("index") or {}, _lib_cache.get("search") or _EMPTY_SEARCH
        try:
            _rebuild_index(db)
        except Exception as e:
            logger.warning("не удалось собрать индекс библиотеки: {}", e)
    _lib_cache["force"] = False
    return _lib_cache.get("index") or {}, _lib_cache.get("search") or _EMPTY_SEARCH


def _library_index(db) -> dict[str, dict[str, dict[str, str]]]:
    """Индекс библиотеки по видам: артист / трек / альбом (совместимость).

    kind → {norm_имя: {"name": название, "id": external_id, "sub": исполнитель}}

    Артисты берём и из таблицы artists, и из artist_name треков: часть
    библиотек отдаёт артистов только через треки. Для треков и альбомов
    храним исполнителя отдельно — запрос вида «Eminem Lose Yourself»
    должен находиться по названию + исполнителю.
    """
    index, _ = _library_data(db)
    return index


def _part_score(tokens: list[str], words: list[str]) -> float:
    """Средняя уверенность фрагмента запроса против списка слов (0..1).

    Тот же примитив, что в _tokens_score, но слова названия/артиста
    подготовлены снаружи — фолбэк «артист и трек» дёргает его парами.
    """
    if not tokens or not words:
        return 0.0
    total = 0.0
    for tok in tokens:
        tt = translit_ru_en(tok)
        best = 0.0
        for word in words:
            best = max(best, _word_score(tt, translit_ru_en(word)))
        total += best
    return total / len(tokens)


def _dual_score(query: str, name: str, sub: str) -> tuple[float, str]:
    """Парное распознавание «артист — трек»: «включи эминем веном» →
    трек «Venom» (Eminem).

    Голос редко даёт точную границу между исполнителем и названием, поэтому
    дробим запрос на фрагменты: каждый фрагмент должен уверенно лечь на своё
    поле (название или исполнитель), иначе внешне похожие фразы
    («богс план» — оба слова названия) трогаем обычным словным скорингом.
    """
    qwords = [tok for tok in norm_key(query).split()
              if tok not in FILLER_WORDS and len(tok) >= 3]
    if len(qwords) < 2 or not sub:
        return 0.0, ""
    name_words = [w for w in norm_key(name).split() if len(w) >= 2]
    sub_words = [w for w in norm_key(sub).split() if len(w) >= 2]
    if not name_words or not sub_words:
        return 0.0, ""
    best = 0.0
    for cut in range(1, len(qwords)):
        head, tail = qwords[:cut], qwords[cut:]
        for t_part, s_part in ((head, tail), (tail, head)):
            st = _part_score(t_part, name_words)
            if st < 0.75:
                continue
            ss = _part_score(s_part, sub_words)
            if ss < 0.75:
                continue
            sc = min(0.94, (st + ss) / 2 + 0.06)
            best = max(best, sc)
    return (round(best, 4), "артист и трек") if best else (0.0, "")


def _score_candidate(query: str, kind: str, entry: dict[str, str]) -> tuple[float, str]:
    """Оценка кандидата с учётом вида: трек/альбом ищутся и по названию,
    и по паре «название + исполнитель», и по «артисту + треку» сразу
    (двойное распознавание голосовой фразы)."""
    score, why = score_pair(query, entry.get("name") or "")
    sub = (entry.get("sub") or "").strip()
    if sub:
        full = f"{entry.get('name') or ''} {sub}"
        score2, why2 = score_pair(query, full)
        if score2 > score:
            score, why = min(score2, 0.97), (
                "название с исполнителем" if why2 == "точное совпадение" else why2)
        dsc, dwhy = _dual_score(query, entry.get("name") or "", sub)
        if dsc > score:
            score, why = dsc, dwhy
    return score, why


def _entry_dur(entry: dict[str, Any]) -> int | None:
    """duration_sec из кэша индекса (None — у артистов/алиасов её нет)."""
    try:
        d = entry.get("dur")
        return int(d) if d not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None


def _bigrams(s: str) -> frozenset:
    """2-граммы строки — дешёвый префильтр для «похожести по написанию»."""
    if len(s) <= 2:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def _fuzz_pick(q: str, kinds: list[str], search: dict, pool: dict) -> None:
    """Бюджетный фолбэк «похоже по написанию» (Левенштейн-случаи).

    Прошлый резолвер прогонял дорогую метрику по ВСЕЙ библиотеке — на 80к+
    треков это держало голосовой запрос в десятках секунд. Теперь кандидаты
    достаются из инвертированного bigram-индекса (классика спеллчекеров):
    берём «свои» бакеты словарём, ранжируем по числу общих bigram и кладём
    в pool только топ. Работа пропорциональна бакетам запроса, а не размеру
    библиотеки. Вызывается, только когда уверенного кандидата ещё нет.
    """
    words = [t for t in q.split() if t not in FILLER_WORDS and len(t) >= 3]
    parts = [translit_ru_en(w) for w in (words or [q])]
    qbg: set[str] = set()
    for p in parts:
        if p:
            qbg |= _bigrams(p)
    if not qbg:
        return
    # Порог пересечения: у случайных строк обычно ≤1 общая bigram,
    # у настоящих опечаток — 2+. Короткие запросы ловятся и по одной.
    need = 2 if len(qbg) >= 4 else 1
    # Хиты считаем по (kind, tr_key): bigram-бакет хранит транслит-ключи,
    # сами записи раскрываются из tmap-бакета уже после ранжирования.
    hits: dict[tuple[str, str], int] = {}
    t0 = time.perf_counter()
    for kind in kinds:
        bix = ((search.get("bix") or {}).get(kind)) or {}
        if not bix:
            continue
        for bgg in qbg:
            for tr_key in bix.get(bgg) or ():
                hits[(kind, tr_key)] = hits.get((kind, tr_key), 0) + 1
            if time.perf_counter() - t0 > _FUZZ_BUDGET_SEC:
                break
        if time.perf_counter() - t0 > _FUZZ_BUDGET_SEC:
            break
    top = sorted(hits.items(), key=lambda kv: -kv[1])[:_SEARCH_BUCKET_CAP]
    for (kind, tr_key), cnt in top:
        if cnt < need:
            continue
        tmap = ((search.get("translit") or {}).get(kind)) or {}
        for e in (tmap.get(tr_key) or ()):
            kk = (kind, norm_key(str(e.get("name") or "")))
            if kk not in pool:
                pool[kk] = (kind, e)


def _display(kind: str, entry: dict[str, str]) -> str:
    """Как показывать кандидата человеку: «Lose Yourself — Eminem»."""
    name = entry.get("name") or ""
    sub = (entry.get("sub") or "").strip()
    if kind != "artist" and sub and sub.lower() != name.lower():
        return f"{name} — {sub}"
    return name


def invalidate_cache() -> None:
    """Сбросить кэш имён (после скана библиотеки).

    Явный вызов — значит «данные изменились»: следующий resolve пересоберёт
    индекс СИНХРОННО, чтобы новые треки сразу находились. Фоновая
    перестройка (stale-while-revalidate) работает только по TTL.
    """
    _lib_cache["ts"] = 0.0
    _lib_cache["force"] = True


def lookup_alias(db, query: str, user_id: str | None = None,
                 kind: str = "artist") -> dict[str, Any] | None:
    """Выученный алиас: «эминем» → «Eminem». user_id=None — общие алиасы.

    Сначала ищем точное совпадение. Если его нет — берём ближайший похожий
    алиас: голос пишет одно и то же слово по-разному («имейджен драгонс» и
    «имайджен драгонс»), и учить каждое написание отдельно не нужно.
    """
    key = norm_key(query)
    if not key:
        return None
    try:
        from app.db.models import QueryAlias

        q = db.query(QueryAlias)
        if user_id:
            rows = q.filter((QueryAlias.user_id == user_id)
                            | (QueryAlias.user_id.is_(None))).all()
        else:
            rows = q.filter(QueryAlias.user_id.is_(None)).all()
        exact = None
        best: tuple[float, Any] | None = None
        for row in rows:
            if kind and row.kind and row.kind != kind:
                continue
            if norm_key(row.alias) == key:
                exact = row
                break
            score, _ = score_pair(query, row.alias)
            if score >= ALIAS_FUZZY_MIN and (best is None or score > best[0]):
                best = (score, row)
        row = exact
        why = "выучено"
        if row is None and best is not None:
            row = best[1]
            why = "выучено (похожее написание)"
        if row is None:
            return None
        return {
            "name": row.target_name,
            # У алиасов название и «чистое» имя совпадают; sub у них нет.
            "title": row.target_name,
            "external_id": row.target_external_id or "",
            "score": 1.0,
            "why": why,
            "kind": row.kind or kind,
            "source": "alias",
            "sub": "",
            "learned_from": row.source or "",
        }
    except Exception as e:
        logger.warning("не удалось прочитать алиасы: {}", e)
    return None


def resolve(query: str | None, db, *, user_id: str | None = None,
            kind: str = "artist", top_k: int = 5,
            extra_names: Iterable[str] | None = None) -> dict[str, Any]:
    """Основная точка входа: строка → кандидаты.

    Возвращает:
      {"ok": True, "query": ..., "best": {...} | None, "candidates": [...],
       "reason": "..."}
    """
    raw = (query or "").strip()
    top_k = max(1, min(20, int(top_k or 5)))
    if not raw:
        return {"ok": True, "query": "", "best": None, "candidates": [], "reason": "пусто"}

    cands: list[dict[str, Any]] = []
    seen: set[str] = set()

    kinds = [kind] if kind in ("artist", "track", "album") else ["artist", "track", "album"]

    for one_kind in kinds:
        alias = lookup_alias(db, raw, user_id=user_id, kind=one_kind)
        if alias:
            alias = dict(alias)
            alias["kind"] = one_kind
            cands.append(alias)
            seen.add(norm_key(alias["name"]))

    index, search = _library_data(db)

    # --- генерация кандидатов из бакетов (по возрастанию затрат) ---
    q = norm_key(raw)
    qt = translit_ru_en(q)
    pq = phonetic_key(q)
    pool: dict[tuple[str, str], tuple[str, dict]] = {}

    def _put(entries: Iterable, one_kind: str) -> None:
        n = 0
        for e in entries or []:
            kk = (one_kind, norm_key(str(e.get("name") or "")))
            if kk not in pool:
                pool[kk] = (one_kind, e)
                n += 1
            if n >= _SEARCH_BUCKET_CAP:
                break

    def _prefix(kind: str, prefix: str) -> list:
        tmap = (search.get("translit") or {}).get(kind) or {}
        tkeys = (search.get("tkeys") or {}).get(kind) or []
        i = bisect.bisect_left(tkeys, prefix)
        out: list = []
        for kk in tkeys[i:i + _SEARCH_BUCKET_CAP]:
            if not kk.startswith(prefix):
                break
            out.extend(tmap.get(kk) or [])
        return out

    for one_kind in kinds:
        e = (index.get(one_kind) or {}).get(q)
        if e is not None:
            pool[(one_kind, q)] = (one_kind, e)
        _put((search.get("translit") or {}).get(one_kind, {}).get(qt), one_kind)
        if pq:
            _put((search.get("phon") or {}).get(one_kind, {}).get(pq), one_kind)

    # токены запроса: слово к слову (обрезаем филеры как при скоринге)
    qtokens = [t for t in q.split() if t not in FILLER_WORDS and len(t) >= 3]
    for tok in qtokens:
        tt = translit_ru_en(tok)
        tp = phonetic_key(tok)
        for one_kind in kinds:
            if tt:
                # токен = имя целиком или его префикс («включи eminem»)
                _put(_prefix(one_kind, tt), one_kind)
            if tp:
                # слово запроса звучит как слово названия/исполнителя
                wmap = (search.get("wphon") or {}).get(one_kind)
                if wmap:
                    _put(wmap.get(tp), one_kind)

    # Фолбэк «похоже по написанию»: только когда уверенного кандидата ещё
    # нет (алиасы и бакеты промахнулись). Перебор бюджетный — см. _fuzz_pick.
    if not any(float(c.get("score") or 0) >= GOOD_ENOUGH for c in cands):
        _fuzz_pick(q, kinds, search, pool)

    for (one_kind, _key), (_own, entry) in pool.items():
        if _key in seen:
            continue
        sc, why = _score_candidate(raw, one_kind, entry)
        if sc <= 0.0:
            continue
        cands.append({
            "name": _display(one_kind, entry),
            # Чистое название без «— Исполнитель»: клиентам, которые потом
            # ищут по имени в каталоге, нужно именно оно.
            "title": entry.get("name") or "",
            "external_id": entry.get("id", ""),
            # Длительность трека — только у kind='track': плеер строит
            # duration_ms из неё, ноль выглядел как битый прогресс.
            "duration_sec": _entry_dur(entry),
            "score": round(sc, 4),
            "why": why,
            "kind": one_kind,
            "source": "library",
            "sub": (entry.get("sub") or "").strip(),
        })
        seen.add(_key)

    for extra in (extra_names or []):
        disp = str(extra or "").strip()
        if not disp:
            continue
        key = norm_key(disp)
        if key in seen:
            continue
        sc, why = score_pair(raw, disp)
        if sc <= 0.0:
            continue
        cands.append({
            "name": disp,
            "title": disp,
            "external_id": "",
            "score": round(sc, 4),
            "why": why,
            "kind": kind,
            "source": "external",
            "sub": "",
        })
        seen.add(key)

    cands.sort(key=lambda c: (-float(c.get("score") or 0.0), len(str(c.get("name") or ""))))
    cands = cands[:top_k]
    best = cands[0] if cands else None
    reason = "нашлось" if best else "ничего не нашлось"
    if best and float(best.get("score") or 0.0) < GOOD_ENOUGH:
        reason = "нашлось, но неуверенно"
    return {
        "ok": True,
        "query": raw,
        "best": best,
        "candidates": cands,
        "reason": reason,
    }


def is_confident(result: dict[str, Any]) -> bool:
    """True, если найденный кандидат достаточно уверенный, чтобы взять его."""
    best = (result or {}).get("best") or {}
    try:
        return float(best.get("score") or 0.0) >= GOOD_ENOUGH
    except Exception:
        return False


def near_misses(query: str | None, names: Iterable[str], limit: int = 3) -> list[str]:
    """Имена, которые «почти» подошли — показываем как подсказки."""
    out: list[tuple[float, str]] = []
    for nm in names or []:
        sc, _ = score_pair(query, nm)
        if sc > 0:
            out.append((sc, str(nm)))
    out.sort(key=lambda x: -x[0])
    return [n for _, n in out[:limit]]


__all__ = [
    "GOOD_ENOUGH",
    "invalidate_cache",
    "is_confident",
    "levenshtein",
    "lookup_alias",
    "near_misses",
    "resolve",
    "score_pair",
]
