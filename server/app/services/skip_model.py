"""Обученный P(skip<30с): логистическая регрессия поверх PlayEvent.

Заменяет эвристику :func:`wave.skip_risk` изнутри, когда событий достаточно
(``MIN_LABELED``). Мало данных — :func:`get_model` возвращает ``None`` и волна
работает на старых прозрачных правилах, ничего не ломается.

Признаки — только то, что доступно и при обучении, и при инференсе внутри
``score_candidates`` (фичи трека, профиль вкуса, якорь энергии, дрейф,
агрегаты пары user/track). Ничего нового в БД не пишем: модель живёт в
процессе (TTL-кэш), метрики — в ``AppSetting['skip_model']`` для диагностики.
"""
from __future__ import annotations

import math
import time
from typing import Any

# Порядок фиксирован: инференс и обучение строят вектор одной функцией.
FEATURES: list[str] = [
    "energy",            # 0..1 (нет фичей -> 0.5)
    "valence",           # 0..1
    "danceability",      # 0..1
    "tempo_norm",        # bpm/200
    "energy_jump",       # |energy - anchor| (якорь: текущий трек / прошлое событие)
    "unfamiliar_genre",  # 0/1 — жанра нет в preferredGenres профиля
    "artist_fatigue",    # min(plays артиста за 7д, 20)/20 (инференс) / повторы (трейн)
    "hour_sin",          # sin(2πh/24)
    "hour_cos",          # cos(2πh/24)
    "night",             # 0/1 — 22..5
    "weekend",           # 0/1 — сб/вс
    "plays_log",         # log1p(plays пары user/track)
    "early_skip_rate",   # early_skips/(plays+1)
    "complete_rate",     # completes/(plays+1)
    "drift_level",       # 0..3 — стрик скипов (none/mild/moderate/strong)
]

MIN_LABELED = 5000   # минимум размеченных строк (роадмап: 5-10к событий)
MIN_POSITIVE = 200   # минимум ранних скипов (иначе дисбаланс не вытянуть)
DATASET_LIMIT = 20000  # свежие события, nuevos primero
MODEL_TTL_SEC = 6 * 3600
SETTINGS_KEY = "skip_model"

_cache: dict[str, Any] = {"at": 0.0, "pipe": None, "metrics": {}}


def label_for(action: str | None, position_sec: Any) -> int | None:
    """Разметка события: 1 — ранний скип, 0 — явный позитив, None — мимо."""
    a = (action or "").strip().lower()
    try:
        pos = int(position_sec) if position_sec is not None else None
    except (TypeError, ValueError):
        pos = None
    if a == "skip":
        if pos is not None and pos < 30:
            return 1
        return None  # поздний скип — неоднозначно, в обучение не берём
    if a in ("complete", "like", "replay", "seek_back"):
        return 0
    if a == "play" and pos is not None and pos >= 180:
        return 0
    return None  # короткие play / abandon — цензура, не размечаем


def features_for(*, energy: float = 0.5, valence: float = 0.5,
                 danceability: float = 0.5, tempo_bpm: float = 0.0,
                 anchor_energy: float | None = None,
                 unfamiliar_genre: bool = False,
                 artist_fatigue: float = 0.0,
                 hour: int = 12, day_of_week: int = 0,
                 plays: int = 0, early_skips: int = 0,
                 completes: int = 0, drift_level: int = 0) -> list[float]:
    """Один вектор признаков. Чистая функция — одна на трейн и инференс."""
    try:
        en = min(max(float(energy), 0.0), 1.0)
    except (TypeError, ValueError):
        en = 0.5
    try:
        va = min(max(float(valence), 0.0), 1.0)
    except (TypeError, ValueError):
        va = 0.5
    try:
        da = min(max(float(danceability), 0.0), 1.0)
    except (TypeError, ValueError):
        da = 0.5
    try:
        tn = min(max(float(tempo_bpm) / 200.0, 0.0), 1.5)
    except (TypeError, ValueError):
        tn = 0.0
    try:
        jump = abs(en - float(anchor_energy)) if anchor_energy is not None else 0.0
    except (TypeError, ValueError):
        jump = 0.0
    try:
        h = int(hour) % 24
    except (TypeError, ValueError):
        h = 12
    try:
        dow = int(day_of_week) % 7
    except (TypeError, ValueError):
        dow = 0
    try:
        pl = max(int(plays), 0)
    except (TypeError, ValueError):
        pl = 0
    try:
        es = max(int(early_skips), 0)
    except (TypeError, ValueError):
        es = 0
    try:
        co = max(int(completes), 0)
    except (TypeError, ValueError):
        co = 0
    try:
        dl = min(max(int(drift_level), 0), 3)
    except (TypeError, ValueError):
        dl = 0
    return [
        en, va, da, tn, min(max(jump, 0.0), 1.0),
        1.0 if unfamiliar_genre else 0.0,
        min(max(float(artist_fatigue), 0.0), 1.0),
        math.sin(2 * math.pi * h / 24.0), math.cos(2 * math.pi * h / 24.0),
        1.0 if (h >= 22 or h <= 5) else 0.0,
        1.0 if dow in (5, 6) else 0.0,
        math.log1p(pl),
        es / (pl + 1.0), co / (pl + 1.0), float(dl),
    ]


def _drift_of_streak(trailing_skips: int) -> int:
    if trailing_skips >= 3:
        return 3
    if trailing_skips == 2:
        return 2
    if trailing_skips == 1:
        return 1
    return 0


def build_dataset(db, limit: int = DATASET_LIMIT) -> tuple[list[list[float]], list[int]]:
    """PlayEvent -> (X, y). Глобально по всем юзерам, признаки относительные."""
    from app.db.models import PlayEvent, Track, TrackFeatures

    rows = (db.query(PlayEvent)
            .order_by(PlayEvent.created_at.desc())
            .limit(max(100, int(limit or DATASET_LIMIT))).all())
    if not rows:
        return [], []
    # батч-карты: фичи, жанр/артист, агрегаты пары
    tids = list({str(r.track_id) for r in rows if r.track_id})
    feats: dict[str, Any] = {}
    for i in range(0, len(tids), 500):
        try:
            for f in db.query(TrackFeatures).filter(
                    TrackFeatures.track_id.in_(tids[i:i + 500])).all():
                feats[str(f.track_id)] = f
        except Exception:
            pass
    meta: dict[str, Any] = {}
    for i in range(0, len(tids), 500):
        try:
            for t in db.query(Track).filter(Track.id.in_(tids[i:i + 500])).all():
                meta[str(t.id)] = t
        except Exception:
            pass
    from app.db.models import TrackStat as _TS
    stats: dict[tuple[str, str], Any] = {}
    try:
        uids = list({str(r.user_id) for r in rows if r.user_id})
        for i in range(0, len(uids), 100):
            for st in db.query(_TS).filter(_TS.user_id.in_(uids[i:i + 100])).all():
                stats[(str(st.user_id), str(st.track_id))] = st
    except Exception:
        pass
    # профили по юзерам (для unfamiliar_genre) — по одному запросу на юзера
    from app.services import taste as _taste
    pref: dict[str, set[str]] = {}
    for uid in {str(r.user_id) for r in rows if r.user_id}:
        try:
            prof = _taste.user_profile(db, uid, top_n=0)
            pref[uid] = {str(k).lower() for k in
                         (prof.get("preferredGenres") or {}).keys()}
        except Exception:
            pref[uid] = set()
    # якорь энергии и стрик скипов — по порядку событий юзера (старые -> новые)
    by_user: dict[str, list] = {}
    for r in rows:
        by_user.setdefault(str(r.user_id), []).append(r)
    X: list[list[float]] = []
    y: list[int] = []
    for uid, evs in by_user.items():
        evs.sort(key=lambda r: (r.created_at or 0, str(r.id)))
        trailing_skips = 0
        prev_energy: float | None = None
        user_genres = pref.get(uid, set())
        for r in evs:
            tid = str(r.track_id)
            lab = label_for(r.action, r.position_sec)
            f = feats.get(tid)
            t = meta.get(tid)
            st = stats.get((uid, tid))
            g = ((t.genre or "").strip().lower()) if t is not None else ""
            try:
                en = float(f.energy) if f is not None and f.energy is not None else 0.5
            except (TypeError, ValueError):
                en = 0.5
            if lab is not None:
                try:
                    va = float(f.valence) if f is not None and f.valence is not None else 0.5
                except (TypeError, ValueError):
                    va = 0.5
                try:
                    da = float(f.danceability) if f is not None and f.danceability is not None else 0.5
                except (TypeError, ValueError):
                    da = 0.5
                try:
                    bpm = float(f.tempo_bpm) if f is not None and f.tempo_bpm else 0.0
                except (TypeError, ValueError):
                    bpm = 0.0
                X.append(features_for(
                    energy=en, valence=va, danceability=da, tempo_bpm=bpm,
                    anchor_energy=prev_energy,
                    unfamiliar_genre=bool(g and g not in user_genres),
                    artist_fatigue=0.0,  # в истории нет окна 7д — честный 0
                    hour=r.hour if r.hour is not None else 12,
                    day_of_week=r.day_of_week if r.day_of_week is not None else 0,
                    plays=int(st.plays or 0) if st is not None else 0,
                    early_skips=int(st.early_skips or 0) if st is not None else 0,
                    completes=int(st.completes or 0) if st is not None else 0,
                    drift_level=_drift_of_streak(trailing_skips)))
                y.append(lab)
            # состояние для следующего события
            if (r.action or "").strip().lower() == "skip":
                trailing_skips += 1
            elif (r.action or "").strip().lower() in (
                    "play", "complete", "replay", "like", "seek_back"):
                trailing_skips = 0
            prev_energy = en
    return X, y


class _NumpyLogReg:
    """Минимальная логистическая регрессия на numpy (фолбек без sklearn).

    Тот же контракт, что у sklearn-Pipeline: predict_proba(X)->(n,2).
    Стандартизация + L2 + балансировка классов, batch gradient descent.
    """

    def __init__(self, l2: float = 1.0, iters: int = 500, lr: float = 0.5):
        self.l2 = l2
        self.iters = iters
        self.lr = lr
        self.mean_: Any = None
        self.scale_: Any = None
        self.w_: Any = None
        self.b_: float = 0.0

    def fit(self, Xa, ya):
        import numpy as _np
        X = _np.asarray(Xa, dtype=float)
        y = _np.asarray(ya, dtype=float)
        self.mean_ = X.mean(axis=0)
        self.scale_ = X.std(axis=0)
        self.scale_[self.scale_ < 1e-9] = 1.0
        Xs = (X - self.mean_) / self.scale_
        n, d = Xs.shape
        w = _np.zeros(d)
        b = 0.0
        # балансировка: позитивы весят больше
        pos = float(y.sum())
        wp = (n - pos) / max(pos, 1.0)
        sw = _np.where(y > 0.5, wp, 1.0)
        lr = self.lr / max(n, 1)
        for _ in range(self.iters):
            z = Xs.dot(w) + b
            p = 1.0 / (1.0 + _np.exp(-_np.clip(z, -30.0, 30.0)))
            err = (p - y) * sw
            g = Xs.T.dot(err) / sw.sum() + self.l2 * w / n
            gb = float(err.sum()) / sw.sum()
            w -= lr * n * g * 0.1
            b -= lr * n * gb * 0.1
        self.w_ = w
        self.b_ = float(b)
        return self

    def predict_proba(self, Xa):
        import numpy as _np
        X = _np.asarray(Xa, dtype=float)
        Xs = (X - self.mean_) / self.scale_
        z = Xs.dot(self.w_) + self.b_
        p = 1.0 / (1.0 + _np.exp(-_np.clip(z, -30.0, 30.0)))
        return _np.vstack([1.0 - p, p]).T


def _fit_pipeline(Xa, ya):
    """sklearn, если есть; иначе numpy-фолбек. Возвращает объект с predict_proba."""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        pipe = Pipeline([
            ("sc", StandardScaler()),
            ("lr", LogisticRegression(C=1.0, class_weight="balanced",
                                      max_iter=1000)),
        ])
        pipe.fit(Xa, ya)
        return pipe, "sklearn"
    except Exception:
        return _NumpyLogReg().fit(Xa, ya), "numpy"


def train(X: list[list[float]], y: list[int]) -> dict | None:
    """LogReg на готовых векторах. Мало данных/позитивов — None (эвристика)."""
    try:
        n = len(y)
        pos = sum(1 for v in y if v)
        if n < MIN_LABELED or pos < MIN_POSITIVE:
            return None
        import numpy as _np
        Xa = _np.asarray(X, dtype=float)
        ya = _np.asarray(y, dtype=int)
        pipe, backend = _fit_pipeline(Xa, ya)
        try:
            from sklearn.metrics import roc_auc_score

            auc = float(roc_auc_score(ya, pipe.predict_proba(Xa)[:, 1]))
        except Exception:
            # ручной AUC через ранги (работает и для numpy-трубы)
            order = _np.argsort(pipe.predict_proba(Xa)[:, 1])
            ranks = _np.empty(n)
            ranks[order] = _np.arange(1, n + 1)
            auc = (float(ranks[ya == 1].sum()) - pos * (pos + 1) / 2.0) / (
                pos * (n - pos)) if pos and pos < n else 0.0
        return {"pipe": pipe, "backend": backend, "n": n, "pos": pos,
                "pos_rate": round(pos / n, 4), "auc_train": round(auc, 4)}
    except Exception:
        return None


def _save_metrics(db, metrics: dict) -> None:
    try:
        from datetime import datetime

        from app.db.models import AppSetting
        payload = dict(metrics)
        payload["saved_at"] = datetime.utcnow().isoformat() + "Z"
        row = db.get(AppSetting, SETTINGS_KEY)
        if row is None:
            row = AppSetting(key=SETTINGS_KEY, value=payload)
            db.add(row)
        else:
            row.value = payload
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def get_model(db):
    """Обученная труба или None. Кэш на процесс + TTL, метрики — в AppSetting."""
    now = time.monotonic()
    hit = _cache.get("pipe")
    if hit is not None and now - float(_cache.get("at") or 0) < MODEL_TTL_SEC:
        return hit
    try:
        X, y = build_dataset(db)
        res = train(X, y)
    except Exception:
        res = None
    if res is None:
        # отрицательный кэш короткий: данные могли подрасти
        _cache["at"] = now - MODEL_TTL_SEC + 600.0
        _cache["pipe"] = None
        try:
            _save_metrics(db, {"active": False, "labeled": len(y),
                               "reason": "need %d labeled / %d positive" % (
                                   MIN_LABELED, MIN_POSITIVE)})
        except Exception:
            pass
        return None
    _cache["at"] = now
    _cache["pipe"] = res["pipe"]
    _cache["metrics"] = res
    try:
        _save_metrics(db, {"active": True, "n": res["n"], "pos": res["pos"],
                           "pos_rate": res["pos_rate"],
                           "auc_train": res["auc_train"]})
    except Exception:
        pass
    return res["pipe"]


def predict_proba(pipe, vec: list[float]) -> float | None:
    """P(ранний скип) 0..1. Любая ошибка — None (звать эвристику)."""
    try:
        import numpy as _np
        p = float(pipe.predict_proba(
            _np.asarray([vec], dtype=float))[:, 1][0])
        return min(max(p, 0.0), 1.0)
    except Exception:
        return None


def status(db) -> dict:
    """Диагностика для API/логов: активна ли модель и на чём обучена."""
    out: dict[str, Any] = {"active": _cache.get("pipe") is not None,
                           "features": FEATURES,
                           "min_labeled": MIN_LABELED,
                           "min_positive": MIN_POSITIVE}
    out.update(_cache.get("metrics") or {})
    try:
        from app.db.models import AppSetting
        row = db.get(AppSetting, SETTINGS_KEY)
        if row is not None and isinstance(row.value, dict):
            out["saved"] = row.value
    except Exception:
        pass
    return out


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["pipe"] = None
    _cache["metrics"] = {}
