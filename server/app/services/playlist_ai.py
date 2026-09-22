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


def _build_candidates(db: Session, query: str, desired: int = 30, limit: int = 280) -> list[Track]:
    from app.services.vibe import analyze_track, detect_mood, vibe_similarity

    # pool: все треки (в prod 700 stratified — упрощаем)
    pool = db.query(Track).limit(2000).all()
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
    seen: dict[str, int] = {}
    out: list[Track] = []
    for sc, t in scored:
        cnt = seen.get(t.artist_name or "unknown", 0)
        if cnt >= 2:
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


def _fallback_result(query: str, candidates: list[Track], desired: int, reason: str = "") -> dict[str, Any]:
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
    # diversity 2 подряд
    out: list[str] = []
    last_artist = None
    repeat = 0
    for t in candidates_sorted:
        if t.artist_name == last_artist:
            repeat += 1
            if repeat >= 2:
                continue
        else:
            repeat = 0
            last_artist = t.artist_name
        out.append(str(t.id))
        if len(out) >= desired:
            break
    return {"name": f"Микс: {query[:24]}", "comment": f"ИИ недоступен: {reason}. Показан локальный набор." if reason else "Локальный набор", "ids": out, "from_fallback": True}


def generate_from_prompt(db: Session, query: str, desired: int = 30, hint_mood: str | None = None) -> dict[str, Any]:
    """Копия mobile generateFromPrompt — кандидаты on-device + LLM на сервере (ai.py)."""
    if hint_mood:
        query = f"{query} {hint_mood}".strip()
    candidates = _build_candidates(db, query, desired, limit=280)
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
        fb = _fallback_result(query, candidates, desired, reason=str(e))
        # резолв songs
        songs = [db.get(Track, sid) for sid in fb["ids"]]
        songs = [s for s in songs if s]
        return {"name": fb["name"], "comment": fb["comment"], "ids": fb["ids"], "songs": songs, "from_fallback": True, "raw": raw}
    parsed = _parse_llm_json(raw, candidates, desired)
    if not parsed:
        # попробуем вытащить ids регексом
        ids = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw)
        if ids:
            parsed = {"name": "KumaFlow Mix", "comment": "", "ids": ids[:desired]}
        else:
            fb = _fallback_result(query, candidates, desired, reason="parse failed")
            songs = [db.get(Track, sid) for sid in fb["ids"]]
            songs = [s for s in songs if s]
            return {"name": fb["name"], "comment": fb["comment"], "ids": fb["ids"], "songs": songs, "from_fallback": True, "raw": raw}
    songs = [db.get(Track, sid) for sid in parsed["ids"]]
    songs = [s for s in songs if s]
    return {"name": parsed["name"], "comment": parsed["comment"], "ids": parsed["ids"], "songs": songs, "from_fallback": False, "raw": raw}
