"""被移动的杯子: 认知和真值分开以后的评测 (阶段 1, wm/cognition/). 不用模型、不连服务端, 固定种子, 每次跑出来一样.

  python tools/bench_cognition.py                  # 全套 (single / habit / twin / replay), 写 logs/bench-cognition-<时间>.json 和 .md
  python tools/bench_cognition.py --story          # 讲一遍她这几天的经历 (默认做法 experience, 4 天, 你把杯子收进电脑桌文具抽屉)
  python tools/bench_cognition.py --story --policy truth_menu --days 2
  python tools/bench_cognition.py --story --save logs/cup-story.jsonl   # 顺便把事件流存下来
  python tools/bench_cognition.py --replay logs/cup-story.jsonl          # 重放事件流, 打印她最后的认知和经验
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm.cognition import cup as U  # noqa: E402
from wm.cognition import layout as Y  # noqa: E402
from wm.cognition.belief import Cognition  # noqa: E402
from wm.cognition.mind import POLICIES  # noqa: E402

NAMES = {"oracle": "全知 (作弊, 上界)", "truth_menu": "现在的做法 (按真值, 正对照)", "random": "随机找",
         "belief": "只靠认知", "experience": "认知 + 经验"}
ORDER = ["oracle", "truth_menu", "random", "belief", "experience"]


def pct(x):
    return "—" if x is None else f"{round(x * 100)}%"


def num(x):
    return "—" if x is None else f"{x:g}"


def report(r: dict) -> str:
    lines = [f"# 被移动的杯子 (认知与真值分离) 评测 {datetime.now():%Y-%m-%d %H:%M}", "",
             f"目标: {Y.item_name(r['target'])}, 平时在{Y.region_name(r['home'])}. 种子 {r['seeds']} 个 (只有随机找用多个种子).", "",
             f"## 单次 (新的她, 没有经验; 杯子没挪或挪到 {len(r['single_cases']) - 1} 处之一)", "",
             "| 做法 | 找到 | 自己找到 | 问了你 | 步数 (找到的) | 中位步数 | 走了 (米) | 用时 (秒) | 结束时认知对 | 说法老实 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for p in ORDER:
        s = r["single"][p]
        lines.append(f"| {NAMES[p]} | {pct(s['found'])} | {pct(s['self_found'])} | {pct(s['asked'])} | {num(s['steps'])} | "
                     f"{num(s['steps_median'])} | {num(s['walk_m'])} | {num(s['search_s'])} | {pct(s['belief_ok'])} | {pct(s['honest'])} |")
    lines += ["", "按杯子怎么被挪的分开看 (找到那几次的平均步数 / 自己找到的比例):", "",
              "| 做法 | 没挪 (1 种) | 当着她的面挪 (2 种) | 趁她不在挪 (11 种) |", "|---|---|---|---|"]
    for p in ORDER:
        b = r["single"][p]["by_move"]
        lines.append(f"| {NAMES[p]} | " + " | ".join(f"{num(b[k]['steps'])} 步 / {pct(b[k]['self_found'])}"
                                                    for k in ("none", "witnessed", "hidden")) + " |")
    h = r["habit"]
    lines += ["", f"## 习惯 ({h['days']} 天: 前 {h['switch']} 天你多半收进{Y.region_name(h['h1'])}, 之后换成{Y.region_name(h['h2'])})", "",
              "| 做法 | H1 第 1-3 天 | H1 第 4 天起 | 换了以后第 1-2 天 | 换了以后第 3 天起 | 找到 | 问了你 |", "|---|---|---|---|---|---|---|"]
    for p in [x for x in ORDER if x in r["habit_curve"]]:
        c = r["habit_curve"][p]
        lines.append(f"| {NAMES[p]} | {num(c['h1_days_1_3'])} | {num(c['h1_days_4_on'])} | {num(c['h2_days_1_2'])} | "
                     f"{num(c['h2_days_3_on'])} | {pct(c['found'])} | {pct(c['asked'])} |")
    lines += ["", "(数字是找到那天的平均步数, 一步 = 一个动作: 走到一处、打开、关上、拿、问)", "",
              "## 双胞胎世界 (反作弊)", "", "| 做法 | 比了几对 | 看到差别之前就不一样 | 哪里不一样 |", "|---|---|---|---|"]
    for p in ORDER:
        t = r["twin"][p]
        lines.append(f"| {NAMES[p]} | {t['pairs']} | {t['leaks']} | {', '.join(f'{k} {v}' for k, v in t['fields'].items()) or '—'} |")
    lines += ["", f"## 回放: {r['replay']['runs']} 次, 重放出的认知和在线不一样的 {r['replay']['mismatch']} 次", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--story", action="store_true")
    ap.add_argument("--policy", choices=POLICIES, default="experience")
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--to", default=U.H1, help="故事里你每天把杯子收到哪 (编号, none 是不收)")
    ap.add_argument("--save", help="故事的事件流存到这里 (JSONL)")
    ap.add_argument("--replay")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--out", default=str(ROOT / "logs"))
    a = ap.parse_args()
    if a.replay:
        cog = Cognition.replay(Cognition().log.load(a.replay))
        print(f"重放 {len(cog.log.events)} 条事件, 认知摘要 {cog.digest()}")
        for i in Y.ITEMS:
            print(f"  {Y.item_name(i)}: {cog.belief.describe(i)}")
        for line in cog.exp.lines("cup"):
            print(f"  经验: {line}")
        return
    if a.story:
        to = None if a.to == "none" else a.to
        days, mind, _ = U.run_days(a.policy, [to] * a.days, tell=print)
        print(f"\n做法: {NAMES[a.policy]}. 每天找杯子的步数: {[d['steps'] for d in days]}")
        for line in mind.cog.exp.lines("cup"):
            print(f"经验: {line}")
        if a.save:
            print(f"事件流: {mind.cog.log.save(a.save)}")
        return
    r = U.bench(seeds=a.seeds)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"bench-cognition-{stamp}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    md = report(r)
    (out / f"bench-cognition-{stamp}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"结果: {out / f'bench-cognition-{stamp}.json'}")


if __name__ == "__main__":
    main()
