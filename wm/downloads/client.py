"""宿主 (8790) 这边: 找到下载服务, 没开着就叫起来, 把请求转过去. 她的工具和页面都经这里 —— 下载服务只认带令牌的本机请求,
令牌在数据目录里 (守门名单挡着, 她的读写工具拿不到).

叫起来: 留一个 wake 记号, 没有小看守在看就起一个 (python -m wm.downloads watch, 和宿主脱开: 重启 8790 不带走它),
小看守把下载服务开起来; 这里等它应答 /health. 小看守说"起不来" (10 分钟挂了 5 次) 的, 10 分钟内不再起, 把原因交回去.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

from . import IDLE_EXIT_S, PORT, SERVICE, DownloadError, summary, wake
from . import daemon as D
from ..config import ROOT

SPAWN = True          # 测试里关掉: 一般的测试不该顺手起出别的进程 (tests/conftest.py)
START_WAIT_S = 30.0   # 叫起来以后最多等多久
GIVE_UP_S = 600.0     # 小看守说起不来以后, 多久之内不再起 (和 wm/services.py 的 WINDOW_S 一样)
_PASS = ("max_file_gb", "reserve_gb", "unknown_ok_mb", "max_active", "max_waiting", "max_open", "keep_records", "mark_of_web",
         "retry_delays", "read_timeout_s", "checkpoint_s")


class _Unreachable(Exception):
    pass


class Client:
    def __init__(self, home: Path, root: Path, conf: dict | None = None):
        self.home, self.root, self.conf = Path(home), Path(root), dict(conf or {})
        self._info: dict = {}
        self._http: aiohttp.ClientSession | None = None
        self._starting: asyncio.Lock | None = None

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(trust_env=False)  # 本机的, 不走代理
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    # ---- 找到、叫醒 ----
    async def _healthy(self, info: dict) -> bool:
        try:
            async with self._session().get(f"http://127.0.0.1:{info['port']}/health", timeout=aiohttp.ClientTimeout(total=2)) as r:
                got = await r.json()
            return r.status == 200 and got.get("service") == SERVICE and got.get("boot") == info.get("boot")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError):
            return False

    def watch_cmd(self) -> list[str]:
        exe = Path(sys.executable)
        quiet = exe.parent / "pythonw.exe"
        cmd = [str(quiet if quiet.is_file() else exe), "-m", "wm.downloads", "watch", "--detach", "--home", str(self.home),
               "--dir", str(self.root),
               "--port", str(int(self.conf.get("port") if self.conf.get("port") is not None else PORT)),
               "--idle-exit", str(float(self.conf.get("idle_exit_s") or IDLE_EXIT_S))]
        passed = {k: self.conf[k] for k in _PASS if k in self.conf}
        for key, flag in (("watch_tick", "--tick"), ("restart_max", "--restart-max")):  # 只有测试会改
            if key in self.conf:
                cmd += [flag, str(self.conf[key])]
        return cmd + (["--limits", json.dumps(passed)] if passed else [])

    def _spawn_watch(self) -> None:
        from .. import localhttp
        from .. import services as S
        # 环境里有代理变量时补上 no_proxy (wm/localhttp.py): 你的电脑上有 http_proxy 却没有 no_proxy, 认环境变量的客户端
        # 访问 127.0.0.1 会被送去代理 (11-11 因此出过事). 我们自己访问本机都是直连的, 这是给底下别的东西垫的一层
        subprocess.Popen(self.watch_cmd(), cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=S.DETACH | S.NO_WINDOW, close_fds=True,
                         env=localhttp.with_local_bypass(dict(os.environ)))

    async def ensure(self, force: bool = False) -> dict:
        """下载服务在跑就返回它的 {port, token, …}; 没在跑就叫起来再返回. 起不来抛 DownloadError("service-down")."""
        if self._starting is None:
            self._starting = asyncio.Lock()
        async with self._starting:
            info = D.daemon_info(self.home)
            if info and await self._healthy(info):
                self._info = info
                return info
            if not SPAWN:
                raise DownloadError("service-down", "下载服务没开着")
            end, spawned, spawn_wall = time.monotonic() + START_WAIT_S, 0.0, 0.0
            while True:
                wake(self.home)  # 等的时候一直留着记号: 下载服务刚好在这时闲得退了, 看守也会再把它开起来
                w = D.watch_info(self.home)
                if not w["running"] and time.monotonic() - spawned > 5.0:
                    since = float(w["since"] or 0)
                    failed = w["state"] == "failed"
                    # 起不来的: 一次都还没起过 (spawn_wall == 0) 时 10 分钟内不再起 (你点"重新起"的除外); 起过以后只认这次起的
                    # 看守自己又失败了的新记录 (since 在我们起它之后) —— 机器忙时新看守 5 秒内还没写出状态文件, 读到的是旧的
                    # "failed", 不能当成它又失败了. 多起一个看守是安全的 (watch.lock 只放一个)
                    if failed and ((not spawn_wall and not force and time.time() - since < GIVE_UP_S) or (spawn_wall and since >= spawn_wall)):
                        raise DownloadError("service-down", f"下载服务起不来 ({w['error'] or '连着挂了几次'}); 先不再起了, 请主人看日志 "
                                                            f"{w['log'] or self.home / 'logs'}, 修好以后让它\"重新起\"")
                    self._spawn_watch()
                    spawned, spawn_wall = time.monotonic(), time.time()
                await asyncio.sleep(0.2)
                info = D.daemon_info(self.home)
                if info and await self._healthy(info):
                    self._info = info
                    return info
                if time.monotonic() > end:
                    raise DownloadError("service-down", f"下载服务 {START_WAIT_S:.0f} 秒还没起来 (日志在 {self.home / 'logs'})")

    # ---- 转过去 ----
    async def _post(self, info: dict, op: str, timeout: float, body: dict) -> dict:
        try:
            async with self._session().post(f"http://127.0.0.1:{info['port']}/rpc", json={"op": op, **body},
                                            headers={"X-WM-Token": info["token"]},
                                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status in (401, 403):  # 令牌对不上: 下载服务重启过, 重新找
                    raise _Unreachable("令牌对不上")
                data = await r.json()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError) as exc:
            raise _Unreachable(str(exc)[:160] or type(exc).__name__) from None
        except (aiohttp.ClientError, ValueError) as exc:
            raise DownloadError("service-down", f"下载服务回的看不懂: {str(exc)[:160]}") from None
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            raise DownloadError(str(data.get("code") or "internal"), str(data.get("error") or "下载服务没做成"))
        return data

    async def rpc(self, op: str, timeout: float = 15.0, **body) -> dict:
        """调下载服务的一个操作. 它没开着会先叫起来; 刚好在这时退了 / 重启了就再找一次."""
        why = ""
        for _ in (1, 2):
            info = self._info or await self.ensure()
            try:
                return await self._post(info, op, timeout, body)
            except _Unreachable as exc:
                self._info, why = {}, str(exc)
        raise DownloadError("service-down", f"下载服务没应答: {why}")

    async def restart(self) -> dict:
        """你点了"重新起": 让下载服务退出 (在下的会从断点接着下), 再叫起来; 之前"起不来"的也再试一次."""
        info = D.daemon_info(self.home)
        if info:
            with contextlib.suppress(DownloadError, _Unreachable):
                await self._post(info, "shutdown", 5.0, {})
            for _ in range(50):
                if not D.daemon_info(self.home):
                    break
                await asyncio.sleep(0.1)
        self._info = {}
        return await self.ensure(force=True)

    def state(self) -> dict:
        """给页面看的: 服务在不在、小看守怎样、队列小结 (只读文件, 不把服务叫起来)."""
        info = D.daemon_info(self.home)
        out = {"running": bool(info), "port": info.get("port"), "pid": info.get("pid"), "watch": D.watch_info(self.home),
               "summary": summary(self.home), "dir": str(self.root)}
        for key in ("dir_error", "legacy"):  # 服务起来时记下的 (只读文件); 要新的看 status 的返回
            if info.get(key):
                out[key] = info[key]
        return out
