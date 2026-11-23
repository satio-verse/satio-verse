"""2D 广场的本机中继 (样板: 主程序不引用, 宿主第 1 步 / 服务器那一步可以替换). 2026-11-15, 世界1.

方案: 知识库《世界与形象方案（2026-11-11）》第二节"2D 广场"; 决定: 《等你拍板的事》世界1 (本机闭环) / 世界6 (只做邀请码房间).

做什么: 邀请码房间、24x14 的格子、权威位置 (客户端只发方向, 中继算新位置)、转发 进出 / 移动 / 说话 / 表情.
不做: 跑 AI、存聊天 (说的话转完就忘, 默认不记任何帧)、落盘 (房间只在内存里, 进程一退就没了)、公开大厅 / 房间列表 /
按名字找人 (世界6: 只做朋友间的邀请码房间)、语音.

本机这一轮的取舍: 房间里每个成员都收到所有人的位置, 由插件客户端的感知门 (plaza_perception) 筛成"她感知得到的".
上服务器那一步必须改成"中继只转附近的人和附近的话" (位置、说话都按距离过滤), 现在朋友都在本机, 先不做.

只听 127.0.0.1, 没有选项. 拒绝带 Origin 头的连接 (浏览器网页连不进来).
只依赖标准库和 aiohttp, 不导入 wm, 不读 ~/.worldmodel.

帧是 JSON 文本, 一帧不超过 4 KB. 上行帧可带 rid, ack / err 会带回去.
  上行  hello {name, create: true | code}       必须是第一帧
        move {dir: left|right|up|down} / say {text} / emote {emote: wave|smile|nod} / bye / ping
  下行  joined {code, you: {id, name, pos}, members: [{id, name, pos}], grid: {w, h}}
        ev {k: entered|left|moved|said|emote, id, name, pos, ts, text?, emote?}
        ack {rid, ok, pos?, why?} / err {rid, why} / pong
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import re
import secrets
import sys
import time
from collections import deque

from aiohttp import WSMsgType, web

HOST = "127.0.0.1"  # 只听本机, 没有别的选项
GRID_W, GRID_H = 24, 14
MAX_MEMBERS = 8
MAX_ROOMS = 16
MAX_FRAME = 4096
MAX_SAY = 200
MAX_NAME = 16
DIRS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}
EMOTES = ("wave", "smile", "nod")
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 0/O/1/I
BAD_CODES_PER_CONN = 3
BAD_CODES_PER_MIN = 20
MOVE_LIMIT = (8, 5.0)    # 5 秒内最多 8 步
SAY_LIMIT = (6, 60.0)    # 每分钟最多 6 句
EMOTE_LIMIT = (6, 60.0)
CLOSE_REPLACED = 4000

_CTL = re.compile("[" + "".join(chr(c) for c in [*range(0x20), 0x7f, *range(0x200b, 0x2010), *range(0x202a, 0x202f), *range(0x2060, 0x2065), *range(0x2066, 0x206a), 0xfeff]) + "]")
_SPACES = re.compile(r"\s+")


def _line(text, limit: int) -> str:
    """名字和说的话: 去控制字符、零宽字符, 压成一行."""
    return _SPACES.sub(" ", _CTL.sub(" ", str(text or ""))).strip()[:limit]


class _Member:
    def __init__(self, mid: str, name: str, pos: tuple[int, int], ws: web.WebSocketResponse):
        self.id, self.name, self.pos, self.ws = mid, name, pos, ws
        self.q: asyncio.Queue = asyncio.Queue()
        self.writer: asyncio.Task | None = None
        self.limits: dict[str, deque] = {"move": deque(), "say": deque(), "emote": deque()}
        self.room: "_Room | None" = None

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "pos": list(self.pos)}


class _Room:
    def __init__(self, code: str):
        self.code = code
        self.members: dict[str, _Member] = {}
        self.counter = 0


class Relay:
    def __init__(self, *, seed=None, clock=time.monotonic, frame_log=None, spawn=None):
        """frame_log: 测试用, 记下每一条收到的帧 (list of (成员名, 原文)); 平时不开, 帧里是别人的话.
        spawn: 测试用, 按顺序给新成员的出生点 [(x, y), ...]; 用完了就随机."""
        self.rng = random.Random(seed)
        self._spawn_list = [tuple(p) for p in (spawn or [])]
        self.clock = clock
        self.frame_log = frame_log
        self.rooms: dict[str, _Room] = {}
        self.runner: web.AppRunner | None = None
        self.port: int | None = None
        self._bad: deque = deque()
        self._conns: set = set()

    # -- 起停 ------------------------------------------------------------------
    async def start(self, port: int = 0) -> int:
        app = web.Application()
        app.router.add_get("/plaza", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, HOST, port).start()
        self.port = self.runner.addresses[0][1]
        return self.port

    async def stop(self) -> None:
        for ws in list(self._conns):
            with contextlib.suppress(Exception):
                await ws.close(code=1001, message=b"relay stopping")
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    # -- 测试用的钩子 -----------------------------------------------------------
    async def kick(self, code: str, name: str) -> bool:
        """把某个成员的连接从中继这头断掉 (测试断线重连用)."""
        room = self.rooms.get(code)
        for m in list(room.members.values()) if room else []:
            if m.name == name:
                await m.ws.close(code=1011, message=b"kicked")
                return True
        return False

    def place(self, code: str, name: str, pos) -> bool:
        """测试用: 把某个成员直接放到某格 (广播一条 moved), 省得靠一步步走来布置场景."""
        room = self.rooms.get(code)
        for m in list(room.members.values()) if room else []:
            if m.name == name:
                m.pos = (int(pos[0]), int(pos[1]))
                self._push(m, {"t": "ack", "rid": None, "ok": True, "pos": list(m.pos)})
                self._broadcast(room, m, "moved")
                return True
        return False

    def members_of(self, code: str) -> list[dict]:
        room = self.rooms.get(code)
        return [m.public() for m in room.members.values()] if room else []

    # -- 连接 ------------------------------------------------------------------
    async def handle(self, request: web.Request):
        if request.headers.get("Origin") is not None:
            return web.Response(status=403, text="不接受网页来的连接")
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=20)
        await ws.prepare(request)
        self._conns.add(ws)
        member: _Member | None = None
        bad = 0
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    f = json.loads(msg.data)
                except ValueError:
                    f = None
                if self.frame_log is not None:
                    self.frame_log.append((member.name if member else "?", msg.data))
                if not isinstance(f, dict):
                    await ws.send_str(json.dumps({"t": "err", "why": "bad_json"}))
                    continue
                if member is None:
                    member = await self._hello(ws, f)
                    if member is None:
                        bad += 1
                        if bad >= BAD_CODES_PER_CONN:
                            break
                    continue
                self._dispatch(member, f)
        finally:
            self._conns.discard(ws)
            if member is not None:
                self._leave(member)
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()
        return ws

    async def _hello(self, ws, f: dict):
        async def err(why: str):
            await ws.send_str(json.dumps({"t": "err", "rid": f.get("rid"), "why": why}))
            return None

        if f.get("t") != "hello":
            return await err("hello_first")
        name = _line(f.get("name"), MAX_NAME)
        if not name:
            return await err("bad_name")
        if f.get("create") is True:
            if len(self.rooms) >= MAX_ROOMS:
                return await err("too_many_rooms")
            code = self._new_code()
            room = self.rooms[code] = _Room(code)
        else:
            now = self.clock()
            while self._bad and now - self._bad[0] >= 60:
                self._bad.popleft()
            if len(self._bad) >= BAD_CODES_PER_MIN:
                return await err("rate")
            code = str(f.get("code") or "").strip().upper()
            room = self.rooms.get(code)
            if room is None:
                self._bad.append(now)
                return await err("bad_code")
        if len(room.members) >= MAX_MEMBERS:
            return await err("room_full")
        if any(m.name.casefold() == name.casefold() for m in room.members.values()):
            return await err("name_taken")
        room.counter += 1
        member = _Member(f"m{room.counter}", name, self._spawn(room), ws)
        member.room = room
        member.writer = asyncio.create_task(self._write(member))
        others = [m.public() for m in room.members.values()]
        room.members[member.id] = member
        self._push(member, {"t": "joined", "code": room.code, "you": member.public(), "members": others,
                            "grid": {"w": GRID_W, "h": GRID_H}})
        self._broadcast(room, member, "entered")
        return member

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_CHARS) for _ in range(8))
            if code not in self.rooms:
                return code

    def _spawn(self, room: _Room) -> tuple[int, int]:
        if self._spawn_list:
            return self._spawn_list.pop(0)
        taken = {m.pos for m in room.members.values()}
        for _ in range(100):
            p = (self.rng.randrange(GRID_W), self.rng.randrange(GRID_H))
            if p not in taken:
                return p
        return (0, 0)

    async def _write(self, m: _Member) -> None:
        while (frame := await m.q.get()) is not None:
            try:
                await m.ws.send_str(json.dumps(frame, ensure_ascii=False))
            except (ConnectionError, RuntimeError):
                return

    def _push(self, m: _Member, frame: dict) -> None:
        m.q.put_nowait(frame)

    def _broadcast(self, room: _Room, who: _Member, kind: str, **extra) -> None:
        ev = {"t": "ev", "k": kind, "id": who.id, "name": who.name, "pos": list(who.pos),
              "ts": round(time.time(), 3), **extra}
        for m in room.members.values():
            if m is not who:
                self._push(m, ev)

    def _leave(self, m: _Member) -> None:
        room = m.room
        m.q.put_nowait(None)
        if room is None or room.members.pop(m.id, None) is None:
            return
        m.room = None
        if room.members:
            self._broadcast(room, m, "left")
        else:
            self.rooms.pop(room.code, None)  # 没人了, 码作废

    # -- 上行帧 ----------------------------------------------------------------
    def _rate_ok(self, m: _Member, kind: str, limit: tuple) -> bool:
        n, window = limit
        q, now = m.limits[kind], self.clock()
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= n:
            return False
        q.append(now)
        return True

    def _dispatch(self, m: _Member, f: dict) -> None:
        t, rid = f.get("t"), f.get("rid")

        def ack(ok: bool, **extra):
            self._push(m, {"t": "ack", "rid": rid, "ok": ok, **extra})

        room = m.room
        if t == "move":
            d = DIRS.get(f.get("dir"))
            if d is None:
                return ack(False, why="bad_dir")
            if not self._rate_ok(m, "move", MOVE_LIMIT):
                return ack(False, why="rate")
            was = m.pos
            m.pos = (min(GRID_W - 1, max(0, m.pos[0] + d[0])), min(GRID_H - 1, max(0, m.pos[1] + d[1])))
            ack(True, pos=list(m.pos))
            if m.pos != was:   # 撞墙没动就不广播
                self._broadcast(room, m, "moved")
        elif t == "say":
            text = _line(f.get("text"), MAX_SAY + 1)
            if not text:
                return ack(False, why="empty")
            if len(text) > MAX_SAY:
                return ack(False, why="too_long")
            if not self._rate_ok(m, "say", SAY_LIMIT):
                return ack(False, why="rate")
            ack(True, pos=list(m.pos))
            self._broadcast(room, m, "said", text=text)
        elif t == "emote":
            if f.get("emote") not in EMOTES:
                return ack(False, why="bad_emote")
            if not self._rate_ok(m, "emote", EMOTE_LIMIT):
                return ack(False, why="rate")
            ack(True, pos=list(m.pos))
            self._broadcast(room, m, "emote", emote=f["emote"])
        elif t == "bye":
            asyncio.create_task(m.ws.close())
        elif t == "ping":
            self._push(m, {"t": "pong"})
        else:
            self._push(m, {"t": "err", "rid": rid, "why": "unknown_frame"})


async def _serve(args) -> None:
    relay = Relay(seed=args.seed)
    port = await relay.start(args.port)
    print(json.dumps({"ready": True, "host": HOST, "port": port}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await relay.stop()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="2D 广场的本机中继 (样板, 只听 127.0.0.1)")
    p.add_argument("--port", type=int, default=8801, help="端口; 0 是随便挑一个空的")
    p.add_argument("--seed", type=int, default=None, help="测试用: 出生点的随机种子")
    args = p.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
