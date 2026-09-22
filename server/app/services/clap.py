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


def get_audio_embedding(path: str | Path, dim: int = 512) -> list[float] | None:
    """Аудио -> эмбеддинг (если audio onnx есть). Пока stub — возвращает None."""
    p = _find_audio_onnx()
    if not p or not Path(path).exists():
        return None
    # TODO: реализовать когда понадобится (librosa 48k 10s -> mel -> onnx)
    return None
