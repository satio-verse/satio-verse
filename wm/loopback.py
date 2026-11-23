"""电脑正在放的声音 (系统声音回环): 给麦克风那边当回声参考 (wm/echo.py), 分清视频的声音和你说的话.

Windows 自带的 WASAPI 回环 (默认播放设备, 共享模式), ctypes 直接调, 不用新装库. 只在内存里: 一个 30 秒的环形缓冲,
16 kHz 单声道 float32, 不落盘、不上传. 只在有麦克风帧进来时录 (touch), 15 秒没人要就停掉、放开设备.

时间线: 环里每个采样有一个绝对序号 n. 播放设备闲着时 WASAPI 一个包都不给, 下一个包到时按它的 QPC 时间戳补上中间的静音,
所以序号和时间一直对得上 (同一个设备时钟); 换了默认设备 (插耳机) 重开, 也按时间戳接着排.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, Structure, byref, c_int, c_longlong, c_uint16, c_uint32, c_uint64, c_void_p

import numpy as np

log = logging.getLogger("wm.loopback")

RATE = 16000
ALLOW_CAPTURE = True    # 测试里关掉 (tests/conftest.py): 测试不真的去录电脑的声音, 回声那边只走"原样放过去"
RING_S = 30.0
IDLE_STOP_S = 15.0      # 这么久没有麦克风帧 (没人要参考) 就停
DEVICE_CHECK_S = 2.0    # 隔这么久看一次默认播放设备换没换
GAP_FILL_MAX_S = 3600.0  # 超过这么长的空档只是把整个环清零, 序号照样按时间往前跳


class _GUID(Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", c_uint16), ("nChannels", c_uint16), ("nSamplesPerSec", c_uint32),
                ("nAvgBytesPerSec", c_uint32), ("nBlockAlign", c_uint16), ("wBitsPerSample", c_uint16),
                ("cbSize", c_uint16)]


class _WAVEFORMATEXTENSIBLE(Structure):
    _pack_ = 1
    _fields_ = [("Format", _WAVEFORMATEX), ("wValidBitsPerSample", c_uint16), ("dwChannelMask", c_uint32),
                ("SubFormat", _GUID)]


_HRESULT = ctypes.HRESULT
_CLSCTX_ALL = 0x17
_LOOPBACK = 0x00020000
_SILENT = 0x2
_E_DEVICE_INVALIDATED = -2004287484  # 0x88890004


class _Com:
    """自己的一份 ole32 (WinDLL 新实例), 不碰 ctypes.windll 上别的模块设的参数类型."""

    def __init__(self):
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoInitializeEx.argtypes = [c_void_p, c_uint32]
        self.ole32.CoTaskMemFree.argtypes = [c_void_p]
        self.ole32.CoCreateInstance.argtypes = [POINTER(_GUID), c_void_p, c_uint32, POINTER(_GUID), POINTER(c_void_p)]
        self.ole32.CoCreateInstance.restype = _HRESULT
        self.ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, POINTER(_GUID)]
        self.CLSID_MMDeviceEnumerator = self.guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        self.IID_IMMDeviceEnumerator = self.guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        self.IID_IAudioClient = self.guid("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
        self.IID_IAudioCaptureClient = self.guid("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")

    def guid(self, s: str) -> _GUID:
        g = _GUID()
        self.ole32.CLSIDFromString(s, byref(g))
        return g

    @staticmethod
    def method(obj: c_void_p, index: int, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        fn = WINFUNCTYPE(_HRESULT, c_void_p, *argtypes)(vtbl[index])
        return lambda *a: fn(obj, *a)

    @staticmethod
    def release(obj: c_void_p | None) -> None:
        if obj:
            vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
            WINFUNCTYPE(c_uint32, c_void_p)(vtbl[2])(obj)


class BoxDecimator:
    """和页面的 mic-worklet.js 同一种降采样 (盒式平均), 参考和麦克风过的是同一种滤波. 可以分段喂, 状态接着."""

    def __init__(self, src_rate: int, dst_rate: int = RATE):
        self.ratio = src_rate / dst_rate
        self.rest = np.zeros(0, np.float64)
        self.t = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        data = np.concatenate([self.rest, x.astype(np.float64)]) if self.rest.size else x.astype(np.float64)
        n = int(np.floor((data.size - self.t) / self.ratio + 1e-9)) if data.size >= self.t + self.ratio else 0
        if n <= 0:
            self.rest = data
            return np.zeros(0, np.float32)
        starts = self.t + self.ratio * np.arange(n)
        a = np.floor(starts).astype(np.int64)
        b = np.maximum(a + 1, np.floor(starts + self.ratio).astype(np.int64))
        cs = np.concatenate([[0.0], np.cumsum(data)])
        out = (cs[b] - cs[a]) / (b - a)
        t_end = self.t + self.ratio * n
        used = int(np.floor(t_end))
        self.rest = data[used:]
        self.t = t_end - used
        return out.astype(np.float32)


class RefRing:
    """16 kHz 参考声音的环形缓冲. n 是绝对序号; t_of / n_at 在序号和 perf_counter 秒之间换算 (最后一个锚点)."""

    def __init__(self, seconds: float = RING_S, rate: int = RATE):
        self.rate = rate
        self.cap = int(seconds * rate)
        self.buf = np.zeros(self.cap, np.float32)
        self.n = 0                 # 下一个要写的序号
        self.anchor: tuple[int, float] | None = None  # (序号, 那个采样的 perf_counter 秒)
        self.lock = threading.Lock()
        self.last_sound = -1e9     # 最后一个不全是零的包放完的时刻 (perf_counter 秒, 按包的时间戳算)
        self.epoch = 0             # 换设备、重开以后 +1 (时间线照样接着, 只是告诉用的人)
        # 给回声处理的诊断看 (wm/echo.py 的 EchoDiag, 只有数字):
        self.fills = 0             # 包的时间戳比按采样数排的位置晚了 >20 ms、补了静音的次数 (停了再放算一次; 正连着放时不该有)
        self.fill_n = 0            # 一共补了多少个采样
        self.skew_max = 0.0        # 连着放的包: 时间戳和按采样数排的位置最多差了多少秒 (诊断读完清零)

    def t_of(self, n: int) -> float | None:
        a = self.anchor
        return None if a is None else a[1] + (n - a[0]) / self.rate

    def n_at(self, t: float) -> int | None:
        a = self.anchor
        return None if a is None else a[0] + int(round((t - a[1]) * self.rate))

    def _write(self, x: np.ndarray) -> None:
        k = x.size
        if k >= self.cap:
            self.buf[:] = x[-self.cap:]
        else:
            i = self.n % self.cap
            j = min(k, self.cap - i)
            self.buf[i:i + j] = x[:j]
            if j < k:
                self.buf[:k - j] = x[j:]
        self.n += k

    def _zeros(self, k: int) -> None:
        if k <= 0:
            return
        if k >= self.cap:
            self.buf[:] = 0
            self.n += k
            return
        self._write(np.zeros(k, np.float32))

    def push(self, x: np.ndarray, t_first: float, silent: bool = False) -> None:
        """一包 16 kHz 采样, t_first 是第一个采样的时刻. 比预计的晚了 >20 ms (设备闲过) 就先补静音;
        连着放的包只把锚点挪到这一包自己的时间戳 (设备时钟和 QPC 有一点漂, 不为它插零, 采样一直是连着的)."""
        with self.lock:
            if self.anchor is not None:
                gap = t_first - self.t_of(self.n)
                if gap > 0.02:
                    k = int(round(min(gap, GAP_FILL_MAX_S) * self.rate))
                    self._zeros(k)
                    self.fills += 1
                    self.fill_n += k
                else:
                    self.skew_max = max(self.skew_max, abs(gap))
            self.anchor = (self.n, t_first)
            if x.size:
                self._write(x)
                if not silent and float(np.max(np.abs(x))) > 1e-5:
                    self.last_sound = t_first + x.size / self.rate

    def advance_to(self, t: float) -> None:
        """设备闲着 (一个包都不来) 时把时间线补到 t: 中间都是静音."""
        with self.lock:
            if self.anchor is None:
                return
            ahead = self.n_at(t) - self.n
            if ahead > 0:
                self._zeros(ahead)
                self.anchor = (self.n, t)

    def read(self, n0: int, k: int) -> np.ndarray:
        """序号 [n0, n0+k) 的采样; 还没写到的、已经被覆盖的都当静音."""
        out = np.zeros(k, np.float32)
        with self.lock:
            lo = max(n0, self.n - self.cap)
            hi = min(n0 + k, self.n)
            if hi > lo:
                idx = np.arange(lo, hi) % self.cap
                out[lo - n0:hi - n0] = self.buf[idx]
        return out


class LoopbackHub:
    """一个进程一份. 有麦克风帧时 touch() 一下, 没在录就开一个线程录; 15 秒没人 touch 就停.
    不是 Windows、或者打不开回环时 available() 是 False, 回声那边就照旧不处理."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.platform == "win32"
        self.ring = RefRing()
        self.thread: threading.Thread | None = None
        self.last_touch = 0.0
        self.stop_flag = threading.Event()
        self.error: str | None = None
        self.fmt: str | None = None
        self.fail_until = 0.0
        self.lag_hint: int | None = None   # 上一条连接量出来的回声延迟 (采样数), 新连接先用它
        self.coupled_at = -1e9             # 最后一次量到麦克风里有系统声音的回声 (外放) 的时刻
        self.lock = threading.Lock()

    def available(self) -> bool:
        return self.enabled and self.error is None

    def touch(self) -> None:
        if not self.enabled or not ALLOW_CAPTURE:
            return
        now = time.perf_counter()
        self.last_touch = now
        if self.thread is None or not self.thread.is_alive():
            if now < self.fail_until:
                return
            with self.lock:
                if self.thread is None or not self.thread.is_alive():
                    self.stop_flag.clear()
                    self.thread = threading.Thread(target=self._run, name="loopback", daemon=True)
                    self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        t = self.thread
        if t is not None and t.is_alive():
            t.join(timeout=2)

    def status(self) -> dict:
        running = self.thread is not None and self.thread.is_alive()
        return {"enabled": self.enabled, "running": running, "format": self.fmt, "error": self.error,
                "sound_ago_s": round(time.perf_counter() - self.ring.last_sound, 1) if self.ring.last_sound > 0 else None}

    # -- 录音线程 ---------------------------------------------------------------
    def _run(self) -> None:
        try:
            com = _Com()
        except OSError as exc:
            self.error = f"ole32: {exc}"
            return
        com.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        try:
            while not self.stop_flag.is_set() and time.perf_counter() - self.last_touch < IDLE_STOP_S:
                try:
                    self._capture(com)
                except OSError as exc:
                    code = getattr(exc, "winerror", None)
                    if code == _E_DEVICE_INVALIDATED:  # 换了设备 / 拔了: 马上重开
                        log.info("播放设备变了, 回环重开")
                        time.sleep(0.2)
                        continue
                    self.error = f"{type(exc).__name__}: {exc}"
                    log.warning("系统声音回环打不开: %s (回声参考先关, 60 秒后再试)", exc)
                    self.fail_until = time.perf_counter() + 60
                    return
        except Exception as exc:  # 别的错 (不该有): 记一次, 60 秒内不再起线程, 免得每帧都重开
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("系统声音回环出错, 60 秒后再试")
            self.fail_until = time.perf_counter() + 60
        finally:
            com.ole32.CoUninitialize()

    def _capture(self, com: _Com) -> None:
        m = com.method
        enum, dev, client, cap, pwfx = c_void_p(), c_void_p(), c_void_p(), c_void_p(), POINTER(_WAVEFORMATEX)()
        try:
            com.ole32.CoCreateInstance(byref(com.CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL,
                                       byref(com.IID_IMMDeviceEnumerator), byref(enum))
            m(enum, 4, c_int, c_int, POINTER(c_void_p))(0, 0, byref(dev))  # 默认播放设备 (eRender, eConsole)
            dev_id = self._device_id(com, dev)
            m(dev, 3, POINTER(_GUID), c_uint32, c_void_p, POINTER(c_void_p))(byref(com.IID_IAudioClient), _CLSCTX_ALL, None, byref(client))
            m(client, 8, POINTER(POINTER(_WAVEFORMATEX)))(byref(pwfx))
            w = pwfx.contents
            ch, rate, bits = w.nChannels, w.nSamplesPerSec, w.wBitsPerSample
            is_float = w.wFormatTag == 3
            if w.wFormatTag == 0xFFFE:
                ext = ctypes.cast(pwfx, POINTER(_WAVEFORMATEXTENSIBLE)).contents
                is_float = ext.SubFormat.Data1 == 3
            self.fmt = f"{rate} Hz {ch} 声道 {bits} 位{'浮点' if is_float else '整数'}"
            m(client, 3, c_int, c_uint32, c_longlong, c_longlong, POINTER(_WAVEFORMATEX), c_void_p)(
                0, _LOOPBACK, 2_000_000, 0, pwfx, None)  # 共享模式回环, 200 ms 缓冲
            m(client, 14, POINTER(_GUID), POINTER(c_void_p))(byref(com.IID_IAudioCaptureClient), byref(cap))
            get_next = m(cap, 5, POINTER(c_uint32))
            get_buf = m(cap, 3, POINTER(POINTER(ctypes.c_ubyte)), POINTER(c_uint32), POINTER(c_uint32),
                        POINTER(c_uint64), POINTER(c_uint64))
            rel_buf = m(cap, 4, c_uint32)
            dec = BoxDecimator(rate)
            m(client, 10)()  # Start
            self.ring.epoch += 1
            self.error = None
            next_check = time.perf_counter() + DEVICE_CHECK_S
            frame_bytes = w.nBlockAlign
            last_packet = time.perf_counter()
            while not self.stop_flag.is_set() and time.perf_counter() - self.last_touch < IDLE_STOP_S:
                n = c_uint32()
                get_next(byref(n))
                while n.value:
                    data = POINTER(ctypes.c_ubyte)()
                    nf, flags, devpos, qpc = c_uint32(), c_uint32(), c_uint64(), c_uint64()
                    get_buf(byref(data), byref(nf), byref(flags), byref(devpos), byref(qpc))
                    k = nf.value
                    silent = bool(flags.value & _SILENT)
                    if silent or not data:
                        mono = np.zeros(k, np.float32)
                    else:
                        raw = ctypes.string_at(data, k * frame_bytes)
                        mono = _to_mono(raw, ch, bits, is_float)
                    rel_buf(nf)
                    t_first = qpc.value / 1e7  # QPC 换成 100 ns 单位的; perf_counter 也是 QPC
                    lead = (dec.rest.size - dec.t) / rate  # 上一包剩下没凑够一个输出采样的那几个源采样
                    self.ring.push(dec(mono), t_first - lead, silent=silent)
                    last_packet = time.perf_counter()
                    get_next(byref(n))
                now = time.perf_counter()
                if now - last_packet > 0.1:  # 设备闲着 (没在放声音): 时间线补静音补到 100 ms 以前
                    self.ring.advance_to(now - 0.1)
                if now >= next_check:
                    next_check = now + DEVICE_CHECK_S
                    cur = c_void_p()
                    try:
                        m(enum, 4, c_int, c_int, POINTER(c_void_p))(0, 0, byref(cur))
                        changed = self._device_id(com, cur) != dev_id
                    finally:
                        com.release(cur)
                    if changed:
                        log.info("默认播放设备换了, 回环跟过去")
                        return
                time.sleep(0.005)
        finally:
            if client:
                try:
                    m(client, 11)()  # Stop
                except OSError:
                    pass
            com.release(cap)
            if pwfx:
                com.ole32.CoTaskMemFree(pwfx)
            com.release(client)
            com.release(dev)
            com.release(enum)

    @staticmethod
    def _device_id(com: _Com, dev: c_void_p) -> str:
        p = ctypes.c_wchar_p()
        com.method(dev, 5, POINTER(ctypes.c_wchar_p))(byref(p))
        try:
            return p.value or ""
        finally:
            com.ole32.CoTaskMemFree(ctypes.cast(p, c_void_p))


def _to_mono(raw: bytes, ch: int, bits: int, is_float: bool) -> np.ndarray:
    if is_float and bits == 32:
        x = np.frombuffer(raw, np.float32)
    elif bits == 16:
        x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    elif bits == 32:
        x = np.frombuffer(raw, np.int32).astype(np.float32) / 2147483648.0
    elif bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = (np.where(v >= 1 << 23, v - (1 << 24), v)).astype(np.float32) / 8388608.0
    else:
        return np.zeros(len(raw) // max(1, ch * bits // 8), np.float32)
    if ch > 1:
        x = x[: x.size - x.size % ch].reshape(-1, ch).mean(axis=1)
    return x.astype(np.float32)
