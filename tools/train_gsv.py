"""用 GPT-SoVITS 整合包 (runtime/gpt-sovits) 微调一个音色: 准备数据 → SoVITS → GPT, 和 WebUI 一键三连 + 1Ba + 1Bb 等价.

  python tools/train_gsv.py models/tts/aimisi/dataset/aimisi.list --name aimisi --out models/tts/aimisi
  python tools/train_gsv.py ... --steps sovits,gpt --sovits-epochs 12   # 只重训, 不重新准备数据

.list 由 tools/voice_dataset.py 生成. 中间文件在 runtime/gpt-sovits/logs/<name>/, 各轮权重在
runtime/gpt-sovits/{SoVITS,GPT}_weights_<version>/, --out 给了就把这次的权重拷到 <out>/weights/.
训练前自己看一眼显存: 别的会话 (LLM LoRA 训练) 在用 GPU 时不要跑.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wm.config import resolve_path  # noqa: E402

PRETRAINED = "GPT_SoVITS/pretrained_models"
S2G = {"v2Pro": f"{PRETRAINED}/v2Pro/s2Gv2Pro.pth", "v2ProPlus": f"{PRETRAINED}/v2Pro/s2Gv2ProPlus.pth"}
S2D = {"v2Pro": f"{PRETRAINED}/v2Pro/s2Dv2Pro.pth", "v2ProPlus": f"{PRETRAINED}/v2Pro/s2Dv2ProPlus.pth"}
S1 = f"{PRETRAINED}/s1v3.ckpt"


def run(gsv: Path, script: str, env: dict, *args: str) -> None:
    cmd = [str(gsv / "runtime" / "python.exe"), "-s", script, *args]
    print(">>", " ".join(cmd[2:]), flush=True)
    t0 = time.time()
    full = {**os.environ, **env}
    # 本机 CUDA_PATH 指向 CUDA 12.6 (没有 cuDNN), 整合包的 onnxruntime-gpu 会去那里加载然后直接报错, 文本步骤全军覆没.
    # 去掉后 G2PW 退回 CPU (只是多音字预测, 很快), torch 用自带的 CUDA 不受影响.
    full.pop("CUDA_PATH", None)
    r = subprocess.run(cmd, cwd=gsv, env=full)
    if r.returncode:
        raise SystemExit(f"{script} 失败 (退出码 {r.returncode})")
    print(f"<< {script} {time.time() - t0:.0f} s", flush=True)


def free_vram_mb() -> str:
    try:
        return subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return "?"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("list", help="训练列表 (wav 路径|说话人|ZH|文本)")
    ap.add_argument("--name", required=True, help="实验名, 也是权重文件名前缀")
    ap.add_argument("--gsv", default="runtime/gpt-sovits", help="整合包目录")
    ap.add_argument("--version", default="v2ProPlus", choices=sorted(S2G))
    ap.add_argument("--steps", default="prep,sovits,gpt")
    ap.add_argument("--batch", type=int, default=2, help="SoVITS 和 GPT 的 batch size (6 GB 卡、和 LLM 共用时用 2)")
    ap.add_argument("--sovits-epochs", type=int, default=12)
    ap.add_argument("--sovits-save-every", type=int, default=4)
    ap.add_argument("--gpt-epochs", type=int, default=15)
    ap.add_argument("--gpt-save-every", type=int, default=5)
    ap.add_argument("--out", help="把这次训练出的权重拷到 <out>/weights/")
    args = ap.parse_args()

    gsv, inp = resolve_path(args.gsv).resolve(), resolve_path(args.list).resolve()
    steps = set(args.steps.split(","))
    exp = gsv / "logs" / args.name
    exp.mkdir(parents=True, exist_ok=True)
    for d in (f"SoVITS_weights_{args.version}", f"GPT_weights_{args.version}"):  # WebUI 启动时才建, 没有的话存权重会失败
        (gsv / d).mkdir(exist_ok=True)
    env = {"version": args.version, "is_half": "True", "_CUDA_VISIBLE_DEVICES": "0", "hz": "25hz",
           "inp_text": str(inp), "inp_wav_dir": str(inp.parent / "wavs"), "exp_name": args.name,
           "opt_dir": str(exp), "i_part": "0", "all_parts": "1"}
    print(f"空闲显存 {free_vram_mb()}", flush=True)

    if "prep" in steps:
        run(gsv, "GPT_SoVITS/prepare_datasets/1-get-text.py",
            {**env, "bert_pretrained_dir": f"{PRETRAINED}/chinese-roberta-wwm-ext-large"})
        (exp / "2-name2text-0.txt").replace(exp / "2-name2text.txt")
        hubert_env = {**env, "cnhubert_base_dir": f"{PRETRAINED}/chinese-hubert-base",
                      "sv_path": f"{PRETRAINED}/sv/pretrained_eres2netv2w24s4ep4.ckpt"}
        run(gsv, "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py", hubert_env)
        run(gsv, "GPT_SoVITS/prepare_datasets/2-get-sv.py", hubert_env)
        run(gsv, "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
            {**env, "pretrained_s2G": S2G[args.version], "s2config_path": f"GPT_SoVITS/configs/s2{args.version}.json"})
        part = exp / "6-name2semantic-0.tsv"
        (exp / "6-name2semantic.tsv").write_text("item_name\tsemantic_audio\n" + part.read_text(encoding="utf-8"),
                                                encoding="utf-8")
        part.unlink()

    started = time.time()
    if "sovits" in steps:
        data = json.loads((gsv / f"GPT_SoVITS/configs/s2{args.version}.json").read_text(encoding="utf-8"))
        data["train"].update({
            "batch_size": args.batch, "epochs": args.sovits_epochs, "text_low_lr_rate": 0.4,
            "pretrained_s2G": S2G[args.version], "pretrained_s2D": S2D[args.version],
            "if_save_latest": True, "if_save_every_weights": True, "save_every_epoch": args.sovits_save_every,
            "gpu_numbers": "0", "grad_ckpt": False, "lora_rank": "32"})
        data["model"]["version"] = data["version"] = args.version
        data["data"]["exp_dir"] = data["s2_ckpt_dir"] = str(exp)
        data["save_weight_dir"] = f"SoVITS_weights_{args.version}"
        data["name"] = args.name
        (exp / f"logs_s2_{args.version}").mkdir(exist_ok=True)
        cfg = exp / "tmp_s2.json"
        cfg.write_text(json.dumps(data), encoding="utf-8")
        run(gsv, "GPT_SoVITS/s2_train.py", env, "--config", str(cfg))

    if "gpt" in steps:
        import yaml

        data = yaml.safe_load((gsv / "GPT_SoVITS/configs/s1longer-v2.yaml").read_text(encoding="utf-8"))
        data["train"].update({
            "batch_size": args.batch, "epochs": args.gpt_epochs, "save_every_n_epoch": args.gpt_save_every,
            "if_save_every_weights": True, "if_save_latest": True, "if_dpo": False,
            "half_weights_save_dir": f"GPT_weights_{args.version}", "exp_name": args.name})
        data["pretrained_s1"] = S1
        data["train_semantic_path"] = str(exp / "6-name2semantic.tsv")
        data["train_phoneme_path"] = str(exp / "2-name2text.txt")
        data["output_dir"] = str(exp / f"logs_s1_{args.version}")
        cfg = exp / "tmp_s1.yaml"
        cfg.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        run(gsv, "GPT_SoVITS/s1_train.py", env, "--config_file", str(cfg))

    if args.out:
        dst = resolve_path(args.out) / "weights"
        dst.mkdir(parents=True, exist_ok=True)
        for d in (f"SoVITS_weights_{args.version}", f"GPT_weights_{args.version}"):
            for f in (gsv / d).glob(f"{args.name}*"):
                if f.stat().st_mtime >= started - 1:
                    shutil.copy2(f, dst / f.name)
                    print("拷贝", dst / f.name)


if __name__ == "__main__":
    main()
