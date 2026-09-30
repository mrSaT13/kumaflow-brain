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


def _token_score(q: str, t: Track) -> float:
    s = 0.0
    ql = q.lower()
    tokens = [tok for tok in re.split(r"\W+", ql) if tok]
    hay = f"{(t.title or '').lower()} {(t.artist_name or '').lower()} {(t.album_name or '').lower()} {(t.genre or '').lower()}"
    for tok in tokens:
        if tok and tok in hay:
            s += 3
    if ql and ql in (t.title or "").lower():
        s += 5
    if ql and ql == (t.genre or "").lower():
        s += 4
    return s


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
                      user_id: str | None = None) -> list[Track]:
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
    pool = q.limit(2000).all()
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
    for t in pool:
        s = _token_score(query, t)
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
    # Сначала пробуем смысловой отбор через CLAP: он и есть «поиск по смыслу»,
    # и на нём запрос уже отфильтрован. Эвристика — запасной путь.
    candidates = _clap_candidates(db, query, limit=150, user_id=user_id)
    if candidates:
        logger.info("playlist_ai: {} кандидатов отобрано CLAP по смыслу запроса", len(candidates))
    else:
        candidates = _build_candidates(db, query, desired, limit=280,
                                       user_id=user_id)
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
    return {"name": parsed["name"], "comment": parsed["comment"], "ids": parsed["ids"], "songs": songs, "from_fallback": False, "raw": raw}
