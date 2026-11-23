"""双胞胎夹具的离线运行器. 样板: 主程序不引用, 宿主第 1 步可以替换. 契约 perception.twin_fixtures; 记忆 cognition-truth-split.

夹具 (JSON): 两个世界 A、B 只差一些事. 把同一条时间线的原始中继帧分别喂给两个世界, 每个世界都走完整的一条路:
  感知门 (Perception) -> 包封 (make_envelope) -> 世界会话 (WorldSession, 确定的假模型)
然后逐步比较: 她收到的事件、发给模型的整段提示、她要做的动作.
  expect = "same":   差在她感知不到的地方 -> 每一步都必须完全相同
  expect = "differ": 差在她感知得到的地方 -> 至少有一步不同
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

from .envelope import Audit, WorldSpec, make_envelope
from .session import WorldSession


def load_perception(plugin_dir):
    """按路径载入插件目录里的感知门 (插件目录名带连字符, 不能直接 import)."""
    path = Path(plugin_dir) / "plaza_perception.py"
    spec = importlib.util.spec_from_file_location("plaza_perception_" + str(abs(hash(str(path)))), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Perception


class PolicyLLM:
    """确定的假模型: 回什么只取决于最后一条用户消息的内容 (同样的提示 -> 同样的动作)."""

    async def stream(self, messages, schema=None, max_tokens=0, id_slot=None, meta=None):
        yield json.dumps({"actions": self.decide(messages[-1]["content"])}, ensure_ascii=False)

    @staticmethod
    def decide(user: str) -> list:
        acts: list = []
        for line in user.splitlines():
            if not line.startswith("[#"):
                continue
            head, _, body = line.partition("] ")
            kind = head.split(" ")[-1]
            kv = dict(p.split("=", 1) for p in body.split(":", 1)[0].split() if "=" in p)
            if kind == "pet_said":
                said = body.split(": ", 1)[1] if ": " in body else ""
                acts.append({"name": "plaza_say", "args": {"text": "听到啦：" + said[:12]}})
            elif kind == "pet_entered" and kv.get("distance_bucket") in ("near", "mid"):
                acts.append({"name": "plaza_emote", "args": {"emote": "wave"}})
            elif kind in ("pet_entered", "pet_moved") and kv.get("direction") in ("left", "right", "up", "down") \
                    and kv.get("distance_bucket") != "near":
                acts.append({"name": "plaza_move", "args": {"direction": kv["direction"]}})
        return acts[:2]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@dataclass
class Step:
    note: str
    events: list
    prompt: str | None
    actions: list


@dataclass
class TwinReport:
    name: str
    expect: str
    steps_a: list = field(default_factory=list)
    steps_b: list = field(default_factory=list)
    diffs: list = field(default_factory=list)   # 不同的步的序号

    @property
    def ok(self) -> bool:
        return (not self.diffs) if self.expect == "same" else bool(self.diffs)


async def _run_world(fixture: dict, variant: str, spec: WorldSpec, perception_factory, llm_factory) -> list[Step]:
    clock = FakeClock()
    ids = iter(range(1, 10_000))
    session = WorldSession(spec, fixture["me"]["name"], llm_factory(), clock=clock, audit=Audit(),
                           boundary=lambda: f"{next(ids):06d}", batch_s=0.4)
    perception = perception_factory()
    audit, seq = session.audit, 0
    steps: list[Step] = []

    async def feed(frame: dict, note: str) -> None:
        nonlocal seq
        events = perception.feed(frame)
        for e in events:
            seq += 1
            env = make_envelope(spec, e, seq, audit, now=clock.now)
            if env is not None:
                session.push(env)
        clock.now += 1.0   # 过 1 秒: 合批的事件到点了
        before = len(session.trace)
        actions = await session.step()
        prompt = session.trace[-1]["user"] if len(session.trace) > before else None
        steps.append(Step(note, [(e["type"], e["fields"], e["text"]) for e in events], prompt,
                          [(a.name, a.args, a.blocked) for a in actions]))

    me = fixture["me"]
    await feed({"t": "joined", "code": "TWIN0001", "you": {"id": me["id"], "name": me["name"], "pos": me["pos"]},
                "members": fixture["variants"][variant]["others"], "grid": fixture.get("grid", {"w": 24, "h": 14})}, "进房间")
    for item in fixture["timeline"]:
        if variant in item.get("only", ["A", "B"]):
            await feed(item["frame"], item.get("note", ""))
    return steps


def run_twin(fixture: dict, spec: WorldSpec, perception_factory, llm_factory=PolicyLLM) -> TwinReport:
    async def both():
        return (await _run_world(fixture, "A", spec, perception_factory, llm_factory),
                await _run_world(fixture, "B", spec, perception_factory, llm_factory))

    a, b = asyncio.run(both())
    return TwinReport(fixture.get("name", ""), fixture.get("expect", "same"), a, b, _diff(a, b))


def _diff(a: list[Step], b: list[Step]) -> list[int]:
    """两个世界各自走完的步序列, 去掉"什么都没发生"的步 (她感知不到的帧不产生事件、不问模型、没有动作), 剩下的必须逐条相同.
    A、B 的时间线里单边的帧让那一边多走几个空步, 所以不能按下标对齐, 要先去空步."""
    def flat(steps):
        return [(s.events, s.prompt, s.actions) for s in steps if s.events or s.prompt is not None or s.actions]
    fa, fb = flat(a), flat(b)
    return [i for i in range(max(len(fa), len(fb))) if i >= len(fa) or i >= len(fb) or fa[i] != fb[i]]
