"""Настоящий sonic-анализ аудиофайлов (без заглушек).

Источники аудио (по приоритету):
  1. Локальный файл: ``track.path`` из Navidrome как есть (когда worker
     монтирует ту же папку в тот же путь, напр. ``/music``) либо склейка
     с ``MUSIC_DIR`` по суффиксу пути.
  2. Стрим из Navidrome через Subsonic API (``download``) во временный файл.

Движок признаков — librosa (должна быть установлена: см. requirements).
Если librosa нет — задача падает с понятной ошибкой, а НЕ пишет
случайные числа в базу.

Что считается (всё из реального сигнала, моно 22050 Гц, суммарно
ANALYSIS_SAMPLE_SECONDS секунд, но тремя кусками — начало/середина/конец,
а не только вступление):
  tempo_bpm (медиана по кускам), key_name/scale (шаблоны Крамхансл–Шмуклера
  по усреднённой хроме), energy (RMS), loudness_db, spectral_centroid/rolloff,
  zero_crossing_rate, mfcc_summary (средние 13 MFCC), chroma_summary
  (средние 12 классов), danceability / valence / arousal — прозрачные
  эвристики от измеренных величин (формулы ниже), mood_vector + mood_labels —
  топ-3 правила.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import warnings as _warnings
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger("audio_analysis")

# Пояс безопасности: librosa 0.10 дёргает deprecated audioread-бэкенд
# (FutureWarning: PySoundFile failed / Audioread support is deprecated).
# Мы в audioread больше не ходим (свой путь soundfile -> ffmpeg ниже),
# фильтры — на случай чужих вызовов librosa.load (sonar-legacy, clap).
_warnings.filterwarnings("ignore", message=".*PySoundFile failed.*")
_warnings.filterwarnings("ignore", message=".*audioread.*", category=FutureWarning)

ANALYZER_VERSION = "librosa-0.10-3x30"

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


# ---------- загрузка аудио без audioread ----------

def get_audio_duration(path: str | Path) -> float | None:
    """Длительность трека без librosa.get_duration (тот дёргает audioread).

    soundfile.info -> ffprobe -> парс stderr ffmpeg. None — не удалось.
    """
    p = str(path)
    try:
        import soundfile as _sf

        _info = _sf.info(p)
        if _info.frames and _info.samplerate:
            return float(_info.frames) / float(_info.samplerate)
    except Exception:
        pass
    import re as _re
    import shutil as _sh
    import subprocess as _sp

    if _sh.which("ffprobe"):
        try:
            _pr = _sp.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", p],
                stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=30)
            _val = (_pr.stdout or b"").decode("utf-8", "ignore").strip()
            if _val:
                return float(_val)
        except Exception:
            pass
    if _sh.which("ffmpeg"):
        try:
            _pr = _sp.run(["ffmpeg", "-i", p],
                          stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=30)
            _err = (_pr.stderr or b"").decode("utf-8", "ignore")
            _m = _re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", _err)
            if _m:
                return (int(_m.group(1)) * 3600 + int(_m.group(2)) * 60
                        + float(_m.group(3)))
        except Exception:
            pass
    return None


def load_audio_mono(
    path: str | Path,
    *,
    sr: int = 22050,
    offset: float = 0.0,
    duration: float | None = None,
) -> tuple[Any, int]:
    """Декодировать срез аудио в моно с ресемплом. Без audioread и ворнингов.

    Путь 1: soundfile напрямую (wav/flac/ogg/большинство mp3, seek по кадрам).
    Путь 2: ffmpeg декодирует только нужный срез (``-ss/-t``) во временный
    wav в памяти -> soundfile. Покрывает всё, что раньше тащил audioread
    (m4a/aac, wma, кривые mp3), но быстрее: целый файл не гоняется.
    """
    import numpy as _np

    p = str(path)
    if not os.path.isfile(p):
        raise RuntimeError(f"Файл не найден: {p}")
    last_err: Exception | None = None
    try:
        import soundfile as _sf

        _info = _sf.info(p)
        _native = int(_info.samplerate) or int(sr)
        _start = int(float(offset or 0.0) * _native) if offset else 0
        _stop = (_start + int(float(duration) * _native)
                 if duration else None)
        y, _fsr = _sf.read(p, start=_start, stop=_stop, always_2d=False)
        y = _np.asarray(y, dtype=float)
        if y.ndim > 1:
            y = y.mean(axis=1)
        if int(_fsr) != int(sr):
            _lib, _ = _require_librosa()
            y = _lib.resample(y, orig_sr=int(_fsr), target_sr=int(sr))
        return _np.ascontiguousarray(y, dtype=float), int(sr)
    except Exception as e:
        last_err = e
    import shutil as _sh
    import subprocess as _sp

    _ff = _sh.which("ffmpeg")
    if not _ff:
        raise RuntimeError(
            f"Не удалось декодировать аудио ({last_err}); ffmpeg не найден"
        ) from last_err
    _cmd = [_ff, "-v", "error"]
    if offset and float(offset) > 0:
        _cmd += ["-ss", f"{float(offset):.3f}"]
    _cmd += ["-i", p]
    if duration:
        _cmd += ["-t", f"{float(duration):.3f}"]
    _cmd += ["-ac", "1", "-ar", str(int(sr)), "-f", "wav", "pipe:1"]
    try:
        _proc = _sp.run(_cmd, stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=180)
    except Exception as e:
        raise RuntimeError(f"ffmpeg не смог декодировать: {e}") from e
    if _proc.returncode != 0 or not _proc.stdout:
        _tail = (_proc.stderr or b"")[-200:]
        raise RuntimeError(f"Не удалось декодировать аудио ({_tail!r})")
    import io as _io

    import soundfile as _sf2

    y, _fsr = _sf2.read(_io.BytesIO(_proc.stdout), always_2d=False)
    y = _np.asarray(y, dtype=float).ravel()
    return _np.ascontiguousarray(y, dtype=float), int(sr)


def _segment_plan(duration_full: Any, sample_seconds: int) -> list[tuple[float, float]]:
    """Куски анализа: 3 × (sample/3) на 15/45/75% длительности.

    Короткие треки или неизвестная длительность — один кусок с начала
    (старое поведение). Суммарно столько же аудио, сколько раньше.
    """
    seg = max(10, int(sample_seconds or 90) // 3)
    try:
        dur = float(duration_full) if duration_full else 0.0
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0 or dur <= seg * 2.5:
        single = min(dur, float(sample_seconds or 90)) if dur > 0 else float(sample_seconds or 90)
        return [(0.0, single)]
    plan = []
    for frac in (0.15, 0.45, 0.75):
        off = min(max(frac * dur - seg / 2.0, 0.0), max(dur - seg, 0.0))
        plan.append((off, float(seg)))
    return plan


def _describe_segment(y, sr: int, librosa, np) -> dict[str, Any]:
    """Сырые измерения одного куска (агрегация — отдельно в _aggregate)."""
    tempo_arr, _beats = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo_arr)[0])

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = [float(v) for v in np.mean(chroma, axis=1)]

    cent = librosa.feature.spectral_centroid(y=y, sr=sr)
    roll = librosa.feature.spectral_rolloff(y=y, sr=sr)
    zcr = librosa.feature.zero_crossing_rate(y)
    rms = librosa.feature.rms(y=y)
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    return {
        "tempo": tempo,
        "chroma_mean": chroma_mean,
        "mfcc_mean": [float(v) for v in np.mean(mfcc, axis=1)],
        "centroid": float(np.mean(cent)),
        "rolloff": float(np.mean(roll)),
        "zcr_m": float(np.mean(zcr)),
        "rms_m": float(np.mean(rms)),
        "onset_m": float(np.mean(onset)),
    }


def _aggregate(raws: list[dict[str, Any]], np) -> dict[str, Any]:
    """Куски -> один трек: tempo медианой (устойчив к брейкдауну),
    остальное средним; key/mood считаются уже от агрегатов."""
    tempos = sorted(float(r["tempo"]) for r in raws)
    tempo = tempos[len(tempos) // 2]
    n_chroma = len(raws[0]["chroma_mean"])
    n_mfcc = len(raws[0]["mfcc_mean"])
    chroma_mean = [float(np.mean([r["chroma_mean"][i] for r in raws]))
                   for i in range(n_chroma)]
    mfcc_mean = [float(np.mean([r["mfcc_mean"][i] for r in raws]))
                 for i in range(n_mfcc)]
    return {
        "tempo": tempo,
        "chroma_mean": chroma_mean,
        "mfcc_mean": mfcc_mean,
        "centroid": float(np.mean([r["centroid"] for r in raws])),
        "rolloff": float(np.mean([r["rolloff"] for r in raws])),
        "zcr_m": float(np.mean([r["zcr_m"] for r in raws])),
        "rms_m": float(np.mean([r["rms_m"] for r in raws])),
        "onset_m": float(np.mean([r["onset_m"] for r in raws])),
    }


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
    """Проанализировать аудиофайл. Всё ниже — измерения реального сигнала.

    Три куска (начало/середина/конец, суммарно sample_seconds) вместо одного
    вступления: оценка устойчива к длинным интро и скитам.
    """
    librosa, np = _require_librosa()
    path = str(path)
    if not os.path.isfile(path):
        raise RuntimeError(f"Файл не найден: {path}")

    duration_full = track_duration_sec or None
    try:
        _gd = get_audio_duration(path)
        if _gd:
            duration_full = _gd
    except Exception:
        pass

    # Жёсткий фильтр длинных треков: миксы/сборники/подкасты (Peyton Parrish —
    # Animals 57с анализа, а 2-часовые висят до упора). До декодирования, чтобы
    # не жечь CPU. Пометка skip_long_track видна в логах задачи.
    try:
        from app.core.config import get_settings as _gs

        _max_dur = int(_gs().analysis_max_duration_sec or 600)
    except Exception:
        _max_dur = 600
    if duration_full and _max_dur > 0 and float(duration_full) > _max_dur:
        raise RuntimeError(
            f"skip_long_track: длительность {int(round(float(duration_full)))}с "
            f"> лимита {_max_dur}с — пропуск без анализа")

    plan = _segment_plan(duration_full, sample_seconds)
    raws = []
    for _off, _len in plan:
        try:
            y, sr = load_audio_mono(path, sr=22050, offset=_off, duration=_len)
        except Exception:
            continue
        if y is None or len(y) < sr:
            continue
        raws.append(_describe_segment(y, sr, librosa, np))
    if not raws:
        raise RuntimeError("Не удалось декодировать аудио (меньше 1 секунды)")

    agg = _aggregate(raws, np)
    tempo = agg["tempo"]
    chroma_mean = agg["chroma_mean"]
    mfcc_mean = agg["mfcc_mean"]
    key_name, scale = estimate_key(chroma_mean)

    centroid = agg["centroid"]
    rolloff = agg["rolloff"]
    zcr_m = agg["zcr_m"]
    rms_m = agg["rms_m"]
    onset_m = agg["onset_m"]

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
