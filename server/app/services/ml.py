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
    try:
        vals = [
            float(f.tempo_bpm if f.tempo_bpm is not None else 0.0) / 200.0,
            float(f.energy if f.energy is not None else 0.0),
            float(f.danceability if f.danceability is not None else 0.0),
            float(f.valence if f.valence is not None else 0.0),
            float(f.arousal if f.arousal is not None else 0.0),
            (float(f.loudness_db if f.loudness_db is not None else -20.0) + 30.0) / 30.0,
            float(f.spectral_centroid if f.spectral_centroid is not None else 0.0) / 4000.0,
            float(f.spectral_rolloff if f.spectral_rolloff is not None else 0.0) / 7000.0,
            float(f.zero_crossing_rate if f.zero_crossing_rate is not None else 0.0) / 0.15,
        ]
    except (TypeError, ValueError):
        return None
    arr = np.array(vals, dtype=np.float32)
    # защита от NaN/inf в фичах (иначе KMeans падает с float() ... not 'NoneType'/nan)
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
    return arr


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
        cluster_t = (
            db.query(TrackCluster).filter(TrackCluster.track_id == target.id).first()
        )
        # лёгкие строки (без ORM-нагрузки — библиотека 150k+), фичи/кластеры —
        # маленькие таблицы целиком (никаких .in_ на 150k id)
        rows = _load_track_rows(db)
        feat_map = _load_feat_map(db)
        cluster_map_all = _load_cluster_map(db)
        cluster_map = cluster_map_all

        scored: list[tuple] = []
        no_features = v_t is None
        for r in rows:
            if str(r.id) == str(target.id):
                continue
            f_r = feat_map.get(str(r.id))
            v_r = _feature_vector(r, f_r)
            if v_t is not None and v_r is not None:
                sim = _cosine(v_t, v_r)
            elif no_features:
                # У цели нет фич (не проанализирован): честный мета-скоринг
                # вместо ничьи 0.08 за жанр. Тот же артист — максимально похоже.
                sim = 0.0
                if target.artist_name and r.artist_name:
                    if r.artist_name == target.artist_name:
                        sim += 0.55
                    else:
                        # пересечение участников ("A/B" ~ "A")
                        ta = {p.strip().lower() for p in str(target.artist_name).split("/") if p.strip()}
                        ra = {p.strip().lower() for p in str(r.artist_name).split("/") if p.strip()}
                        if ta & ra:
                            sim += 0.35
                if target.album_name and r.album_name and r.album_name == target.album_name:
                    sim += 0.30
                if target.genre and r.genre and target.genre == r.genre:
                    sim += 0.08
                sim += min(r.play_count or 0, 100) * 0.002  # до +0.2 за популярность
                if r.starred:
                    sim += 0.10
                if r.rating:
                    try:
                        sim += float(r.rating) * 0.015
                    except (TypeError, ValueError):
                        pass
                if target.year and r.year:
                    try:
                        sim -= min(abs(int(target.year) - int(r.year)) * 0.004, 0.08)
                    except (TypeError, ValueError):
                        pass
            else:
                sim = 0.0
            # тот же исполнитель — бонус и в sonic-режиме
            if target.artist_name and r.artist_name and r.artist_name == target.artist_name:
                sim += 0.25
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
            scored.append((sim, r.play_count or 0, r))
        # тайбрейк по популярности — никаких «все по 8%»
        scored.sort(key=lambda kv: (kv[0], kv[1]), reverse=True)
        return [
            {
                "track_id": str(r.id),
                "title": r.title,
                "artist_name": r.artist_name,
                "album_name": r.album_name,
                "score": round(float(s), 4),
            }
            for s, _, r in scored[:top_k]
        ]


# ---------- 2. 3-шаговый cold-start с идеальными сочетаниями ----------
#
# Важно для больших библиотек (150k+ треков): НИКАКИХ db.query(Track).all()
# (полные ORM-объекты ×3 прохода = сотни МБ и обрыв соединения) и никаких
# .in_(ids) на 150k id (лимиты параметров БД). Один проход лёгкими колонками,
# фичи/кластеры — маленькими таблицами целиком.

def _load_track_rows(db):
    """Лёгкие строки треков: только колонки, нужные скорингу (атрибутный доступ)."""
    return db.query(
        Track.id,
        Track.title,
        Track.artist_name,
        Track.album_name,
        Track.genre,
        Track.year,
        Track.play_count,
        Track.starred,
        Track.rating,
        Track.last_played_at,
    ).all()


def _load_feat_map(db) -> dict[str, TrackFeatures]:
    try:
        return {str(f.track_id): f for f in db.query(TrackFeatures).all()}
    except Exception:
        return {}


def _load_cluster_map(db) -> dict[str, int]:
    """track_id -> cluster_id (предпочитаем kmeans). Маленькая таблица — целиком."""
    try:
        rows = db.query(TrackCluster).all()
    except Exception:
        return {}
    out: dict[str, int] = {}
    for c in rows:
        key = str(c.track_id)
        if key not in out or getattr(c, "algorithm", "") == "kmeans":
            out[key] = c.cluster_id
    return out


def _step1_signal(rows, feat_map: dict, hist_cnt: dict | None = None,
                  fav_set: set | None = None) -> dict[str, float]:
    """Собирает веса по artist/genre/mood с учётом прослушиваний, избранного, оценок, давности.

    Чистая функция от лёгких строк (без SQL внутри — один проход по библиотеке).
    hist_cnt/fav_set: per-user персонализация через PlayHistory/Favorite,
    иначе глобально по play_count/starred (как раньше).
    Порт: mobile ml_store.seedTaste + MLService 0.6 floor.
    """
    weights: dict[str, float] = {}
    now = datetime.utcnow()
    per_user = bool(hist_cnt or fav_set)
    hist_cnt = hist_cnt or {}
    fav_set = fav_set or set()
    for t in rows:
        tid = str(t.id)
        base = 0.0
        if per_user:
            # per-user: PlayHistory + Favorite как в mobile ml_store
            base += hist_cnt.get(tid, 0) * 0.9  # play
            if tid in fav_set:
                base += 10.0  # like
        else:
            base += (t.play_count or 0) * 0.45
            if t.starred:
                base += 8.0
            if t.rating:
                try:
                    base += float(t.rating) * 1.6
                except (TypeError, ValueError):
                    pass
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
        # маленький базовый вес чтобы не было пусто при cold-start (mobile 0.6 floor)
        base += 0.6
        if t.artist_name:
            key = f"artist::{t.artist_name}"
            weights[key] = weights.get(key, 0.0) + base
        if t.genre:
            key = f"genre::{t.genre}"
            weights[key] = weights.get(key, 0.0) + base * 0.85 + 0.7
        f = feat_map.get(tid)
        if f and f.mood_labels:
            for m in f.mood_labels[:2]:
                key = f"mood::{str(m).lower()}"
                weights[key] = weights.get(key, 0.0) + base * 0.35
    if weights:
        mx = max(weights.values())
        if mx > 20:
            for k in weights:
                weights[k] = weights[k] / mx * 20
    return weights


def _novelty_factor(last_played, now: datetime | None = None) -> float:
    """Новизна как mobile calculateNoveltyScore: days/30, clamp 0..1.

    Недавнее → понижающий коэффициент (волна не гоняет вчерашнее по кругу),
    неслыханное → 1.0. Мягко: минимум 0.25, чтобы любимое не исчезало совсем.
    """
    if not last_played:
        return 1.0
    try:
        days = ((now or datetime.utcnow()) - last_played).days
    except Exception:
        return 1.0
    if days < 0:
        return 1.0
    return 0.25 + 0.75 * min(days / 30.0, 1.0)


def _step2_candidates(rows, signal: dict[str, float],
                      feat_map: dict, cluster_map: dict[str, int],
                      novelty_map: dict[str, Any] | None = None,
                      seed_vec=None, seed_cluster: int | None = None,
                      mood_filter: str | None = None) -> list[tuple]:
    """Расширяем кандидатов: точные совпадения + кластер-соседи + sonic-близость к топ-сигналам + fallback.

    Чистая функция от лёгких строк (без SQL внутри).
    novelty_map: track_id -> last_played (новизна mobile: недавнее понижаем).
    seed_vec/seed_cluster: волна от трека (как mobile wave от текущей песни).
    mood_filter: оставить только треки с этим настроением (волна по настроению).
    """
    candidates: dict[str, float] = {}
    if not rows:
        return []
    # топ-сигналы для центроида (для sonic расширения)
    top_signals = sorted(signal.items(), key=lambda kv: kv[1], reverse=True)[:6]
    top_artist = {k[8:] for k, v in top_signals if k.startswith("artist::")}
    top_genre = {k[7:] for k, v in top_signals if k.startswith("genre::")}
    # центроид по фичам топ-треков
    centroid = None
    if _HAS_NP:
        vecs = []
        for t in rows:
            if t.artist_name in top_artist or t.genre in top_genre:
                f = feat_map.get(str(t.id))
                v = _feature_vector(t, f)
                if v is not None:
                    vecs.append(v)
        if vecs:
            centroid = np.mean(np.vstack(vecs), axis=0)

    # частота кластеров среди топ-сигналов
    cluster_pop = Counter()
    for t in rows:
        if t.artist_name in top_artist or t.genre in top_genre:
            cid = cluster_map.get(str(t.id))
            if cid is not None:
                cluster_pop[cid] += 1
    popular_clusters = {cid for cid, _ in cluster_pop.most_common(3)}

    for t in rows:
        tid = str(t.id)
        # фильтр волны по настроению
        f = feat_map.get(tid)
        if mood_filter:
            moods = {str(m).lower() for m in (f.mood_labels or [])} if f else set()
            if mood_filter.lower() not in moods:
                continue
        score = 0.0
        w = 0.0
        if t.artist_name and f"artist::{t.artist_name}" in signal:
            w = signal[f"artist::{t.artist_name}"]
            score += w * 0.55 + 1.2
        if t.genre and f"genre::{t.genre}" in signal:
            w = signal[f"genre::{t.genre}"]
            score += w * 0.33 + 0.7
        # муд совпадение
        if f and f.mood_labels:
            for m in f.mood_labels:
                if f"mood::{str(m).lower()}" in signal:
                    score += signal[f"mood::{str(m).lower()}"] * 0.18
                    break
        # кластер-соседи: если кластер популярный — добавим даже без прямого сигнала
        cid = cluster_map.get(tid)
        if cid is not None and cid in popular_clusters and score < 0.1:
            score += 0.9
        elif cid is not None and score == 0:
            score += 0.18
        # волна от сида: sonic-близость к треку + тот же кластер
        if seed_vec is not None and f is not None:
            v = _feature_vector(t, f)
            if v is not None:
                sim = _cosine(v, seed_vec)
                if sim > 0.5:
                    score += (sim - 0.5) * 3.0
            if seed_cluster is not None and cid is not None and cid == seed_cluster:
                score += 0.8
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
        # новизна mobile: недавнее крутим реже
        if novelty_map is not None:
            score *= _novelty_factor(novelty_map.get(tid))
        if score > 0:
            # немного рандома чтобы каждый день плейлист был чуть разным (но детерминированно по id)
            rnd = (hash(tid) % 100) / 1000.0
            candidates[tid] = score + rnd

    if not candidates:
        for t in rows:
            tid = str(t.id)
            candidates[tid] = 1.0 + (hash(tid) % 10) / 100.0
    by_id = {str(t.id): t for t in rows}
    return [(by_id[tid], s) for tid, s in candidates.items() if tid in by_id]


def _step3_diversify(items: list[tuple], n: int = 30) -> list:
    """MMR + идеальные сочетания: штраф за похожесть, ограничение на артистов/жанры, энергетическая кривая.

    На большой библиотеке MMR по всем трекам — O(n²): сначала урезаем пул
    до топ-2000 по скору (качество не страдает, скорость — в разы).
    items: (track_row, score, features|None, cluster_id|None) — работает
    и с ORM-треками, и с лёгкими строками (нужны .id/.artist_name/.genre).
    """
    if not items:
        return []
    pool = sorted(items, key=lambda x: x[1], reverse=True)
    if len(pool) > 2000:
        # держим топ по скору + небольшую случайную примесь для разнообразия
        import random as _rnd

        head, tail = pool[:1500], pool[1500:]
        _rnd.Random(42).shuffle(tail)
        pool = head + tail[:500]
        pool.sort(key=lambda x: x[1], reverse=True)
    chosen: list = []
    chosen_vecs: list[Any] = []
    chosen_features: list = []
    artist_cnt: Counter = Counter()
    genre_cnt: Counter = Counter()
    penalty = 0.32
    # вектора строим ОДИН раз (на 2000 пуле × 30 итераций иначе 60k построений)
    pool_vecs: list[Any] = [_feature_vector(t, f) for t, _, f, _ in pool]
    # для идеального сочетания запоминаем уже выбранные энергии/темпы
    while pool and len(chosen) < n:
        best_idx = -1
        best_val = -1e9
        for i, (t, s, f, cluster) in enumerate(pool):
            v = pool_vecs[i]
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
        v = pool_vecs.pop(best_idx)
        chosen.append(t)
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
        arc: list = []
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


def cold_start_playlist(server_id: str, n: int = 30, user_id: str | None = None,
                        seed_track_id: str | None = None, mood: str | None = None,
                        novelty: bool = True) -> dict[str, Any]:
    """Полный 3-шаговый cold-start с идеальными сочетаниями. Возвращает метаданные + список ID треков.

    user_id: если указан — персонализация (копируем mobile cold_start), иначе глобально.
    Дизлайки и баны артиста исключаются (правила mobile). Новизна mobile:
    недавнее крутится реже. seed_track_id — волна от трека, mood — волна
    по настроению (как «Моя волна» Яндекс Музыки).
    Один проход лёгкими строками (без ORM-нагрузки): безопасно на 150k+ треков.
    """
    with session_scope() as db:
        rows = _load_track_rows(db)
        feat_map = _load_feat_map(db)
        cluster_map = _load_cluster_map(db)
        hist_cnt: dict[str, int] = {}
        fav_set: set[str] = set()
        disliked: set[str] = set()
        banned: set[str] = set()
        novelty_map: dict[str, Any] = {}
        if user_id:
            from app.db.models import ArtistBan as _AB
            from app.db.models import TrackDislike as _TD
            from app.db.models import TrackStat as _TS

            try:
                for tid, when in db.query(PlayHistory.track_id, PlayHistory.played_at).filter(
                        PlayHistory.user_id == user_id).all():
                    tid = str(tid)
                    hist_cnt[tid] = hist_cnt.get(tid, 0) + 1
                    if when and (tid not in novelty_map or when > novelty_map[tid]):
                        novelty_map[tid] = when
                # статы с мобилы — в тот же вес прослушиваний/новизны
                for st in db.query(_TS).filter(_TS.user_id == user_id).all():
                    tid = str(st.track_id)
                    hist_cnt[tid] = hist_cnt.get(tid, 0) + (st.plays or 0)
                    if st.last_played and (tid not in novelty_map or st.last_played > novelty_map[tid]):
                        novelty_map[tid] = st.last_played
                fav_set = {str(tid) for (tid,) in db.query(Favorite.track_id).filter(
                    Favorite.user_id == user_id).all()}
                disliked = {str(r.track_id) for r in
                            db.query(_TD).filter(_TD.user_id == user_id).all()}
                banned = {str(r.artist_name) for r in
                          db.query(_AB).filter(_AB.user_id == user_id).all()}
            except Exception:
                pass
        # исключения mobile: дизлайки и забаненные артисты не кандидаты
        excluded_dis = excluded_ban = 0
        if disliked or banned:
            kept = []
            for t in rows:
                tid = str(t.id)
                if tid in disliked:
                    excluded_dis += 1
                    continue
                if t.artist_name and t.artist_name in banned:
                    excluded_ban += 1
                    continue
                kept.append(t)
            rows = kept
        if not novelty:
            novelty_map = {}
        elif not novelty_map:
            # глобальная новизна по last_played_at из строк
            novelty_map = {str(t.id): t.last_played_at for t in rows if t.last_played_at}
        # сид волны
        seed_vec = None
        seed_cluster = None
        if seed_track_id:
            f = feat_map.get(str(seed_track_id))
            if f is not None:
                seed_vec = _feature_vector(None, f)  # type: ignore[arg-type]
            seed_cluster = cluster_map.get(str(seed_track_id))
        signal = _step1_signal(rows, feat_map, hist_cnt, fav_set)
        candidates = _step2_candidates(rows, signal, feat_map, cluster_map,
                                       novelty_map=novelty_map, seed_vec=seed_vec,
                                       seed_cluster=seed_cluster, mood_filter=mood)
        items: list[tuple] = []
        for t, s in candidates:
            tid = str(t.id)
            items.append((t, s, feat_map.get(tid), cluster_map.get(tid)))
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
        "excluded_disliked": excluded_dis,
        "excluded_banned": excluded_ban,
        "steps": [
            {"step": 1, "name": "signal", "items": len(signal)},
            {"step": 2, "name": "candidates", "items": len(candidates)},
            {"step": 3, "name": "diversified", "items": len(tracks)},
        ],
    }


# ---------- 3. Кластеризация KMeans по фичам ----------

def build_clusters(server_id: str | None = None, k: int = 8) -> dict[str, Any]:
    """KMeans по нормированным фичам. Перезаписывает TrackCluster (algorithm='kmeans').

    server_id игнорируется: кластеризуем всю базу (Navidrome + диск).
    """
    if not _HAS_NP:
        return {"status": "numpy не установлен"}
    try:
        from sklearn.cluster import KMeans
    except Exception as e:  # noqa: BLE001
        return {"status": f"sklearn не установлен: {e}"}

    with session_scope() as db:
        # bulk-загрузка: только треки с фичами (иначе на 150k+ треков — N+1 запросов
        # и минуты ожидания; раньше тут был db.get() в цикле по всем трекам).
        feat_rows: list[TrackFeatures] = db.query(TrackFeatures).all()
        if len(feat_rows) < k:
            return {"status": "not enough data", "tracks": len(feat_rows)}
        feats: list[tuple[str, np.ndarray]] = []
        for f in feat_rows:
            # нужен существующий трек (фичи-сироты пропускаем тихо)
            t = db.get(Track, f.track_id)
            v = _feature_vector(t, f) if t is not None else _feature_vector(None, f)  # type: ignore[arg-type]
            if v is not None:
                feats.append((str(f.track_id), v))
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
    """Косинусный поиск по сохранённым эмбеддингам (CLAP/MuLan).

    Если установлен voyager — использует HNSW-индекс в памяти, иначе
    brute-force через numpy (для CPU/малых библиотек достаточно).
    """
    if not _HAS_NP:
        return []
    q = np.array(query_vec, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0:
        return []
    q = q / q_norm
    with session_scope() as db:
        rows = db.query(TrackEmbedding).all()
        if not rows:
            return []
        # попытка voyager (опционально)
        try:
            import voyager  # type: ignore

            dim = len(query_vec)
            # voyager требует одинаковый dim
            filtered = [(r, np.frombuffer(r.vector, dtype=np.float32)) for r in rows if np.frombuffer(r.vector, dtype=np.float32).shape[0] == dim]
            if filtered:
                vecs = np.vstack([v / (float(np.linalg.norm(v)) or 1.0) for _, v in filtered])
                index = voyager.Index.Space.Cosine(dim)
                index.add_items(vecs)
                # voyager 2.x: query
                ids, dists = index.query(q, k=min(top_k, len(filtered)))
                id_map = {i: r for i, (r, _) in enumerate(filtered)}
                out = []
                for idx, dist in zip(ids, dists):
                    r = id_map.get(int(idx))
                    if r is None:
                        continue
                    t = db.get(Track, r.track_id)
                    if t is None:
                        continue
                    # voyager возвращает косинусное расстояние (1 - cosine)
                    score = 1.0 - float(dist)
                    out.append({"track_id": str(t.id), "title": t.title, "artist_name": t.artist_name, "score": round(score, 4)})
                return out
        except Exception:
            pass
        scored = []
        for r in rows:
            v = np.frombuffer(r.vector, dtype=np.float32)
            if v.shape[0] != q.shape[0]:
                continue
            # нормализуем заранее
            vn = float(np.linalg.norm(v))
            if vn == 0:
                continue
            v = v / vn
            s = float(np.dot(q, v))
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
