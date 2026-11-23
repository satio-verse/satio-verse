"""延迟埋点: 每轮对话一个 TurnTrace, 打点 -> 派生指标 -> 会话统计 (P50/P95) -> JSONL 日志."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from .clock import now_ms

# 打点按时间线大致顺序排列, 前端瀑布图按这个顺序画.
MARKS = [
    ("speech_start", "开始说话"),
    ("speech_end", "说完 (最后一个有声片段)"),
    ("spec_start", "投机请求发起"),
    ("asr_final", "ASR 判定结束"),
    ("spec_commit", "投机结果确认"),
    ("backchannel_sent", "起势动作已发出"),
    ("client_backchannel", "前端开始起势动作"),
    ("llm_request", "LLM 请求发出"),
    ("llm_first_token", "LLM 首 token"),
    ("intent_ready", "意图字段闭合"),
    ("client_intent", "前端开始执行动作"),
    ("emotion_ready", "情绪字段闭合"),
    ("first_clause", "首个可合成分句"),
    ("tts_first_request", "TTS 首次请求"),
    ("tts_first_chunk", "TTS 首包"),
    ("rvc_first_in", "RVC 首段输入"),
    ("rvc_first_out", "RVC 首段输出"),
    ("audio_first_sent", "首段音频发出"),
    ("face_first_sent", "首段面部动画发出"),
    ("client_audio_start", "用户听到声音"),
    ("llm_done", "LLM 输出结束"),
    ("tts_done", "TTS 全部完成"),
    ("client_audio_end", "播放结束"),
]
MARK_NAMES = {name for name, _ in MARKS}
CLIENT_MARKS = {"client_backchannel", "client_intent", "client_audio_start", "client_audio_end"}
# 投机阶段提前算好、确认后才真正下发的打点: 按下发时刻计, 指标才反映用户实际看到/听到的时间.
DELIVERY_MARKS = {"backchannel_sent", "intent_ready", "emotion_ready", "audio_first_sent", "face_first_sent"}

# 指标 = 终点打点 - 起点打点. 带 ★ 的是 P0/P1 的验收指标.
METRICS = {
    "endpoint_ms": ("asr_final", "speech_end", "说完→ASR 判定结束"),
    "reaction_ms": ("client_backchannel", "speech_end", "说完→角色起势 (首个可见反应)"),
    "llm_ttft_ms": ("llm_first_token", "llm_request", "LLM 首 token"),
    "intent_ms": ("intent_ready", "speech_end", "★ 说完→意图可执行"),
    "intent_llm_ms": ("intent_ready", "llm_request", "LLM 请求→意图闭合"),
    "motion_ms": ("client_intent", "speech_end", "说完→前端执行动作"),
    "first_clause_ms": ("first_clause", "llm_request", "LLM 请求→首分句"),
    "tts_ttfb_ms": ("tts_first_chunk", "tts_first_request", "TTS 首包"),
    "rvc_ms": ("rvc_first_out", "rvc_first_in", "RVC 首段处理"),
    "server_audio_ms": ("audio_first_sent", "speech_end", "说完→首段音频发出"),
    "voice_ms": ("client_audio_start", "speech_end", "★ 说完→听到声音"),
    "face_lag_ms": ("face_first_sent", "audio_first_sent", "首段面部动画比音频晚"),
    "llm_total_ms": ("llm_done", "llm_request", "LLM 全部输出"),
    "spec_lead_ms": ("spec_commit", "spec_start", "投机提前量 (发起→确认)"),
}


class TurnTrace:
    def __init__(self, turn: int, source: str, text: str = ""):
        self.turn = turn
        self.source = source
        self.text = text
        self.wall = datetime.now().isoformat(timespec="milliseconds")
        self.marks: dict[str, float] = {}
        self.meta: dict = {}
        self.interrupted = False

    def mark(self, name: str, t: float | None = None, overwrite: bool = False) -> None:
        if name not in MARK_NAMES:
            raise ValueError(f"未知打点: {name}")
        if overwrite or name not in self.marks:
            self.marks[name] = now_ms() if t is None else float(t)

    def has(self, name: str) -> bool:
        return name in self.marks

    def metrics(self) -> dict[str, float]:
        out = {}
        for key, (end, start, _) in METRICS.items():
            if end in self.marks and start in self.marks:
                out[key] = round(self.marks[end] - self.marks[start], 1)
        return out

    def to_dict(self) -> dict:
        origin = self.marks.get("speech_end", min(self.marks.values(), default=0.0))
        return {
            "turn": self.turn,
            "source": self.source,
            "text": self.text,
            "wall": self.wall,
            "interrupted": self.interrupted,
            # 相对"说完"的毫秒数, 负数表示发生在说完之前 (例如开始说话).
            "marks": {k: round(v - origin, 1) for k, v in sorted(self.marks.items(), key=lambda kv: kv[1])},
            "metrics": self.metrics(),
            "meta": self.meta,
        }


def _percentile(values: list[float], q: float) -> float:
    return round(float(np.percentile(np.asarray(values, dtype=float), q)), 1)


class LatencyStats:
    def __init__(self):
        self._values: dict[str, list[float]] = {k: [] for k in METRICS}
        self._lock = threading.Lock()
        self.turns = 0

    def add(self, trace: TurnTrace) -> None:
        with self._lock:
            self.turns += 1
            for key, value in trace.metrics().items():
                self._values[key].append(value)

    def summary(self) -> dict:
        with self._lock:
            rows = {}
            for key, values in self._values.items():
                if not values:
                    continue
                rows[key] = {
                    "label": METRICS[key][2],
                    "n": len(values),
                    "p50": _percentile(values, 50),
                    "p95": _percentile(values, 95),
                    "mean": round(sum(values) / len(values), 1),
                }
            return {"turns": self.turns, "metrics": rows}


class TraceLog:
    """每轮一行 JSON, 按日期分文件, 方便事后用 pandas/jq 分析."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.Lock()

    def write(self, record: dict, kind: str = "latency") -> Path:
        """kind: latency (每轮延迟) / motion-eval (前端发来的动作执行评估, 以后当强化学习的奖励数据)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{kind}-{datetime.now():%Y%m%d}.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
