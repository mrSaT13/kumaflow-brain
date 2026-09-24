#!/usr/bin/env python
"""Скачать CLAP ONNX для CPU (build-cache).

Источник: Xenova/clap-htsat-unfused (ONNX-экспорт LAION CLAP, quantized):
  onnx/text_model_quantized.onnx (~127МБ) -> clap_text.onnx
  onnx/audio_model_quantized.onnx (~34МБ)  -> clap_audio.onnx
  + tokenizer.json, config.json (+ опционально vocab/merges для токенайзера)

Почему не laion/*: в laion/larger_clap_general и laion/clap-htsat-unfused
лежат только pytorch_model.bin (pickle) — ONNX там нет, bake молча
пропускался и образ выходил того же размера (GHCR 1.79GiB дважды).

Два пути скачивания (с фолбеком):
  1. snapshot_download (нужен huggingface_hub>=0.30 + hf-xet: Xenova живёт на XET-CAS);
  2. пофайловый hf_hub_download (тоже умеет XET);
  3. прямой HTTPS resolve/main (последний шанс, для не-XET файлов).

Запуск: python -m ml.download_clap  (или python ml/download_clap.py)
Кэш: повторный запуск — skip если файлы уже есть и размер совпадает.
Env: HF_TOKEN / HUGGINGFACE_HUB_TOKEN optional для rate-limit.
Выход: 0 — всё на месте; 1 — модели нет (сборка Docker должна УПАСТЬ,
а не запекать пустой образ).
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback
import urllib.request
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
# Нефатально, но помогает transformers-токенайзеру (BPE artefacts).
OPTIONAL = ["vocab.json", "merges.txt", "tokenizer_config.json",
            "special_tokens_map.json", "preprocessor_config.json"]


def _ok() -> bool:
    return all((DST / name).exists() and (DST / name).stat().st_size > 1024
               for name in WANT.values())


def _report_state(tag: str) -> None:
    print(f"[clap:{tag}] cwd={os.getcwd()} dst={DST}")
    for name in list(WANT.values()) + OPTIONAL:
        p = DST / name
        print(f"[clap:{tag}]   {name}: {'OK ' + str(p.stat().st_size) if p.exists() else 'MISSING'}")


if _ok():
    print(f"[clap] already in {DST} — skip download")
    sys.exit(0)

token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or None

# --- путь 1: snapshot_download (быстро, одним архивом; нужен hf-xet) ---
snap_ok = False
try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[clap] huggingface_hub not found — pip install huggingface_hub")
    traceback.print_exc()
    snapshot_download = None  # type: ignore

if snapshot_download is not None:
    print(f"[clap] try snapshot_download {MODEL} (~165MB quantized)...")
    try:
        snap = snapshot_download(
            repo_id=MODEL,
            local_dir=str(DST),
            local_dir_use_symlinks=False,
            token=token,
            allow_patterns=["onnx/text_model_quantized.onnx",
                            "onnx/audio_model_quantized.onnx",
                            "tokenizer.json", "config.json",
                            "vocab.json", "merges.txt",
                            "tokenizer_config.json",
                            "special_tokens_map.json",
                            "preprocessor_config.json"],
        )
        for src_rel, dst_name in WANT.items():
            cand = DST / src_rel
            alt = Path(snap) / src_rel
            src = cand if cand.exists() else alt
            dst = DST / dst_name
            if src.exists() and src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
                print(f"[clap] {src_rel} -> {dst_name} ({dst.stat().st_size // 1048576}MB)")
        snap_ok = _ok()
        print(f"[clap] snapshot path _ok={snap_ok}")
    except Exception:
        print("[clap] WARN: snapshot_download failed, fallback to plain HTTPS:")
        traceback.print_exc()
else:
    print("[clap] WARN: no snapshot_download, using plain HTTPS directly")


# --- путь 2: пофайловый hf_hub_download (умеет XET-подписи/редиректы) ---
# ВАЖНО: сырой urllib на /resolve/main/ для XET-репо отдаёт поинтер или 404,
# поэтому фолбек обязан идти через huggingface_hub, а не через plain HTTPS.
def _hub_fetch(repo_path: str, dst: Path, tries: int = 3) -> bool:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("[clap] WARN: no hf_hub_download, will try plain HTTPS")
        return False
    for attempt in range(1, tries + 1):
        try:
            got = hf_hub_download(repo_id=MODEL, filename=repo_path, token=token)
            src = Path(got)
            if src.exists() and src.stat().st_size > 1024:
                shutil.copy2(src, dst)
                print(f"[clap] hub {repo_path} -> {dst.name} "
                      f"({dst.stat().st_size // 1048576}MB, try {attempt})")
                return True
            print(f"[clap] hub {repo_path}: tiny file, retry {attempt}")
        except Exception as e:  # noqa: BLE001
            print(f"[clap] hub {repo_path} try {attempt} failed: {e}")
    print(f"[clap] hub {repo_path} FAILED after {tries} tries")
    return False


# --- путь 3: прямой HTTPS (последний шанс, для не-XET файлов) ---
def _http_fetch(repo_path: str, dst: Path, tries: int = 3) -> bool:
    url = f"https://huggingface.co/{MODEL}/resolve/main/{repo_path}"
    headers = {"User-Agent": "KumaFlowBrain/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_err: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
                shutil.copyfileobj(r, f, length=1024 * 256)
            if dst.stat().st_size > 1024:
                print(f"[clap] http {repo_path} -> {dst.name} "
                      f"({dst.stat().st_size // 1048576}MB, try {attempt})")
                return True
            print(f"[clap] http {repo_path}: tiny file, retry {attempt}")
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[clap] http {repo_path} try {attempt} failed: {e}")
    if last_err is not None:
        print(f"[clap] http {repo_path} FAILED after {tries} tries: {last_err}")
    return False


if not _ok():
    print("[clap] fallback: per-file download via hub, then plain HTTPS...")
    for src_rel, dst_name in WANT.items():
        dst = DST / dst_name
        if dst.exists() and dst.stat().st_size > 1024:
            continue
        if _hub_fetch(src_rel, dst):
            continue
        _http_fetch(src_rel, dst)
    for extra in OPTIONAL:
        dst = DST / extra
        if not dst.exists() or dst.stat().st_size <= 1024:
            try:
                _http_fetch(extra, dst, tries=1)
            except Exception:
                pass

onnxs = list(DST.rglob("*.onnx"))
print(f"[clap] onnx in {DST}: {[(p.name, p.stat().st_size) for p in onnxs]}")
_report_state("final")
if not _ok():
    print("[clap] FATAL: после скачивания нет всех файлов.")
    sys.exit(1)
print("[clap] OK — text+audio+tokenizer на месте")
