#!/usr/bin/env python
"""Скачать CLAP ONNX для CPU (build-cache).

Модель: laion/larger_clap_general (HTSAT-unfused, 512-dim, ~350MB).
Кладёт в server/ml/models/: clap_text.onnx, clap_audio.onnx, tokenizer.json, config.json
Запуск: python ml/download_clap.py  (или в Dockerfile.server RUN python ml/download_clap.py)
Кэш: повторный запуск — skip если файлы уже есть и размер совпадает.
Env: HF_TOKEN / HUGGINGFACE_HUB_TOKEN optional для приватных/rate-limit.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MODEL = "laion/larger_clap_general"
DST = Path(__file__).parent / "models"
DST.mkdir(parents=True, exist_ok=True)

NEED = ["clap_text.onnx", "clap_audio.onnx", "tokenizer.json", "config.json"]

def _has_all() -> bool:
    return all((DST / f).exists() and (DST / f).stat().st_size > 1024 for f in NEED)

if _has_all():
    print(f"[clap] already in {DST} — skip download")
    sys.exit(0)

try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[clap] huggingface_hub not found — pip install huggingface_hub")
    sys.exit(1)

token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or None
print(f"[clap] downloading {MODEL} -> {DST} (this may take a few minutes, ~350MB)...")
# allow_patterns у snapshot_download поддерживает fnmatch
# У laion/larger_clap_general ONNX лежит как *onnx* + tokenizer
try:
    snapshot_download(
        repo_id=MODEL,
        local_dir=str(DST),
        local_dir_use_symlinks=False,
        token=token,
        allow_patterns=["*.onnx", "*.json", "*.txt", "*.model"],
        # игнор больших .bin/.safetensors (PyTorch) — экономим трафик
        ignore_patterns=["*.safetensors", "*.bin", "*.pt", "*.ckpt"],
    )
except TypeError:
    # старые версии без allow_patterns/ignore_patterns
    snapshot_download(repo_id=MODEL, local_dir=str(DST), local_dir_use_symlinks=False, token=token)

# проверка: найти onnx
onnxs = list(DST.rglob("*.onnx"))
print(f"[clap] done, found {len(onnxs)} onnx: {[p.name for p in onnxs[:5]]}")
for f in NEED:
    p = DST / f
    if not p.exists():
        # пробуем найти рекурсивно и скопировать наверх
        found = next(DST.rglob(f), None)
        if found and found != p:
            import shutil
            shutil.copy2(found, p)
            print(f"[clap] copied {found} -> {p}")
if _has_all():
    print("[clap] OK — all files present")
else:
    print(f"[clap] WARN — some files missing in {DST}:")
    for f in NEED:
        p = DST / f
        print(f"  {f}: {'OK '+str(p.stat().st_size) if p.exists() else 'MISSING'}")
    # не фатально — text encoder может работать без audio
