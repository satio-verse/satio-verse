"""语音合成对比: 首包延迟、实时率、清晰度 (用 SenseVoice 识别合成语音算字错率), 并把样音存成 WAV 供试听.

  python tools/bench_tts.py                          # matcha (当前配置) 对比 Kokoro 的几个中文女声
  python tools/bench_tts.py --kokoro-sids 3,4,5,6 --threads 4
  python tools/bench_tts.py --audition female        # 每个中文女声念同一句, 方便挑音色 (男声: male)
  python tools/bench_tts.py --no-matcha --kokoro-sids "" --gsv http://127.0.0.1:9881 \\
      --gsv-ref models/tts/aimisi/ref/ref_a --gsv-model e12=<GPT .ckpt>,<SoVITS .pth>
                                                     # GPT-SoVITS api_v2: 依次换权重 (/set_*_weights), 每组 × 每段参考音各测一遍.
                                                     # 参考音给前缀, 读 <前缀>.wav 和 <前缀>.txt; 不给 --gsv-model 就测服务当前的权重
  python tools/bench_tts.py --no-matcha --kokoro-sids "" --aniflive http://127.0.0.1:9882 --aniflive-model aimisi-v2proplus
                                                     # AnifLive-TTS (V2ProPlus 的 TensorRT 版, 2026-10-27 试): POST /v1/audio/speech 流式

样音写到 logs/tts-samples/<引擎>/NN.wav, 报告写到 logs/tts-bench-*.md.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wm.audio import resample  # noqa: E402
from wm.clauses import speakable_text  # noqa: E402
from wm.config import ROOT, load_config, resolve_path  # noqa: E402
from wm.tts import HttpTTS, SherpaTTS  # noqa: E402

# 角色平时会说的话: 短答、带数字、带英文、问句、长一点的叙述.
SENTENCES = ["你好呀，我是小流。", "好的，马上就来！", "我把球放回地上了。", "这个问题有点难，让我想一想。",
             "今天是十月二十三号，气温十八度。", "你知道吗，月亮其实一直在慢慢离开地球。", "要不要一起去公园散散步？",
             "妈妈买了五个苹果和两个橙子。", "我刚刚学会了用 Python 写程序。", "谢谢你陪我聊天，晚安，做个好梦。"]



def edit_distance(a: str, b: str) -> int:
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (ca != cb))
    return d[-1]


def make_asr(cfg: dict):
    import sherpa_onnx

    s = cfg["asr"]["vad_sensevoice"]
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(resolve_path(s["model"])), tokens=str(resolve_path(s["tokens"])), num_threads=4,
        language="zh", use_itn=True)


def recognize(asr, pcm: np.ndarray, sr: int) -> str:
    x = resample(pcm, sr, 16000).astype(np.float32) / 32768.0
    st = asr.create_stream()
    st.accept_waveform(16000, x)
    asr.decode_stream(st)
    return st.result.text


def write_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.astype("<i2").tobytes())


async def bench(name: str, tts_cfg: "dict | HttpTTS | AnifLiveTTS", asr, out_dir: Path) -> dict:
    tts = SherpaTTS({"sherpa": tts_cfg}) if isinstance(tts_cfg, dict) else tts_cfg
    t0 = time.perf_counter()
    await tts.load()
    load_s = time.perf_counter() - t0
    async for _ in tts.stream("预热一下。"):
        pass
    ttfb, rtf, errors, chars, rows = [], [], 0, 0, []
    for i, text in enumerate(SENTENCES):
        t0 = time.perf_counter()
        first, parts = None, []
        async for pcm, sr in tts.stream(text):
            first = first or time.perf_counter() - t0
            parts.append(pcm)
        total = time.perf_counter() - t0
        audio = np.concatenate(parts)
        write_wav(out_dir / name / f"{i + 1:02d}.wav", audio, tts.sample_rate)
        heard = recognize(asr, audio, tts.sample_rate)
        ref, hyp = speakable_text(text), speakable_text(heard)
        e = edit_distance(ref, hyp)
        errors, chars = errors + e, chars + len(ref)
        ttfb.append(first * 1000)
        rtf.append(total / (len(audio) / tts.sample_rate))
        rows.append((text, heard, e, len(parts)))
    return {"name": name, "load_s": load_s, "sr": tts.sample_rate, "ttfb": ttfb, "rtf": rtf,
            "cer": errors / max(1, chars), "rows": rows}


async def audition(kokoro: dict, which: str, out: Path) -> None:
    """Kokoro v1.1-zh: 编号 3~57 是中文女声 (zf_*), 58~102 是中文男声 (zm_*). 文件名带编号, 挑中后把编号写进 tts.kokoro.sid."""
    import onnxruntime as ort

    meta = ort.InferenceSession(str(resolve_path(kokoro["model"])), providers=["CPUExecutionProvider"]).get_modelmeta()
    names = meta.custom_metadata_map["speaker_names"].split(",")
    prefix = "zf_" if which == "female" else "zm_"
    tts = SherpaTTS({"sherpa": kokoro})
    await tts.load()
    text = "你好呀，我是小流。今天想去哪里走走？"
    for sid, name in enumerate(names):
        if not name.startswith(prefix):
            continue
        tts.cfg = {**kokoro, "sid": sid}
        audio = np.concatenate([p async for p, _ in tts.stream(text)])
        write_wav(out / f"sid{sid:03d}-{name}.wav", audio, tts.sample_rate)
        print(f"  {sid} {name}", flush=True)
    print(f"已写入 {out}")


class AnifLiveTTS:
    """AnifLive-TTS 的 POST /v1/audio/speech (stream=true 回 PCM16 块, 采样率在 X-TTS-Sample-Rate) 当成一个能测的引擎.
    只在这里测, 不进 wm/tts.py: 它自己写的部分是 PolyForm Noncommercial (不许商用), 见知识库"语音识别与合成"."""

    def __init__(self, url: str, model: str, language: str = "zh", sample_rate: int = 32000):
        self.url, self.model, self.language, self.sample_rate = url.rstrip("/"), model, language, sample_rate
        self._session = None

    async def load(self) -> None:
        import aiohttp

        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))
        async with self._session.get(f"{self.url}/health") as r:
            health = await r.json()
        if not health.get("ready"):
            raise SystemExit(f"AnifLive-TTS 还没就绪: {health}")

    async def stream(self, text: str):
        body = {"model": self.model, "voice_profile": "default", "text": text, "language": self.language, "stream": True}
        async with self._session.post(f"{self.url}/v1/audio/speech", json=body) as r:
            if r.status != 200:
                raise SystemExit(f"AnifLive-TTS 合成失败 HTTP {r.status}: {await r.text()}")
            self.sample_rate = int(r.headers.get("X-TTS-Sample-Rate", self.sample_rate))
            rest = b""
            async for chunk in r.content.iter_any():
                data = rest + chunk
                usable = len(data) // 2 * 2
                rest = data[usable:]
                if usable:
                    yield np.frombuffer(data[:usable], dtype=np.int16).copy(), self.sample_rate


def _gsv_path(p: str) -> str:
    """项目里的相对路径转成绝对路径; 其余 (如 GPT_SoVITS/pretrained_models/...) 原样交给服务, 按整合包根解析."""
    q = resolve_path(p)
    return str(q) if q.exists() else p


async def bench_gsv(args, cfg: dict, asr, out_dir: Path) -> list[dict]:
    import aiohttp

    models = [m.split("=", 1) for m in args.gsv_model] or [["gsv", ""]]
    refs = args.gsv_ref or [""]
    results = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
        for label, paths in models:
            if paths:
                gpt, sovits = (_gsv_path(p.strip()) for p in paths.split(","))
                for ep, path in (("set_gpt_weights", gpt), ("set_sovits_weights", sovits)):
                    async with http.get(f"{args.gsv.rstrip('/')}/{ep}", params={"weights_path": path}) as r:
                        if r.status != 200:
                            raise SystemExit(f"{ep} {path} 失败: {await r.text()}")
            for ref in refs:
                h = {**cfg["tts"]["http"], "preset": "gpt_sovits", "base_url": args.gsv, "streaming_mode": args.gsv_mode}
                if args.gsv_sr:
                    h["sample_rate"] = args.gsv_sr
                if ref:
                    h["ref_audio_path"] = str(resolve_path(ref + ".wav"))
                    h["prompt_text"] = resolve_path(ref + ".txt").read_text(encoding="utf-8").strip()
                name = "-".join(["gsv", label] + ([Path(ref).name] if ref else []) + ([f"m{args.gsv_mode}"] if args.gsv_mode != 1 else []))
                tts = HttpTTS({"http": h})
                print(f"测 {name} …", flush=True)
                results.append(await bench(name, tts, asr, out_dir))
                await tts._session.close()
    return results


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kokoro-sids", default="3,4,5,6,7,8", help="Kokoro 音色编号, 3~57 是中文女声, 58~102 是中文男声")
    ap.add_argument("--threads", type=int, help="CPU 线程数, 默认用各引擎配置里的 num_threads")
    ap.add_argument("--no-matcha", action="store_true")
    ap.add_argument("--audition", choices=["female", "male"], help="只生成试听样音: 每个中文女声 (或男声) 念同一句")
    ap.add_argument("--gsv", metavar="URL", help="GPT-SoVITS api_v2 服务地址, 如 http://127.0.0.1:9881")
    ap.add_argument("--gsv-ref", action="append", default=[], metavar="前缀", help="参考音 <前缀>.wav + <前缀>.txt, 可重复")
    ap.add_argument("--gsv-model", action="append", default=[], metavar="名字=GPT,SoVITS",
                    help="要测的权重, 路径相对项目根或整合包根, 可重复")
    ap.add_argument("--gsv-mode", type=int, default=1, help="api_v2 的 streaming_mode: 1 按分句整段, 2/3 分块流式")
    ap.add_argument("--gsv-sr", type=int, help="raw 音频的采样率, 默认取 tts.http.sample_rate (v2Pro 32000; v4/v5 是 48000)")
    ap.add_argument("--aniflive", metavar="URL", help="AnifLive-TTS 服务地址, 如 http://127.0.0.1:9882")
    ap.add_argument("--aniflive-model", default="aimisi-v2proplus", help="AnifLive-TTS 的模型包 id")
    args = ap.parse_args()
    cfg = load_config()
    kokoro = cfg["tts"]["kokoro"]
    asr = make_asr(cfg)
    out_dir = ROOT / cfg["trace"]["log_dir"] / "tts-samples"
    if args.audition:
        await audition(kokoro, args.audition, out_dir / f"audition-{args.audition}")
        return
    engines = []
    if not args.no_matcha:
        m = copy.deepcopy(cfg["tts"]["sherpa"])
        m["num_threads"] = args.threads or m.get("num_threads", 2)
        engines.append(("matcha", m))
    for sid in [int(x) for x in args.kokoro_sids.split(",") if x.strip()]:
        engines.append((f"kokoro-sid{sid}", {**kokoro, "sid": sid, "num_threads": args.threads or kokoro.get("num_threads", 4)}))
    results = []
    for name, c in engines:
        print(f"测 {name} …", flush=True)
        results.append(await bench(name, c, asr, out_dir))
    if args.gsv:
        results += await bench_gsv(args, cfg, asr, out_dir)
    if args.aniflive:
        tts = AnifLiveTTS(args.aniflive, args.aniflive_model)
        print(f"测 aniflive-{args.aniflive_model} …", flush=True)
        results.append(await bench(f"aniflive-{args.aniflive_model}", tts, asr, out_dir))
        await tts._session.close()
    lines = [f"# 语音合成对比 {datetime.now():%Y-%m-%d %H:%M}", "",
             f"{len(SENTENCES)} 句, CPU {args.threads or '按配置的'} 线程. 首包 = 发出请求到收到第一段音频; 实时率 = 合成耗时 / 语音时长; "
             "字错率 = SenseVoice 识别合成语音和原文的差异 (越低越清楚). 样音在 logs/tts-samples/.", "",
             "| 引擎 | 采样率 | 载入 | 首包 P50 / 最大 | 实时率 P50 | 字错率 |", "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['sr']} | {r['load_s']:.1f} s | {np.median(r['ttfb']):.0f} / {max(r['ttfb']):.0f} ms | "
                     f"{np.median(r['rtf']):.2f} | {r['cer']:.1%} |")
    for r in results:
        bad = [row for row in r["rows"] if row[2]]
        if bad:
            lines += ["", f"{r['name']} 识别有差异的句子:"]
            lines += [f"- {t} → {h} ({e} 字)" for t, h, e, _ in bad]
    report = "\n".join(lines) + "\n"
    out = ROOT / cfg["trace"]["log_dir"] / f"tts-bench-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"已写入 {out}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
