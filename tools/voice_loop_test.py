"""语音闭环实测: 不用真麦克风, 用本地 TTS 合成"用户说的话", 按真实节奏 (40 ms 一帧) 推给 /ws,
像浏览器一样回报 client_* 打点, 收集每轮的延迟指标.

  python -m wm                                   # 先启动服务 (asr.engine=sherpa)
  python tools/voice_loop_test.py --repeat 2

和浏览器的差别: 这里"听到声音"取的是收到第一帧音频的时刻再加 30 ms (浏览器首段的排队缓冲),
不含声卡输出延迟 (浏览器里通常再多 10~50 ms).

面部数据晚到: 按浏览器 AudioOut 的排队规则 (首段 +30 ms, 之后 +5 ms, 接在上一段后面) 算出每一帧何时被听到,
和这一帧 face 消息的到达时间比. 晚到的帧在浏览器里用频谱口型代替.
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

from wm.audio import resample  # noqa: E402
from wm.clauses import speakable_text  # noqa: E402
from wm.config import ROOT, load_config  # noqa: E402
from wm.pipeline import AUDIO_HEADER  # noqa: E402
from wm.tts import SherpaTTS  # noqa: E402

# 后两行带逗号, 合成语音里会有句中停顿, 用来检验"停顿不等于说完".
UTTERANCES = ["你好呀", "举起右手", "先挥挥左手再点点头", "我今天考试没考好", "给我表演个后空翻", "两只手一起举过头顶",
              "你觉得明天会下雨吗", "慢慢地摇摇头", "给大家鞠个躬吧", "今天中午吃什么好呢",
              "嗯，举起左手吧", "等一下，先别动"]
FRAME = 640  # 16 kHz 下 40 ms
FIRST_LEAD_MS = 30
NEXT_LEAD_MS = 5
FACE_LOOKAHEAD_MS = 15  # AudioOut.faceAt 默认提前 15 ms 取帧
KEYS = ["endpoint_ms", "reaction_ms", "intent_ms", "motion_ms", "llm_ttft_ms", "first_clause_ms",
        "tts_ttfb_ms", "server_audio_ms", "voice_ms", "face_lag_ms", "spec_lead_ms"]


def edit_distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def now_ms() -> float:
    return time.perf_counter() * 1000


async def synth_inputs(cfg: dict, texts: list[str]) -> dict[str, np.ndarray]:
    tts = SherpaTTS(cfg["tts"])
    await tts.load()
    out = {}
    for text in texts:
        parts, sr = [], None
        async for pcm, sr in tts.stream(text):
            parts.append(pcm)
        out[text] = resample(np.concatenate(parts), sr, 16000)
    return out


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.offset = 0.0
        self.queue: asyncio.Queue = asyncio.Queue()
        self.audio_start: dict[int, float] = {}
        self.audio_samples: dict[int, float] = {}
        self.play_next: dict[int, float] = {}                         # turn -> 下一段在浏览器里开始播放的时刻
        self.chunks: dict[tuple[int, int], list] = {}                 # (turn, seg) -> [(句内偏移 s, 开始播放 ms, 时长 s)]
        self.face: dict[int, list[float]] = {}                        # turn -> [晚到帧数, 总帧数, 帧率]
        self.spec = {"start": 0, "commit": 0, "cancel": 0}
        self.cancel_reasons: dict[str, int] = {}

    async def sync_clock(self) -> None:
        best = None
        for i in range(8):
            t0 = now_ms()
            await self.ws.send_json({"type": "ping", "id": i, "t_client": t0})
            while True:
                msg = await self.queue.get()
                if msg.get("type") == "pong" and msg.get("id") == i:
                    break
            rtt = now_ms() - t0
            if best is None or rtt < best[0]:
                best = (rtt, msg["t_server"] - (t0 + rtt / 2))
        self.offset = best[1]

    async def mark(self, turn: int, name: str, t: float | None = None) -> None:
        await self.ws.send_json({"type": "mark", "turn": turn, "name": name,
                                 "t": (now_ms() if t is None else t) + self.offset})

    def check_face(self, msg: dict, t: float) -> None:
        stat = self.face.setdefault(msg["turn"], [0, 0, msg["fps"]])
        chunks = self.chunks.get((msg["turn"], msg["seg"]), [])
        for k in range(msg["start"], msg["start"] + msg["n"]):
            ft = k / msg["fps"]
            play = next((st + (ft - off) * 1000 for off, st, dur in chunks if off <= ft < off + dur), None)
            if play is None:
                continue  # 句末多出的一帧, 不在任何一段音频里
            stat[1] += 1
            if t > play - FACE_LOOKAHEAD_MS:
                stat[0] += 1

    async def reader(self) -> None:
        async for msg in self.ws:
            t = now_ms()
            if msg.type == aiohttp.WSMsgType.BINARY:
                _, turn, seg, sr, _ = AUDIO_HEADER.unpack_from(msg.data)
                if turn not in self.audio_start:
                    self.audio_start[turn] = t + FIRST_LEAD_MS
                    await self.mark(turn, "client_audio_start", self.audio_start[turn])
                n = (len(msg.data) - AUDIO_HEADER.size) // 2
                self.audio_samples[turn] = self.audio_samples.get(turn, 0.0) + n / sr * 1000
                start = max(self.play_next.get(turn, 0.0), t + (NEXT_LEAD_MS if turn in self.play_next else FIRST_LEAD_MS))
                self.play_next[turn] = start + n / sr * 1000
                chunks = self.chunks.setdefault((turn, seg), [])
                off = chunks[-1][0] + chunks[-1][2] if chunks else 0.0
                chunks.append((off, start, n / sr))
                continue
            data = json.loads(msg.data)
            kind, turn = data.get("type"), data.get("turn")
            if kind == "spec":
                self.spec[data["state"]] += 1
                if data["state"] == "cancel":
                    reason = data.get("reason", "?")
                    self.cancel_reasons[reason] = self.cancel_reasons.get(reason, 0) + 1
            if kind == "face":
                self.check_face(data, t)
            if kind == "backchannel":
                await self.mark(turn, "client_backchannel", t)
            elif kind == "intent":
                await self.mark(turn, "client_intent", t)
            elif kind == "audio_end":
                end = self.audio_start.get(turn, t) + self.audio_samples.get(turn, 0.0)
                await self.mark(turn, "client_audio_end", max(end, t))
            await self.queue.put(data)


async def speak(ws, pcm: np.ndarray, stop: asyncio.Event) -> None:
    """先 0.3 s 静音, 再按实时节奏发语音, 之后一直发静音直到这一轮结束 (端点检测需要尾部静音)."""
    silence = np.zeros(FRAME, np.int16)
    frames = [silence] * 8 + [pcm[i:i + FRAME] for i in range(0, len(pcm), FRAME)]
    t0 = time.perf_counter()
    i = 0
    while not stop.is_set():
        frame = frames[i] if i < len(frames) else silence
        if len(frame) < FRAME:
            frame = np.concatenate([frame, silence[:FRAME - len(frame)]])
        await ws.send_bytes(frame.astype("<i2").tobytes())
        i += 1
        await asyncio.sleep(max(0.0, t0 + i * 0.04 - time.perf_counter()))


def pct(values, q):
    return round(float(np.percentile(values, q))) if values else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8790/ws")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--label", default="", help="写进报告标题和文件名, 例如 spec-off")
    args = ap.parse_args()
    cfg = load_config()
    print("合成测试语音…")
    inputs = await synth_inputs(cfg, UTTERANCES)

    rows, prewarm = [], {}
    async with aiohttp.ClientSession() as session, session.ws_connect(args.url) as ws:
        client = Client(ws)
        reader = asyncio.create_task(client.reader())
        hello = await client.queue.get()
        print("服务端引擎:", {k: v.get("engine") for k, v in hello["status"].items() if isinstance(v, dict) and "engine" in v})
        await client.sync_clock()
        for rep in range(args.repeat):
            for text in UTTERANCES:
                stop = asyncio.Event()
                sender = asyncio.create_task(speak(ws, inputs[text], stop))
                heard, trace, intent = "", None, None
                try:
                    while trace is None:
                        msg = await asyncio.wait_for(client.queue.get(), 30)
                        if msg["type"] == "turn_start":
                            heard = msg["text"]
                        elif msg["type"] == "intent":
                            intent = msg
                        elif msg["type"] == "trace" and not msg["trace"]["interrupted"]:
                            trace = msg["trace"]
                            prewarm = msg.get("prewarm") or {}
                finally:
                    stop.set()
                    await sender
                m = trace["metrics"]
                acts = [a["do"] + (f"/{a['side']}" if a.get("side") else "") for a in (intent or {}).get("acts", [])]
                hit = "speculative" in trace["meta"]
                timings = (trace["meta"].get("llm") or {}).get("timings") or {}
                ref, hyp = speakable_text(text), speakable_text(heard)
                errors = edit_distance(ref, hyp)
                face = trace["meta"].get("face") or {}
                late, total, fps = client.face.get(trace["turn"], [0, 0, 30])
                cm = trace["meta"].get("confidence") or {}
                rows.append({"text": text, "heard": heard, "op": (intent or {}).get("op"), "acts": acts, "spec": hit, "errors": errors, "ref_len": len(ref), "prompt_n": timings.get("prompt_n"),
                             "face_provider": face.get("provider"), "face_segs": face.get("segments") or [],
                             "face_late_ms": late * 1000 / fps, "face_total_ms": total * 1000 / fps,
                             "conf": cm.get("overall"), "esc": cm.get("escalated"), "esc_changed": cm.get("changed"), **m})
                print(f"  说: {text:<10} 识别: {heard:<12} 动作: {(intent or {}).get('op')} {acts}{'  [投机命中]' if hit else ''}")
                print("     " + "  ".join(f"{k.replace('_ms', '')} {m[k]:.0f}" for k in KEYS if k in m))
                await asyncio.sleep(0.5)
        reader.cancel()

    spec = client.spec
    title = f"# 语音闭环实测 {args.label} {datetime.now():%Y-%m-%d %H:%M}".replace("  ", " ")
    lines = [title, "",
             f"引擎: {json.dumps({k: v.get('engine') for k, v in hello['status'].items() if isinstance(v, dict) and 'engine' in v}, ensure_ascii=False)}",
             f"{len(UTTERANCES)} 句 x {args.repeat} 轮, 输入为本地 TTS 合成语音按实时节奏推送.",
             f"投机执行: 发起 {spec['start']} 次, 命中 {spec['commit']} 次, 取消 {spec['cancel']} 次 "
             f"{json.dumps(client.cancel_reasons, ensure_ascii=False)}; "
             f"{sum(r['spec'] for r in rows)}/{len(rows)} 轮用上了投机结果.",
             f"识别字错率 {sum(r['errors'] for r in rows) / max(1, sum(r['ref_len'] for r in rows)):.1%}, "
             f"整句完全正确 {sum(r['errors'] == 0 for r in rows)}/{len(rows)}.",
             f"LLM 每轮新算 token P50 {pct([r['prompt_n'] for r in rows if r['prompt_n'] is not None], 50)}; "
             f"KV 预热 {json.dumps(prewarm, ensure_ascii=False)}.", "",
             "| 指标 | P50 (ms) | P95 (ms) |", "|---|---|---|"]
    for key in KEYS:
        values = [r[key] for r in rows if key in r]
        lines.append(f"| {key} | {pct(values, 50)} | {pct(values, 95)} |")
    segs = [s for r in rows for s in r["face_segs"]]
    if segs:
        provider = next(r["face_provider"] for r in rows if r["face_provider"])
        lines += ["", f"面部动画 (A2F, {provider}): {len(segs)} 句, 每句 {pct([s['frames'] for s in segs], 50)} 帧 (P50); "
                  f"推理 P50 {pct([s['infer_ms'] for s in segs], 50)} ms, 解算 P50 {pct([s['solve_ms'] for s in segs], 50)} ms, "
                  f"合计 P50 {pct([s['total_ms'] for s in segs], 50)} / P95 {pct([s['total_ms'] for s in segs], 95)} ms; "
                  f"每句分 {pct([s.get('updates', 1) for s in segs], 50)} 段下发 (P50).",
                  f"面部数据晚到 (这段时间用频谱口型): 占说话时长 {sum(r['face_late_ms'] for r in rows) / max(1e-9, sum(r['face_total_ms'] for r in rows)):.1%}; "
                  f"每轮晚到 P50 {pct([r['face_late_ms'] for r in rows], 50)} ms / P95 {pct([r['face_late_ms'] for r in rows], 95)} ms."]
    escs = [r for r in rows if r.get("esc")]
    if any(r.get("conf") is not None for r in rows):
        lines += ["", f"意图置信度 P50 {np.median([r['conf'] for r in rows if r.get('conf') is not None]):.2f}; "
                  f"复核 {len(escs)}/{len(rows)} 轮, 改变结果 {sum(bool(r.get('esc_changed')) for r in escs)} 轮, "
                  f"复核耗时 P50 {pct([r['esc']['ms'] for r in escs], 50)} ms "
                  f"(超时 {sum(1 for r in escs if r['esc'].get('error'))} 轮)." if escs else
                  f"意图置信度 P50 {np.median([r['conf'] for r in rows if r.get('conf') is not None]):.2f}; 没有触发复核."]
    lines += ["", "| 说的话 | 识别结果 | op | 动作 | 投机 | intent_ms | voice_ms |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['text']} | {r['heard']} | {r['op']} | {' '.join(r['acts'])} | {'命中' if r['spec'] else ''} | "
                     f"{r.get('intent_ms', '—')} | {r.get('voice_ms', '—')} |")
    report = "\n".join(lines) + "\n"
    suffix = f"-{args.label}" if args.label else ""
    out = ROOT / cfg["trace"]["log_dir"] / f"voice-loop-{datetime.now():%Y%m%d-%H%M%S}{suffix}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print()
    print(report)
    print(f"已写入 {out}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
