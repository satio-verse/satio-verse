"""按 config 的 tts.http.server 启动 GPT-SoVITS 的 api_v2 (整合包 runtime/gpt-sovits), 等端口就绪后保持前台运行, Ctrl+C 退出.

  python tools/start_tts.py                    # 端口取 tts.http.base_url (默认 9880), 权重取 tts.http.server
  python tools/start_tts.py --port 9881        # 另开一个试听/测试用的, 不碰主服务用的那个
  python tools/start_tts.py --base             # 不加载微调权重, 用底模 (零样本对照)
  python tools/start_tts.py --port 9881 --version v5dev \\
      --gpt models/tts/satio/weights/satio-e12.ckpt \\
      --sovits models/tts/satio/weights/satio_e6_lora64.pth   # v5dev 的 A/B 试听实例

服务端 (python -m wm --tts gpt_sovits) 每个分句 POST 一次 /tts, 参考音和文本在 tts.http.ref_audio_path / prompt_text.
api_v2 换权重时会把当前权重写回 -c 给的 yaml, 所以 yaml 每次按配置重新生成在整合包的 TEMP/ 里, 不手改.
"""
from __future__ import annotations

import argparse
import http.client
import json
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
        "v2ProPlus": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
        "v5dev": "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5dev.pth",
        "v5turbo": "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth"}


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


def prewarm(port: int, ref_audio: str, prompt_text: str, text: str = "嗯，我在。",
            steps: int = 0) -> tuple[float, int]:
    """先合成一句丢掉: 第一次合成要捕获 CUDA graph, 冷启能到 8s 级; 预热后真实首包回到 1s 内.

    返回 (耗时, 音频字节数). 字节数用来判断这次到底出了声音没有: AR 侧捕获失败时
    TTS.py 的 except 分支 yield 16000 个零(48k 下当 0.33s 播, 约 32000 字节),
    而正常一句「嗯，我在。」约 18 万字节, 差一个数量级.
    失败不算错 —— 语音服务已经就绪, 预热只是把冷启的代价从"她第一句话"挪到"启动时".
    """
    body = {
        "text": text, "text_lang": "zh", "ref_audio_path": ref_audio, "prompt_text": prompt_text,
        "prompt_lang": "zh", "text_split_method": "cut0", "batch_size": 1, "speed_factor": 1.0,
        "media_type": "raw", "streaming_mode": 3, "min_chunk_length": 8, "seed": 42,
    }
    if steps:  # v5 系: 预热也要用和线上一样的步数, 不然预热和真实首包差一截
        body["sample_steps"] = int(steps)
    body = json.dumps(body).encode("utf-8")
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    conn.request("POST", "/tts", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}: {resp.read()[:200]!r}")
    nbytes = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        nbytes += len(chunk)
    conn.close()
    return time.perf_counter() - t0, nbytes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, help="默认取 tts.http.base_url 的端口")
    ap.add_argument("--base", action="store_true", help="用底模, 不加载 tts.http.server 里的微调权重")
    ap.add_argument("--gpt", help="临时换 GPT 权重 (.ckpt), 默认取 tts.http.server.gpt")
    ap.add_argument("--sovits", help="临时换 SoVITS 权重 (.pth), 默认取 tts.http.server.sovits")
    ap.add_argument("--version", help="临时换 version (v2ProPlus/v5dev/v5turbo), 默认取 tts.http.server.version")
    ap.add_argument("--cpu", action="store_true", help="不用显卡 (很慢, 只用来排查)")
    ap.add_argument("--aux", choices=("cpu", "cuda"),
                    help="BERT/CNHuBERT 放哪边算; v5 系列默认 cpu (省约 1G 显存, 且 fp32 特征更准)")
    ap.add_argument("--no-prewarm", action="store_true", help="启动后不预热 (默认会先合成一句丢掉, 把 CUDA graph 的冷启挪到启动时)")
    ap.add_argument("--print-only", action="store_true", help="只打印命令和生成的 yaml")
    args = ap.parse_args()

    h = load_config()["tts"]["http"]
    s = h["server"]
    gsv = resolve_path(s["dir"]).resolve()
    port = args.port or urlparse(h["base_url"]).port or 9880
    version = args.version or s.get("version", "v2ProPlus")
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
    # v5 系列: BERT/CNHuBERT 默认在 CPU 上算. 显存省约 1G, 且 fp32 特征不会再让「歇一会儿」念成「谢我」;
    # 只在建声纹特征时各跑一次, 128 字以内实测 +0.1s 左右.
    if not args.cpu:
        aux = args.aux or ("cpu" if version.startswith("v5") else "cuda")
        env["WM_TTS_AUX_DEVICE"] = aux
        env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    base_env = env

    # AR 侧的 CUDA graph: 捕获成功时解码一步从 ~29ms 掉到 ~6ms, 短句首包 1.9s -> 0.6s, 音质完全不变.
    # 但 6G 卡上捕获也可能失败 (RuntimeError: operation not permitted when stream is capturing),
    # 一次失败就是一句 0.33s 纯静音 + TTS.py 的 except 分支删权重再重载, 所以:
    #   1) 默认只给 v5turbo 打开 —— v5dev + satio LoRA 那套实测捕获会挂;
    #   2) 预热时看音频字节数, 不像人声就立刻关图重启 (下面那段).
    # WM_V5_GRAPH=0/1 可以强制关/开.
    want_graph = version == "v5turbo"
    if os.environ.get("WM_V5_GRAPH") is not None:
        want_graph = os.environ["WM_V5_GRAPH"] == "1"

    def spawn(graph_on: bool) -> subprocess.Popen:
        child_env = dict(base_env)
        if version.startswith("v5"):
            child_env["CUDAGraph"] = "1" if graph_on else "0"
        return subprocess.Popen(cmd, cwd=gsv, env=child_env)

    graph_on = want_graph
    proc = spawn(graph_on)
    try:
        if not wait_ready(port, proc):
            sys.exit(f"GPT-SoVITS 没有起来 (退出码 {proc.poll()})")
        print(f"GPT-SoVITS 已就绪: http://127.0.0.1:{port}/tts  ({version}, "
              f"{'底模' if args.base else Path(sovits).name}, AR graph {'on' if graph_on else 'off'})")
        ref_audio = h.get("ref_audio_path") or ""
        if not args.no_prewarm and not args.cpu and ref_audio:
            ref = str(resolve_path(ref_audio))
            prompt = h.get("prompt_text") or ""
            steps = h.get("sample_steps") or 0
            try:
                spent, nbytes = prewarm(port, ref, prompt, steps=steps)
                print(f"预热完成: {spent:.2f}s ({nbytes} 字节)", flush=True)
                if nbytes < 60000 and graph_on:
                    print("预热出来的音频太短, 像是 AR graph 捕获失败; 关掉 CUDA graph 再起一次", flush=True)
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    graph_on = False
                    proc = spawn(False)
                    if not wait_ready(port, proc):
                        sys.exit(f"关图重启也没起来 (退出码 {proc.poll()})")
                    spent, nbytes = prewarm(port, ref, prompt, steps=steps)
                    print(f"关图后预热: {spent:.2f}s ({nbytes} 字节)", flush=True)
                # 再热一次长一点的句子: 短句只走单块, 多块拼接那条路也要热
                again, _ = prewarm(port, ref, prompt,
                                   "我先去把窗户关上，你等我一下，回来再跟你说今天的事。", steps=steps)
                print(f"预热(长句)完成: {again:.2f}s", flush=True)
            except Exception as e:  # 预热失败不该拦住服务, 只是第一句话会慢
                print(f"预热没成 ({e!r}), 服务照常")
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
