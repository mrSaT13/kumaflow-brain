"""ПОТОК волны: тембр и k-NN по эмбеддингам.

Этап 3, часть 2. Раньше волна знала только ЦЕНТРОИД (среднее) и муд-вектор:
трек сравнивался со «средней температурой» сессии, а материал трека (тембр) и
похожесть на КОНКРЕТНЫЙ играющий трек в скоринге не участвовали вообще.

Два сигнала, которых здесь не было:

1) ТЕМБР. `TrackFeatures.mfcc_summary` (13 средних MFCC) и `chroma_summary`
   (12-D профиль гармоник) считаются анализатором и лежат в базе, но
   рекомендатель их не читал — только дедуп по фингерпринту. Тембр — это
   «материал» трека: ударные/струнные/синты/вокал. Похожий тембр = меньше
   шока на стыке пачек и больше «того же звука» в пачке.

2) K-NN ПО ЭМБЕДДИНГАМ. Центроид — среднее: трек, похожий на ОДИН конкретный
   трек сессии, но не на её среднее, получал низкий балл и в пачку не
   попадал. k-NN смотрит на конкретные треки: среднее top-k косинусов.
   Это СОДЕРЖАНИЕ потока, а дуга (`wave.order_by_wave_arc`) — его ФОРМА.
   Правило из заметок сессии не нарушается: дуга даёт форму, k-NN — что
   играть.

Модуль чистый: без БД и без побочных эффектов. numpy желателен (быстрый
k-NN на 512-D векторах CLAP), но не обязателен: тембр считается на чистом
Python, k-NN без numpy работает на укороченном числе опорных треков.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

# Сколько опорных треков (сиды + сессия) берём для k-NN. Больше — точнее,
# но k-NN дорожает: опора умножается на размер пула.
MAX_REFS = 24
# top-k усредняемых соседей. 1 = «похож на конкретный трек», 3 = мягче.
TOP_K = 3


def _as_list(v: Any) -> list[float]:
    if isinstance(v, dict):
        v = v.get("mean")
    if not isinstance(v, (list, tuple)):
        return []
    out: list[float] = []
    for x in v:
        try:
            f = float(x)
        except (TypeError, ValueError):
            return []
        if f != f or f in (float("inf"), float("-inf")):  # NaN/inf
            return []
        out.append(f)
    return out


def chroma_profile(feat: Any) -> list[float] | None:
    """12-D профиль гармоник (лады), L2-нормированный. None = нет данных.

    Гармоника — самая «слышимая» часть тембра: ударные дают ровный фон,
    струнные/вокал — свои лады.
    """
    try:
        raw = _as_list(getattr(feat, "chroma_summary", None))
    except Exception:
        return None
    if len(raw) < 12:
        return None
    v = raw[:12]
    n = sum(x * x for x in v) ** 0.5
    if n <= 0:
        return None
    return [x / n for x in v]


def mfcc_profile(feat: Any) -> list[float] | None:
    """12-D форма тембра: MFCC c1..c12, z-нормированные внутри трека.

    c0 (общая энергия) выкинут: это громкость, а не тембр. z-норма внутри
    трека оставляет только ФОРМУ коэффициентов (как в dedup_fingerprint),
    поэтому векторы разных треков сравнимы между собой.
    """
    try:
        raw = _as_list(getattr(feat, "mfcc_summary", None))
    except Exception:
        return None
    if len(raw) < 13:
        return None
    v = raw[1:13]
    mean = sum(v) / len(v)
    var = sum((x - mean) ** 2 for x in v) / len(v)
    std = var ** 0.5
    if std <= 1e-9:
        return None
    return [(x - mean) / std for x in v]


def timbre_vector(feat: Any) -> dict[str, list[float]] | None:
    """Дескриптор тембра трека: {"chroma": [...], "mfcc": [...]}.

    None, если у трека нет ни анализа, ни хотя бы одной из частей.
    """
    ch = chroma_profile(feat)
    mf = mfcc_profile(feat)
    if not ch and not mf:
        return None
    out: dict[str, list[float]] = {}
    if ch:
        out["chroma"] = ch
    if mf:
        out["mfcc"] = mf
    return out or None


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _norm(a: Sequence[float]) -> float:
    return sum(float(x) * float(x) for x in a) ** 0.5


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    """Косинус без numpy (для коротких векторов тембра)."""
    if not a or not b:
        return 0.0
    n = len(a)
    if len(b) != n:
        return 0.0
    na, nb = _norm(a), _norm(b)
    if na <= 0 or nb <= 0:
        return 0.0
    return _dot(a, b) / (na * nb)


def timbre_similarity(a: dict | None, b: dict | None) -> float:
    """Похожесть тембра 0..1: половина — гармоника, половина — форма.

    Гармоника косинусом (профили неотрицательны), форма — косинусом с
    отсечкой снизу: противоположная форма = 0, а не «анти-похоже».
    """
    if not a or not b:
        return 0.0
    s = 0.0
    w = 0.0
    ca, cb = a.get("chroma"), b.get("chroma")
    if ca and cb:
        s += 0.5 * max(0.0, cosine(ca, cb))
        w += 0.5
    ma, mb = a.get("mfcc"), b.get("mfcc")
    if ma and mb:
        s += 0.5 * max(0.0, cosine(ma, mb))
        w += 0.5
    if w <= 0:
        return 0.0
    return max(0.0, min(1.0, s / w))


def timbre_centroid(vectors: Iterable[dict | None]) -> dict[str, list[float]] | None:
    """Средний тембр по набору треков (сиды / сессия)."""
    chromas: list[list[float]] = []
    mfccs: list[list[float]] = []
    for v in vectors:
        if not v:
            continue
        if v.get("chroma"):
            chromas.append(v["chroma"])
        if v.get("mfcc"):
            mfccs.append(v["mfcc"])
    out: dict[str, list[float]] = {}
    if chromas:
        dim = len(chromas[0])
        chromas = [c for c in chromas if len(c) == dim]
        if chromas:
            mean = [sum(c[i] for c in chromas) / len(chromas) for i in range(dim)]
            n = _norm(mean)
            out["chroma"] = [x / n for x in mean] if n > 0 else mean
    if mfccs:
        dim = len(mfccs[0])
        mfccs = [m for m in mfccs if len(m) == dim]
        if mfccs:
            mean = [sum(m[i] for m in mfccs) / len(mfccs) for i in range(dim)]
            # центроид снова центрируем: иначе он «уехал» бы в сторону
            # среднего уровня и сравнение с одиночным треком было бы кривым.
            cmean = sum(mean) / len(mean)
            mean = [x - cmean for x in mean]
            n = _norm(mean)
            out["mfcc"] = [x / n for x in mean] if n > 0 else mean
    return out or None


def timbre_to_centroid(vec: dict | None, centroid: dict | None) -> float:
    """Похожесть тембра трека на средний тембр (сидов/сессии)."""
    if not vec or not centroid:
        return 0.0
    return timbre_similarity(vec, centroid)


def knn_scores(cand_map: dict[str, Sequence[float]],
               ref_map: dict[str, Sequence[float]],
               ref_ids: Sequence[str] | None = None,
               top_k: int = TOP_K) -> dict[str, float]:
    """k-NN: среднее top-k косинусов кандидата к ОПОРНЫМ трекам.

    Опорные треки — сиды вкуса и то, что играет сейчас (session). Отличие
    от центроида: сравнение идёт с КАЖДЫМ опорным треком по отдельности, а не
    с их средним. Трек, похожий на один конкретный трек сессии (но не на
    её среднее), получает здесь реальный балл.

    Возвращает {track_id: 0..1}. Пусто, если опор или кандидатов нет.
    """
    ids = [str(i) for i in (ref_ids if ref_ids is not None else list(ref_map))]
    refs: list[tuple[str, Sequence[float]]] = []
    for rid in ids:
        v = ref_map.get(rid)
        if v is None or len(v) == 0:
            continue
        refs.append((rid, v))
        if len(refs) >= MAX_REFS:
            break
    if not refs or not cand_map:
        return {}
    dim = len(refs[0][1])
    refs = [(rid, v) for rid, v in refs if len(v) == dim]
    if not refs:
        return {}
    k = max(1, min(int(top_k or TOP_K), len(refs)))
    out: dict[str, float] = {}
    try:
        import numpy as np

        R = np.asarray([v for _, v in refs], dtype=np.float32)
        norms = np.linalg.norm(R, axis=1, keepdims=True)
        Rn = R / np.maximum(norms, 1e-9)
        for tid, vec in cand_map.items():
            if vec is None or len(vec) != dim:
                continue
            c = np.asarray(vec, dtype=np.float32)
            n = float(np.linalg.norm(c))
            if n <= 0:
                continue
            sims = Rn @ (c / n)
            top = np.sort(sims)[::-1][:k]
            out[tid] = float(max(0.0, float(top.mean())))
        return out
    except Exception:
        # Чистый Python. Опоры урезаем: без numpy 512-D умножение дорогое.
        keep = refs[:12]
        for tid, vec in cand_map.items():
            if vec is None or len(vec) != dim:
                continue
            sims = sorted((cosine(vec, v) for _, v in keep), reverse=True)[:k]
            if sims:
                out[tid] = max(0.0, sum(sims) / len(sims))
        return out


def tempo_compatibility(tempo_a: float | None, tempo_b: float | None,
                        window: float = 40.0) -> float:
    """Совместимость темпа соседей 0..1: чем ближе BPM, тем лучше стык."""
    try:
        ta = float(tempo_a or 0.0)
        tb = float(tempo_b or 0.0)
    except (TypeError, ValueError):
        return 0.5
    if ta <= 0 or tb <= 0:
        return 0.5
    w = max(1.0, float(window or 40.0))
    return max(0.0, 1.0 - abs(ta - tb) / w)
