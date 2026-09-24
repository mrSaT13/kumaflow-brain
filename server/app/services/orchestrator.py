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


# Совместимость тональностей по квинтовому кругу (диджейское микширование).
# key_name вида "C", "G#", "Ab" (+ ♯/♭), scale "major"/"minor".
_SEMI = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4,
         "F": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9,
         "A#": 10, "BB": 10, "B": 11}


def parse_key(key_name: Any, scale: Any) -> tuple[int | None, str | None]:
    """(полутон тоники 0-11, 'major'/'minor'). (None, None) если не разобрать."""
    try:
        k = (str(key_name or "").strip().upper()
             .replace("♯", "#").replace("♭", "B").replace(" ", ""))
        semi = _SEMI.get(k)
        if semi is None and len(k) >= 1:
            semi = _SEMI.get(k[:2]) if len(k) >= 2 else _SEMI.get(k[:1])
        s = (str(scale or "").strip().lower() or None)
        if s not in ("major", "minor"):
            s = None
        if semi is None:
            return None, None
        return semi, s
    except Exception:
        return None, None


def key_compatibility(key1: Any, scale1: Any, key2: Any, scale2: Any) -> float:
    """0..1: насколько гладко key1 -> key2. Незнакомое = 0.5 (нейтрально).

    1.0 та же тональность, 0.9 относительные (C-dur/A-moll — общие знаки),
    0.8 сосед по квинтовому кругу, дальше — затухание.
    """
    s1, m1 = parse_key(key1, scale1)
    s2, m2 = parse_key(key2, scale2)
    if s1 is None or s2 is None:
        return 0.5
    if s1 == s2:
        if m1 == m2:
            return 1.0
        return 0.7  # параллельные (та же тоника, другой лад)
    # Относительные (C-dur/A-moll) — общие знаки: минор приводим к мажору (+3).
    n1 = (s1 + 3) % 12 if m1 == "minor" else s1
    n2 = (s2 + 3) % 12 if m2 == "minor" else s2
    f1, f2 = (n1 * 7) % 12, (n2 * 7) % 12  # позиция на квинтовом круге
    if f1 == f2:
        return 0.9
    d = min((f1 - f2) % 12, (f2 - f1) % 12)
    if d == 1:
        return 0.8 if m1 == m2 else 0.6
    if d == 2:
        return 0.4
    return 0.2


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
