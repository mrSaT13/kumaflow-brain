#!/usr/bin/env python
"""Скачать CLAP ONNX для CPU (build-cache).

Источник: Xenova/clap-htsat-unfused (ONNX-экспорт LAION CLAP, quantized):
  onnx/text_model_quantized.onnx (~127МБ) -> clap_text.onnx
  onnx/audio_model_quantized.onnx (~34МБ)  -> clap_audio.onnx
  + tokenizer.json, config.json

Почему не laion/*: в laion/larger_clap_general и laion/clap-htsat-unfused
лежат только pytorch_model.bin (pickle) — ONNX там нет, bake молча
пропускался и образ выходил того же размера (см. GHCR 1.79GiB дважды).

Запуск: python ml/download_clap.py  (или в Dockerfile.server)
Кэш: повторный запуск — skip если файлы уже есть и размер совпадает.
Env: HF_TOKEN / HUGGINGFACE_HUB_TOKEN optional для rate-limit.
Выход: 0 — всё на месте; 1 — модели нет (сборка Docker должна УПАСТЬ,
а не запекать пустой образ).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

MODEL = "Xenova/clap-htsat-unfused"
DST = Path(__file__).parent / "models"
DST.mkdir(parents=True, exist_ok=True)

# (путь в репо -> каноническое имя у нас)
WANT = {
    "onnx/text_model_quantized.onnx": "clap_text.onnx",
    "onnx/audio_model_quantized.onnx": "clap_audio.onnx",
    "tokenizer.json": "tokenizer.json",
    "config.json": "config.json",
}


def _ok() -> bool:
    return all((DST / name).exists() and (DST / name).stat().st_size > 1024
               for name in WANT.values())


if _ok():
    print(f"[clap] already in {DST} — skip download")
    sys.exit(0)

try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[clap] FATAL: huggingface_hub not found — pip install huggingface_hub")
    sys.exit(1)

token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or None
print(f"[clap] downloading {MODEL} -> {DST} (~165MB quantized)...")
try:
    snap = snapshot_download(
        repo_id=MODEL,
        local_dir=str(DST),
        local_dir_use_symlinks=False,
        token=token,
        allow_patterns=["onnx/text_model_quantized.onnx",
                        "onnx/audio_model_quantized.onnx",
                        "tokenizer.json", "config.json"],
    )
except Exception as e:
    print(f"[clap] FATAL: snapshot_download failed: {e}")
    sys.exit(1)

# раскладываем в канонические имена (snapshot кладёт с подпапкой onnx/)
for src_rel, dst_name in WANT.items():
    src = Path(snap) / src_rel if not (DST / src_rel).exists() else (DST / src_rel)
    dst = DST / dst_name
    if src.exists() and src != dst:
        shutil.copy2(src, dst)
        print(f"[clap] {src_rel} -> {dst_name} ({dst.stat().st_size // 1048576}MB)")

onnxs = list(DST.rglob("*.onnx"))
print(f"[clap] onnx in {DST}: {[(p.name, p.stat().st_size) for p in onnxs]}")
if not _ok():
    print("[clap] FATAL: после скачивания нет всех файлов:")
    for name in WANT.values():
        p = DST / name
        print(f"  {name}: {'OK ' + str(p.stat().st_size) if p.exists() else 'MISSING'}")
    sys.exit(1)
print("[clap] OK — text+audio+tokenizer на месте")
