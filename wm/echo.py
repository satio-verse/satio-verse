"""麦克风里的回声: 外放时视频 (和别的程序) 放出来的声音被麦克风收进去, 别当成你在说话.

浏览器自带的回声消除只认它自己放的声音 (她说话), 你在 Firefox、B 站客户端里放的视频它不知道. 这里拿系统声音回环
(wm/loopback.py, 电脑此刻正在放的声音) 当参考, 在服务端把它从麦克风里去掉, 再交给 VAD 和识别:

1. 对齐: 麦克风帧按到达时间粗对一次, 再用 GCC-PHAT 量出回声比参考晚多少 (外放 → 麦克风 → 浏览器 → 这里, 常见 60–300 ms,
   蓝牙音箱更长), 之后两边都按采样数接着走, 隔一会儿复量一次, 变了就把滤波器跟着挪.
2. 线性回声消除: Speex 那套 MDF (分块频域自适应滤波, 前景 / 背景两套滤波器, 按漏回声估计调步长), 160 ms 长.
   参考是从回环录来的, 写进环总要晚一点: 远端块 (从对齐点往前 pre 个采样起、一块长) 的尾巴比环里已写到的位置新时读出来是 0, 这些块的谱
   进了滤波器, 后面 M 块的回声估计都带着洞, 卡在平台 (11-11 第三次真机: Q = 回环录到 → 麦克风录到 → 最快那批帧送到服务端 只有 33 ms).
   所以麦克风的块多攒 hold_ms 再处理 (参考多写进来一点), 每块记环里还剩多少, 不够 pre_safety_ms 就把往前留的减下来.
3. 残余回声压制: 线性去不掉的 (喇叭失真、混响) 按回声估计逐频点压; 一帧里几乎全是回声时整帧压到 -35 dB, 免得 VAD 起来.
4. 整句复核 (在 wm/asr.py): 识别出来的这句话要是几乎都能在同一段系统声音的识别结果里按顺序找到, 就当是视频里的话, 丢掉.

没在放声音、戴耳机 (麦克风里听不到参考)、不是 Windows 时不压, 原样放过去 (和压过的一样晚 hold_ms + 20 ms: 输出一直是输入晚固定那么多, 开始 / 停放的那一刻不插静音).
参考声音只在内存里, 不存、不传; 只有麦克风开着才录.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from scipy import fft as sfft
from scipy.signal import fftconvolve, lfilter

from .loopback import RATE, LoopbackHub

log = logging.getLogger("wm.echo")

DEFAULTS = {
    "enabled": True,          # 外放回声处理 (要系统声音回环, 只在 Windows 上)
    "text_check": True,       # 整句复核: 识别结果几乎都在同一段系统声音里就丢掉
    "text_ratio": 0.6,        # 这句话里有多少字能在系统声音的识别结果里按顺序找到, 算回声
    "block": 320,             # 20 ms 一块
    "partitions": 8,          # 滤波器 8 块 = 160 ms (从量出来的延迟往前留 pre_ms). 12 块 (240 ms) 评测里线性消除一样 (±0.1 dB)、你说话时认出的也没多, 只多 1–4 ms/s 的算力, 没用
    # 参考 (回环) 写进环总要晚一点: 远端块 (从对齐点往前 pre 个采样起、一块长) 的尾巴比环里已写到的位置新时, 读出来是 0, 这些块的谱进了滤波器,
    # 后面 M 块的回声估计都带着洞, 线上滤波器卡在平台. 块尾比已写到的位置新多少 = pre + 回环写进环的延迟 - Q (Q = lag + skew_ms: 回环录到 →
    # 麦克风录到 → 最快那批帧送到服务端; 时间线重对只换映射, 不改它). 11-11 第三次真机 Q 只有 33 ms、往前留 32 ms, 线上只消 4 dB (tools/echo_bench.py
    # 的 q33 环境复现了). 两个办法一起用: 麦克风的块多等一会儿再处理, 不够时再把往前留的减下来.
    "hold_ms": 40,            # 麦克风的块多攒这么多更新的采样才处理 (整块, 20 ms 的倍数): 多等一会儿参考就多写进来一点. 整个输出也晚这么多, 一直一样
                              # (不管在不在外放, 开始 / 停放的那一刻不插静音、不丢采样), 识别不在乎; 回环卡住时麦克风照样往前走 (这个等待只数麦克风自己的采样, 不等回环)
    "pre_ms": 32,             # 滤波器往前最多留这么多 (直达声前面那一段: 回声里比量到的延迟早的那点能量); 加大要多等. 评测里 96 ms 只在假想的 fan_p2 (有条弱径早 51 ms: 4 → 9 dB) 更好, 其余环境没有更好, 真机时序 q33 上还差 1 dB (7.8 → 6.8)
    "pre_safety_ms": 3,       # 远端块的尾巴比环里已写到的位置至少要早这么多; 最近 5 秒里最小的余量不到这个数, 就把往前留的减到够 (只往小调; 换设备回到 pre_ms)
    "pre_min_ms": 8,          # 往前留的不少于这么多
    "pre_calm_s": 3.0,        # 读的位置刚挪过 (时间线重对、复量延迟) 以后这么久不记余量、不调 pre (这几秒的余量是乱的)
    "lag_guess_ms": 100,      # 还没量到之前先按这个对齐
    "max_lag_ms": 1000,       # 回声最多比参考晚这么多 (蓝牙音箱 200–300 ms)
    "gcc_window_s": 2.0,
    "gcc_every_s": 0.5,
    "gcc_conf": 9.0,          # GCC-PHAT 峰值的 z 分数够这么高才算量到了 (没有回声时一般 4–5, 偶尔 8 上下)
    "confirm_s": 6.0,         # 两次量到的延迟要在这么久之内、差不到 2 ms 才算"外放" (一次的偶然峰不算)
    "min_erle_db": 2.0,       # 线性消除确实消掉了这么多才压残余 (戴耳机时消不掉什么, 不压)
    "coupled_s": 30.0,        # 量到回声以后这么久内都算"外放"
    "engage_s": 3.0,          # 系统声音停了这么久以后不再处理 (原样放过去)
    "dsp": True,              # 交出去的是去掉回声的 (关掉: 只对齐、量耦合给整句复核用, 麦克风原样交出去)
    "res": True,              # 残余回声压制 (关掉只剩线性消除)
    "nl": True,               # 残余里按参考功率估的那部分 (喇叭失真、长混响; 关掉只按线性回声估计压)
    "nl_quantile": 50,
    "nl_overestimate": 2.0,
    "res_overestimate": 2.0,  # 残余回声估计放大几倍再压 (3 dB)
    "res_floor_db": -15.0,    # 你在说话的帧: 回声占上风的频点最多压这么多
    "gate_floor_db": -45.0,   # 几乎全是回声的帧: 整帧压这么多 (-35 时 VAD 还常被残渣叫起来, 评测里"在听"多一倍)
    "gate_presence": 0.15,    # 一帧里扣掉回声以后剩下的能量占比低于这个, 算"全是回声"
    "gate_overestimate": 3.0,  # 判"全是回声"时残余估计放大几倍 (整个频段一起比)
    "diag_s": 5.0,            # 外放时每这么多秒在日志里记一行诊断 (EchoDiag: 只有数字, 不录音、不记字); 0 = 不记
    "realign_min": 8,         # 复量的延迟和现在用的差这么多个采样 (0.5 ms) 才挪滤波器 (2026-11-11 真机: 差 3 个就挪, 154.8↔155.0 ms 来回挪了 9 次)
    # 页面取麦克风时要浏览器做哪些处理 (这里能去掉外放回声时才给页面, 见 mic_processing): 降噪和自动增益夹在"喇叭到麦克风"和这里的
    # 回声消除之间, 把回声变得不再是参考的线性函数 (2026-11-11 真机: 固定的线性滤波器最多也只消得掉约 12 dB), 默认关掉
    "mic_ec": True,           # 浏览器的回声消除 (管她自己的声音; 也可以写 "all" / "remote-only", 看浏览器认不认)
    "mic_ns": True,           # 浏览器的降噪 (11-11 第三次真机试关掉过: 上限涨了, 可底噪从 -56 涨到 -40 dBFS, 滤波器学不动; 先开着, 第四次试只关自动增益)
    "mic_agc": False,         # 浏览器的自动增益
    "post_gain": True,        # 浏览器的自动增益关了时, 回声处理之后自己补音量 (SpeechGain)
    "post_gain_target_db": -26.0,
    "post_gain_max_db": 12.0,
}


def mic_processing(conf: dict, available: bool) -> dict | None:
    """给页面的麦克风处理 (hello 的 mic; 页面那边是 web/audio.js 的 micConstraints). 这里能去掉外放回声时 (Windows、系统声音
    回环可用、开着) 按配置给; 不能时 None: 浏览器照默认, 回声消除、降噪、自动增益都开着, 和原来一样."""
    if not (available and conf.get("enabled", True)):
        return None
    ec = conf.get("mic_ec", True)
    ec = ec if isinstance(ec, str) and ec in ("all", "remote-only") else bool(ec)
    return {"echoCancellation": ec, "noiseSuppression": bool(conf.get("mic_ns", False)),
            "autoGainControl": bool(conf.get("mic_agc", False))}


class SpeechGain:
    """回声处理之后的慢音量调整, 浏览器的自动增益关了时用 (页面报来的 mic_settings): 把你说话的音量往 post_gain_target_db 上拉.
    只在电脑没在放声音时学 (那时麦克风里只有你和底噪), 外放时冻住: 不跟着视频的回声去调, 也不会因为回声大就把你调小.
    只往上补 (最多 post_gain_max_db), 不往下压; 补完会削顶的那一帧少补一点. 往上每秒最多 2.5 dB, 往下每帧 0.5 dB."""

    def __init__(self, conf: dict | None = None):
        c = {**DEFAULTS, **(conf or {})}
        self.target = float(c["post_gain_target_db"])
        self.max_db = float(c["post_gain_max_db"])
        self.gain_db = 0.0
        self.levels: collections.deque = collections.deque(maxlen=200)  # 最近学到的说话帧的音量 (40 ms 一帧, 约 8 秒的话)
        self.floor: collections.deque = collections.deque(maxlen=250)   # 最近 10 秒的帧音量 (估底噪)
        self.prev = 1.0          # 上一帧末尾用的线性增益 (帧内平滑过渡, 不出咔哒声)
        self.learned = 0
        self.logged_db, self.logged_at = 0.0, -1e9

    def process(self, pcm: np.ndarray, engaged: bool) -> np.ndarray:
        x = pcm.astype(np.float64)
        if not x.size:
            return pcm
        if not engaged and np.any(pcm):  # 没在放声音, 也不是半双工时页面发来的静音帧
            lv = 10 * np.log10(float(np.mean(x * x)) / 32768.0 ** 2 + 1e-12)
            self.floor.append(lv)
            noise = float(np.percentile(self.floor, 10)) if len(self.floor) >= 25 else -70.0
            if lv > max(noise + 12.0, -55.0):  # 比底噪高 12 dB 以上: 多半是你在说话
                self.levels.append(lv)
                self.learned += 1
                if len(self.levels) >= 25:  # 攒够 1 秒的话才开始调
                    want = min(max(self.target - float(np.percentile(self.levels, 75)), 0.0), self.max_db)
                    self.gain_db += max(-0.5, min(0.1, want - self.gain_db))
                    now = time.perf_counter()
                    if abs(self.gain_db - self.logged_db) >= 1.0 and now - self.logged_at >= 10.0:
                        self.logged_db, self.logged_at = self.gain_db, now
                        log.info("补音量: %+.1f dB (你说话约 %.1f dBFS, 学了 %d 帧)", self.gain_db,
                                 float(np.percentile(self.levels, 75)), self.learned)
        g = 10 ** (self.gain_db / 20)
        peak = float(np.max(np.abs(x)))
        if peak * g > 30000.0:  # 补了要削顶: 这一帧少补点
            g = max(1.0, 30000.0 / max(peak, 1.0))
        # 往上从上一帧的增益慢慢过渡过来 (不出咔哒声); 往下这一帧直接用低的 (从高的过渡下来, 前半帧照样会削顶)
        y = x * (np.linspace(self.prev, g, x.size, endpoint=False) if g > self.prev else g)
        self.prev = g
        return np.clip(np.round(y), -32768, 32767).astype(np.int16)

_PUNCT = re.compile(r"[^0-9a-z㐀-鿿぀-ヿ가-힯]+")


def norm_text(s: str) -> str:
    return _PUNCT.sub("", (s or "").lower())


def lcs_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def echo_ratio(heard: str, ref: str) -> float:
    """heard (麦克风识别结果) 里按顺序能在 ref (同一段系统声音的识别结果) 里找到的字占多少."""
    a, b = norm_text(heard), norm_text(ref)
    if not a:
        return 0.0
    return lcs_len(a, b) / len(a)


def gcc_phat(mic: np.ndarray, ref_ext: np.ndarray, max_lag: int, beta: float = 0.8) -> tuple[int, float]:
    """mic 长 W; ref_ext 长 W + max_lag, ref_ext[max_lag + n] 和 mic[n] 是"零延迟"对齐的.
    返回 (lag, z): mic[n] 最像 ref_ext[max_lag - lag + n], z 是峰值的 z 分数 (越大越确定)."""
    nfft = sfft.next_fast_len(mic.size + ref_ext.size, real=True)
    A = sfft.rfft(mic - mic.mean(), nfft)
    B = sfft.rfft(ref_ext - ref_ext.mean(), nfft)
    G = np.conj(A) * B
    G /= np.abs(G) ** beta + 1e-12
    c = sfft.irfft(G, nfft)[:max_lag + 1]
    k = int(np.argmax(c))
    sd = float(np.std(c))
    z = (float(c[k]) - float(np.mean(c))) / sd if sd > 0 else 0.0
    return max_lag - k, z


def lti_fit_db(x: np.ndarray, d: np.ndarray, nfft: int = 4096, fs: int = RATE) -> float | None:
    """这一段上"一个不随时间变的线性滤波器"能把参考 x 从麦克风 d 里消掉多少 dB (x、d 已经按量到的延迟对齐, 一样长).
    做法: 按 nfft 点一帧求平均的互谱, H = Sxd / Sxx, 变回冲激响应 (零延迟前后各 nfft/2 点) 去滤 x, 从 d 里减掉, 比话音频段的功率.
    是拿这一段自己拟合自己, 和线上那个一边走一边学的滤波器比着看:
    - 比线上实际消掉的高得多 = 线上的滤波器没学好 / 对齐在晃, 毛病在这边;
    - 本身就低 = 麦克风里的回声在这一段里不是参考的固定线性函数 (延迟在漂或在跳、有自动增益在动、浏览器的降噪、喇叭失真),
      或者麦克风里主要是别的声音. 再看短一点的一段 (逐秒) 是不是高得多: 是 = 这几秒里路径在变.
    毫不相干的两路约 0 dB; 最多报 40 dB; 帧太少 (不到 6 帧) 返回 None."""
    h = lti_h(x, d, nfft)
    return None if h is None else fit_with(x, d, h, nfft, fs)


def lti_h(x: np.ndarray, d: np.ndarray, nfft: int = 4096) -> np.ndarray | None:
    """lti_fit_db 的前半: 这一段整段拟合出来的冲激响应 h (长 nfft, 下标 nfft // 2 是零延迟, 前后各 nfft / 2 点); 帧太少返回 None."""
    hop = nfft // 2
    n = min(x.size, d.size)
    k = (n - nfft) // hop + 1
    if k < 6:
        return None
    x, d = x[:n], d[:n]
    w = np.hanning(nfft)
    idx = np.arange(nfft)[None, :] + hop * np.arange(k)[:, None]
    X = sfft.rfft(x[idx] * w, axis=1)
    D = sfft.rfft(d[idx] * w, axis=1)
    Sxx = (np.abs(X) ** 2).sum(axis=0)
    Sxd = (np.conj(X) * D).sum(axis=0)
    H = Sxd / (Sxx + 1e-3 * float(Sxx.mean()) + 1e-9)
    return np.roll(sfft.irfft(H, nfft), nfft // 2)  # 中间是零延迟


def fit_with(x: np.ndarray, d: np.ndarray, h: np.ndarray, nfft: int = 4096, fs: int = RATE) -> float | None:
    """lti_fit_db 的后半: 用冲激响应 h (下标 nfft // 2 是零延迟) 去滤 x, 从 d 里减掉, 比话音频段的功率 (最多报 40 dB)."""
    n = min(x.size, d.size)
    x, d = x[:n], d[:n]
    y = fftconvolve(x, h)[nfft // 2:nfft // 2 + n]
    core = slice(nfft // 2, n - nfft // 2)  # 两头滤波器没吃满, 不算
    f = np.fft.rfftfreq(core.stop - core.start, 1 / fs)
    band = (f >= 300) & (f <= 3400)
    pd = float((np.abs(sfft.rfft(d[core])) ** 2)[band].sum())
    pr = float((np.abs(sfft.rfft(d[core] - y[core])) ** 2)[band].sum())
    if pd <= 0:
        return None
    return round(10 * np.log10(pd / max(pr, pd * 1e-4)), 1)


def fit_many(x: np.ndarray, d: np.ndarray, hs: list, nfft: int = 4096, fs: int = RATE) -> list:
    """fit_with 一次算好几个冲激响应 (hs 里是 None 的跳过) 的拟合: x 的 FFT 只做一次 (诊断的后台线程里用, 省算力)."""
    n = min(x.size, d.size)
    x, d = x[:n], d[:n]
    core = slice(nfft // 2, n - nfft // 2)
    f = np.fft.rfftfreq(core.stop - core.start, 1 / fs)
    band = (f >= 300) & (f <= 3400)
    pd = float((np.abs(sfft.rfft(d[core])) ** 2)[band].sum())
    if pd <= 0:
        return [None] * len(hs)
    big = sfft.next_fast_len(n + nfft - 1, real=True)
    X = sfft.rfft(x, big)
    out = []
    for h in hs:
        if h is None:
            out.append(None)
            continue
        y = sfft.irfft(X * sfft.rfft(h, big), big)[nfft // 2:nfft // 2 + n]
        pr = float((np.abs(sfft.rfft(d[core] - y[core])) ** 2)[band].sum())
        out.append(round(10 * np.log10(pd / max(pr, pd * 1e-4)), 1))
    return out


def ir_summary(h: np.ndarray, pre: int, win: int, fs: int = RATE) -> dict:
    """整段拟合出的冲激响应 h (下标 len // 2 是零延迟) 的几个数 (诊断用): 能量落在滤波器窗口之前 / 里面 / 之后各占多少 (窗口 = 对齐点前
    pre 个采样起、共 win 个采样), 最高的三个峰 ([相对对齐点的毫秒数, 幅度 dB], 隔开 1 ms 以上才算另一个), 底 (所有抽头幅度的中位数 dB:
    峰比它高得多才算真有东西)."""
    k = np.arange(h.size) - h.size // 2
    e = h * h
    tot = float(e.sum())
    before, after = k < -pre, k >= win - pre
    inside = ~(before | after)
    a = np.abs(h)
    peaks: list[tuple[int, float]] = []
    for i in np.argsort(-a):
        if all(abs(int(i) - j) > 16 for j, _ in peaks):
            peaks.append((int(i), float(a[i])))
        if len(peaks) == 3:
            break
    return {"ir_frac": [round(float(e[m].sum()) / tot, 3) for m in (before, inside, after)] if tot > 0 else [None, None, None],
            "ir_peaks": [[round(float(i - h.size // 2) * 1000 / fs, 2), round(20 * np.log10(v + 1e-9), 1)] for i, v in peaks],
            "ir_floor_db": round(20 * np.log10(float(np.median(a)) + 1e-9), 1)}


def band_split(d: np.ndarray, e: np.ndarray, fs: int = RATE) -> dict:
    """麦克风 d 的能量在 <300 / 300–3400 / >3400 Hz 各占多少; 线性消除后的 e 比 d 在这三个频带各低多少 dB (诊断用)."""
    n = min(d.size, e.size)
    f = np.fft.rfftfreq(n, 1 / fs)
    D = np.abs(sfft.rfft(d[:n])) ** 2
    E = np.abs(sfft.rfft(e[:n])) ** 2
    masks = (f < 300, (f >= 300) & (f <= 3400), f > 3400)
    pd = [float(D[m].sum()) for m in masks]
    pe = [float(E[m].sum()) for m in masks]
    tot = sum(pd)
    return {"band_mic": [round(v / tot, 3) for v in pd] if tot > 0 else [None, None, None],
            "band_lin_db": [round(10 * np.log10(a / max(b, 1e-9)), 1) if a > 0 else None for a, b in zip(pd, pe)]}


def fine_offset(d: np.ndarray, x: np.ndarray, max_shift: int = 640, beta: float = 0.8) -> tuple[int, float]:
    """已经对齐的麦克风 d 和参考 x (一样长) 之间还差几个采样 (正 = 回声比对齐的位置晚), 只在 ±max_shift 里找
    (默认 ±40 ms: 浏览器或声卡丢掉一个 10 ms 的缓冲也看得到). 返回 (偏移, z); z 是峰值相对整条相关的 z 分数,
    低了说明这一段里找不到回声. 看延迟漂不漂、跳不跳用."""
    n = sfft.next_fast_len(d.size + max_shift, real=True)
    A = sfft.rfft(d - d.mean(), n)
    B = sfft.rfft(x - x.mean(), n)
    G = A * np.conj(B)
    G /= np.abs(G) ** beta + 1e-12
    c = sfft.irfft(G, n)
    near = np.concatenate([c[-max_shift:], c[:max_shift + 1]])
    k = int(np.argmax(near))
    sd = float(np.std(c))
    z = (float(near[k]) - float(np.mean(c))) / sd if sd > 0 else 0.0
    return k - max_shift, z


def _dbfs(power: float) -> float:
    """int16 量级的均方 → dBFS."""
    return round(10 * np.log10(power / 32768.0 ** 2 + 1e-12), 1)


class EchoDiag:
    """外放时每 every 秒出一行只有数字的诊断, 好在真机上看出"消不干净"卡在哪 (2026-11-11 真机: 线性消除只有 4 dB,
    延迟在 37–55 ms 之间跳, 评测里看不出来). 这几秒的麦克风和参考只在内存里, 算完数字就丢: 不落盘、不上传、不记字.
    费时的那几项 (拟合、逐秒量延迟, 一次几十毫秒) 在后台线程里算, 不挡着回声处理. 一行里有:
      s / frames / gap_ms   这一段多长、来了多少帧麦克风、两帧之间最长隔了多久 (页面被压着时会成批到)
      skew_ms               麦克风这边比对上时间线那一刻"晚到"了多少 (按最近一秒里到得最早的一帧算): 一步一步变大 =
                            页面那边在丢采样 (或者送得越来越晚), 对照 fine_ms 有没有跟着跳
      blk / zero / idle     对上时间线、在放声音的块数 (20 ms 一块); 半双工发来的静音块; 没在放声音的块
      used / gated          这些块里交出"处理过的"占多少; 判成"几乎全是回声"整帧压掉的占多少
      pre_ms / pre_cut      滤波器现在往前留多少毫秒; 这一段里因为远端块的尾巴读到没写的位置、把往前留的减下来几次
      mic_db / ref_db / lin_db / out_db   麦克风、参考、线性消除后、最后交出去的音量 (dBFS)
      le_med                每块线性消除消掉多少 dB 的中位数 (线上那个一边走一边学的滤波器实际做到的)
      fit_db / fit1_db      拿这一段自己拟合一个固定的线性滤波器能消掉多少 dB (lti_fit_db): 整段 / 逐秒拟合再取中位数
                            (逐秒的帧短, 有混响时自己就只有十来 dB). le_med 远低于 fit_db = 线上的滤波器没学好;
                            fit_db 低而 fit1_db 没跟着低 = 这几秒里路径在变 (看 fine_ms、gain_db); 两个都低 =
                            回声不是参考的线性函数 (浏览器的降噪、喇叭失真), 或者麦克风里主要是别的声音
      fine_ms               逐秒量的延迟偏移 (毫秒; 量不到的是 null): 一路变大变小 = 在漂, 忽高忽低 = 在跳
      gain_db               逐秒"麦克风比参考响多少": 来回摆好几 dB = 有自动增益在动
      lag_ms / realign / resync / gcc / z   现在用的延迟; 这一段里换了几次延迟、重对了几次时间线; GCC 量了几次 / 成了几次; 最近一次的 z
      ref_fill / ref_skew_ms   回环这边: 这一段里时间线上补了几次静音、一共多少毫秒 (正在连着放时不该有); 连着的包的时间戳
                            和按采样数排的位置最多差多少毫秒
      erle_db / leak / coupled / adapted    滤波器自己的读数
    11-11 第三次真机后加的几项 (只测不改, 不影响处理; 算在后台线程里, 算不出来就不给):
      q_ms                  物理延迟 Q = lag_ms + skew_ms: 回环录到 → 麦克风录到 → 最快那批帧送到服务端 (时间线重新对齐只换映射, 不改它)
      tail_blk / tail_miss / tail_min_ms / tail_short_ms
                            回环正在出声的这些块 (tail_blk) 里, 远端块读完以后环里还剩多少采样没读: 有几块是负的 (读到了还没写的位置, 这块
                            的尾巴读出来是 0: tail_miss), 最小余量 (毫秒, 负的 = 缺了这么多), 缺的块平均缺多少毫秒. 往前留得多、Q 小、回环
                            写进环晚时会有 (块尾比环里已写到的位置新了 pre + 写进环的延迟 - Q)
      band_mic / band_lin_db   麦克风能量在 <300 / 300–3400 / >3400 Hz 各占多少; 线上线性消除 (mic 比 lin) 在这三个频带各多少 dB
                            (fit_db 只算中间那个频带, mic_db / lin_db / le_med 是全频带)
      ir_frac / ir_peaks / ir_floor_db   整段拟合出的冲激响应 (零延迟前后各 128 ms, 相对对齐点): 能量落在滤波器窗口 (对齐点前 pre_ms 起共
                            partitions 块) 之前 / 里面 / 之后各占多少; 最高的三个峰 [位置 ms, 幅度 dB]; 底 (所有抽头幅度的中位数 dB)
      fit_win_db            冲激响应只留窗口里那段时的拟合 = 这个窗口的滤波器最多能做到多少 (比 fit_db 低很多 = 能量在窗口外)
      fit_prev_db           上一段拟合出的冲激响应拿来滤这一段 (比 fit_db 低很多 = 路径在变, 或者对齐换了: 对照 lag_ms / realign)
      leak_med / mu_med / erle_med   滤波器自己 (每 5 块取一次) 的 leak、归一化步长 (自适应时算作回声的那部分占残余的比例)、erle 的中位数"""

    def __init__(self, N: int, every_s: float, sync: bool = False):
        self.N, self.every = N, float(every_s)
        self.sync = sync  # True: 当场算完 (测试、离线评测用); False: 费时的交给后台线程
        self.t0: float | None = None
        self.last_frame: float | None = None
        self.prev = {"realign": 0, "resync": 0, "gcc_tried": 0, "gcc_ok": 0, "pre_cut": 0}
        self.prev_fill: tuple[int, int] | None = None
        self.gcc_z: float | None = None
        self.prev_h: list = [None]  # 上一段整段拟合出的冲激响应 (fit_prev_db 用; 后台线程读写, 只有诊断用)
        self._reset()

    def _reset(self) -> None:
        self.frames, self.gap = 0, 0.0
        self.blocks = self.zero = self.idle = self.used = self.gated = 0
        self.p_mic = self.p_ref = self.p_lin = self.p_out = 0.0
        self.n_out = 0
        self.le: list[float] = []
        self.d: list[np.ndarray] = []
        self.x: list[np.ndarray] = []
        self.eb: list[np.ndarray] = []              # 线性消除后的块 (分频带看消除用)
        self.tail_n = self.tail_miss = 0            # 远端块: 回环出声时读了几块、其中几块读到了还没写的位置
        self.tail_min: int | None = None            # 最小余量 (采样数, 负的 = 缺了这么多)
        self.tail_short: list[int] = []             # 缺的块各缺多少个采样
        self.fstat: list[tuple] = []                # 滤波器自己的读数 (leak, erle, 归一化步长), 每 5 块一份

    def frame(self, t: float) -> None:
        if self.t0 is None:
            self.t0 = t
        if self.last_frame is not None:
            self.gap = max(self.gap, t - self.last_frame)
        self.last_frame = t
        self.frames += 1

    def skip(self, zero: bool) -> None:
        if zero:
            self.zero += 1
        else:
            self.idle += 1

    def block(self, d: np.ndarray, x: np.ndarray, e: np.ndarray, out: np.ndarray | None, le: float,
              used: bool, gated: bool) -> None:
        self.blocks += 1
        self.used += used
        self.gated += gated
        self.p_mic += float(d @ d)
        self.p_ref += float(x @ x)
        self.p_lin += float(e @ e)
        if out is not None:
            self.p_out += float(out @ out)
            self.n_out += 1
        self.le.append(float(le))
        self.d.append(d.copy())
        self.x.append(x)
        self.eb.append(e.copy())

    def tail(self, margin: int) -> None:
        """远端块读完以后, 环里已写到的位置比这块的尾巴新多少个采样 (负的 = 读到了还没写的位置: 这块的尾巴读出来是 0). 只是数数."""
        self.tail_n += 1
        if margin < 0:
            self.tail_miss += 1
            self.tail_short.append(-margin)
        if self.tail_min is None or margin < self.tail_min:
            self.tail_min = margin

    def filt(self, leak: float, erle: float, mu: float | None) -> None:
        """滤波器自己的读数 (leak、erle、归一化步长), 每 5 块记一份."""
        self.fstat.append((float(leak), float(erle), None if mu is None else float(mu)))

    def due(self, now: float) -> bool:
        return self.t0 is not None and now - self.t0 >= self.every

    def flush(self, now: float, guard: "EchoGuard", done) -> None:
        """这一段攒够了: 便宜的读数当场取, 费时的交给后台线程, 算完调 done(这一行); 清空重来.
        这一段里在放声音的不到半秒就不出."""
        ring = guard.ring
        fills = (getattr(ring, "fills", 0), getattr(ring, "fill_n", 0))
        if self.blocks >= 25:
            st, n = guard.stats, self.blocks * self.N
            was = self.prev_fill or fills
            skew = None
            if guard.sync_c0 is not None and guard.clock_recent is not None:
                skew = round((guard.clock_recent - guard.sync_c0) * 1000, 1)
            row = {"s": round(now - (self.t0 or now), 1), "frames": self.frames, "gap_ms": round(self.gap * 1000),
                   "skew_ms": skew, "blk": self.blocks, "zero": self.zero, "idle": self.idle,
                   "used": round(self.used / self.blocks, 2), "gated": round(self.gated / self.blocks, 2),
                   "pre_ms": round(guard.pre * 1000 / RATE, 1), "pre_cut": st["pre_cut"] - self.prev["pre_cut"],
                   "mic_db": _dbfs(self.p_mic / n), "ref_db": _dbfs(self.p_ref / n), "lin_db": _dbfs(self.p_lin / n),
                   "out_db": _dbfs(self.p_out / (self.n_out * self.N)) if self.n_out else None,
                   "le_med": round(float(np.median(self.le)), 1),
                   "lag_ms": None if guard.lag is None else round(guard.lag * 1000 / RATE, 1),
                   "realign": st["realign"] - self.prev["realign"], "resync": st["resync"] - self.prev["resync"],
                   "gcc": [st["gcc_tried"] - self.prev["gcc_tried"], st["gcc_ok"] - self.prev["gcc_ok"]],
                   "z": None if self.gcc_z is None else round(self.gcc_z, 1),
                   "ref_fill": [fills[0] - was[0], round((fills[1] - was[1]) * 1000 / RATE)],
                   "ref_skew_ms": round(float(getattr(ring, "skew_max", 0.0)) * 1000, 2),
                   "erle_db": round(float(guard.mdf.erle), 1), "leak": round(float(guard.mdf.leak), 3),
                   "coupled": guard.coupled(), "adapted": bool(guard.mdf.adapted)}
            job = (row, self.d, self.x, self.eb, (guard.pre, guard.M * guard.N), self.prev_h, self.fstat,
                   (self.tail_n, self.tail_miss, self.tail_min, self.tail_short))
            if self.sync:
                done(self._finish(*job))
            else:
                threading.Thread(target=lambda: done(self._finish(*job)), name="echo-diag", daemon=True).start()
        self.prev = {k: guard.stats[k] for k in self.prev}
        self.prev_fill = fills
        if hasattr(ring, "skew_max"):
            ring.skew_max = 0.0
        self.t0 = now
        self._reset()

    @staticmethod
    def _finish(row: dict, d_blocks: list, x_blocks: list, e_blocks: list | None = None, win: tuple | None = None,
                prev: list | None = None, fstat: list | None = None, tail: tuple | None = None) -> dict:
        """费时的几项: 整段和逐秒的线性拟合、逐秒的延迟偏移和音量差. 只往这一行里添数字."""
        d, x = np.concatenate(d_blocks), np.concatenate(x_blocks)
        quiet = (32768.0 * 10 ** (-50 / 20)) ** 2
        fine, gain, fit1 = [], [], []
        for i in range(0, d.size - RATE + 1, RATE):
            ds, xs = d[i:i + RATE], x[i:i + RATE]
            px = float(xs @ xs) / RATE
            if px < quiet:  # 这一秒参考几乎没声音
                fine.append(None)
                gain.append(None)
                continue
            off, z = fine_offset(ds, xs)
            fine.append(round(off * 1000 / RATE, 2) if z >= 6 else None)
            gain.append(round(_dbfs(float(ds @ ds) / RATE) - _dbfs(px), 1))
            c = lti_fit_db(xs, ds, 2048)
            if c is not None:
                fit1.append(c)
        h = lti_h(x, d)  # 整段拟合出的冲激响应: fit_db 和后加的几项共用
        row.update(fit_db=None, fit1_db=round(float(np.median(fit1)), 1) if fit1 else None, fine_ms=fine, gain_db=gain)
        try:  # 后加的几项 (只测不改): 算不出来就不给, 不碍上面的
            row.update(EchoDiag._extra(row, d, x, e_blocks, win, prev, fstat, tail, h))
        except Exception:  # noqa: BLE001
            log.debug("外放诊断: 后加的几项没算出来", exc_info=True)
            if h is not None:
                row["fit_db"] = fit_with(x, d, h)
        return row

    @staticmethod
    def _extra(row: dict, d: np.ndarray, x: np.ndarray, e_blocks: list | None, win: tuple | None, prev: list | None,
               fstat: list | None, tail: tuple | None, h: np.ndarray | None) -> dict:
        """q_ms、参考尾巴、分频带、冲激响应、滤波器自己的读数、上一段的冲激响应 (字段见 EchoDiag 的说明). 每一组各算各的: 哪组算不出来只丢哪组."""
        out: dict = {}

        def put(fn) -> None:
            try:
                out.update(fn())
            except Exception:  # noqa: BLE001
                log.debug("外放诊断: 有一组没算出来", exc_info=True)

        def med(i):
            v = [t[i] for t in fstat if t[i] is not None]
            return round(float(np.median(v)), 3) if v else None
        put(lambda: {"q_ms": (round(row["lag_ms"] + row["skew_ms"], 1)
                              if row.get("lag_ms") is not None and row.get("skew_ms") is not None else None)})
        if tail is not None:
            n, miss, mn, short = tail
            put(lambda: dict(tail_blk=n, tail_miss=miss, tail_min_ms=None if mn is None else round(mn * 1000 / RATE, 1),
                             tail_short_ms=round(float(np.mean(short)) * 1000 / RATE, 1) if short else None))
        if fstat:
            put(lambda: dict(leak_med=med(0), erle_med=None if med(1) is None else round(med(1), 1), mu_med=med(2)))
        if e_blocks:
            put(lambda: band_split(d, np.concatenate(e_blocks)))
        if h is not None:
            k = np.arange(h.size) - h.size // 2
            hw = None
            if win is not None:
                pre, wlen = win
                put(lambda: ir_summary(h, pre, wlen))
                hw = np.where((k >= -pre) & (k < wlen - pre), h, 0.0)
            hp = prev[0] if prev is not None and prev[0] is not None and prev[0].size == h.size else None

            def fits():  # 整段 / 只留窗口 / 上一段的冲激响应各拟合一次 (x 的 FFT 共用)
                a, b, c = fit_many(x, d, [h, hw, hp])
                r = {"fit_db": a}
                if win is not None:
                    r["fit_win_db"] = b
                if prev is not None:
                    r["fit_prev_db"] = c
                return r
            put(fits)
            if "fit_db" not in out:
                put(lambda: {"fit_db": fit_with(x, d, h)})
            if prev is not None:
                prev[0] = h
        return out


class MDF:
    """Speex mdf.c (1.2) 的浮点版, 单声道. 信号按 int16 的量级 (±32768); Speex 的谱按 1/n 归一,
    这里的谱 (numpy) 没归一, 用到谱的常数和步长都按 S2 = (2N)^2 换算过, 和原版等价. 每次 process 一块 N 个采样."""

    MIN_LEAK = 0.005
    VAR1_UPDATE, VAR2_UPDATE, VAR_BACKTRACK = 0.5, 0.25, 4.0

    def __init__(self, N: int = 320, M: int = 8, fs: int = RATE, preemph: float = 0.9):
        self.N, self.M, self.fs, self.pre = N, M, fs, preemph
        self.S2 = float(2 * N) ** 2
        self.win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(2 * N) / (2 * N))
        r = 0.982 if fs < 24000 else 0.992
        den2 = r * r + 0.7 * (1 - r) * (1 - r)
        self.notch_b = np.array([r, -2 * r, r])
        self.notch_a = np.array([1.0, -2 * r, den2])
        self.spec_average = N / fs
        self.beta0 = 2.0 * N / fs
        self.beta_max = 0.5 * N / fs
        self.reset()

    def reset(self) -> None:
        N, M = self.N, self.M
        K = N + 1
        self.X = np.zeros((M + 1, K), complex)
        self.W = np.zeros((M, K), complex)
        self.F = np.zeros((M, K), complex)
        self.E = np.zeros(K, complex)
        self.x_prev = np.zeros(N)
        self.power = np.zeros(K)
        self.power_1 = np.full(K, 1.0 / self.S2)
        self.Eh = np.zeros(K)
        self.Yh = np.zeros(K)
        self.Pey = 1.0
        self.Pyy = 1.0
        self.Davg1 = self.Davg2 = self.Dvar1 = self.Dvar2 = 0.0
        self.leak = 1.0
        self.adapted = False
        self.sum_adapt = 0.0
        self.saturated = 0
        self.screwed = 0
        self.count = 0
        self.notch_zi = np.zeros(2)
        self.memX = self.memD = self.memE = 0.0
        decay = np.exp(-2.4 / M)
        prop = 0.7 * decay ** np.arange(M)
        self.prop = 0.8 * prop / prop.sum()
        self.erle = 0.0  # 平滑的 10*log10(Sdd/See), 看消掉了多少

    def _far(self, far: np.ndarray, mem: float) -> tuple[np.ndarray, float]:
        return far - self.pre * np.concatenate([[mem], far[:-1]]), float(far[-1])

    def prime(self, far_hist: np.ndarray) -> None:
        """用参考的历史 ((M+2)*N 个采样, 最后 N 个是"上一块") 重建 X: 换了对齐、停过一阵以后接着用."""
        N, M = self.N, self.M
        need = (M + 2) * N
        h = np.zeros(need)
        k = min(need, far_hist.size)
        h[need - k:] = far_hist[far_hist.size - k:]
        xp, self.memX = self._far(h, 0.0)
        blocks = xp.reshape(M + 2, N)
        for j in range(M + 1):  # X[j] = [第 b-j-1 块, 第 b-j 块], b 是"上一块"
            b = M + 1 - j
            self.X[j] = np.fft.rfft(np.concatenate([blocks[b - 1], blocks[b]]))
        self.x_prev = blocks[-1].copy()
        self.E[:] = 0

    def shift(self, delta: int) -> None:
        """参考改成早 delta 个采样 (对齐时减的延迟多了 delta): 冲激响应跟着左移 delta, 不用从头收敛.
        之后要 prime 一次 (X 也按新的对齐重建)."""
        if delta == 0:
            return
        N, M = self.N, self.M
        if abs(delta) >= M * N:
            self.reset()
            return
        for filt in (self.W, self.F):
            h = np.fft.irfft(filt, 2 * N, axis=1)[:, :N].reshape(-1)
            h = np.concatenate([h[delta:], np.zeros(delta)]) if delta > 0 else np.concatenate([np.zeros(-delta), h[:delta]])
            filt[:] = np.fft.rfft(np.concatenate([h.reshape(M, N), np.zeros((M, N))], axis=1), axis=1)
        self.E[:] = 0

    def _adjust_prop(self) -> None:
        prop = np.sqrt(1.0 + np.sum(np.abs(self.W) ** 2, axis=1))
        prop += 0.1 * prop.max()
        self.prop = 0.99 * prop / (1.0 + prop.sum())

    def process(self, mic: np.ndarray, far: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """mic, far: 各 N 个采样 (int16 量级的 float). 返回 (去掉回声的麦克风, 回声估计 = mic - 输出)."""
        N, M, S2 = self.N, self.M, self.S2
        n2 = 2 * N
        ss = 0.35 / M
        d_in, self.notch_zi = lfilter(self.notch_b, self.notch_a, mic, zi=self.notch_zi)
        d = d_in - self.pre * np.concatenate([[self.memD], d_in[:-1]])
        self.memD = float(d_in[-1])
        xp, self.memX = self._far(far, self.memX)
        self.X[1:] = self.X[:-1].copy()
        self.X[0] = sfft.rfft(np.concatenate([self.x_prev, xp]))
        self.x_prev = xp
        Sxx = float(xp @ xp)
        y_fg = sfft.irfft(np.sum(self.F * self.X[:M], axis=0), n2)[N:]
        e_fg = d - y_fg
        Sff = float(e_fg @ e_fg)
        if self.adapted:
            self._adjust_prop()
        # 背景滤波器按上一块的误差走一步 (上一块的误差配 X[1:], 晚一块)
        if self.saturated == 0:
            self.W += (self.power_1[None, :] * self.prop[:, None]) * np.conj(self.X[1:M + 1]) * self.E[None, :]
        else:
            self.saturated -= 1
        # 防圆周卷积: 每块约束第 0 块和轮到的一块 (AUMDF)
        idx = [0, 1 + self.count % (M - 1)] if M > 1 else [0]
        w = sfft.irfft(self.W[idx], n2, axis=1)
        w[:, N:] = 0
        self.W[idx] = sfft.rfft(w, axis=1)
        self.count += 1
        y_bg = sfft.irfft(np.sum(self.W * self.X[:M], axis=0), n2)[N:]
        Dbf = 10.0 + float((y_fg - y_bg) @ (y_fg - y_bg))
        e_bg = d - y_bg
        See = float(e_bg @ e_bg)
        # 前景 / 背景: 背景明显更好就拷过去, 明显更差 (双讲时学歪了) 就退回前景
        dS = Sff - See
        self.Davg1 = 0.6 * self.Davg1 + 0.4 * dS
        self.Davg2 = 0.85 * self.Davg2 + 0.15 * dS
        self.Dvar1 = 0.36 * self.Dvar1 + 0.16 * Sff * Dbf
        self.Dvar2 = 0.7225 * self.Dvar2 + 0.0225 * Sff * Dbf
        y_out = y_fg
        if (dS * abs(dS) > Sff * Dbf or self.Davg1 * abs(self.Davg1) > self.VAR1_UPDATE * self.Dvar1
                or self.Davg2 * abs(self.Davg2) > self.VAR2_UPDATE * self.Dvar2):
            self.Davg1 = self.Davg2 = self.Dvar1 = self.Dvar2 = 0.0
            self.F = self.W.copy()
            y_out = self.win[N:] * y_fg + self.win[:N] * y_bg  # 平滑换过去
        elif (-dS * abs(dS) > self.VAR_BACKTRACK * Sff * Dbf or -self.Davg1 * abs(self.Davg1) > self.VAR_BACKTRACK * self.Dvar1
              or -self.Davg2 * abs(self.Davg2) > self.VAR_BACKTRACK * self.Dvar2):
            self.W = self.F.copy()
            y_bg, e_bg, See = y_fg, e_fg, Sff
            self.Davg1 = self.Davg2 = self.Dvar1 = self.Dvar2 = 0.0
        out, _ = lfilter([1.0], [1.0, -self.pre], d - y_out, zi=[self.pre * self.memE])  # 去加重
        self.memE = float(out[-1])
        if self.saturated == 0 and np.any(np.abs(mic) >= 32000):
            self.saturated = 1
        Sey = float(e_bg @ y_bg)
        Syy = float(y_bg @ y_bg)
        Sdd = float(d @ d)
        z = np.zeros(N)
        EY = sfft.rfft(np.stack([np.concatenate([z, e_bg]), np.concatenate([z, y_bg])]), axis=1)
        self.E = EY[0]
        Rf = np.abs(self.E) ** 2 / S2
        Yf = np.abs(EY[1]) ** 2 / S2
        Xf = np.abs(self.X[0]) ** 2 / S2
        self.power = (1 - ss) * self.power + 1.0 + ss * Xf
        Ehd = Rf - self.Eh
        Yhd = Yf - self.Yh
        Pyy = float(np.sqrt(Yhd @ Yhd))
        Pey = float(Ehd @ Yhd) / Pyy if Pyy > 0 else 0.0
        sa = self.spec_average
        self.Eh = (1 - sa) * self.Eh + sa * Rf
        self.Yh = (1 - sa) * self.Yh + sa * Yf
        See = max(See, n2 * 100.0)
        alpha = min(self.beta0 * Syy, self.beta_max * See) / See
        self.Pey = (1 - alpha) * self.Pey + alpha * Pey
        self.Pyy = max(1.0, (1 - alpha) * self.Pyy + alpha * Pyy)
        self.Pey = min(max(self.Pey, self.MIN_LEAK * self.Pyy), self.Pyy)
        self.leak = self.Pey / self.Pyy
        RER = (0.0001 * Sxx + 3.0 * self.leak * Syy) / See
        RER = min(max(RER, Sey * Sey / (1 + See * Syy)), 0.5)
        if not self.adapted and self.sum_adapt > M and self.leak * Syy > 0.03 * Syy:
            self.adapted = True
        if self.adapted:
            r = self.leak * Yf
            e = Rf + 1.0
            r = 0.7 * np.minimum(r, 0.5 * e) + 0.3 * RER * e
            self.power_1 = r / (e * (self.power + 10.0)) / S2
        else:
            rate = min(0.25 * Sxx, See) / See if Sxx > n2 * 1000.0 else 0.0
            self.power_1 = rate / (self.power + 10.0) / S2
            self.sum_adapt += rate
        # 发散了就重来 (Speex 的 "got slapped")
        if not (np.isfinite(Sff) and np.isfinite(See) and Sff < n2 * 1e9 and See < n2 * 1e9):
            self.screwed += 50
        elif Sff > Sdd + n2 * 10000.0:
            self.screwed += 1
        else:
            self.screwed = 0
        if self.screwed >= 50:
            log.info("回声消除发散了, 重来")
            self.reset()
            return mic.astype(np.float64), np.zeros(N)
        if Sdd > n2 * 100.0:
            self.erle = 0.95 * self.erle + 0.05 * 10 * np.log10(Sdd / See)
        return out, mic - out


class Suppressor:
    """残余回声压制 + 全是回声时整帧压. 根号汉宁窗、半重叠 (分析 / 合成同一个窗, 不压时完全还原), 输出晚 N 个采样."""

    def __init__(self, N: int, conf: dict, fs: int = RATE):
        self.N = N
        self.win = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(2 * N) / (2 * N)))
        self.e_buf = np.zeros(2 * N)
        self.y_buf = np.zeros(2 * N)
        self.ola = np.zeros(N)
        self.G = np.ones(N + 1)
        self.R = np.zeros(N + 1)
        f = np.fft.rfftfreq(2 * N, 1 / fs)
        self.band = (f >= 300) & (f <= 3400)
        self.beta = float(conf["res_overestimate"])
        self.floor = 10 ** (float(conf["res_floor_db"]) / 20)
        self.gate_floor = 10 ** (float(conf["gate_floor_db"]) / 20)
        self.presence_thr = float(conf["gate_presence"])
        self.frame_beta = float(conf.get("gate_overestimate", 3.0))
        self.reverb = float(np.exp(-N / fs / 0.08))  # 残余回声按 80 ms 的时间常数拖尾
        self.presence = 1.0
        self.gated = False
        # 线性滤波器描述不了的那部分 (喇叭失真、超出 160 ms 的混响): 按参考的功率估. 系数从"几乎全是回声"的帧里学
        # (这一帧线性消除消掉了 6 dB 以上: 你在说话的帧消不掉你的声音, 到不了), 取最近 10 秒里"残余 / 参考"的中位数
        self.x_buf = np.zeros(2 * N)
        self.ratios: collections.deque = collections.deque(maxlen=max(10, int(10 * fs / N)))
        self.g_nl = 0.0
        self.nl_q = float(conf.get("nl_quantile", 50))
        self.nl_beta = float(conf.get("nl_overestimate", 2.0))
        self.spread = np.ones(9) / 9  # 失真会散到相邻频率上: 参考的谱先抹宽一点

    def process(self, e: np.ndarray, y: np.ndarray, leak: float, active: bool, x: np.ndarray | None = None,
                local_erle_db: float = 0.0) -> np.ndarray:
        N = self.N
        self.e_buf[:N] = self.e_buf[N:]
        self.e_buf[N:] = e
        self.y_buf[:N] = self.y_buf[N:]
        self.y_buf[N:] = y
        if x is not None:
            self.x_buf[:N] = self.x_buf[N:]
            self.x_buf[N:] = x
        if active:
            F = sfft.rfft(self.win * (np.stack([self.e_buf, self.y_buf, self.x_buf]) if x is not None
                                      else np.stack([self.e_buf, self.y_buf])), axis=1)
            E = F[0]
            Y2 = np.abs(F[1]) ** 2
            R = min(1.0, 2.0 * leak) * Y2
            if x is not None:
                Px = np.convolve(np.abs(F[2]) ** 2, self.spread, mode="same")
                xx = float(Px[self.band].sum())
                if xx > 1e3 and local_erle_db >= 6.0:  # 参考在出声, 这一帧几乎全是回声
                    self.ratios.append(float((np.abs(E[self.band]) ** 2).sum()) / xx)
                    if len(self.ratios) >= 10:
                        self.g_nl = float(np.percentile(self.ratios, self.nl_q))
                # 不让它超过线性回声估计的一半 (免得哪里学偏了、反过来压你的声音)
                cap = 0.5 * float(Y2[self.band].sum()) / xx if xx > 1e3 else 0.0
                R = np.maximum(R, self.nl_beta / self.beta * min(self.g_nl, cap) * Px)
            self.R = np.maximum(R, self.reverb * self.R)
            P = np.abs(E) ** 2
            near = np.maximum(P - self.beta * self.R, 0.0)
            # 这一帧是不是"几乎全是回声": 整个话音频段一起比 (逐频点的残余估计对不太准, 加起来就稳了)
            tot = float(P[self.band].sum())
            self.presence = max(0.0, tot - self.frame_beta * float(self.R[self.band].sum())) / tot if tot > 1e-3 else 1.0
            self.gated = self.presence < self.presence_thr
            g = np.sqrt(near / (P + 1e-9))
            g[1:-1] = (g[:-2] + g[1:-1] + g[2:]) / 3  # 相邻频点平滑一点, 少些"音乐噪声"
            g = np.clip(g, self.gate_floor if self.gated else self.floor, 1.0)
            self.G = g if self.gated else np.maximum(g, 0.5 * self.G)  # 你在说话时一块最多收 6 dB, 别把字头字尾吃掉
            E = E * self.G
        else:
            E = sfft.rfft(self.win * self.e_buf)
            self.presence, self.gated = 1.0, False
            self.R[:] = 0
            self.G[:] = 1.0
        frame = sfft.irfft(E, 2 * N) * self.win
        out = self.ola + frame[:N]
        self.ola = frame[N:].copy()
        return out


class EchoGuard:
    """一条连接一个. process() 吃麦克风的 16 kHz int16 帧, 吐同样长的帧 (整体晚 N 个采样)."""

    def __init__(self, hub: LoopbackHub, conf: dict | None = None):
        c = {**DEFAULTS, **(conf or {})}
        self.conf = c
        self.hub = hub
        self.ring = hub.ring
        self.N = int(c["block"])
        self.M = int(c["partitions"])
        self.pre_max = int(c["pre_ms"] * RATE / 1000)
        self.pre_min = min(self.pre_max, int(c["pre_min_ms"] * RATE / 1000))
        self.pre_safety = int(c["pre_safety_ms"] * RATE / 1000)
        self.pre_calm = float(c["pre_calm_s"])
        self.hold = int(round(float(c["hold_ms"]) * RATE / 1000 / self.N)) * self.N  # 采样数, 整块
        self.max_lag = int(c["max_lag_ms"] * RATE / 1000)
        self.gcc_w = int(c["gcc_window_s"] * RATE)
        self.mdf = MDF(self.N, self.M)
        self.sup = Suppressor(self.N, c)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="echo")
        self.lock = threading.Lock()
        self.mic_n = 0                  # 已经切成块处理了的麦克风采样数
        self.in_buf = np.zeros(0)       # 还没凑够一块的
        self.out_q: collections.deque = collections.deque()
        self.out_pending = np.zeros(0)
        self.clock: collections.deque = collections.deque()  # (到达时刻, 零延迟下第 0 个采样的时刻)
        self.c0: float | None = None
        self.clock_recent: float | None = None    # 最近一秒里到得最早的一帧算出来的"第 0 个采样的时刻"
        self.sync: tuple[int, int] | None = None  # (麦克风序号, 环里"零延迟"对应的序号)
        self.sync_c0: float | None = None
        self.sync_epoch = -1
        self.lag: int | None = None     # 回声比参考晚多少个采样 (量到的)
        self.lag_votes: collections.deque = collections.deque(maxlen=3)
        self.gcc_hits: collections.deque = collections.deque(maxlen=6)  # (时刻, 量到的延迟): 互相印证用
        self.pre = self.pre_max                 # 滤波器现在往前留多少个采样 (远端块的尾巴读到没写的位置时往下减, 见 _adapt_pre)
        self.margins: collections.deque = collections.deque(maxlen=250)  # 最近远端块读完以后环里还剩的采样数 (回环出声时的, 最近 5 秒)
        self.pre_hold_until = 0.0               # 这个时刻以前不调 pre (刚挪过 / 刚重对过时间线, 余量是乱的)
        self.gcc_next = 0.0
        self.mic_hist = np.zeros(self.gcc_w)        # 最近的原始麦克风 (GCC 用)
        self.mic_hist_zero = np.zeros(self.gcc_w, bool)  # 半双工发来的零帧不拿来量
        self.need_prime = True
        self.engaged = False
        # (块的麦克风序号, 环里零延迟序号, 这一块扣掉回声后剩下的占比; 没在压回声时是 1)
        self.blocks: collections.deque = collections.deque(maxlen=int(40 * RATE / self.N))
        self.presence_hist: collections.deque = collections.deque(maxlen=max(1, int(0.4 * RATE / self.N)))
        self.stats = {"frames": 0, "engaged": 0, "gated": 0, "gcc_tried": 0, "gcc_ok": 0, "realign": 0,
                      "resync": 0, "echo_drops": 0, "pre_cut": 0}
        self.now = time.perf_counter()  # 最后一帧到达的时刻; 这里的时间都按它算 (离线评测时是模拟的时间线)
        self.res_at = -1e9              # 最后一次压残余回声的时刻
        # 只有数字的诊断 (EchoDiag): 外放时每 diag_s 秒在日志里记一行; last_diag 是最近一行 (测试、排查时看)
        self.diag = EchoDiag(self.N, c["diag_s"]) if float(c.get("diag_s") or 0) > 0 else None
        self.last_diag: dict | None = None

    # -- 对外 ------------------------------------------------------------------
    async def process_async(self, pcm: np.ndarray) -> np.ndarray:
        t = time.perf_counter()
        self.hub.touch()
        return await asyncio.get_running_loop().run_in_executor(self.pool, self.process, pcm, t)

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    @property
    def out_delay(self) -> int:
        """输出比输入晚多少个采样: 残余压制的重叠相加晚一块, 加上 hold 多攒的."""
        return self.N + self.hold

    def coupled(self, within_s: float | None = None) -> bool:
        """最近量到过麦克风里有参考的回声 (外放, 不是耳机). 记在 hub 上, 新连接也知道."""
        return self.now - self.hub.coupled_at < (self.conf["coupled_s"] if within_s is None else within_s)

    def user_likely(self) -> bool:
        """最近 0.4 秒里有不全是回声的声音, 或者这会儿根本没在压回声 (插话打断看这个)."""
        if not (self.engaged and self.coupled() and self.mdf.adapted and self.conf["dsp"] and self.conf["res"]):
            return True
        if not self.presence_hist or self.now - self.res_at > 0.3:  # 这会儿没在压 (读数是旧的)
            return True
        return max(self.presence_hist) >= 2 * self.conf["gate_presence"]

    def status(self) -> dict:
        return {**self.stats, "lag_ms": None if self.lag is None else round(self.lag * 1000 / RATE, 1),
                "coupled": self.coupled(), "engaged": self.engaged, "adapted": self.mdf.adapted,
                "erle_db": round(float(self.mdf.erle), 1), "pre_ms": round(self.pre * 1000 / RATE, 1),
                "loopback": self.hub.status()}

    def ref_for(self, s0: int, s1: int) -> np.ndarray | None:
        """输出流 (整体晚 out_delay 个采样) 里 [s0, s1) 那一段同时在放的系统声音 (前后各多 0.3 秒, float32 ±1);
        没量到外放回声、或者那时系统没声音就是 None."""
        if not self.coupled(60.0):
            return None
        lag = self._lag()
        m0, m1 = s0 - self.out_delay, s1 - self.out_delay
        p0 = self._ref_index(m0)
        if p0 is None or m1 <= m0:
            return None
        pad = int(0.3 * RATE)
        ref = self.ring.read(p0 - lag - pad, (m1 - m0) + 2 * pad)
        if float(np.sqrt(np.mean(ref.astype(np.float64) ** 2))) < 10 ** (-48 / 20):
            return None
        return ref

    def near_ratio(self, s0: int, s1: int) -> float | None:
        """输出流 [s0, s1) 这一段里, 有多少块"不全是回声" (扣掉回声后剩下的占比够高). 没在压回声的块算不是回声."""
        m0, m1 = s0 - self.out_delay, s1 - self.out_delay
        thr = 2 * self.conf["gate_presence"]
        with self.lock:
            vals = [pr for bm, _, pr in self.blocks if m0 <= bm < m1]
        return sum(1 for v in vals if v >= thr) / len(vals) if vals else None

    def is_echo(self, heard: str, ref_text: str) -> tuple[bool, float]:
        r = echo_ratio(heard, ref_text)
        n = len(norm_text(heard))
        return n > 0 and (r >= self.conf["text_ratio"] if n > 2 else r >= 0.99), r

    # -- 处理 ------------------------------------------------------------------
    def _lag(self) -> int:
        if self.lag is not None:
            return self.lag
        if self.hub.lag_hint is not None:
            return self.hub.lag_hint
        return int(self.conf["lag_guess_ms"] * RATE / 1000)

    def _set_lag(self, new: int) -> None:
        """换延迟 (第一次量到、或者复量变了): 参考读的位置是 p - 延迟 + 往前留的, 位置挪了多少, 滤波器里的冲激响应就跟着挪多少."""
        old = self._lag()
        if new != old:
            self.mdf.shift(new - old)
            self.need_prime = True
        self.lag = new
        self.margins.clear()
        self.pre_hold_until = self.now + self.pre_calm  # 读的位置刚挪过, 余量是乱的

    def _adapt_pre(self) -> None:
        """远端块读完以后环里已写到的位置比这块的尾巴还早 (余量 < pre_safety): 这块的尾巴读出来是 0, 这些块的谱进了滤波器, 后面 M 块的回声估计都带着洞,
        线上滤波器卡在平台. 往前少留一点: 最近几秒里最小的余量不够, 就把往前留的减到刚好够. 只往小里调 (回声里比量到的延迟早的那点能量
        够不着了, 比读到 0 强)."""
        if self.now < self.pre_hold_until or len(self.margins) < 50:
            return
        new = max(self.pre_min, self.pre - (self.pre_safety - min(self.margins)))
        if new < self.pre:
            self.mdf.shift(self.pre - new)  # 读的位置早了 (pre - new) 个采样: 参考晚了这么多, 冲激响应往左挪
            self.pre = new
            self.need_prime = True
            self.margins.clear()
            self.pre_hold_until = self.now + 1.0
            self.stats["pre_cut"] += 1
            log.info("外放回声: 远端块的尾巴比环里已写到的位置还新, 往前留的减到 %.0f ms", new * 1000 / RATE)

    def _ref_index(self, m: int) -> int | None:
        """麦克风序号 m 对应的环里"零延迟"序号."""
        with self.lock:
            for bm, bp, _ in reversed(self.blocks):
                if bm <= m:
                    return bp + (m - bm)
            if self.sync is not None:
                return self.sync[1] + (m - self.sync[0])
        return None

    def _track_clock(self, t: float, n_end: int) -> None:
        c = t - n_end / RATE
        self.clock.append((t, c))
        while self.clock and t - self.clock[0][0] > 10.0:
            self.clock.popleft()
        recent = min(cc for tt, cc in self.clock if t - tt < 1.0)
        floor = min(cc for _, cc in self.clock)
        self.clock_recent = recent  # 诊断看: 比对上时间线那一刻 (sync_c0) 晚了多少 = 麦克风这边丢了多少采样 / 送得多晚
        if self.c0 is None or recent - floor > 0.3:  # 停过麦克风 / 断过: 时间线重来
            self.clock.clear()
            self.clock.append((t, c))
            self.c0 = c
            self.sync = None
        else:
            self.c0 = floor

    def _maybe_resync(self) -> None:
        if self.c0 is None:
            return
        fresh = self.sync is None or self.sync_epoch != self.ring.epoch
        drifted = self.sync_c0 is not None and self.c0 < self.sync_c0 - 0.1  # 上次对齐时卡过, 时刻估晚了
        if not (fresh or drifted):
            return
        n = self.ring.n_at(self.c0 + self.mic_n / RATE)
        if n is None:
            return
        if self.sync is not None and not fresh:
            old = self.sync[1] + (self.mic_n - self.sync[0])
            self.mdf.shift(old - n)  # 零延迟点挪早了 old-n 个采样: 参考跟着早, 冲激响应往右挪
        elif self.sync is not None:
            self.mdf.reset()  # 换了设备: 回声路径也变了
            self.pre = self.pre_max  # 新设备的余量要重新量: 往前留的也回到设定值
        with self.lock:
            self.sync = (self.mic_n, n)
        self.sync_c0 = self.c0
        self.sync_epoch = self.ring.epoch
        self.need_prime = True
        self.stats["resync"] += 1
        self.margins.clear()
        self.pre_hold_until = self.now + self.pre_calm  # 时间线刚重对过, 读的位置要等 GCC 复量才跟上: 这几秒的余量是乱的

    def process(self, pcm: np.ndarray, t: float) -> np.ndarray:
        x = pcm.astype(np.float64)
        k = x.size
        self.stats["frames"] += 1
        self.in_buf = np.concatenate([self.in_buf, x]) if self.in_buf.size else x
        self._track_clock(t, self.mic_n + self.in_buf.size)
        if k >= self.gcc_w:
            self.mic_hist = x[-self.gcc_w:].copy()
            self.mic_hist_zero = np.full(self.gcc_w, not np.any(pcm))
        else:
            self.mic_hist = np.concatenate([self.mic_hist[k:], x])
            self.mic_hist_zero = np.concatenate([self.mic_hist_zero[k:], np.full(k, not np.any(pcm))])
        now = self.now = t
        self.engaged = self.hub.available() and now - self.ring.last_sound < self.conf["engage_s"]
        if self.diag is not None:
            self.diag.frame(t)
        if self.engaged:
            self.stats["engaged"] += 1
            self._maybe_resync()
        N = self.N
        while self.in_buf.size >= N + self.hold:  # hold: 多攒 hold 个更新的采样才处理最早的那一块 (等参考写进环)
            blk, self.in_buf = self.in_buf[:N], self.in_buf[N:]
            self.out_q.append(self._block(blk))
            self.mic_n += N
        if self.engaged and self.sync is not None and now >= self.gcc_next:
            # 还没对上时勤量; 对上了、延迟也稳了就 2 秒量一次 (一次大 FFT 约 10 ms)
            stable = self.coupled() and self.lag is not None and len(self.lag_votes) >= 3
            self.gcc_next = now + (4 * self.conf["gcc_every_s"] if stable else self.conf["gcc_every_s"])
            self._measure_lag()
        if self.diag is not None and self.diag.due(now):
            self.diag.flush(now, self, self._diag_done)
        parts = [self.out_pending]
        have = self.out_pending.size
        while have < k and self.out_q:
            b = self.out_q.popleft()
            parts.append(b)
            have += b.size
        flat = np.concatenate(parts)
        out, self.out_pending = flat[:k], flat[k:]
        if out.size < k:  # 第一帧不是整块时开头补零 (之后整体晚这么多, 是连着的)
            out = np.concatenate([np.zeros(k - out.size), out])
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)

    def _diag_done(self, row: dict) -> None:
        """诊断的一行算好了 (多半在后台线程里): 记进日志. 只有数字."""
        self.last_diag = row
        log.info("外放诊断 %s", json.dumps(row, ensure_ascii=False, separators=(",", ":")))

    def _block(self, d: np.ndarray) -> np.ndarray:
        N = self.N
        p = None
        if self.sync is not None:
            p = self.sync[1] + (self.mic_n - self.sync[0])
            with self.lock:
                self.blocks.append((self.mic_n, p, 1.0))
        if not self.engaged or p is None or not np.any(d):  # 没在放声音 / 还没对上 / 半双工时页面发来的静音帧
            self.need_prime = True
            if self.diag is not None:
                self.diag.skip(zero=not np.any(d))
            return self.sup.process(d, np.zeros(N), 0.0, False)
        lag = self._lag()
        pre = self.pre
        start = p - lag + pre
        if self.need_prime:
            self.mdf.prime(self.ref_read(start - (self.M + 2) * N, (self.M + 2) * N))
            self.need_prime = False
        far_n = self.ring.n  # 读之前环里写到哪了 (读的时候回环要是又写进一包, 只会多不会少: 余量只会量小、不会量大)
        e, y = self.mdf.process(d, self.ref_read(start, N))
        margin = far_n - (start + N)  # 这块读完, 环里已写到的位置比块尾新多少个采样 (负的: 尾巴那段读出来是 0)
        live = self.now - self.ring.last_sound < 0.3  # 回环这会儿在出声 (不出声时环是按时间补的静音, 余量不算)
        if live and self.now >= self.pre_hold_until:  # 读的位置也稳了才记
            self.margins.append(margin)
            if len(self.margins) >= 50 and (self.mic_n // N) % 25 == 0:
                self._adapt_pre()
        if self.diag is not None:  # 只数数 (诊断用, 不碍声音; 出错只丢诊断): 上面那个余量, 滤波器自己的读数
            try:
                if live:
                    self.diag.tail(margin)
                if (self.mic_n // N) % 5 == 0:
                    pw1 = getattr(self.mdf, "power_1", None)
                    mu = None if pw1 is None else float(np.dot(pw1, self.mdf.power + 10.0)) * self.mdf.S2 / pw1.size
                    self.diag.filt(self.mdf.leak, self.mdf.erle, mu)
            except Exception:  # noqa: BLE001
                pass
        # 量到外放回声、线性消除也确实消掉了东西, 才交出处理过的; 否则 (戴耳机、还没对上) 交原样的, 滤波器照样在后台学
        use = self.conf["dsp"] and self.mdf.adapted and self.coupled() and self.mdf.erle >= self.conf["min_erle_db"]
        # 和直达声对齐的那一块参考 (残余压制的非线性部分、诊断用): 滤波器里直达声在第 pre 个抽头
        x_al = self.ref_read(start - pre, N) if self.conf["nl"] or self.diag is not None else None
        le = 10 * np.log10((float(d @ d) + 1.0) / (float(e @ e) + 1.0))  # 这一块消掉了多少
        if not use:
            if self.diag is not None:
                self.diag.block(d, x_al, e, None, le, False, False)
            return self.sup.process(d, np.zeros(N), 0.0, False)
        res = bool(self.conf["res"])
        out = self.sup.process(e, y, self.mdf.leak, res, x_al if self.conf["nl"] else None, le)
        if res:
            self.res_at = self.now
            self.presence_hist.append(self.sup.presence)
            with self.lock:
                self.blocks[-1] = (self.mic_n, p, self.sup.presence)
            if self.sup.gated:
                self.stats["gated"] += 1
        if self.diag is not None:
            self.diag.block(d, x_al, e, out, le, True, res and self.sup.gated)
        return out

    def ref_read(self, n0: int, k: int) -> np.ndarray:
        """环里序号 [n0, n0+k) 的参考, int16 量级的 float64 (还没录到的当静音)."""
        return self.ring.read(n0, k).astype(np.float64) * 32768

    def _measure_lag(self) -> None:
        if self.mic_hist_zero.any():
            return
        mic = self.mic_hist
        if float(np.sqrt(np.mean(mic ** 2))) < 30:  # 麦克风几乎没声音 (-60 dBFS)
            return
        m_end = self.mic_n + self.in_buf.size
        p_end = self._ref_index(m_end - 1)
        if p_end is None:
            return
        p_end += 1
        ref = self.ring.read(p_end - self.gcc_w - self.max_lag, self.gcc_w + self.max_lag).astype(np.float64)
        if float(np.sqrt(np.mean(ref[-self.gcc_w:] ** 2))) < 10 ** (-50 / 20):
            return
        self.stats["gcc_tried"] += 1
        lag, z = gcc_phat(mic, ref, self.max_lag)
        if self.diag is not None:
            self.diag.gcc_z = z
        if z < self.conf["gcc_conf"]:
            return
        self.stats["gcc_ok"] += 1
        # 两次量到的延迟对得上 (差不到 2 ms、隔不远) 才算外放: 没有回声时偶尔也会冒出一个高峰, 但位置是乱的
        agree = any(abs(lag - lv) <= 32 and self.now - tv < self.conf["confirm_s"] for tv, lv in self.gcc_hits)
        self.gcc_hits.append((self.now, lag))
        if not agree:
            return
        self.hub.coupled_at = self.now
        self.lag_votes.append(lag)
        new = int(np.median(self.lag_votes))
        if self.lag is None:
            self._set_lag(new)
        elif (abs(new - self.lag) >= int(self.conf["realign_min"]) and len(self.lag_votes) >= 2
              and max(self.lag_votes) - min(self.lag_votes) <= 32):
            self._set_lag(new)
            self.stats["realign"] += 1
        self.hub.lag_hint = self.lag
