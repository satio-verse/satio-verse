"""哼唱: 把一小段简谱或内置曲调合成成"哼"的声音 (纯 numpy, 不经过 TTS 引擎).

TTS 只会说话, 唱不出调子; 哼唱不用字, 自己按音符算基频和共振峰就够了. 这里只做一小段
(默认最多 8 秒), 桌宠接在说话后面下发 (wm/pipeline.py), QQ 那边做成语音条 (wm/channels/manager.py).

- 跟调子: 原话里带简谱 (1-7, 0 休止, # / b 升降, ^ / ' 升八度, v / , 降八度, - 延长一拍) 就照它哼;
  说了内置曲名 (小星星、两只老虎、生日快乐、欢乐颂) 就哼那一首; 只说"哼两句"就哼小星星的第一段.
- 只在明确让她哼/唱时触发, "你喜欢唱歌吗"这种闲聊不触发.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from .audio import encode_wav, to_int16

SAMPLE_RATE = 24000
MAX_SECONDS = 8.0
DEFAULT_BPM = 120.0
_BASE_MIDI = 60  # 简谱 1 = C4 (261.6 Hz), 哼唱大概在这个音区

# 简谱级数 -> 相对 1 的半音数
_DEGREE = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}

# 内置的几首短曲 (只取前两句, 8 秒以内). 每首都是简谱字符串, 和用户写的格式一样.
_TUNES = {
    "小星星": "1 1 5 5 6 6 5 4 4 3 3 2 2 1",
    "两只老虎": "1 2 3 1 1 2 3 1 3 4 5 3 4 5",
    "生日快乐": "5 5 6 5 1^ 7 5 5 6 5 2^ 1^",
    "欢乐颂": "3 3 4 5 5 4 3 2 1 1 2 3 3 2 2",
}
_ALIASES = {
    "小星星": ("小星星", "一闪一闪亮晶晶", "twinkle"),
    "两只老虎": ("两只老虎", "two tigers"),
    "生日快乐": ("生日快乐", "生日歌", "happy birthday"),
    "欢乐颂": ("欢乐颂", "ode to joy"),
}

# 明确在让她哼/唱 (不是聊到唱歌): "唱歌"本身不算, 要有量词、称呼或者"跟着调子"
_HUM_ASK = re.compile(
    r"(?:哼|唱)(?:两句|一段|一首|一个|一下|首歌|个歌|小曲|小调|支歌)|"
    r"(?:给|让|请|来|和|跟|陪|听|要|你|我|咱们|我们)\s*(?:我|你)?\s*(?:哼|唱)|"
    r"(?:哼|唱)\s*(?=[#b]?[0-7])|"
    r"跟着.{0,6}(?:调子|哼)|"
    r"(?:^|[，。！？,.!?\s])(?:哼|唱)\s*$"
)
# 点了曲名时, 去掉曲名以后剩下的话像不像在让她唱 ("唱小星星" / "给我唱小星星吧"), 而不是 "我喜欢唱小星星"
_DIRECT = re.compile(r"(?:^|[，。！？,.!?\s]|给|让|请|来|和|跟|陪|听|要|你|我|咱们|我们)\s*(?:我|你)?\s*(?:哼|唱).{0,4}$")
# 看起来像一小段简谱: 至少三个音, 可以带升降号、八度记号、延长线
_NOTATION = re.compile(r"(?:[#b])?[0-7][\^v',]*(?:-+)?")
_HAS_NOTATION = re.compile(r"(?:[#b]?[1-7][\^v',]*(?:-+)?[\s,，、/]*){3,}")
# 明确说别唱/不要唱时不算 (别把"不要唱了"听成点歌)
_NO_HUM = re.compile(r"(?:别|不要|不用|不想|不许|不准|别再|不要再)\s*(?:哼|唱)")


@dataclass(frozen=True)
class Hum:
    """一段哼唱: name 是曲名 (给日志和字幕), notes 是 [(MIDI, 拍数)], bpm 是速度."""
    name: str
    notes: tuple[tuple[int, float], ...]
    bpm: float = DEFAULT_BPM

    @property
    def seconds(self) -> float:
        beat = 60.0 / (self.bpm or DEFAULT_BPM)
        return sum(b for _, b in self.notes) * beat


def _parse_notation(seq: str, bpm: float = DEFAULT_BPM) -> tuple[tuple[int, float], ...]:
    """简谱字符串 -> [(MIDI, 拍数)]. 0 是休止, MIDI 0 表示不发声."""
    notes: list[tuple[int, float]] = []
    for tok in _NOTATION.findall(seq):
        m = re.match(r"([#b]?)([0-7])([\^v',]*)(-*)", tok)
        if not m:
            continue
        acc, deg, octave, hold = m.groups()
        midi = 0 if deg == "0" else _BASE_MIDI + _DEGREE[int(deg)] + (1 if acc == "#" else -1 if acc == "b" else 0)
        if midi:
            midi += 12 * (octave.count("^") + octave.count("'") - octave.count("v") - octave.count(","))
            midi = max(24, min(96, midi))
        notes.append((midi, 1.0 + len(hold)))
    return tuple(notes)


def _find_tune(text: str) -> str | None:
    lower = text.lower()
    for name, aliases in _ALIASES.items():
        if any(a.lower() in lower for a in aliases):
            return name
    return None


def _direct_with_tune(text: str, tune: str) -> bool:
    """点了曲名时, 把曲名拿掉再看: "唱小星星" 是让她唱, "我喜欢唱小星星" 只是聊到."""
    rest = text
    for alias in _ALIASES.get(tune, ()):
        rest = re.sub(re.escape(alias), "", rest, flags=re.I)
    return bool(_DIRECT.search(rest))


def _limit(notes: tuple[tuple[int, float], ...], bpm: float, max_seconds: float = MAX_SECONDS):
    """只留前 max_seconds 秒 (在音与音之间截断, 不把一个音切一半)."""
    beat = 60.0 / (bpm or DEFAULT_BPM)
    out, used = [], 0.0
    for midi, beats in notes:
        if used + beats * beat > max_seconds:
            break
        out.append((midi, beats))
        used += beats * beat
    return tuple(out)


def request(text: str) -> Hum | None:
    """原话是不是在让她哼/唱. 是的话返回一段要哼的调子, 否则 None."""
    s = str(text or "").strip()
    if not s or _NO_HUM.search(s):
        return None
    tune = _find_tune(s)
    if not (_HUM_ASK.search(s) or (tune is not None and _direct_with_tune(s, tune))):
        return None
    notes: tuple[tuple[int, float], ...] = ()
    if _HAS_NOTATION.search(s):
        notes = _parse_notation(s)
    name = tune or ""
    if not notes and tune:
        notes = _parse_notation(_TUNES[tune])
    if not notes:  # 只说"哼两句"没点歌: 哼小星星的第一段
        name = name or "小星星"
        notes = _parse_notation(_TUNES[name])
    if not notes:
        return None
    return Hum(name or "跟着调子", _limit(notes, DEFAULT_BPM))


def midi_hz(midi: int) -> float:
    return 440.0 * 2.0 ** ((int(midi) - 69) / 12.0)


def synth(hum: Hum, sample_rate: int = SAMPLE_RATE, max_seconds: float = MAX_SECONDS) -> np.ndarray:
    """把一段哼唱合成成 int16 PCM. 闭口哼唱的音色: 基频 + 少量谐波, 两个很低的共振峰, 带一点颤音和滑音."""
    sr = int(sample_rate)
    beat = 60.0 / (hum.bpm or DEFAULT_BPM)
    out: list[np.ndarray] = []
    remaining = max(0.0, float(max_seconds)) * sr
    prev = None
    for midi, beats in hum.notes:
        if remaining <= 0:
            break
        n = min(int(round(float(beats) * beat * sr)), int(remaining))
        if n <= 0:
            continue
        remaining -= n
        t = np.arange(n, dtype=np.float32) / sr
        if midi <= 0:  # 休止
            out.append(np.zeros(n, np.float32))
            prev = None
            continue
        f0 = midi_hz(midi)
        # 音与音之间滑过去 (45 ms), 再叠 5 Hz 左右的小颤音, 听起来像人在哼而不是电子音
        base = np.full(n, f0, np.float32) if prev is None else \
            prev + (f0 - prev) * np.minimum(1.0, t / 0.045)
        freq = base * (1.0 + 0.012 * np.sin(2 * np.pi * 5.2 * t + midi))
        phase = np.cumsum(2 * np.pi * freq / sr)
        wave = np.zeros(n, np.float32)
        for h in range(1, 13):
            fh = h * f0
            if fh > sr / 2:
                break
            # 闭口哼唱: 第一共振峰约 300 Hz, 第二约 900 Hz; 高次谐波很快衰减
            g = (0.9 / h ** 1.15) * (0.45 + 0.9 * math.exp(-((fh - 300) / 170) ** 2)
                                     + 0.5 * math.exp(-((fh - 900) / 260) ** 2))
            wave += g * np.sin(h * phase).astype(np.float32)
        env = np.minimum(1.0, t / 0.025) * np.minimum(1.0, (t[-1] - t) / 0.06 + 0.04)
        if n > int(0.05 * sr):  # 每个音收一点, 两个音之间听得出来
            tail = int(0.02 * sr)
            env[-tail:] *= np.linspace(1.0, 0.15, tail, dtype=np.float32)
        out.append((wave * env).astype(np.float32))
        prev = f0
    if not out:
        return np.zeros(0, np.int16)
    y = np.concatenate(out)
    pad = np.zeros(int(0.06 * sr), np.float32)
    y = np.concatenate([pad, y, pad])
    peak = float(np.abs(y).max()) or 1.0
    return to_int16(y / peak * 0.32)


def wav_bytes(hum: Hum, sample_rate: int = SAMPLE_RATE) -> bytes:
    """QQ 语音条用: 一小段 WAV (协议端会转成它要的格式)."""
    return encode_wav(synth(hum, sample_rate), sample_rate)


def label(hum: Hum) -> str:
    """字幕、日志里写的一句 (不含调子本身, 免得把简谱念出来)."""
    return f"♪ {hum.name}" if hum.name else "♪ 哼唱"
