"""体素街区的带宽基准 (路线图 D11 的本机版): 一个中继 + N 个假客户端, 量每人 字节/秒 与帧间隔 p95.

做什么:
  - 在本进程里起 `tools/voxel_relay.py` 的 Relay (不用另开窗口), 然后开 N 个 aiohttp 客户端连上去;
  - 每个客户端 10 Hz 发 input (随机小步走), 6% 的概率放一块方块, 一次性说一句话;
  - 统计每个客户端的: 收字节数 / 收帧数 / 按帧类型分的字节数 / 帧间隔 p95;
  - 输出 `docs/world_wire-<时间>.json` 并打印一张表, 最后给一句「在不在预算内」的判断.

口径 (照 Hemifuture §16.3 的带宽闭环):
  - 预算: 每玩家上行+下行 <= 20 KB/s (20 * 1024 B/s);
  - 服务端 tick p95: 一个 tick 的处理耗时 95 分位, 目标 < 50 ms (D11 验收项);
  - 明确写「不覆盖 300 机器人档」: 本机单进程 + 8 人, 是 Phase 0 的「一个体素房间 + 4-8 人」档.

不做: 不做 300 机器人、不做跨机、不测 GPU/渲染、不写任何生产配置.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import statistics
import sys
import time
from pathlib import Path

from aiohttp import ClientSession, WSMsgType

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import voxel_relay as R  # noqa: E402


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(0.95 * (len(s) - 1))))
    return s[idx]


class FakeClient:
    def __init__(self, name: str, *, seed: int):
        self.name = name
        self.rng = random.Random(seed)
        self.bytes_in = 0
        self.bytes_out = 0
        self.frames = 0
        self.by_type: dict[str, int] = {}
        self.intervals: list[float] = []
        self.code = ""
        self.own = (8.0, 8.0)
        self.pos_frames = 0
        self.welcome = None
        self.snapshot_blocks: int | None = None
        self.queued = False
        self.errors: list[str] = []

    async def run(self, host: str, port: int, seconds: float, code: str, stop: asyncio.Event) -> None:
        async with ClientSession() as sess:
            async with sess.ws_connect(f"ws://{host}:{port}/ws", max_msg_size=0) as ws:
                await self._send(ws, {"t": "hello", "name": self.name, "code": code})
                t_end = time.monotonic() + seconds
                last = None
                last_input = 0.0
                while not stop.is_set() and time.monotonic() < t_end:
                    now = time.monotonic()
                    if now - last_input >= 0.1:
                        last_input = now
                        await self._send(ws, {"t": "input", "move": [self.rng.uniform(-1, 1), self.rng.uniform(-1, 1)],
                                              "yaw": self.rng.uniform(0, 6.28)})
                        if self.rng.random() < 0.06:
                            x = self.rng.randrange(0, R.V.GRID)
                            z = self.rng.randrange(0, R.V.GRID)
                            y = self.rng.randrange(1, 3)
                            await self._send(ws, {"t": "place", "x": x, "y": y, "z": z,
                                                  "mat": self.rng.randrange(0, 4), "rid": 1})
                    try:
                        msg = await ws.receive(timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if msg.type != WSMsgType.TEXT:
                        break
                    raw = msg.data
                    t = time.monotonic()
                    self.bytes_in += len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                    self.frames += 1
                    if last is not None:
                        self.intervals.append((t - last) * 1000.0)
                    last = t
                    try:
                        f = json.loads(raw)
                    except ValueError:
                        continue
                    ty = str(f.get("t") or "?")
                    self.by_type[ty] = self.by_type.get(ty, 0) + len(raw)
                    if ty == "welcome":
                        self.welcome = f
                        self.code = f.get("code", "")
                    elif ty == "snapshot" and self.snapshot_blocks is None:
                        self.snapshot_blocks = len(f.get("blocks") or [])
                    elif ty == "pos":
                        self.pos_frames += 1
                        for p in f.get("p") or []:
                            if isinstance(p, list) and len(p) >= 3 and p[0] in ("you", getattr(self, "pid", None)):
                                self.own = (p[1], p[2])
                    elif ty == "queued":
                        self.queued = True
                    elif ty == "err":
                        self.errors.append(str(f.get("why")))

    async def _send(self, ws, frame: dict) -> None:
        data = json.dumps(frame, separators=(",", ":"))
        self.bytes_out += len(data.encode("utf-8"))
        await ws.send_str(data)


async def run_bench(clients: int, seconds: float, port: int) -> dict:
    relay = R.Relay()
    real_port = await relay.start(port)
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    made: list[FakeClient] = []
    try:
        t0 = time.monotonic()
        # 第一个客户端建房, 其余等它拿到 code 再进
        first = FakeClient("bot-01", seed=1)
        made.append(first)
        tasks.append(asyncio.create_task(first.run(R.HOST, real_port, seconds, "", stop)))
        waited = 0.0
        while not first.code and waited < 5.0:
            await asyncio.sleep(0.05)
            waited += 0.05
        code = first.code
        for i in range(2, clients + 1):
            c = FakeClient(f"bot-{i:02d}", seed=i)
            made.append(c)
            tasks.append(asyncio.create_task(c.run(R.HOST, real_port, seconds, code, stop)))
        await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = max(0.1, time.monotonic() - t0)
        server_ticks = list(relay.tick_ms)      # 服务端每个 tick 的真实耗时 (D11 的 tick p95)
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await relay.stop()

    rows = []
    for c in made:
        rows.append({
            "name": c.name,
            "bytes_in": c.bytes_in,
            "bytes_out": c.bytes_out,
            "bytes_total_s": round((c.bytes_in + c.bytes_out) / elapsed, 1),
            "frames": c.frames,
            "frames_s": round(c.frames / elapsed, 1),
            "pos_frames": c.pos_frames,
            "frame_gap_p95_ms": round(p95(c.intervals), 1),
            "by_type": {k: v for k, v in sorted(c.by_type.items(), key=lambda kv: -kv[1])},
            "queued": c.queued,
            "errors": c.errors,
        })
    per_client = [r["bytes_total_s"] for r in rows]
    gaps = [r["frame_gap_p95_ms"] for r in rows if r["frame_gap_p95_ms"]]
    summary = {
        "clients": clients,
        "seconds": round(elapsed, 2),
        "port": real_port,
        "code": code,
        "bytes_per_client_s": round(statistics.mean(per_client), 1) if per_client else 0.0,
        "bytes_per_client_s_max": max(per_client) if per_client else 0.0,
        # 口径: tick_p95_ms = 服务端一个 tick 的处理耗时 p95 (D11 的验收项, 目标 < 50 ms);
        #       frame_gap_p95_ms = 客户端收到的帧之间隔 p95 (10 Hz 位置帧下理论 100 ms, 看抖动)
        "tick_p95_ms": round(p95(server_ticks), 1),
        "tick_max_ms": round(max(server_ticks), 1) if server_ticks else 0.0,
        "ticks": len(server_ticks),
        "frame_gap_p95_ms": round(statistics.mean(gaps), 1) if gaps else 0.0,
        "frames_per_client_s": round(statistics.mean([r["frames_s"] for r in rows]), 1) if rows else 0.0,
        "budget_bytes_s": 20 * 1024,
        "within_budget": bool(per_client) and max(per_client) <= 20 * 1024,
        "note": "本机 8 人档 (Phase 0 的一个体素房间), 不覆盖 300 机器人档",
        "rows": rows,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="体素街区带宽基准 (本机档)")
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--port", type=int, default=0, help="0 = 随机端口")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    summary = asyncio.run(run_bench(max(2, args.clients), max(2.0, args.seconds), args.port))
    out = Path(args.out) if args.out else ROOT / "docs" / time.strftime("world_wire-%Y%m%d-%H%M%S.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"clients={summary['clients']}  seconds={summary['seconds']}  port={summary['port']}")
    print(f"{'客户端':<8}{'字节/秒':>9}{'帧/秒':>8}{'pos/秒':>8}{'帧距p95':>10}")
    for r in summary["rows"]:
        print(f"{r['name']:<8}{r['bytes_total_s']:>9.1f}{r['frames_s']:>8.1f}"
              f"{r['pos_frames'] / max(summary['seconds'], 0.1):>8.1f}{r['frame_gap_p95_ms']:>10.1f}")
    print(f"中位/均值 字节/人/秒 = {summary['bytes_per_client_s']}  (最大 {summary['bytes_per_client_s_max']})")
    print(f"服务端 tick p95 = {summary['tick_p95_ms']} ms (max {summary['tick_max_ms']}, {summary['ticks']} ticks)"
          f"   客户端帧距 p95 = {summary['frame_gap_p95_ms']} ms   预算 = {summary['budget_bytes_s']} B/s")
    print("结论: " + ("在预算内" if summary["within_budget"] else "超预算, 需要先改增量协议"))
    print(f"已写 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
