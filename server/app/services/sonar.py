"""«Сонар» мозга: аудио-фингерпринтинг библиотеки без внешних API.

Принцип — как у Shazam/Dejavu, имя — своё: пики спектрограммы -> парные
хэши (f1|f2|dt) с оффсетом -> матчинг гистограммой дельт оффсетов.

DSP — чистый numpy (STFT руками), без scipy/librosa в ядре: тесты идут
везде. Загрузка файла: общий load_audio_mono из sonic-анализа
(soundfile -> ffmpeg, без audioread-ворнингов). Аудио берётся как
в sonic-анализе: локальный файл либо Subsonic ``download`` (см. enroll
в workers/tasks).

Признаки/пороги — константами ниже. Хранилище — ``audio_fingerprints``:
~500 хэшей на трек при ENROLL_SECONDS=90.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

SR = 11025          # моно, даунсэмпл — пикам хватает
N_FFT = 1024
HOP = 512
FREQ_MIN = 30.0     # Гц
FREQ_MAX = 5000.0   # Гц
PEAKS_PER_FRAME = 6
AMP_FACTOR = 1.8    # пик > среднего кадра × фактор
FANOUT = 10         # пар на один якорный пик
DT_MIN = 0.05       # c — минимальная дельта пары
DT_MAX = 2.0        # c — максимальная дельта пары
MAX_HASHES_PER_SEC = 12   # бюджет плотности (равномерно по секундам)
ENROLL_SECONDS = 90.0     # сколько секунд трека печатаем
QUERY_SECONDS = 20.0      # сколько секунд записи разбираем
MIN_HITS = 6        # минимум совпадений дельты для матча
CONFIRM_RATIO = 1.4  # лучший должен обойти второго в столько раз...
CONFIRM_STRONG = 12  # ...либо набрать столько хитов и так
DUP_MIN_SHARED = 25  # общих хэшей для пары-дубля


# ---------- DSP (чистый numpy) ----------

def stft_mag(y, sr: int = SR) -> tuple[Any, float, float]:
    """Магнитудная спектрограмма. Возвращает (S [bins×frames], df, dt)."""
    import numpy as _np
    y = _np.asarray(y, dtype=float).ravel()
    if y.size < N_FFT:
        y = _np.pad(y, (0, N_FFT - y.size))
    win = _np.hanning(N_FFT)
    n_frames = 1 + (y.size - N_FFT) // HOP
    frames = _np.lib.stride_tricks.as_strided(
        y, shape=(n_frames, N_FFT),
        strides=(y.strides[0] * HOP, y.strides[0])).copy()
    S = _np.abs(_np.fft.rfft(frames * win, axis=1)).T  # bins × frames
    return S, float(sr) / N_FFT, float(HOP) / sr


def find_peaks(S, df: float, dt: float) -> list[tuple[int, float]]:
    """Пики: топ-N локальных максимумов кадра выше порога. [(bin, t_sec)]."""
    import numpy as _np
    S = _np.asarray(S, dtype=float)
    n_bins, n_frames = S.shape
    lo = max(1, int(FREQ_MIN / df))
    hi = min(n_bins - 2, int(FREQ_MAX / df) + 1)
    if hi <= lo or n_frames == 0:
        return []
    peaks: list[tuple[int, float]] = []
    for fi in range(n_frames):
        col = S[lo:hi, fi]
        if col.size == 0:
            continue
        thr = float(col.mean()) * AMP_FACTOR
        # локальные максимумы по ±3 бина
        cand = []
        for i in range(3, col.size - 3):
            v = col[i]
            if v > thr and v >= col[i - 3:i].max() and v >= col[i + 1:i + 4].max():
                cand.append((float(v), lo + i))
        cand.sort(reverse=True)
        for _, b in cand[:PEAKS_PER_FRAME]:
            peaks.append((int(b), round(fi * dt, 3)))
    return peaks


def _pack_hash(f1: int, f2: int, dt_ms: int) -> str:
    h = ((f1 & 0x1FF) << 15) | ((f2 & 0x1FF) << 6) | (dt_ms & 0x3F)
    return "%06x" % h


def gen_hashes(peaks: list[tuple[int, float]],
               fanout: int = FANOUT) -> list[tuple[str, float]]:
    """Пары пиков -> [(hash_hex, t_sec я (якорь))]."""
    out: list[tuple[str, float]] = []
    n = len(peaks)
    for i in range(n):
        f1, t1 = peaks[i]
        for j in range(i + 1, min(i + 1 + fanout, n)):
            f2, t2 = peaks[j]
            d = t2 - t1
            if d < DT_MIN or d > DT_MAX:
                continue
            out.append((_pack_hash(f1, f2, int(d * 1000) // 50), t1))
    return out


def budget_hashes(hashes: list[tuple[str, float]],
                  max_per_sec: int = MAX_HASHES_PER_SEC) -> list[tuple[str, float]]:
    """Равномерный бюджет плотности: не больше N хэшей на каждую секунду."""
    if not hashes:
        return []
    by_sec: dict[int, list] = defaultdict(list)
    for h, t in hashes:
        by_sec[int(t)].append((h, t))
    out = []
    for sec in sorted(by_sec):
        grp = by_sec[sec]
        if len(grp) > max_per_sec:
            step = len(grp) / max_per_sec
            grp = [grp[int(k * step)] for k in range(max_per_sec)]
        out.extend(grp)
    return out


def fingerprint_samples(y, sr: int = SR,
                        max_seconds: float = ENROLL_SECONDS) -> list[tuple[str, float]]:
    """Сэмплы -> хэши с бюджетом. Чистая функция (тестируется без файлов)."""
    import numpy as _np
    y = _np.asarray(y, dtype=float).ravel()
    if y.size == 0:
        return []
    # ресэмпл до SR простым дециматом/повтором (точность пикам не критична)
    if sr != SR and sr > 0:
        ratio = sr / SR
        if ratio > 1:
            step = int(round(ratio))
            y = y[::step]
        else:
            rep = int(round(1.0 / ratio))
            y = _np.repeat(y, rep)
    max_n = int(SR * max_seconds)
    y = y[:max_n]
    if y.size < N_FFT:
        return []
    S, df, dt = stft_mag(y, SR)
    peaks = find_peaks(S, df, dt)
    return budget_hashes(gen_hashes(peaks))


def load_mono(path, sr: int = SR, duration: float | None = None):
    """Декодировать аудио в моно. Без audioread: soundfile -> ffmpeg."""
    try:
        from app.services.audio_analysis import load_audio_mono as _load

        y, _sr = _load(str(path), sr=sr, offset=0.0, duration=duration)
        import numpy as _np

        return _np.asarray(y, dtype=float), int(_sr)
    except Exception:
        pass
    # legacy-фолбэк (как раньше): librosa, затем soundfile целиком
    try:
        import librosa as _lib
        y, _ = _lib.load(str(path), sr=sr, mono=True,
                         duration=duration)
        return y, sr
    except Exception:
        pass
    import soundfile as _sf
    import numpy as _np
    y, f_sr = _sf.read(str(path), always_2d=False)
    y = _np.asarray(y, dtype=float)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if duration:
        y = y[:int(int(f_sr) * duration)]
    return y, int(f_sr)


def fingerprint_file(path, max_seconds: float = ENROLL_SECONDS) -> list[tuple[str, float]]:
    """Файл -> хэши. Нужен librosa или soundfile (как у sonic-анализа)."""
    y, f_sr = load_mono(path, duration=max_seconds + 5.0)
    return fingerprint_samples(y, f_sr, max_seconds=max_seconds)


# ---------- хранилище ----------

def store(db, track_id: str, hashes: list[tuple[str, float]]) -> int:
    """Перезаписать отпечаток трека. Возвращает число записанных хэшей."""
    from app.db.models import AudioFingerprint as _AF
    tid = str(track_id)
    try:
        db.query(_AF).filter(_AF.track_id == tid).delete()
        db.flush()
        rows = [dict(track_id=tid, h=h, t_off=float(t)) for h, t in hashes]
        for i in range(0, len(rows), 1000):
            db.bulk_insert_mappings(_AF, rows[i:i + 1000])
        db.commit()
        return len(rows)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def fingerprinted_ids(db) -> set[str]:
    from app.db.models import AudioFingerprint as _AF
    try:
        return {str(r[0]) for r in db.query(_AF.track_id).distinct().all()}
    except Exception:
        return set()


def coverage(db) -> dict:
    from app.db.models import AudioFingerprint as _AF
    from app.db.models import Track as _T
    try:
        total = db.query(_T).count()
    except Exception:
        total = 0
    try:
        fp = db.query(_AF.track_id).distinct().count()
        hashes = db.query(_AF).count()
    except Exception:
        fp, hashes = 0, 0
    return {"tracks_total": total, "fingerprinted": fp,
            "hashes_total": hashes,
            "coverage_pct": round(fp / total * 100.0, 1) if total else 0.0}


# ---------- матчинг ----------

def match(db, qhashes: list[tuple[str, float]],
          limit_tracks: int = 5) -> list[dict]:
    """Гистограмма дельт оффсетов. Возвращает топ-кандидатов."""
    from app.db.models import AudioFingerprint as _AF
    if not qhashes:
        return []
    qmap: dict[str, list[float]] = defaultdict(list)
    for h, t in qhashes:
        qmap[h].append(float(t))
    keys = list(qmap.keys())
    per_track: dict[str, Counter] = defaultdict(Counter)
    per_track_total: Counter = Counter()
    try:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            try:
                rows = db.query(_AF.track_id, _AF.h, _AF.t_off).filter(
                    _AF.h.in_(chunk)).all()
            except Exception:
                continue
            for tid, h, toff in rows:
                tid = str(tid)
                for qt in qmap[h]:
                    per_track[tid][round(float(toff) - qt, 1)] += 1
                    per_track_total[tid] += 1
    except Exception:
        return []
    scored = []
    for tid, hist in per_track.items():
        if not hist:
            continue
        best_delta, hits = hist.most_common(1)[0]
        scored.append({"track_id": tid, "hits": hits,
                       "delta": best_delta,
                       "total": per_track_total[tid],
                       "coherence": round(hits / max(per_track_total[tid], 1), 3)})
    scored.sort(key=lambda d: (d["hits"], d["coherence"]), reverse=True)
    return scored[:max(1, int(limit_tracks or 5))]


def recognize(db, qhashes: list[tuple[str, float]]) -> dict | None:
    """Лучший матч или None. Правило: хиты + отрыв от второго."""
    cands = match(db, qhashes, limit_tracks=2)
    if not cands:
        return None
    best = cands[0]
    if best["hits"] < MIN_HITS:
        return None
    if len(cands) > 1:
        second = cands[1]["hits"]
        if best["hits"] < CONFIRM_STRONG and best["hits"] < second * CONFIRM_RATIO:
            return None
    return best


def recognize_track(db, qhashes: list[tuple[str, float]]) -> dict | None:
    """Матч + мета трека (для API/плееров: сразу external_id)."""
    best = recognize(db, qhashes)
    if not best:
        return None
    try:
        from app.db.models import Track as _T
        t = db.get(_T, best["track_id"])
        if t is None:
            return None
        return {"track_id": str(t.id), "external_id": str(t.external_id or ""),
                "title": t.title, "artist_name": t.artist_name,
                "album_name": t.album_name, "genre": t.genre,
                "hits": best["hits"], "coherence": best["coherence"],
                "delta": best["delta"]}
    except Exception:
        return None


# ---------- дубли и enrich ----------

def duplicates(db, min_shared: int = DUP_MIN_SHARED,
               max_tracks: int = 2000) -> list[dict]:
    """Пары треков с общими хэшами — кандидаты в дубли (сшивка вручную)."""
    from app.db.models import AudioFingerprint as _AF
    from app.db.models import Track as _T
    try:
        tids = [str(r[0]) for r in db.query(_AF.track_id).distinct()
                .limit(max_tracks).all()]
    except Exception:
        return []
    if len(tids) < 2:
        return []
    # хэши чанка -> треки с ними
    h2t: dict[str, set[str]] = defaultdict(set)
    try:
        for i in range(0, len(tids), 200):
            chunk = tids[i:i + 200]
            for tid, h in db.query(_AF.track_id, _AF.h).filter(
                    _AF.track_id.in_(chunk)).all():
                h2t[h].add(str(tid))
    except Exception:
        return []
    pair_shared: Counter = Counter()
    for tracks in h2t.values():
        if len(tracks) < 2:
            continue
        lst = sorted(tracks)
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                pair_shared[(lst[a], lst[b])] += 1
    groups = [{"track_a": a, "track_b": b, "shared": c}
              for (a, b), c in pair_shared.items() if c >= min_shared]
    groups.sort(key=lambda g: g["shared"], reverse=True)
    # мета для читаемости
    try:
        want = list({g["track_a"] for g in groups[:200]} |
                    {g["track_b"] for g in groups[:200]})
        meta = {}
        for i in range(0, len(want), 500):
            for t in db.query(_T).filter(_T.id.in_(want[i:i + 500])).all():
                meta[str(t.id)] = t
        for g in groups[:200]:
            for k, mk in (("track_a", "meta_a"), ("track_b", "meta_b")):
                t = meta.get(g[k])
                g[mk] = ({"title": t.title, "artist_name": t.artist_name,
                          "album_name": t.album_name,
                          "external_id": str(t.external_id or "")}
                         if t is not None else None)
    except Exception:
        pass
    return groups


def enrich(db, max_tracks: int = 2000,
           min_shared: int = DUP_MIN_SHARED) -> dict:
    """Добить пустые genre/year/album_name из фингерпринт-двойника.

    Пишем только в пустые поля, ничего не затираем. Возвращает счётчики.
    """
    from app.db.models import Track as _T
    groups = duplicates(db, min_shared=min_shared, max_tracks=max_tracks)
    filled = {"genre": 0, "year": 0, "album_name": 0, "tracks": 0}
    if not groups:
        return {"ok": True, **filled, "pairs": 0}
    want = list({g["track_a"] for g in groups} | {g["track_b"] for g in groups})
    meta: dict[str, Any] = {}
    try:
        for i in range(0, len(want), 500):
            for t in db.query(_T).filter(_T.id.in_(want[i:i + 500])).all():
                meta[str(t.id)] = t
    except Exception:
        return {"ok": False, **filled, "pairs": 0, "error": "db"}
    touched = 0
    for g in groups:
        a, b = meta.get(g["track_a"]), meta.get(g["track_b"])
        if a is None or b is None:
            continue
        # донор — у кого больше заполнено
        def _full(t):
            return sum(1 for v in (t.genre, t.year, t.album_name) if v)
        donor, recv = (a, b) if _full(a) >= _full(b) else (b, a)
        if _full(donor) == _full(recv):
            continue
        changed = False
        try:
            if not recv.genre and donor.genre:
                recv.genre = donor.genre
                filled["genre"] += 1
                changed = True
            if not recv.year and donor.year:
                recv.year = donor.year
                filled["year"] += 1
                changed = True
            if not recv.album_name and donor.album_name:
                recv.album_name = donor.album_name
                filled["album_name"] += 1
                changed = True
        except Exception:
            continue
        if changed:
            touched += 1
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, **filled, "pairs": len(groups), "error": "commit"}
    filled["tracks"] = touched
    return {"ok": True, **filled, "pairs": len(groups)}
