"""体素街区的中继 (样板/原型, 与 plaza_relay.py 同一套写法): 16x16 体素房间、最多 8 人、传送门、AOI 增量.

做什么:
  - 邀请码房间: 一个邀请码 = 一座"街区", 里面 fixed 两个房间 (garden 庭院 / street 街道), 各最多 8 人;
  - 权威位置: 客户端只发方向 (move=[dx,dz]) 和 yaw, 中继按 20 Hz tick 积分、自己算碰撞与边界;
  - 权威方块: 放置/拆掉都要中继点头 (距离、层高、上限、不压人), 客户端只是预言;
  - AOI 增量: 按 4x4 chunk 切, 进 1 出 2 的滞后 (Enter 1 / Exit 2), 只下发视野 chunk 里的方块与人和话;
  - 传送门: 走进拱门 (客户端自己判定) 发 portal, 中继把这个人从 garden 换到 street 或反过来;
  - 满员排队: 第 9 个人不报错, 回 {"t":"queued","position":N,"of":8}, 有人走了按顺序放进去;
  - /health 给验收脚本看.

不做 (与 plaza_relay.py 的取舍一致):
  - 跑 AI、存聊天 (说过的转完就忘)、落盘 (街区只在内存里, 进程一退就没了)、公开大厅 / 房间列表、
    按名字找人、语音、跨机 (只听 127.0.0.1).
  - 不做 Genesis / UE5 / 300 机器人档: 这是 Phase 0「一个体素房间 + 4-8 人」的本机原型.

协议 (JSON 文本帧, 每帧 <= 4 KB; 上行可带 rid, 回执带回去):
  上行  hello {name, code}                  必须是第一帧; code 空 = 新建街区
        input {move: [dx, dz], yaw}         10 Hz 发; dx/dz in [-1, 1], 长度 > 1 会归一化
        place {x, y, z, mat, rid} / remove {x, y, z, rid}
        say {text} / portal {rid} / ping
  下行  welcome {you, code, grid, max, tick_hz, room}
        snapshot {room, version, blocks: [[x,y,z,mat]], players: [...]}
        patch {version, set: [[x,y,z,mat]], del: [[x,y,z]]}      只看得到 AOI 内的
        pos {p: [[id, x, z, yaw]]}                              10 Hz
        ev {k: entered|left|said|place|remove|portal, id, name, pos, text?, block?}
        queued {position, of}
        ack {rid, ok, why} / err {rid, why} / pong

边界: 房间真值是 wm/world/voxel.py (纯 Python, 可单测); 这里只做网络、tick、AOI 下发与排队.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

ROOT_HINT = "G:\\RunProgram\\WorldModel_v1"
sys.path.insert(0, ROOT_HINT)

from wm.world import voxel as V  # noqa: E402

HOST = "127.0.0.1"  # 只听本机, 没有别的选项
PORT = 8802
TICK_HZ = 20
POS_HZ = 10           # 位置帧 10 Hz (tick 的两倍一次)
MAX_FRAME = 4096
MAX_SAY = 200
MAX_NAME = 16
MAX_WORLDS = 8
MAX_MEMBERS = V.MAX_PLAYERS
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 0/O/1/I
WORLD_ROOMS = ("garden", "street")               # 一座街区的两个房间
PORTAL_SPAWN = {"garden": (8.0, 13.0), "street": (8.0, 13.0)}
PARTNER = {"garden": "street", "street": "garden"}

_CTL = re.compile("[" + "".join(chr(c) for c in [*range(0x20), 0x7f, *range(0x200b, 0x2010), *range(0x202a, 0x202f), *range(0x2060, 0x2065), *range(0x2066, 0x206a), 0xfeff]) + "]")
_SPACES = re.compile(r"\s+")


def _line(text, limit: int) -> str:
    """名字和说的话: 去控制字符、零宽字符, 压成一行."""
    return _SPACES.sub(" ", _CTL.sub(" ", str(text or ""))).strip()[:limit]


def _p95(values: list[float]) -> float:
    """95 分位 (线性插值); 空列表返回 0."""
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * 0.95
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def _origin_ok(origin: str | None) -> bool:
    """本机原型: 允许浏览器 (file:// 的 Origin 是 "null" 或没有头; http://127.0.0.1 起头的也放行).

    只监听 127.0.0.1, 所以「放行本机网页」不扩大攻击面; 局域网连不进来.
    """
    if not origin:
        return True
    o = origin.strip().lower()
    if o in ("null", "file://"):
        return True
    return o.startswith("http://127.0.0.1") or o.startswith("http://localhost")


@dataclass(eq=False)          # eq=False: 靠 id 判等/可哈希, 连接才能放进 set
class _Conn:
    ws: web.WebSocketResponse
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pid: str = ""
    name: str = ""
    world: "_World | None" = None
    room: str = "garden"
    want: tuple[float, float] = (0.0, 0.0)
    yaw: float = 0.0
    closed: bool = False

    def send(self, frame: dict) -> None:
        if not self.closed:
            self.queue.put_nowait(frame)


@dataclass
class _World:
    """一座街区: 两个房间 + 这个邀请码下的所有连接 + 排队."""
    code: str
    rooms: dict = field(default_factory=lambda: {r: V.VoxelWorld(r) for r in WORLD_ROOMS})
    conns: dict = field(default_factory=dict)   # pid -> _Conn
    waiting: list = field(default_factory=list)  # [_Conn] 排队中
    counter: int = 0

    def room(self, name: str) -> V.VoxelWorld:
        return self.rooms[name]

    def near(self, c: _Conn) -> list[_Conn]:
        """和 c 在同一个房间、且 chunk 距离 <= 退出阈值的人 (说话/事件用这个范围)."""
        w = self.room(c.room)
        me = w.player(c.pid)
        if me is None:
            return []
        mine = V.chunk_of(me.pos[0], me.pos[1])
        out = []
        for other in self.conns.values():
            if other is c or other.room != c.room or not other.pid:
                continue
            p = w.player(other.pid)
            if p is None:
                continue
            if V.chunk_dist(mine, V.chunk_of(p.pos[0], p.pos[1])) <= V.CHUNK_EXIT:
                out.append(other)
        return out


class Relay:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.worlds: dict[str, _World] = {}
        self.conns: set[_Conn] = set()
        self.runner: web.AppRunner | None = None
        self.port = PORT
        self._tick_task: asyncio.Task | None = None
        self._tick = 0
        self.frames = 0          # 验收用: 一共下发多少帧
        self.bytes_out = 0       # 验收用: 一共下发多少字节
        self.tick_ms: list[float] = []   # 验收用: 每个 tick 的真实耗时 (ms), D11 的 tick p95 就看它

    # -- 起停 ------------------------------------------------------------------
    async def start(self, port: int = 0) -> int:
        app = web.Application()
        app.router.add_get("/ws", self.handle)
        app.router.add_get("/health", self.health)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, HOST, port).start()
        self.port = self.runner.addresses[0][1]
        self._tick_task = asyncio.create_task(self._tick_loop())
        return self.port

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        for c in list(self.conns):
            c.closed = True
            with contextlib.suppress(Exception):
                await c.ws.close(code=1001, message=b"relay stopping")
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def health(self, _request: web.Request) -> web.Response:
        players = sum(len(w.conns) for w in self.worlds.values())
        return web.json_response({"ok": True, "worlds": len(self.worlds), "players": players,
                                  "tick_hz": TICK_HZ, "frames": self.frames, "bytes_out": self.bytes_out,
                                  "ticks": len(self.tick_ms), "tick_ms_p95": _p95(self.tick_ms),
                                  "tick_ms_max": round(max(self.tick_ms), 2) if self.tick_ms else 0.0})

    # -- 连接 ------------------------------------------------------------------
    async def handle(self, request: web.Request):
        if not _origin_ok(request.headers.get("Origin")):
            return web.Response(status=403, text="不接受这个来源的网页连接")
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=20)
        await ws.prepare(request)
        c = _Conn(ws=ws)
        self.conns.add(c)
        writer = asyncio.create_task(self._writer(c))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    f = json.loads(msg.data)
                except ValueError:
                    f = None
                if not isinstance(f, dict):
                    c.send({"t": "err", "why": "bad_json"})
                    continue
                if not c.pid:
                    await self._hello(c, f)
                    continue
                await self._dispatch(c, f)
        finally:
            c.closed = True
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
            self.conns.discard(c)
            self._leave(c)
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()
        return ws

    async def _writer(self, c: _Conn) -> None:
        while not c.closed:
            frame = await c.queue.get()
            try:
                data = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                continue
            if c.closed or c.ws.closed:
                return
            try:
                await c.ws.send_str(data)
            except Exception:
                return
            self.frames += 1
            self.bytes_out += len(data.encode("utf-8"))

    async def _hello(self, c: _Conn, f: dict) -> None:
        def err(why: str) -> None:
            c.send({"t": "err", "rid": f.get("rid"), "why": why})

        if f.get("t") != "hello":
            return err("hello_first")
        name = _line(f.get("name"), MAX_NAME)
        if not name:
            return err("bad_name")
        code = _line(f.get("code"), 8).upper()
        if code:
            world = self.worlds.get(code)
            if world is None:
                return err("bad_code")
        else:
            if len(self.worlds) >= MAX_WORLDS:
                return err("too_many_worlds")
            code = self._new_code()
            world = self.worlds[code] = _World(code)
        c.name = name
        c.world = world
        c.room = "garden"
        if self._full(world):
            c.pid = "waiting"       # 占位, tick 里会尝试放进去
            world.waiting.append(c)
            c.send({"t": "queued", "position": len(world.waiting), "of": MAX_MEMBERS})
            return
        self._admit(c, world, "garden")

    def _full(self, world: _World) -> bool:
        return all(len([x for x in world.conns.values() if x.room == r]) >= MAX_MEMBERS for r in WORLD_ROOMS)

    def _admit(self, c: _Conn, world: _World, room: str) -> None:
        world.counter += 1
        c.pid = f"p{world.counter}"
        c.room = room
        w = world.room(room)
        try:
            p = w.add_player(c.pid, c.name, PORTAL_SPAWN.get(room))
        except V.VoxelError as e:
            # 出生点被方块占了 (有人在那儿放过东西): 退回世界默认找空位的逻辑
            try:
                p = w.add_player(c.pid, c.name)
            except V.VoxelError as e2:
                c.pid = ""
                c.send({"t": "err", "why": str(e2)})
                return
        w.update_interest(c.pid)
        world.conns[c.pid] = c
        c.send({"t": "welcome", "you": {"id": p.id, "name": p.name, "pos": list(p.pos), "yaw": p.yaw},
                "code": world.code, "grid": {"w": V.GRID, "h": V.GRID}, "max": MAX_MEMBERS,
                "tick_hz": TICK_HZ, "room": room})
        c.send({"t": "snapshot", **w.snapshot()})
        # entered 事件由世界 add_player 发出, 别人下一 tick 从 events_since 收到; 不要再手工广播一遍

    def _leave(self, c: _Conn) -> None:
        if not c.pid or c.pid == "waiting":
            return
        world = c.world
        if world is None:
            return
        w = world.room(c.room)
        w.remove_player(c.pid)          # 世界 emit left; 手工广播会重复
        world.conns.pop(c.pid, None)
        if not world.conns and not world.waiting:
            self.worlds.pop(world.code, None)
            return
        self._admit_next(world)
        c.pid = ""

    def _admit_next(self, world: _World) -> None:
        while world.waiting:
            c = world.waiting.pop(0)
            if c.closed:
                continue
            for room in WORLD_ROOMS:
                if len([x for x in world.conns.values() if x.room == room]) < MAX_MEMBERS:
                    self._admit(c, world, room)
                    return
            world.waiting.insert(0, c)
            return

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_CHARS) for _ in range(6))
            if code not in self.worlds:
                return code

    # -- 上行帧 ----------------------------------------------------------------
    async def _dispatch(self, c: _Conn, f: dict) -> None:
        t = f.get("t")
        rid = f.get("rid")
        world = c.world
        if world is None or c.pid == "waiting":
            return
        if t == "input":
            mv = f.get("move")
            if isinstance(mv, (list, tuple)) and len(mv) == 2:
                try:
                    dx, dz = float(mv[0]), float(mv[1])
                except (TypeError, ValueError):
                    dx, dz = 0.0, 0.0
                c.want = (max(-1.0, min(1.0, dx)), max(-1.0, min(1.0, dz)))
            yaw = f.get("yaw")
            if isinstance(yaw, (int, float)):
                c.yaw = float(yaw)
            w = world.room(c.room)
            w.set_yaw(c.pid, c.yaw)
        elif t in ("place", "remove"):
            w = world.room(c.room)
            try:
                x, y, z = int(f.get("x")), int(f.get("y")), int(f.get("z"))
            except (TypeError, ValueError):
                c.send({"t": "ack", "rid": rid, "ok": False, "why": "bad_coords"})
                return
            if t == "place":
                mat = int(f.get("mat") or 0)
                ok, why = w.place(c.pid, x, y, z, mat)
                block = [x, y, z, mat if ok else 0]
            else:
                mat = w.block_at(x, y, z)          # 先记下真材质 —— 拆掉之后就问不到了
                ok, why = w.remove(c.pid, x, y, z)
                block = [x, y, z, mat if (ok and mat is not None) else 0]
            # 事件由世界 events_since 统一下发 (place/remove 世界会自己 emit), 这里只回执
            c.send({"t": "ack", "rid": rid, "ok": ok, "why": why, "block": block})
        elif t == "say":
            text = _line(f.get("text"), MAX_SAY)
            if not text:
                return
            try:
                world.room(c.room).say(c.pid, text)   # 世界 emit said, tick 里按 AOI 下发
            except V.VoxelError:
                c.send({"t": "ack", "rid": rid, "ok": False, "why": "说不了话"})
        elif t == "portal":
            self._portal(c, rid)
        elif t == "ping":
            c.send({"t": "pong"})
        else:
            c.send({"t": "err", "rid": rid, "why": "unknown_type"})

    def _portal(self, c: _Conn, rid) -> None:
        world = c.world
        if world is None:
            return
        old_room, new_room = c.room, PARTNER[c.room]
        if len([x for x in world.conns.values() if x.room == new_room]) >= MAX_MEMBERS:
            c.send({"t": "ack", "rid": rid, "ok": False, "why": "对面满员了"})
            return
        old = world.room(old_room)
        old.remove_player(c.pid)        # 世界 emit left (旧房间), 别人下一 tick 收到
        world.conns.pop(c.pid, None)
        world.counter += 1
        c.pid = f"p{world.counter}"
        c.room = new_room
        w = world.room(new_room)
        try:
            p = w.add_player(c.pid, c.name, PORTAL_SPAWN.get(new_room))
        except V.VoxelError:
            p = w.add_player(c.pid, c.name)     # 对面出生点被方块占了
        w.update_interest(c.pid)
        world.conns[c.pid] = c
        c.send({"t": "ack", "rid": rid, "ok": True, "why": "", "pos": list(p.pos)})
        c.send({"t": "snapshot", **w.snapshot()})
        # entered 由世界 emit; portal 世界不知道, 只广播这一个
        self._broadcast_ev(world, new_room, {"k": "portal", "id": c.pid, "name": c.name, "pos": list(p.pos)},
                           except_pid=c.pid)

    def _broadcast_ev(self, world: _World, room: str, ev: dict, *, except_pid: str = "") -> None:
        """把一个事件发给同一房间里 chunk 距离够近的人 (自己除外)."""
        base = dict(ev)
        base.setdefault("t", "ev")
        for other in list(world.conns.values()):
            if other.pid in ("", "waiting") or other.room != room or other.pid == except_pid:
                continue
            mine = world.room(other.room).player(other.pid)
            if mine is None:
                continue
            pos = ev.get("pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                if V.chunk_dist(V.chunk_of(mine.pos[0], mine.pos[1]),
                                V.chunk_of(float(pos[0]), float(pos[1]))) > V.CHUNK_EXIT:
                    continue
            other.send(base)

    # -- tick ------------------------------------------------------------------
    async def _tick_loop(self) -> None:
        dt = 1.0 / TICK_HZ
        pos_every = max(1, TICK_HZ // POS_HZ)
        next_at = time.perf_counter()
        while True:
            # 按「目标时刻」排, 而不是每轮 sleep(dt): Windows 上 asyncio.sleep(0.05) 实际会睡成 ~62 ms,
            # 累积下来 tick 只有 16 Hz, 位置帧跟着掉到 8 Hz.
            next_at += dt
            delay = next_at - time.perf_counter()
            await asyncio.sleep(delay if delay > 0 else 0.0)
            if delay < -0.2:                 # 落后太多就重新对表, 免得越积越多
                next_at = time.perf_counter()
            self._tick += 1
            t_begin = time.perf_counter()
            for world in list(self.worlds.values()):
                for c in list(world.conns.values()):
                    if c.closed or not c.pid:
                        continue
                    w = world.room(c.room)
                    try:
                        w.move(c.pid, c.want[0], c.want[1], dt)
                    except Exception:
                        continue
                for c in list(world.conns.values()):
                    if c.closed or not c.pid:
                        continue
                    w = world.room(c.room)
                    try:
                        w.update_interest(c.pid)
                        patch = w.patch(c.pid)
                        if patch.get("set") or patch.get("del"):
                            c.send({"t": "patch", **patch})
                        for ev in w.events_since(c.pid):
                            # 自己做的 place/remove/said 不回放: 客户端已经通过 ack / patch / 本地回声知道了,
                            # 再发一遍聊天会在界面上重复
                            if ev.get("id") == c.pid and ev.get("k") in ("place", "remove", "said"):
                                continue
                            c.send({**ev, "t": "ev"})
                        if self._tick % pos_every == 0:
                            ps = [[p["id"], round(p["pos"][0], 2), round(p["pos"][1], 2), round(p.get("yaw", 0.0), 2)]
                                  for p in w.nearby(c.pid)]
                            me = w.player(c.pid)
                            if me is not None:
                                ps.append([c.pid, round(me.pos[0], 2), round(me.pos[1], 2), round(c.yaw, 2)])
                            c.send({"t": "pos", "p": ps})
                    except V.VoxelError:
                        continue
            self.tick_ms.append((time.perf_counter() - t_begin) * 1000.0)
            if len(self.tick_ms) > 2400:      # 只留最近两分钟, 免得内存一直涨
                del self.tick_ms[:1200]


async def _amain(port: int) -> None:
    relay = Relay()
    real = await relay.start(port)
    print(f"体素中继已就绪: ws://{HOST}:{real}/ws  (tick {TICK_HZ} Hz, 每房间最多 {MAX_MEMBERS} 人)", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await relay.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="体素街区中继 (本机原型)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
