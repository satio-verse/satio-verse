"""她的认知 (b_t) 和经验. 只由她收到的观察 (含自己动作的结果、问来的话) 更新, 可以过时、可以错; 世界的真值拿不到.

每样东西一条 (fact):
  at / how    以为在哪, 凭什么: seen 亲眼看到 / placed 自己放的 / held 拿在手上 / told 听你说的 (还没去看) /
              missing 去看过原处已经不在 (现在不知道在哪) / taken 看着别人拿走了 / unknown 没见过
  seen_at / seen_t   最后一次亲眼看到在哪、什么时候
  lost_from   不见了以前以为在哪 (找到时记经验用)
  ruled_out   不见了以后找过、没有的地方 -> 时刻
  ev          依据: 最近几条事件的序号
观察是"这一刻看得到的几处, 每处里有什么" (全看到了): 看到的记 seen; 以为在这几处、这次没看到的记 missing.
没看到的变化不会让这里变 —— 你趁她不在把杯子收进抽屉, 她记得的还是"最后看到在咖啡机上".

经验 (Experience): 东西不在原以为的地方、后来在哪找到. 按种类记 (杯子), 越新的越算数 (每多一条, 旧的乘 DECAY),
每条带依据 (事件序号), 可以撤销 (revoke): 撤掉以后排序就回到没有那几条时的样子.
"""
from __future__ import annotations

import hashlib
import json

from . import layout as Y
from .events import EventLog

KNOWN = ("seen", "placed", "told")  # 以为在某一处 (at 是那一处)
LOST = ("missing", "taken")         # 找不着
FILL_SAY = {"full": "里面装得快满了", "half": "里面大约半杯", "low": "里面快见底了", "empty": "里面是空的"}  # 看到的液面粗档


def _fact() -> dict:
    return {"at": None, "how": "unknown", "t": None, "seen_at": None, "seen_t": None, "lost_from": None,
            "ruled_out": {}, "ev": []}


def ago(secs: float) -> str:
    s = max(0, int(secs))
    if s < 90:
        return "刚才"
    if s < 3600:
        return f"{round(s / 60)} 分钟前"
    if s < 86400:
        return f"{s // 3600} 小时前"
    return f"{s // 86400} 天前"


def _digest(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class Belief:
    def __init__(self, layout=None):
        self.layout = layout or Y.LEGACY
        self.facts: dict[str, dict] = {}
        self.checked: dict[str, int] = {}  # 某一处最后一次全看到里面是哪一条事件 (序号; 真的钟两份观察可能同一时刻)
        self.here: str | None = None
        self.view: list[str] = []    # 这一刻看得到的几处
        self.opened: list[str] = []  # 她这一处开着的抽屉
        self.t: float | None = None
        self.felt: list[list] = []   # 最近身上的感觉 [时刻, 部位, wet / hot / burn / bruise, 在哪一段] (感知门报来的, 最多 10 条)

    def fact(self, item: str) -> dict:
        return self.facts.get(item) or _fact()

    def _f(self, item: str) -> dict:
        return self.facts.setdefault(item, _fact())

    def observe(self, obs: dict, seq: int) -> list[tuple]:
        """收一份观察 -> 认知的变化 [(种类, 东西, ...)]: seen 看到 / found 不在原处、在这里找到 (原以为在哪, 在哪找到) /
        missing 原处不在了 / held 拿起来了 / placed 放下了 / taken 看着被拿走 / told 听说在哪."""
        t, seen = obs["t"], obs["seen"]
        self.t, self.here, self.view, self.opened = t, obs["at"], sorted(seen), list(obs.get("open", []))
        ch: list[tuple] = []
        act, res = obs.get("act"), obs.get("result") or {}
        if act and act[0] == "put" and res.get("ok"):  # 自己放下的
            f = self._f(act[1])
            f.update(at=act[2], how="placed", t=t, ruled_out={})
            ch.append(("placed", act[1], act[2]))
        for ev in obs.get("events", []):  # 看着别人从她面前拿走、放到她看不到的地方
            if ev.get("kind") == "moved" and ev.get("from") and not ev.get("to"):
                f = self._f(ev["item"])
                f.update(at=None, how="taken", t=t, lost_from=ev["from"], ruled_out={ev["from"]: t}, by=ev.get("by"))
                f["ev"] = (f["ev"] + [seq])[-4:]
                ch.append(("taken", ev["item"], ev["from"]))
        for r in sorted(seen):
            self.checked[r] = seq
            for i in seen[r]:
                ch += self._saw(i, r, t, seq)
        present = {i for items in seen.values() for i in items} | set(obs.get("hand", []))
        for i, f in sorted(self.facts.items()):
            if f["how"] in KNOWN and f["at"] in seen and i not in present:
                f.update(how="missing", lost_from=f["at"], at=None, t=t)
                f["ruled_out"] = {f["lost_from"]: t}
                f["ev"] = (f["ev"] + [seq])[-4:]
                ch.append(("missing", i, f["lost_from"]))
        for i, f in self.facts.items():  # 找不着的东西: 这一眼看到的几处也没有
            if f["how"] in LOST:
                for r in seen:
                    f["ruled_out"].setdefault(r, t)
        for i in obs.get("hand", []):
            f = self._f(i)
            if f["how"] != "held":
                f.update(at="hand", how="held", t=t, lost_from=None, ruled_out={})
                f["ev"] = (f["ev"] + [seq])[-4:]
                ch.append(("held", i))
        for i, b in (obs.get("fill") or {}).items():  # 看得到杯口的: 液面粗档
            f = self._f(i)
            if f.get("fill") != b:
                ch.append(("fill", i, f.get("fill"), b))
            f.update(fill=b, fill_t=t)
        for ev in obs.get("events", []):  # 感觉到、看到的后果 (感知门报来的, 没有温度、毫升)
            if ev.get("kind") == "felt":
                self.felt = (self.felt + [[t, ev["part"], ev["level"], ev["stage"]]])[-10:]
                ch.append(("felt", ev["part"], ev["level"], ev["stage"]))
            elif ev.get("kind") == "spill":
                ch.append(("spill", ev["item"], ev["amount"], ev["where"], ev["stage"], ev["touched"]))
            elif ev.get("kind") == "slip":  # 刚体: 手里的东西滑了一下
                ch.append(("slip", ev["item"], ev["hand"]))
            elif ev.get("kind") in ("drop", "fall"):  # 脱手了 / 看见掉了、倒了: 杯子不在手里了, 在哪不知道 (掉到地上还是台面上)
                if ev["item"] in self.layout.ITEMS:
                    f = self._f(ev["item"])
                    f.update(at=None, how="dropped", t=t, dropped_to=ev.get("where"))
                    f["ev"] = (f["ev"] + [seq])[-4:]
                ch.append(("dropped", ev["item"], ev["hand"]) if ev["kind"] == "drop" else
                          ("fell", ev["item"], ev["where"], ev["toppled"], ev["loud"]))
            elif ev.get("kind") == "noise":
                ch.append(("noise", ev["loud"]))
            elif ev.get("kind") == "empty_sip":  # 喝不到: 知道是空的
                f = self._f(ev["item"])
                if f.get("fill") != "empty":
                    ch.append(("fill", ev["item"], f.get("fill"), "empty"))
                f.update(fill="empty", fill_t=t)
                ch.append(("empty", ev["item"]))
        told = obs.get("told")
        if told and told.get("item"):
            f = self._f(told["item"])
            if told.get("at") and not (f["how"] in ("seen", "held") and f["at"] in self.view + ["hand"]):
                if f["how"] in KNOWN and f["at"] != told["at"]:
                    f["lost_from"] = f["at"]  # 以为在别处, 你说在这里: 找到时也算"挪过地方"
                f.update(at=told["at"], how="told", t=t, by=told.get("by"))
                f["ev"] = (f["ev"] + [seq])[-4:]
            ch.append(("told", told["item"], told.get("at")))
        return ch

    def _saw(self, i: str, r: str, t: float, seq: int) -> list[tuple]:
        f = self._f(i)
        how, at, lost = f["how"], f["at"], f["lost_from"]
        if how in LOST or (how == "told" and lost):
            out = [("found", i, lost, r)]
        elif how in KNOWN and at != r:  # 以为在别处, 在这里看到了: 也是一次"挪过地方"的发现
            out = [("found", i, at, r)]
        elif how in ("unknown", "held") or at != r:
            out = [("seen", i, r)]
        else:
            out = []
        f.update(at=r, how="seen", t=t, seen_at=r, seen_t=t, lost_from=None, ruled_out={})
        f["ev"] = (f["ev"] + [seq])[-4:]
        return out

    def describe(self, item: str, now: float | None = None) -> str:
        """被问"X 在哪"时她说的: 只用自己知道的, 带上凭什么、多久以前."""
        f, name = self.fact(item), self.layout.item_name(item)
        now = self.t if now is None else now
        how = f["how"]
        if how == "unknown":
            return f"我还没见过{name}"
        fill = FILL_SAY.get(f.get("fill"), "")
        if how == "held":
            return f"{name}在我手里" + (f", {fill}" if fill else "")
        if how == "dropped":
            where = {"floor": "掉到地上了", "surface": "掉在台面上了"}.get(f.get("dropped_to"), "掉下去了")
            return f"{ago((now or 0) - (f['t'] or 0))}{name}{where}, 还没去捡"
        when = ago((now or 0) - (f["t"] or 0))
        if how == "seen" and f["at"] in self.view and f["t"] == self.t:
            return f"{name}就在{self.layout.region_name(f['at'])}, 我正看着呢" + (f", {fill}" if fill else "")
        if how == "seen":
            return (f"最后看到{name}在{self.layout.region_name(f['at'])} ({when}{', ' + fill if fill else ''}), 之后没再看过, "
                    "不知道现在还在不在")
        if how == "placed":
            return f"{when}我把{name}放在{self.layout.region_name(f['at'])}了, 之后没再看过"
        if how == "told":
            return f"你说{name}在{self.layout.region_name(f['at'])}, 我还没去看过"
        last = (f"最后看到{name}在{self.layout.region_name(f['seen_at'])} ({ago((now or 0) - f['seen_t'])})"
                if f["seen_at"] else f"{name}原来在{self.layout.region_name(f['lost_from'])}")
        if how == "taken":
            return f"{when}看着{f.get('by') or '别人'}把{name}拿走了, 不知道放哪了"
        others = [self.layout.region_name(r) for r in f["ruled_out"] if r != f["lost_from"]]
        return (f"{last}; {when}去看已经不在了" + (f", {'、'.join(others)}也找过, 没有" if others else "")
                + ", 现在不知道在哪")

    def state(self) -> dict:
        return {"facts": self.facts, "checked": self.checked, "here": self.here, "view": self.view,
                "opened": self.opened, "t": self.t, "felt": self.felt}


class Experience:
    DECAY = 0.6     # 每多一条, 以前的乘这个 (习惯变了两三次就改过来)
    PROMOTE = 0.5   # 分数到这个才算"常在这里找到", 找东西时提到前面去

    def __init__(self, layout=None):
        self.layout = layout or Y.LEGACY
        self.records: list[dict] = []  # {kind, item, lost_from, found_at, t, ev}

    def record(self, item: str, lost_from: str, found_at: str, t: float, ev: int) -> None:
        self.records.append({"kind": self.layout.kind_of(item), "item": item, "lost_from": lost_from, "found_at": found_at,
                             "t": t, "ev": ev})

    def scores(self, kind: str) -> dict[str, float]:
        recs = [r for r in self.records if r["kind"] == kind]
        out: dict[str, float] = {}
        for k, r in enumerate(recs):
            out[r["found_at"]] = out.get(r["found_at"], 0.0) + self.DECAY ** (len(recs) - 1 - k)
        return out

    def revoke(self, evs) -> int:
        """撤掉依据是这几条事件的经验 (比如那几次是别人故意乱放的). 返回撤掉几条."""
        evs = set(evs)
        n = len(self.records)
        self.records = [r for r in self.records if r["ev"] not in evs]
        return n - len(self.records)

    def lines(self, kind: str, top: int = 2) -> list[str]:
        score = self.scores(kind)
        count: dict[str, int] = {}
        for r in self.records:
            if r["kind"] == kind:
                count[r["found_at"]] = count.get(r["found_at"], 0) + 1
        best = sorted(score, key=lambda r: -score[r])[:top]
        return [f"{self.layout.KINDS.get(kind, '东西')}不在原以为的地方时, 在{self.layout.region_name(r)}找到过 {count[r]} 次" for r in best]

    def state(self) -> dict:
        return {"records": self.records}


class Cognition:
    """认知 + 经验 + 事件流. 在线 (perceive) 和回放 (replay) 走同一个 apply, 所以回放出来的一模一样."""

    def __init__(self, world: str = "room", layout=None):
        self.layout = layout or Y.LEGACY
        self.belief = Belief(self.layout)
        self.exp = Experience(self.layout)
        self.log = EventLog(world)

    def perceive(self, obs: dict, act: list | None = None) -> list[tuple]:
        return self.apply(self.log.add("obs", {**obs, **({"act": list(act)} if act else {})}))

    def apply(self, ev: dict) -> list[tuple]:
        ch = self.belief.observe(ev["data"], ev["seq"])
        for c in ch:
            if c[0] == "found" and c[2] and c[2] != c[3]:
                self.exp.record(c[1], c[2], c[3], ev["t"], ev["seq"])
        return ch

    @classmethod
    def replay(cls, events: list[dict], world: str = "room", layout=None) -> "Cognition":
        cog = cls(world, layout=layout)
        for ev in events:
            cog.log.events.append(ev)
            cog.apply(ev)
        return cog

    def digest(self) -> str:
        return _digest({"belief": self.belief.state(), "exp": self.exp.state()})
