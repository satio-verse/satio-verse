"""小屋"接下来做什么" (room_think -> room_plan) 的离线评测: 几组固定的心情 / 需求 / 体感, 用真模型按服务端同样的提示词生成,
看挑得对不对 (缺哪项挑没挑补它的)、那句心里话能不能用、照抄示例没有、多快. 直接连 llama-server, 不经过 8790, 不进任何对话.

  python tools/bench_room_plan.py                     # 服务端现在的做法 (rank), 默认 config 的 llm.endpoint (8784)、room.think.id_slot 槽位, 每组 3 次
  python tools/bench_room_plan.py --variant base      # 对照: 全部候选、不点明最缺的
  python tools/bench_room_plan.py --temperature 0.3 --reps 5

变体: base 全部候选 + 示例; hint 再点明最缺的那项和能补它的; rank 再按需求排序只留前 room.think.max_options 件 (服务端用的);
zero 同 rank 但不带示例.

结果写到 logs/room-plan-bench-<时间>.md.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm import room_plan as R  # noqa: E402
from wm.cloud import with_profile  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.llm import LlamaCppLLM  # noqa: E402

ALL = ["computer", "read", "rest", "coffee", "window", "books", "visit", "drink", "snack", "yoga", "tidy"]
# (名字, 小时, 上行 room_think, 该补的需求 (None: 不缺, 挑什么都行), 不该挑的)
CASES = [
    ("渴了", 15, {"mood": "thirsty", "needs": {"energy": 70, "food": 66, "water": 22, "fun": 60, "social": 58},
                 "options": ALL, "recent": [{"id": "read", "ok": True}]}, "water", []),
    ("饿了", 12, {"mood": "hungry", "needs": {"energy": 66, "food": 20, "water": 60, "fun": 55, "social": 60},
                 "options": ALL, "recent": [{"id": "computer", "ok": True}]}, "food", []),
    ("想你了", 20, {"mood": "lonely", "needs": {"energy": 60, "food": 70, "water": 68, "fun": 62, "social": 18},
                   "options": ALL, "recent": [{"id": "yoga", "ok": True}, {"id": "read", "ok": True}]}, "social", []),
    ("无聊", 16, {"mood": "bored", "needs": {"energy": 72, "food": 70, "water": 65, "fun": 20, "social": 55},
                 "options": [o for o in ALL if o != "computer"], "recent": [{"id": "computer", "ok": True}]}, "fun", []),
    ("刚摔了", 21, {"mood": "low", "needs": {"energy": 55, "food": 62, "water": 60, "fun": 42, "social": 50},
                   "options": [o for o in ALL if o != "yoga"], "recent": [{"id": "yoga", "ok": False}],
                   "body": {"injured": "屁股", "falls": [{"act": "yoga"}]},
                   "practice": [{"act": "yoga", "skill": "treeR", "n": 6, "avg": 0.12, "better": False, "falls": 1, "pain": 0}]},
     None, []),
    ("困了", 23, {"mood": "sleepy", "needs": {"energy": 15, "food": 60, "water": 62, "fun": 55, "social": 50},
                 "options": ["rest", "read", "window", "visit", "drink", "tidy"], "recent": [{"id": "read", "ok": True}]}, "energy", ["tidy"]),
    ("都不缺", 10, {"mood": "happy", "needs": {"energy": 85, "food": 80, "water": 78, "fun": 76, "social": 80},
                   "options": ALL, "recent": [{"id": "window", "ok": True}]}, None, ["window"]),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint")
    ap.add_argument("--slot", type=int)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--variant", choices=["base", "hint", "rank", "zero"], default="rank")
    ap.add_argument("--persona", default="", help="人设文字 (模拟面板里的人设预设)")
    ap.add_argument("--no-remind", action="store_true", help="有人设时不在本次状态末尾提醒口吻 (对照)")
    args = ap.parse_args()
    cfg = load_config()
    rc = cfg["room"]["think"]
    llm = LlamaCppLLM({**cfg["llm"], "endpoint": args.endpoint or cfg["llm"]["endpoint"]})
    acts = R.load_acts(rc["scene"])
    temp = rc["temperature"] if args.temperature is None else args.temperature
    slot = rc["id_slot"] if args.slot is None else args.slot
    rc = {**rc, "temperature": temp, "id_slot": slot}
    v = args.variant
    examples = {t["pick"] + t["thought"] for _, t in R.EXAMPLES}
    picks: dict[str, set] = {}
    rows, stats = [], {"n": 0, "valid": 0, "fit": 0, "fit_n": 0, "thought": 0, "copied": 0, "ms": []}
    for name, hour, msg, need, avoid in CASES:
        for _ in range(args.reps):
            req = R.parse_request(msg, acts)
            if v in ("rank", "zero"):
                req = R.shortlist(req, acts, rc.get("max_options", 5))
            msgs = with_profile(R.build_messages(req, acts, hour, examples=v != "zero", hint=v != "base",
                                                 persona=bool(args.persona) and not args.no_remind), args.persona)
            t0 = time.perf_counter()
            raw = await R.ask_local(llm, msgs, req["options"], rc)
            ms = round((time.perf_counter() - t0) * 1000)
            plan = R.parse_output(raw, req, acts)
            stats["n"] += 1
            stats["ms"].append(ms)
            fit = None
            if plan:
                picks.setdefault(name, set()).add(plan["pick"])
                stats["valid"] += 1
                stats["thought"] += bool(plan["thought"])
                stats["copied"] += (plan["pick"] + plan["thought"]) in examples
                # 挑对: 补那项补得够多 (12 分以上, 或者候选里没有更多的), 不是不该挑的
                best = max(acts[o]["gives"].get(need, 0) for o in msg["options"]) if need else 0
                fit = (acts[plan["pick"]]["gives"].get(need, 0) >= min(12, best) if need else True) and plan["pick"] not in avoid
                if need:
                    stats["fit_n"] += 1
                    stats["fit"] += fit
            rows.append(f"| {name} | {plan and plan['pick']} | {plan and plan['thought']} | {plan and ','.join(plan['how'])} | "
                        f"{'' if fit is None else '对' if fit else '不对'} | {ms} | {req['options'].index(plan['pick']) + 1 if plan else ''} |")
            print(rows[-1], flush=True)
    await llm.close()
    ms = sorted(stats["ms"])
    summary = (f"- 模型 {cfg['llm'].get('model_label')}, 槽位 {slot}, 温度 {temp}, 变体 {args.variant}, 人设 {len(args.persona)} 字, "
               f"每组 {args.reps} 次, 共 {stats['n']} 次\n"
               f"- 输出可用 {stats['valid']}/{stats['n']}; 有需要补的组里挑对 {stats['fit']}/{stats['fit_n']}; "
               f"心里话可用 {stats['thought']}/{stats['valid']}; 照抄示例 {stats['copied']}\n"
               f"- 各组挑过的: " + "; ".join(f"{k} {'/'.join(sorted(v))}" for k, v in picks.items()) + "\n"
               f"- 耗时 中位 {ms[len(ms) // 2]} ms, 最慢 {ms[-1]} ms (第一次含前缀预填)\n")
    print(summary)
    out = ROOT / "logs" / f"room-plan-bench-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("# 小屋接下来做什么 · 评测\n\n" + summary + "\n| 组 | 挑的 | 心里话 | 做法 | 对不对 | ms | 第几个候选 |\n|---|---|---|---|---|---|---|\n"
                   + "\n".join(rows) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    asyncio.run(main())
