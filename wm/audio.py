"""音频小工具: int16/float32 转换、WAV 编解码、重采样、电平."""
from __future__ import annotations

import io
import wave
from math import gcd

import numpy as np


def to_int16(x: np.ndarray) -> np.ndarray:
    if x.dtype == np.int16:
        return x
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)


def to_float32(x: np.ndarray) -> np.ndarray:
    if x.dtype == np.float32:
        return x
    return x.astype(np.float32) / 32768.0


def encode_wav(pcm: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(to_int16(pcm).tobytes())
    return buf.getvalue()


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """返回 (int16 单声道, 采样率). 非 16 位 PCM (如 float WAV) 交给 scipy."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if width != 2:
            raise wave.Error("非 16 位")
        pcm = np.frombuffer(frames, dtype=np.int16)
    except wave.Error:
        from scipy.io import wavfile
        sr, arr = wavfile.read(io.BytesIO(data))
        ch = 1 if arr.ndim == 1 else arr.shape[1]
        pcm = to_int16(arr.astype(np.float32)) if arr.dtype.kind == "f" else arr.astype(np.int16)
        pcm = pcm.reshape(-1)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return pcm, int(sr)


def resample(pcm: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to or len(pcm) == 0:
        return pcm
    from scipy.signal import resample_poly
    g = gcd(int(sr_from), int(sr_to))
    y = resample_poly(to_float32(pcm), sr_to // g, sr_from // g)
    return to_int16(y.astype(np.float32))


def rms_db(pcm: np.ndarray) -> float:
    if len(pcm) == 0:
        return -120.0
    x = to_float32(pcm)
    return float(20.0 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9))
