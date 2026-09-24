"""CLAP text/audio эмбеддинги CPU ONNX (laion/larger_clap_general, 512-dim).

Если файлов нет — всё тихо fallback в keyword (не падает).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("clap")

MODELS_DIR = Path(__file__).resolve().parents[2] / "ml" / "models"
TEXT_ONNX_CANDIDATES = [MODELS_DIR / "clap_text.onnx", MODELS_DIR / "text_model.onnx", *list(MODELS_DIR.rglob("*.onnx"))]
AUDIO_ONNX_CANDIDATES = [MODELS_DIR / "clap_audio.onnx", MODELS_DIR / "audio_model.onnx"]

_sess_text = None
_sess_audio = None
_tokenizer = None


def _find_text_onnx() -> Path | None:
    # ищем text onnx приоритетно с "text" в имени
    for p in TEXT_ONNX_CANDIDATES:
        if p.exists() and "text" in p.name.lower():
            return p
    for p in MODELS_DIR.rglob("*.onnx"):
        if "text" in p.name.lower():
            return p
    # иначе первый onnx
    for p in MODELS_DIR.rglob("*.onnx"):
        return p
    return None


def _find_audio_onnx() -> Path | None:
    for p in AUDIO_ONNX_CANDIDATES:
        if p.exists():
            return p
    for p in MODELS_DIR.rglob("*.onnx"):
        if "audio" in p.name.lower():
            return p
    return None


def is_available() -> bool:
    try:
        s = get_settings()
        if not s.clap_enabled:
            return False
    except Exception:
        pass
    p = _find_text_onnx()
    return bool(p and p.exists() and p.stat().st_size > 1024)


def _get_tokenizer():
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    # пробуем transformers AutoTokenizer если есть tokenizer.json
    try:
        from transformers import AutoTokenizer  # type: ignore

        # local tokenizer.json рядом
        tok_path = MODELS_DIR / "tokenizer.json"
        if tok_path.exists():
            _tokenizer = AutoTokenizer.from_pretrained(str(MODELS_DIR), local_files_only=True, trust_remote_code=True)
        else:
            _tokenizer = AutoTokenizer.from_pretrained("laion/larger_clap_general", trust_remote_code=True)
        return _tokenizer
    except Exception as e:
        logger.warning("clap tokenizer not available: {}", e)
        return None


def _get_text_session():
    global _sess_text
    if _sess_text is not None:
        return _sess_text
    p = _find_text_onnx()
    if not p:
        return None
    try:
        import onnxruntime as ort  # type: ignore

        _sess_text = ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])
        logger.info("clap text session loaded: {}", p)
        return _sess_text
    except Exception as e:
        logger.warning("clap text session failed: {}", e)
        return None


def get_text_embedding(q: str, dim: int = 512) -> list[float] | None:
    """Текст -> 512-дим нормализованный вектор. None = fallback."""
    if not q or not q.strip():
        return None
    if not is_available():
        return None
    q = q.strip()[:200]
    # 1. пробуем onnx
    try:
        sess = _get_text_session()
        tok = _get_tokenizer()
        if sess is not None and tok is not None:
            enc = tok(q, padding="max_length", truncation=True, max_length=77, return_tensors="np")
            # разные экспорты: input_ids / attention_mask
            ort_inputs = {}
            for inp in sess.get_inputs():
                name = inp.name
                if "input_ids" in name and "input_ids" in enc:
                    ort_inputs[name] = enc["input_ids"].astype(np.int64)
                elif "attention_mask" in name and "attention_mask" in enc:
                    ort_inputs[name] = enc["attention_mask"].astype(np.int64)
            if not ort_inputs:
                # fallback: первый инпут
                ort_inputs[sess.get_inputs()[0].name] = enc["input_ids"].astype(np.int64)
            out = sess.run(None, ort_inputs)
            vec = np.array(out[0][0], dtype=np.float32)
            n = float(np.linalg.norm(vec))
            if n > 0:
                vec = vec / n
            # усечь/пад до dim
            if vec.shape[0] != dim:
                if vec.shape[0] > dim:
                    vec = vec[:dim]
                else:
                    vec = np.pad(vec, (0, dim - vec.shape[0]))
                n2 = float(np.linalg.norm(vec))
                if n2 > 0:
                    vec = vec / n2
            return vec.astype(np.float32).tolist()
    except Exception as e:
        logger.warning("clap text embedding failed (onnx): {}", e)
    # 2. fallback — хеш-эмбеддинг (детерминированный, лучше чем keyword но без семантики)
    try:
        import hashlib

        h = hashlib.sha256(q.lower().encode("utf-8")).digest()
        # растягиваем до dim
        rep = (h * ((dim // len(h)) + 1))[:dim]
        vec = np.frombuffer(rep, dtype=np.uint8).astype(np.float32) / 255.0
        vec = vec - 0.5
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec = vec / n
        return vec.tolist()
    except Exception:
        return None


def is_audio_available() -> bool:
    """Аудио-ONNX доступен и фича включена (opt-in, динозавр-friendly)."""
    try:
        s = get_settings()
        if not getattr(s, "clap_audio_enabled", False):
            return False
    except Exception:
        return False
    p = _find_audio_onnx()
    return bool(p and p.exists() and p.stat().st_size > 1024)


def _get_audio_session():
    global _sess_audio
    if _sess_audio is not None:
        return _sess_audio
    p = _find_audio_onnx()
    if not p:
        return None
    try:
        import onnxruntime as ort  # type: ignore

        try:
            threads = int(getattr(get_settings(), "clap_audio_threads", 1) or 1)
        except Exception:
            threads = 1
        threads = max(1, min(4, threads))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        _sess_audio = ort.InferenceSession(
            str(p), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        logger.info("clap audio session loaded: {} threads={}", p, threads)
        return _sess_audio
    except Exception as e:
        logger.warning("clap audio session failed: {}", e)
        return None


def get_audio_embedding(path: str | Path, dim: int = 512) -> list[float] | None:
    """Аудио -> нормализованный эмбеддинг CPU ONNX. None = фолбек на librosa.

    Динозавр-friendly: opt-in флагом, 1 поток, ~10с моно 22кГц, ошибок наружу нет.
    Без модели/флага — тихо None, скоринг идет по старым 9 фичам 1в1.
    """
    if not is_audio_available():
        return None
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        sess = _get_audio_session()
        if sess is None:
            return None
        import librosa  # type: ignore

        # Легкий вход: моно 22050, первые 10с (не весь трек — быстро и хватает
        # для тембра/грува; librosa и так уже грузила 90с в analyze_file).
        y, _ = librosa.load(str(p), sr=22050, mono=True, duration=10.0)
        if y is None or len(y) < 22050:
            return None
        # log-mel 64 полосы. Раскладку подгоняем под вход модели:
        # Xenova audio_model ждёт [B, T, M], иные экспорты — NCHW.
        mel = librosa.feature.melspectrogram(y=y, sr=22050, n_mels=64)
        mel = np.log1p(np.maximum(mel, 0)).astype(np.float32)  # [M, T]
        ort_inputs = {}
        try:
            inputs = list(sess.get_inputs())
            first = inputs[0]
            dims = [int(d) if isinstance(d, int) else -1
                    for d in (first.shape or [])]
        except Exception:
            inputs, first, dims = [], None, []
        try:
            if first is not None and len(dims) == 3:
                ort_inputs[first.name] = mel.T[np.newaxis, :, :]  # [1, T, M]
            elif first is not None and len(dims) == 4:
                ort_inputs[first.name] = mel[np.newaxis, np.newaxis, :, :]  # NCHW
            elif first is not None:
                ort_inputs[first.name] = mel.T[np.newaxis, :, :]
            else:
                return None
        except Exception:
            return None
        out = sess.run(None, ort_inputs)
        vec = np.array(out[0].reshape(-1), dtype=np.float32)
        n = float(np.linalg.norm(vec))
        if n <= 0:
            return None
        vec = vec / n
        if vec.shape[0] != dim:
            if vec.shape[0] > dim:
                vec = vec[:dim]
            else:
                vec = np.pad(vec, (0, dim - vec.shape[0]))
            n2 = float(np.linalg.norm(vec))
            if n2 > 0:
                vec = vec / n2
        return vec.astype(np.float32).tolist()
    except Exception as e:
        logger.warning("clap audio embedding failed (fallback librosa): {}", e)
        return None
