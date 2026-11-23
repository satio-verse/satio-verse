"""输出端音色补偿 (wm.tts.ToneShaper) 的行为约定.

背景: 自己微调出来的声音比生产低约 4 dB、8 kHz 以上能量只有三分之一, 听感偏闷偏轻。
补偿放在 TTS 客户端 (wm), 按声音 profile 配置; 换回别的音色时配置里没有 output, 必须完全不动音频。
"""
from __future__ import annotations

import numpy as np
import pytest

from wm.tts import HttpTTS, ToneShaper

SR = 32000


def _tone(freq: float, ms: int, amp: float = 0.1) -> np.ndarray:
    n = int(SR * ms / 1000)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def _rms_db(x: np.ndarray) -> float:
    y = x.astype(np.float64) / 32768.0
    return 20 * np.log10(np.sqrt((y ** 2).mean()) + 1e-12)


def _hf_ratio(x: np.ndarray) -> float:
    y = x.astype(np.float64) / 32768.0
    spec = np.abs(np.fft.rfft(y)) ** 2
    freqs = np.fft.rfftfreq(len(y), 1 / SR)
    total = spec.sum() + 1e-20
    return float(spec[freqs > 8000].sum() / total)


def test_gain_only_scales_level():
    shaper = ToneShaper(SR, gain_db=4.0)
    src = _tone(220, 500)
    out = shaper.process(src)
    assert _rms_db(out) - _rms_db(src) == pytest.approx(4.0, abs=0.15)


def test_shelf_brightens_high_band():
    rng = np.random.default_rng(0)
    src = (rng.normal(0, 0.05, SR // 2) * 32767).astype(np.int16)
    before = _hf_ratio(src)
    out = ToneShaper(SR, shelf_db=6.0, shelf_fc=6500).process(src)
    # 白噪声在 8k 以上本来就占一半能量, +6 dB 搁架让这半边的份额继续上升
    assert _hf_ratio(out) > before + 0.15


def test_streaming_chunks_match_one_shot():
    rng = np.random.default_rng(1)
    src = (rng.normal(0, 0.05, SR) * 32767).astype(np.int16)
    one_shot = ToneShaper(SR, gain_db=4.0, shelf_db=1.7).process(src)

    chunked = ToneShaper(SR, gain_db=4.0, shelf_db=1.7)
    parts = [chunked.process(src[i:i + 1600]) for i in range(0, len(src), 1600)]
    # 块边界会因 int16 取整有 ±1 的差, 只要求整体一致
    assert np.abs(np.concatenate(parts).astype(np.int32) - one_shot.astype(np.int32)).max() <= 2


def test_inactive_shaper_leaves_audio_untouched():
    shaper = ToneShaper(SR)  # 没有参数 = 不启用
    src = _tone(220, 100)
    assert not shaper.active
    assert shaper.process(src) is src


def test_clipping_is_guarded():
    loud = (np.ones(SR // 10) * 0.99 * 32767).astype(np.int16)
    out = ToneShaper(SR, gain_db=12.0).process(loud)
    assert out.dtype == np.int16
    assert np.abs(out).max() <= 32767


def test_http_tts_reads_output_block(tmp_path, monkeypatch):
    monkeypatch.setattr("wm.tts.TONE_FILE", tmp_path / "nope.json")  # 不让仓库里的热加载文件插手
    cfg = {"http": {"base_url": "http://127.0.0.1:9880", "sample_rate": SR,
                    "output": {"gain_db": 4.0, "shelf_db": 1.7, "shelf_fc": 6500}}}
    tts = HttpTTS(cfg)
    sh = tts.shaper()
    assert sh is not None
    assert sh.gain_db == 4.0

    plain = HttpTTS({"http": {"base_url": "http://127.0.0.1:9880", "sample_rate": SR}})
    assert plain.shaper() is None


def test_tone_file_is_hot_reloaded(tmp_path, monkeypatch):
    """换声音时只改 config/tts-tone.json, 服务端不用重启 —— 靠 mtime 判断."""
    tone = tmp_path / "tts-tone.json"
    monkeypatch.setattr("wm.tts.TONE_FILE", tone)
    tts = HttpTTS({"http": {"base_url": "http://127.0.0.1:9880", "sample_rate": SR}})
    assert tts.shaper() is None

    tone.write_text('{"gain_db": 4.0, "shelf_db": 1.7, "shelf_fc": 6500}', encoding="utf-8")
    sh = tts.shaper()
    assert sh is not None and sh.gain_db == 4.0 and sh.shelf_db == 1.7

    # 同一路径重写 (mtime 变) 后应当读到新参数
    import os
    import time as _t
    _t.sleep(0.01)
    tone.write_text('{"gain_db": 2.0}', encoding="utf-8")
    os.utime(tone, ns=(tone.stat().st_atime_ns + 1000, tone.stat().st_mtime_ns + 1000))
    sh2 = tts.shaper()
    assert sh2 is not None and sh2.gain_db == 2.0 and sh2.shelf_db == 0.0


def test_limiter_caps_peaks() -> None:
    """提亮+提音量后峰值会顶到满刻度, 软限幅要把峰值压回阈值以内 (硬削顶是听得出来的破音)."""
    loud = _tone(220.0, 300, amp=0.95)
    sh = ToneShaper(SR, gain_db=9.0, shelf_db=4.0, limit=0.9)
    out = sh.process(loud)
    peak = np.abs(out.astype(np.float64) / 32768.0).max()
    assert peak <= 0.9005, peak
    assert peak > 0.5, peak  # 限幅不能把音量压没


def test_limiter_keeps_quiet_signal_linear() -> None:
    """限幅只该管峰值; 小信号要原样通过 (否则每句话都被压扁, 音色反而变差)."""
    quiet = _tone(440.0, 200, amp=0.05)
    sh = ToneShaper(SR, limit=0.9)
    out = sh.process(quiet)
    err = np.abs(out.astype(np.float64) - quiet.astype(np.float64)).max()
    assert err <= 40, err  # tanh 在很小的输入上近似线性


def test_limiter_alone_counts_as_active() -> None:
    assert ToneShaper(SR, limit=0.9).active
    assert not ToneShaper(SR, limit=0.0).active
    assert not ToneShaper(SR, limit=1.0).active