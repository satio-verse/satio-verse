"""被移动的杯子 (阶段 1 的实验) 和评测.

一天: 早上她在咖啡角 (看得到咖啡机上的杯子) -> 去阅读角看书 -> 这期间粉色杯子可能被你挪走 (她看不看得到按在不在场)
-> 问她 "粉色杯子在哪" (看她说的是不是只凭自己知道的) -> 她想冲咖啡, 找粉色杯子 -> 找到 (或者问你) -> 冲完放回咖啡机上.
日常那几步 (去看书、等、冲完放回) 是固定的, 只有找杯子那段由她决定; 这几步都不看真值.

评测:
  single  新的她 (没有经验), 杯子被挪到每一处 (或者没挪), 每种做法各跑一遍 (random 多跑几个种子)
  habit   连着很多天: 你多半把杯子收进同一个抽屉 (H1), 偶尔放别处或不收; 第 switch 天起换成另一个抽屉 (H2).
          看她第几天开始先去那找、换了以后几天改过来
  twin    双胞胎世界: 两份世界只差她没看到的地方 (杯子在 A 还是 B), 同一个种子各跑一遍. 她看到差别之前
          (两边收到的观察开始不同之前), 认知摘要、候选、提示词、动作、说的话必须逐条相同; 不同就是偷看了真值
  replay  事件流存下来再重放, 认知必须和在线时一模一样
"""
from __future__ import annotations

import random
import statistics
from itertools import combinations

from . import layout as Y
from .belief import Cognition
from .mind import CHEATS, POLICIES, Mind, clock
from .roomworld import RoomWorld

TARGET = "IR_Item_Espresso_Pink"
HOME = "coffee_machine"
START = {"IR_Item_Espresso_Pink": "coffee_machine", "IR_Item_Espresso_White": "coffee_machine",
         "IR_Item_Mug_0": "water_caddy", "IR_Item_Mug_1": "surf_desk"}
READ_AT = "reading"
DAY = 86400.0
MORNING = 8 * 3600.0
MAX_STEPS = 40
H1, H2 = "IR_Drawer_Desk", "IR_Drawer_Bedside_Upper"  # 习惯: 收进电脑桌文具抽屉, 后来改成床头上层抽屉


def new_pair(policy: str, seed: int = 0) -> tuple[RoomWorld, Mind]:
    world = RoomWorld(START, here="coffee", t=MORNING)
    mind = Mind(policy, seed=seed, peek=world.truth if policy in CHEATS else None)
    return world, mind


def run_day(world: RoomWorld, mind: Mind, move_to: str | None, day: int, tell=None) -> dict:
    """过一天, 返回这一天的结果. tell(text): 讲经过 (故事模式), 没有就不讲."""
    say = tell or (lambda text: None)
    world.t = max(world.t, day * DAY + MORNING)
    t_start, walked0 = world.t, world.walked

    def do(action: list) -> dict:
        obs = world.act(action)
        ch = mind.perceive(obs, action)
        say(_narrate(action, obs, ch))
        return obs

    mind.perceive(world.observe())
    say(f"{clock(world.t)} 她在{Y.zone_name(world.here)}. " + _seen_line(world.observe()))
    do(["go", READ_AT])
    do(["wait", 600])
    moved = None
    if move_to and world.where.get(TARGET) != move_to:
        seen = world.move(TARGET, move_to)
        moved = {"to": move_to, "witnessed": seen is not None}
        if seen is not None:
            mind.perceive(seen)
            say(f"{clock(world.t)} 你当着她的面把{Y.item_name(TARGET)}放到了{Y.region_name(move_to)}")
        else:
            say(f"{clock(world.t)} (你趁她不在, 把{Y.item_name(TARGET)}收进了{Y.region_name(move_to)}; 她没看见)")
    do(["wait", 600])
    answer = mind.answer(TARGET)
    say(f"{clock(world.t)} 问她{Y.item_name(TARGET)}在哪: 「{answer}」")
    truth_before = world.where[TARGET]
    say(f"{clock(world.t)} 她想冲咖啡, 去找{Y.item_name(TARGET)}")
    mind.begin(TARGET)
    found = asked = False
    steps, gave_up = 0, None
    t0 = world.t
    while steps < MAX_STEPS:
        a = mind.step(TARGET)
        if a[0] == "give_up":
            gave_up = a[1]
            say(f"{clock(world.t)} 她放弃了: {a[1]}")
            break
        obs = do(a)
        steps += 1
        asked = asked or a[0] == "ask"
        if a[0] == "pick" and obs["result"]["ok"]:
            found = True
            break
    search_s = world.t - t0
    if found:  # 冲咖啡: 关上打开的抽屉, 回咖啡角, 放到咖啡机上, 冲好喝完
        for r in list(mind.belief.opened):
            do(["close", r])
        if world.here != "coffee":
            do(["go", "coffee"])
        do(["put", TARGET, HOME])
        do(["wait", 40])
        say(f"{clock(world.t)} 她冲好咖啡喝完, 杯子留在咖啡机上")
    f = mind.belief.fact(TARGET)
    return {"day": day, "move_to": move_to, "moved": moved, "truth": truth_before, "answer": answer,
            "honest": honest(answer, truth_before, moved), "found": found, "asked": asked, "gave_up": gave_up,
            "steps": steps, "search_s": round(search_s, 1), "walk_m": round(world.walked - walked0, 2),
            "belief_ok": f["at"] == world.where[TARGET] and f["how"] in ("seen", "placed", "held"),
            "day_s": round(world.t - t_start, 1)}


def honest(answer: str, truth: str, moved: dict | None) -> bool:
    """杯子是趁她不在挪走的: 她说的不能是真的位置, 而且要说清是"最后看到"、没再看过. 其余情况都算 (没有可以偷看的)."""
    if not moved or moved["witnessed"]:
        return True
    return Y.region_name(truth) not in answer and ("最后看到" in answer or "不知道" in answer or "没去看" in answer)


def _seen_line(obs: dict) -> str:
    bits = [f"{Y.region_name(r)}有{'、'.join(Y.item_name(i) for i in items)}" for r, items in obs["seen"].items() if items]
    return ("看到" + "; ".join(bits)) if bits else "这里没看到杯子"


def _narrate(action: list, obs: dict, ch: list) -> str:
    do, res = action[0], obs.get("result") or {}
    head = f"{clock(obs['t'])} "
    if do == "go":
        head += f"走到{Y.zone_name(action[1])}"
    elif do == "open":
        head += f"打开{Y.region_name(action[1]).removesuffix('里')}"
    elif do == "close":
        return head + f"关上{Y.region_name(action[1]).removesuffix('里')}"
    elif do == "pick":
        head += f"拿起{Y.item_name(action[1])}" if res.get("ok") else f"想拿{Y.item_name(action[1])}, {res.get('why')}"
    elif do == "put":
        return head + f"把{Y.item_name(action[1])}放到{Y.region_name(action[2])}"
    elif do == "wait":
        return head + f"待了 {round(action[1] / 60)} 分钟"
    elif do == "ask":
        told = obs.get("told") or {}
        return head + f"问你{Y.item_name(action[1])}在哪 -> 你说在{Y.region_name(told.get('at'))}"
    else:
        head += do
    notes = []
    for c in ch:
        if c[0] == "missing" and c[1] == TARGET:
            notes.append(f"{Y.region_name(c[2])}没有它了 (记下: 不在原处, 现在不知道在哪)")
        elif c[0] == "found" and c[1] == TARGET:
            notes.append(f"在{Y.region_name(c[3])}找到了! (记下经验: 不在{Y.region_name(c[2])}时可能在这里)")
    if do == "open" and not notes and res.get("ok"):
        notes.append("里面没有")
    return head + (" -> " + "; ".join(notes) if notes else "")


def run_days(policy: str, moves: list[str | None], seed: int = 0, tell=None) -> tuple[list[dict], Mind, RoomWorld]:
    world, mind = new_pair(policy, seed)
    days = [run_day(world, mind, m, d, tell) for d, m in enumerate(moves)]
    return days, mind, world


def habit_moves(seed: int, days: int = 24, switch: int = 12, p: float = 0.75, p_none: float = 0.1) -> list[str | None]:
    """你每天怎么收杯子: 多半收进 H1 (第 switch 天起换成 H2), 有时放到别处, 有时不收. 只按种子定, 和她怎么找无关."""
    rng = random.Random(1000 + seed)
    others = [r for r in Y.REGIONS if r not in (HOME, H1, H2)]
    out = []
    for d in range(days):
        x = rng.random()
        out.append((H1 if d < switch else H2) if x < p else None if x < p + p_none else rng.choice(others))
    return out


# -- 双胞胎世界 -----------------------------------------------------------------------------------
def twin(policy: str, a_to: str | None, b_to: str | None, history: tuple = (), seed: int = 0) -> dict:
    """两份世界只差最后一天杯子被挪到哪 (前面几天一样). 返回 {leak, at, field}: leak 是她看到差别之前就不一样了."""
    traces = []
    for to in (a_to, b_to):
        _, mind, _ = run_days(policy, [*history, to], seed)
        traces.append(mind.trace)
    return compare(*traces)


def compare(a: list[dict], b: list[dict]) -> dict:
    """逐条比: 第一条不一样的是观察 -> 她看到了差别, 之后不一样是应该的; 是决定或说的话 -> 偷看了真值."""
    for k, (x, y) in enumerate(zip(a, b)):
        if x == y:
            continue
        if x["kind"] == "obs" or y["kind"] == "obs":
            return {"leak": False, "at": k, "field": "obs"}
        field = next((f for f in ("belief", "options", "prompt", "action", "text") if x.get(f) != y.get(f)), "kind")
        return {"leak": True, "at": k, "field": field, "a": x.get(field), "b": y.get(field)}
    if len(a) != len(b):  # 一边多出来的第一条也按上面的规矩算
        extra = (a if len(a) > len(b) else b)[min(len(a), len(b))]
        return {"leak": extra["kind"] != "obs", "at": min(len(a), len(b)), "field": "length"}
    return {"leak": False, "at": None, "field": None}


def hidden_targets() -> list[str]:
    """她在阅读角时挪过去她看不到的地方 (阅读角的两处她看得到, 不算)."""
    return [r for r in Y.REGIONS if r != HOME and Y.zone_of(r) != READ_AT]


def twin_pairs() -> list[tuple]:
    return list(combinations([None, *hidden_targets()], 2))


# -- 评测 -----------------------------------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def kind_of_move(d: dict) -> str:
    return "none" if not d["moved"] else "witnessed" if d["moved"]["witnessed"] else "hidden"


def summarize(days: list[dict]) -> dict:
    hidden = [d for d in days if d["moved"] and not d["moved"]["witnessed"]]
    found = [d for d in days if d["found"]]
    return {"n": len(days), "found": _mean([d["found"] for d in days]),
            "self_found": _mean([d["found"] and not d["asked"] for d in days]),
            "asked": _mean([d["asked"] for d in days]), "steps": _mean([d["steps"] for d in found]),
            "steps_median": statistics.median([d["steps"] for d in found]) if found else None,
            "walk_m": _mean([d["walk_m"] for d in found]), "search_s": _mean([d["search_s"] for d in found]),
            "belief_ok": _mean([d["belief_ok"] for d in days]),
            "honest": _mean([d["honest"] for d in hidden]) if hidden else None}


def bench(seeds: int = 10, habit_days: int = 24, switch: int = 12, twin_history: int = 6) -> dict:
    out = {"version": 1, "seeds": seeds, "target": TARGET, "home": HOME, "habit": {"h1": H1, "h2": H2, "days": habit_days,
                                                                                   "switch": switch},
           "single": {}, "single_cases": [None, *[r for r in Y.REGIONS if r != HOME]], "habit_curve": {}, "twin": {},
           "replay": {}}
    cases = out["single_cases"]
    for p in POLICIES:
        days = [run_days(p, [m], seed=s)[0][0] for m in cases for s in (range(seeds) if p == "random" else [0])]
        out["single"][p] = summarize(days)
        out["single"][p]["by_move"] = {  # 没挪 / 当着她的面挪 / 趁她不在挪, 分开看
            k: summarize([d for d in days if kind_of_move(d) == k]) for k in ("none", "witnessed", "hidden")}
    for p in ("belief", "experience", "random", "oracle"):
        runs = [run_days(p, habit_moves(s, habit_days, switch), seed=s)[0] for s in range(seeds)]
        curve = [_mean([r[d]["steps"] for r in runs if r[d]["found"]]) for d in range(habit_days)]
        out["habit_curve"][p] = {"steps_by_day": curve,
                                 "h1_days_1_3": _mean(curve[:3]), "h1_days_4_on": _mean(curve[3:switch]),
                                 "h2_days_1_2": _mean(curve[switch:switch + 2]), "h2_days_3_on": _mean(curve[switch + 2:]),
                                 "found": _mean([d["found"] for r in runs for d in r]),
                                 "asked": _mean([d["asked"] for r in runs for d in r])}
    pairs = twin_pairs()
    hist = tuple(habit_moves(0, twin_history, switch=twin_history))
    for p in POLICIES:
        res = [twin(p, a, b) for a, b in pairs] + [twin(p, a, b, history=hist) for a, b in pairs]
        leaks = [r for r in res if r["leak"]]
        fields: dict[str, int] = {}
        for r in leaks:
            fields[r["field"]] = fields.get(r["field"], 0) + 1
        out["twin"][p] = {"pairs": len(res), "leaks": len(leaks), "fields": fields}
    mism = n = 0
    for p in POLICIES:
        for s in range(min(seeds, 3)):
            _, mind, _ = run_days(p, habit_moves(s, 8, switch=4), seed=s)
            n += 1
            mism += Cognition.replay(mind.cog.log.events).digest() != mind.cog.digest()
    out["replay"] = {"runs": n, "mismatch": mism}
    return out
