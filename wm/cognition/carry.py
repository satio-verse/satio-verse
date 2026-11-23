"""端东西的经验和预测 (最小后果闭环, 认知这边): 行动前预测 -> 执行 -> 感知后果 -> 更新经验 -> 下次提前调整.

她只收感知门 (carry_world.perceive / 小屋的 room-liquid.js 的 perceive) 出来的后果: 每一段 (拿起 / 挪到身前 / 走 / 放下)
看到洒出来多大一股、液面掉了几档, 哪里烫了、湿了; 端了多久 (她自己的钟). 不收毫升、温度. 这个文件不 import 环境 (测试查).

- 情境 (ctx): 装了几档 (fill: full / half / low / empty; None 是没看到杯口, 不知道), 杯子 (cup: mug / espresso, 看得出大小),
  是不是刚冲好的热咖啡 (hot), 路线 (route: none 不走 / short / turns), 谁倒的 (source: self / user).
  这一趟要做成什么样 (都只来自她看到、被告诉的, 不读真值): 要送多少 (want, 默认就是现在装的), 什么时候要 (by: 几秒以内; None
  是没说), 想不想喝热的 (hot_pref: True 要热的 / False 凉一点也行 / None 不知道), 说了不急 (relaxed), 等凉要等多久 (wait_s,
  看杯子: 小屋的浓缩杯约 8 分钟, 评测台的马克杯 22 分钟), 能不能少装 (can_fill: 她自己装的才能).
- 做法 (choice, 和物理评测台 web/motion-eval.js 的 carry 能挑的几样一样): 拿放快慢 (hand: 拿起和放下一起)、端稳 (care)、
  先端到身前再走 (settle)、等它凉一点再端 (wait)、装多少 (fill: as_is / less, 只有自己装的时候能选).
- 阶段和评测台的一样, 一段接一段不重叠: 拿起 pick、挪到身前 lift (拿住到开始走; 小屋里送到嘴边也算)、走 walk、放下 put.
- 预测: 每一段洒多少 = 这一段照常端的基础风险 x 做法的折扣; 烫到多少 = 烫的基础风险 x (洒的折扣) x (等凉的折扣).
  先验是常识 (越满越容易洒; 放慢、端稳那一段减半; 等凉了烫得轻一半, 都是假设). 基础风险用全部经历一起估 (不管当时用了
  什么做法, 按折扣折算), 折扣用"用了那个做法的几次"估; 经验按情境相似程度加权 (同一种杯子 1, 换了杯子 0.6; 路线只管
  "走"那一段). 装得不一样的经历按装量的倍数折算 (半杯约是满杯的 0.3 倍): 只端过满杯的时候, 半杯按满杯的三成估, 不是一样高
  (2026-10-27 物理评测台: 满杯学来的"等凉"半杯也用上了, 白等 22 分钟). 烫按感觉分轻重: 烫伤算 1, 烫一下算 0.3.
  (不分开估的话, 用了端稳还洒的几次会让端稳看着比没试过的照常还糟, 她第三趟就退回照常又洒)
- 挑做法: 代价都折成"送到这一杯算 1 分", 和评测台的得分一个尺子 (carry_world.task_score):
  洒了少送的、要擦的, 烫到的 (按这一趟最重的那下算), 费劲, 费的时间 (每分钟 0.01, 说了不急只算四分之一),
  晚了 (超过要的时间 0.5), 等凉了不是要的热咖啡, 少装就少送; 没把握 (这种杯子、路线经历少) 的再加一点.
  最小的那个; 说得出依据 ("上次端接近满杯在拿起时洒了 -> 这次拿起放慢"; "烫到过, 不过 5 分钟内要送到, 等凉来不及, 这次不等").
  程序不变, 没有"永远慢走": 换哪个做法只来自她自己的经历和这一趟要什么.
- 不知道装了多少 (fill None): 先算看一眼值不值 (知道了能挑得更好, 省下的比看一眼费的多就先看, 见 choose 的 look).
- 每次的预测单独记下, 结果出来以后对账 (Brier 分).
"""
from __future__ import annotations

import itertools
import math

STAGES = ("pick", "lift", "walk", "put")
STAGE_ZH = {"pick": "拿起", "lift": "挪到身前", "walk": "走", "put": "放下"}
FILLS = ("empty", "low", "half", "full")
FILL_ZH = {"empty": "空杯", "low": "快见底的", "half": "大约半杯", "full": "接近满杯"}
CUP_ZH = {"mug": "马克杯", "espresso": "浓缩咖啡杯"}
CHOICES = {"hand": ("normal", "slow"), "care": (0, 1), "settle": (0, 1), "wait": (0, 1), "fill": ("as_is", "less")}
CHOICE_ZH = {("hand", "slow"): "拿放放慢", ("care", 1): "端稳", ("settle", 1): "先端到身前再走", ("wait", 1): "等它凉一点再端",
             ("fill", "less"): "少装一点"}
DEFAULT = {"hand": "normal", "care": 0, "settle": 0, "wait": 0, "fill": "as_is"}

# 先验 (常识, 不是这次的结果): 接近满杯、照常端时每一段洒的可能; 越空越小. 定得低: 没经历过的时候她照常端
# (定高了第一趟就按常识端稳, 看不出是经历让她改的)
PRIOR = {"full": {"pick": 0.06, "lift": 0.06, "walk": 0.05, "put": 0.05}}
RATIO = {"full": 1.0, "half": 0.3, "low": 0.1, "empty": 0.0}  # 装量的倍数 (先验): 装得不一样的经历按它折算
for _f in ("half", "low", "empty"):
    PRIOR[_f] = {s: p * RATIO[_f] for s, p in PRIOR["full"].items()}
NEAR = (1.0, 0.5, 0.25)  # 装量差 0 / 1 / 2 档的经历算几成 (按倍数折算以后)
HELP = 0.5          # 先验: 那一段放慢、端稳, 风险减半 (假设; 试过以后按经验)
BURN_GIVEN_SPILL = 0.5  # 热的洒出来, 碰到身上的先验
ALPHA = 2.0         # 基础风险的先验算几次的分量
BETA = 2.0          # 折扣的先验算几次的分量
CAP = 2.0           # 一段预计洒多少 (按看到的粗档折算, 不是概率) 的上限
WORSE = 1.5         # 做法的折扣最多到这么多: 试出来某个做法让那一段洒得更多也认 (先端到身前再走, 挪到身前那一段反而洒得多)
# 看到洒出来多大一股 (感知门的粗档, 和 room-liquid.js 的 AMOUNTS 一样) 算多少: 大致按看到的量 (一大股 0.6, 泼出来一大片 1.2).
# 每一档算得一样重的话, 放下多洒的 3 ml (刚好跨过一档) 和走路少洒的 47 ml 看着一样大, 她就不愿意端稳 (2026-10-27 物理评测台)
MAG = {"none": 0.0, "drops": 0.03, "little": 0.12, "some": 0.3, "stream": 0.6, "splash": 1.2, True: 0.6, False: 0.0, None: 0.0}
MAG_ZH = {"drops": "几滴", "little": "一点", "some": "一些", "stream": "一大股", "splash": "泼出来一大片"}
BURN_W = {"burn": 1.0, "hot": 0.3}  # 烫伤算 1, 烫一下算 0.3 (等凉一点, 溅上了也只是烫一下)

# -- 代价: 都折成"送到这一杯算 1 分" (和评测台的得分、carry_world.task_score 一个尺子) ------------------------------
SPILL_COST = 0.17   # 看到洒的粗档每 1 少送的 (一大股 0.6 约 40 ml, 马克杯的一成)
CLEAN_COST = 0.03   # 洒出来的还得擦 (她还不会擦, 先算一点)
BURN_COST = 0.15    # 这一趟烫伤 (1) 一次: 评测台按最重的烫伤程度 x 0.5 扣, 烫伤程度一般 0.2-0.3
EFFORT = {("hand", "slow"): 0.03, ("care", 1): 0.03, ("settle", 1): 0.02}  # 费劲 (比没经历过时按常识省下的多一点)
SECS = 13.0         # 照常端一趟多少秒 (先验; 按她自己看的钟学)
DELTA_S = {("hand", "slow"): 2.5, ("care", 1): 1.5, ("settle", 1): 1.0}  # 这几样多费几秒 (先验)
PER_MIN = 0.01      # 费的时间: 每分钟
RELAXED = 0.25      # 说了不急: 时间只算这么多
LATE = 0.5          # 晚了 (超过 by)
COLD = {True: 0.3, False: 0.0, None: 0.05}  # 等凉了: 要热的 / 凉一点也行 / 不知道
LESS = 0.6          # 少装一点: 装到六成
UNC = 0.3           # 没把握: 洒、擦、烫这几项再加这么多倍 / sqrt(2 + 相似经历折合几次)
LOOK_COST = 0.005   # 看一眼杯口 (一两秒, 加上停一下)


def _mag(o: dict) -> float:
    return MAG.get(o.get("spill"), 0.0)


def _burn(o: dict) -> float:
    return max((BURN_W.get(lv, 0.0) for _, lv in o.get("felt", [])), default=0.0)


def _jerk(o: dict) -> bool:
    """这一段手抖了一下 (动作本身的毛病, 她感觉得到): 洒了、烫了不算端法的事, 不拿去学."""
    return any(lv == "jerk" for _, lv in o.get("felt", []))


def _prod(xs) -> float:
    out = 1.0
    for x in xs:
        out *= x
    return out


def lower(fill: str) -> str:
    return FILLS[max(0, FILLS.index(fill) - 1)] if fill in FILLS else fill


def effective_fill(ctx: dict, choice: dict) -> str:
    return lower(ctx["fill"]) if choice.get("fill") == "less" and ctx.get("can_fill") else ctx["fill"]


def relevant(stage: str, choice: dict) -> tuple:
    """这一段洒多少看做法里的哪几项: 拿起看拿放快慢; 挪到身前、走看端稳和先端到身前; 放下看端稳和拿放快慢.
    (等凉一点只管烫不烫, 不管洒不洒, 见 _estimate)"""
    if stage == "pick":
        return ("hand", choice["hand"]),
    if stage in ("lift", "walk"):
        return ("care", choice["care"]), ("settle", choice["settle"])
    return ("care", choice["care"]), ("hand", choice["hand"])


def options(ctx: dict) -> list[dict]:
    keys = ["hand", "care"] + (["settle"] if ctx.get("route") != "none" else []) + (["wait"] if ctx.get("hot") else []) \
        + (["fill"] if ctx.get("can_fill") else [])
    out = []
    for vals in itertools.product(*(CHOICES[k] for k in keys)):
        c = dict(DEFAULT)
        c.update(zip(keys, vals))
        out.append(c)
    return out


def _mins(s: float) -> str:
    return f"{round(s / 60)} 分钟" if s >= 90 else f"{round(s)} 秒"


class CarryMind:
    def __init__(self):
        self.records: list[dict] = []   # {ctx, choice, outcome, ev}
        self.ledger: list[dict] = []    # 每次的预测和结果对账 {predict, outcome, brier}
        self.pending: dict | None = None

    # -- 经验 ------------------------------------------------------------------------------------
    @staticmethod
    def _near(rf: str, fill: str) -> float:
        """装量相似: 差 0 / 1 / 2 档. 空杯 (倍数 0) 的经历折算不过来, 不算."""
        if RATIO.get(rf, 0) <= 0 or RATIO.get(fill, 0) <= 0:
            return 0.0
        d = abs(FILLS.index(rf) - FILLS.index(fill))
        return NEAR[d] if d < len(NEAR) else 0.0

    def _weight(self, rec: dict, ctx: dict, fill: str, stage: str) -> float:
        r = rec["ctx"]
        w = self._near(effective_fill(r, rec["choice"]), fill)
        if r.get("cup") != ctx.get("cup"):
            w *= 0.6
        if stage == "walk" and r.get("route") != ctx.get("route"):
            w *= 0.5
        if r.get("hot") != ctx.get("hot"):
            w *= 0.8  # 冷热不影响洒不洒, 只影响烫不烫 (见 _estimate)
        return w

    @staticmethod
    def _helped(stage: str, choice: dict) -> list[tuple]:
        """这一段用了哪几个"减少风险"的做法 (拿放放慢、端稳、先端到身前)."""
        return [kv for kv in relevant(stage, choice) if kv in CHOICE_ZH]

    def _estimate(self, stage: str, ctx: dict, choice: dict) -> dict:
        """这一段: 洒的可能 (spill)、烫到的可能 (burn)、依据 (相似情境里经历了几次、洒了几次)、有多少把握 (n_eff).
        风险 = 基础风险 (照常端) x 用了的做法的折扣. 基础风险和折扣交替估两遍."""
        fill = effective_fill(ctx, choice)
        zero = {"spill": 0.0, "burn": 0.0, "n": 0, "spilled": 0, "burned": 0, "base": 0.0, "n_eff": 0.0}
        if RATIO.get(fill, 0) <= 0 or (stage == "walk" and ctx.get("route") == "none"):
            return zero  # 空杯不洒; 不走就没有"走"这一段
        p0 = PRIOR[fill][stage]
        b0 = p0 * BURN_GIVEN_SPILL if ctx.get("hot") else 0.0
        # 情境相似度只管基础风险 (换了路线、杯子, 这一段本来洒多少可能不一样); 做法的折扣 (端稳少洒几成) 换了路线、杯子照样算,
        # 只按装量相似加权. 都按相似度打折的话, 换条件时折扣退回"减半"的先验, 她挑最便宜的那个 (2026-10-27 物理评测台:
        # 换成拐弯的路线, 她只挑了"先端到身前再走", 没用上端稳)
        rows = []
        for rec in self.records:
            w = self._weight(rec, ctx, fill, stage)
            if w <= 0:
                continue
            rf = effective_fill(rec["ctx"], rec["choice"])
            k = RATIO[fill] / RATIO[rf]  # 装得不一样: 按装量的倍数折算成这个装量
            o = rec["outcome"]["stages"].get(stage) or {}
            if _jerk(o):
                continue
            rows.append((w, _mag(o) * k, (_burn(o) * k if ctx.get("hot") else 0.0), self._helped(stage, rec["choice"]),
                         self._near(rf, fill), bool(rec["choice"].get("wait")), _mag(o) > 0, _burn(o) > 0))
        # 先按"装得差不多的都算"估一个共同的基础风险 (common) 和折扣, 再拿 common 当先验估这个情境的 (换了路线、杯子时经历少,
        # 退回的是相似情境学到的, 不是"很少洒"的常识先验; 不然她会以为没走过的路线更安全, 端稳不值得)
        factors = {kv: HELP for row in rows for kv in row[3]}
        common = p0
        for _ in range(3):
            disc = lambda helped: _prod(factors[kv] for kv in helped)  # noqa: E731
            common = min(CAP, (ALPHA * p0 + sum(r[4] * r[1] for r in rows)) / (ALPHA + sum(r[4] * disc(r[3]) for r in rows)))
            for kv in factors:  # 用了这个做法的几次: 实际洒的 / 照基础风险该洒的
                used = [(r[4], r[1], r[3]) for r in rows if kv in r[3]]
                expect = sum(wf * common * _prod(factors[o] for o in h if o != kv) for wf, _, h in used)
                factors[kv] = min(WORSE, (BETA * HELP + sum(wf * s for wf, s, _ in used) / max(common, 1e-6)) / (BETA + expect / max(common, 1e-6)))
        disc = lambda helped: _prod(factors[kv] for kv in helped)  # noqa: E731
        base = min(CAP, (ALPHA * common + sum(r[0] * r[1] for r in rows)) / (ALPHA + sum(r[0] * disc(r[3]) for r in rows)))
        # 烫: 先验是洒得少烫得也少 (和洒同一个折扣), 再按烫到的那几次自己估一个 (用了端稳还是烫到, 端稳管不住这一下);
        # 等凉一点再端另有一个折扣 (只管烫). 都是试过才知道有多大用 (2026-10-27 小屋: 送到嘴边那一下溅到胸口, 和洒在哪一段不是一回事)
        bf, cool = dict(factors), HELP
        for _ in range(3):
            bdisc = lambda h, waited: _prod(bf[kv] for kv in h) * (cool if waited else 1.0)  # noqa: E731
            bcommon = (ALPHA * b0 + sum(r[4] * r[2] for r in rows)) / (ALPHA + sum(r[4] * bdisc(r[3], r[5]) for r in rows))
            bc = max(bcommon, 1e-6)
            for kv in bf:
                used = [r for r in rows if kv in r[3]]
                expect = sum(r[4] * bcommon * _prod(bf[o] for o in r[3] if o != kv) * (cool if r[5] else 1.0) for r in used)
                bf[kv] = min(WORSE, (BETA * factors[kv] + sum(r[4] * r[2] for r in used) / bc) / (BETA + expect / bc))
            used = [r for r in rows if r[5]]
            expect = sum(r[4] * bcommon * _prod(bf[o] for o in r[3]) for r in used)
            cool = min(WORSE, (BETA * HELP + sum(r[4] * r[2] for r in used) / bc) / (BETA + expect / bc))
        burn = (ALPHA * bcommon + sum(r[0] * r[2] for r in rows)) / (ALPHA + sum(r[0] * bdisc(r[3], r[5]) for r in rows))
        k = _prod(factors.get(kv, HELP) for kv in self._helped(stage, choice))
        kb = _prod(bf.get(kv, HELP) for kv in self._helped(stage, choice)) * (cool if choice.get("wait") else 1.0)
        n_eff = sum(r[0] for r in rows)  # 相似的经历折合几次: 越少越没把握
        spill, burn = min(CAP, base * k), min(0.95, burn * kb)
        return {"spill": spill, "burn": burn, "n": len(rows), "spilled": sum(1 for r in rows if r[6]),
                "burned": sum(1 for r in rows if r[7]), "base": base, "n_eff": n_eff}

    def predict(self, ctx: dict, choice: dict) -> dict:
        return {stage: self._estimate(stage, ctx, choice) for stage in STAGES}

    def secs(self, ctx: dict, choice: dict) -> float:
        """端一趟多少秒 (不算等凉): 同一种杯子、同一条路线的经历里扣掉做法多费的, 再加上这次的."""
        extra = lambda c: sum(DELTA_S.get((k, v), 0.0) for k, v in c.items())  # noqa: E731
        same = [r["outcome"]["secs"] - extra(r["choice"]) for r in self.records
                if r["ctx"].get("cup") == ctx.get("cup") and r["ctx"].get("route") == ctx.get("route") and r["outcome"].get("secs")]
        return (ALPHA * SECS + sum(same)) / (ALPHA + len(same)) + extra(choice)

    def breakdown(self, ctx: dict, choice: dict) -> dict:
        """预计的代价, 一项一项 (都按"送到这一杯算 1 分"折算)."""
        pr = self.predict(ctx, choice)
        spill = sum(p["spill"] for p in pr.values())
        burns = sorted((p["burn"] for p in pr.values()), reverse=True)
        trip_burn = min(1.0, burns[0] + 0.25 * sum(burns[1:]))  # 这一趟最重的那下 (评测台按最重的算), 别的几段烫到再算一点
        wait_s = float(ctx.get("wait_s") or 0) if choice.get("wait") else 0.0
        total_s = self.secs(ctx, choice) + wait_s
        by = ctx.get("by")
        out = {"spill": SPILL_COST * spill, "clean": CLEAN_COST * spill, "burn": BURN_COST * trip_burn,
               "effort": sum(EFFORT.get((k, v), 0.0) for k, v in choice.items()),
               "time": PER_MIN * total_s / 60 * (RELAXED if ctx.get("relaxed") else 1.0),
               "late": LATE if by is not None and total_s > float(by) else 0.0,
               "cold": COLD.get(ctx.get("hot_pref")) if choice.get("wait") else 0.0,
               "short": (1 - LESS) if choice.get("fill") == "less" and ctx.get("can_fill") else 0.0}
        # 没把握: 端过别的, 这一种 (新杯子、新路线) 相似的经历少, 洒、擦、烫这几项加一点, 小心一点.
        # 一次都没端过的时候按常识来 (常识里很少洒, 不加; 加了第一趟就"按常识端稳", 看不出是经历让她改的)
        n_eff = min((p["n_eff"] for p in pr.values() if p["spill"] > 0 or p["burn"] > 0), default=0.0)
        out["unsure"] = UNC * (out["spill"] + out["clean"] + out["burn"]) / math.sqrt(ALPHA + n_eff) if self.records else 0.0
        return {k: round(v, 4) for k, v in out.items()}

    def cost(self, ctx: dict, choice: dict) -> float:
        return sum(self.breakdown(ctx, choice).values())

    # -- 挑做法 ----------------------------------------------------------------------------------
    def _best(self, ctx: dict) -> dict:
        changes = lambda c: sum(c[k] != DEFAULT[k] for k in c)  # noqa: E731
        return min(options(ctx), key=lambda c: (round(self.cost(ctx, c), 6), changes(c)))

    def fill_belief(self, ctx: dict) -> dict:
        """没看到杯口时, 她猜装了多少: 同一种杯子、同一个人倒的那几次看到的 (加一的平滑)."""
        seen = [r["ctx"]["fill"] for r in self.records
                if r["ctx"].get("cup") == ctx.get("cup") and r["ctx"].get("source", "self") == ctx.get("source", "self")]
        cands = ("full", "half", "low")
        n = {f: 1 + seen.count(f) for f in cands}
        return {f: n[f] / sum(n.values()) for f in cands}

    def look_value(self, ctx: dict) -> dict:
        """不知道装了多少: 看一眼值多少 = 不看、按猜的挑一个做法的预计代价 - 看了再挑的预计代价."""
        belief = self.fill_belief(ctx)
        per = {f: {**ctx, "fill": f} for f in belief}
        blind = min(options(per["full"]), key=lambda c: sum(p * self.cost(per[f], c) for f, p in belief.items()))
        blind_cost = sum(p * self.cost(per[f], blind) for f, p in belief.items())
        seen_cost = sum(p * self.cost(per[f], self._best(per[f])) for f, p in belief.items())
        return {"belief": {f: round(p, 2) for f, p in belief.items()}, "value": round(blind_cost - seen_cost, 4), "blind": blind}

    def choose(self, ctx: dict) -> dict:
        """行动前: 每个做法预测一遍, 挑预计后果 + 代价最小的 (一样的话挑改动少的), 记下预测和依据.
        不知道装了多少 (fill None) 时先算看一眼值不值: 值就返回 {look: True} (看了以后带上 fill 再来), 不值就按猜的挑."""
        guessed = ctx.get("fill") is None
        if guessed:
            lv = self.look_value(ctx)
            guess = max(lv["belief"], key=lv["belief"].get)
            if lv["value"] > LOOK_COST:
                return {"look": True, "why": f"没看到装了多少 (猜{FILL_ZH[guess]}, 把握 {lv['belief'][guess]:.0%}), 装得不一样端法也不一样, "
                                             f"先看一眼 (省下的 {lv['value']:.3f} 比看一眼费的多)", "look_value": lv["value"]}
            ctx = {**ctx, "fill": guess}
            best = lv["blind"]  # 不看: 按猜的几种装量平均下来代价最小的那个 (不是按最可能的那一档)
        else:
            best = self._best(ctx)
        pr = self.predict(ctx, best)
        self.pending = {"ctx": dict(ctx), "choice": best, "predict": {s: round(p["spill"], 3) for s, p in pr.items()},
                        "why": self.why(ctx, best), "expect": self.breakdown(ctx, best), "guessed": guessed}
        return {"choice": best, "predict": self.pending["predict"], "why": self.pending["why"], "cost": round(self.cost(ctx, best), 3),
                "default_cost": round(self.cost(ctx, DEFAULT), 3), "expect": self.pending["expect"], "look": False}

    def _not_waiting(self, ctx: dict, choice: dict) -> str:
        """烫到过、能等却没等: 为什么 (要热的 / 来不及 / 等那么久不值)."""
        if not ctx.get("hot") or choice.get("wait") or not any(_burn(o or {}) > 0 for r in self.records if r["ctx"].get("hot")
                                                                 for o in r["outcome"]["stages"].values()):
            return ""
        waited = {**choice, "wait": 1}
        if ctx.get("hot_pref") is True:
            return "烫到过, 不过你要热的, 这次不等"
        b = self.breakdown(ctx, waited)
        if b["late"]:
            return f"烫到过, 不过 {_mins(float(ctx['by']))}以内要送到, 等凉 ({_mins(float(ctx.get('wait_s') or 0))}) 来不及, 这次不等"
        now = self.breakdown(ctx, choice)
        saved = now["burn"] - b["burn"]
        return f"烫到过, 不过等 {_mins(float(ctx.get('wait_s') or 0))}只少烫一点 (省 {saved:.2f}, 等的时间算 {b['time'] - now['time'] + b['cold']:.2f}), 这次不等"

    def why(self, ctx: dict, choice: dict) -> str:
        """依据: 改了的每一项, 是哪一段的哪几次经历让她这么改 (同一条经历引出的几项并成一句); 没改就说照常 (上次怎么样);
        烫到过却不等, 说为什么; 这种杯子、路线经历少, 说没把握."""
        bits: dict[str, list[str]] = {}  # 依据 -> 因为它改的几项 (没经历过的依据是空串)
        for k, v in choice.items():
            if (k, v) not in CHOICE_ZH:
                continue
            if k == "wait":  # 等凉一点: 依据是烫到过 (哪一段、哪里)
                hot = [r for r in self.records if r["ctx"].get("hot")]
                burned = [r for r in hot if any(_burn(o or {}) > 0 for o in r["outcome"]["stages"].values())]
                parts = sorted({p for r in burned for o in r["outcome"]["stages"].values() for p, lv in (o or {}).get("felt", [])
                                if lv in BURN_W})
                reason = f"以前端刚冲好的咖啡烫到过{'、'.join(parts[:3])} ({len(hot)} 趟里 {len(burned)} 趟)" if burned else ""
                bits.setdefault(reason, []).append(CHOICE_ZH[(k, v)])
                continue
            stages = [s for s in STAGES if k in dict(relevant(s, choice))] if k != "fill" else list(STAGES)
            base = dict(choice, **{k: DEFAULT[k]})
            ev = {s: self._estimate(s, ctx, base) for s in stages}
            hit = [(s, e) for s, e in ev.items() if e["spilled"]]
            hot = [(s, e) for s, e in ev.items() if e["burned"]]
            if not hit and hot:  # 没洒, 是烫到了 (小屋里送到嘴边那一下溅到胸口)
                s, e = max(hot, key=lambda se: se[1]["burned"] / max(1, se[1]["n"]))
                parts = sorted({p for r in self.records for p, lv in (r["outcome"]["stages"].get(s) or {}).get("felt", []) if lv in BURN_W})
                reason = f"以前{STAGE_ZH[s]}时烫到过{'、'.join(parts[:3])} ({e['n']} 次里 {e['burned']} 次)"
            elif hit:
                s, e = max(hit, key=lambda se: se[1]["spilled"] / max(1, se[1]["n"]))
                cups = [c for c in CUP_ZH if any(r["ctx"].get("cup") == c and _mag(r["outcome"]["stages"].get(s) or {}) > 0
                                                 and self._weight(r, ctx, effective_fill(ctx, base), s) > 0 for r in self.records)]
                moved = "" if ctx.get("cup") in cups else ", 这回换了杯子, 照着来"
                fills = sorted({effective_fill(r["ctx"], r["choice"]) for r in self.records
                                if _mag(r["outcome"]["stages"].get(s) or {}) > 0}, key=FILLS.index)
                what = FILL_ZH.get(ctx["fill"], "") if fills == [ctx["fill"]] else "、".join(FILL_ZH[f] for f in fills)
                reason = (f"以前端{what}的{'、'.join(CUP_ZH[c] for c in cups) or '杯子'}在{STAGE_ZH[s]}时洒过 "
                          f"({e['n']} 次里 {e['spilled']} 次{moved})")
            else:
                reason = ""
            bits.setdefault(reason, []).append(CHOICE_ZH[(k, v)])
        out = [f"{r} -> 这次{'、'.join(names)}" if r else f"这次{'、'.join(names)} (没经历过, 按常识)" for r, names in bits.items()]
        no_wait = self._not_waiting(ctx, choice)
        if no_wait:
            out.append(no_wait)
        jerks = sum(1 for r in self.records if any(_jerk(o or {}) for o in r["outcome"]["stages"].values()))
        if jerks:
            out.append(f"有 {jerks} 趟是手抖了一下才洒、才烫的, 那几段不算端法的事")
        if self.records and not any(r["ctx"].get("cup") == ctx.get("cup") for r in self.records):
            out.append(f"{CUP_ZH.get(ctx.get('cup'), '这种杯子')}没端过, 没把握")
        elif self.records and ctx.get("route") not in ("none", None) and not any(r["ctx"].get("route") == ctx.get("route") for r in self.records):
            out.append("这条路没走过, 没把握")
        if out:
            return "; ".join(out)
        same = [r for r in self.records if r["ctx"].get("fill") == ctx["fill"]]
        wet = sum(1 for r in same if any(_mag(o or {}) > 0 for o in r["outcome"]["stages"].values()))
        what = f"以前端{FILL_ZH.get(ctx['fill'], '')}的"
        if not same:
            return "没端过这样的, 照常端"
        if not wet:
            return f"{what}都没洒 ({len(same)} 次), 照常端"
        return f"{what}洒过 ({len(same)} 次里 {wet} 次), 不过放慢、端稳也省不了多少, 照常端"

    def prompt(self, ctx: dict) -> str:
        """这一步要是问模型, 发给它的就是这几行: 只有她的情境、这一趟要什么、经验和每个做法的预测."""
        want = [f"{_mins(float(ctx['by']))}以内要" if ctx.get("by") is not None else "没说什么时候要",
                {True: "要热的", False: "凉一点也行", None: "不知道要不要热的"}[ctx.get("hot_pref")]]
        if ctx.get("relaxed"):
            want.append("说了不急")
        route = {"none": "不用走", "turns": "路上要拐弯"}.get(ctx.get("route"), "路不远")
        lines = [f"要端{FILL_ZH.get(ctx['fill'], '')}{'热' if ctx.get('hot') else ''}的{CUP_ZH.get(ctx.get('cup'), '杯子')}"
                 f" ({route}; {', '.join(want)})."]
        for c in options(ctx):
            pr, b = self.predict(ctx, c), self.breakdown(ctx, c)
            name = "、".join(CHOICE_ZH[(k, v)] for k, v in c.items() if (k, v) in CHOICE_ZH) or "照常"
            lines.append(f"{name}: " + ", ".join(f"{STAGE_ZH[s]}洒 {pr[s]['spill']:.2f}" for s in STAGES if ctx.get("route") != "none" or s != "walk")
                         + f", 烫 {b['burn'] / BURN_COST:.2f}, 约 {_mins(self.secs(ctx, c) + (float(ctx.get('wait_s') or 0) if c.get('wait') else 0))}"
                         + f", 代价 {sum(b.values()):.3f}")
        return "\n".join(lines)

    # -- 结果 ------------------------------------------------------------------------------------
    def learn(self, outcome: dict, ev=None) -> dict:
        """端完: 收感知到的结果 ({stages: {段: {spill, drop, felt}}, delivered, secs}), 记一条经验, 和行动前的预测对账."""
        p = self.pending or {"ctx": outcome.get("ctx"), "choice": dict(DEFAULT), "predict": {}}
        self.pending = None
        ctx = dict(p["ctx"])
        if p.get("guessed") and outcome.get("start_fill") in FILLS:  # 没看杯口、按猜的端的: 拿起来的时候看到了装多少, 经历按看到的记
            ctx["fill"] = outcome["start_fill"]
        self.records.append({"ctx": ctx, "choice": p["choice"], "outcome": outcome, "ev": ev})
        brier = None
        if p.get("predict"):  # 预测的是"这一段洒多少 (按看到的粗档折算)", 和看到的比
            brier = round(sum((p["predict"][s] - _mag(outcome["stages"].get(s) or {})) ** 2 for s in STAGES) / len(STAGES), 4)
        row = {"choice": p["choice"], "predict": p.get("predict"), "spilled": [s for s in STAGES if _mag(outcome["stages"].get(s) or {}) > 0],
               "splash": {s: (outcome["stages"].get(s) or {}).get("spill") for s in STAGES if _mag(outcome["stages"].get(s) or {}) > 0},
               "brier": brier, "why": p.get("why"), "expect": p.get("expect")}
        self.ledger.append(row)
        return row

    def revoke(self, evs) -> int:
        evs = set(evs)
        n = len(self.records)
        self.records = [r for r in self.records if r["ev"] not in evs]
        return n - len(self.records)

    def state(self) -> dict:
        return {"records": self.records}
