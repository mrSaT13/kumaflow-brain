"""Настоящий sonic-анализ аудиофайлов (без заглушек).

Источники аудио (по приоритету):
  1. Локальный файл: ``track.path`` из Navidrome как есть (когда worker
     монтирует ту же папку в тот же путь, напр. ``/music``) либо склейка
     с ``MUSIC_DIR`` по суффиксу пути.
  2. Стрим из Navidrome через Subsonic API (``download``) во временный файл.

Движок признаков — librosa (должна быть установлена: см. requirements).
Если librosa нет — задача падает с понятной ошибкой, а НЕ пишет
случайные числа в базу.

Что считается (всё из реального сигнала, моно 22050 Гц, первые
ANALYSIS_SAMPLE_SECONDS секунд):
  tempo_bpm, key_name/scale (шаблоны Крамхансл–Шмуклера по средней хроме),
  energy (RMS), loudness_db, spectral_centroid/rolloff, zero_crossing_rate,
  mfcc_summary (средние 13 MFCC), chroma_summary (средние 12 классов),
  danceability / valence / arousal — прозрачные эвристики от измеренных
  величин (формулы ниже), mood_vector + mood_labels — топ-3 правила.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger("audio_analysis")

ANALYZER_VERSION = "librosa-0.10"

AUDIO_SUFFIXES = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".wav", ".wma", ".aac"}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Профили Крамхансл–Шмуклера (корреляция средней хромы с повёрнутым шаблоном).
_KRUMHANSL_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KRUMHANSL_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

MAX_DOWNLOAD_MB = 200


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _require_librosa():
    try:
        import librosa  # type: ignore
        import numpy as _np  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Для sonic-анализа нужна librosa: pip install -r requirements.txt "
            "(блок ML) либо соберите backend-образ заново"
        ) from e
    return librosa, _np


# ---------- источник аудио ----------

def resolve_local_file(track_path: str | None, music_dir: str = "") -> Path | None:
    """Найти файл на диске worker'а. None — нет локального доступа."""
    if not track_path:
        return None
    p = Path(track_path)
    if p.is_file():
        return p
    if not music_dir:
        return None
    base = Path(music_dir)
    # Пробуем суффиксы исходного пути: /music/Artist/Album/01.mp3 →
    # MUSIC_DIR/Artist/Album/01.mp3, MUSIC_DIR/Album/01.mp3, MUSIC_DIR/01.mp3
    parts = PurePosixPath(track_path).parts
    for i in range(len(parts)):
        cand = base.joinpath(*parts[i:])
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _auth_params(user: str, password: str, token: str) -> dict[str, str]:
    salt = secrets.token_hex(6)
    params = {"u": user, "v": "1.16.1", "c": "KumaFlowBrain", "f": "json"}
    if token:
        params["t"] = token
        params["s"] = salt
    else:
        params["t"] = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
        params["s"] = salt
    return params


def download_from_navidrome(external_id: str, cfg: dict[str, Any]) -> Path:
    """Скачать трек через Subsonic download во временный файл. Возвращает путь."""
    url = (cfg.get("url") or "").rstrip("/")
    user = (cfg.get("user") or "").strip()
    if not url or not user:
        raise RuntimeError("Navidrome не настроен (url/user) — не откуда скачать аудио")
    params = _auth_params(user, cfg.get("password") or "", (cfg.get("token") or "").strip())
    params["id"] = external_id
    target = f"{url}/rest/download"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".audio")
    tmp_path = Path(tmp.name)
    try:
        with httpx.stream("GET", target, params=params, timeout=120.0) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            if "json" in ctype or "xml" in ctype:
                raise RuntimeError(f"Navidrome вернул не аудио, а {ctype}")
            size = 0
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_MB * 1024 * 1024:
                        raise RuntimeError(f"Файл больше лимита {MAX_DOWNLOAD_MB} МБ")
        if tmp_path.stat().st_size == 0:
            raise RuntimeError("Скачан пустой файл")
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------- чистые эвристики (тестируются без librosa) ----------

def estimate_key(chroma_mean: list[float]) -> tuple[str, str]:
    """Тональность: корреляция средней хромы с шаблонами мажор/минор."""
    best_key, best_mode, best_score = "C", "major", -2.0
    for shift in range(12):
        for mode, profile in (("major", _KRUMHANSL_MAJOR), ("minor", _KRUMHANSL_MINOR)):
            # Тоника шаблона (индекс 0) должна лечь на pitch-класс shift.
            rotated = profile[-shift:] + profile[:-shift] if shift else list(profile)
            # корреляция Пирсона вручную (без numpy — работает везде)
            n = 12
            mx = sum(chroma_mean) / n
            my = sum(rotated) / n
            num = sum((a - mx) * (b - my) for a, b in zip(chroma_mean, rotated))
            den = (sum((a - mx) ** 2 for a in chroma_mean) * sum((b - my) ** 2 for b in rotated)) ** 0.5
            score = num / den if den > 0 else 0.0
            if score > best_score:
                best_score, best_key, best_mode = score, NOTE_NAMES[shift], mode
    return best_key, best_mode


def mood_from_features(feats: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    """Настроение из измеренных величин. Формулы зафиксированы и прозрачны."""
    energy = _clamp(feats.get("energy", 0.5))
    valence = _clamp(feats.get("valence", 0.5))
    dance = _clamp(feats.get("danceability", 0.5))
    tempo = float(feats.get("tempo_bpm") or 120.0)
    loud = float(feats.get("loudness_db") or -20.0)
    zcr = float(feats.get("zero_crossing_rate") or 0.05)
    major = feats.get("is_major", 1.0)

    scores = {
        "энергичный": energy,
        "спокойный": 1.0 - energy,
        "танцевальный": dance,
        "счастливый": _clamp(valence * 0.7 + major * 0.3),
        "меланхоличный": _clamp((1.0 - valence) * 0.7 + (1.0 - major) * 0.3),
        "агрессивный": _clamp(energy * 0.5 + _clamp((loud + 12.0) / 12.0) * 0.3 + _clamp(zcr / 0.12) * 0.2),
        "мечтательный": _clamp((1.0 - energy) * 0.5 + _clamp((110.0 - tempo) / 80.0) * 0.5),
        "тёплый": _clamp(valence * 0.5 + major * 0.5),
        "тёмный": _clamp((1.0 - valence) * 0.5 + (1.0 - major) * 0.5),
    }
    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    vector = {
        "energetic": energy,
        "valence": valence,
        "arousal": _clamp(feats.get("arousal", energy)),
        "calm": 1.0 - energy,
    }
    return vector, [k for k, _ in top]


# ---------- главный проход ----------

def analyze_file(
    path: str | Path,
    *,
    sample_seconds: int = 90,
    track_duration_sec: int | None = None,
) -> dict[str, Any]:
    """Проанализировать аудиофайл. Всё ниже — измерения реального сигнала."""
    librosa, np = _require_librosa()
    path = str(path)
    if not os.path.isfile(path):
        raise RuntimeError(f"Файл не найден: {path}")

    duration_full = track_duration_sec or None
    try:
        duration_full = float(librosa.get_duration(path=path))
    except Exception:
        pass

    y, sr = librosa.load(path, sr=22050, mono=True, duration=sample_seconds)
    if y is None or len(y) < sr:
        raise RuntimeError("Не удалось декодировать аудио (меньше 1 секунды)")

    tempo_arr, _beats = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo_arr)[0])

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = [float(v) for v in np.mean(chroma, axis=1)]
    key_name, scale = estimate_key(chroma_mean)

    cent = librosa.feature.spectral_centroid(y=y, sr=sr)
    roll = librosa.feature.spectral_rolloff(y=y, sr=sr)
    zcr = librosa.feature.zero_crossing_rate(y)
    rms = librosa.feature.rms(y=y)
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)

    centroid = float(np.mean(cent))
    rolloff = float(np.mean(roll))
    zcr_m = float(np.mean(zcr))
    rms_m = float(np.mean(rms))
    onset_m = float(np.mean(onset))
    mfcc_mean = [float(v) for v in np.mean(mfcc, axis=1)]

    # Производные величины (формулы зафиксированы — см. docstring модуля).
    energy = _clamp((rms_m - 0.02) / 0.25)
    loudness_db = float(20.0 * np.log10(rms_m + 1e-10))
    tempo_norm = _clamp((tempo - 60.0) / 120.0)
    tempo_score = _clamp(1.0 - abs(tempo - 124.0) / 90.0)  # пик танцевальности ~124 BPM
    danceability = _clamp(0.4 * tempo_score + 0.35 * energy + 0.25 * _clamp(onset_m / 8.0))
    is_major = 1.0 if scale == "major" else 0.0
    bright = _clamp((centroid - 500.0) / 5500.0)
    valence = _clamp(0.45 * is_major + 0.25 * tempo_norm + 0.20 * bright + 0.10 * energy)
    arousal = _clamp(0.50 * energy + 0.30 * tempo_norm + 0.20 * _clamp((loudness_db + 30.0) / 30.0))

    mood_vector, mood_labels = mood_from_features({
        "energy": energy, "valence": valence, "danceability": danceability,
        "arousal": arousal, "tempo_bpm": tempo, "loudness_db": loudness_db,
        "zero_crossing_rate": zcr_m, "is_major": is_major,
    })

    return {
        "analyzer": ANALYZER_VERSION,
        "duration_sec": int(round(duration_full)) if duration_full else None,
        "tempo_bpm": round(tempo, 1),
        "key_name": key_name,
        "scale": scale,
        "energy": round(energy, 3),
        "danceability": round(danceability, 3),
        "valence": round(valence, 3),
        "arousal": round(arousal, 3),
        "loudness_db": round(loudness_db, 2),
        "spectral_centroid": round(centroid, 1),
        "spectral_rolloff": round(rolloff, 1),
        "zero_crossing_rate": round(zcr_m, 4),
        "mfcc_summary": {"mean": [round(v, 4) for v in mfcc_mean]},
        "chroma_summary": {"mean": [round(v, 4) for v in chroma_mean]},
        "mood_vector": mood_vector,
        "mood_labels": mood_labels,
    }
