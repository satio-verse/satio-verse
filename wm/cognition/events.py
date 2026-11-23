"""事件流: 她收到的每一份观察 (obs), 都按顺序编号记下来, 存成 JSONL. 认知 (belief.py) 只从这串事件算出来,
所以拿同一串事件重放 (Cognition.replay) 得到的认知和在线时一模一样 —— 回放、复现实验、查"她为什么这么以为"都靠它.

一条: {"seq": 序号, "t": 世界时间 (秒), "world": 哪个世界 (小窝 room / 以后的现实镜头 real), "kind": "obs", "data": 观察}.
世界分开记: 现实镜头里的杯子和小窝里的杯子不是同一个东西, 以后不能共用位置.
"""
from __future__ import annotations

import json
from pathlib import Path


class EventLog:
    def __init__(self, world: str = "room"):
        self.world = world
        self.events: list[dict] = []

    def add(self, kind: str, data: dict) -> dict:
        ev = {"seq": len(self.events), "t": data.get("t"), "world": self.world, "kind": kind, "data": data}
        self.events.append(ev)
        return ev

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="\n") as f:
            for ev in self.events:
                f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
        return p

    @staticmethod
    def load(path: str | Path) -> list[dict]:
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
