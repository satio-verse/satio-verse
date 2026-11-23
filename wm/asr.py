"""流式 ASR 适配层.

SherpaASR 用 sherpa-onnx 的在线识别器 (边说边出部分结果, 自带端点检测).
EnergyVAD 只负责记录"最后一个有声片段"到达的时间, 作为"说完"的打点, 端点检测本身仍由 ASR 决定.
"""
from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .audio import rms_db, to_float32
from .config import resolve_path

_CJK_SPACE = re.compile(r"(?<=[㐀-鿿])\s+(?=[㐀-鿿])")


def clean_text(text: str) -> str:
    return _CJK_SPACE.sub("", text.strip())


class EnergyVAD:
    def __init__(self, threshold_db: float = -42.0):
        self.threshold_db = threshold_db
        self.speech_start: float | None = None
        self.last_voiced: float | None = None

    def update(self, pcm: np.ndarray, t_arrival: float) -> bool:
        voiced = rms_db(pcm) > self.threshold_db
        if voiced:
            if self.speech_start is None:
                self.speech_start = t_arrival
            self.last_voiced = t_arrival
        return voiced

    def reset(self) -> None:
        self.speech_start = None
        self.last_voiced = None


class SherpaASRSession:
    """每个 WebSocket 连接一个识别流. 解码放在单线程池里, 不阻塞事件循环, 也保证顺序."""

    def __init__(self, recognizer, sample_rate: int):
        self.recognizer = recognizer
        self.sample_rate = sample_rate
        self.stream = recognizer.create_stream()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
        self.last_partial = ""

    def _result(self) -> str:
        r = self.recognizer.get_result(self.stream)
        return clean_text(r if isinstance(r, str) else getattr(r, "text", ""))

    def _decode(self, samples: np.ndarray) -> list[tuple[str, str]]:
        self.stream.accept_waveform(self.sample_rate, samples)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        events = []
        text = self._result()
        if text != self.last_partial:
            self.last_partial = text
            events.append(("partial", text))
        if self.recognizer.is_endpoint(self.stream):
            if text:
                events.append(("final", text))
            self.recognizer.reset(self.stream)
            self.last_partial = ""
        return events

    async def accept(self, pcm: np.ndarray) -> list[tuple[str, str]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, self._decode, to_float32(pcm))

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


class SherpaASR:
    name = "sherpa"
    streaming_partials = True  # 部分结果稳定一段时间就可以拿来投机

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sample_rate = cfg.get("sample_rate", 16000)
        self.recognizer = None

    def load(self) -> None:
        import sherpa_onnx  # 可选依赖, 只在启用时导入

        s = self.cfg["sherpa"]
        p = lambda key: str(resolve_path(s[key]))  # noqa: E731
        common = dict(
            num_threads=s.get("num_threads", 2),
            sample_rate=self.sample_rate,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=s.get("rule1_min_trailing_silence", 2.4),
            rule2_min_trailing_silence=s.get("rule2_min_trailing_silence", 0.6),
            rule3_min_utterance_length=s.get("rule3_min_utterance_length", 20),
            provider=s.get("provider", "cpu"),
        )
        kind = s.get("model_type", "zipformer2_ctc")
        if kind == "zipformer2_ctc":
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
                tokens=p("tokens"), model=p("model"), **common)
        elif kind == "transducer":
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=p("tokens"), encoder=p("encoder"), decoder=p("decoder"), joiner=p("joiner"), **common)
        elif kind == "paraformer":
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
                tokens=p("tokens"), encoder=p("encoder"), decoder=p("decoder"), **common)
        else:
            raise ValueError(f"未知 sherpa ASR 模型类型: {kind}")

    def create_session(self) -> SherpaASRSession:
        return SherpaASRSession(self.recognizer, self.sample_rate)


class VadSenseVoiceSession:
    """VAD 判定停顿和说完, SenseVoice 整句识别. 产出事件:
      ("speech_start", "")  VAD 认为开始说话 (插话检测用)
      ("partial", text)     说话中每 interim_ms 识别一次, 只用于显示和插话
      ("pause", text)       静音满 pause_ms: 把这一句识别一遍, 可以拿来投机
      ("resume", "")        停顿后又开口了, 之前的停顿结果作废
      ("final", text)       停顿后静音满 endpoint_ms - pause_ms 仍没开口: 沿用停顿时的识别结果
      ("echo", text)        这句是外放的回声 (同一段系统声音里也有这些话, wm/echo.py), 丢掉, 后面紧跟 utt_end
      ("utt_end", "")       这一段声音结束了, 但没有要交出去的话 (没认出字, 或者是回声): 不算你在说话了
    """

    def __init__(self, engine: "VadSenseVoiceASR", cfg: dict):
        import sherpa_onnx

        self.rec = engine.recognizer
        self.sr = engine.sample_rate
        self.vad = sherpa_onnx.VoiceActivityDetector(engine.vad_config, buffer_size_in_seconds=30)
        ms = lambda key, default: int(cfg.get(key, default) * self.sr / 1000)  # noqa: E731
        self.preroll_n = ms("preroll_ms", 500)
        self.hold_n = ms("endpoint_ms", 400) - ms("pause_ms", 150)
        self.interim_n = ms("interim_ms", 0)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
        self.preroll: list[np.ndarray] = []
        self.utt: list[np.ndarray] | None = None
        self.seen = 0
        self.paused_at: int | None = None
        self.pause_text = ""
        self.last_interim = 0
        self.last_meta: dict = {}
        self.utt_start = 0          # 这一句从输入流的第几个采样开始 (含前置的 preroll)
        self.echo = None            # wm/echo.EchoGuard: 外放时复核这句是不是视频里的话 (Session 挂上)
        self.echo_text = True

    def _echo_check(self, text: str) -> bool:
        """这句是不是外放的视频声音: 同一段时间里电脑放出来的声音也认一遍, 这句的字几乎都按顺序在里面就是.
        只在量到过外放回声 (不是耳机) 且那段时间系统在出声时才查; 多花一次识别 (几十毫秒)."""
        if self.echo is None or not self.echo_text or not text:
            return False
        ref = self.echo.ref_for(self.utt_start, self.paused_at)
        if ref is None:
            return False
        t0 = time.perf_counter()
        # 一两个字、而且这一句几乎每块都判成"全是回声": 回声的残渣 ("嗯""啊"), 不用再认参考.
        # 只看这个比例不够: 回声比你的声音响 10 dB 时你的话也大多判成"全是回声", 但还认得出来, 交给下面按字比
        from .echo import norm_text
        near = self.echo.near_ratio(self.utt_start, self.paused_at)
        if near is not None and near < 0.05 and len(norm_text(text)) <= 2:
            echo, ratio, why = True, 0.0, "gated"
        else:
            stream = self.rec.create_stream()
            stream.accept_waveform(self.sr, ref)
            self.rec.decode_stream(stream)
            echo, ratio = self.echo.is_echo(text, clean_text(getattr(stream.result, "text", "") or ""))
            why = "text"
        self.last_meta["echo_check"] = {"ratio": round(ratio, 2), "near": None if near is None else round(near, 2),
                                        "echo": echo, "by": why, "ms": round((time.perf_counter() - t0) * 1000, 1)}
        if echo:
            self.echo.stats["echo_drops"] += 1
        return echo

    def _recognize(self) -> str:
        t0 = time.perf_counter()
        audio = np.concatenate(self.utt)
        stream = self.rec.create_stream()
        stream.accept_waveform(self.sr, audio)
        self.rec.decode_stream(stream)
        r = stream.result
        wave = np.asarray(audio, np.float64)
        self.last_meta = {"emotion": getattr(r, "emotion", ""), "event": getattr(r, "event", ""),
                          "lang": getattr(r, "lang", ""), "decode_ms": round((time.perf_counter() - t0) * 1000, 1),
                          "audio_s": round(wave.size / self.sr, 2),
                          # 这一段的音量 (dBFS, 只有数字): 换了麦克风处理 (浏览器降噪、自动增益关掉) 以后看你的声音够不够大
                          "level_db": round(10 * float(np.log10(np.mean(wave * wave) + 1e-12)), 1) if wave.size else None}
        return clean_text(r.text)

    def _process(self, samples: np.ndarray) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        self.seen += len(samples)
        self.vad.accept_waveform(samples)
        speaking = self.vad.is_speech_detected()
        segment_ended = False
        while not self.vad.empty():
            self.vad.pop()
            segment_ended = True
        if self.utt is None:
            self.preroll.append(samples)
            while sum(len(x) for x in self.preroll) > self.preroll_n and len(self.preroll) > 1:
                self.preroll.pop(0)
            if not (speaking or segment_ended):
                return events
            self.utt, self.preroll = self.preroll, []
            self.utt_start = self.seen - sum(len(x) for x in self.utt)
            self.last_interim = self.seen
            events.append(("speech_start", ""))
        else:
            self.utt.append(samples)
        if self.paused_at is None and segment_ended:
            self.paused_at = self.seen
            self.pause_text = self._recognize()
            if self.pause_text and self._echo_check(self.pause_text):
                # 视频里的话: 这一段整个丢掉, 后面再开口算新的一句 (不和回声拼在一起)
                events += [("echo", self.pause_text), ("utt_end", "")]
                self.utt, self.paused_at, self.pause_text = None, None, ""
                return events
            if self.pause_text:
                events.append(("pause", self.pause_text))
        elif self.paused_at is not None and speaking:
            self.paused_at, self.pause_text = None, ""
            events.append(("resume", ""))
        if self.paused_at is not None and self.seen - self.paused_at >= self.hold_n:
            events.append(("final", self.pause_text) if self.pause_text else ("utt_end", ""))
            self.utt, self.paused_at, self.pause_text = None, None, ""
        elif self.paused_at is None and self.interim_n > 0 and self.seen - self.last_interim >= self.interim_n:
            self.last_interim = self.seen
            text = self._recognize()
            if text:
                events.append(("partial", text))
        return events

    async def accept(self, pcm: np.ndarray) -> list[tuple[str, str]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, self._process, to_float32(pcm))

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


class VadSenseVoiceASR:
    name = "vad_sensevoice"
    streaming_partials = False  # 中间结果不稳定可用, 投机只在 pause 事件上发起

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sample_rate = cfg.get("sample_rate", 16000)
        self.recognizer = None
        self.vad_config = None

    def load(self) -> None:
        import sherpa_onnx

        s = self.cfg["vad_sensevoice"]
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(resolve_path(s["model"])), tokens=str(resolve_path(s["tokens"])),
            num_threads=s.get("num_threads", 4), language=s.get("language", "zh"), use_itn=s.get("use_itn", True))
        self.vad_config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(resolve_path(s["vad_model"])), threshold=s.get("vad_threshold", 0.5),
                min_silence_duration=s.get("pause_ms", 150) / 1000, min_speech_duration=s.get("min_speech_ms", 120) / 1000,
                window_size=512),
            sample_rate=self.sample_rate, num_threads=1)
        # 预热: 首次解码要初始化, 别让第一句话替它买单.
        stream = self.recognizer.create_stream()
        stream.accept_waveform(self.sample_rate, np.zeros(self.sample_rate // 2, np.float32))
        self.recognizer.decode_stream(stream)

    def can_transcribe(self) -> bool:
        """模型载入成功才能整段识别; 没载入时不能把空结果当成"听不出话"."""
        return self.recognizer is not None

    def transcribe(self, pcm: np.ndarray) -> str:
        """一整段 16 kHz 单声道 int16 PCM -> 文字 (QQ 语音这种离线整句识别). 慢, 调用方放线程里跑."""
        if self.recognizer is None:
            return ""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(self.sample_rate, to_float32(pcm))
        self.recognizer.decode_stream(stream)
        return clean_text(getattr(stream.result, "text", "") or "")

    def create_session(self) -> VadSenseVoiceSession:
        return VadSenseVoiceSession(self, self.cfg["vad_sensevoice"])


class NullASR:
    name = "none"
    sample_rate = 16000

    def load(self) -> None:
        pass

    def create_session(self):
        return None

    def can_transcribe(self) -> bool:
        return False

    def transcribe(self, pcm: np.ndarray) -> str:
        return ""


def create_asr(cfg: dict):
    engine = cfg.get("engine", "none")
    if engine == "sherpa":
        return SherpaASR(cfg)
    if engine == "vad_sensevoice":
        return VadSenseVoiceASR(cfg)
    if engine == "none":
        return NullASR()
    raise ValueError(f"未知 ASR 引擎: {engine}")
