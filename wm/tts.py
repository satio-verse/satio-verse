"""流式 TTS 适配层. 每个引擎实现 stream(text) -> 异步迭代 (int16 PCM, 采样率).

- MockTTS: 用共振峰加法合成"咿呀"元音, 不需要模型; 元音随字变化, 口型分析能看出差别.
- SherpaTTS: sherpa-onnx 离线 TTS (matcha / vits / kokoro), 按分句生成, 回调里逐段推出.
- HttpTTS: 外部流式服务. 预设 gpt_sovits (api_v2 /tts) 和 cosyvoice (官方 FastAPI), 以及通用 raw PCM.
"""
from __future__ import annotations

import asyncio
import threading
import zlib
from pathlib import Path
from typing import AsyncIterator

import aiohttp
import numpy as np

from .clauses import STRONG, WEAK
from .config import resolve_path
from .audio import to_int16

# (F1, F2) Hz, 粗略的成年女声元音共振峰.
_VOWELS = [(850, 1250), (320, 2500), (360, 850), (520, 2000), (560, 950)]  # a i u e o


class MockTTS:
    name = "mock"

    def __init__(self, cfg: dict):
        m = cfg.get("mock", {})
        self.sample_rate = int(m.get("sample_rate", 24000))
        self.first_chunk = m.get("first_chunk_ms", 80) / 1000.0
        self.rtf = float(m.get("rtf", 0.15))
        self.syllable = float(m.get("syllable_s", 0.17))

    async def load(self) -> None:
        pass

    def synth(self, text: str) -> np.ndarray:
        sr = self.sample_rate
        parts = []
        f0_base = 215.0
        for idx, ch in enumerate(text):
            if ch in STRONG:
                parts.append(np.zeros(int(0.22 * sr), np.float32))
                continue
            if ch in WEAK or ch.isspace():
                parts.append(np.zeros(int(0.11 * sr), np.float32))
                continue
            if not ch.isalnum():
                continue
            n = int(self.syllable * sr)
            t = np.arange(n, dtype=np.float32) / sr
            code = zlib.crc32(ch.encode("utf-8"))
            f1, f2 = _VOWELS[code % 5]
            f0 = f0_base * (1.0 + 0.08 * ((code >> 3) % 5 - 2) / 2) * (1.0 - 0.002 * idx)
            harmonics = np.arange(1, int(4500 // f0) + 1, dtype=np.float32)
            freqs = harmonics * f0
            gain = 1.0 / (1.0 + ((freqs - f1) / 90.0) ** 2) + 0.7 / (1.0 + ((freqs - f2) / 130.0) ** 2) + 0.02
            wave = (gain[:, None] * np.sin(2 * np.pi * freqs[:, None] * t[None, :])).sum(axis=0)
            env = np.minimum(1.0, t / 0.02) * np.minimum(1.0, (t[-1] - t) / 0.05 + 0.05)
            # 字间留一点缝, 口型才会开合.
            env *= np.where(t > self.syllable * 0.82, 0.05, 1.0)
            parts.append((wave * env).astype(np.float32))
        if not parts:
            return np.zeros(0, np.int16)
        y = np.concatenate(parts)
        peak = float(np.abs(y).max()) or 1.0
        return to_int16(y / peak * 0.5)

    async def stream(self, text: str) -> AsyncIterator[tuple[np.ndarray, int]]:
        pcm = self.synth(text)
        step = int(0.1 * self.sample_rate)
        await asyncio.sleep(self.first_chunk)
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step]
            if i:
                await asyncio.sleep(len(chunk) / self.sample_rate * self.rtf)
            yield chunk, self.sample_rate


class SherpaTTS:
    name = "sherpa"

    def __init__(self, cfg: dict, key: str = "sherpa"):
        self.cfg = cfg[key]
        self.name = key  # "sherpa" (matcha 等) 或 "kokoro"
        self.tts = None
        self.sample_rate = None

    async def load(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._load)

    def _load(self) -> None:
        import sherpa_onnx

        s = self.cfg
        p = lambda key: str(resolve_path(s[key])) if s.get(key) else ""  # noqa: E731
        kind = s.get("model_type", "matcha")
        if kind == "matcha":
            model = sherpa_onnx.OfflineTtsModelConfig(
                matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                    acoustic_model=p("acoustic_model"), vocoder=p("vocoder"), lexicon=p("lexicon"),
                    tokens=p("tokens"), data_dir=p("data_dir"), dict_dir=p("dict_dir")),
                num_threads=s.get("num_threads", 2), provider=s.get("provider", "cpu"))
        elif kind == "vits":
            model = sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=p("model"), lexicon=p("lexicon"), tokens=p("tokens"),
                    data_dir=p("data_dir"), dict_dir=p("dict_dir")),
                num_threads=s.get("num_threads", 2), provider=s.get("provider", "cpu"))
        elif kind == "kokoro":
            model = sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=p("model"), voices=p("voices"), tokens=p("tokens"), lexicon=p("lexicon"),
                    data_dir=p("data_dir"), dict_dir=p("dict_dir")),
                num_threads=s.get("num_threads", 2), provider=s.get("provider", "cpu"))
        else:
            raise ValueError(f"未知 sherpa TTS 模型类型: {kind}")
        fsts = ",".join(str(resolve_path(f)) for f in s.get("rule_fsts", "").split(",") if f.strip()
                        and resolve_path(f.strip()).exists())
        config = sherpa_onnx.OfflineTtsConfig(model=model, rule_fsts=fsts, max_num_sentences=1)
        if not config.validate():
            raise RuntimeError("sherpa-onnx TTS 配置校验失败, 检查模型路径")
        self.tts = sherpa_onnx.OfflineTts(config)
        self.sample_rate = self.tts.sample_rate

    async def stream(self, text: str) -> AsyncIterator[tuple[np.ndarray, int]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        done = object()

        def callback(samples, progress) -> int:
            loop.call_soon_threadsafe(queue.put_nowait, to_int16(np.asarray(samples, dtype=np.float32)).copy())
            return 0 if stop.is_set() else 1

        def run():
            try:
                self.tts.generate(text, sid=self.cfg.get("sid", 0), speed=self.cfg.get("speed", 1.0),
                                  callback=callback)
            except Exception as exc:  # 交给协程侧抛出
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        future = loop.run_in_executor(None, run)
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, Exception):
                    raise item
                if len(item):
                    yield item, self.sample_rate
        finally:
            stop.set()
            await asyncio.shield(future)


class HttpTTS:
    name = "http"

    def __init__(self, cfg: dict):
        self.cfg = cfg["http"]
        self.preset = self.cfg.get("preset", "gpt_sovits")
        self.base = self.cfg["base_url"].rstrip("/")
        self.sample_rate = int(self.cfg.get("sample_rate", 32000))
        self._session: aiohttp.ClientSession | None = None

    async def load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.get("timeout_s", 60)))

    def _request(self, text: str) -> dict:
        c = self.cfg
        if self.preset == "gpt_sovits":
            return {"method": "POST", "url": self.base + "/tts", "json": {
                "text": text, "text_lang": c.get("text_lang", "zh"),
                # api_v2 的工作目录是整合包根, 相对路径要先按项目根解析.
                "ref_audio_path": str(resolve_path(c["ref_audio_path"])) if c.get("ref_audio_path") else "",
                "prompt_text": c.get("prompt_text", ""),
                "prompt_lang": c.get("prompt_lang", "zh"), "text_split_method": "cut0",
                "batch_size": 1, "speed_factor": c.get("speed", 1.0),
                "media_type": "raw", "streaming_mode": c.get("streaming_mode", True),
                "min_chunk_length": int(c.get("min_chunk_length", 8))}}
        if self.preset == "cosyvoice":
            form = aiohttp.FormData()
            form.add_field("tts_text", text)
            if c.get("spk_id"):
                form.add_field("spk_id", c["spk_id"])
                return {"method": "POST", "url": self.base + "/inference_sft", "data": form}
            form.add_field("prompt_text", c.get("prompt_text", ""))
            ref = Path(resolve_path(c["ref_audio_path"]))
            form.add_field("prompt_wav", ref.read_bytes(), filename=ref.name, content_type="audio/wav")
            return {"method": "POST", "url": self.base + "/inference_zero_shot", "data": form}
        if self.preset == "raw":
            return {"method": "POST", "url": self.base + c.get("path", "/tts"), "json": {"text": text}}
        raise ValueError(f"未知 HTTP TTS 预设: {self.preset}")

    async def stream(self, text: str) -> AsyncIterator[tuple[np.ndarray, int]]:
        req = self._request(text)
        method, url = req.pop("method"), req.pop("url")
        async with self._session.request(method, url, **req) as r:
            if r.status != 200:
                raise RuntimeError(f"TTS 服务返回 {r.status}: {(await r.text())[:300]}")
            carry = b""
            async for chunk in r.content.iter_any():
                data = carry + chunk
                usable = len(data) - len(data) % 2
                carry = data[usable:]
                if usable:
                    yield np.frombuffer(data[:usable], dtype=np.int16).copy(), self.sample_rate


def create_tts(cfg: dict):
    engine = cfg.get("engine", "mock")
    if engine == "mock":
        return MockTTS(cfg)
    if engine in ("sherpa", "kokoro"):  # kokoro = sherpa-onnx 跑 Kokoro v1.1-zh, 配置在 tts.kokoro
        return SherpaTTS(cfg, engine)
    if engine in ("http", "gpt_sovits", "cosyvoice"):
        if engine != "http":
            cfg = {**cfg, "http": {**cfg["http"], "preset": engine}}
        return HttpTTS(cfg)
    raise ValueError(f"未知 TTS 引擎: {engine}")
