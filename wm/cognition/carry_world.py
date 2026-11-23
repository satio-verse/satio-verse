"""端东西的环境这一侧 (真值在这里, 她那边 carry.py 拿不到): 感知门, 和一个示意环境.

感知门 perceive(raw): 物理评测台 (tools/bench_carry.py, 液体在 web/physics/liquid.js) 按阶段记的真值 ->
她感知得到的后果. 规则 (见知识库「绫音认知与自主生活落地方案」的"感知门"):
  - 液面: 杯子在手里看得到杯口, 只给粗档 (接近满杯 / 大约半杯 / 快见底 / 空), 掉了几档;
  - 洒出: 端着的杯子洒出来, 一次洒到 VISIBLE_ML 以上她才注意得到 (再少就是挂在杯沿的几滴); 量只按液面掉了几档;
  - 碰到身上: 哪个部位、烫一下 / 烫伤 / 只是湿了 (不给温度、毫升).
  不进她那边的: 毫升、温度、落在背后的那一摊、液体模块自己记的洒出账.

示意环境 SimCarry: 不是物理. 只给学习逻辑做单元测试和快速对照 (几毫秒一趟), 规律按物理评测台看到的大致方向
(越满越容易洒; 不端稳时拿起、挪到身前、放下洒得多; 放慢、端稳那一段少洒), 数字是编的. 真的里程碑用物理评测台跑.
"""
from __future__ import annotations

import random

from .carry import STAGES

VISIBLE_ML = 0.5  # 端着的杯子一次洒出这么多 (约 10 滴) 她才注意得到
# 看到端着的杯子洒出来多大一股 (粗档, 不是毫升): 几滴 / 一点 / 一些 / 一大股 / 泼出来一大片, 每一档大约是上一档的两三倍.
# 只有"洒没洒、液面掉没掉档"的话, 物理评测台上每一趟挪到身前、走、放下都会洒一点, 满杯的马克杯洒 50 ml 液面也还是"接近满杯",
# 她分不出端稳以后少洒了多少, 里程碑里来回换做法; 三档 (几滴 / 一些 / 一大股) 时走路洒 78 ml 和端稳以后的 31 ml 都是"一大股",
# 端稳最大的好处看不出来 (2026-10-27 前两轮; 八种做法各三趟的分段数据见知识库实验记录)
# 名字和小屋那边 (desktop/web/room-liquid.js 的 AMOUNTS) 一样
SPLASH = ((3.0, "drops"), (10.0, "little"), (20.0, "some"), (40.0, "stream"))  # 再多是泼出来一大片 (splash)
# 部位的说法和 web/physics/body-hit.js 的 PART_NAMES 一样 (小屋里刺激就按这个记)
PARTS = {"rightHand": "右手", "leftHand": "左手", "pelvis": "肚子下面", "belly": "肚子", "chest": "胸口", "leftThigh": "左腿",
         "rightThigh": "右腿", "leftShin": "左小腿", "rightShin": "右小腿", "leftFoot": "左脚", "rightFoot": "右脚", "mouth": "嘴"}
LEVELS = ("wet", "hot", "burn")


def fill_bin(frac: float) -> str:
    """看杯口的粗档: 接近满杯 / 大约半杯 / 快见底 / 空 (和小屋液体 desktop/web/room-liquid.js 的 LEVELS 一样)."""
    return "full" if frac >= 0.75 else "half" if frac >= 0.3 else "low" if frac >= 0.03 else "empty"


def _rank(b: str) -> int:
    return ("empty", "low", "half", "full").index(b)


def splash(ml: float) -> str:
    """看到洒出来多大一股: none (没注意到) / drops / little / some / stream / splash."""
    if ml < VISIBLE_ML:
        return "none"
    return next((name for top, name in SPLASH if ml < top), "splash")


def perceive(raw: dict) -> dict:
    """评测台一趟的真值 -> 她感知到的后果. raw: {cap (ml), stages: {段: {ml0, ml1, spilled, contacts: {部位: {level}}}},
    delivered, secs}. 返回 {stages: {段: {spill: none / drops / little / some / stream / splash, drop, felt: [[部位, level]]}},
    delivered, secs, end_fill, start_fill (拿起来时看到的装量)}."""
    cap = raw["cap"]
    out = {}
    for s in STAGES:
        st = raw["stages"].get(s) or {}
        b0, b1 = fill_bin(st.get("ml0", 0) / cap), fill_bin(st.get("ml1", 0) / cap)
        felt = sorted([PARTS.get(p, p), c["level"]] for p, c in (st.get("contacts") or {}).items() if c.get("level") in LEVELS)
        out[s] = {"spill": splash(st.get("spilled", 0)), "drop": max(0, _rank(b0) - _rank(b1)), "felt": felt}
    last = (raw["stages"].get("put") or {}).get("ml1", 0)
    first = (raw["stages"].get("pick") or {}).get("ml0", 0)  # 拿起来的时候看得到杯口
    return {"stages": out, "delivered": bool(raw.get("delivered")), "secs": round(raw.get("secs", 0)), "end_fill": fill_bin(last / cap),
            "start_fill": fill_bin(first / cap)}


# -- 物理评测台 (web/motion-eval.js 的 carry, 液体在 web/physics/liquid.js) -----------------------------
BENCH_CAP = 3.141592653589793 * 0.04 ** 2 * 0.09 * 1e6   # 评测台的马克杯: 内半径 4 cm、内高 9 cm (约 452 ml)
HAND_SLOW = 0.6   # 拿放放慢: 行动层的 handSpeed
LESS = 0.6        # 少装一点: 装到容量的六成
WAIT_S = 1320     # 等它凉一点: 倒好以后放 22 分钟再端 (412 ml 80 ℃ -> 约 55 ℃, 溅上了只是烫一下; 评测台按液体模型冷却)


def scenario(ctx: dict, choice: dict) -> str:
    """她挑的做法 -> 评测台的场景名 (carry:装量>走法>温度>键=值...)."""
    fill = ctx["fill"]
    if choice.get("fill") == "less" and ctx.get("can_fill"):
        fill = str(LESS if ctx["fill"] == "full" else LESS / 2)
    how = "turn" if ctx.get("route") == "turns" else "normal"
    parts = [fill, how, "hot" if ctx.get("hot") else "cold", f"care={int(choice['care'])}"]
    if choice.get("hand") == "slow":
        parts.append(f"hand={HAND_SLOW}")
    if choice.get("settle"):
        parts.append("settle=1")
    if choice.get("wait"):
        parts.append(f"wait={int(ctx.get('wait_s') or WAIT_S)}")
    return "carry:" + ">".join(parts)


def raw_from_bench(carry: dict) -> dict:
    """评测台一趟的 extra.carry -> perceive 要的真值格式. 评测台按事件分段 (一段接一段, 不重叠), 每一段开始、结束时杯里多少
    (phaseMl)、洒了多少 (phases)、碰到身上哪里 (contactPhases, 按碰到的那一刻算段) 都直接给; 老版本没有 phaseMl 时按 phases 依次扣,
    没有 contactPhases 时算在洒得最多的那一段 (approx_contacts 标出来)."""
    ml, stages = float(carry["ml"]), {}
    phases, spans, by_phase = carry.get("phases") or {}, carry.get("phaseMl") or {}, carry.get("contactPhases")
    top = max(phases, key=phases.get) if phases else "lift"
    for s in STAGES:
        out = float(phases.get(s, 0.0))
        ml0, ml1 = spans.get(s) or (ml, max(0.0, ml - out))
        contacts = (by_phase or {}).get(s, {}) if by_phase is not None else (carry.get("contacts") or {}) if s == top else {}
        stages[s] = {"ml0": float(ml0), "ml1": float(ml1), "spilled": out, "contacts": contacts}
        ml = float(ml1)
    return {"cap": BENCH_CAP, "stages": stages, "delivered": bool(carry.get("delivered")), "secs": carry.get("secs", 0),
            "left": carry.get("left"), "ml": carry["ml"], "target": carry.get("target", carry["ml"]), "score": carry.get("score"),
            "approx_contacts": by_phase is None, "worstBurn": carry.get("worstBurn", 0), "wait": carry.get("wait", 0),
            "startTemp": carry.get("startTemp")}


# 这一趟做得怎么样 (评测这边定的, 她拿不到): 评测台的得分再算上时间和要不要热的. 和她自己的代价 (carry.py) 用一个尺子,
# 但这里按真的: 用户到底要不要热的 (hot_truth)、什么时候要 (by)、说没说不急 (relaxed)
PER_MIN = 0.01   # 每分钟 (端的那十几秒加上等凉)
RELAXED = 0.25   # 说了不急
LATE = 0.5       # 超过要的时间
COLD = 0.3       # 要热的, 端过去是凉了的


def task_score(raw: dict, task: dict | None = None) -> float:
    """评测台的得分 (送到、剩多少、烫伤、超过 20 秒) - 费的时间 - 晚了 - 要热的却等凉了. 没送到 0 分 (防"不走");
    少装就少送 (防"先倒空", 评测台按要送的量算); 等凉的时间按分钟扣、超过要的时间再扣 (防"一直等")."""
    if not raw["delivered"]:
        return 0.0
    task = task or {}
    wait = float(raw.get("wait") or 0)
    total = wait + float(raw.get("secs") or 0)
    out = score(raw) - PER_MIN * total / 60 * (RELAXED if task.get("relaxed") else 1.0)
    if task.get("by") is not None and total > float(task["by"]):
        out -= LATE
    if wait > 0 and task.get("hot_truth"):
        out -= COLD
    return round(out, 3)


def score(raw: dict) -> float:
    """评测台的得分 (web/motion-eval.js 的 carry): 送到才有分, min(1, 剩下的 / 要送的量) - 0.5 x 最重的烫伤 - 0.01 x 超过 20 秒的秒数.
    示意环境没有烫伤程度, 烫伤按 0.3 算."""
    if raw.get("score") is not None:
        return raw["score"]
    if not raw["delivered"]:
        return 0.0
    burn = raw.get("worstBurn") or (0.3 if any(c.get("level") == "burn" for st in raw["stages"].values() for c in st["contacts"].values()) else 0.0)
    return round(min(1.0, raw["left"] / raw["target"]) - 0.5 * burn - 0.01 * max(0.0, raw["secs"] - 20), 3)


# -- 示意环境 (不是物理) ------------------------------------------------------------------------------
BASE = {"pick": 0.8, "lift": 0.6, "walk": 0.3, "put": 0.5}  # 接近满杯、照常端时每一段洒的可能
FILL_K = {"full": 1.0, "half": 0.15, "low": 0.03, "empty": 0.0}
CUP = {"mug": 412.0, "espresso": 120.0}                           # 容量 (ml)
SECS = {("hand", "slow"): 1.5, ("care", 1): 2.0, ("settle", 1): 1.0}


class SimCarry:
    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def run(self, ctx: dict, choice: dict) -> dict:
        """端一趟: 按情境和做法掷每一段洒不洒、洒多少、烫没烫. 返回和物理评测台一样格式的真值."""
        fill = ctx["fill"]
        if choice.get("fill") == "less" and ctx.get("can_fill"):
            fill = {"full": "half", "half": "low"}.get(fill, fill)
        cap = CUP.get(ctx.get("cup"), 412.0)
        ml = cap * {"full": 0.92, "half": 0.5, "low": 0.15, "empty": 0.0}[fill]
        stages, secs = {}, 12.0 + sum(SECS.get((k, v), 0) for k, v in choice.items())
        for s in STAGES:
            p = BASE[s] * FILL_K[fill] * (1.2 if ctx.get("cup") == "espresso" else 1.0) * (0 if s == "walk" and ctx.get("route") == "none" else 1)
            if s == "walk" and ctx.get("route") == "turns":
                p *= 2
            if s in ("pick", "put") and choice["hand"] == "slow":
                p *= {"pick": 0.2, "put": 0.3}[s]
            if s in ("lift", "walk", "put") and choice["care"]:
                p *= {"lift": 0.3, "walk": 0.4, "put": 0.6}[s]
            if s in ("lift", "walk") and choice["settle"]:
                p *= {"lift": 0.5, "walk": 0.7}[s]
            ml0, spilled, contacts = ml, 0.0, {}
            if ml > 0 and self.rng.random() < min(0.95, p):
                spilled = min(ml, self.rng.uniform(1.0, 0.06 * cap))
                ml -= spilled
                if ctx.get("hot") and s in ("pick", "lift", "put") and self.rng.random() < 0.6:
                    contacts["rightHand"] = {"level": "burn" if spilled > 0.03 * cap and not choice.get("wait") else "hot"}  # 等凉了只是烫一下
            else:
                spilled = self.rng.uniform(0, 0.3) if ml > 0 else 0.0  # 挂在杯沿的几滴, 她注意不到
                ml -= spilled
            stages[s] = {"ml0": round(ml0, 2), "ml1": round(ml, 2), "spilled": round(spilled, 2), "contacts": contacts}
        return {"cap": cap, "stages": stages, "delivered": True, "secs": round(secs, 1), "left": round(ml, 1), "ml": round(stages["pick"]["ml0"], 1),
                "target": round(cap * {"full": 0.92, "half": 0.5, "low": 0.15}.get(ctx.get("want") or ctx["fill"], 0.92), 1),
                "wait": float(ctx.get("wait_s") or WAIT_S) if choice.get("wait") else 0}
