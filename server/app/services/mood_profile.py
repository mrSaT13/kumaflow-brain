"""Mood-профиль юзера: в каких настроениях он живёт (а не что лайкнул).

Волна знала «что любишь» (лайки/артисты), но не «какой звук твой»:
без пилюли играла среднее по больнице. Профиль — взвешенный по
свежести центроид 4-осевого муд-пространства (valence/energy/arousal/
dance — те же числа анализатора) по позитивным прослушиваниям за окно.
Скипы сюда не входят сознательно: скип чаще «не этот трек», а не
«не это настроение» (ими рулит session_drift).

Использование — мягкий приор в скоринге (`character.mood_profile_weight`,
дефолт 0.04): трек ближе к твоему центроиду — чуть выше. Пилюля при этом
побеждает всегда: она режет пул жёстким фильтром, профиль лишь выбирает
внутри него твой оттенок агрессивного/спокойного.

Минимум 5 треков с анализом, иначе centroid None (вклад 0, волна 1в1).
Кэш на сессию: за HTTP-запрос профиль считают и волна, и скоринг.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

# Окно профиля и затухание свежести (период полураспада).
WINDOW_DAYS = 30
HALF_LIFE_DAYS = 14.0
# Минимум треков с анализом, чтобы центроид что-то значил.
MIN_TRACKS = 5
# Веса позитивных сигналов (лайк — самый явный вкус).
ACTION_W = {"like": 3.0, "favorite": 3.0, "replay": 2.0, "complete": 1.5}

_AXES = ("valence", "energy", "arousal", "dance")


def _utcnow() -> datetime:
    try:
        from app.core.time import utcnow as _u

        return _u()
    except Exception:
        return datetime.utcnow()


def _decay(age_days: float) -> float:
    try:
        return 0.5 ** (max(0.0, float(age_days)) / HALF_LIFE_DAYS)
    except (TypeError, ValueError):
        return 0.0


def get_profile(db, user_id: str, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """{centroid, spread, weight, tracks, top_moods}. Пусто — centroid None.

    Никогда не падает: ошибка БД/пустая история — нулевой профиль.
    """
    empty: dict[str, Any] = {"centroid": None, "spread": 0.0, "weight": 0.0,
                              "tracks": 0, "top_moods": []}
    try:
        uid = str(user_id or "").strip()
        if not uid:
            return empty
        try:
            from app.services.taste import _req_store as _rs

            cache = _rs(db, "moodprof")
            hit = cache.get(uid)
            if isinstance(hit, dict) and hit.get("centroid") is not None:
                return hit
        except Exception:
            cache = None
        from app.db.models import PlayEvent, PlayHistory, TrackFeatures

        now = _utcnow()
        try:
            days = max(7, min(180, int(days or WINDOW_DAYS)))
        except (TypeError, ValueError):
            days = WINDOW_DAYS
        since = now - timedelta(days=days)
        # Сигнал -> вес: история прослушиваний + позитивные события.
        sig: dict[str, float] = {}
        try:
            for tid, when in db.query(PlayHistory.track_id, PlayHistory.played_at).filter(
                    PlayHistory.user_id == uid,
                    PlayHistory.played_at >= since).all():
                w = 1.0 * _decay((now - when).total_seconds() / 86400.0) if when else 0.5
                sig[str(tid)] = sig.get(str(tid), 0.0) + w
        except Exception:
            pass
        try:
            for tid, act, when in db.query(
                    PlayEvent.track_id, PlayEvent.action, PlayEvent.created_at).filter(
                    PlayEvent.user_id == uid,
                    PlayEvent.created_at >= since).all():
                base = ACTION_W.get(str(act or "").strip().lower())
                if not base:
                    continue
                w = base * _decay((now - when).total_seconds() / 86400.0) if when else base * 0.5
                sig[str(tid)] = sig.get(str(tid), 0.0) + w
        except Exception:
            pass
        if not sig:
            return empty
        tids = list(sig)
        feats: dict[str, Any] = {}
        try:
            for f in db.query(TrackFeatures).filter(
                    TrackFeatures.track_id.in_(tids)).all():
                feats[str(f.track_id)] = f
        except Exception:
            pass
        acc: dict[str, float] = {a: 0.0 for a in _AXES}
        tot = 0.0
        used = 0
        mood_c: Counter = Counter()
        vecs: list[tuple[float, dict[str, float]]] = []
        for tid, w in sig.items():
            f = feats.get(tid)
            if f is None or w <= 0:
                continue
            v: dict[str, float] = {}
            for axis, attr in (("valence", "valence"), ("energy", "energy"),
                               ("arousal", "arousal"), ("dance", "danceability")):
                try:
                    raw = getattr(f, attr, None)
                    val = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    val = None
                if val is not None:
                    v[axis] = min(max(val, 0.0), 1.0)
            if not v:
                continue
            for a, val in v.items():
                acc[a] += val * w
            tot += w
            used += 1
            vecs.append((w, v))
            try:
                for m in list(getattr(f, "mood_labels", None) or [])[:2]:
                    mood_c[str(m).lower()] += 1
            except Exception:
                pass
        if used < MIN_TRACKS or tot <= 0:
            return empty
        centroid = {a: acc[a] / tot for a in _AXES if acc[a] > 0}
        if not centroid:
            return empty
        # Разброс: взвешенное RMS-отклонение от центроида (уверенность вкуса).
        num = den = 0.0
        for w, v in vecs:
            for a, c in centroid.items():
                if a in v:
                    num += w * (v[a] - c) ** 2
                    den += w
        spread = (num / den) ** 0.5 if den > 0 else 0.0
        out = {"centroid": centroid, "spread": round(float(spread), 3),
               "weight": round(float(tot), 2), "tracks": used,
               "top_moods": [{"name": m, "count": c}
                             for m, c in mood_c.most_common(6)]}
        try:
            if cache is not None:
                cache[uid] = out
        except Exception:
            pass
        return out
    except Exception:
        return empty
