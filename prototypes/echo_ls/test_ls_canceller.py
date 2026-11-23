"""最小二乘线性消除原型 (ls_canceller.py) 的测试: 合成的线性回声, 不录麦克风、不录系统声音、不联网.

在项目根目录跑 (只能用 pytest):
    python -B -m pytest prototypes/echo_ls -q -p no:cacheprovider
"""
import sys
from pathlib import Path

import numpy as np
from scipy.signal import lfilter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (HERE, ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ls_canceller import LSCanceller  # noqa: E402
from wm.echo import MDF  # noqa: E402

FS = 16000


def speechish(n, rng, seed_env=0.0):
    """像说话的信号: 有色噪声 (300 Hz–3 kHz 的共振), 按音节忽大忽小."""
    x = lfilter([1.0], [1.0, -1.6, 0.8], rng.standard_normal(n))
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * np.arange(n) / FS + seed_env) ** 2
    return x * env / np.sqrt(np.mean((x * env) ** 2))


def db(x):
    return 10 * np.log10(np.mean(np.asarray(x, np.float64) ** 2) + 1e-20)


def linear_path(rng, n):
    h = np.zeros(800)
    h[40], h[90], h[300] = 0.6, -0.3, 0.1
    h[40:800] += 0.02 * rng.standard_normal(760) * np.exp(-np.arange(760) / 150)
    return h


def run(f, mic, far):
    return np.concatenate([f.process(mic[i:i + 320], far[i:i + 320])[0] for i in range(0, mic.size - 319, 320)])


def test_ls_cancels_a_linear_echo_under_a_loud_noise_floor():
    """线性回声 + -40 dBFS 的底噪: 最小二乘的线性消除能消到底噪那么低 (回声比底噪高 22 dB 时消掉 15 dB 以上); 8 秒之后就在了."""
    rng = np.random.default_rng(21)
    n = FS * 14
    far = speechish(n, rng) * 6000
    echo = np.convolve(far, linear_path(rng, n))[:n]
    mic = echo + rng.standard_normal(n) * 32768 * 10 ** (-40 / 20)
    ls = LSCanceller(320, 8)
    out = run(ls, mic, far)
    tail = slice(FS * 8, out.size)
    assert db(mic[tail]) - db(out[tail]) > 15
    assert ls.adapted and ls.erle > 8


def test_ls_does_not_drift_while_you_talk():
    """你在说话 (和参考不相关、音量和回声差不多) 的那几秒滤波器不被带歪: 回声照样消、不比原样更糟; 你说完了很快又回到 15 dB 以上."""
    rng = np.random.default_rng(23)
    n = FS * 16
    far = speechish(n, rng) * 6000
    echo = np.convolve(far, linear_path(rng, n))[:n]
    near = np.zeros(n)
    s0, s1 = FS * 8, FS * 11
    near[s0:s1] = speechish(s1 - s0, rng, 2.0) * 3500
    mic = echo + near + rng.standard_normal(n) * 32768 * 10 ** (-45 / 20)
    out = run(LSCanceller(320, 8), mic, far)
    seg = slice(s0 + FS // 2, s1)
    assert db(out[seg] - near[seg]) < db(mic[seg] - near[seg]) - 10  # 你说话时回声照样少 10 dB 以上
    assert db(out[seg]) < db(mic[seg]) + 1                           # 比原样没有更糟 (没发散)
    after = slice(s1 + FS * 3, n - 320)
    assert db(mic[after]) - db(out[after]) > 15                      # 你说完三秒以后又回来了


def test_ls_keeps_the_learned_path_when_the_alignment_moves():
    """对齐的位置挪了 (复量延迟变了): shift 把学到的相关跟着挪, 不用从头学; 方向和 MDF.shift 一样."""
    rng = np.random.default_rng(24)
    n = FS * 12
    far = speechish(n, rng) * 6000
    echo = np.convolve(far, np.concatenate([np.zeros(400), linear_path(rng, n)]))[:n]  # 响应从第 440 个抽头起: 挪 160 个也不会跑到负的抽头去
    mic = echo + rng.standard_normal(n) * 32768 * 10 ** (-50 / 20)
    for mk in (lambda: LSCanceller(320, 8), lambda: MDF(320, 8)):
        f = mk()
        outs = []
        d = 160  # 第 8 秒起参考改成晚 160 个采样读 (等于 "对齐时减的延迟多了 160")
        for i in range(0, n - 319, 320):
            if i == FS * 8:
                f.shift(d)
            far_blk = far[i:i + 320] if i < FS * 8 else far[max(0, i - d):max(0, i - d) + 320]
            if far_blk.size < 320:
                far_blk = np.concatenate([far_blk, np.zeros(320 - far_blk.size)])
            outs.append(f.process(mic[i:i + 320], far_blk)[0])
        out = np.concatenate(outs)
        seg = slice(FS * 8 + FS, FS * 10)
        assert db(mic[seg]) - db(out[seg]) > 15, type(f).__name__
