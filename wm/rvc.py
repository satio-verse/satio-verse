"""RVC 音色层: 以分句为单位把 TTS 输出转换成目标音色.

RVC 逐帧对齐、不改变时长, 所以 TTS 给出的时间信息整体平移即可继续使用.
分句级转换的代价是: 首段声音要等第一句 TTS 全部合成完再加 RVC 处理时间, 因此首句切得越短越好.
"""
from __future__ import annotations

import asyncio
import base64

import aiohttp
import numpy as np

from .audio import decode_wav, encode_wav


class NoRVC:
    name = "none"
    enabled = False

    async def load(self) -> None:
        pass

    async def convert(self, pcm: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
        return pcm, sr


class MockRVC:
    """原样返回, 只模拟处理耗时, 用来验证流水线和埋点."""
    name = "mock"
    enabled = True

    def __init__(self, cfg: dict):
        m = cfg.get("mock", {})
        self.base = m.get("base_ms", 30) / 1000.0
        self.rtf = float(m.get("rtf", 0.15))

    async def load(self) -> None:
        pass

    async def convert(self, pcm: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
        await asyncio.sleep(self.base + len(pcm) / sr * self.rtf)
        return pcm, sr


class HttpRVC:
    """预设 rvc_python: `python -m rvc_python api` 的 POST /convert, 请求体 {"audio_data": base64 WAV}, 返回 WAV.
    预设 wav_body: 请求体直接是 WAV 字节, 返回 WAV. 两种都按分句调用."""
    name = "http"
    enabled = True

    def __init__(self, cfg: dict):
        self.cfg = cfg["http"]
        self.base = self.cfg["base_url"].rstrip("/")
        self.preset = self.cfg.get("preset", "rvc_python")
        self._session: aiohttp.ClientSession | None = None

    async def load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.get("timeout_s", 30)))

    async def convert(self, pcm: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
        wav = encode_wav(pcm, sr)
        if self.preset == "rvc_python":
            kwargs = {"json": {"audio_data": base64.b64encode(wav).decode("ascii")}}
            url = self.base + self.cfg.get("path", "/convert")
        elif self.preset == "wav_body":
            kwargs = {"data": wav, "headers": {"Content-Type": "audio/wav"}}
            url = self.base + self.cfg.get("path", "/convert")
        else:
            raise ValueError(f"未知 RVC 预设: {self.preset}")
        async with self._session.post(url, **kwargs) as r:
            if r.status != 200:
                raise RuntimeError(f"RVC 服务返回 {r.status}: {(await r.text())[:300]}")
            return decode_wav(await r.read())


def create_rvc(cfg: dict):
    engine = cfg.get("engine", "none")
    if engine == "none":
        return NoRVC()
    if engine == "mock":
        return MockRVC(cfg)
    if engine == "http":
        return HttpRVC(cfg)
    raise ValueError(f"未知 RVC 引擎: {engine}")
