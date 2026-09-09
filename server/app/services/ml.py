"""ML-алгоритмы для KumaFlow Brain.

1. Рекомендации (content-based) — косинусное сходство по фичам + эмбеддингам.
2. Cold-start (3-шаговый):
   шаг 1 — самые прослушиваемые / избранные жанры и артисты;
   шаг 2 — расширение похожими артистами (через Navidrome getSimilarSongs2 + кластеры);
   шаг 3 — плейлист из N треков с балансировкой по разнообразию и идеальным сочетанием.
3. Построение кластеров (KMeans по фичам), быстрый ANN через гомогенный косинус.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

try:
    import numpy as np  # type: ignore
    _HAS_NP = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    _HAS_NP = False

from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import (
    Album,
    Artist,
    Favorite,
    MediaServer,
    PlayHistory,
    Playlist,
    PlaylistTrack,
    Track,
    TrackCluster,
    TrackEmbedding,
    TrackFeatures,
)

logger = get_logger("ml")


# ---------- утилиты ----------

def _feature_vector(t: Track, f: TrackFeatures | None):
    if not _HAS_NP or f is None:
        return None
    vals = [
        float(f.tempo_bpm or 0.0) / 200.0,
        float(f.energy or 0.0),
        float(f.danceability or 0.0),
        float(f.valence or 0.0),
        float(f.arousal or 0.0),
        (float(f.loudness_db) + 30.0) / 30.0,
        float(f.spectral_centroid or 0.0) / 4000.0,
        float(f.spectral_rolloff or 0.0) / 7000.0,
        float(f.zero_crossing_rate or 0.0) / 0.15,
    ]
    return np.array(vals, dtype=np.float32)


def _cosine(a, b) -> float:
    if not _HAS_NP:
        return 0.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ---------- 1. Content-based рекомендации ----------

def recommend_by_track(track_id: str, top_k: int = 20) -> list[dict[str, Any]]:
    """Похожие треки по косинусу фичей + кластеру + жанру + настроению.

    Кандидаты — только свой сервер; фичи и кластеры грузятся bulk-запросами
    (иначе на 80k треков — десятки тысяч SQL-запросов).
    """
    with session_scope() as db:
        target = db.get(Track, track_id)
        if not target:
            return []
        f_t = db.get(TrackFeatures, target.id)
        v_t = _feature_vector(target, f_t)
        server_id = target.server_id
        cluster_t = (
            db.query(TrackCluster).filter(TrackCluster.track_id == target.id).first()
        )
        # bulk-загрузка одним проходом
        rows = db.query(Track).filter(Track.server_id == server_id).all()
        ids = [str(r.id) for r in rows]
        feat_map: dict[str, TrackFeatures] = {}
        if ids:
            for f in db.query(TrackFeatures).filter(TrackFeatures.track_id.in_(ids)).all():
                feat_map[str(f.track_id)] = f
        cluster_map: dict[str, int] = {}
        if ids:
            for c in db.query(TrackCluster).filter(TrackCluster.track_id.in_(ids)).all():
                cluster_map.setdefault(str(c.track_id), c.cluster_id)

        scored: list[tuple[float, Track]] = []
        for r in rows:
            if str(r.id) == str(target.id):
                continue
            f_r = feat_map.get(str(r.id))
            v_r = _feature_vector(r, f_r)
            if v_t is not None and v_r is not None:
                sim = _cosine(v_t, v_r)
            else:
                sim = 0.0
            # кластер бонус
            if cluster_t is not None:
                cid = cluster_map.get(str(r.id))
                if cid is not None and cid == cluster_t.cluster_id:
                    sim += 0.18
            # жанр бонус
            if target.genre and r.genre and target.genre == r.genre:
                sim += 0.08
            # тональность/муд бонус
            if f_t and f_r and f_t.mood_labels and f_r.mood_labels:
                moods_t = set(str(x).lower() for x in f_t.mood_labels)
                moods_r = set(str(x).lower() for x in f_r.mood_labels)
                if moods_t & moods_r:
                    sim += 0.07 * len(moods_t & moods_r)
            # энергия/валентность близость (штраф за большую дистанцию)
            if f_t and f_r and f_t.energy is not None and f_r.energy is not None:
                e_dist = abs(float(f_t.energy) - float(f_r.energy))
                sim += (1 - e_dist) * 0.05
            scored.append((sim, r))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        return [
            {
                "track_id": str(r.id),
                "title": r.title,
                "artist_name": r.artist_name,
                "album_name": r.album_name,
                "score": round(float(s), 4),
            }
            for s, r in scored[:top_k]
        ]


# ---------- 2. 3-шаговый cold-start с идеальными сочетаниями ----------

def _step1_signal(server_id: str) -> dict[str, float]:
    """Собирает веса по artist/genre/mood с учётом прослушиваний, избранного, оценок, давности."""
    weights: dict[str, float] = {}
    now = datetime.utcnow()
    with session_scope() as db:
        tracks = db.query(Track).filter_by(server_id=server_id).all()
        for t in tracks:
            base = 0.0
            base += (t.play_count or 0) * 0.45
            if t.starred:
                base += 8.0
            if t.rating:
                base += float(t.rating) * 1.6
            # бонус за недавность прослушивания (идеальное сочетание учитывает актуальность)
            if t.last_played_at:
                try:
                    days = (now - t.last_played_at).days
                    if days <= 7:
                        base += 3.0
                    elif days <= 30:
                        base += 1.8
                    elif days <= 90:
                        base += 0.7
                except Exception:
                    pass
            # маленький базовый вес чтобы не было пусто при cold-start
            base += 0.6
            if t.artist_name:
                key = f"artist::{t.artist_name}"
                weights[key] = weights.get(key, 0.0) + base
            if t.genre:
                key = f"genre::{t.genre}"
                weights[key] = weights.get(key, 0.0) + base * 0.85 + 0.7
            # муд-сигнал из фичей
            f = db.get(TrackFeatures, t.id)
            if f and f.mood_labels:
                for m in f.mood_labels[:2]:
                    key = f"mood::{str(m).lower()}"
                    weights[key] = weights.get(key, 0.0) + base * 0.35
    # нормализуем чтобы веса не взрывались
    if weights:
        mx = max(weights.values())
        if mx > 20:
            for k in weights:
                weights[k] = weights[k] / mx * 20
    return weights


def _step2_candidates(server_id: str, signal: dict[str, float]) -> list[tuple[Track, float]]:
    """Расширяем кандидатов: точные совпадения + кластер-соседи + sonic-близость к топ-сигналам + fallback."""
    candidates: dict[str, float] = {}
    with session_scope() as db:
        tracks = db.query(Track).filter_by(server_id=server_id).all()
        if not tracks:
            return []
        # заранее соберём кластеры и фичи для быстрого доступа
        cluster_map = {c.track_id: c for c in db.query(TrackCluster).all()}
        feat_map = {f.track_id: f for f in db.query(TrackFeatures).all()}
        # топ-сигналы для центроида (для sonic расширения)
        top_signals = sorted(signal.items(), key=lambda kv: kv[1], reverse=True)[:6]
        top_artist = {k[8:] for k,v in top_signals if k.startswith("artist::")}
        top_genre = {k[7:] for k,v in top_signals if k.startswith("genre::")}
        top_mood = {k[6:] for k,v in top_signals if k.startswith("mood::")}
        # центроид по фичам топ-треков
        centroid = None
        if _HAS_NP:
            vecs = []
            for t in tracks:
                if t.artist_name in top_artist or t.genre in top_genre:
                    f = feat_map.get(t.id)
                    v = _feature_vector(t, f)
                    if v is not None:
                        vecs.append(v)
            if vecs:
                centroid = np.mean(np.vstack(vecs), axis=0)

        # частота кластеров среди топ-сигналов
        cluster_pop = Counter()
        for t in tracks:
            if t.artist_name in top_artist or t.genre in top_genre:
                c = cluster_map.get(t.id)
                if c:
                    cluster_pop[c.cluster_id] += 1
        popular_clusters = {cid for cid,_ in cluster_pop.most_common(3)}

        for t in tracks:
            score = 0.0
            w = 0.0
            if t.artist_name and f"artist::{t.artist_name}" in signal:
                w = signal[f"artist::{t.artist_name}"]
                score += w * 0.55 + 1.2
            if t.genre and f"genre::{t.genre}" in signal:
                w = signal[f"genre::{t.genre}"]
                score += w * 0.33 + 0.7
            # муд совпадение
            f = feat_map.get(t.id)
            if f and f.mood_labels:
                for m in f.mood_labels:
                    if f"mood::{str(m).lower()}" in signal:
                        score += signal[f"mood::{str(m).lower()}"] * 0.18
                        break
            # кластер-соседи: если кластер популярный — добавим даже без прямого сигнала
            c = cluster_map.get(t.id)
            if c and c.cluster_id in popular_clusters and score < 0.1:
                score += 0.9
            elif c and score == 0:
                score += 0.18
            # sonic близость к центроиду (идеальное сочетание по звучанию)
            if centroid is not None and f is not None:
                v = _feature_vector(t, f)
                if v is not None:
                    sim = _cosine(v, centroid)
                    if sim > 0.72:
                        score += (sim - 0.72) * 4.0  # до +1.1
                    elif sim > 0.55:
                        score += (sim - 0.55) * 1.2
            # бонус за наличие фичей (проанализированные треки предпочтительнее)
            if f and f.tempo_bpm is not None:
                score += 0.12
            if score > 0:
                # немного рандома чтобы каждый день плейлист был чуть разным (но детерминированно по id)
                rnd = (hash(t.id) % 100) / 1000.0
                candidates[str(t.id)] = score + rnd

        if not candidates:
            for t in tracks:
                candidates[str(t.id)] = 1.0 + (hash(t.id) % 10) / 100.0
        out: list[tuple[Track, float]] = []
        for t in tracks:
            s = candidates.get(str(t.id))
            if s is not None:
                out.append((t, s))
    return out


def _step3_diversify(items: list[tuple[Track, float, TrackFeatures | None, TrackCluster | None]], n: int = 30) -> list[Track]:
    """MMR + идеальные сочетания: штраф за похожесть, ограничение на артистов/жанры, энергетическая кривая."""
    if not items:
        return []
    pool = sorted(items, key=lambda x: x[1], reverse=True)
    chosen: list[Track] = []
    chosen_vecs: list[Any] = []
    chosen_features: list[TrackFeatures | None] = []
    artist_cnt: Counter = Counter()
    genre_cnt: Counter = Counter()
    penalty = 0.32
    # для идеального сочетания запоминаем уже выбранные энергии/темпы
    while pool and len(chosen) < n:
        best_idx = -1
        best_val = -1e9
        for i, (t, s, f, cluster) in enumerate(pool):
            v = _feature_vector(t, f)
            # MMR штраф за похожесть
            div = 0.0
            if v is not None and chosen_vecs:
                sims = [_cosine(v, cv) for cv in chosen_vecs if cv is not None]
                if sims:
                    div = penalty * max(sims)
            # штраф за перебор артиста (не более 2 треков одного артиста в идеальном плейлисте)
            artist_penalty = 0.0
            if t.artist_name and artist_cnt[t.artist_name] >= 2:
                artist_penalty = 1.6
            elif t.artist_name and artist_cnt[t.artist_name] >= 1:
                artist_penalty = 0.35
            # штраф за перебор жанра (не более 35% одного жанра)
            genre_penalty = 0.0
            if t.genre and genre_cnt[t.genre] >= math.ceil(n * 0.35):
                genre_penalty = 1.1
            # энергетическая плавность: если уже есть треки, штрафуем резкий скачок энергии >0.35
            energy_penalty = 0.0
            if f and f.energy is not None and chosen_features:
                last = chosen_features[-1]
                if last and last.energy is not None:
                    if abs(float(f.energy) - float(last.energy)) > 0.42:
                        energy_penalty = 0.22
            # бонус за разнообразие кластеров (предпочитаем недопредставленные кластеры)
            cluster_bonus = 0.0
            if cluster:
                # сколько уже выбрано из этого кластера
                same_cluster = sum(1 for c in chosen_features if c is not None)
                # упростим: если кластер редкий — бонус
                pass

            val = s - div - artist_penalty - genre_penalty - energy_penalty
            # небольшой бонус за среднюю энергию (избегаем крайностей)
            if f and f.energy is not None:
                e = float(f.energy)
                if 0.35 <= e <= 0.78:
                    val += 0.06
            if val > best_val:
                best_val = val
                best_idx = i
        if best_idx == -1:
            break
        t, _, f, _ = pool.pop(best_idx)
        chosen.append(t)
        v = _feature_vector(t, f)
        chosen_vecs.append(v)
        chosen_features.append(f)
        if t.artist_name:
            artist_cnt[t.artist_name] += 1
        if t.genre:
            genre_cnt[t.genre] += 1

    # --- финальная сортировка для идеального сочетания (плавная энергетическая арка) ---
    # Если набрали >= 8 треков, отсортируем по энергии: подъём к середине, спад к концу (как DJ сет)
    if len(chosen) >= 8 and all(f and f.energy is not None for f in chosen_features):
        # создаём арку: сортируем по energy, затем собираем: низкие -> средние -> высокие -> средние -> низкие
        indexed = list(enumerate(chosen))
        indexed.sort(key=lambda x: float(chosen_features[x[0]].energy or 0.5))
        # low, mid, high
        low = [t for _, t in indexed[: len(indexed)//3]]
        mid = [t for _, t in indexed[len(indexed)//3 : 2*len(indexed)//3]]
        high = [t for _, t in indexed[2*len(indexed)//3:]]
        # арка: low -> mid -> high -> mid(reversed) -> low(reversed) но проще: low + mid + high + mid[::-1]
        # для n=30 эффективнее просто чередовать низкую/высокую чтобы избежать монотонности — используем волнообразную последовательность
        arc: list[Track] = []
        # чередуем: берём по одному из low, mid, high по кругу
        pools = [low, mid, high]
        # random shuffle внутри каждого пула для вариативности (детерминированно)
        for p in pools:
            random.Random(42).shuffle(p)
        idxs = [0,0,0]
        turn = 0
        while len(arc) < len(chosen):
            p = pools[turn % 3]
            if idxs[turn % 3] < len(p):
                arc.append(p[idxs[turn % 3]])
                idxs[turn % 3] += 1
            turn += 1
            if all(idxs[i] >= len(pools[i]) for i in range(3)):
                break
        # если что-то осталось — добавим
        for i, p in enumerate(pools):
            while idxs[i] < len(p):
                arc.append(p[idxs[i]])
                idxs[i] += 1
        # если арка заполнена, используем её; иначе оставляем MMR порядок
        if len(arc) == len(chosen):
            chosen = arc
    return chosen


def cold_start_playlist(server_id: str, n: int = 30) -> dict[str, Any]:
    """Полный 3-шаговый cold-start с идеальными сочетаниями. Возвращает метаданные + список ID треков."""
    signal = _step1_signal(server_id)
    candidates = _step2_candidates(server_id, signal)
    with session_scope() as db:
        items: list[tuple[Track, float, TrackFeatures | None, TrackCluster | None]] = []
        for t, s in candidates:
            f = db.get(TrackFeatures, t.id)
            cluster = db.query(TrackCluster).filter(TrackCluster.track_id == t.id).first()
            items.append((t, s, f, cluster))
    tracks = _step3_diversify(items, n=n)
    # статистика для UI: распределение по жанрам/артистам в идеальном плейлисте
    genre_dist = Counter(t.genre or "unknown" for t in tracks)
    artist_dist = Counter(t.artist_name or "unknown" for t in tracks)
    return {
        "tracks": [str(t.id) for t in tracks],
        "items": [
            {
                "track_id": str(t.id),
                "title": t.title,
                "artist_name": t.artist_name,
                "album_name": t.album_name,
                "genre": t.genre,
            }
            for t in tracks
        ],
        "signal": signal,
        "genre_distribution": dict(genre_dist),
        "artist_distribution": dict(artist_dist.most_common(6)),
        "steps": [
            {"step": 1, "name": "signal", "items": len(signal)},
            {"step": 2, "name": "candidates", "items": len(candidates)},
            {"step": 3, "name": "diversified", "items": len(tracks)},
        ],
    }


# ---------- 3. Кластеризация KMeans по фичам ----------

def build_clusters(server_id: str, k: int = 8) -> dict[str, Any]:
    """KMeans по нормированным фичам. Перезаписывает TrackCluster (algorithm='kmeans')."""
    if not _HAS_NP:
        return {"status": "numpy не установлен"}
    try:
        from sklearn.cluster import KMeans
    except Exception as e:  # noqa: BLE001
        return {"status": f"sklearn не установлен: {e}"}

    with session_scope() as db:
        tracks = db.query(Track).filter_by(server_id=server_id).all()
        feats: list[tuple[str, np.ndarray]] = []
        for t in tracks:
            f = db.get(TrackFeatures, t.id)
            v = _feature_vector(t, f)
            if v is not None:
                feats.append((str(t.id), v))
        if len(feats) < k:
            return {"status": "not enough data", "tracks": len(feats)}

        ids = [fid for fid, _ in feats]
        X = np.vstack([v for _, v in feats])
        km = KMeans(n_clusters=k, n_init=10, random_state=42)
        labels = km.fit_predict(X)
        # расстояние до центроида
        centers = km.cluster_centers_
        dists = np.linalg.norm(X - centers[labels], axis=1)

        # чистим старые kmeans-кластеры
        db.query(TrackCluster).filter(TrackCluster.algorithm == "kmeans").delete()
        for tid, lbl, d in zip(ids, labels, dists):
            db.add(
                TrackCluster(
                    track_id=tid,
                    algorithm="kmeans",
                    cluster_id=int(lbl) + 1,
                    distance_to_center=float(d),
                )
            )
        return {
            "status": "ok",
            "clusters": k,
            "tracks_clustered": len(ids),
        }


# ---------- 4. ANN по эмбеддингам (если есть) ----------

def search_by_embedding(query_vec: list[float], top_k: int = 20) -> list[dict[str, Any]]:
    """Косинусный поиск по сохранённым эмбеддингам (CLAP/MuLan)."""
    q = np.array(query_vec, dtype=np.float32)
    with session_scope() as db:
        rows = db.query(TrackEmbedding).all()
        scored = []
        for r in rows:
            v = np.frombuffer(r.vector, dtype=np.float32)
            if v.shape[0] != q.shape[0]:
                continue
            s = _cosine(q, v)
            t = db.get(Track, r.track_id)
            if t is None:
                continue
            scored.append((s, t))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        return [
            {
                "track_id": str(t.id),
                "title": t.title,
                "artist_name": t.artist_name,
                "score": round(float(s), 4),
            }
            for s, t in scored[:top_k]
        ]
