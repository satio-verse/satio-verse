"""最小二乘 (维纳) 线性回声消除 (2026-11-11 的原型, 没进 wm/): 和 wm.echo.MDF 同一套接口 (process / prime / shift / reset; erle / leak / adapted).

评测里没有哪个环境比 MDF 明显更好 (见 README.md), 所以没并进 wm/echo.py. 这里留着做法和测试, 以后要再试 (比如换了一台路径很不稳的机器) 可以直接拿去用:
    from ls_canceller import LSCanceller
    guard.mdf = LSCanceller(guard.N, guard.M, conf=...)    # 换进 EchoGuard 里的 mdf 位置即可 (接口一样)
"""
from __future__ import annotations

import numpy as np
from scipy import fft as sfft
from scipy.linalg import solve_toeplitz

RATE = 16000
DEFAULTS = {
    "ls_tau_s": 3.0,          # 相关统计的遗忘时间常数 (评测里 6 秒更好一点)
    "ls_update_s": 1.0,       # 隔多久解一次 (前 8 次每 0.25 s, 挪过以后 4 秒内每 0.5 s)
    "ls_reg": 1e-3,           # 解法方程时对角线上加的 (占自相关零点的比例; 数据少时另外多加一点)
    "ls_gain_track": True,    # 增益用标量跟一下 (浏览器的自动增益几秒一摆)
    "ls_leak_floor": 0.3,     # 残余压制看的"漏回声"系数的下限 (解出来的残差和回声估计不相关, 按相关估出来的总是很小, 残余压制就不压了)
}


class LSCanceller:
    """线性回声消除的另一种做法: 最小二乘 (维纳). 和 MDF 同一套接口 (process / prime / shift / reset; erle / leak / adapted), 抽头一样多、对齐一样.

    MDF 一块一块跟着误差走: 底噪大、回声路径晃、你说话的时候, 步长控制一旦走偏就可能卡在半路. 这里不走反馈, 而是把"参考和麦克风的相关"累起来,
    隔一会儿解一次法方程 (2026-11-11 的离线评测里: 稳态比 MDF 低 1–3 dB, 增益摆动时持平, 路径在变时更差, 开销多 12 ms/s 上下, 所以只是个选项):
    - 每 4 块 (80 ms) 一帧, 用 FFT 算这一帧里参考 x 的自相关 r(k) 和麦克风 d 与 x 的互相关 p(k) (k = 0 .. L-1; 不加窗, 没有边缘的偏差);
      参考有声音的帧按 ls_tau_s 的时间常数指数遗忘地累进 R (托普利兹) 和 P; 参考没声音时不累、也不遗忘.
    - 每隔 ls_update_s 解 R h = P (Levinson). 对角线上加一点正则 (数据少时多加, 免得几秒的数据拟合出一个乱的滤波器); h 的范数不许比
      "麦克风 / 参考" 的总比例大太多. 换滤波器时在一块里淡入.
    - 作用: 和 MDF 一样的分块频域卷积 (M 块 × N 个采样), 再乘一个跟得上自动增益的标量增益.
    和参考不相关的 (你的话、底噪) 在互相关里平均掉了, 只剩 L / 样本数 那么一点随机误差, 不会被你的话带歪, 也没有"卡在半路"的状态.
    延迟变了 (shift): 互相关整体挪, 统计留四成, 接下来 4 秒勤解. 开销: 整个 EchoGuard 每秒音频 40–44 ms (MDF 的是 28–31), 大头是解法方程
    (240 ms 的滤波器一次约 16 ms, 每秒一次)."""

    def __init__(self, N: int = 320, M: int = 12, fs: int = RATE, conf: dict | None = None):
        c = {**DEFAULTS, **(conf or {})}
        self.N, self.M, self.fs = N, M, fs
        self.L = N * M
        self.B = 4                                   # 几块一帧算一次相关
        self.F = N * self.B
        self.nfft = sfft.next_fast_len(self.F + self.L - 1, real=True)
        self.lam = float(np.exp(-self.F / fs / float(c["ls_tau_s"])))
        self.slow_blocks = max(1, int(round(float(c["ls_update_s"]) * fs / N)))
        self.mid_blocks = max(1, self.slow_blocks // 2)
        self.fast_blocks = max(1, self.slow_blocks // 4)
        self.reg = float(c["ls_reg"])
        self.leak_floor = float(c["ls_leak_floor"])
        self.gain_track = bool(c["ls_gain_track"])
        self.min_active = 0.6 * fs                   # 这么多"参考有声音的采样"攒够才第一次解
        self.win = np.sin(0.5 * np.pi * (np.arange(N) + 0.5) / N) ** 2   # 换滤波器时新的淡入
        self.reset()

    def reset(self) -> None:
        N, M, L = self.N, self.M, self.L
        K = N + 1
        self.X = np.zeros((M + 1, K), complex)       # 参考最近几块的谱 ([上一块, 这一块] 的 2N 点, 和 MDF 一样)
        self.W = np.zeros((M, K), complex)           # 现在用的滤波器
        self.W_old = None
        self.x_prev = np.zeros(N)
        self.xh = np.zeros(self.F + L - 1)           # 参考的历史 (和麦克风同一条时间线, 算相关用)
        self.dh = np.zeros(self.F)
        self.R = np.zeros(L)
        self.P = np.zeros(L)
        self.Sd = 0.0                                # 麦克风 / 参考的能量累计 (限 h 的范数用)
        self.Sx = 0.0
        self.acc_active = 0.0                        # 累进去的"参考有声音的采样数" (遗忘之后的)
        self.blocks_in_frame = 0
        self.count = 0
        self.solves = 0
        self.force = False
        self.fast_until = 0
        self.leak = 1.0
        self.adapted = False
        self.erle = 0.0                              # 平滑的 10*log10(Sdd/See), 和 MDF 一样在加重过的信号上算 (min_erle_db 两种做法通用)
        self.memd = self.meme = 0.0
        self.slope = 0.0
        self.gain = 1.0
        self.gdy = self.gyy = 1e-9

    def prime(self, far_hist: np.ndarray) -> None:
        """用参考的历史 ((M+2)*N 个采样, 最后 N 个是"上一块") 重建 X 和相关用的历史: 换了对齐、停过一阵以后接着用."""
        N, M = self.N, self.M
        need = (M + 2) * N
        h = np.zeros(need)
        k = min(need, far_hist.size)
        h[need - k:] = far_hist[far_hist.size - k:]
        blocks = h.reshape(M + 2, N)
        for j in range(M + 1):
            b = M + 1 - j
            self.X[j] = sfft.rfft(np.concatenate([blocks[b - 1], blocks[b]]))
        self.x_prev = blocks[-1].copy()
        t = np.zeros(self.xh.size)
        k = min(t.size, far_hist.size)
        t[t.size - k:] = far_hist[far_hist.size - k:]
        self.xh = t

    def shift(self, delta: int) -> None:
        """参考改成早 delta 个采样 (对齐时减的延迟多了 delta): 互相关 P'(k) = P(k + delta); 统计只留四成, 马上重解, 接下来 4 秒勤解."""
        if delta == 0:
            return
        L = self.L
        if abs(delta) >= L:
            self.reset()
            return
        P = np.zeros(L)
        if delta > 0:
            P[:L - delta] = self.P[delta:]
        else:
            P[-delta:] = self.P[:L + delta]
        keep = 0.4
        self.P, self.R = P * keep, self.R * keep
        self.Sd, self.Sx, self.acc_active = self.Sd * keep, self.Sx * keep, self.acc_active * keep
        self.fast_until = self.count + int(4 * self.fs / self.N)
        self.force = True

    def _solve(self) -> np.ndarray:
        r = self.R.copy()
        eps = self.reg + 0.1 * self.L / max(self.acc_active, 1.0)   # 数据少时多加一点
        r[0] = r[0] * (1.0 + eps) + 1.0
        try:
            h = solve_toeplitz(r, self.P)
        except Exception:  # noqa: BLE001 - 病态: 当没解出来
            return np.zeros(self.L)
        if not np.all(np.isfinite(h)):
            return np.zeros(self.L)
        if self.Sx > 0 and self.Sd > 0:  # 不许比"麦克风 / 参考"的总比例大太多 (病态时的保险)
            cap = 2.0 * float(np.sqrt(self.Sd / self.Sx))
            nh = float(np.linalg.norm(h))
            if nh > cap:
                h *= cap / nh
        return h

    def _set_filter(self, h: np.ndarray) -> None:
        N, M = self.N, self.M
        self.W_old = self.W.copy()
        self.W = sfft.rfft(np.concatenate([h.reshape(M, N), np.zeros((M, N))], axis=1), axis=1)

    def _frame_update(self) -> None:
        F, L, nfft = self.F, self.L, self.nfft
        xe, d = self.xh, self.dh
        xf = xe[L - 1:]                              # 这一帧的参考 (xe 前面 L-1 个是再早的历史)
        ex = float(xf @ xf)
        if ex / F <= 300.0:                          # 参考几乎没声音 (-65 dBFS 上下以下): 不累, 也不遗忘
            return
        Xe, Xf, D = sfft.rfft(xe, nfft), sfft.rfft(xf, nfft), sfft.rfft(d, nfft)
        p = sfft.irfft(np.conj(D) * Xe, nfft)[:L][::-1]    # p(k) = Σ d[n] x[n-k]
        r = sfft.irfft(np.conj(Xf) * Xe, nfft)[:L][::-1]   # r(k) = Σ x[n] x[n-k]
        self.R = self.lam * self.R + r
        self.P = self.lam * self.P + p
        self.Sd = self.lam * self.Sd + float(d @ d)
        self.Sx = self.lam * self.Sx + ex
        self.acc_active = self.lam * self.acc_active + F

    def process(self, mic: np.ndarray, far: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """mic, far: 各 N 个采样 (int16 量级的 float). 返回 (去掉回声的麦克风, 回声估计)."""
        N, M = self.N, self.M
        n2 = 2 * N
        self.xh = np.concatenate([self.xh[N:], far])
        self.dh = np.concatenate([self.dh[N:], mic])
        self.X[1:] = self.X[:-1].copy()
        self.X[0] = sfft.rfft(np.concatenate([self.x_prev, far]))
        self.x_prev = far.copy()
        y = sfft.irfft(np.sum(self.W * self.X[:M], axis=0), n2)[N:]
        if self.W_old is not None:                   # 刚换了滤波器: 这一块里从旧的淡入到新的
            y_old = sfft.irfft(np.sum(self.W_old * self.X[:M], axis=0), n2)[N:]
            y = self.win * y + (1 - self.win) * y_old
            self.W_old = None
        Syy0 = float(y @ y)
        if self.gain_track and Syy0 > n2 * 100.0:    # 回声的增益在几秒的尺度上摆 (浏览器的自动增益): 标量跟一下
            self.gdy = 0.97 * self.gdy + float(mic @ y)
            self.gyy = 0.97 * self.gyy + Syy0
            self.gain = float(min(2.0, max(0.5, self.gdy / self.gyy)))
            y = y * self.gain
        e = mic - y
        Sdd, See, Syy = float(mic @ mic), float(e @ e), float(y @ y)
        if Syy > n2 * 100.0:                         # 漏回声: 残差里和回声估计同向的那一部分 (幅度回归斜率的平方)
            self.slope = 0.9 * self.slope + 0.1 * float(e @ y) / Syy
            self.leak = float(min(1.0, max(self.leak_floor, 4.0 * self.slope ** 2)))
        self.blocks_in_frame += 1
        if self.blocks_in_frame >= self.B:
            self.blocks_in_frame = 0
            self._frame_update()
        self.count += 1
        every = (self.fast_blocks if self.solves < 8 else
                 self.mid_blocks if self.count < self.fast_until else self.slow_blocks)
        if (self.count % every == 0 or self.force) and self.acc_active >= self.min_active and self.R[0] > 0:
            self.force = False
            self._set_filter(self._solve())
            self.solves += 1
            if not self.adapted and self.acc_active >= self.fs * 1.0:
                self.adapted = True
        d_pe = mic - 0.9 * np.concatenate([[self.memd], mic[:-1]])
        e_pe = e - 0.9 * np.concatenate([[self.meme], e[:-1]])
        self.memd, self.meme = float(mic[-1]), float(e[-1])
        Sdd, See = float(d_pe @ d_pe), float(e_pe @ e_pe)
        if Sdd > n2 * 100.0:
            self.erle = 0.95 * self.erle + 0.05 * 10 * np.log10(Sdd / max(See, 1e-9))
        return e, y
