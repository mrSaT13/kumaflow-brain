"""Дедуп по аудио-фингерпринту (mfcc+chroma+tempo+key), а не только по тегам.

Fingerprint строится из уже посчитанного TrackFeatures (sonic-анализ):
vec = [mfcc(13, z-norm), chroma(12, l2), tempo/energy/valence, key one-hot(24)]
cosine >= threshold (default 0.985) + duration ±3с = дубли.
_live/remix_ маркеры — всегда только ручные (как в dedup.py).
"""
from __future__ import annotations

import hashlib

THRESHOLD_DEFAULT = 0.985
THRESHOLD_MANUAL = 0.97

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def audio_fingerprint_vec(f) -> list[float] | None:
    try:
        import numpy as _np
    except Exception:
        return None
    try:
        mfcc = ((f.mfcc_summary or {}).get("mean") or []) if isinstance(f.mfcc_summary, dict) else []
        chroma = ((f.chroma_summary or {}).get("mean") or []) if isinstance(f.chroma_summary, dict) else []
        if len(mfcc) < 13 or len(chroma) < 12:
            return None
        import numpy as np

        m = np.array(mfcc[:13], dtype=float)
        c = np.array(chroma[:12], dtype=float)
        m = (m - m.mean()) / (m.std() or 1.0)
        c = c / (float(np.linalg.norm(c)) or 1.0)
        tempo = float(f.tempo_bpm or 120.0)
        t = max(-2.0, min(2.0, (tempo - 120.0) / 60.0)) / 2.0
        en = float(f.energy or 0.5)
        va = float(f.valence or 0.5)
        key = (f.key_name or "C").replace("♯", "#").replace("♭", "b")
        scale = (f.scale or "major").lower()
        key_vec = [0.0] * 24
        try:
            ki = NOTE_NAMES.index(key)
            key_vec[ki + (0 if "maj" in scale else 12)] = 1.0
        except Exception:
            pass
        vec = np.concatenate([m, c, np.array([t, en, va]) * 0.5, np.array(key_vec) * 0.3])
        n = float(np.linalg.norm(vec))
        if n <= 0:
            return None
        return (vec / n).tolist()
    except Exception:
        return None


def fingerprint_hash(vec: list[float]) -> str:
    import struct

    try:
        b = struct.pack(f"{len(vec)}f", *[float(x) for x in vec])
    except Exception:
        b = repr(vec).encode()
    return hashlib.sha1(b).hexdigest()[:16]


def _cosine(a: list[float], b: list[float]) -> float:
    try:
        s = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return s / ((na * nb) or 1.0)
    except Exception:
        return 0.0


def _has_version_marker(*texts: str) -> bool:
    t = " ".join((x or "") for x in texts).lower()
    for m in ("live", "remix", "remaster", "acoustic", "demo", "edit", "version", "mix", "cover", "концерт"):
        if m in t:
            return True
    return False


def find_fingerprint_groups(db, threshold: float = THRESHOLD_DEFAULT, limit_tracks: int = 20000) -> list[dict]:
    """Группы аудио-дублей. O(бакеты по длительности), не O(N²) на 150k."""
    from app.db.models import Track, TrackFeatures

    threshold = max(0.9, min(0.999, float(threshold or THRESHOLD_DEFAULT)))
    feats = db.query(TrackFeatures).limit(limit_tracks).all()
    if not feats:
        return []
    tids = [str(f.track_id) for f in feats]
    tracks = {str(t.id): t for t in db.query(Track).filter(Track.id.in_(tids[:5000])).all()} if tids else {}
    # вектора
    vecs: dict[str, list[float]] = {}
    for f in feats:
        v = audio_fingerprint_vec(f)
        if v:
            vecs[str(f.track_id)] = v
    # бакеты по длительности ±3с
    buckets: dict[int, list[str]] = {}
    for tid in vecs:
        t = tracks.get(tid)
        if t is None:
            try:
                t = db.get(Track, tid)
                if t is None:
                    continue
                tracks[tid] = t
            except Exception:
                continue
        try:
            dur = int(float(t.duration_sec or 0) // 3)
        except Exception:
            dur = 0
        buckets.setdefault(dur, []).append(tid)
    groups: list[dict] = []
    used: set[str] = set()
    for _, ids in buckets.items():
        if len(ids) < 2:
            continue
        for i, a in enumerate(ids):
            if a in used:
                continue
            grp = [a]
            ta = tracks.get(a)
            for b in ids[i + 1:]:
                if b in used:
                    continue
                tb = tracks.get(b)
                # длительность ±3с строго
                try:
                    if ta and tb and ta.duration_sec and tb.duration_sec:
                        if abs(float(ta.duration_sec) - float(tb.duration_sec)) > 3:
                            continue
                except Exception:
                    pass
                s = _cosine(vecs[a], vecs[b])
                if s >= threshold:
                    if _has_version_marker(getattr(ta, "title", ""), getattr(tb, "title", ""),
                                           getattr(ta, "album_name", ""), getattr(tb, "album_name", "")):
                        continue  # live/remix — только вручную
                    grp.append(b)
            if len(grp) >= 2:
                # keep: больше play_count/starred
                def _score(tid: str):
                    t = tracks.get(tid)
                    return (int(getattr(t, "play_count", 0) or 0), bool(getattr(t, "starred", False)))
                grp.sort(key=_score, reverse=True)
                keep, *drop = grp
                # средняя схожесть внутри группы
                ss = []
                for x in drop:
                    ss.append(_cosine(vecs[keep], vecs[x]))
                avg = sum(ss) / len(ss) if ss else 1.0
                items = []
                for tid in grp:
                    t = tracks.get(tid)
                    items.append({"id": tid, "title": getattr(t, "title", "?") if t else "?",
                                  "artist_name": getattr(t, "artist_name", None) if t else None,
                                  "duration_sec": getattr(t, "duration_sec", None) if t else None})
                groups.append({"type": "fingerprint", "score": round(avg, 4),
                               "keep_id": keep, "drop_ids": drop, "tracks": items})
                used.update(grp)
    groups.sort(key=lambda g: g["score"], reverse=True)
    return groups
