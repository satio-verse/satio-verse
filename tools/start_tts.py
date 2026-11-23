"""按 config 的 tts.http.server 启动 GPT-SoVITS 的 api_v2 (整合包 runtime/gpt-sovits), 等端口就绪后保持前台运行, Ctrl+C 退出.

  python tools/start_tts.py                    # 端口取 tts.http.base_url (默认 9880), 权重取 tts.http.server
  python tools/start_tts.py --port 9881        # 另开一个试听/测试用的, 不碰主服务用的那个
  python tools/start_tts.py --base             # 不加载微调权重, 用底模 (零样本对照)

服务端 (python -m wm --tts gpt_sovits) 每个分句 POST 一次 /tts, 参考音和文本在 tts.http.ref_audio_path / prompt_text.
api_v2 换权重时会把当前权重写回 -c 给的 yaml, 所以 yaml 每次按配置重新生成在整合包的 TEMP/ 里, 不手改.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wm.config import load_config, resolve_path  # noqa: E402

BASE = {"v2Pro": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
        "v2ProPlus": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"}


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, help="默认取 tts.http.base_url 的端口")
    ap.add_argument("--base", action="store_true", help="用底模, 不加载 tts.http.server 里的微调权重")
    ap.add_argument("--gpt", help="临时换 GPT 权重 (.ckpt), 默认取 tts.http.server.gpt")
    ap.add_argument("--sovits", help="临时换 SoVITS 权重 (.pth), 默认取 tts.http.server.sovits")
    ap.add_argument("--cpu", action="store_true", help="不用显卡 (很慢, 只用来排查)")
    ap.add_argument("--print-only", action="store_true", help="只打印命令和生成的 yaml")
    args = ap.parse_args()

    h = load_config()["tts"]["http"]
    s = h["server"]
    gsv = resolve_path(s["dir"]).resolve()
    port = args.port or urlparse(h["base_url"]).port or 9880
    version = s.get("version", "v2ProPlus")
    gpt = "GPT_SoVITS/pretrained_models/s1v3.ckpt" if args.base else str(resolve_path(args.gpt or s["gpt"]))
    sovits = BASE[version] if args.base else str(resolve_path(args.sovits or s["sovits"]))
    py = gsv / "runtime" / "python.exe"
    missing = [str(p) for p in (py, gsv / gpt, gsv / sovits) if not p.exists()]
    if missing and not args.print_only:
        sys.exit("找不到: " + ", ".join(missing) + "\n整合包见 README「声音克隆」一节, 权重用 tools/train_gsv.py 训练")

    yaml_path = gsv / "TEMP" / f"wm-tts-{port}.yaml"
    yaml_text = "\n".join([
        "custom:",
        "  bert_base_path: GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "  cnhuhbert_base_path: GPT_SoVITS/pretrained_models/chinese-hubert-base",
        f"  device: {'cpu' if args.cpu else 'cuda'}",
        f"  is_half: {'false' if args.cpu else 'true'}",
        f"  t2s_weights_path: {Path(gpt).as_posix()}",
        f"  version: {version}",
        f"  vits_weights_path: {Path(sovits).as_posix()}", ""])
    cmd = [str(py), "-s", "api_v2.py", "-a", "127.0.0.1", "-p", str(port), "-c", str(yaml_path)]
    print(" ".join(cmd))
    if args.print_only:
        print(yaml_text)
        return
    yaml_path.parent.mkdir(exist_ok=True)
    yaml_path.write_text(yaml_text, encoding="utf-8")
    env = dict(os.environ)
    # 本机 CUDA_PATH 指向没有 cuDNN 的 CUDA 12.6, 整合包的 onnxruntime-gpu (G2PW) 会去那里加载并报错; 去掉后 G2PW 走 CPU.
    env.pop("CUDA_PATH", None)
    proc = subprocess.Popen(cmd, cwd=gsv, env=env)
    try:
        if not wait_ready(port, proc):
            sys.exit(f"GPT-SoVITS 没有起来 (退出码 {proc.poll()})")
        print(f"GPT-SoVITS 已就绪: http://127.0.0.1:{port}/tts  ({version}, {'底模' if args.base else Path(sovits).name})")
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
