"""世界宿主: 管一个世界插件的进程. 样板: 主程序不引用, 宿主第 1 步可以替换. 契约第 4 节(叫停)、第 6 节.

桥协议 v0 (契约没定, 宿主第 1 步会定正式的; 这里是样板). 插件在 127.0.0.1 上开一个本机服务, 只收带令牌的宿主:
  GET /health                         -> {"ok": true, "state": ...}
  WebSocket /host  (Authorization: Bearer <令牌>)
    插件 -> 宿主  {"t":"event","type","fields","text"}      事件 (宿主按清单打包封, 没声明的丢掉)
                  {"t":"receipt","id","type","fields"}      自己动作的回执, 也是事件
                  {"t":"panel","data":{...}}                旁观面板用的全部真值: 只转给面板, 绝不进会话
                  {"t":"result","id","data"}                query 的返回
                  {"t":"left"}                              叫停之后的离场确认
                  {"t":"state","state":...}
    宿主 -> 插件  {"t":"act","id","name","args"} / {"t":"query","id","name"} / {"t":"leave"}
插件进程启动后在标准输出打一行 {"ready": true, "port": N} (端口环境变量 WM_PLUGIN_PORT 给 0 就由它自己挑).

叫停 leave(): 立刻停发新动作, 发 leave, 等 left 至多 ack_timeout_s 秒; 超时由 wm.services._kill_tree (看守的现成函数) 结束整棵进程树.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass

import aiohttp

from wm.channels.outbound import clean_in

from .envelope import Audit, Envelope, WorldSpec, make_envelope
from .session import WorldSession

MAX_BRIDGE_FRAME = 64 * 1024
READY_TIMEOUT = 20.0


@dataclass(frozen=True)
class LeaveResult:
    status: str       # acknowledged: 在时限内确认离场; killed: 超时, 进程树被结束; gone: 本来就没有进程
    elapsed: float    # 从叫停到确认 (或到被结束) 的秒数


def default_kill(pid: int) -> None:
    from wm.services import _kill_tree   # 看守的现成函数, 只 import 不改
    _kill_tree(int(pid), timeout=3.0)


class WorldHost:
    def __init__(self, spec: WorldSpec, session: WorldSession, *, audit: Audit | None = None, kill=default_kill,
                 on_panel=None, on_log=None):
        self.spec, self.session = spec, session
        self.audit = audit if audit is not None else session.audit
        self.kill = kill
        self.on_panel, self.on_log = on_panel, on_log
        self.panel: dict | None = None
        self.log: deque = deque(maxlen=400)
        self.state = "new"
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.port = 0
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tasks: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self._left = asyncio.Event()
        self._leaving = False
        self._stopped = False
        self._seq = 0

    # -- 起停 ------------------------------------------------------------------
    async def launch(self, argv: list[str], env: dict, token: str, cwd=None) -> None:
        """起插件进程, 等它打出 ready 那一行, 再连桥."""
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self.pid = self.proc.pid
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), READY_TIMEOUT)
            info = json.loads(line.decode("utf-8", "replace"))
            if not info.get("ready"):
                raise ValueError("没有 ready")
        except Exception as exc:  # noqa: BLE001
            await self.stop()
            raise RuntimeError(f"插件进程没有起来: {exc!r}") from exc
        self._tasks += [asyncio.create_task(self._drain(self.proc.stdout)), asyncio.create_task(self._drain(self.proc.stderr))]
        await self.attach(int(info["port"]), token)

    async def attach(self, port: int, token: str, pid: int | None = None) -> None:
        self.port = port
        self.pid = self.pid or pid
        self._http = aiohttp.ClientSession(trust_env=False)   # 不走环境里的代理 (记忆 session-env-no-proxy)
        self._ws = await self._http.ws_connect(f"http://127.0.0.1:{port}/host", headers={"Authorization": f"Bearer {token}"},
                                               max_msg_size=MAX_BRIDGE_FRAME, heartbeat=20)
        self.state = "connected"
        self._tasks += [asyncio.create_task(self._read()), asyncio.create_task(self._drive())]

    @staticmethod
    async def _drain(stream) -> None:
        with contextlib.suppress(Exception):
            while await stream.readline():
                pass

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        for t in self._tasks:
            while not t.done():
                t.cancel()
                await asyncio.wait([t], timeout=1)
        self._tasks = []
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.close()
        await self._reap()
        self.state = "stopped"

    async def _reap(self) -> None:
        if self.pid and self._alive():
            await asyncio.get_running_loop().run_in_executor(None, self.kill, self.pid)
        if self.proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.proc.wait(), 5)

    def _alive(self) -> bool:
        try:
            import psutil
            p = psutil.Process(self.pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except Exception:  # noqa: BLE001
            return False

    # -- 记录 ------------------------------------------------------------------
    def _note(self, kind: str, **detail) -> None:
        item = {"k": kind, "ts": round(time.time(), 3), **detail}
        self.log.append(item)
        if self.on_log:
            self.on_log(item)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- 插件 -> 宿主 ----------------------------------------------------------
    async def _read(self) -> None:
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                f = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(f, dict):
                continue
            t = f.get("t")
            if t in ("event", "receipt"):
                self._on_event(f)
            elif t == "panel":
                if isinstance(f.get("data"), dict):
                    self.panel = f["data"]   # 全部真值: 只留给面板, 不碰会话
                    if self.on_panel:
                        self.on_panel(self.panel)
            elif t == "result":
                self._on_result(f)
            elif t == "left":
                self._left.set()
            elif t == "state":
                self.state = str(f.get("state") or self.state)[:20]
                self._note("state", state=self.state)
        if not self._leaving and not self._stopped and self.state not in ("room_gone", "left") and not self.state.startswith("failed"):
            self.state = "bridge_closed"
            self._note("state", state=self.state)

    def _on_event(self, f: dict) -> None:
        env = make_envelope(self.spec, {"type": f.get("type"), "fields": f.get("fields"), "text": f.get("text"),
                                        **{k: v for k, v in f.items() if k in ("trusted", "as_instruction", "is_owner")}},
                            self._next_seq(), self.audit)
        if env is None:
            return
        self._note("in", seq=env.seq, type=env.type, sense=env.sense, fields=env.fields, text=env.text)
        if not self._leaving:
            self.session.push(env)
            self._wake.set()

    def _on_result(self, f: dict) -> None:
        """query 的返回不是声明过的事件类型: 作为一条回执式的注记交给会话, 同样过 clean_in."""
        note = Envelope(self._next_seq(), self.spec.prefix, round(time.time(), 3), str(f.get("name") or "query")[:32],
                        "receipt", "batch", {}, clean_in(json.dumps(f.get("data"), ensure_ascii=False), 400))
        self._note("in", seq=note.seq, type=note.type, sense=note.sense, fields={}, text=note.text)
        if not self._leaving:
            self.session.push(note)
            self._wake.set()

    # -- 宿主 -> 插件 ----------------------------------------------------------
    async def _send(self, frame: dict) -> bool:
        if self._ws is None or self._ws.closed:
            return False
        try:
            await self._ws.send_str(json.dumps(frame, ensure_ascii=False))
            return True
        except (ConnectionError, RuntimeError):
            return False

    async def _drive(self) -> None:
        while not self._stopped:
            nd = self.session.next_due_in()
            w = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait([w], timeout=0.5 if nd is None else min(0.5, max(0.01, nd)))
            finally:
                w.cancel()
            self._wake.clear()
            if self._leaving or self._stopped:
                continue
            try:
                actions = await self.session.step()
            except Exception as exc:  # noqa: BLE001 - 模型出错这一轮就算了
                self.audit.add("model_error", error=repr(exc)[:120])
                continue
            for a in actions:
                if self._leaving:
                    break
                decl = self.spec.actions[a.name]
                if a.blocked:
                    self._note("blocked", name=a.name, why=a.blocked)
                    if decl.receipt:
                        env = make_envelope(self.spec, {"type": decl.receipt, "fields": {"ok": False, "why": "blocked"}},
                                            self._next_seq(), self.audit)
                        if env:
                            self._note("in", seq=env.seq, type=env.type, sense=env.sense, fields=env.fields, text=env.text)
                            self.session.push(env)
                    continue
                self._note("act", id=a.id, name=a.name, args=a.args)
                await self._send({"t": "query" if decl.kind == "query" else "act", "id": a.id, "name": a.name, "args": a.args})

    # -- 叫停 ------------------------------------------------------------------
    async def leave(self) -> LeaveResult:
        t0 = time.monotonic()
        self._leaving = True
        self._note("leave", timeout_s=self.spec.leave_s)
        if self.pid is None or not self._alive():
            await self.stop()
            return LeaveResult("gone", time.monotonic() - t0)
        ack = False
        if await self._send({"t": "leave"}):
            w = asyncio.ensure_future(self._left.wait())
            try:
                await asyncio.wait([w], timeout=self.spec.leave_s)
            finally:
                w.cancel()
            ack = self._left.is_set()
        if ack:
            status, elapsed = "acknowledged", time.monotonic() - t0
            if self.proc is not None:   # 确认了, 让它自己退; 久久不退也结束掉
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.proc.wait(), 3)
        else:
            self.audit.add("leave_timeout_killed", timeout_s=self.spec.leave_s)
            status = "killed"
            await self._reap()
            elapsed = time.monotonic() - t0
        self._note("left", status=status, elapsed=round(elapsed, 3))
        await self.stop()
        return LeaveResult(status, elapsed)


def plugin_env(base: dict | None = None, **extra) -> dict:
    """给插件进程的环境: 干净的副本 (去掉代理变量, 只对本机说话), 再加上宿主给的键."""
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update({k: str(v) for k, v in extra.items()})
    return env
