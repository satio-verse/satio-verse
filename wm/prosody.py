"""韵律分析: 一段合成语音 -> 重音和句末语调事件, 给前端的说话表演 (点头、扬眉、歪头) 排时间.

每 10 ms 一帧: 能量 (dB) 和基频 (归一化自相关, 75-500 Hz, 有声才算). 基频换成相对这段中位数的半音.
  - 重音: 有声帧上 "能量 z 分 + 0.6 x 音高 z 分" 的局部极大 (前后 120 ms 内最大), 超过阈值, 相邻至少 0.35 s.
    strength 0..1; hi 表示音高明显高于平均 (> 2 个半音), 适合配扬眉.
  - 句末: 一段有声之后静音 >= 120 ms 是一个语调短语的结尾; 看结尾前 250 ms 的基频走向, 升 > 1.5 个半音是 rise
    (问句/未完的语气), 降 > 1.5 个半音是 fall (陈述收尾).
只用 numpy, 每秒语音约 2-4 ms.
"""
from __future__ import annotations

import numpy as np

SR = 16000
HOP = 160   # 10 ms
WIN = 640   # 40 ms


def _to16k(pcm: np.ndarray, sr: int) -> np.ndarray:
    x = np.asarray(pcm, dtype=np.float32)
    if pcm.dtype == np.int16:
        x = x / 32768.0
    if sr != SR:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(sr, SR)
        x = resample_poly(x, SR // g, sr // g).astype(np.float32)
    return x


def contour(pcm: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """-> (能量 dB, 基频 Hz; 无声帧为 0), 每 10 ms 一帧."""
    x = _to16k(pcm, sr)
    n = max(0, (len(x) - WIN) // HOP + 1)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx] * np.hanning(WIN)[None, :]
    energy = 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-10)
    spec = np.fft.rfft(frames, 2 * WIN, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :WIN]
    ac = ac / (ac[:, :1] + 1e-12)
    lo, hi = SR // 500, SR // 75
    lag = lo + np.argmax(ac[:, lo:hi], axis=1)
    peak = ac[np.arange(n), lag]
    voiced = (peak > 0.45) & (energy > energy.max() - 35)
    f0 = np.where(voiced, SR / lag, 0.0)
    return energy, f0


def warmup() -> None:
    """首次重采样要导入 scipy (约 1-2 s), 服务启动时先做一次."""
    analyze(np.zeros(4410, dtype=np.int16), 22050)


def analyze(pcm: np.ndarray, sr: int) -> list[dict]:
    """-> [{"t": 秒 (相对这段开头), "k": "accent", "s": 0..1, "hi": bool} | {"t", "k": "rise" | "fall"}]."""
    energy, f0 = contour(pcm, sr)
    voiced = f0 > 0
    if voiced.sum() < 5:
        return []
    st = np.zeros_like(f0)
    st[voiced] = 12 * np.log2(f0[voiced] / np.median(f0[voiced]))
    ev: list[dict] = []
    # 重音
    ez = (energy - energy[voiced].mean()) / (energy[voiced].std() + 1e-6)
    sz = st / (st[voiced].std() + 1e-6)
    prom = np.where(voiced, ez + 0.6 * sz, -9.0)
    w, last = 12, -1e9
    for i in range(len(prom)):
        if prom[i] < 0.9 or prom[i] < prom[max(0, i - w):i + w + 1].max():
            continue
        t = i * HOP / SR + WIN / (2 * SR)
        if t - last < 0.35:
            continue
        last = t
        ev.append({"t": round(t, 3), "k": "accent", "s": round(float(np.clip((prom[i] - 0.9) / 1.6, 0, 1)), 2),
                   "hi": bool(st[i] > 2.0)})
    # 句末语调: 有声段之后 >= 120 ms 静音 (或这段结束)
    i, n = 0, len(voiced)
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        j = i
        while j < n and (voiced[j] or (j + 1 < n and voiced[j + 1] and not voiced[j])):  # 容忍单帧掉声
            j += 1
        gap_end = j
        while gap_end < n and not voiced[gap_end]:
            gap_end += 1
        if (gap_end - j >= 12 or gap_end >= n) and j - i >= 15:
            seg = st[max(i, j - 25):j]
            seg = seg[seg != 0] if np.any(seg != 0) else seg
            if len(seg) >= 6:
                half = len(seg) // 2
                delta = float(np.median(seg[half:]) - np.median(seg[:half]))
                kind = "rise" if delta > 1.5 else "fall" if delta < -1.5 else None
                if kind:
                    ev.append({"t": round(j * HOP / SR + WIN / (2 * SR), 3), "k": kind})
        i = gap_end
    return sorted(ev, key=lambda e: e["t"])
