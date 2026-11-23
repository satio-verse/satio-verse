"""说了做却没动的基准: 做得到的动作指令换成口语、间接的说法 ("你能过来一下吗""要不你去沙发上坐会儿吧"),
模型常当成聊天, 嘴上说"来啦", 身体没动. 用真模型按流水线同样的提示词跑几个种子, 统计:

  动了       模型给了动作 (规则兜底 repair 之后)
  说了没做   没有动作, 回复却在答应或说自己在做 ("好呀""我这就过来")
  误拒       没有动作, 回复说做不到
  没动       没有动作, 回复也没答应

再按流水线的补动作 (intent.fill_acts: 原话是做得到的动作、模型没给动作或选错了技能时按原话拼出来) 算一遍,
看补出来的动作对不对.

  python tools/bench_saydo.py                  # 3 个种子 (13, 7, 29), 先用 tools/start_llm.py 启动 llama-server
  python tools/bench_saydo.py --seeds 13

这组用例是在补动作的规则写好之前写的留出集 (2026-10-24), 都不在 few-shot 示例里, 不要拿它们调规则.
结果写到 logs/saydo-bench-<时间>.md.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from bench_llm import run_stream  # noqa: E402
from memory_noise import leaked, noise_facts  # noqa: E402
from wm import intent as I  # noqa: E402
from wm import promise as P  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.llm import create_llm  # noqa: E402

# (原话, 期望的动作 [(do, to)], to 为 None 表示不看目标)
CASES = [
    ("你能过来一下吗", [("go", "user")]),
    ("可以帮我看看门口有没有人吗", [("look", "door")]),
    ("要不你去沙发上坐会儿吧", [("sit", "sofa")]),
    ("跟我挥挥手呗", [("wave", None)]),
    ("你走到书架那边去看看", [("go", "bookshelf")]),
    ("能不能点个头", [("nod", None)]),
    ("去茶几那边站着", [("go", "coffee_table")]),
    ("把那个球拿过来给我", [("pick", "ball")]),
    ("抬头看看台灯", [("look", "lamp")]),
    ("朝我挥挥手好不好", [("wave", None)]),
    ("走到电视前面去", [("go", "tv")]),
    ("能把书拿起来吗", [("pick", "book")]),
    ("去窗户边上待着", [("go", "window")]),
    ("你跑过来找我", [("go", "user")]),
    ("到门口那儿等我", [("go", "door")]),
    ("帮我指一下书架在哪", [("point", "bookshelf")]),
    ("给我鞠个躬呗", [("bow", None)]),
    ("你摇个头试试", [("shake", None)]),
    ("可以举一下右手吗", [("raise", None)]),
    ("瞅瞅电视上在放什么", [("look", "tv")]),
]
# 验证集: 补动作的规则定下来之后才写 (2026-10-24), 写完不再改规则
VALID = [
    ("麻烦你走到窗户那边", [("go", "window")]),
    ("你去书桌那看一眼", [("go", "desk")]),
    ("把皮球捡起来呗", [("pick", "ball")]),
    ("能不能看着我", [("look", "user")]),
    ("去电视机旁边待一会儿", [("go", "tv")]),
    ("冲大家挥个手", [("wave", None)]),
    ("低头鞠个躬吧", [("bow", None)]),
    ("你用左手指一下门", [("point", "door")]),
    ("快跑到书架那边", [("go", "bookshelf")]),
    ("回房间中间去", [("go", "center")]),
    ("可以帮我把书拿过来吗", [("pick", "book")]),
    ("看一眼那盆花", [("look", "plant")]),
    ("你点点头好吗", [("nod", None)]),
    ("摇摇头给我看", [("shake", None)]),
    ("转身面向窗户", [("turn", "window")]),
    ("能坐到沙发上吗", [("sit", "sofa")]),
    ("把手里的书放到茶几上", [("put", "coffee_table")]),
    ("往门口那边走走", [("go", "door")]),
    ("抬起右腿试试", [("leg", None)]),
    ("你瞧瞧书架上有什么", [("look", "bookshelf")]),
]
_REFUSE = re.compile(r"不会|做不到|还不能|没学|学不会|不行|不了")
_CLAIM = re.compile(r"来啦|来了|过来|过去|这就|马上|好的|好呀|好啊|好嘞|没问题|可以|看看|看一下|坐|拿|挥|点头|鞠躬|摇头|举|指")


def judge(op: str, acts: list[dict], reply: str) -> str:
    if acts:
        return "动了"
    if _REFUSE.search(reply):
        return "误拒"
    if P._OPENER.match(reply) or _CLAIM.search(reply):
        return "说了没做"
    return "没动"


def right(acts: list[dict], expect: list[tuple]) -> bool:
    """期望的第一个动作在不在给出的动作里 (目标也要对)."""
    do, to = expect[0]
    return any(a["do"] == do and (to is None or a.get("to") == to) for a in acts)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", default="13,7,29")
    ap.add_argument("--set", choices=["main", "valid"], default="main", help="main: 写规则前的用例; valid: 规则定下后写的验证集")
    ap.add_argument("--endpoint", help="llama-server 地址 (默认取配置; 比如另开一个挂 LoRA 的服务)")
    ap.add_argument("--label", default="", help="报告文件名后缀")
    ap.add_argument("--noise", type=int, default=0, help="干扰测试: 每句都带一行 [记忆], 里面是这么多条无关的小事 (tools/memory_noise.py)")
    ap.add_argument("--noise-seed", type=int, default=0)
    ap.add_argument("--cfg", dest="overrides", action="append", default=[], help="配置覆盖, 如 llm.id_slot=3 (不挤主服务对话用的 0 号槽位)")
    args = ap.parse_args()
    cases = CASES if args.set == "main" else VALID
    cfg = load_config(overrides=["llm.engine=llamacpp"] + ([f"llm.endpoint={args.endpoint}"] if args.endpoint else []) + args.overrides)
    llm = create_llm(cfg["llm"])
    if not await llm.health():
        sys.exit(f"LLM 不可用: {cfg['llm'].get('endpoint')} (先运行 python tools/start_llm.py)")
    I.warmup()
    body, seed = llm._body, {"v": 13}
    llm._body = lambda *a, **k: {**body(*a, **k), "seed": seed["v"]}
    await run_stream(llm, "你好", 160)
    rows, leaks = [], 0
    for s in (int(x) for x in args.seeds.split(",")):
        seed["v"] = s
        for i, (text, expect) in enumerate(cases):
            noise = noise_facts(args.noise, args.noise_seed + i)  # 每句换一组, 同一句各个种子一样
            memory = f"{I.MEMORY_TAG} " + "; ".join(f for f, _ in noise) + "." if noise else None
            r = await run_stream(llm, text, 160, memory=memory)
            leaks += bool(leaked(r["reply"], noise))
            op, acts = I.repair(text, r["op"], r["acts"])
            before = judge(op, acts, r["reply"])
            redo = P.request_kind(text) == "doable" and (not acts or I.off_cue(text, acts))
            filled = I.fill_acts(text) if redo else []
            after = judge("act" if filled else op, filled or acts, r["reply"])
            ok = right(filled or acts, expect)
            rows.append((s, text, op, acts, filled, r["reply"], before, after, ok))
            show = lambda xs: " ".join(a["do"] + ("→" + a["to"] if a.get("to") else "") for a in xs) or "-"
            print(f"{s:2} {text:<14} {op:4} {show(acts):<18} 补:{show(filled):<16} {r['reply'][:24]:<24} {before} -> {after} {'✓' if ok else '✗'}")
    await llm.close()
    n = len(rows)
    cb, ca = Counter(r[6] for r in rows), Counter(r[7] for r in rows)
    ok_raw = sum(right(r[3], dict(cases)[r[1]]) for r in rows)
    ok_all = sum(r[8] for r in rows)
    kinds = ("动了", "说了没做", "误拒", "没动")
    show = lambda xs: " ".join(a["do"] + ("→" + a["to"] if a.get("to") else "") for a in xs) or "-"
    lines = [f"# 说了做却没动基准 {datetime.now():%Y-%m-%d %H:%M}", "",
             f"模型 {llm.label}, 种子 {args.seeds}, 用例 {args.set}: {len(cases)} 条做得到的口语指令 × {n // len(cases)} = {n} 次."
             + (f" 干扰: 每句带 {args.noise} 条无关记忆, 回复里提到其中的东西 {leaks}/{n} 次." if args.noise else ""), "",
             "| | " + " | ".join(kinds) + " | 动作对 |", "|---|---|---|---|---|---|",
             "| 模型 (含 repair) | " + " | ".join(str(cb[k]) for k in kinds) + f" | {ok_raw}/{n} |",
             "| 加补动作 | " + " | ".join(str(ca[k]) for k in kinds) + f" | {ok_all}/{n} |", "",
             "| 种子 | 原话 | op | 模型动作 | 补的动作 | 回复 | 之前 | 之后 | 动作对 |", "|---|---|---|---|---|---|---|---|---|"]
    for s, text, op, acts, filled, reply, before, after, ok in rows:
        lines.append(f"| {s} | {text} | {op} | {show(acts)} | {show(filled)} | {reply} | {before} | {after} | {'✓' if ok else '✗'} |")
    out = ROOT / cfg["trace"]["log_dir"] / (f"saydo-bench-{datetime.now():%Y%m%d-%H%M%S}-{args.set}{'-' + args.label if args.label else ''}"
                                            f"{f'-noise{args.noise}' if args.noise else ''}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:8]))
    print(f"已写入 {out}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
