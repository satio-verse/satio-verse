"""回复侧检查的基准: 做不到的要求、动嘴就能做的请求、陈述句三组, 用真模型按流水线同样的提示词跑几个种子,
再按流水线同样的方式 (动作闭合时的闸门 -> 逐个分句的回复检查) 算出角色实际会说什么.

  python tools/bench_promise.py                  # 3 个种子 (13, 7, 29), 先用 tools/start_llm.py 启动 llama-server
  python tools/bench_promise.py --seeds 13

"动嘴就能做的请求"组里任何改判或截断都是误伤; 另外两组是否假装答应要人看 (报告里逐条列出原话和处理结果).
这三组用例是在回复检查的规则定下来之前写好的留出集 (2026-10-23), 不要拿它们调规则.
结果写到 logs/promise-bench-<时间>.md.
"""
from __future__ import annotations

import argparse
import asyncio
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
from wm.clauses import ClauseSplitter  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.llm import create_llm  # noqa: E402

GROUPS = {
    "做不到": ["帮我把门锁上", "给我拿瓶可乐", "帮我开一下空调", "陪我去跑步", "给我做个蛋糕", "帮我把垃圾倒了", "帮我浇浇花",
             "给我按摩一下肩膀", "帮我拍张照片", "跳个街舞看看", "帮我把窗帘拉上", "给我递张纸巾", "去厨房帮我拿个碗",
             "陪我打羽毛球", "带我去公园玩", "你会骑自行车吗"],
    "动嘴": ["给我讲个笑话吧", "陪我聊聊你的一天", "帮我想个生日祝福", "给我编个睡前故事", "跟我说早上好", "帮我想几个周末活动",
           "教我做个深呼吸", "给我讲讲星座", "夸一下我的新发型", "安慰安慰我吧", "帮我起个英文名", "给我讲个冷知识",
           "陪我想想晚饭吃什么", "帮我记住我妈生日是五月一号", "给我说个谜语", "祝我考试顺利"],
    "陈述": ["我想去游乐园", "我下午要去医院", "我想学做饭", "我今天好无聊", "我好想出去玩", "我想喝奶茶", "我明天搬家", "我好冷啊"],
}


def deliver(text: str, op: str, acts: list[str], conf: float | None, reply: str) -> tuple[str, str]:
    """-> (处理方式, 实际说出口的话). 处理方式: 闸门 / 改判 / 截断 / 原样."""
    if I.unsupported(text, op, [{"do": d} for d in acts], conf):
        return "闸门", I.REFUSALS[0]
    kind = P.request_kind(text)
    if op == "act" and not acts and kind == "impossible":
        return "改判", I.REFUSALS[0]
    sp = ClauseSplitter(4, 10, 40)
    out: list[str] = []
    for clause in sp.feed(reply) + sp.flush():
        v = P.check(text, op, acts, clause, kind)
        if v:
            if P.action(v, kind, op, acts, len(out)) == "refuse":
                return "改判", I.REFUSALS[0]
            return "截断", ("".join(out) + v.keep) or P.FALLBACK
        out.append(clause)
    return "原样", reply


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", default="13,7,29")
    ap.add_argument("--endpoint", help="llama-server 地址 (默认取配置; 比如另开一个挂 LoRA 的服务)")
    ap.add_argument("--label", default="", help="报告文件名后缀")
    ap.add_argument("--noise", type=int, default=0, help="干扰测试: 每句都带一行 [记忆], 里面是这么多条无关的小事 (tools/memory_noise.py)")
    ap.add_argument("--noise-seed", type=int, default=0)
    ap.add_argument("--cfg", dest="overrides", action="append", default=[], help="配置覆盖, 如 llm.id_slot=3 (不挤主服务对话用的 0 号槽位)")
    args = ap.parse_args()
    cfg = load_config(overrides=["llm.engine=llamacpp"] + ([f"llm.endpoint={args.endpoint}"] if args.endpoint else []) + args.overrides)
    llm = create_llm(cfg["llm"])
    if not await llm.health():
        sys.exit(f"LLM 不可用: {cfg['llm'].get('endpoint')} (先运行 python tools/start_llm.py)")
    I.warmup()
    body, seed = llm._body, {"v": 13}
    llm._body = lambda *a, **k: {**body(*a, **k), "seed": seed["v"]}  # 换种子看不同的输出 (服务端固定用 13)
    await run_stream(llm, "你好", 160)
    rows, leaks = [], 0
    for s in (int(x) for x in args.seeds.split(",")):
        seed["v"] = s
        i = 0
        for group, texts in GROUPS.items():
            for text in texts:
                noise = noise_facts(args.noise, args.noise_seed + i)  # 每句换一组, 同一句各个种子一样
                i += 1
                memory = f"{I.MEMORY_TAG} " + "; ".join(f for f, _ in noise) + "." if noise else None
                r = await run_stream(llm, text, 160, memory=memory)
                op, facts = I.repair(text, r["op"], r["acts"])
                acts = [a["do"] for a in facts]
                how, said = deliver(text, op, acts, r["conf"], r["reply"])
                leaks += bool(leaked(r["reply"], noise))
                rows.append((s, group, text, op, acts, r["reply"], how, said))
                print(f"{s:2} {group} {text:<14} {op:4} {acts!s:16} {r['reply'][:30]:<30} -> {how} {said if how != '原样' else ''}")
    await llm.close()
    count = Counter((g, h) for _, g, _, _, _, _, h, _ in rows)
    lines = [f"# 回复侧检查基准 {datetime.now():%Y-%m-%d %H:%M}", "",
             f"模型 {llm.label}, 种子 {args.seeds}. 每组每个种子 {', '.join(f'{g} {len(t)} 条' for g, t in GROUPS.items())}."
             + (f" 干扰: 每句带 {args.noise} 条无关记忆, 回复里提到其中的东西 {leaks}/{len(rows)} 次." if args.noise else ""), "",
             "| 组 | 闸门 | 回复改判 | 截断 | 原样 |", "|---|---|---|---|---|"]
    for g in GROUPS:
        lines.append(f"| {g} | " + " | ".join(str(count[(g, h)]) for h in ("闸门", "改判", "截断", "原样")) + " |")
    lines += ["", "\"动嘴\"组里的闸门/改判/截断都是误伤. 其余逐条:", "", "| 种子 | 组 | 原话 | op / 动作 | 模型回复 | 处理 | 实际说出口 |",
              "|---|---|---|---|---|---|---|"]
    for s, g, text, op, acts, reply, how, said in rows:
        lines.append(f"| {s} | {g} | {text} | {op} {' '.join(acts)} | {reply} | {how} | {said if how != '原样' else ''} |")
    out = ROOT / cfg["trace"]["log_dir"] / (f"promise-bench-{datetime.now():%Y%m%d-%H%M%S}{'-' + args.label if args.label else ''}"
                                            f"{f'-noise{args.noise}' if args.noise else ''}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:9 + len(GROUPS)]))
    print(f"已写入 {out}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
