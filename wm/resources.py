"""实时资源监测: 每隔几秒采一次整机 CPU、内存、显卡 (nvidia-smi) 和本项目进程, 保留最近几分钟.

延迟变化常常不是代码问题, 而是后台负载 (之前实测 WSL 和视频网站占了 40-50% CPU, 说完到听到声音从 660 ms 涨到 1100 ms),
所以还会列出这段时间平均占 CPU 最多的外部进程.

分两档:
- 每 interval 秒: 整机 CPU/内存 (几毫秒)、nvidia-smi (约 80 ms)、本项目进程 (缓存的进程句柄);
- 进程普查 (后台线程, 开头隔 60 秒做两次, 之后每 300 秒一次): 记下所有进程的累计 CPU 时间, 和上一次相减得到
  这段时间的平均占用; 同时重新找本项目的进程 (比如重启过的 llama-server).

普查在 Windows 上一次 NtQuerySystemInformation 拿全部进程 (约 800 个, 十几毫秒, 系统调用期间不占 GIL).
以前用 psutil.process_iter: 别的用户 / 受保护的进程 (这台机器约 200 个) 打不开句柄, psutil 每个都退回去拍一次全系统快照
(约 19 ms, 期间握着 GIL), 一次普查 4-7 秒; 这几秒里服务端主线程每次放开 GIL (读文件、系统调用) 都要等上一个快照,
QQ 回复里做 Path.resolve 的那步从 0.03 秒拖到 5 秒多 (2026-11-01, 见知识库 资源监测).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from collections import deque

log = logging.getLogger("wm.resources")

OURS = ("llama-server", "blender")  # 本项目会起的外部进程 (按进程名包含)
GPU_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    class _ProcInfo(ctypes.Structure):  # SYSTEM_PROCESS_INFORMATION 的前半段, 后面的字段用不到
        _fields_ = [("NextEntryOffset", wintypes.ULONG), ("NumberOfThreads", wintypes.ULONG),
                    ("WorkingSetPrivateSize", ctypes.c_longlong), ("HardFaultCount", wintypes.ULONG),
                    ("NumberOfThreadsHighWatermark", wintypes.ULONG), ("CycleTime", ctypes.c_ulonglong),
                    ("CreateTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
                    ("ImageName", _UnicodeString), ("BasePriority", wintypes.LONG), ("UniqueProcessId", ctypes.c_void_p)]

    _query = ctypes.WinDLL("ntdll").NtQuerySystemInformation  # 自己的 WinDLL 实例, 不改 ctypes.windll.ntdll 上别人的设置
    _query.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    _query.restype = ctypes.c_long
    _nt_size = [2 << 20]  # 上次够用的缓冲区大小 (这台机器约 800 个进程时要 1.5 MB)


def _nt_processes() -> dict:
    """Windows: 一次 NtQuerySystemInformation(SystemProcessInformation) 拿到 {pid: (名字, 累计 CPU 秒)}.

    名字是映像名, 和任务管理器一样 (psutil 只有个别系统进程叫法不同: Memory Compression 它叫 MemCompression)."""
    while True:
        buf, need = ctypes.create_string_buffer(_nt_size[0]), wintypes.ULONG(0)
        status = _query(5, buf, _nt_size[0], ctypes.byref(need)) & 0xFFFFFFFF
        if status in (0xC0000004, 0xC0000023) and _nt_size[0] < 1 << 26:  # 缓冲区不够 (两次调用之间又起了进程): 加大再来
            _nt_size[0] = max(_nt_size[0] * 2, need.value + need.value // 4)
            continue
        if status:
            raise OSError(f"NtQuerySystemInformation 返回 0x{status:08X}")
        break
    cur, off = {}, 0
    while True:
        e = _ProcInfo.from_buffer(buf, off)
        pid = e.UniqueProcessId or 0
        if pid in (0, 4):  # psutil 和任务管理器都这么叫
            name = "System Idle Process" if pid == 0 else "System"
        else:
            name = ctypes.wstring_at(e.ImageName.Buffer, e.ImageName.Length // 2) if e.ImageName.Buffer else ""
        cur[pid] = (name, (e.UserTime + e.KernelTime) / 1e7)  # 100 ns 为单位
        if not e.NextEntryOffset:
            return cur
        off += e.NextEntryOffset


class ResourceMonitor:
    def __init__(self, interval: float = 2.0, keep: int = 150, top: int = 3, census_s: float = 300):
        self.interval, self.top, self.census_s = interval, top, census_s
        self.samples: deque = deque(maxlen=keep)
        self.smi = shutil.which("nvidia-smi")
        self.ours: dict = {}         # pid -> psutil.Process
        self.others: list = []       # 上一次普查得出的外部大户
        self.others_span_s: int | None = None  # 上面这个是多长一段时间的平均 (头一次普查只记基线, 还没有)
        self.census_at: float | None = None
        self._last_cpu: dict = {}    # pid -> (名字, 累计 CPU 秒)
        self._nt = os.name == "nt"   # 一次取全部进程失败过就退回 psutil 逐个取
        self.tasks: list[asyncio.Task] = []
        try:
            import psutil
            self.psutil = psutil
            psutil.cpu_percent(None)
            me = psutil.Process(os.getpid())
            me.cpu_percent(None)
            self.ours[me.pid] = me
        except ImportError:  # 没装 psutil 时只报显卡
            self.psutil = None

    def start(self) -> None:
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._loop(self.sample, lambda: self.interval)),
                          asyncio.create_task(self._loop(self.census, self._census_gap, first=5))]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self.tasks = []

    def _census_gap(self) -> float:
        return 60 if self.others_span_s is None else self.census_s  # 第二次 (第一次出外部大户) 隔 60 秒

    async def _loop(self, fn, gap, first: float = 0) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(first)
        while True:
            try:
                out = await loop.run_in_executor(None, fn)
                if out is not None:
                    self.samples.append(out)
            except Exception as exc:  # 采样失败不影响服务
                log.warning("资源采样失败: %s", exc)
            await asyncio.sleep(gap())

    def _gpu(self) -> dict | None:
        if not self.smi:
            return None
        try:
            out = subprocess.run([self.smi, f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            vals = [x.strip() for x in out.stdout.splitlines()[0].split(",")]
            num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None  # noqa: E731
            util, used, total, temp, power = (num(v) for v in vals)
            return {"util": util, "vram_mb": used, "vram_total_mb": total, "temp_c": temp, "power_w": power}
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    def _processes(self) -> dict:
        """{pid: (名字, 累计 CPU 秒)}: Windows 上一次系统调用拿全; 别的系统 (或那个调用失败过) 用 psutil 逐个取."""
        if self._nt:
            try:
                return _nt_processes()
            except (OSError, ValueError) as exc:
                self._nt = False
                log.warning("一次取全部进程失败, 退回 psutil 逐个取 (Windows 上要几秒, 会拖慢主线程): %s", exc)
        cur = {}
        for p in self.psutil.process_iter(["pid", "name", "cpu_times"]):
            times = p.info["cpu_times"]
            if times is not None:
                cur[p.info["pid"]] = (p.info["name"] or "", times.user + times.system)
        return cur

    def census(self) -> None:
        """进程普查 (在后台线程里): 外部进程这段时间的平均 CPU, 重新发现本项目的进程."""
        ps = self.psutil
        if ps is None:
            return None
        now, cur, n = time.time(), self._processes(), ps.cpu_count() or 1
        for pid, (name, _) in cur.items():
            if pid not in self.ours and any(k in name.lower() for k in OURS):
                try:
                    p = ps.Process(pid)
                    p.cpu_percent(None)
                    self.ours[pid] = p
                except ps.Error:
                    pass
        if self.census_at is not None:
            span = max(1.0, now - self.census_at)
            rows = []
            for pid, (name, cpu) in cur.items():
                old = self._last_cpu.get(pid)
                if old is None or old[0] != name or pid in self.ours or pid == 0:
                    continue
                pct = (cpu - old[1]) / span / n * 100
                if pct >= 1 and name.lower() not in ("system idle process", "idle"):
                    rows.append({"pid": pid, "name": name, "cpu": round(pct, 1)})
            self.others = sorted(rows, key=lambda r: -r["cpu"])[: self.top]
            self.others_span_s = round(span)
        self._last_cpu, self.census_at = cur, now
        return None

    def sample(self) -> dict:
        s = {"t": time.time(), "gpu": self._gpu()}
        ps = self.psutil
        if ps is not None:
            vm = ps.virtual_memory()
            s["cpu"] = ps.cpu_percent(None)
            s["ram_mb"], s["ram_total_mb"] = round(vm.used / 2**20), round(vm.total / 2**20)
            n, ours = ps.cpu_count() or 1, []
            for pid, p in list(self.ours.items()):
                try:
                    ours.append({"pid": pid, "name": p.name(), "cpu": round(p.cpu_percent(None) / n, 1),
                                 "rss_mb": round(p.memory_info().rss / 2**20), "self": pid == os.getpid()})
                except ps.Error:  # 进程已经退出
                    del self.ours[pid]
            s["ours"] = ours
            s["others"] = self.others
            s["others_span_s"] = self.others_span_s
        return s

    def snapshot(self, history: int = 60) -> dict:
        items = list(self.samples)
        return {"interval": self.interval, "latest": items[-1] if items else None,
                "history": [{"t": x["t"], "cpu": x.get("cpu"), "gpu": (x.get("gpu") or {}).get("util"),
                             "vram_mb": (x.get("gpu") or {}).get("vram_mb")} for x in items[-history:]]}
