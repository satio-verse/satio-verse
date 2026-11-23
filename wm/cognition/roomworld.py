"""无界面的小窝 (认知实验的环境, 不是桌宠页面). 世界的真值 —— 东西实际在哪、抽屉开没开、她站在哪 —— 只在这里.

她 (mind.py) 拿不到这个对象, 只拿到 observe / act 返回的观察:
  - 站在哪一处就看得到那一处的台面, 和那一处打开着的抽屉、冰箱里面; 别处的、关着的看不到;
  - 动作没做成时的原因只说她看得到的 ("这里没看到粉色浓缩咖啡杯"), 不说东西其实在哪;
  - 问你 (ask) 时你照实说 —— 这是她问来的消息, 算观察的一种 (told).
你 (或者别人) 挪东西 (move) 只改真值; 起点或终点在她那一处时她看得到, 返回一份观察, 不然返回 None (她不知道).

简化 (阶段 1): 只按"在不在同一处"算看不看得到, 不算距离和遮挡; 观察是结构化的 (直接给编号), 不经过视觉识别.
"""
from __future__ import annotations

from . import layout as Y

WALK = 0.7     # 走路 m/s (加起步转身 2 秒)
COST = {"open": 2.0, "close": 1.5, "pick": 2.0, "put": 2.0, "look": 1.0, "ask": 6.0}
HANDS = 2


class RoomWorld:
    def __init__(self, where: dict[str, str], here: str = "coffee", t: float = 8 * 3600.0, world: str = "room"):
        self.where = dict(where)   # 东西 -> 放在哪 (region) 或 "hand"
        self.opened: set[str] = set()
        self.here = here
        self.t = t
        self.world = world
        self.walked = 0.0          # 走了多少米 (评测看)
        self.history: list[dict] = []  # 真值这边发生过的事 (评测看, 不给她)

    def truth(self) -> dict:
        """真值快照: 只给执行器和评测器 (还有故意作弊的对照策略)."""
        return {"where": dict(self.where), "opened": sorted(self.opened), "here": self.here, "t": self.t}

    def visible(self) -> list[str]:
        return [r for r, (_, z, box) in Y.REGIONS.items() if z == self.here and (not box or r in self.opened)]

    def observe(self, **extra) -> dict:
        """这一刻她看得到的: 看得到的几处各有什么 (全看到了), 手里拿着什么, 这一处开着的抽屉."""
        seen = {r: sorted(i for i, at in self.where.items() if at == r) for r in self.visible()}
        return {"world": self.world, "t": round(self.t, 1), "at": self.here, "seen": seen,
                "hand": sorted(i for i, at in self.where.items() if at == "hand"),
                "open": sorted(r for r in self.opened if Y.zone_of(r) == self.here), **extra}

    def _fail(self, do: str, why: str) -> dict:
        self.t += 1.0
        return self.observe(result={"do": do, "ok": False, "why": why})

    def act(self, action: list) -> dict:
        """她做一个动作 -> 做完那一刻的观察 (带 result: 做成没有、没做成她看得到的原因)."""
        do, args = action[0], action[1:]
        if do == "go":
            z = args[0]
            if z not in Y.ZONES:
                return self._fail(do, "没有这个地方")
            d = Y.dist(self.here, z)
            self.t += d / WALK + 2.0
            self.walked += d
            self.here = z
        elif do in ("open", "close"):
            r = args[0]
            if r not in Y.REGIONS or Y.zone_of(r) != self.here:
                return self._fail(do, f"这里没有{Y.region_name(r)}")
            if not Y.is_box(r):
                return self._fail(do, f"{Y.region_name(r)}不用打开")
            (self.opened.add if do == "open" else self.opened.discard)(r)
            self.t += COST[do]
        elif do == "pick":
            i = args[0]
            if self.where.get(i) not in self.visible():
                return self._fail(do, f"这里没看到{Y.item_name(i)}")
            if sum(1 for at in self.where.values() if at == "hand") >= HANDS:
                return self._fail(do, "两只手都拿着东西")
            self.where[i] = "hand"
            self.t += COST[do]
        elif do == "put":
            i, r = args
            if self.where.get(i) != "hand":
                return self._fail(do, f"手里没有{Y.item_name(i)}")
            if r not in Y.REGIONS or Y.zone_of(r) != self.here:
                return self._fail(do, f"这里没有{Y.region_name(r)}")
            if Y.is_box(r) and r not in self.opened:
                return self._fail(do, f"{Y.region_name(r)}关着")
            self.where[i] = r
            self.t += COST[do]
        elif do == "look":
            self.t += COST[do]
        elif do == "wait":
            self.t += float(args[0])
        elif do == "ask":  # 问你: 你照实说 (拿在她手里的不用问)
            i = args[0]
            self.t += COST[do]
            at = self.where.get(i)
            return self.observe(result={"do": do, "ok": True, "why": ""},
                                told={"item": i, "at": at if at in Y.REGIONS else None, "by": "你"})
        else:
            return self._fail(do, "不会做这个")
        return self.observe(result={"do": do, "ok": True, "why": ""})

    def move(self, item: str, to: str, by: str = "你") -> dict | None:
        """别人挪东西 (改真值). 起点或终点在她那一处: 她看着, 返回一份观察 (终点在她这一处时连抽屉里面也算看到了);
        不然返回 None —— 她没看到, 什么也不知道."""
        src = self.where.get(item)
        if src == "hand" or to not in Y.REGIONS:
            raise ValueError(f"挪不了 {item} -> {to}")
        self.where[item] = to
        self.history.append({"t": self.t, "move": item, "from": src, "to": to, "by": by})
        near_src, near_to = src in Y.REGIONS and Y.zone_of(src) == self.here, Y.zone_of(to) == self.here
        if not (near_src or near_to):
            return None
        obs = self.observe(events=[{"kind": "moved", "item": item, "from": src if near_src else None,
                                    "to": to if near_to else None, "by": by}])
        if near_to and to not in obs["seen"]:  # 放进她面前的抽屉: 看着放进去的
            obs["seen"][to] = sorted(i for i, at in self.where.items() if at == to)
        return obs
