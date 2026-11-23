"""面部动画 (A2F) 实测: 推理设备耗时、口型和声音的对齐、和 LLM 抢显卡时的相互影响.

  python tools/bench_face.py                 # DirectML 和 CPU 都测; llama-server 在跑时加测抢显卡
  python tools/bench_face.py --providers dml --no-llm

对齐: 下巴张开度 (jawOpen) 和语音响度包络做互相关, 取相关最大的时间差. 正值 = 嘴比声音晚.
抢显卡: 让 LLM 连续生成, 同时让 A2F 一句接一句地算, 对比两边各自单独跑时的速度.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wm.a2f import A2F, FaceStream  # noqa: E402
from wm.config import ROOT, load_config  # noqa: E402
from wm.tts import create_tts  # noqa: E402

SENTENCES = ["你好呀，很高兴见到你。", "今天天气真不错，我们一起出去走走吧。", "我刚才把书放回书架上了。",
             "这个问题有点难，让我想一想。", "好的，马上就来！", "你知道吗，月亮其实一直在慢慢离开地球。",
             "谢谢你陪我聊天，晚安。", "妈妈买了五个苹果和两个橙子。"]
LLM_PROMPT = "请用三百字介绍一下长城的历史。"


def envelope(pcm: np.ndarray, sr: int, hop: float = 0.01, win: float = 0.025) -> np.ndarray:
    x = pcm.astype(np.float32) / 32768.0
    h, w = int(sr * hop), int(sr * win)
    n = len(x) // h
    pad = np.pad(x, (w // 2, w))
    return np.array([np.sqrt(np.mean(pad[i * h:i * h + w] ** 2)) for i in range(n)])


def best_lag(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[int, float]:
    """b 相对 a 的时间差 (步数): 正值表示 b 晚于 a."""
    a = (a - a.mean()) / (a.std() + 1e-9)
    b = (b - b.mean()) / (b.std() + 1e-9)
    best = (0, -1.0)
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            c = float(np.mean(a[:len(a) - lag] * b[lag:]))
        else:
            c = float(np.mean(a[-lag:] * b[:len(b) + lag]))
        if c > best[1]:
            best = (lag, c)
    return best


async def llm_tokens_per_s(endpoint: str, n_predict: int = 160) -> float:
    body = {"prompt": LLM_PROMPT, "n_predict": n_predict, "temperature": 0, "cache_prompt": False, "stream": False}
    async with aiohttp.ClientSession() as s, s.post(endpoint + "/completion", json=body) as r:
        data = await r.json()
    t = data.get("timings") or {}
    return float(t.get("predicted_per_second") or 0.0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="dml,cpu")
    ap.add_argument("--no-llm", action="store_true", help="不测和 LLM 抢显卡")
    args = ap.parse_args()
    cfg = load_config()
    tts = create_tts(cfg["tts"])
    await tts.load()
    clips = []
    for text in SENTENCES:
        parts = [p async for p, _ in tts.stream(text)]
        clips.append((text, np.concatenate(parts), tts.sample_rate))
    audio_s = sum(len(c[1]) / c[2] for c in clips)
    lines = [f"# 面部动画实测 {datetime.now():%Y-%m-%d %H:%M}", "",
             f"TTS: {cfg['tts']['engine']}/{cfg['tts'].get('sherpa', {}).get('model_type', '')}, "
             f"{len(clips)} 句共 {audio_s:.1f} s 语音. 模型: Audio2Face-3D v2.3 (Mark), 30 fps.", "",
             "| 设备 | 每句推理 P50 | 每句解算 P50 | 每句合计 P50 / 最大 | 每秒语音耗时 |", "|---|---|---|---|---|"]
    engines = {}
    for prov in args.providers.split(","):
        a = A2F({"a2f": {**cfg.get("face", {}).get("a2f", {}), "provider": prov}})
        await a.load()
        if a.provider != prov:
            lines.append(f"| {prov} | 不可用 (退回 {a.provider}) | | | |")
            continue
        engines[prov] = a
        rs = [a.process(FaceStream(), pcm, sr) for _, pcm, sr in clips]
        tot = [r["total_ms"] for r in rs]
        lines.append(f"| {prov} | {np.median([r['infer_ms'] for r in rs]):.0f} ms | {np.median([r['solve_ms'] for r in rs]):.0f} ms | "
                     f"{np.median(tot):.0f} / {max(tot):.0f} ms | {sum(tot) / audio_s:.0f} ms |")

    a = engines.get("dml") or next(iter(engines.values()))
    lags, corrs = [], []
    for _, pcm, sr in clips:
        w = a.process(FaceStream(), pcm, sr)["weights"]
        jaw = w[:, a.names.index("jawOpen")]
        env = envelope(pcm, sr)
        t = np.arange(len(env)) * 0.01
        jaw100 = np.interp(t, np.arange(len(jaw)) / a.fps, jaw)
        lag, c = best_lag(env, jaw100, 20)
        lags.append(lag * 10)
        corrs.append(c)
    lines += ["", f"口型对齐 ({a.provider}): 下巴张开度和响度包络的最佳时间差 中位 {np.median(lags):+.0f} ms "
              f"(各句 {', '.join(f'{x:+d}' for x in lags)}), 相关系数中位 {np.median(corrs):.2f}. 正值 = 嘴比声音晚."]

    endpoint = cfg["llm"].get("endpoint") or f"http://127.0.0.1:{cfg['llm'].get('server', {}).get('port', 8784)}"
    if not args.no_llm and "dml" in engines:
        try:
            alone = await llm_tokens_per_s(endpoint)
        except aiohttp.ClientError as exc:
            lines += ["", f"抢显卡: 连不上 LLM ({exc}), 跳过."]
        else:
            stop = False
            busy: list[float] = []

            def face_loop():
                i = 0
                while not stop:
                    _, pcm, sr = clips[i % len(clips)]
                    busy.append(engines["dml"].process(FaceStream(), pcm, sr)["total_ms"])
                    i += 1

            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(None, face_loop)
            await asyncio.sleep(0.3)
            together = await llm_tokens_per_s(endpoint)
            stop = True
            await fut
            alone_face = np.median([engines["dml"].process(FaceStream(), pcm, sr)["total_ms"] for _, pcm, sr in clips])
            lines += ["", "抢显卡 (LLM 连续生成 160 token 的同时, A2F 一句接一句不停地算; 实际对话里 A2F 只在每句合成后算一次):", "",
                      "| | 单独跑 | 同时跑 |", "|---|---|---|",
                      f"| LLM 生成速度 | {alone:.0f} token/s | {together:.0f} token/s |",
                      f"| A2F 每句合计 P50 | {alone_face:.0f} ms | {np.median(busy):.0f} ms |"]
    report = "\n".join(lines) + "\n"
    out = ROOT / cfg["trace"]["log_dir"] / f"face-bench-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"已写入 {out}")
    print(json.dumps({"lags_ms": lags}))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
