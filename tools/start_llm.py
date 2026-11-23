"""按 config 的 llm.server 启动 llama-server, 等到 /health 就绪后保持前台运行, Ctrl+C 退出.

  python tools/start_llm.py                    # 默认 CUDA0, 全部层放显卡
  python tools/start_llm.py --cpu              # 纯 CPU
  python tools/start_llm.py --which escalate   # 升级复核模型 (llm.escalate.server, 默认 Qwen3.5-2B, 端口 8785)
  python tools/start_llm.py --which embed      # 记忆检索的嵌入服务 (memory.embed.server, Qwen3-Embedding-0.6B, 端口 8786)
  python tools/start_llm.py --which local      # 本机大模型 (llm.local_api.server, 默认纯 CPU、端口 8788; 桌宠、QQ、工作台选"本机模型"时用)

--parallel 4 留出四个槽位: 0 给第一条 /ws 连接的意图请求, 1 给长回复, 2 给日志整理, 3 给第二条连接 (llm.session_slots,
比如桌宠的文字接口); 基准脚本的基线两次调用用 1/2. 各自的 few-shot 前缀 KV 都能常驻.
上下文按槽位平分; 提示词加上空间和物体交互示例后约 2.9k token, 所以 ctx 设 16384 (每槽 4k).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wm import localhttp  # noqa: E402
from wm.config import load_config, resolve_path  # noqa: E402


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with localhttp.open_direct(f"http://127.0.0.1:{port}/health", timeout=1) as r:  # 直连: 别让环境变量里的代理劫走
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", action="store_true", help="不用显卡")
    ap.add_argument("--print-only", action="store_true", help="只打印命令")
    ap.add_argument("--which", choices=["main", "escalate", "embed", "local"], default="main",
                    help="主模型, 低置信度时的复核模型, 记忆检索的嵌入模型, 或本机大模型 (OpenAI 兼容, 给云端模型的位置用)")
    ap.add_argument("--lora", help="挂一个 LoRA 适配器 (GGUF, tools/train_lora.py --export 的结果); 默认取 llm.server.lora")
    ap.add_argument("--port", type=int, help="换端口 (比如另开一个挂 LoRA 的服务和原来的对比)")
    ap.add_argument("--model", help="换一个模型文件 (比如合并了 LoRA 的 GGUF); 默认取配置")
    args = ap.parse_args()

    cfg = load_config()
    llm = cfg["llm"]
    s = {"main": llm["server"], "escalate": {**llm["server"], **llm["escalate"]["server"]},
         "embed": {**llm["server"], **cfg["memory"]["embed"]["server"]},
         "local": {**llm["server"], **(llm.get("local_api") or {}).get("server", {})}}[args.which]
    if args.which == "local" and not (args.model or (llm.get("local_api") or {}).get("server", {}).get("model")):
        sys.exit("没配本机大模型的文件: 在 config/local.json 的 llm.local_api.server.model 写上 GGUF 的路径, 或者加 --model")
    exe, model = resolve_path(s["exe"]), resolve_path(args.model or s["model"])
    missing = [str(p) for p in (exe, model) if not p.exists()]
    if missing and not args.print_only:
        sys.exit("找不到: " + ", ".join(missing) + "\n先运行: python tools/setup_models.py llama-cuda llm")
    cpu = args.cpu or s.get("device", "CUDA0") == "CPU"
    port = args.port or int(s.get("port", 8784))
    lora = args.lora or (s.get("lora") if args.which == "main" and not args.model else None)  # 换了模型文件时不再挂配置里的 LoRA
    cmd = [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
           "-c", str(s.get("ctx", 8192)), "--parallel", str(s.get("parallel", 3)),
           "-b", "512", "-ub", "512", "-t", str(s.get("threads", 6)), "-tb", str(s.get("threads", 6)),
           "-ngl", "0" if cpu else str(s.get("gpu_layers", 99)), "--jinja", "--no-webui"]
    if not cpu:
        cmd += ["--device", s.get("device", "CUDA0"), "--flash-attn", "on"]
    else:  # 纯 CPU 也要说明不用显卡: 只给 -ngl 0 时这份带 CUDA 的 llama-server 照样在显卡上开缓冲区 (2026-11-07 评测时和占着显卡的
        cmd += ["--device", "none"]  # 语音合成抢, 报 CUDA illegal memory access 崩了)
    if lora:
        if not resolve_path(lora).exists() and not args.print_only:
            sys.exit(f"找不到 LoRA: {lora}")
        cmd += ["--lora", str(resolve_path(lora))]
    if args.which == "embed":  # Qwen3-Embedding: 取最后一个 token 的隐状态 (模型说明里的用法)
        cmd += ["--embedding", "--pooling", "last"]
    if args.which == "local":  # 本机大模型: 提示缓存放内存里的上限 (默认 8 GB, 这台电脑内存紧), 名字就叫配置里的模型名
        cmd += ["--cache-ram", str(s.get("cache_ram", 2048))]
        if s.get("mmproj"):  # 看图用的投影文件 (Qwen3.5 这类多模态模型的 mmproj-*.gguf)
            cmd += ["--mmproj", str(resolve_path(s["mmproj"]))]
        if (llm.get("local_api") or {}).get("model"):
            cmd += ["--alias", str(llm["local_api"]["model"])]
    print(" ".join(cmd), flush=True)
    if args.print_only:
        return
    proc = subprocess.Popen(cmd, cwd=str(exe.parent))
    try:
        t0 = time.time()
        if not wait_ready(port, proc):
            proc.terminate()
            sys.exit("llama-server 没能就绪, 看上面的日志.")
        print(f"\nllama-server 已就绪 ({time.time() - t0:.1f} s): http://127.0.0.1:{port}  (Ctrl+C 退出)", flush=True)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
