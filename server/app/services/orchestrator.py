"""Порт lib/services/playlist_orchestrator.dart (копия, не вырезаем с mobile)."""
from __future__ import annotations

from typing import Any

from app.services.vibe import analyze_track


def _energy_of(track: Any, feat_map: dict[str, Any] | None = None) -> float:
    # track может быть dict или ORM Track
    genre = getattr(track, "genre", None) or (track.get("genre") if isinstance(track, dict) else None)
    # если есть фичи energy — используем напрямую
    tid = str(getattr(track, "id", "") or (track.get("id") if isinstance(track, dict) else ""))
    feats = (feat_map or {}).get(tid) if feat_map else None
    e = None
    if feats and getattr(feats, "energy", None) is not None:
        e = float(feats.energy)
    base = analyze_track(genre, energy=e)
    return float(base.get("energy", 0.6))


def orchestrate(tracks: list[Any], feat_map: dict[str, Any] | None = None, start_with: str = "mixed", end_with: str = "mixed") -> list[Any]:
    """Сортировка по энергии как в mobile orchestrate()."""
    if not tracks:
        return []
    scored = [(t, _energy_of(t, feat_map)) for t in tracks]
    if start_with == "energetic" and end_with == "calm":
        scored.sort(key=lambda kv: kv[1], reverse=True)
    elif start_with == "calm" and end_with == "energetic":
        scored.sort(key=lambda kv: kv[1])
    elif start_with == "energetic" and end_with == "energetic":
        scored.sort(key=lambda kv: abs(kv[1] - 0.5), reverse=True)
    else:  # mixed — чередуем high/low
        scored.sort(key=lambda kv: kv[1], reverse=True)
        high = [t for t, e in scored if e >= 0.6]
        low = [t for t, e in scored if e < 0.6]
        res: list[Any] = []
        i = j = 0
        turn = True
        while i < len(high) or j < len(low):
            if turn and i < len(high):
                res.append(high[i]); i += 1
            elif not turn and j < len(low):
                res.append(low[j]); j += 1
            elif i < len(high):
                res.append(high[i]); i += 1
            else:
                res.append(low[j]); j += 1
            turn = not turn
        return res
    return [t for t, _ in scored]


def create_energy_wave(tracks: list[Any], feat_map: dict[str, Any] | None = None, segments: int = 3) -> list[Any]:
    """calm -> energetic -> calm волной."""
    if not tracks:
        return []
    scored = [(t, _energy_of(t, feat_map)) for t in tracks]
    scored.sort(key=lambda kv: kv[1])
    n = len(scored)
    seg_size = (n + segments - 1) // segments
    out: list[Any] = []
    for idx in range(segments):
        seg = scored[idx * seg_size : (idx + 1) * seg_size]
        if idx % 2 == 1:
            seg = list(reversed(seg))
        out.extend([t for t, _ in seg])
    return out


def is_smooth_transition(cur: Any, nxt: Any, feat_map: dict[str, Any] | None = None) -> bool:
    from app.services.vibe import detect_mood

    g1 = (getattr(cur, "genre", None) or (cur.get("genre") if isinstance(cur, dict) else "") or "").lower()
    g2 = (getattr(nxt, "genre", None) or (nxt.get("genre") if isinstance(cur, dict) else "") or "").lower()
    if g1 and g1 == g2:
        return True
    e1 = _energy_of(cur, feat_map)
    e2 = _energy_of(nxt, feat_map)
    if abs(e1 - e2) < 0.3:
        return True
    # mood exact
    m1 = detect_mood(analyze_track(g1, energy=e1))
    m2 = detect_mood(analyze_track(g2, energy=e2))
    return m1 == m2
