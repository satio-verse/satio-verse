"""哼唱: 认她是不是在点歌、简谱/内置曲调、合成出来的音高和时长 (纯 numpy, 不装新东西)."""
import io
import wave

import numpy as np
import pytest

from wm import hum as H


def test_request_only_when_she_is_asked():
    assert H.request("哼两句").name == "小星星"
    assert H.request("唱首歌吧").name == "小星星"
    assert H.request("给我唱小星星吧").name == "小星星"
    assert H.request("唱两只老虎").name == "两只老虎"
    assert H.request("跟着这个调子：1 1 5 5 6 6 5").name == "跟着调子"
    # 只是聊到唱歌, 没让她唱
    assert H.request("你喜欢唱歌吗") is None
    assert H.request("我喜欢唱小星星") is None
    assert H.request("群里有人唱歌") is None
    assert H.request("别唱了") is None and H.request("不要哼了") is None
    assert H.request("") is None


def test_explicit_notation_follows_the_tune():
    h = H.request("跟着这个调子：1 1 5 5 6 6 5")
    assert [n for n, _ in h.notes] == [60, 60, 67, 67, 69, 69, 67]
    assert h.seconds == pytest.approx(3.5)
    # 升降号、八度、延长、休止
    h = H.request("哼 1 #1 2^ 0 3-")
    assert [n for n, _ in h.notes] == [60, 61, 74, 0, 64]
    assert [b for _, b in h.notes] == [1, 1, 1, 1, 2]


def test_long_notation_is_cut_at_eight_seconds():
    h = H.request("哼 " + " ".join(["1"] * 30))
    assert h.seconds <= H.MAX_SECONDS and len(h.notes) == 16


def _pitch(pcm, sr, start=0.08, length=0.35):
    x = pcm[int(start * sr):int((start + length) * sr)].astype(np.float32)
    x -= x.mean()
    corr = np.correlate(x, x, "full")[len(x) - 1:]
    lo, hi = int(sr / 500), int(sr / 100)
    lag = lo + int(np.argmax(corr[lo:hi]))
    return sr / lag


def test_synth_makes_a_hum_at_the_note_pitch():
    h = H.Hum("单音", ((60, 2.0),))  # C4 = 261.6 Hz
    pcm = H.synth(h)
    assert pcm.dtype == np.int16 and len(pcm) > H.SAMPLE_RATE
    assert _pitch(pcm, H.SAMPLE_RATE) == pytest.approx(261.6, rel=0.04)
    assert np.abs(pcm).max() > 1000

    buf = io.BytesIO(H.wav_bytes(h))
    with wave.open(buf, "rb") as w:
        assert w.getframerate() == H.SAMPLE_RATE and w.getnchannels() == 1 and w.getnframes() == len(pcm)


def test_empty_notes_is_silence():
    assert len(H.synth(H.Hum("空", ()))) == 0
