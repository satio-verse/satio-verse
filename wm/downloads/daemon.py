"""下载服务的进程和它的小看守.

  python -m wm.downloads serve [--home 数据目录] [--dir 下载目录] [--port N] [--idle-exit 秒] [--limits JSON]
      下载服务: 只绑 127.0.0.1, 每次启动换一枚令牌 (写在 <数据目录>/daemon.json, 宿主照它找); 没活、没人叫 idle-exit 秒就自己退出.
      同一个数据目录只跑一个 (daemon.lock); 已经有一个在跑就直接退出.
  python -m wm.downloads watch [同上]
      小看守: 用 wm/services.py 的 Supervisor 看着 serve (挂了拉起来, 10 分钟挂 5 次就不再拉, 状态写在 <数据目录>/watch/state.json).
      下载服务没活自己退了, 看守也跟着退. 宿主要用下载服务时, 发现没人看着就起一个 (client.py).
  python -m wm.downloads status [--home 数据目录]
      现在怎样 (不联网, 只读文件): 服务在不在、几个在下、几个等点头.

本机接口 (只有宿主用; 都要带令牌 X-WM-Token, /health 除外):
  GET  /health                  -> {"ok": true, "service": "wm-downloads", "boot": …}
  POST /rpc {"op": …, …}        -> {"ok": true, …} 或 {"ok": false, "code": …, "error": …}
    enqueue / status / wait / pause / resume / cancel / cleanup   她的七个工具 (宿主带上是哪个会话)
    answer / shown / changes / ack / info / shutdown              宿主自己的: 你的答复、卡片放没放、盯变化、回报交没交
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import web

from . import (IDLE_EXIT_S, PORT, SERVICE, DownloadError, Limits, dir_for, legacy_text, read_json, real_home, summary, wanted,
               write_json)
from .queue import USER, DownloadQueue

log = logging.getLogger("wm.downloads")
TEST_ALLOW_ENV = "WM_DL_TEST_ALLOW"  # 测试放行的本机"主机:端口" (逗号隔开); 真的数据目录上不认


def _alive(pid) -> bool:
    try:
        import psutil
        p = psutil.Process(int(pid))
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001 (进程没了、看不到)
        return False


def _ours(pid, word: str) -> bool:
    """这个进程号还是我们的 (进程号会被别的程序重用: 再看一眼命令行里有没有 wm.downloads 和 word)."""
    if not _alive(pid):
        return False
    if int(pid) == os.getpid():
        return True
    try:
        import psutil
        line = " ".join(psutil.Process(int(pid)).cmdline())
    except Exception:  # noqa: BLE001
        return False
    return f"wm.downloads {word}" in line  # 紧跟在模块名后面的那个词 (路径里也可能有 serve 这几个字母)


def daemon_info(home: Path) -> dict:
    """在跑的下载服务 (daemon.json; 进程不在了返回空)."""
    info = read_json(Path(home) / "daemon.json")
    return info if info.get("port") and _ours(info.get("pid"), "serve") else {}


def watch_info(home: Path) -> dict:
    """小看守的状态 (和 wm/services.py 的 state.json 一个样子), 加上 running: 看守进程还在不在."""
    st = read_json(Path(home) / "watch" / "state.json")
    st["running"] = _ours(st.get("pid"), "watch")
    row = next((s for s in st.get("services") or [] if s.get("name") == "downloads"), {})
    return {"running": st["running"], "pid": st.get("pid"), "state": row.get("state", ""), "error": row.get("error", ""),
            "since": row.get("since"), "restarts": len(row.get("restarts") or []), "log": row.get("log", "")}


def _lock(home: Path, name: str = "daemon.lock"):
    """同一个数据目录只跑一个下载服务 (小看守用 watch.lock): 独占那个文件的第一个字节, 进程没了系统自己放开."""
    home.mkdir(parents=True, exist_ok=True)
    f = open(home / name, "a+b")  # noqa: SIM115 (整个进程活着的时候都拿着)
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def test_allow(home: Path, extra: list[str] | None = None) -> frozenset:
    """测试放行的本机地址. 真的数据目录 (~/.worldmodel/serve/downloads) 上一律不认: 这个口子只给临时目录里的测试用."""
    items = [x.strip().lower() for x in (os.environ.get(TEST_ALLOW_ENV) or "").split(",") + list(extra or []) if x.strip()]
    if not items:
        return frozenset()
    if os.path.normcase(os.path.abspath(home)) == os.path.normcase(os.path.abspath(real_home())):
        log.warning("真的数据目录上不认测试放行名单 (%s), 没有放行", ", ".join(items))
        return frozenset()
    log.warning("测试放行: 这些本机地址当成可以下的 —— %s (只该出现在测试里)", ", ".join(items))
    return frozenset(items)


def _limits(raw: str | None) -> Limits:
    from ..config import load_config
    conf = dict((load_config().get("downloads") or {}))
    extra = json.loads(raw) if raw else {}
    limits = Limits.from_config({**conf, **extra})
    for key in ("retry_delays", "read_timeout_s", "checkpoint_s"):  # 只有测试会改这几样 (--limits)
        if key in extra:
            setattr(limits, key, tuple(extra[key]) if key == "retry_delays" else float(extra[key]))
    return limits


def _logging(home: Path, name: str = "daemon.log", console: bool = True) -> None:
    """日志: 下载服务写 <数据目录>/daemon.log (顺带打到标准输出), 小看守写 watch.log (它是 pythonw 起的, 没有标准输出)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if console and sys.stdout is not None:
        out = logging.StreamHandler(sys.stdout)
        out.setFormatter(fmt)
        root.addHandler(out)
    with contextlib.suppress(OSError):
        home.mkdir(parents=True, exist_ok=True)
        keep = logging.handlers.RotatingFileHandler(home / name, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        keep.setFormatter(fmt)
        root.addHandler(keep)


GIVE_UP_STEPS = 60  # 小看守连着这么多遍 (按 tick 算约半分钟) 都做不完一遍, 就不再试了 (下载服务照旧开着)


class _Quiet:
    """同一类小毛病只在第一次、之后每 every 秒记一笔 (带上这一阵出了几次), 日志不刷屏."""

    def __init__(self, every: float = 60.0):
        self.every, self.last, self.count = every, None, 0

    def note(self, what: str, exc: BaseException) -> None:
        self.count += 1
        now = time.monotonic()
        if self.last is None or now - self.last >= self.every:
            log.warning("%s: %s: %s (这一阵第 %d 次)", what, type(exc).__name__, str(exc)[:160], self.count)
            self.last, self.count = now, 0


class Service:
    def __init__(self, home: Path, root: Path, limits: Limits, allow: frozenset, idle_exit: float):
        from ..agent.tools import _proxy  # 和 web_fetch 用同一个代理 (环境变量, 没有就用系统代理)
        self.home, self.idle_exit = home, idle_exit
        self.token = secrets.token_urlsafe(32)
        self.queue = DownloadQueue(home, root, limits, allow=allow, proxy=_proxy())
        self.stop = asyncio.Event()
        self.port = 0

    # ---- 本机接口 ----
    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": SERVICE, "boot": self.queue.boot, "pid": os.getpid()})

    async def rpc(self, request: web.Request) -> web.Response:
        host = (request.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return web.json_response({"ok": False, "code": "forbidden", "error": "只认本机"}, status=403)
        if not hmac.compare_digest(request.headers.get("X-WM-Token", ""), self.token):
            return web.json_response({"ok": False, "code": "forbidden", "error": "令牌不对"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "code": "bad-args", "error": "请求要是 JSON 对象"}, status=400)
        try:
            return web.json_response({"ok": True, **(await self.call(str(body.get("op") or ""), body))})
        except DownloadError as exc:
            return web.json_response({"ok": False, "code": exc.code, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 (没想到的错: 告诉宿主, 服务接着跑)
            log.exception("处理 %s 时出错", body.get("op"))
            return web.json_response({"ok": False, "code": "internal", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}, status=500)

    @staticmethod
    def _actor(body: dict) -> dict:
        """谁在动这个任务: 宿主说是你 (页面上点的) 才算你, 不然按会话算."""
        actor = body.get("actor")
        if isinstance(actor, dict) and actor.get("kind") == "user":
            return USER
        return {"kind": "session", "id": str((actor or {}).get("id") or body.get("session") or "")}

    async def call(self, op: str, b: dict) -> dict:
        q = self.queue
        if op == "enqueue":
            return q.enqueue(b.get("args") or {}, session=str(b.get("session") or ""), owner=str(b.get("owner") or ""),
                             owner_title=str(b.get("owner_title") or ""), run=str(b.get("run") or ""))
        if op == "status":
            return q.status(b.get("id"), str(b.get("viewer") or ""))
        if op == "wait":
            return {"job": await q.wait(b.get("id"), float(b.get("wait_s") if b.get("wait_s") is not None else 30),
                                        str(b.get("viewer") or ""))}
        if op == "pause":
            return {"job": await q.pause(b.get("id"), self._actor(b))}
        if op == "resume":
            return {"job": q.resume(b.get("id"), self._actor(b))}
        if op == "cancel":
            return {"job": await q.cancel(b.get("id"), self._actor(b))}
        if op == "cleanup":
            return q.cleanup(str(b.get("scope") or ""), self._actor(b))
        if op == "answer":
            return {"job": q.answer(str(b.get("ref") or ""), b.get("ok") is True, str(b.get("via") or ""))}
        if op == "shown":
            q.shown(b.get("id"), b.get("seq"))
            return {}
        if op == "changes":
            return await q.changes(int(b.get("since") or 0), str(b.get("boot") or ""), float(b.get("wait_s") or 0))
        if op == "ack":
            return {"acked": q.ack(b.get("id"))}
        if op == "info":
            return {"boot": q.boot, "dir": str(q.root), "home": str(self.home), "proxy": q.proxy or "", "pid": os.getpid(),
                    "limits": {"max_file_bytes": q.limits.max_file_bytes, "reserve_bytes": q.limits.reserve_bytes,
                               "unknown_ok_bytes": q.limits.unknown_ok_bytes, "max_active": q.limits.max_active},
                    "summary": summary(self.home), "storage": dict(q.storage), "dir_error": q.dir_error, "legacy": dict(q.legacy)}
        if op == "shutdown":
            self.stop.set()
            return {}
        raise DownloadError("bad-args", f"没有 {op or '(空)'} 这个操作")

    # ---- 跑 ----
    async def run(self, port: int) -> int:
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_post("/rpc", self.rpc)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            log.error("端口 %s 绑不上: %s", port, exc)
            await runner.cleanup()
            return 3
        self.port = sock.getsockname()[1]
        await web.SockSite(runner, sock).start()
        await self.queue.start()
        write_json(self.home / "daemon.json", {"pid": os.getpid(), "port": self.port, "token": self.token, "boot": self.queue.boot,
                                               "started": time.time(), "dir": str(self.queue.root), "dir_error": self.queue.dir_error,
                                               "legacy": dict(self.queue.legacy)})
        log.info("下载服务起来了: 127.0.0.1:%s, 数据 %s, 下载到 %s, 代理 %s", self.port, self.home, self.queue.root,
                 self.queue.proxy or "不用")
        idle = asyncio.ensure_future(self._idle())
        stopped = asyncio.ensure_future(self.stop.wait())
        try:
            await asyncio.wait([idle, stopped], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (idle, stopped):
                t.cancel()
            await self.queue.close()
            await runner.cleanup()
            with contextlib.suppress(OSError):
                (self.home / "daemon.json").unlink()
        log.info("下载服务退出 (%s)", "有人让停" if self.stop.is_set() else "没活了")
        return 0

    async def _idle(self) -> None:
        """没在排在下的、没有没回报的、宿主也有一阵没来过: 退出 (下次要用时看守再把它开起来).
        退之前把宿主留的 wake 记号拿掉: 看守看到"没人要用"才跟着退, 不然会再把它开起来."""
        while True:
            await asyncio.sleep(0.5)
            if not self.queue.busy() and time.time() - self.queue.last_activity >= self.idle_exit:
                with contextlib.suppress(OSError):
                    (self.home / "wake").unlink()
                return


def serve(a) -> int:
    home = Path(a.home)
    _logging(home)
    lock = _lock(home)
    if lock is None:
        log.info("这个数据目录已经有一个下载服务在跑, 这一个退出")
        return 0
    try:
        allow = test_allow(home, a.test_allow)
        return asyncio.run(Service(home, Path(a.dir), _limits(a.limits), allow, float(a.idle_exit)).run(int(a.port)))
    finally:
        lock.close()


def _free_port(prefer: int) -> int:
    for port in (prefer, 0):
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return 0


def serve_cmd(home: Path, root: Path, port: int, idle_exit: float = IDLE_EXIT_S, limits: str | None = None,
              python: str | None = None) -> list[str]:
    from .. import services as S
    cmd = [python or S._console_python(), "-m", "wm.downloads", "serve", "--home", str(home), "--dir", str(root),
           "--port", str(port), "--idle-exit", str(idle_exit)]
    return cmd + (["--limits", limits] if limits else [])


def watch(a) -> int:
    """小看守. 状态机是 wm/services.py 的 Supervisor (挂了按退避拉起来, 10 分钟挂 restart_max 次就不再拉)."""
    from .. import services as S
    # 总看守只认写死的几个名字 (services.ORDER); 这里是我们自己的进程, 给它添上这一个, 不改 wm/services.py.
    # 以后总看守能按清单起服务了, 下载服务直接归它管, 这个小看守就不用了.
    if "downloads" not in S.ORDER:
        S.ORDER.append("downloads")
    S.LABEL.setdefault("downloads", "下载队列")
    home = Path(a.home)
    if a.detach:
        # 宿主起的: 再起一个真正干活的自己, 这一个马上退出. 干活的那个的父进程就不在了, 不算宿主的子进程 ——
        # 有人连进程树一起结束宿主 (任务管理器、看守的 _kill_tree) 时不会把正在下的一起带走.
        argv = [x for x in sys.argv[1:] if x != "--detach"]
        subprocess.Popen([sys.executable, "-m", "wm.downloads", *argv], cwd=os.getcwd(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=S.DETACH | S.NO_WINDOW, close_fds=True)
        return 0
    # 同一个数据目录只跑一个小看守: 机器忙时第一个还没写出状态文件, 宿主等不到就会再起一个, 两个看守各管一个下载服务
    # (2026-11-11 全套测试里留下过这样一对). 状态文件有早有晚, 锁是抢得到才算 —— 抢不到的马上退出
    lock = _lock(home, "watch.lock")
    if lock is None:
        print("小看守已经在跑了")
        return 0
    try:
        running = daemon_info(home)  # 下载服务还在 (上一个看守没了): 认它的端口, 它没了再接手
        port = int(running.get("port") or 0) or _free_port(int(a.port))
        spec = S.Spec("downloads", serve_cmd(home, Path(a.dir), port, float(a.idle_exit), a.limits), port, "/health", 60.0)
        quiet = _Quiet()

        class Watcher(S.Supervisor):
            """wm/services.py 的 Supervisor, 只在"写状态文件"这一步多一层: 写失败不往外抛. Windows 上宿主、页面正好开着
            state.json 在读时, 看守"写临时文件再换"会换不了 (PermissionError); 原来的 save 没接住, 看守就崩了, 收尾还会把
            正在下的下载服务一起结束 (2026-11-11 评审时在这台机器上量过: 一边反复换、一边反复读, 6 秒里换失败 244/378 次).
            这里稍等再换, 还不行就记一笔、这一遍的状态先不存, 下一遍照常再写. 不改 wm/services.py (公用的, 别的会话也在动)."""

            def save(self) -> None:
                for attempt in range(4):
                    try:
                        return super().save()
                    except OSError as exc:
                        if attempt == 3:
                            quiet.note("小看守写状态文件没成", exc)
                            return
                        time.sleep(0.03 * (attempt + 1))

        sup = Watcher([spec], home=home / "watch", logs=home / "logs", tick=float(a.tick), restart_max=int(a.restart_max))
        with contextlib.suppress(OSError):
            for old in (home / "watch").glob("req-*.json"):  # 上一次留下的请求不算
                old.unlink(missing_ok=True)
        keep, bad = False, 0  # keep: 看守自己出了岔子退出时, 别把正在下的下载服务一起结束
        try:
            while not sup.stop_all:
                try:
                    sup.step()
                    bad = 0
                except OSError as exc:  # 读请求、起子进程、开日志这些小事出错 (比如别的程序正占着文件): 记一笔, 下一遍再来
                    bad += 1
                    quiet.note("小看守这一遍没做完", exc)
                    if bad >= GIVE_UP_STEPS:
                        log.error("小看守连着 %d 遍都没做完, 不再试了 (下载服务照旧开着; 下次宿主要用时会起新的小看守接手)", bad)
                        keep = True
                        return 1
                    time.sleep(sup.tick)
                    continue
                st = sup.st["downloads"]["state"]
                if st == "failed":  # 起不来: 状态留在 state.json 里 (宿主读得到, 不会再一遍遍起), 看守退出
                    return 1
                if st in ("down", "off", "stopped") and "downloads" not in sup.proc and not wanted(home):
                    return 0  # 没活了 (下载服务自己退了): 看守也退
                time.sleep(sup.tick)
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception:  # noqa: BLE001 (没想到的错: 记下来, 不连累正在下的下载服务)
            log.exception("小看守出了没想到的错, 退出 (下载服务照旧开着; 下次宿主要用时会起新的小看守接手)")
            keep = True
            return 2
        finally:
            if not keep:
                for name in list(sup.proc):
                    sup._reap(name)
            sup.save()
    finally:
        lock.close()


def status(a) -> int:
    home = Path(a.home)
    d, w, s = daemon_info(home), watch_info(home), summary(home)
    print(f"下载服务: {'在跑 (进程 %s, 端口 %s)' % (d['pid'], d['port']) if d else '没在跑 (有活的时候才开)'}")
    print(f"小看守: {'在跑 (进程 %s)' % w['pid'] if w['running'] else '没在跑'}"
          + (f"; 上次的状态 {w['state']} {w['error']}" if w["state"] in ("failed", "down") else ""))
    print(f"队列: {s['active']} 个在排或在下, {s['awaiting']} 个等点头, {s['unreported']} 个结束了还没回报")
    print(f"数据目录: {home}")
    print(f"下载文件夹: {d.get('dir') or a.dir}")  # 服务在跑就说它实际用的, 不在跑说照配置会用的
    if d.get("dir_error"):
        print(f"下载文件夹写不进去: {d['dir_error']} (服务起来时试的; 不会换到别的地方)")
    if d.get("legacy"):
        print(legacy_text(d["legacy"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m wm.downloads", description="下载队列的服务")
    ap.add_argument("what", choices=["serve", "watch", "status"])
    ap.add_argument("--home", default=str(real_home()), help="数据目录 (默认 ~/.worldmodel/serve/downloads)")
    ap.add_argument("--dir", default="", help="下载到哪 (默认照配置 downloads.dir, 没配是 <桌宠文件夹>/download-queue)")
    ap.add_argument("--port", type=int, default=-1, help=f"只绑本机的端口 (默认照配置 downloads.port, 没配是 {PORT}; 0 = 随便挑一个)")
    ap.add_argument("--idle-exit", type=float, default=IDLE_EXIT_S, help="没活多久自己退出 (秒)")
    ap.add_argument("--limits", help="覆盖配置里 downloads 的几样上限 (JSON 对象; 测试用)")
    ap.add_argument("--test-allow", action="append", default=[], help="测试放行的本机 主机:端口 (真的数据目录上不认)")
    ap.add_argument("--tick", type=float, default=0.5, help="小看守多久看一次 (秒)")
    ap.add_argument("--restart-max", type=int, default=5, help="小看守: 10 分钟里挂几次就不再拉")
    ap.add_argument("--detach", action="store_true", help="小看守: 另起一个脱开的自己来干活, 这一个马上退出 (宿主用)")
    a = ap.parse_args(argv)
    if not a.dir or (a.what != "status" and a.port < 0):
        from ..config import load_config
        conf = load_config().get("downloads") or {}
        a.dir = a.dir or str(dir_for(conf))
        a.port = a.port if a.port >= 0 else int(conf.get("port") or PORT)
    if a.what == "watch" and not a.detach:  # 真干活的小看守 (--detach 的那个只是转手起它, 马上退出): 日志写 <数据目录>/watch.log
        _logging(Path(a.home), "watch.log", console=False)
    return {"serve": serve, "watch": watch, "status": status}[a.what](a)


if __name__ == "__main__":
    sys.exit(main())
