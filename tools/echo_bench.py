"""外放回声离线评测 (wm/echo.py): 合成"视频外放 + 你说话"的场景, 几种做法过同一套 VAD + SenseVoice, 比误听和认字.

    python tools/echo_bench.py [--variants base,full,...] [--seeds 1] [--env clean,fan,...] [--out 结果.json] [--cache 目录]
    python tools/echo_bench.py --core --variants cur,mdf,ls --env fan      只跑回声处理本身 (不过 VAD、识别), 快, 看线性消除

场景 (每个 32 秒, 16 kHz):
- 视频: Kokoro 合成的解说 (男声 / 女声轮换) + 合成的背景音乐 (和弦、贝斯、鼓点); 爱弥斯的台词当动漫片段.
- 外放回声: 喇叭高通 + 软削波, 合成的房间冲激响应 (RT60 0.2–0.5 s), 喇叭到浏览器再到服务端的延迟 40–200 ms, 轻 / 中 / 重三档.
- 你说话: Kokoro 另外几个声音 + 模型自带的三段真人录音, 每场 4 句, 在 -26 dBFS 上下.
- 麦克风帧 40 ms 一帧, 到达时间有 0–20 ms 抖动, 偶尔卡 150 ms 一下子补上 (页面主线程忙).
种类: echo (只有外放, 你不说话) / dt (外放时你说话) / user (没放东西, 只有你说) / hp (戴耳机: 在放, 麦克风里没有).

做法:
- base   原来的: 麦克风原样给 VAD; 没认出字的一段不清"在听"
- reset  只加"没认出字就清掉在听"
- text   对齐 + 整句复核, 麦克风原样
- aec    线性回声消除 (不压残余, 不复核)
- res    线性消除 + 残余压制 (不复核)
- full   线性消除 + 残余压制 + 整句复核 (默认)
- cur    2026-11-11 改之前的回声处理 (backups/echo-ls-20261111/wm/echo.py 原样载入): 线性消除只有 MDF, 没学会就交原样麦克风
  full 在 11-11 之后就是新的 wm/echo.py: 等 hold_ms + 往前留的按环里写到哪减 (pre_safety_ms), 块数 8.
- 下面这几个是 11-11 当天试的草稿, 要加 --module prototypes/echo_ls/echo_with_ls_20261111.py 才有意义 (里面有 linear / unlearned_suppress / 12 块;
  用 wm/echo.py 跑时这些键不存在, 等于 full):
  mdf / ls   线性消除分别用 MDF / 最小二乘 (asr.echo.linear); 都带"没学会也压"、12 块、等 20 ms、往前留的按环里写到哪减
  fixm8      只有"等参考 + 往前留的按环里写到哪减" (块数还是 8, 没学会不压); mdf96 往前最多留 96 ms
  mdf0 / ls0 同 mdf / ls, 但关掉"确认外放、还没学会时也压" (asr.echo.unlearned_suppress=false), 单看它的作用
  这些没进 wm/ (最小二乘、没学会也压、12 块评测里都不比原来好), 数字和原因见 prototypes/echo_ls/README.md.

环境 (--env, 同一个种子的场景在各环境里是同一份视频、同一份回声、同一份你的话, 只有麦克风那一路不同):
- clean       原来的: 麦克风里只有 -64 dBFS 的白噪声, 没有浏览器的处理
- fan         近端稳态底噪: 有色、像风扇 (低频重 + 几根叶片音), -40 dBFS 上下 (--noise-db), 浏览器的降噪和自动增益都关 (11-11 第三次真机)
- fan_ns      fan 加浏览器式降噪 (稳态噪声的谱减, 底噪降约 15 dB, 回声里弱的频点也被削)
- fan_ns_agc  再加几秒尺度的增益摆动 ±3–5 dB (11-11 第二次真机: 降噪和自动增益都开着)
- fan_p2      fan 加"回声有两条径": 弱的 (0.7 倍) 那条比强的早 51 ms, GCC 量到的是强的 (11-11 第三次真机: 量到的延迟在 42 / 93 ms 之间跳,
              线上滤波器恒在 4 dB 上下, 同一段上往前后各 128 ms 的固定线性滤波器却有 18–20 dB). 滤波器往前只留 32 ms 时够不到早的那条
- q33 / q150  真机的时序 (11-11 第三次 / 第二次): 回环 10 ms 一包、写进环再晚 ring_late_ms (默认 15, --net 改), 麦克风帧最快 arrive_ms 到服务端,
              回环 → 麦克风的物理延迟 delay_ms (Q 约 33 / 150 ms), 15 秒起麦克风断流 1.6 秒、恢复后时间线在晚了 51 ms 的时刻重新对齐
浏览器式的处理加在"回声 + 你的话 + 底噪"混好之后、进回声处理之前; 标准答案 (你的话的干净录音) 不受影响.

指标: 外放时你没说话的时候 VAD 起了几次、"在听"挂了多久 (最长一段多久)、交出去几句假话; 你说的话认出了几句、字错率
(以干净录音的识别结果为准)、有几句整句被"复核"当回声丢掉; 戴耳机和没放东西时和原来比有没有变差; 回声处理每秒音频花多少毫秒 (线程 CPU 时间,
不含诊断; 机器忙时也准). 线性消除 dB 和真机诊断同一口径: 外放、参考在出声的那些 5 秒里, "麦克风 - 线性消除后" 的能量差 (mic_db - lin_db),
fit_db 是同一段上一个固定的线性滤波器 (lti_fit_db) 能做到的上限.
只用本机模型和合成的声音, 不录你的麦克风、不录系统声音、不联网.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import zlib
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
from scipy.ndimage import minimum_filter1d
from scipy.signal import butter, lfilter, resample_poly

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm import echo as EC  # noqa: E402
from wm.asr import VadSenseVoiceASR  # noqa: E402
from wm.config import load_config, resolve_path  # noqa: E402
from wm.loopback import RefRing  # noqa: E402

FS = 16000
VIDEO_LINES = [
    "大家好，欢迎回到我的频道，今天我们来聊一聊最近很火的开源大模型。",
    "这次更新最大的亮点，是推理速度直接提升到了原来的五倍。",
    "我们先看一下官方给出的测试数据，十六G显存就能跑二十七B的模型。",
    "很多人问我，这个召回率到底靠不靠谱，我自己也测了一下。",
    "在长文本的场景下，它的表现确实比上一代稳定很多。",
    "不过要注意的是，量化以后精度会有一点下降。",
    "接下来我会演示一下，怎么在自己的电脑上把它部署起来。",
    "首先打开终端，把仓库克隆到本地，然后安装依赖。",
    "如果你用的是笔记本，记得把散热模式调到性能模式。",
    "好了，模型已经加载完成，我们来问它几个问题试试。",
    "可以看到，它的回答速度非常快，基本上是秒出。",
    "总的来说，这个版本还是很值得一试的。",
    "如果觉得这期视频对你有帮助，记得一键三连支持一下。",
    "我们下期再见，拜拜。",
]
USER_LINES = [
    "这个模型我能在自己电脑上跑吗",
    "你觉得他说的五倍速是真的吗",
    "帮我记一下这个视频的标题",
    "等一下我先暂停一下",
    "你看懂他刚才那张图了吗",
    "这个up主讲得还挺清楚的",
    "我们换一个视频看吧",
    "绫音你觉得怎么样",
    "我有点饿了等会儿去吃饭",
    "他用的是什么显卡啊",
    "继续放吧",
    "这一段我没太听懂",
]
VIDEO_VOICES = [60, 90, 30]        # Kokoro v1.1-zh: 3–57 女声, 58–102 男声 (量过基频)
USER_VOICES = [10, 45, 70, 100]
REAL_USER = ["models/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/test_wavs/zh.wav",
             "models/asr/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01/test_wavs/0.wav",
             "models/asr/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01/test_wavs/1.wav"]

VARIANTS = {
    "base": {"guard": False, "reset": False},
    "reset": {"guard": False, "reset": True},
    "text": {"guard": True, "reset": True, "conf": {"dsp": False, "text_check": True}},
    "aec": {"guard": True, "reset": True, "conf": {"res": False, "text_check": False}},
    "res": {"guard": True, "reset": True, "conf": {"text_check": False}},
    "full": {"guard": True, "reset": True, "conf": {}},
    # 2026-11-11 的回声消除改动: 同一组场景、同一组种子比"改之前"和"改之后"
    "cur": {"guard": True, "reset": True, "frozen": True, "conf": {}},
    "mdf": {"guard": True, "reset": True, "conf": {"linear": "mdf", "unlearned_suppress": True}},
    "ls": {"guard": True, "reset": True, "conf": {"linear": "ls", "unlearned_suppress": True, "ls_tau_s": 6.0}},
    "mdf0": {"guard": True, "reset": True, "conf": {"linear": "mdf", "unlearned_suppress": False}},
    "ls0": {"guard": True, "reset": True, "conf": {"linear": "ls", "unlearned_suppress": False, "ls_tau_s": 6.0}},
    "fixm8": {"guard": True, "reset": True, "conf": {"partitions": 8, "unlearned_suppress": False}},  # 只有"等参考 + 往前留的按环里写到哪减"
    "mdf96": {"guard": True, "reset": True, "conf": {"pre_ms": 96, "unlearned_suppress": True}},    # 往前最多留 96 ms (够得着早 51 ms 的径; 会按环里写到哪减)
}
FROZEN = ROOT / "backups" / "echo-ls-20261111" / "wm" / "echo.py"  # 改之前的 wm/echo.py (cur 用)

# 环境: 麦克风那一路在"回声 + 你的话"之外多了什么. fan 的底噪电平 (dBFS) 用 --noise-db 改.
ENVS = {
    "clean": {},
    "fan": {"fan": True},
    "fan_ns": {"fan": True, "ns": True},
    "fan_ns_agc": {"fan": True, "ns": True, "agc": True},
    # 回声里有两条径, 弱的那条早 51 ms (强的那条才是 GCC 量到的延迟): 滤波器只往前留 32 ms 时够不到弱的那条 (11-11 第三次真机: 量到的延迟
    # 在 42 / 93 ms 之间跳, 线上滤波器恒在 4 dB 上下, 同一段上固定的线性滤波器 (往前后各 128 ms) 能做到 18–20 dB)
    "fan_p2": {"fan": True, "p2": (51, 0.7)},
    # 时间线和回环写进环的时序 (11-11 第三次真机: Q = 回环录到 → 麦克风录到 → 最快那批帧送到服务端 只有 33 ms; 回环 10 ms 一包、写进环晚几毫秒;
    # 麦克风断流 1.6 秒后时间线在晚了 51 ms 的时刻重新对齐, 物理延迟没变): 往前留 32 ms 时远端块的尾巴比环里已写到的位置新, 读出来是 0.
    # q33 = Q 约 33 ms, q150 = Q 约 150 ms (第二次真机的样子, 对照). arrive_ms: 麦克风帧最快多久到服务端; delay_ms: 回环 → 麦克风的物理延迟;
    # ring_late_ms: 回环采样写进环再多晚多少 (10 ms 一包采完才写, 本来就晚 0–10 ms, 加上这个 15 就是 15–25 ms: 现在的代码在 q33 上线性消除 4.3 dB、
    # "交处理过的"占 0.59, 和第三次真机一样; 5 时 8.3 dB, 25 时 1.9 dB); stall: (第几秒起, 断流几秒, 恢复那一帧又晚到几秒)
    "q33": {"fan": True, "arrive_ms": 20, "delay_ms": 10, "ring_late_ms": 15, "stall": (15.0, 1.6, 0.051)},
    "q150": {"fan": True, "arrive_ms": 30, "delay_ms": 118, "ring_late_ms": 15, "stall": (15.0, 1.6, 0.051)},
}


# -- 素材 ------------------------------------------------------------------------
def load_wav(p: str) -> np.ndarray:
    with wave.open(str(p)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64) / 32768
        sr = w.getframerate()
    return to16k(x, sr)


def to16k(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == FS:
        return x
    g = np.gcd(sr, FS)
    return resample_poly(x, FS // g, sr // g)


class Voices:
    """Kokoro 合成 (本机 sherpa-onnx), 合成过的存在缓存目录里."""

    def __init__(self, cache: Path):
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        self.tts = None

    def _load(self):
        import sherpa_onnx
        K = "models/tts/kokoro-multi-lang-v1_1/"
        p = lambda s: ",".join(str(resolve_path(x)) for x in s.split(","))  # noqa: E731
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=p(K + "model.onnx"), voices=p(K + "voices.bin"), tokens=p(K + "tokens.txt"),
                data_dir=p(K + "espeak-ng-data"), lexicon=p(K + "lexicon-us-en.txt," + K + "lexicon-zh.txt")),
                num_threads=4, provider="cpu"),
            rule_fsts=p(K + "date-zh.fst," + K + "phone-zh.fst," + K + "number-zh.fst"), max_num_sentences=1)
        self.tts = sherpa_onnx.OfflineTts(cfg)

    def get(self, text: str, sid: int, speed: float = 1.0) -> np.ndarray:
        key = self.cache / f"k{sid}_{speed:.2f}_{hashlib.md5(text.encode()).hexdigest()[:12]}.npy"
        if key.exists():
            return np.load(key)
        if self.tts is None:
            self._load()
        a = self.tts.generate(text, sid=sid, speed=speed)
        x = to16k(np.array(a.samples, np.float64), a.sample_rate)
        np.save(key, x)
        return x


def music(T: int, rng: np.random.Generator) -> np.ndarray:
    """合成的流行乐小段: C-G-Am-F 和弦 (泛音叠加) + 贝斯 + 底鼓 + 踩镲."""
    t = np.arange(T) / FS
    bpm = rng.uniform(88, 120)
    beat = 60 / bpm
    chords = [(261.6, 329.6, 392.0), (196.0, 246.9, 293.7), (220.0, 261.6, 329.6), (174.6, 220.0, 261.6)]
    bass = [130.8, 98.0, 110.0, 87.3]
    x = np.zeros(T)
    bar = 4 * beat
    for i in range(int(T / FS / bar) + 1):
        a, b = int(i * bar * FS), min(T, int((i + 1) * bar * FS))
        if a >= T:
            break
        tt = t[a:b] - t[a]
        env = np.minimum(1, tt / 0.05) * np.exp(-tt / (bar * 1.5))
        for f in chords[i % 4]:
            for h, amp in ((1, 1.0), (2, 0.4), (3, 0.2)):
                x[a:b] += 0.15 * amp * np.sin(2 * np.pi * f * h * tt + rng.uniform(0, 6)) * env
        x[a:b] += 0.35 * np.sin(2 * np.pi * bass[i % 4] * tt) * np.exp(-tt / bar)
    for k in range(int(T / FS / beat) + 1):
        a = int(k * beat * FS)
        n = min(int(0.15 * FS), T - a)
        if n <= 0:
            break
        tt = np.arange(n) / FS
        x[a:a + n] += 0.6 * np.sin(2 * np.pi * (50 + 60 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 25)  # 底鼓
        h = int(a + beat * FS / 2)
        m = min(int(0.04 * FS), T - h)
        if m > 0:
            x[h:h + m] += 0.08 * rng.standard_normal(m) * np.exp(-np.arange(m) / (0.008 * FS))  # 踩镲
    return x / (np.sqrt(np.mean(x ** 2)) + 1e-12)


def rir(rng: np.random.Generator, rt60: float, drr_db: float = 6.0, n_s: float = 0.35) -> np.ndarray:
    """合成的房间冲激响应: 2 ms 处的直达声 + 几个早期反射 + 指数衰减的混响尾巴; drr_db 是直达声比其余的 (笔记本外放
    麦克风离喇叭 10–20 cm, 大约 +5..+12 dB; 桌面音箱远一点, 0..+6 dB)."""
    n = int(n_s * FS)
    t = np.arange(n) / FS
    d0 = int(0.002 * FS)
    tail = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    tail[:d0 + int(0.003 * FS)] = 0
    for _ in range(6):
        k = d0 + int(rng.uniform(0.003, 0.025) * FS)
        tail[k] += rng.uniform(-1, 1) * 3 * np.sqrt(np.mean(tail[tail != 0] ** 2))
    tail *= np.sqrt(10 ** (-drr_db / 10) / np.sum(tail ** 2))
    tail[d0] += 1.0
    return tail


def rms_db(x: np.ndarray) -> float:
    return 10 * np.log10(np.mean(x ** 2) + 1e-20)


def set_db(x: np.ndarray, db: float, mask: np.ndarray | None = None) -> np.ndarray:
    m = x if mask is None else x[mask]
    return x * 10 ** ((db - rms_db(m)) / 20)


def fan_noise(T: int, rng: np.random.Generator, db: float) -> np.ndarray:
    """近端稳态底噪, 像风扇: 低频重、往高频一路衰减 (功率谱 ∝ 1/f), 叠几根叶片通过频率 (100–160 Hz 起的倍频) 的音; 整体 db dBFS.
    (11-11 第三次真机: 关了浏览器降噪以后安静时的底噪从约 -56 涨到约 -40 dBFS.)"""
    X = np.fft.rfft(rng.standard_normal(T))
    f = np.fft.rfftfreq(T, 1 / FS)
    shape = 1.0 / np.sqrt(np.maximum(f, 40.0))
    low = f < 25
    shape[low] *= f[low] / 25
    x = np.fft.irfft(X * shape, T)
    x /= np.sqrt(np.mean(x ** 2))
    t = np.arange(T) / FS
    f0 = rng.uniform(100, 160)
    tones = sum(a * np.sin(2 * np.pi * f0 * h * t + rng.uniform(0, 6.28)) for h, a in ((1, 1.0), (2, 0.7), (3, 0.5), (4, 0.35), (5, 0.25)))
    x = x + 0.4 * tones / np.sqrt(np.mean(tones ** 2))
    return set_db(x, db)


def ns_stationary(x: np.ndarray, noise: np.ndarray | None = None, floor_db: float = -18.0, win_s: float = 1.5) -> np.ndarray:
    """浏览器式降噪的粗模型: 稳态噪声的谱减. 20 ms 帧、10 ms 一移; 噪声的功率谱 (给了 noise 就用它的平均谱: 相当于浏览器里早就收敛好了的
    噪声估计; 没给就取平滑功率谱的滑窗最小值); 维纳式的增益 (最多压 floor_db), 增益在时间上再平滑一点.
    噪声降十几 dB; 回声里比噪声弱的频点 / 时刻也被削, 回声不再是参考的线性函数."""
    N, H = 320, 160
    w = np.sqrt(np.hanning(N + 1)[:-1])

    def stft(s):
        pad = np.concatenate([np.zeros(N), s, np.zeros(2 * N)])
        nfr = (pad.size - N) // H + 1
        idx = np.arange(N)[None, :] + H * np.arange(nfr)[:, None]
        return pad.size, idx, np.fft.rfft(pad[idx] * w, axis=1)
    size, idx, X = stft(x)
    Ps = lfilter([0.2], [1.0, -0.8], np.abs(X) ** 2, axis=0)
    if noise is not None:
        Nn = (np.abs(stft(noise)[2]) ** 2).mean(axis=0)[None, :]
    else:
        Nn = minimum_filter1d(Ps, size=max(3, int(win_s * FS / H)), axis=0, mode="nearest") * 1.7
    xi = np.maximum(Ps / (Nn + 1e-12) - 1.0, 0.0)
    G = np.maximum(xi / (xi + 1.0), 10 ** (floor_db / 20))
    G = lfilter([0.5], [1.0, -0.5], G, axis=0)
    y = np.fft.irfft(X * G, N, axis=1) * w
    out = np.zeros(size)
    np.add.at(out, idx, y)
    return out[N:N + x.size]


def gain_swing(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """浏览器式自动增益的粗模型: 几秒尺度的增益来回摆 ±3–5 dB (正弦, 周期 4–9 秒, 加一点缓慢的随机游走)."""
    t = np.arange(x.size) / FS
    g = rng.uniform(3.0, 5.0) * np.sin(2 * np.pi * t / rng.uniform(4.0, 9.0) + rng.uniform(0, 6.28))
    w = lfilter([1 - 0.99997], [1.0, -0.99997], rng.standard_normal(x.size))
    return x * 10 ** ((g + 0.5 * w / (np.std(w) + 1e-12)) / 20)


def env_echo(echo: np.ndarray, env: str) -> np.ndarray:
    """环境里改回声本身的那一部分 (p2: 多一条径). 总电平不变 (按有声音的那些采样的均方根)."""
    p2 = ENVS[env].get("p2")
    if not p2:
        return echo
    ms, g = p2
    k = int(ms * FS / 1000)
    out = g * echo + np.concatenate([np.zeros(k), echo[:echo.size - k]])  # 强的那条晚 k 个采样, 弱的 (g 倍) 在原来的位置
    act = np.abs(lfilter([1], [1, -0.999], np.abs(echo))) > 1e-4
    return out * (np.sqrt(np.mean(echo[act] ** 2)) / (np.sqrt(np.mean(out[act] ** 2)) + 1e-12)) if act.any() else out


def apply_env(mic: np.ndarray, env: str, seed: int, noise_db: float) -> np.ndarray:
    """环境加在"回声 + 你的话 + 白噪声"混好之后、进回声处理之前. 用单独的随机数, 场景本身的那一份不动 (各环境里是同一份视频、回声、你的话)."""
    spec = ENVS[env]
    if not spec:
        return mic
    rng = np.random.default_rng(seed + 424242)
    fan = None
    if spec.get("fan"):
        fan = fan_noise(mic.size, rng, noise_db)
        mic = mic + fan
    if spec.get("ns"):
        mic = ns_stationary(mic, fan)
    if spec.get("agc"):
        mic = gain_swing(mic, rng)
    return mic


def make_scene(kind: str, level: str, seed: int, voices: Voices, aimisi: list[np.ndarray], dur: float = 32.0,
               env: str = "clean", noise_db: float = -40.0) -> dict:
    rng = np.random.default_rng(seed)
    T = int(dur * FS)
    # 视频声音: 2 秒前奏, 然后一句接一句解说 (隔 0.3–0.8 s), 音乐垫底到结束
    speech = np.zeros(T)
    pos = int(2.0 * FS)
    lines = list(VIDEO_LINES)
    rng.shuffle(lines)
    li = 0
    while pos < T - FS:
        if aimisi and rng.random() < 0.25:
            x = aimisi[int(rng.integers(len(aimisi)))]
        else:
            x = voices.get(lines[li % len(lines)], VIDEO_VOICES[li % len(VIDEO_VOICES)], speed=float(rng.choice([1.0, 1.15])))
            li += 1
        x = set_db(x, -16)
        n = min(x.size, T - pos)
        speech[pos:pos + n] += x[:n]
        pos += n + int(rng.uniform(0.3, 0.8) * FS)
    bgm = set_db(music(T, rng), -16 - rng.uniform(8, 14))
    video = np.clip(speech + bgm, -1, 1)
    # 外放到麦克风
    lv = {"soft": -32.0, "mid": -24.0, "loud": -16.0}[level]
    b, a = butter(2, rng.uniform(200, 350) / (FS / 2), "high")
    clip = rng.uniform(0.5, 0.8)
    spk = lfilter(b, a, np.tanh(video / clip) * clip)
    drr = float(rng.uniform(0, 12))
    echo = np.convolve(spk, rir(rng, rng.uniform(0.2, 0.5), drr))[:T]
    delay = int(rng.uniform(0.04, 0.2) * FS)  # 喇叭 → 麦克风 → 浏览器 (不含帧到达的抖动)
    if ENVS[env].get("delay_ms") is not None:  # 环境里指定了物理延迟 (随机数照抽, 别的不受影响)
        delay = int(ENVS[env]["delay_ms"] * FS / 1000)
    echo = np.concatenate([np.zeros(delay), echo[:T - delay]])
    act = np.abs(lfilter([1], [1, -0.999], np.abs(echo))) > 1e-4
    echo = set_db(echo, lv, act if act.any() else None)
    # 你说的话
    near = np.zeros(T)
    utts = []
    if kind in ("dt", "user", "hp"):
        at = rng.uniform(2.5, 4.0)
        for i in range(4):
            if i == 0 or rng.random() < 0.3:
                src = REAL_USER[int(rng.integers(len(REAL_USER)))]
                x = load_wav(str(ROOT / src))
            else:
                x = voices.get(USER_LINES[int(rng.integers(len(USER_LINES)))], USER_VOICES[int(rng.integers(len(USER_VOICES)))],
                               speed=float(rng.choice([1.0, 1.1])))
            x = np.convolve(x, rir(rng, 0.25, drr_db=8.0, n_s=0.2))[:x.size]
            x = set_db(x, -26 + rng.uniform(-3, 3))
            s = int(at * FS)
            n = min(x.size, T - s)
            if n < FS:
                break
            near[s:s + n] += x[:n]
            utts.append({"start": s, "end": s + n, "clean": x[:n]})
            at += n / FS + rng.uniform(1.5, 4.0)  # 两句之间隔 1.5–4 秒, 不叠在一起
    mic = near + rng.standard_normal(T) * 10 ** (-64 / 20)
    echo = env_echo(echo, env)
    if kind in ("echo", "dt"):
        mic = mic + echo
    ref = video if kind in ("echo", "dt", "hp") else np.zeros(T)
    mic = apply_env(mic, env, seed, noise_db)
    return {"kind": kind, "level": level, "seed": seed, "env": env, "mic": mic, "ref": ref, "utts": utts, "delay": delay, "drr": drr,
            "net": {k: v for k, v in ENVS[env].items() if k in ("arrive_ms", "ring_late_ms", "stall")},
            "echo": echo if kind in ("echo", "dt") else None}


# -- 跑一个做法 ------------------------------------------------------------------
class FakeHub:
    def __init__(self):
        self.ring = RefRing()
        self.lag_hint = None
        self.coupled_at = -1e9

    def available(self):
        return True

    def touch(self):
        pass

    def status(self):
        return {}


_DIAG_T = [0.0]  # EchoDiag 里费时的那几项 (线上在后台线程里算) 用掉的线程 CPU 时间: 不算进回声处理的开销
_MODULES: dict = {}
MODULE_OVERRIDE = [""]  # --module: 不是 cur 的做法也用这个文件里的 wm/echo.py (改之前在别处试新版本用)


def _time_diag(mod) -> None:
    orig = mod.EchoDiag._finish
    if getattr(orig, "_timed", False):
        return

    def timed(*a, **k):
        t = time.thread_time()
        try:
            return orig(*a, **k)
        finally:
            _DIAG_T[0] += time.thread_time() - t
    timed._timed = True
    mod.EchoDiag._finish = staticmethod(timed)


def echo_module(variant: dict):
    """cur 用改之前的 wm/echo.py (原样载入, 叫 wm.echo_frozen), 别的用现在的 (给了 --module 就用那个文件)."""
    path = str(FROZEN) if variant.get("frozen") else (variant.get("module") or MODULE_OVERRIDE[0])
    mod = EC
    if path:
        if path not in _MODULES:
            name = "wm.echo_" + hashlib.md5(path.encode()).hexdigest()[:8]
            spec = importlib.util.spec_from_file_location(name, path)
            _MODULES[path] = importlib.util.module_from_spec(spec)
            sys.modules[name] = _MODULES[path]
            spec.loader.exec_module(_MODULES[path])
        mod = _MODULES[path]
    _time_diag(mod)
    return mod


def run_variant(scene: dict, engine: VadSenseVoiceASR | None, variant: dict, seed: int) -> dict:
    """engine 是 None 时只跑回声处理本身 (--core): 不过 VAD 和识别, 只看线性消除."""
    rng = np.random.default_rng(seed + 999)
    mic, ref = scene["mic"], scene["ref"]
    T = mic.size
    sess = engine.create_session() if engine is not None else None
    hub = FakeHub()
    mod = echo_module(variant) if variant["guard"] else None
    guard = mod.EchoGuard(hub, variant.get("conf") or {}) if mod is not None else None
    rows: list[dict] = []
    if guard is not None:
        if sess is not None:
            sess.echo = guard
            sess.echo_text = bool(guard.conf["text_check"])
        if guard.diag is not None:  # 真机上每 5 秒一行的诊断, 这里当场算完收起来 (线性消除 dB、fit 上限从这里取)
            guard.diag.sync = True
            guard._diag_done = rows.append
    diag0 = _DIAG_T[0]
    T0 = 5000.0
    pk = FS // 100
    rp = 0
    stall_until = -1.0
    pending = []
    events = []
    cost = 0.0
    nframes = T // 640
    net = scene.get("net") or {}
    arrive_base = net.get("arrive_ms", 30) / 1000   # 麦克风帧最快多久到服务端 (从采到这一帧最后一个采样起)
    ring_late = net.get("ring_late_ms", 0) / 1000   # 回环采样写进环再多晚多少 (默认: 10 ms 一包, 这包采完才写进去, 到达时环里最新的比"现在"晚 0–10 ms)
    stall = net.get("stall")                         # (第几秒起, 断流几秒, 恢复那一帧又晚到几秒): 时间线在晚了的时刻重新对齐
    stall_done = False
    for k in range(nframes):
        cap_end = T0 + (k + 1) * 640 / FS
        arrive = cap_end + arrive_base + rng.uniform(0, 0.02)
        if rng.random() < 0.004:
            stall_until = arrive + 0.15
        if stall is not None and not stall_done and cap_end - T0 >= stall[0]:  # 断流: 这以后 stall[1] 秒内的帧都攒着
            stall_until = max(stall_until, T0 + stall[0] + stall[1])
            stall_done = True
            resume_late = stall[2]
        if arrive < stall_until:
            pending.append(k)
            continue
        if stall is not None and stall_done and resume_late:  # 恢复的那一帧晚到 (之后的正常到)
            arrive += resume_late
            resume_late = 0.0
        for kk in pending + [k]:
            while rp < T and T0 + rp / FS < arrive - 0.01 - ring_late:
                hub.ring.push(ref[rp:rp + pk].astype(np.float32), T0 + rp / FS)
                rp += pk
            frame = np.clip(np.round(mic[kk * 640:(kk + 1) * 640] * 32768), -32768, 32767).astype(np.int16)
            if guard is not None:
                t1 = time.thread_time()
                frame = guard.process(frame, arrive)
                cost += time.thread_time() - t1
            if sess is None:
                continue
            for kind, text in sess._process(frame.astype(np.float32) / 32768):
                events.append({"kind": kind, "text": text, "at": sess.seen, "utt_start": sess.utt_start,
                               "meta": dict(sess.last_meta) if kind in ("final", "echo") else None})
        pending = []
    cost -= _DIAG_T[0] - diag0
    delay = guard.out_delay if guard is not None else 0
    return {"events": events, "delay": delay, "cost_ms_per_s": cost / (T / FS) * 1000, "diag": rows,
            "guard": guard.status() if guard is not None else None, "reset": variant["reset"]}


def diag_metrics(rows: list[dict]) -> dict:
    """和真机诊断同一口径: 外放、参考在出声 (ref_db > -50)、这一段里在放声音的块够多 (≥ 100 块 = 2 秒) 的那几行."""
    act = [r for r in rows if r.get("blk", 0) >= 100 and r.get("ref_db", -120.0) > -50 and r.get("coupled")]

    def med(xs, nd=1):
        xs = [x for x in xs if x is not None]
        return round(float(np.median(xs)), nd) if xs else None
    return {"lin_db": med([r["mic_db"] - r["lin_db"] for r in act]), "fit_db": med([r.get("fit_db") for r in act]),
            "fit1_db": med([r.get("fit1_db") for r in act]), "used": med([r["used"] for r in act], 2), "diag_rows": len(act)}


def cer(hyp: str, ref: str) -> float:
    a, b = EC.norm_text(hyp), EC.norm_text(ref)
    if not b:
        return 0.0 if not a else 1.0
    prev = list(range(len(a) + 1))
    for i, cb in enumerate(b, 1):
        cur = [i]
        for j, ca in enumerate(a, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return min(1.0, prev[-1] / len(b))


def score(scene: dict, res: dict, truth: list[str]) -> dict:
    """把事件对到你说的那几句上."""
    d = res["delay"]
    utts = scene["utts"]
    finals = [e for e in res["events"] if e["kind"] == "final"]
    starts = [e["at"] - d for e in res["events"] if e["kind"] == "speech_start"]
    # "在听": speech_start 起, 到 final / utt_end 止 (原来的: 没认出字的一段一直挂到下一句 final)
    listen = 0
    listen_late = 0   # 起点在头 1.5 秒以后的 (开头那一两秒对流起点的几毫秒偏移敏感: 整个场景推后 5 ms, 原来的做法也会多冒出一个误起)
    longest = 0
    on = None
    for e in res["events"]:
        if e["kind"] == "speech_start" and on is None:
            on = e["at"]
        elif e["kind"] == "final" or (e["kind"] == "utt_end" and res["reset"]):
            if on is not None:
                listen += e["at"] - on
                listen_late += (e["at"] - on) if on - d >= 1.5 * FS else 0
                longest = max(longest, e["at"] - on)
                on = None
    if on is not None:
        listen += scene["mic"].size + d - on
        listen_late += (scene["mic"].size + d - on) if on - d >= 1.5 * FS else 0
        longest = max(longest, scene["mic"].size + d - on)
    # 你说话的时间段 (前后各放宽 0.6 s), 之外起的 VAD 都算误起
    busy = np.zeros(scene["mic"].size + FS * 4, bool)
    for u in utts:
        busy[max(0, u["start"] - int(0.6 * FS)):u["end"] + int(0.6 * FS)] = True
    false_starts = sum(1 for s in starts if 0 <= s < busy.size and not busy[s])
    false_starts_late = sum(1 for s in starts if 1.5 * FS <= s < busy.size and not busy[s])
    matched = [[] for _ in utts]
    fake = []
    for f in finals:
        a, b = f["utt_start"] - d, f["at"] - d
        best, ov = None, 0
        for i, u in enumerate(utts):
            o = min(b, u["end"]) - max(a, u["start"])
            if o > ov:
                best, ov = i, o
        if best is None:
            fake.append(f["text"])
        else:
            matched[best].append(f["text"])
    cers = [cer("".join(m), t) for m, t in zip(matched, truth)]
    # 你的话整句被"复核"当成回声丢掉的 (这一段有三成以上落在被丢的那几段里, 又没认出来)
    drops = [(e["utt_start"] - d, e["at"] - d) for e in res["events"] if e["kind"] == "echo"]
    lost = 0
    for i, u in enumerate(utts):
        ov = sum(max(0, min(b, u["end"]) - max(a, u["start"])) for a, b in drops)
        if ov >= 0.3 * (u["end"] - u["start"]) and not (matched[i] and cers[i] < 0.5):
            lost += 1
    return {"n_utts": len(utts), "hit": sum(1 for m, c in zip(matched, cers) if m and c < 0.5), "lost": lost,
            "cer": float(np.mean(cers)) if cers else None, "fake_finals": len(fake), "fake_chars": sum(len(EC.norm_text(x)) for x in fake),
            "false_starts": false_starts, "false_starts_late": false_starts_late, "starts": len(starts), "listen_s": listen / FS,
            "listen_late_s": listen_late / FS, "longest_s": longest / FS,
            "echo_drops": sum(1 for e in res["events"] if e["kind"] == "echo"),
            "cost_ms_per_s": res["cost_ms_per_s"], "fake_texts": fake[:6], **diag_metrics(res.get("diag") or []),
            "guard": {k: res["guard"][k] for k in ("lag_ms", "adapted", "erle_db", "gated", "gcc_ok", "gcc_tried", "echo_drops")}
            if res["guard"] else None}


def lower_priority() -> None:
    """评测放在低优先级跑 (机器上有真的在用的桌宠、服务端时, 别抢它们的 CPU). 用线程 CPU 时间量开销, 优先级低了也不影响数字."""
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:  # noqa: BLE001 - 没有 psutil / 没权限就算了
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="base,reset,text,aec,res,full")
    ap.add_argument("--seeds", type=int, default=1, help="每种场景几个随机种子")
    ap.add_argument("--kinds", default="echo,dt,user,hp")
    ap.add_argument("--levels", default="soft,mid,loud")
    ap.add_argument("--env", default="clean", help="环境, 逗号分隔: " + " / ".join(ENVS))
    ap.add_argument("--noise-db", type=float, default=-40.0, help="fan* 环境里底噪的电平 (dBFS)")
    ap.add_argument("--core", action="store_true", help="只跑回声处理本身, 不过 VAD 和识别 (快; 只看线性消除、开销)")
    ap.add_argument("--threads", type=int, default=0, help="识别用几个线程 (0 = 照配置; 机器上有别的在跑时给 1–2)")
    ap.add_argument("--no-lowprio", action="store_true", help="不降优先级")
    ap.add_argument("--out", default="")
    ap.add_argument("--conf", default="", help="给带回声处理的做法加的参数 (JSON), 调参用")
    ap.add_argument("--net", default="", help="改环境里的时序参数 (JSON), 例如 '{\"ring_late_ms\": 15}' (arrive_ms / delay_ms / ring_late_ms / stall)")
    ap.add_argument("--module", default="", help="别处的 wm/echo.py (改之前先在草稿里试): 不是 cur 的做法都用它")
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "wm_echo_bench"))
    args = ap.parse_args()
    if not args.no_lowprio:
        lower_priority()
    MODULE_OVERRIDE[0] = args.module
    if args.net:
        for e in args.env.split(","):
            ENVS[e].update({k: (tuple(v) if isinstance(v, list) else v) for k, v in json.loads(args.net).items()})
    cfg = load_config()
    engine = None
    if not args.core:
        if args.threads > 0:
            cfg["asr"]["vad_sensevoice"]["num_threads"] = args.threads
        engine = VadSenseVoiceASR(cfg["asr"])
        engine.load()
    voices = Voices(Path(args.cache))
    aimisi = [set_db(load_wav(p), -16) for p in sorted(glob.glob(str(ROOT / "models/tts/aimisi/dataset/wavs/*.wav")))[:20]]
    envs = args.env.split(",")
    # 你说的每句话的"标准答案": 干净录音的识别结果 (各环境同一个场景的话是同一份, 认一次)
    truths: dict = {}
    rows = []
    vnames = args.variants.split(",")
    for env in envs:  # 一个环境一个环境地做 (场景占内存, 做完就放掉)
        scenes = []
        for kind in args.kinds.split(","):
            for level in (args.levels.split(",") if kind in ("echo", "dt") else ["mid"]):
                for s in range(args.seeds):
                    scenes.append(make_scene(kind, level, 100 * s + zlib.crc32(f'{kind}/{level}'.encode()) % 97, voices, aimisi,
                                             env=env, noise_db=args.noise_db))
        for sc in scenes:
            if engine is None:
                sc["truth"] = []
                continue
            key = (sc["kind"], sc["level"], sc["seed"])
            if key not in truths:
                truths[key] = [engine.transcribe(np.clip(np.round(u["clean"] * 32768), -32768, 32767).astype(np.int16)) for u in sc["utts"]]
            sc["truth"] = truths[key]
        for vname in vnames:
            v = dict(VARIANTS[vname])
            if args.conf and v["guard"]:
                v["conf"] = {**(v.get("conf") or {}), **json.loads(args.conf)}
            for sc in scenes:
                res = run_variant(sc, engine, v, sc["seed"])
                if engine is None:
                    r = {"cost_ms_per_s": res["cost_ms_per_s"], **diag_metrics(res["diag"]),
                         "guard": {k: res["guard"][k] for k in ("lag_ms", "adapted", "erle_db", "gated", "gcc_ok", "gcc_tried")} if res["guard"] else None}
                else:
                    r = score(sc, res, sc["truth"])
                r.update(variant=vname, env=sc["env"], kind=sc["kind"], level=sc["level"], seed=sc["seed"])
                rows.append(r)
                print(json.dumps({k: (round(x, 3) if isinstance(x, float) else x) for k, x in r.items() if k not in ("fake_texts",)},
                                 ensure_ascii=False), flush=True)
        del scenes
    # 汇总
    summary: dict = {}
    if engine is None:
        print("\n环境        做法    | 线性消除 dB (mic-lin 中位) / fit 上限 / 逐秒 fit / 交处理过的占比 | 毫秒/秒")
    else:
        print("\n环境        做法    | 外放没说话: VAD误起 (头 1.5 秒以后的) / 在听秒 (头 1.5 秒以后的; 最长段) / 假句(字) | 外放时你说: 认出 / 字错率 / 假句 / 被复核丢 (误起) "
              "| 线性消除 dB / fit 上限 | 没放: 字错率 | 耳机: 字错率 | 毫秒/秒")
    for env in envs:
        for vname in vnames:
            R = [r for r in rows if r["variant"] == vname and r["env"] == env]
            if not R:
                continue

            def agg(kind, key, fn=np.mean):
                xs = [r[key] for r in R if r["kind"] == kind and r.get(key) is not None]
                return float(fn(xs)) if xs else float("nan")

            def med(key, kinds=("echo", "dt")):
                xs = [r[key] for r in R if r["kind"] in kinds and r.get(key) is not None]
                return float(np.median(xs)) if xs else float("nan")
            cost = float(np.mean([r["cost_ms_per_s"] for r in R if r["kind"] in ("echo", "dt")] or [0.0]))
            if engine is None:
                s = {"lin_db": med("lin_db"), "fit_db": med("fit_db"), "fit1_db": med("fit1_db"), "used": med("used"), "cost": cost}
                print(f"{env:11s} {vname:7s} | {s['lin_db']:5.1f} / {s['fit_db']:5.1f} / {s['fit1_db']:5.1f} / {s['used']:.2f} | {cost:.1f}")
            else:
                e = [r for r in R if r["kind"] == "echo"]
                dtr = [r for r in R if r["kind"] == "dt"]
                s = {"echo_false_starts": sum(r["false_starts"] for r in e), "echo_listen_s": sum(r["listen_s"] for r in e),
                     "echo_false_starts_late": sum(r.get("false_starts_late", 0) for r in e),
                     "echo_listen_late_s": sum(r.get("listen_late_s", 0.0) for r in e),
                     "echo_longest_s": max([r["longest_s"] for r in e] or [0.0]),
                     "echo_fake": sum(r["fake_finals"] for r in e), "echo_fake_chars": sum(r["fake_chars"] for r in e),
                     "dt_hit": f"{sum(r['hit'] for r in dtr)}/{sum(r['n_utts'] for r in dtr)}", "dt_cer": agg("dt", "cer"),
                     "dt_fake": sum(r["fake_finals"] for r in dtr), "dt_lost": sum(r["lost"] for r in dtr),
                     "dt_false_starts": sum(r["false_starts"] for r in dtr),
                     "lin_db": med("lin_db"), "fit_db": med("fit_db"),
                     "user_cer": agg("user", "cer"), "hp_cer": agg("hp", "cer"), "cost": cost}
                print(f"{env:11s} {vname:7s} | {s['echo_false_starts']:3d} ({s['echo_false_starts_late']:3d}) / {s['echo_listen_s']:6.1f} ({s['echo_listen_late_s']:5.1f}; 最长 {s['echo_longest_s']:4.1f}) / "
                      f"{s['echo_fake']}({s['echo_fake_chars']}) | {s['dt_hit']} / {s['dt_cer']:.3f} / {s['dt_fake']} / {s['dt_lost']} "
                      f"(误起 {s['dt_false_starts']}) | {s['lin_db']:5.1f} / {s['fit_db']:5.1f} | {s['user_cer']:.3f} | {s['hp_cer']:.3f} | {s['cost']:.1f}")
            summary.setdefault(env, {})[vname] = s
    if args.out:
        Path(args.out).write_text(json.dumps({"rows": rows, "summary": summary, "args": vars(args)}, ensure_ascii=False, indent=1),
                                  encoding="utf-8")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
