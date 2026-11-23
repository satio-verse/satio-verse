"""她 (认知实验里的主体): 认知 + 经验 + 事件流 + 怎么找东西. 只收观察, 拿不到世界的真值 (这个文件不 import roomworld).

找东西的每一步 (step): 按认知列出还能去看的地方 (options) 排好序, 拼一份提示词 (prompt: 只用她的认知和经验;
阶段 2 交给模型挑, 现在按排序挑第一个), 给出一个动作. 每一步都记进 trace, 双胞胎测试逐条比对.
几种做法 (POLICIES):
  belief      最后看到的地方 -> 由近到远 (同一处里先台面后抽屉)
  experience  最后看到的地方 -> 经验里常找到的地方 -> 由近到远
  random      随机顺序找 (不管最后在哪看到; 找过的不再找)
  oracle      作弊: 直接看真值 (上界, 不是能用的成绩)
  truth_menu  作弊, 照桌宠现在的写法: 冲咖啡开头直接查杯子在不在咖啡机上 (人还没走过去), 不在就不做, 也不找
后两种要传 peek (读真值的函数), 是故意的: 双胞胎测试必须把它们抓出来, 证明检查有用.
找了 budget 步还没找到 (或者没地方可找了) 就问你一次, 你说在哪就去看.
"""
from __future__ import annotations

import random
from datetime import datetime

from . import layout as Y
from .belief import KNOWN, Cognition, Experience

POLICIES = ("belief", "experience", "random", "oracle", "truth_menu")
CHEATS = ("oracle", "truth_menu")
BUDGET = 16
REAL_T = 1e8  # 比这个大的时刻是真的钟 (桌宠小屋, 秒), 小的是实验里从第 1 天 0 点数的秒


def clock(t: float | None) -> str:
    if (t or 0) > REAL_T:
        return f"{datetime.fromtimestamp(t):%m-%d %H:%M}"
    d, s = divmod(int(t or 0), 86400)
    return f"第 {d + 1} 天 {s // 3600:02d}:{s % 3600 // 60:02d}"


class Mind:
    def __init__(self, policy: str = "experience", seed: int = 0, peek=None, world: str = "room", budget: int = BUDGET,
                 regions=None, trace: bool = True, layout=None):
        """regions: 这个环境里有的放东西的地方 (桌宠小屋还没有抽屉), 没给就是布局里全部; trace=False 不留比对用的逐条记录 (常驻的不留)."""
        if policy not in POLICIES:
            raise ValueError(f"没有这种做法: {policy}")
        if (policy in CHEATS) != (peek is not None):
            raise ValueError("作弊的对照 (oracle / truth_menu) 要传 peek, 别的做法不能传")
        self.layout = layout or Y.LEGACY
        self.policy, self.peek, self.budget = policy, peek, budget
        self.regions = [r for r in self.layout.REGIONS if regions is None or r in regions]
        self.cog = Cognition(world, layout=self.layout)
        self.rng = random.Random(seed)
        self.keep = trace
        self.trace: list[dict] = []  # 收到的观察、每次决定 (认知摘要、候选、提示词、动作)、说的话
        self.search: dict | None = None

    def _log(self, entry: dict) -> None:
        if self.keep:
            self.trace.append(entry)

    @property
    def belief(self):
        return self.cog.belief

    def perceive(self, obs: dict, act: list | None = None) -> list[tuple]:
        ch = self.cog.perceive(obs, act)
        self._log({"kind": "obs", "obs": self.cog.log.events[-1]["data"]})
        return ch

    def answer(self, item: str) -> str:
        """被问 "X 在哪"."""
        text = self.belief.describe(item)
        self._log({"kind": "say", "text": text})
        return text

    def begin(self, item: str) -> None:
        ev = self.cog.log.events
        self.search = {"item": item, "seq0": ev[-1]["seq"] if ev else 0, "steps": 0, "asked": False, "order": None}

    def _checked(self) -> set[str]:
        """这次找的时候已经全看过的地方 (开始找那一刻看得到的也算)."""
        return {r for r, s in self.belief.checked.items() if s >= self.search["seq0"]}

    def options(self, item: str) -> list[str]:
        """还能去看的地方, 排好序. 只读认知和经验 (作弊的两种读 peek)."""
        if self.policy == "oracle":
            at = self.peek()["where"].get(item)
            return [at] if at in self.layout.REGIONS else []
        if self.policy == "truth_menu":
            return ["coffee_machine"] if self.peek()["where"].get(item) == "coffee_machine" else []
        b, f, s = self.belief, self.belief.fact(item), self.search
        cands = [r for r in self.regions if r not in f["ruled_out"] and r not in self._checked()]
        first = [f["at"]] if f["how"] in KNOWN and f["at"] in self.regions and (f["how"] == "told" or f["at"] in cands) else []
        if self.policy == "random":
            if s["order"] is None:
                s["order"] = self.rng.sample(self.regions, len(self.regions))
            if f["how"] != "told":
                first = []
            rest = [r for r in s["order"] if r in cands]
        else:
            idx = list(self.layout.REGIONS).index
            def distance(region):
                return round(self.layout.dist(b.here, self.layout.zone_of(region)), 3) if b.here in self.layout.ZONES else float("inf")
            rest = sorted(cands, key=lambda r: (distance(r), self.layout.is_box(r), idx(r)))
            if self.policy == "experience":
                sc = self.cog.exp.scores(self.layout.kind_of(item))
                rest = sorted((r for r in cands if sc.get(r, 0) >= Experience.PROMOTE), key=lambda r: (-sc[r], idx(r))) + rest
        return list(dict.fromkeys(first + rest))

    def prompt(self, item: str, opts: list[str]) -> str:
        """这一步要是问模型, 发给它的就是这几行: 只有她的认知、经验和候选."""
        b, name = self.belief, self.layout.item_name(item)
        lines = [f"{clock(b.t)}, 你在{self.layout.zone_name(b.here)}.", f"你想用{name}冲咖啡, 先得找到它.", f"你记得: {b.describe(item)}."]
        looked = [self.layout.region_name(r) for r in self.layout.REGIONS if r in self._checked()]
        if looked:
            lines.append(f"这次看过: {'、'.join(looked)}.")
        lines += [f"经验: {x}." for x in self.cog.exp.lines(self.layout.kind_of(item))]
        lines.append("能去看的地方: " + "".join(f"{r} {self.layout.region_name(r)}, " for r in opts) + "ask 问用户.")
        return "\n".join(lines)

    def step(self, item: str) -> list:
        """找 item 的下一步动作: pick / close / go / open / look / ask / give_up."""
        if self.search is None or self.search["item"] != item:
            self.begin(item)
        opts = self.options(item)
        action = self._choose(item, opts)
        if action[0] != "give_up":
            self.search["steps"] += 1
        if self.keep:
            self._log({"kind": "decide", "belief": self.cog.digest(), "options": opts,
                       "prompt": self.prompt(item, opts), "action": action})
        return action

    def _choose(self, item: str, opts: list[str]) -> list:
        b, f, s = self.belief, self.belief.fact(item), self.search
        if self.policy == "truth_menu" and not opts:  # 照现在的冲咖啡脚本: 杯子不在咖啡机上就不做, 就算看得到在别处
            return ["give_up", "咖啡杯不在咖啡机上"]
        if f["how"] == "seen" and f["at"] in b.view:
            return ["pick", item]
        for r in b.opened:  # 打开看过、里面没有: 先关上
            return ["close", r]
        if self.policy not in CHEATS and not s["asked"] and (not opts or s["steps"] >= self.budget):
            s["asked"] = True
            return ["ask", item]
        if not opts:
            return ["give_up", "没找到"]
        r = opts[0]
        if self.layout.zone_of(r) != b.here:
            return ["go", self.layout.zone_of(r)]
        if self.layout.is_box(r) and r not in b.opened:
            return ["open", r]
        return ["look"]
