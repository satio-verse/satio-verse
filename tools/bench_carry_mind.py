"""端咖啡里程碑 (最小后果闭环): 端咖啡的经历改变她下一次的选择. 行动前预测 -> 执行 -> 感知后果 -> 更新经验 -> 下次提前调整.

她 (wm/cognition/carry.py) 每一趟开始前自己挑做法 (拿放放慢、端稳、先端到身前、等它凉一点再端、少装一点), 环境端完一趟, 真值经感知门
(carry_world.perceive: 每一段洒没洒、液面掉了几档、哪里烫了) 变成她的经历. 对照组每一趟都是没经历过的她 (清空经验).
之后换条件各端一趟, 看经验迁不迁得过去、这一趟要什么 (什么时候要、要不要热的、不知道装了多少) 有没有让她换做法.
得分有两个: 评测台的 (送到、剩多少、烫伤), 和算上时间的 (carry_world.task_score: 再扣等凉的时间、晚了、要热的却凉了).

  python tools/bench_carry_mind.py                  # 物理评测台 (web/motion-eval.js 的 carry, 液体 web/physics/liquid.js), 自己起模拟服务 (8811)
  python tools/bench_carry_mind.py --env sim        # 示意环境 (不是物理, 数字是编的, 只看学习逻辑)
  python tools/bench_carry_mind.py --seeds 1,2,3 --trips 6 --tabs 6
结果写 logs/bench-carry-mind-<时间>.json 和 .md (评测时才看真值: 送到没有、剩多少、得分、烫伤; 她自己只看感知到的).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from wm.cognition.carry import CHOICE_ZH, STAGE_ZH, CarryMind  # noqa: E402
from wm.cognition.carry_world import WAIT_S, SimCarry, perceive, raw_from_bench, scenario, score, task_score  # noqa: E402

# 这一趟要把一满杯给你送过去 (评测台按送到的量算分): 少装就是少送, "少装一点"只在她自己装、自己喝的时候能选 (can_fill).
# 你没说什么时候要、要不要热的 (她那边是 None; 评测这边按真的: 不急着要, 也不在乎凉一点)
CTX = {"fill": "full", "cup": "mug", "hot": True, "route": "short", "can_fill": False, "wait_s": WAIT_S, "source": "self",
       "by": None, "hot_pref": None}
TASK = {"hot_truth": False}
# 换条件: (说法, 她知道的情境, 评测这边真的要什么, 真的装了多少 (她没看到杯口时))
TRANSFER = [("拐弯的路线", {"route": "turns"}, {}, None),
            ("半杯", {"fill": "half"}, {}, None),
            ("你说要热的", {"hot_pref": True}, {"hot_truth": True}, None),
            ("5 分钟以内要", {"by": 300}, {"by": 300}, None),
            ("你说不急、凉一点也行", {"relaxed": True, "hot_pref": False}, {"relaxed": True}, None),
            ("你倒的、没看杯口 (其实半杯)", {"fill": None, "source": "user"}, {}, "half")]


class PhysicsEnv:
    name, cups = "物理评测台 (web/motion-eval.js 的 carry)", False

    def __init__(self, port: int, tabs: int):
        from tune_motion import Evaluator
        self.ev = Evaluator(None, port, tabs, "silvercat.vrm")

    def run_many(self, jobs: list[tuple]) -> list[dict]:
        res = self.ev.run([{"params": {}, "seeds": [seed], "scenarios": [scenario(ctx, choice)]} for ctx, choice, seed in jobs])
        out = []
        for (ctx, choice, _), r in zip(jobs, res):
            got = [x for x in r["results"] if x.get("carry")]
            if not got:
                raise RuntimeError(f"{scenario(ctx, choice)} 没有结果: {r['results']}")
            out.append(raw_from_bench(got[0]["carry"]))
        return out

    def close(self):
        self.ev.close()


class SimEnv:
    name, cups = "示意环境 (不是物理)", True

    def __init__(self, *_):
        self.rng = {}

    def run_many(self, jobs: list[tuple]) -> list[dict]:
        return [self.rng.setdefault(seed, SimCarry(seed)).run(ctx, choice) for ctx, choice, seed in jobs]

    def close(self):
        pass


def name_of(choice: dict) -> str:
    return "、".join(CHOICE_ZH[(k, v)] for k, v in choice.items() if (k, v) in CHOICE_ZH) or "照常"


def trip_row(k, c, raw, row, task, looked=False) -> dict:
    """一趟的评测 (看真值): 送到的量按要送的量 (满杯) 算, 少装了就是少送了; 算上时间的得分按这一趟真的要什么."""
    burned = any(c2.get("level") == "burn" for st in raw["stages"].values() for c2 in st["contacts"].values())
    return {"trip": k, "choice": name_of(c["choice"]), "why": c["why"], "predict": c["predict"], "perceived_spill": row["spilled"],
            "brier": row["brier"], "delivered": raw["delivered"], "left_frac": round(raw["left"] / raw["target"], 3) if raw["target"] else 1.0,
            "secs": raw["secs"], "burn": raw.get("worstBurn") or (0.3 if burned else 0.0), "score": score(raw),
            "task_score": task_score(raw, task), "wait_min": round((raw.get("wait") or 0) / 60, 1), "looked": looked,
            "approx_contacts": raw.get("approx_contacts", False)}


def plan(mind: CarryMind, ctx: dict, actual: str | None) -> tuple[dict, dict, bool]:
    """她挑做法; 要先看一眼 (look) 的, 看到真的装了多少再挑. 返回 (她的选择, 环境照着跑的情境, 看没看)."""
    c = mind.choose(ctx)
    real = {**ctx, "fill": actual or ctx["fill"]}
    if c.get("look"):
        return mind.choose(real), real, True
    return c, real, False


def milestone(env, seeds: list[int], trips: int) -> dict:
    runs = {(kind, s): {"mind": CarryMind(), "rows": []} for kind in ("lived", "cleared") for s in seeds}
    for k in range(1, trips + 1):
        jobs, keys = [], []
        for (kind, s), run in runs.items():
            if kind == "cleared":
                run["mind"] = CarryMind()  # 每一趟都是没经历过的她
            c = run["mind"].choose(CTX)
            jobs.append((CTX, c["choice"], s * 100 + k))
            keys.append(((kind, s), c))
        for (key, c), raw in zip(keys, env.run_many(jobs)):
            run = runs[key]
            row = run["mind"].learn(perceive(raw), ev=f"{key[0]}{key[1]}-{k}")
            run["rows"].append(trip_row(k, c, raw, row, TASK))
        print(f"第 {k} 趟: " + "; ".join(f"{kind}{s} {runs[(kind, s)]['rows'][-1]['choice']} 剩 {runs[(kind, s)]['rows'][-1]['left_frac']}"
                                        for kind, s in runs), flush=True)
    transfer = list(TRANSFER) + ([("浓缩咖啡杯", {"cup": "espresso"}, {}, None)] if env.cups else [])
    moved = {}
    for label, extra, truth, actual in transfer:
        ctx, task = {**CTX, **extra}, {**TASK, **truth}
        jobs, keys = [], []
        for (kind, s), run in runs.items():
            mind = run["mind"] if kind == "lived" else CarryMind()
            c, real, looked = plan(mind, ctx, actual)
            jobs.append((real, c["choice"], s * 100 + 50))
            keys.append(((kind, s), mind, c, looked))
        moved[label] = {}
        for (key, mind, c, looked), raw in zip(keys, env.run_many(jobs)):
            row = mind.learn(perceive(raw), ev=f"{key[0]}{key[1]}-{label}")
            moved[label].setdefault(key[0], []).append(trip_row(label, c, raw, row, task, looked))
        print(f"{label}: " + "; ".join(f"{k} {' / '.join(sorted({r['choice'] for r in v}))}" for k, v in moved[label].items()), flush=True)
    return {"env": env.name, "ctx": CTX, "seeds": seeds, "trips": trips,
            "runs": {f"{kind}{s}": run["rows"] for (kind, s), run in runs.items()}, "transfer": moved,
            "records": {f"lived{s}": runs[("lived", s)]["mind"].records for s in seeds}}  # 有经验的她最后的全部经历 (查问题用)


def summary(rows: list[dict]) -> dict:
    mean = lambda k: round(statistics.mean(r[k] for r in rows if r[k] is not None), 3) if rows else None  # noqa: E731
    return {"n": len(rows), "delivered": mean("delivered"), "left_frac": mean("left_frac"), "secs": mean("secs"), "burn": mean("burn"),
            "wait_min": mean("wait_min"), "score": mean("score") if all(r["score"] is not None for r in rows) else None,
            "task_score": mean("task_score"), "looked": sum(1 for r in rows if r.get("looked")),
            "spill_stages": round(statistics.mean(len(r["perceived_spill"]) for r in rows), 2)}


def report(res: dict) -> str:
    seeds, trips = res["seeds"], res["trips"]
    lines = [f"# 端咖啡里程碑 {datetime.now():%Y-%m-%d %H:%M}", "",
             f"环境: {res['env']}. 给你端一满杯刚冲好的热咖啡 (马克杯、短路线; 你没说什么时候要、要不要热的). 种子 {seeds}, 每组 {trips} 趟. "
             "\"有经验\"每一趟按以前感知到的后果自己挑做法; \"清空经验\"每一趟都是没经历过的她. "
             "得分: 评测台的 / 算上时间的 (再扣等凉和端的时间每分钟 0.01、晚了 0.5、要热的却凉了 0.3).", "",
             "| 趟 | 有经验: 做法 | 剩 | 用时 | 等凉 (分钟) | 烫伤 | 得分 | 算上时间 | 清空经验: 做法 | 剩 | 烫伤 | 得分 | 算上时间 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k in range(trips):
        a = [res["runs"][f"lived{s}"][k] for s in seeds]
        b = [res["runs"][f"cleared{s}"][k] for s in seeds]
        sa, sb = summary(a), summary(b)
        names = lambda rows: " / ".join(sorted({r["choice"] for r in rows}))  # noqa: E731
        lines.append(f"| {k + 1} | {names(a)} | {sa['left_frac']:.0%} | {sa['secs']} | {sa['wait_min']} | {sa['burn']} | {sa['score']} | "
                     f"{sa['task_score']} | {names(b)} | {sb['left_frac']:.0%} | {sb['burn']} | {sb['score']} | {sb['task_score']} |")
    later = lambda kind: summary([r for s in seeds for r in res["runs"][f"{kind}{s}"][2:]])  # noqa: E731
    la, lb = later("lived"), later("cleared")
    lines += ["", f"第 3 趟起 (有经验 / 清空): 剩 {la['left_frac']:.0%} / {lb['left_frac']:.0%}, 用时 {la['secs']} / {lb['secs']} 秒 "
              f"(另外等凉平均 {la['wait_min']} / {lb['wait_min']} 分钟), 烫伤 {la['burn']} / {lb['burn']}, 评测台得分 {la['score']} / {lb['score']}, "
              f"算上时间 {la['task_score']} / {lb['task_score']}, 感知到洒的段数 {la['spill_stages']} / {lb['spill_stages']}.", "",
              "她说的依据 (有经验, 第一个种子):", ""]
    for r in res["runs"][f"lived{seeds[0]}"]:
        lines.append(f"- 第 {r['trip']} 趟 {r['choice']}: {r['why']} -> 感知到洒在: "
                     f"{'、'.join(STAGE_ZH[s] for s in r['perceived_spill']) or '没洒'} (预测得分 Brier {r['brier']})")
    lines += ["", "换条件 (有经验的她 vs 没经历过的她, 各端一趟):", "",
              "| 条件 | 有经验: 做法 | 剩 | 等凉 (分钟) | 烫伤 | 算上时间 | 清空: 做法 | 剩 | 等凉 (分钟) | 烫伤 | 算上时间 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, by in res["transfer"].items():
        a, b = by["lived"], by["cleared"]
        sa, sb = summary(a), summary(b)
        look = lambda x: f" (先看了一眼 {x['looked']}/{x['n']})" if x["looked"] else ""  # noqa: E731
        lines.append(f"| {label} | {' / '.join(sorted({r['choice'] for r in a}))}{look(sa)} | {sa['left_frac']:.0%} | {sa['wait_min']} | "
                     f"{sa['burn']} | {sa['task_score']} | {' / '.join(sorted({r['choice'] for r in b}))}{look(sb)} | {sb['left_frac']:.0%} | "
                     f"{sb['wait_min']} | {sb['burn']} | {sb['task_score']} |")
    lines += ["", "换条件时她说的依据 (有经验, 第一个种子):", ""]
    for label, by in res["transfer"].items():
        lines.append(f"- {label}: {by['lived'][0]['choice']}: {by['lived'][0]['why']}")
    if any(r.get("approx_contacts") for rows in res["runs"].values() for r in rows):
        lines += ["", "注: 评测台还没按阶段记碰到身上的刺激, 烫到算在洒得最多的那一段 (approx_contacts)."]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", choices=["physics", "sim"], default="physics")
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--trips", type=int, default=6)
    ap.add_argument("--port", type=int, default=8811)
    ap.add_argument("--tabs", type=int, default=6)
    ap.add_argument("--out", default=str(ROOT / "logs"))
    a = ap.parse_args()
    env = (PhysicsEnv if a.env == "physics" else SimEnv)(a.port, a.tabs)
    try:
        res = milestone(env, [int(s) for s in a.seeds.split(",")], a.trips)
    finally:
        env.close()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    (out / f"bench-carry-mind-{stamp}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    md = report(res)
    (out / f"bench-carry-mind-{stamp}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"结果: {out / f'bench-carry-mind-{stamp}.md'}")


if __name__ == "__main__":
    main()
