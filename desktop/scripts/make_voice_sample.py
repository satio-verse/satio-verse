"""生成桌宠语音自检用的测试语音: 用本机的 TTS (config 里的 sherpa 配置, 和 tools/voice_loop_test.py 同一套) 合成一句话,
重采样到 16 kHz, 前面加 1 s 静音、后面加 3 s 静音 (端点检测要尾部静音), 存成 16 位单声道 WAV.
自检时宿主用 Chromium 的假麦克风 (--use-file-for-fake-audio-capture) 把它当成麦克风输入, 不碰真实麦克风.

用法: python desktop/scripts/make_voice_sample.py 输出.wav 一句话
"""
from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wm.audio import resample  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.tts import SherpaTTS  # noqa: E402

RATE = 16000


async def synth(text: str) -> np.ndarray:
    tts = SherpaTTS(load_config()["tts"])
    await tts.load()
    parts, sr = [], RATE
    async for pcm, sr in tts.stream(text):
        parts.append(pcm)
    return resample(np.concatenate(parts), sr, RATE)


def main() -> None:
    out, text = Path(sys.argv[1]), sys.argv[2]
    speech = asyncio.run(synth(text)).astype(np.int16)
    pcm = np.concatenate([np.zeros(RATE, np.int16), speech, np.zeros(3 * RATE, np.int16)])
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype("<i2").tobytes())
    print(f"{out}: {text}, 语音 {len(speech) / RATE:.2f} s, 总长 {len(pcm) / RATE:.2f} s")


if __name__ == "__main__":
    main()
