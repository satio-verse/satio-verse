"""体素中继的功能冒烟 (不是带宽基准, 那个是 bench_world_wire.py).

两个真 websocket 客户端走一遍: 建房 → 第二个人进 → 放方块 → 说话 → 传送 → 拆方块,
检查「事件不重不漏 + 回执里的材质是真材质 + 传送真的换房间」.

    python tools/voxel_smoke.py            # 随机端口, 起一个中继
    python tools/voxel_smoke.py --port 8802 --keep   # 连已经在跑的中继, 不自己起

输出: docs/voxel_smoke-<时间>.json + 控制台一张表; 有失败时退出码 1.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import voxel_relay as R  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError):
        _s.reconfigure(encoding="utf-8", errors="replace")


class Client:
    """一个假客户端: 只收帧、按类型记下来, 不做预测/插值."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.frames: list[dict] = []
        self.welcome: dict | None = None

    async def open(self, session: aiohttp.ClientSession, url: str, code: str = "") -> dict:
        self.ws = await session.ws_connect(url)
        await self.ws.send_json({"t": "hello", "name": self.name, "code": code})
        # 等到 welcome (或 err), 顺手把之前的帧都收下
        for _ in range(200):
            try:
                msg = await self.ws.receive(timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            f = json.loads(msg.data)
            self.frames.append(f)
            if f.get("t") == "welcome":
                self.welcome = f
                return f
            if f.get("t") == "err":
                raise RuntimeError(f"{self.name} 进不去: {f.get('why')}")
        raise RuntimeError(f"{self.name} 等不到 welcome")

    async def pump(self, seconds: float = 0.45) -> list[dict]:
        """再收一会儿帧, 返回这次新收到的."""
        out: list[dict] = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                msg = await self.ws.receive(timeout=0.05)
            except asyncio.TimeoutError:
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            f = json.loads(msg.data)
            self.frames.append(f)
            out.append(f)
        return out

    async def send(self, frame: dict, *, seconds: float = 0.35) -> list[dict]:
        await self.ws.send_json(frame)
        return await self.pump(seconds)

    def evs(self, frames: list[dict] | None = None, k: str | None = None) -> list[dict]:
        src = self.frames if frames is None else frames
        return [f for f in src if f.get("t") == "ev" and (k is None or f.get("k") == k)]

    async def close(self) -> None:
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()


def check(name: str, ok: bool, detail) -> dict:
    row = {"check": name, "ok": bool(ok), "detail": detail}
    print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return row


async def run(port: int, keep: bool) -> dict:
    relay = None
    if keep:
        real_port = port
    else:
        relay = R.Relay()
        real_port = await relay.start(port)
    url = f"ws://{R.HOST}:{real_port}/ws"
    rows: list[dict] = []
    a, b = Client("甲"), Client("乙")
    try:
        async with aiohttp.ClientSession() as session:
            # 1) 建房
            wa = await a.open(session, url, "")
            code = wa.get("code", "")
            rows.append(check("建房拿到房间码", bool(code), {"code": code, "room": wa.get("room")}))
            rows.append(check("welcome 带自己的 id/出生点", bool(wa.get("you", {}).get("id")),
                              {"you": wa.get("you")}))
            pid_a = wa["you"]["id"]

            # 2) 第二个人用同一个码进来
            wb = await b.open(session, url, code)
            pid_b = wb["you"]["id"]
            got_a = await a.pump()
            enters = [e for e in a.evs(got_a, "entered") if e.get("id") == pid_b]
            rows.append(check("甲只收到 1 条「乙进来」事件", len(enters) == 1, {"count": len(enters)}))
            rows.append(check("两人 id 不同", pid_a != pid_b, {"甲": pid_a, "乙": pid_b}))

            # 3) 甲在自己旁边放一块 (出生点 (8,13), 放 (8,1,10) 距离 3, 不会压到人)
            acks = await a.send({"t": "place", "x": 8, "y": 1, "z": 10, "mat": 2, "rid": 101})
            ack = next((f for f in acks if f.get("t") == "ack" and f.get("rid") == 101), None)
            rows.append(check("放方块回执 ok", bool(ack and ack.get("ok")), ack))
            rows.append(check("回执材质就是放的材质", bool(ack and ack.get("block", [None] * 4)[3] == 2),
                              ack.get("block") if ack else None))

            got_b = await b.pump()
            places = [e for e in b.evs(got_b, "place")]
            rows.append(check("乙只收到 1 条 place 事件", len(places) == 1, {"count": len(places)}))
            b_acks = [f for f in b.frames if f.get("t") == "patch"]
            rows.append(check("乙通过 patch 知道了这块", any([8, 1, 10, 2] in p.get("set", []) for p in b_acks),
                              {"patches": len(b_acks)}))

            # 4) 甲说话
            got_a2 = await a.send({"t": "say", "text": "你好呀"})
            got_b2 = await b.pump()
            said_b = [e for e in b.evs(got_b2, "said") if e.get("text") == "你好呀"]
            rows.append(check("乙只收到 1 条 said 事件", len(said_b) == 1, {"count": len(said_b)}))
            said_a = [e for e in a.evs(got_a2, "said")]
            rows.append(check("说话的人不会收到自己的 said", len(said_a) == 0, {"count": len(said_a)}))

            # 5) 甲拆那块, 回执要带真材质
            acks = await a.send({"t": "remove", "x": 8, "y": 1, "z": 10, "rid": 102})
            ack = next((f for f in acks if f.get("t") == "ack" and f.get("rid") == 102), None)
            mat = ack.get("block", [None] * 4)[3] if ack else None
            rows.append(check("拆方块回执 ok 且带回真材质 2", bool(ack and ack.get("ok") and mat == 2),
                              {"ack": ack}))

            # 6) 乙传送去对面
            got_a3 = await b.send({"t": "portal", "rid": 201})
            ack = next((f for f in got_a3 if f.get("t") == "ack" and f.get("rid") == 201), None)
            rows.append(check("传送回执 ok", bool(ack and ack.get("ok")), ack))
            snaps = [f for f in got_a3 if f.get("t") == "snapshot"]
            room_b = snaps[-1].get("room") if snaps else None
            rows.append(check("传送后收到新房间快照", room_b == "street", {"room": room_b}))
            got_a4 = await a.pump()
            lefts = [e for e in a.evs(got_a4, "left") if e.get("id") == pid_b]
            rows.append(check("甲只收到 1 条「乙离开」事件", len(lefts) == 1, {"count": len(lefts)}))

            # 7) 乙 ping → pong (链路还活着)
            got_b3 = await b.send({"t": "ping"})
            rows.append(check("传送后连接仍可用 (ping/pong)", any(f.get("t") == "pong" for f in got_b3),
                              {"frames": len(got_b3)}))
    finally:
        await a.close()
        await b.close()
        if relay is not None:
            with contextlib.suppress(Exception):
                await relay.stop()

    ok = all(r["ok"] for r in rows)
    summary = {
        "ok": ok,
        "passed": sum(1 for r in rows if r["ok"]),
        "total": len(rows),
        "port": real_port,
        "checks": rows,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="体素中继功能冒烟")
    ap.add_argument("--port", type=int, default=0, help="0 = 随机端口")
    ap.add_argument("--keep", action="store_true", help="连已经在跑的中继, 不自己起")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    summary = asyncio.run(run(args.port, args.keep))
    out = Path(args.out) if args.out else ROOT / "docs" / time.strftime("voxel_smoke-%Y%m%d-%H%M%S.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{summary['passed']}/{summary['total']} 项通过 -> {out}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
