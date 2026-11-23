"""2D 广场插件的服务进程. 样板: 主程序不引用, 宿主第 1 步可以替换.

它握着全部真值 (中继发来所有人的位置), 只把她感知得到的事件交给宿主 (plaza_perception 是唯一的门);
旁观面板要的全部真值走 panel 帧, 宿主只转给面板.

环境变量 (清单里 service.cmd 是固定的 ["python", "relay_client.py"], 每次运行的参数都从这里给):
  WM_PLUGIN_TOKEN   只有宿主知道的令牌, 桥的连接必须带 (没有就不启动)
  WM_PLUGIN_PORT    本机桥的端口; 0 或不给 = 自己挑一个空的, 在标准输出打 {"ready": true, "port": N}
  PLAZA_RELAY       ws://127.0.0.1:端口/plaza   (只许 127.0.0.1)
  PLAZA_NAME        在广场里的名字
  PLAZA_CODE        邀请码; 不给就必须 PLAZA_CREATE=1 开一个新房间 (码从 panel 帧和 /health 里看)
  PLAZA_BACKOFF     重连退避秒数, 逗号分隔, 默认 0.5,1,2,5

掉线后带着同一个邀请码退避重连 (房间还有别人时码有效); 房间没了 (bad_code) 就停止重连, 报 room_gone.
联网只连 PLAZA_RELAY; aiohttp 客户端不读环境里的代理 (trust_env=False).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plaza_perception import Perception  # noqa: E402

MAX_FRAME = 4096
ACK_TIMEOUT = 5.0
HOST = "127.0.0.1"


def parse_relay(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "ws" or u.hostname != HOST:
        raise ValueError("PLAZA_RELAY 只能是 ws://127.0.0.1:端口/plaza")
    return url


class PlazaClient:
    def __init__(self, relay: str, name: str, token: str, *, code: str = "", create: bool = False, port: int = 0,
                 backoff=(0.5, 1.0, 2.0, 5.0)):
        self.relay, self.name, self.token = parse_relay(relay), name, token
        self.code, self.create, self.port = code.strip().upper(), create, int(port)
        self.backoff = tuple(backoff)
        self.p = Perception()
        self.state = "connecting"
        self.joins = 0
        self._http: aiohttp.ClientSession | None = None
        self._relay_ws: aiohttp.ClientWebSocketResponse | None = None
        self._host_ws: web.WebSocketResponse | None = None
        self._backlog: deque = deque(maxlen=300)   # 宿主还没连上时攒着的帧
        self._acks: dict[str, asyncio.Future] = {}
        self._rid = 0
        self._leaving = asyncio.Event()
        self._done = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._relay_task: asyncio.Task | None = None

    # -- 起停 ------------------------------------------------------------------
    async def serve(self) -> int:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/host", self._host)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
        await self._runner.setup()
        await web.TCPSite(self._runner, HOST, self.port).start()
        self.port = self._runner.addresses[0][1]
        self._http = aiohttp.ClientSession(trust_env=False)
        self._relay_task = asyncio.create_task(self._relay_loop())
        return self.port

    async def wait_done(self) -> None:
        await self._done.wait()

    async def close(self) -> None:
        self._leaving.set()
        if self._relay_task is not None:
            while not self._relay_task.done():
                self._relay_task.cancel()
                await asyncio.wait([self._relay_task], timeout=1)
        if self._relay_ws is not None and not self._relay_ws.closed:
            with contextlib.suppress(Exception):
                await self._relay_ws.close()
        if self._http is not None:
            await self._http.close()
        if self._host_ws is not None and not self._host_ws.closed:
            with contextlib.suppress(Exception):
                await self._host_ws.close()   # 不关的话 runner.cleanup 会等宿主那条桥自己断
        if self._runner is not None:
            await self._runner.cleanup()
        self._done.set()

    # -- 本机桥 ----------------------------------------------------------------
    async def _health(self, request: web.Request):
        return web.json_response({"ok": True, "state": self.state, "code": self.code})

    async def _host(self, request: web.Request):
        if request.headers.get("Authorization", "") != f"Bearer {self.token}":
            return web.Response(status=401, text="令牌不对")
        if self._host_ws is not None and not self._host_ws.closed:
            return web.Response(status=409, text="已经有一个宿主连着")
        ws = web.WebSocketResponse(max_msg_size=64 * 1024, heartbeat=20)
        await ws.prepare(request)
        self._host_ws = ws
        await ws.send_str(json.dumps({"t": "state", "state": self.state}))
        while self._backlog:
            await ws.send_str(self._backlog.popleft())
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    f = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(f, dict):
                    asyncio.create_task(self._from_host(f))
        finally:
            if self._host_ws is ws:
                self._host_ws = None
        return ws

    async def _to_host(self, frame: dict) -> None:
        text = json.dumps(frame, ensure_ascii=False)
        ws = self._host_ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_str(text)
                return
            except (ConnectionError, RuntimeError):
                pass
        if frame.get("t") in ("event", "receipt", "state"):
            self._backlog.append(text)

    async def _emit(self, events: list[dict]) -> None:
        for e in events:
            await self._to_host({"t": "event", **e})

    async def _set_state(self, state: str) -> None:
        self.state = state
        await self._to_host({"t": "state", "state": state})
        await self._to_host({"t": "panel", "data": {**self.p.panel(), "state": state}})

    # -- 宿主 -> 插件 ----------------------------------------------------------
    async def _from_host(self, f: dict) -> None:
        t, fid = f.get("t"), f.get("id")
        if t == "leave":
            await self._leave()
        elif t == "act":
            await self._act(fid, f.get("name"), f.get("args") or {})
        elif t == "query":
            data = self.p.look() if f.get("name") == "plaza_look" else {"error": "unknown_query"}
            await self._to_host({"t": "result", "id": fid, "name": f.get("name"), "data": data})

    async def _act(self, fid, name, args: dict) -> None:
        frame = {"plaza_move": lambda: {"t": "move", "dir": args.get("direction")},
                 "plaza_say": lambda: {"t": "say", "text": args.get("text")},
                 "plaza_emote": lambda: {"t": "emote", "emote": args.get("emote")}}.get(name)
        ok, why = False, "unknown_action"
        if frame is not None:
            ok, why = await self._ask_relay(frame())
        await self._to_host({"t": "receipt", "id": fid, "type": "act_result",
                             "fields": {"ok": ok, "why": why, "own_position": self.p.own_position()}})

    async def _ask_relay(self, frame: dict) -> tuple[bool, str]:
        ws = self._relay_ws
        if ws is None or ws.closed or self.state != "in_room":
            return False, "offline"
        self._rid += 1
        rid = f"r{self._rid}"
        fut = self._acks[rid] = asyncio.get_running_loop().create_future()
        try:
            await ws.send_str(json.dumps({**frame, "rid": rid}))
            ack = await asyncio.wait_for(fut, ACK_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            return False, "timeout"
        finally:
            self._acks.pop(rid, None)
        return bool(ack.get("ok")), str(ack.get("why") or "")

    async def _leave(self) -> None:
        """叫停: 离开中继 (别人会看到她走), 确认离场, 然后退出."""
        self._leaving.set()
        self.state = "left"
        ws = self._relay_ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.send_str(json.dumps({"t": "bye"}))
                await ws.close()
        await self._to_host({"t": "left"})
        await asyncio.sleep(0.2)   # 让确认帧先出去
        self._done.set()

    # -- 中继 ------------------------------------------------------------------
    async def _relay_loop(self) -> None:
        attempt = 0
        while not self._leaving.is_set():
            try:
                async with self._http.ws_connect(self.relay, max_msg_size=MAX_FRAME, heartbeat=20) as ws:
                    self._relay_ws = ws
                    hello = {"t": "hello", "name": self.name}
                    hello.update({"code": self.code} if self.code else {"create": True})
                    await ws.send_str(json.dumps(hello))
                    if await self._serve_relay(ws):
                        attempt = 0
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                pass
            finally:
                self._relay_ws = None
                for fut in list(self._acks.values()):
                    if not fut.done():
                        fut.cancel()
            if self._leaving.is_set() or self.state == "room_gone":
                return
            await self._set_state("reconnecting")
            await asyncio.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            attempt += 1

    async def _serve_relay(self, ws) -> bool:
        joined = False
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                f = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(f, dict):
                continue
            t = f.get("t")
            if t == "err":
                why = f.get("why")
                if why == "bad_code" and self.joins:
                    await self._set_state("room_gone")   # 房间没了 (人都走了): 不再重连
                    self._done.set()
                    return False
                if why in ("name_taken", "room_full", "rate"):
                    return False                          # 稍后再试
                await self._set_state("failed:" + str(why)[:20])
                self._leaving.set()
                self._done.set()
                return False
            if t == "ack":
                fut = self._acks.get(f.get("rid"))
                if fut is not None and not fut.done():
                    fut.set_result(f)
            events = self.p.feed(f)
            if t == "joined":
                joined = True
                self.joins += 1
                self.code = f.get("code") or self.code
                self.state = "in_room"
                await self._to_host({"t": "state", "state": "in_room"})
                events.insert(0, {"type": "act_result", "fields": {"ok": True, "why": "joined",
                                                                     "own_position": self.p.own_position()}, "text": ""})
            await self._emit(events)
            if t in ("joined", "ev", "ack"):   # 面板要看全部真值: 视野外的人动了也要更新
                await self._to_host({"t": "panel", "data": {**self.p.panel(), "state": self.state}})
        return joined


async def amain() -> int:
    token = os.environ.get("WM_PLUGIN_TOKEN", "")
    name = os.environ.get("PLAZA_NAME", "")
    if not token or not name:
        print("缺 WM_PLUGIN_TOKEN 或 PLAZA_NAME", file=sys.stderr)
        return 2
    code, create = os.environ.get("PLAZA_CODE", ""), os.environ.get("PLAZA_CREATE") == "1"
    if not code and not create:
        print("要给 PLAZA_CODE, 或者 PLAZA_CREATE=1 开新房间", file=sys.stderr)
        return 2
    backoff = tuple(float(x) for x in os.environ.get("PLAZA_BACKOFF", "0.5,1,2,5").split(",") if x.strip())
    try:
        client = PlazaClient(os.environ.get("PLAZA_RELAY", ""), name, token, code=code, create=create,
                             port=int(os.environ.get("WM_PLUGIN_PORT", "0") or 0), backoff=backoff)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    port = await client.serve()
    print(json.dumps({"ready": True, "port": port}), flush=True)
    await client.wait_done()
    await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
