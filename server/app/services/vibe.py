"""Порт mobile lib/services/ml/vibe_similarity.dart (копия, не вырезаем)."""
from __future__ import annotations

# genre -> vibe базовый профиль (energy, valence, danceability, bpm, acousticness, instrumentalness)
GENRE_PROFILES: dict[str, dict[str, float]] = {
    "electronic": {"energy": 0.85, "valence": 0.6, "danceability": 0.9, "bpm": 128, "acousticness": 0.15, "instrumentalness": 0.5},
    "pop": {"energy": 0.7, "valence": 0.65, "danceability": 0.75, "bpm": 118, "acousticness": 0.3, "instrumentalness": 0.1},
    "rock": {"energy": 0.75, "valence": 0.55, "danceability": 0.5, "bpm": 122, "acousticness": 0.25, "instrumentalness": 0.2},
    "hip-hop": {"energy": 0.75, "valence": 0.55, "danceability": 0.8, "bpm": 95, "acousticness": 0.2, "instrumentalness": 0.15},
    "rap": {"energy": 0.75, "valence": 0.5, "danceability": 0.85, "bpm": 92, "acousticness": 0.2, "instrumentalness": 0.1},
    "metal": {"energy": 0.95, "valence": 0.35, "danceability": 0.4, "bpm": 140, "acousticness": 0.05, "instrumentalness": 0.3},
    "punk": {"energy": 0.9, "valence": 0.5, "danceability": 0.55, "bpm": 155, "acousticness": 0.1, "instrumentalness": 0.1},
    "classical": {"energy": 0.2, "valence": 0.45, "danceability": 0.15, "bpm": 85, "acousticness": 0.95, "instrumentalness": 0.9},
    "ambient": {"energy": 0.15, "valence": 0.4, "danceability": 0.2, "bpm": 80, "acousticness": 0.8, "instrumentalness": 0.85},
    "jazz": {"energy": 0.4, "valence": 0.5, "danceability": 0.4, "bpm": 110, "acousticness": 0.6, "instrumentalness": 0.6},
    "folk": {"energy": 0.35, "valence": 0.5, "danceability": 0.35, "bpm": 105, "acousticness": 0.75, "instrumentalness": 0.3},
    "blues": {"energy": 0.45, "valence": 0.4, "danceability": 0.35, "bpm": 88, "acousticness": 0.6, "instrumentalness": 0.25},
    "country": {"energy": 0.5, "valence": 0.55, "danceability": 0.5, "bpm": 112, "acousticness": 0.65, "instrumentalness": 0.15},
    "r&b": {"energy": 0.55, "valence": 0.5, "danceability": 0.7, "bpm": 90, "acousticness": 0.4, "instrumentalness": 0.1},
    "soul": {"energy": 0.5, "valence": 0.55, "danceability": 0.55, "bpm": 95, "acousticness": 0.45, "instrumentalness": 0.15},
    "reggae": {"energy": 0.55, "valence": 0.7, "danceability": 0.7, "bpm": 90, "acousticness": 0.35, "instrumentalness": 0.1},
    "disco": {"energy": 0.8, "valence": 0.75, "danceability": 0.9, "bpm": 122, "acousticness": 0.2, "instrumentalness": 0.15},
    "house": {"energy": 0.85, "valence": 0.6, "danceability": 0.92, "bpm": 128, "acousticness": 0.1, "instrumentalness": 0.6},
    "techno": {"energy": 0.9, "valence": 0.45, "danceability": 0.88, "bpm": 132, "acousticness": 0.05, "instrumentalness": 0.75},
    "indie": {"energy": 0.6, "valence": 0.5, "danceability": 0.55, "bpm": 115, "acousticness": 0.45, "instrumentalness": 0.2},
    "alternative": {"energy": 0.65, "valence": 0.48, "danceability": 0.5, "bpm": 118, "acousticness": 0.35, "instrumentalness": 0.2},
}

def analyze_track(genre: str | None, energy: float | None = None, valence: float | None = None) -> dict[str, float]:
    g = (genre or "").strip().lower()
    base = GENRE_PROFILES.get(g, {"energy": 0.6, "valence": 0.5, "danceability": 0.6, "bpm": 120, "acousticness": 0.4, "instrumentalness": 0.2})
    # если есть реальные фичи — подмешиваем (как в mobile vibe 0.6+0.4)
    if energy is not None:
        base = dict(base)
        base["energy"] = base["energy"] * 0.6 + float(energy) * 0.4
    if valence is not None:
        base = dict(base) if "energy" not in locals() or isinstance(base, dict) else base
        # already copied if energy case
        if "energy" not in str(base):
            base = dict(base)
        base["valence"] = base["valence"] * 0.6 + float(valence) * 0.4
    return base

def vibe_distance(a: dict[str, float], b: dict[str, float]) -> float:
    # weighted abs diffs как в mobile /4
    w = {"energy": 1.0, "valence": 0.8, "danceability": 0.9, "bpm": 0.7, "acousticness": 0.6}
    # bpm нормируем /150
    s = 0.0
    for k, weight in w.items():
        av = a.get(k, 0.5)
        bv = b.get(k, 0.5)
        if k == "bpm":
            av /= 150.0
            bv /= 150.0
        s += abs(av - bv) * weight
    return s / 4.0

def vibe_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    return 1.0 - vibe_distance(a, b)

def detect_mood(feats: dict[str, float]) -> str:
    e = feats.get("energy", 0.5)
    v = feats.get("valence", 0.5)
    if e > 0.7:
        if v > 0.6:
            return "energetic"
        if v < 0.4:
            return "aggressive"
        return "happy"
    if e < 0.4:
        if v < 0.45:
            return "sad"
        return "calm"
    # mid
    if v > 0.65:
        return "happy"
    if v < 0.4:
        return "melancholic"
    return "focused"

MOOD_COMPAT = {
    "energetic": ["energetic", "happy", "aggressive"],
    "happy": ["happy", "energetic", "calm"],
    "calm": ["calm", "sad", "focused"],
    "sad": ["sad", "melancholic", "calm"],
    "aggressive": ["aggressive", "energetic"],
}

def mood_compatibility(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if b in MOOD_COMPAT.get(a, []) or a in MOOD_COMPAT.get(b, []):
        return 0.8
    return 0.3
