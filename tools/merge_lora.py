"""把 LoRA 合并进 Qwen3.5-0.8B 原始权重, 再转成 GGUF 并量化成 Q8_0 (在 runtime/train-venv 里跑).

推理时挂 LoRA (llama-server --lora) 每一层要多算一次低秩乘法, 首句延迟多约 20 ms; 合并后和原模型一样快.

  runtime/train-venv/Scripts/python tools/merge_lora.py models/train/lora-v2
  -> models/train/lora-v2/merged/            合并后的权重 (safetensors, 其余文件原样复制)
  -> models/llm-lora/Qwen3.5-0.8B-lora-v2-Q8_0.gguf

合并: 原始权重里语言模型的线性层 W 换成 W + (alpha / r) * B @ A, 按 float32 算完再转回原来的 bf16.
适配器的键 "base_model.model.model.layers.N.X.lora_A.weight" 对应原始权重的 "model.language_model.layers.N.X.weight".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "models" / "train" / "Qwen3.5-0.8B"
CONVERT = ROOT / "runtime" / "llama-convert"
QUANTIZE = ROOT / "runtime" / "llama" / "llama-quantize.exe"


def merge(adapter: Path, out: Path) -> int:
    cfg = json.loads((adapter / "adapter_config.json").read_text(encoding="utf-8"))
    scale = cfg["lora_alpha"] / cfg["r"]
    lora = load_file(adapter / "adapter_model.safetensors")
    shard = next(BASE.glob("model.safetensors-*.safetensors"))
    weights = load_file(shard)
    pre, done = "base_model.model.model.", 0
    for k in [k for k in lora if k.endswith(".lora_A.weight")]:
        name = k[len(pre):-len(".lora_A.weight")]
        target = f"model.language_model.{name}.weight"
        if target not in weights:
            sys.exit(f"原始权重里没有 {target}")
        a, b = lora[k].float(), lora[k.replace("lora_A", "lora_B")].float()
        w = weights[target]
        weights[target] = (w.float() + scale * (b @ a)).to(w.dtype)
        done += 1
    out.mkdir(parents=True, exist_ok=True)
    for f in BASE.iterdir():  # 配置、分词、许可等原样复制
        if f.is_file() and f != shard:
            shutil.copy2(f, out / f.name)
    save_file(weights, out / shard.name, metadata={"format": "pt"})
    return done


def main() -> None:
    adapter = ROOT / sys.argv[1]
    merged = adapter / "merged"
    n = merge(adapter, merged)
    print(f"合并了 {n} 个线性层 -> {merged}")
    name = f"Qwen3.5-0.8B-{adapter.name}"
    dst = ROOT / "models" / "llm-lora"
    dst.mkdir(parents=True, exist_ok=True)
    f16 = dst / f"{name}-F16.gguf"
    env = {**os.environ, "PYTHONPATH": str(CONVERT / "gguf-py")}
    subprocess.check_call([sys.executable, str(CONVERT / "convert_hf_to_gguf.py"), str(merged), "--outtype", "f16",
                           "--outfile", str(f16)], env=env)
    q8 = dst / f"{name}-Q8_0.gguf"
    subprocess.check_call([str(QUANTIZE), str(f16), str(q8), "Q8_0"], cwd=str(QUANTIZE.parent))
    f16.unlink()
    print(f"已写入 {q8}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
