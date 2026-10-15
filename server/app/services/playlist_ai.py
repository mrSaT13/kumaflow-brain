"""Порт ai_mix_service.dart generateFromPrompt + daily (копия, не вырезаем с mobile)."""
from __future__ import annotations

import json
import re
import random
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Track, TrackFeatures

logger = get_logger("playlist_ai")


def _token_score(q: str, t: Track,
                 alias_map: dict[str, list[str]] | None = None) -> float:
    s = 0.0
    ql = q.lower()
    tokens = [tok for tok in re.split(r"\W+", ql) if tok]
    # Расширяем токены запроса вариантами (транслит/алиасы, включая выученные):
    # иначе «форсаж» никогда не встретит альбом "Fast & Furious".
    try:
        tokens = _expand_tokens(tokens, alias_map=alias_map)
    except Exception:
        pass
    hay = f"{(t.title or '').lower()} {(t.artist_name or '').lower()} {(t.album_name or '').lower()} {(t.genre or '').lower()}"
    try:
        hay = f"{hay} {_translit_ru_en(hay)}"
    except Exception:
        pass
    for tok in tokens:
        if tok and len(tok) >= 3 and tok in hay:
            s += 3
    if ql and ql in (t.title or "").lower():
        s += 5
    if ql and ql == (t.genre or "").lower():
        s += 4
    # «фильм/ост/саундтрек/soundtrack» в запросе — предпочитаем треки,
    # у которых альбом похож на саундтрек, а не любой трек тех же артистов.
    try:
        if any(w in ql for w in ("фильм", "ост", "саундтрек", "soundtrack", "ost")):
            _al = (t.album_name or "").lower()
            if any(w in _al for w in ("ost", "soundtrack", "саундтрек", "fast", "furious", "film")):
                s += 4
    except Exception:
        pass
    return s


# Минимальная таблица RU->EN транслита для поиска (не для показа).
_RU_EN = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})


def _translit_ru_en(s: str) -> str:
    """Грубый RU->EN транслит нижнего регистра (для ILIKE/скоринга)."""
    try:
        return (s or "").lower().translate(_RU_EN)
    except Exception:
        return s or ""


# Известные франшизы/синонимы, которые транслитом не мостястя:
# «форсаж» транслитом даст forsazh, а в тегах лежит Fast & Furious.
_QUERY_ALIASES: dict[str, list[str]] = {
    'форсаж': ['fast', 'furious', 'forsazh'],
    'fast': ['форсаж', 'furious'],
    'furious': ['форсаж', 'fast'],
    'forsazh': ['fast', 'furious', 'форсаж'],
    'мстители': ['avengers'],
    'avengers': ['мстители'],
    'пираты': ['pirates', 'caribbean'],
    'кариб': ['pirates', 'caribbean'],
    'сега': ['sega'],
    'sega': ['сега'],
    'гонки': ['racing'],
    'racing': ['гонки'],
}


# Кэш expansion на процесс (запрос -> результат): повторные генерации
# по тому же тексту не дёргают LLM.
_EXPAND_CACHE: dict[str, dict] = {}


def _llm_expand_query(query: str) -> dict:
    """Понимание запроса через LLM (дешёвый вызов, без треков).

    Возвращает {"terms": [...до 12...], "songs": [{"title", "artist"}...до 20...]}.
    Любая ошибка/неконфиг -> {} (вызывающий остаётся на алиасах+транслите).
    Знание модели тут — только для поиска, ID она не выдумывает:
    резолв идёт по названиям в нашей БД.
    """
    q = (query or "").strip()
    if not q:
        return {}
    try:
        _ck = q.lower()
        if _ck in _EXPAND_CACHE:
            return _EXPAND_CACHE[_ck]
    except Exception:
        pass
    try:
        from app.services.ai import chat, is_configured

        if not is_configured():
            return {}
        prompt = (
            'Ты — музыкальный поисковик. Пользователь попросил подборку.\n'
            f'Запрос: "{q[:200]}"\n'
            'Верни СТРОГО JSON без markdown: {"terms": ["поисковые синонимы/переводы/названия франшиз на русском и английском, до 12 штук"], '
            '"songs": [{"title": "название песни", "artist": "исполнитель"}... до 20, только если запрос про конкретный фильм/игру/франшизу с известным саундтреком, иначе []}.\n'
            'Правила: terms — короткие токены для текстового поиска по тегам (без стоп-слов); songs — только уверенно известные треки франшизы, не выдумывай.'
        )
        raw = chat(messages=[{"role": "user", "content": prompt}],
                   temperature=0.2, max_tokens=400)
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        data = json.loads(m.group(0)) if m else json.loads(raw or "{}")
        terms: list[str] = []
        if isinstance(data, dict):
            for t in (data.get("terms") or [])[:12]:
                s = str(t or "").strip().lower()
                if s and len(s) >= 2 and s not in terms:
                    terms.append(s)
            songs: list[dict] = []
            for s in (data.get("songs") or [])[:20]:
                if not isinstance(s, dict):
                    continue
                _ti = str(s.get("title") or "").strip()
                _ar = str(s.get("artist") or "").strip()
                if _ti:
                    songs.append({"title": _ti, "artist": _ar})
            out = {"terms": terms, "songs": songs}
            try:
                if len(_EXPAND_CACHE) > 500:
                    _EXPAND_CACHE.clear()
                _EXPAND_CACHE[_ck] = out
            except Exception:
                pass
            return out
    except Exception as e:  # noqa: BLE001 — expansion опционален
        logger.warning("playlist_ai query expansion failed, fallback to aliases: {}", e)
    return {}


def _expand_tokens(tokens: list[str],
                   alias_map: dict[str, list[str]] | None = None) -> list[str]:
    """Токены + их транслит-варианты + алиасы франшиз (без дублей).

    alias_map: статика + выученное (см. _get_alias_map). Без него —
    статика + in-memory выученное, чтобы чистые эвристики тоже умнели.
    """
    try:
        _am = alias_map if alias_map is not None else _get_alias_map()
    except Exception:
        _am = _QUERY_ALIASES
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens or []:
        for cand in [tok, _translit_ru_en(tok)] + list((_am or {}).get(tok, [])):
            c = (cand or "").strip().lower()
            if c and len(c) >= 3 and c not in seen:
                seen.add(c)
                out.append(c)
    return out


# Ключ AppSetting для выученных алиасов + in-memory зеркало.
_LEARNED_KEY = "playlist_ai_learned_aliases"
_LEARNED_ALIASES: dict[str, list[str]] = {}


def _get_alias_map(db: Session | None = None) -> dict[str, list[str]]:
    """Статика + выученное. БД читаем best-effort (иначе только in-memory)."""
    merged: dict[str, list[str]] = {k: list(v) for k, v in _QUERY_ALIASES.items()}
    try:
        _learned: dict = dict(_LEARNED_ALIASES)
        if db is not None:
            from app.db.models import AppSetting as _AS

            try:
                row = db.get(_AS, _LEARNED_KEY)
                _val = dict(getattr(row, "value", None) or {})
                _db_aliases = _val.get("aliases") or {}
                if isinstance(_db_aliases, dict):
                    for _k, _v in _db_aliases.items():
                        if isinstance(_v, list):
                            _learned[str(_k)] = [str(x) for x in _v[:8]]
                    _LEARNED_ALIASES.update(_learned)
            except Exception:
                pass
        for _k, _v in _learned.items():
            if _k in merged:
                for _x in _v:
                    if _x not in merged[_k]:
                        merged[_k].append(_x)
            else:
                merged[_k] = list(_v)
    except Exception:
        pass
    return merged


def _learn_aliases(db: Session | None, query: str, terms: list[str]) -> None:
    """Автообновление словаря: токены запроса -> terms от LLM.

    Пишем best-effort в AppSetting + in-memory. Капы: 8 терминов на токен,
    300 токенов всего — словарь не распухает. Ошибка записи ничего не роняет.
    """
    try:
        _terms = [str(t or "").strip().lower() for t in (terms or [])]
        _terms = [t for t in _terms if t and len(t) >= 3][:8]
        if not _terms or db is None:
            return
        _base = [tok for tok in re.split(r"\W+", (query or "").lower()) if tok and len(tok) >= 3][:6]
        if not _base:
            return
        from app.db.models import AppSetting as _AS

        try:
            row = db.get(_AS, _LEARNED_KEY)
            _cur: dict = {}
            if row is not None:
                try:
                    _cur = dict(getattr(row, "value", None) or {}).get("aliases") or {}
                except Exception:
                    _cur = {}
            if not isinstance(_cur, dict):
                _cur = {}
            _changed = False
            for _b in _base:
                _have = list(_cur.get(_b) or [])
                for _t in _terms:
                    if _t != _b and _t not in _have and len(_have) < 8:
                        _have.append(_t)
                        _changed = True
                if _have:
                    _cur[_b] = _have
            # кап на размер словаря
            _keys = list(_cur.keys())[:300]
            _cur = {k: _cur[k] for k in _keys}
            if _changed:
                if row is None:
                    db.add(_AS(key=_LEARNED_KEY, value={"aliases": _cur}))
                else:
                    row.value = {"aliases": _cur}
                try:
                    db.flush()
                except Exception:
                    pass
                for _b in _base:
                    if _b in _cur:
                        _LEARNED_ALIASES[_b] = list(_cur[_b])
                logger.info("playlist_ai: словарь пополнен: {} -> {}", _base, _terms)
        except Exception as e:  # noqa: BLE001
            logger.warning("playlist_ai learn aliases failed: {}", e)
    except Exception:
        pass


def _tuning_cap(db: Session, user_id: str | None = None) -> int:
    """Личный лимит треков одного артиста (дефолт 2). Ошибка -> 2."""
    try:
        from app.services import rec_tuning as _rt

        v = (_rt.get_all(db, user_id).get("repeats") or {}).get(
            "artist_cap", 2)
        return max(1, min(20, int(float(v))))
    except Exception:
        return 2


def _clap_candidates(db: Session, query: str, limit: int = 150,
                     user_id: str | None = None) -> list[Track] | None:
    """Отбор кандидатов по смыслу запроса через CLAP-эмбеддинг.

    Зачем это вместо отправки 2000 треков в LLM. Старый путь брал pool из
    2000 строк, фильтровал по словам/настроению и отдавал модели ~280 треков —
    это 20–25 тыс. символов промпта, на которых локальная модель обычно
    упирается в контекст и уходит в keyword-фолбэк. Здесь фильтрует не LLM, а
    векторное пространство: модель получает горстку уже осмысленных кандидатов
    и только ранжирует их.

    Возвращает None, если CLAP недоступен/выключен или эмбеддингов ещё нет —
    тогда вызывающий берёт прежний эвристический путь.
    """
    try:
        from app.db.models import TrackEmbedding as _TE
        from app.services.clap import get_text_embedding, is_available
        from app.services.ml import search_by_embedding

        if not is_available():
            return None
        has = db.query(_TE).filter(_TE.model == "clap_text").count()
        if not has:
            return None
        vec = get_text_embedding(query)
        if not vec:
            return None
        hits = search_by_embedding(vec, top_k=min(limit * 2, 400), model="clap_text")
        if not hits:
            return None
        # search_by_embedding отдаёт свой срез: внешний id, title, artist, score.
        # score здесь — косинус к вектору запроса, то есть уже «смысловая близость».
        out: list[Track] = []
        seen: dict[str, int] = {}
        cap = _tuning_cap(db, user_id)
        for h in hits:
            t = db.get(Track, str(h.get("track_id") or ""))
            if t is None:
                continue
            artist = t.artist_name or "unknown"
            # Тот же diversity-cap, что и в эвристическом пути: не больше
            # cap треков одного артиста, иначе микс вырождается в одного исполнителя.
            if seen.get(artist, 0) >= cap:
                continue
            seen[artist] = seen.get(artist, 0) + 1
            out.append(t)
            if len(out) >= limit:
                break
        return out or None
    except Exception as e:
        logger.warning("clap candidates failed, fallback to heuristics: {}", e)
        return None


def _build_candidates(db: Session, query: str, desired: int = 30,
                      limit: int = 280,
                      user_id: str | None = None,
                      extra_terms: list[str] | None = None,
                      song_hints: list[dict] | None = None) -> list[Track]:
    from app.services.vibe import analyze_track, detect_mood, vibe_similarity

    # pool: все треки (в prod 700 stratified — упрощаем)
    #
    # Только то, что сыграет плеер. Локальные файлы (disk:/demo-) — служебные:
    # они нужны ради аудио-фичей, но в плейлисте им место только мешает, потому
    # что при выгрузке в Navidrome `playlist_push` их отбрасывает и очередь
    # выходит короче, чем запросили. В демо-режиме фильтр выключен сам.
    q = db.query(Track)
    try:
        from app.services import playable as _pl

        q = _pl.apply(q, db)
    except Exception:  # noqa: BLE001 — не блокируем подбор из-за фильтра
        pass
    # Сначала — релевантные запросу (ILIKE по расширенным токенам),
    # иначе нишевый запрос (саундтрек франшизы) тонет в первых 2000 строках
    # 78к библиотеки и LLM выбирает из нерелевантных. Остаток добиваем
    # случайной выборкой, чтобы микс не вырождался в один альбом.
    MATCH_CAP, POOL_CAP = 1000, 2000
    matched: list[Track] = []
    try:
        _alias_map = _get_alias_map(db)
    except Exception:
        _alias_map = None
    try:
        from sqlalchemy import func as _func
        from sqlalchemy import or_ as _or

        _base_toks = [tok for tok in re.split(r"\W+", query.lower()) if tok]
        for _et in (extra_terms or []):
            _base_toks.extend([tok for tok in re.split(r"\W+", str(_et).lower()) if tok])
        _toks = [t for t in _expand_tokens(_base_toks, alias_map=_alias_map) if len(t) >= 3][:16]
        if _toks:
            _conds = []
            for _tok in _toks:
                _like = f"%{_tok}%"
                _conds.append(Track.title.ilike(_like))
                _conds.append(Track.artist_name.ilike(_like))
                _conds.append(Track.album_name.ilike(_like))
            # Подсказки песен от LLM резолвим напрямую (title/artist),
            # даже если альбом в тегах пустой и франшиза там не упомянута.
            for _sh in (song_hints or [])[:20]:
                try:
                    _st = str((_sh or {}).get("title") or "").strip()
                    _sa = str((_sh or {}).get("artist") or "").strip()
                except Exception:
                    continue
                if _st and len(_st) >= 3:
                    _conds.append(Track.title.ilike(f"%{_st}%"))
                if _sa and len(_sa) >= 3:
                    _conds.append(Track.artist_name.ilike(f"%{_sa}%"))
            matched = q.filter(_or(*_conds)).limit(MATCH_CAP).all()
    except Exception as e:  # noqa: BLE001 — откат на старый путь
        logger.warning("playlist_ai match prefilter failed, fallback to pool: {}", e)
        matched = []
    pool: list[Track] = list(matched)
    if len(pool) < POOL_CAP:
        try:
            _seen_ids = {str(t.id) for t in pool}
            _rest = q.order_by(_func.random()).limit(
                min(POOL_CAP * 2, POOL_CAP - len(pool) + 500)).all()
            for _t in _rest:
                if str(_t.id) not in _seen_ids:
                    pool.append(_t)
                    _seen_ids.add(str(_t.id))
                if len(pool) >= POOL_CAP:
                    break
        except Exception:
            # старый SQLite без random() — добиваем первыми строками
            try:
                _seen_ids = {str(t.id) for t in pool}
                for _t in q.limit(POOL_CAP * 2).all():
                    if str(_t.id) not in _seen_ids:
                        pool.append(_t)
                    if len(pool) >= POOL_CAP:
                        break
            except Exception:
                pass
    pool = pool[:POOL_CAP]
    if not pool:
        return []
    # vibe target из запроса (эвристика mood слов)
    ql = query.lower()
    target_mood = None
    for m in ["energetic", "happy", "calm", "sad", "aggressive", "melancholic", "focused"]:
        if m in ql or ("энергич" in ql and m == "energetic") or ("спокойн" in ql and m == "calm") or ("грустн" in ql and m == "sad"):
            target_mood = m
            break
    scored: list[tuple[float, Track]] = []
    # Нормализуем подсказки песен один раз (ниже — простое вхождение).
    _hints: list[tuple[str, str]] = []
    try:
        for _sh in (song_hints or [])[:20]:
            _ht = str((_sh or {}).get("title") or "").strip().lower()
            _ha = str((_sh or {}).get("artist") or "").strip().lower()
            if _ht and len(_ht) >= 3:
                _hints.append((_ht, _ha))
    except Exception:
        _hints = []
    # Расширенный текст запроса для скоринга (оригинал + terms от LLM).
    _score_q = query
    try:
        if extra_terms:
            _score_q = f"{query} {' '.join(str(x) for x in extra_terms[:12])}"
    except Exception:
        pass
    for t in pool:
        s = _token_score(_score_q, t, alias_map=_alias_map)
        # Прямое попадание в подсказанную песню — сильный бонус:
        # название совпало целиком, артист (если дан) — в теге.
        if _hints:
            try:
                _tt = (t.title or "").lower()
                _ta = (t.artist_name or "").lower()
                for _ht, _ha in _hints:
                    if _ht and _ht in _tt and (not _ha or _ha in _ta or _ta in _ha):
                        s += 8
                        break
            except Exception:
                pass
        # vibe
        try:
            feat = db.get(TrackFeatures, t.id)
            vibe = analyze_track(t.genre, energy=getattr(feat, "energy", None), valence=getattr(feat, "valence", None)) if feat else analyze_track(t.genre)
            mood = detect_mood(vibe)
            if target_mood and mood == target_mood:
                s += 2.5
            if feat and feat.energy is not None and target_mood in ("energetic", "calm"):
                target_e = 0.85 if target_mood == "energetic" else 0.3
                s += (1 - abs(float(feat.energy) - target_e)) * 1.2
        except Exception:
            pass
        # ML boost
        try:
            from app.db.database import session_scope  # avoid cycle

            # легкий — не тянем MLService, просто play_count
            s += (t.play_count or 0) * 0.02
        except Exception:
            pass
        scored.append((s, t))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    # diversity cap 60% артистов + jitter
    cap = _tuning_cap(db, user_id)
    seen: dict[str, int] = {}
    out: list[Track] = []
    for sc, t in scored:
        cnt = seen.get(t.artist_name or "unknown", 0)
        if cnt >= cap:
            continue
        jitter = random.uniform(0.8, 1.0)
        # смешаем скор с jitter ordering — сортировка уже есть, просто фильтр
        out.append(t)
        seen[t.artist_name or "unknown"] = cnt + 1
        if len(out) >= limit:
            break
    if len(out) < 40:
        # fallback random
        extra = [t for t in pool if t not in out]
        random.shuffle(extra)
        out.extend(extra[: max(0, 40 - len(out))])
    return out[:limit]


def _build_prompt(query: str, desired: int, candidates: list[Track]) -> str:
    lines = []
    for idx, t in enumerate(candidates, 1):
        dur = f"{(t.duration_sec or 0)//60}:{(t.duration_sec or 0)%60:02d}" if t.duration_sec else "?"
        lines.append(f'{idx}. [{t.id}] "{t.title}" — {t.artist_name} | {t.genre or "?"} | {dur}')
    cat = "\n".join(lines)
    return f'''Ты — музыкальный куратор KumaFlow. Пользователь попросил подборку.
Запрос пользователя: "{query}"
Желаемое количество треков: {desired}
Кандидаты (выбери ТОЛЬКО из этого списка, не выдумывай ID):
{cat}

Верни СТРОГО JSON без markdown:
{{
  "name": "Короткое креативное название 2-4 слова на русском",
  "comment": "1-2 предложения описания, почему эти треки подходят под запрос (на русском)",
  "ids": ["id1","id2",... ровно {desired} или близко, только ID из списка]
}}
Правила:
- ids только из кандидатов, без повторов
- Разнообразие артистов (не более 2 треков одного артиста подряд)
- Учитывай жанр и настроение запроса
- Ответ только JSON, без пояснений до/после'''


def _parse_llm_json(raw: str, candidates: list[Track], desired: int) -> dict[str, Any] | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    cand = m.group(0) if m else raw
    try:
        data = json.loads(cand)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ids = data.get("ids")
    if not isinstance(ids, list):
        return None
    cand_ids = {str(t.id) for t in candidates}
    uniq: list[str] = []
    seen = set()
    for x in ids:
        sid = str(x).strip()
        if sid in cand_ids and sid not in seen:
            uniq.append(sid)
            seen.add(sid)
    if len(uniq) < max(3, desired // 2):
        return None
    name = str(data.get("name") or "KumaFlow Mix")[:60]
    comment = str(data.get("comment") or "")[:300]
    return {"name": name, "comment": comment, "ids": uniq[:desired]}


def _fallback_result(query: str, candidates: list[Track], desired: int,
                     reason: str = "", cap: int = 2) -> dict[str, Any]:
    # energy sort + diversity
    from app.db.database import session_scope
    from app.db.models import TrackFeatures

    # пробуем sort по energy
    try:
        from app.services.vibe import analyze_track

        # bulk feats
        cand_ids = [t.id for t in candidates]
        # simple sort по energy desc если есть
        candidates_sorted = sorted(candidates, key=lambda t: (t.play_count or 0), reverse=True)
    except Exception:
        candidates_sorted = candidates
    # diversity cap подряд
    cap = max(1, min(20, int(cap)))
    out: list[str] = []
    last_artist = None
    repeat = 0
    for t in candidates_sorted:
        if t.artist_name == last_artist:
            repeat += 1
            if repeat >= cap:
                continue
        else:
            repeat = 0
            last_artist = t.artist_name
        out.append(str(t.id))
        if len(out) >= desired:
            break
    return {"name": f"Микс: {query[:24]}", "comment": f"ИИ недоступен: {reason}. Показан локальный набор." if reason else "Локальный набор", "ids": out, "from_fallback": True}


def generate_from_prompt(db: Session, query: str, desired: int = 30,
                         hint_mood: str | None = None,
                         user_id: str | None = None) -> dict[str, Any]:
    """Копия mobile generateFromPrompt — кандидаты on-device + LLM на сервере (ai.py)."""
    if hint_mood:
        query = f"{query} {hint_mood}".strip()
    cap = _tuning_cap(db, user_id)
    # Понимание запроса через LLM (дешёвый вызов, ~400 токенов, без треков):
    # синонимы/переводы/треклист франшизы. Недоступна -> {} и работаем
    # на ручных алиасах + транслите. ID модель не выдумывает — резолв
    # подсказанных песен идёт по названиям в нашей БД ниже.
    _exp: dict = {}
    try:
        _exp = _llm_expand_query(query) or {}
    except Exception:
        _exp = {}
    _xterms: list[str] = list(_exp.get("terms") or [])
    _songs: list[dict] = list(_exp.get("songs") or [])
    _clap_q = query if not _xterms else f"{query} {' '.join(_xterms[:8])}"
    # Сначала пробуем смысловой отбор через CLAP: он и есть «поиск по смыслу»,
    # и на нём запрос уже отфильтрован. Эвристика — запасной путь.
    candidates = _clap_candidates(db, _clap_q, limit=150, user_id=user_id)
    if candidates:
        logger.info("playlist_ai: {} кандидатов отобрано CLAP по смыслу запроса", len(candidates))
    else:
        candidates = _build_candidates(db, query, desired, limit=280,
                                       user_id=user_id,
                                       extra_terms=_xterms or None,
                                       song_hints=_songs or None)
    if not candidates:
        return {"name": "Пусто", "comment": "Библиотека пуста", "ids": [], "songs": [], "from_fallback": True, "raw": ""}
    prompt = _build_prompt(query, desired, candidates)
    raw = ""
    try:
        from app.services.ai import chat, is_configured

        if is_configured():
            raw = chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1200,
            )
        else:
            raise RuntimeError("AI not configured")
    except Exception as e:
        logger.warning("playlist_ai llm failed: {}", e)
        fb = _fallback_result(query, candidates, desired, reason=str(e),
                              cap=cap)
        # резолв songs
        songs = [db.get(Track, sid) for sid in fb["ids"]]
        songs = [s for s in songs if s]
        return {"name": fb["name"], "comment": fb["comment"], "ids": fb["ids"], "songs": songs, "from_fallback": True, "raw": raw}
    parsed = _parse_llm_json(raw, candidates, desired)
    if not parsed:
        # Попробуем вытащить ids регексом. ВАЖНО: только из списка кандидатов.
        # Раньше брались ЛЮБЫЕ uuid из ответа модели, и если она «выдумала» трек,
        # его id уезжал в PlaylistTrack и ронял транзакцию на Postgres
        # (FK-ошибка → 500 на весь запрос). Теперь незнакомые id отбрасываются.
        found = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw)
        allowed = {str(t.id) for t in candidates}
        ids: list[str] = []
        for sid in found:
            if sid in allowed and sid not in ids:
                ids.append(sid)
            if len(ids) >= desired:
                break
        if ids:
            parsed = {"name": "KumaFlow Mix", "comment": "", "ids": ids}
        else:
            fb = _fallback_result(query, candidates, desired,
                              reason="parse failed", cap=cap)
            songs = [db.get(Track, sid) for sid in fb["ids"]]
            songs = [s for s in songs if s]
            return {"name": fb["name"], "comment": fb["comment"], "ids": fb["ids"], "songs": songs, "from_fallback": True, "raw": raw}
    # Последняя страховка: в ids не должно быть ничего, чего нет в кандидатах.
    _allowed = {str(t.id) for t in candidates}
    parsed["ids"] = [sid for sid in dict.fromkeys(parsed["ids"]) if sid in _allowed]
    songs = [db.get(Track, sid) for sid in parsed["ids"]]
    songs = [s for s in songs if s]
    # Самообучение словаря: раз LLM поняла запрос, запоминаем связку
    # «токены запроса -> её terms», чтобы в следующий раз сработало даже
    # без модели. Best-effort, генерацию не роняет.
    try:
        if _xterms:
            _learn_aliases(db, query, _xterms)
            try:
                db.commit()
            except Exception:
                pass
    except Exception:
        pass
    return {"name": parsed["name"], "comment": parsed["comment"], "ids": parsed["ids"], "songs": songs, "from_fallback": False, "raw": raw}
