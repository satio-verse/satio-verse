"""角色混淆的留出评测: 带 [记忆] 行问用户自己的事, 只看模型自己怎么答 (不走服务端的直接回答).

0.8B 碰到用户问自己的事, 常把"我"答成它自己 ("我开的什么车" -> "我开的是黑色的特斯拉").
这组用例在生成 LoRA 训练数据之前写好 (2026-10-24), 训练数据会和它去重, 不要拿它调训练数据.

  python tools/bench_role.py                          # 3 个种子 (13, 7, 29), 先用 tools/start_llm.py 启动 llama-server
  python tools/bench_role.py --label lora-v1          # 换了 LoRA 之后再跑, 报告文件名带上标签

四类:
  用户的事      记着: 回复要说出记着的内容, 而且是在说用户 ("你……"), 不能说成角色自己的
  没记着        问的事记忆里没有 (带着别的记忆): 要说不知道/没说过, 不能编
  问角色        问的是角色自己, 记忆里有用户的同类事: 不能把用户的事说成自己的
  无关          和记忆无关的闲聊/问题: 不能扯到记忆里的事
结果写到 logs/role-bench-<时间>-<标签>.md.
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

from memory_noise import leaked, noise_facts  # noqa: E402
from wm import intent as I  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.json_stream import JSONObjectStream  # noqa: E402
from wm.llm import create_llm  # noqa: E402

STATE = "[当前] 你在用户面前, 站着; 手里没拿东西."
ABSTAIN = r"不知道|没告诉|没说过|还没说|没跟我说|不记得|没提过|告诉我|还不知道|不清楚|没听你说"

# (类别, 记忆里的事实, 问句, 要出现的 (正则, 任一), 不能出现的 (正则))
CASES = [
    ("用户的事", ["用户住在成都"], "我住哪来着", r"成都", r"我住在|我在成都|我住成都"),
    ("用户的事", ["用户在一家医院当护士"], "我是干什么的", r"护士", r"我是护士|我在医院|我是一名护士"),
    ("用户的事", ["用户的女朋友叫小雨"], "我女朋友叫什么名字", r"小雨", r"我女朋友|我的女朋友"),
    ("用户的事", ["用户对芒果过敏"], "我能吃芒果吗", r"过敏", r"我.{0,3}过敏|我能吃|我可以吃|我也能吃"),
    ("用户的事", ["用户每周三晚上去游泳"], "我周三晚上一般干嘛", r"游泳", r"我.{0,4}游泳|我一般"),
    ("用户的事", ["用户养了一只叫可乐的边牧"], "我家狗是什么品种", r"边牧", r"我家|我的狗|我养"),
    ("用户的事", ["用户在南京读研究生"], "我在哪上学", r"南京", r"我在南京|我在读"),
    ("用户的事", ["用户最喜欢的歌手是周深"], "我最喜欢哪个歌手", r"周深", r"我最喜欢|我喜欢周深"),
    ("用户的事", ["用户下个月要去云南旅游"], "我下个月有什么安排", r"云南", r"我.{0,4}要去|我下个月"),
    ("用户的事", ["用户开的是一辆红色的比亚迪"], "我开的是什么车", r"比亚迪", r"我开的|我的车"),
    ("用户的事", ["用户的生日是三月十二号"], "我生日是几号", r"三月十二|3月12", r"我的生日|我生日是"),
    ("用户的事", ["用户最近在学吉他"], "我最近在学什么", r"吉他", r"我.{0,3}在学|我最近"),
    ("用户的事", ["用户不吃香菜"], "我吃不吃香菜", r"不吃|不喜欢", r"我不吃|我也不吃|我吃"),
    ("用户的事", ["用户的妈妈是小学老师"], "我妈是做什么的", r"老师", r"我妈|我妈妈"),
    ("没记着", ["用户住在成都"], "我的车是什么牌子的", ABSTAIN, r"我的车是|你的车是|你开的是"),
    ("没记着", ["用户喜欢吃火锅"], "我哥叫什么名字", ABSTAIN, r"我哥叫|你哥叫|我哥哥叫"),
    ("没记着", ["用户在一家医院当护士"], "我老家是哪里的", ABSTAIN, r"我老家|你老家是|你老家在"),
    ("没记着", ["用户最近在学吉他"], "我对什么过敏", ABSTAIN, r"我对.{1,4}过敏|你对.{1,4}过敏"),
    ("没记着", ["用户每周三晚上去游泳"], "我养了什么宠物", ABSTAIN, r"我养了|你养了"),
    ("没记着", ["用户的女朋友叫小雨"], "我是哪年出生的", ABSTAIN, r"\d{2,4}年|我是.{1,4}年"),
    ("问角色", ["用户住在成都"], "你住在哪里", r".", r"我住在成都|我在成都|我也住在成都"),
    ("问角色", ["用户在一家医院当护士"], "你是做什么的", r".", r"我是护士|我在医院|我是一名护士"),
    ("问角色", ["用户养了一只叫可乐的边牧"], "你养宠物了吗", r".", r"我养了.{0,4}边牧|我养了.{0,3}可乐|我的狗叫可乐"),
    ("问角色", ["用户最喜欢的歌手是周深"], "你最喜欢哪个歌手", r".", r"我最喜欢.{0,2}周深"),
    ("无关", ["用户对芒果过敏"], "今天天气挺好的", r".", r"芒果|过敏"),
    ("无关", ["用户的女朋友叫小雨"], "给我讲个笑话吧", r".", r"小雨|女朋友"),
    ("无关", ["用户开的是一辆红色的比亚迪"], "你觉得晚饭吃什么好", r".", r"比亚迪|车"),
    ("无关", ["用户在南京读研究生"], "我今天有点累", r".", r"南京|研究生"),
]


def judge(reply: str, expect: str, forbid: str) -> bool:
    return bool(re.search(expect, reply)) and not re.search(forbid, reply)


async def ask(llm, text: str, memory: str) -> dict:
    parser, meta = JSONObjectStream(("reply",)), {}
    async for delta in llm.stream(I.build_messages(text, state=STATE, memory=memory), schema=I.build_schema(),
                                  max_tokens=160, meta=meta):
        parser.feed(delta)
    return parser.result


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", default="13,7,29")
    ap.add_argument("--label", default="base")
    ap.add_argument("--endpoint", help="llama-server 地址 (默认取配置)")
    ap.add_argument("--noise", type=int, default=0, help="干扰测试: 每条的 [记忆] 行后面再加这么多条无关的小事 (tools/memory_noise.py)")
    ap.add_argument("--noise-seed", type=int, default=0)
    ap.add_argument("--cfg", dest="overrides", action="append", default=[], help="配置覆盖, 如 llm.id_slot=3 (不挤主服务对话用的 0 号槽位)")
    args = ap.parse_args()
    over = ["llm.engine=llamacpp"] + ([f"llm.endpoint={args.endpoint}"] if args.endpoint else []) + args.overrides
    cfg = load_config(overrides=over)
    llm = create_llm(cfg["llm"])
    if not await llm.health():
        sys.exit(f"LLM 不可用: {cfg['llm'].get('endpoint')} (先运行 python tools/start_llm.py)")
    body, seed = llm._body, {"v": 13}
    llm._body = lambda *a, **k: {**body(*a, **k), "seed": seed["v"]}  # 换种子看不同的输出 (服务端固定用 13)
    rows, leaks = [], 0
    for s in (int(x) for x in args.seeds.split(",")):
        seed["v"] = s
        for i, (kind, facts, text, expect, forbid) in enumerate(CASES):
            noise = noise_facts(args.noise, args.noise_seed + i)  # 每条换一组, 同一条各个种子一样
            memory = f"{I.MEMORY_TAG} " + "; ".join(list(facts) + [f for f, _ in noise]) + "."
            r = await ask(llm, text, memory)
            reply = r.get("reply", "")
            leaks += bool(leaked(reply, noise))
            ok = judge(reply, expect, forbid)
            rows.append((s, kind, text, facts, reply, ok))
            print(f"{'✓' if ok else '✗'} {s:2} {kind:4} {text:<12} {reply}")
    await llm.close()
    kinds = list(dict.fromkeys(c[0] for c in CASES))
    count, total = Counter(r[1] for r in rows if r[5]), Counter(r[1] for r in rows)
    lines = [f"# 角色混淆评测 {datetime.now():%Y-%m-%d %H:%M} {args.label}", "",
             f"模型 {llm.label}, 种子 {args.seeds}, {len(CASES)} 条 x {len(rows) // len(CASES)}. 只看模型自己的回答 (不走服务端直接回答)."
             + (f" 干扰: 每条的记忆行多带 {args.noise} 条无关记忆, 回复里提到其中的东西 {leaks}/{len(rows)} 次." if args.noise else ""), "",
             "| 类别 | 通过 |", "|---|---|"]
    lines += [f"| {k} | {count[k]}/{total[k]} |" for k in kinds]
    lines += [f"| **合计** | **{sum(count.values())}/{len(rows)}** |", "",
              "| 种子 | 类别 | 问句 | 记忆 | 回复 | |", "|---|---|---|---|---|---|"]
    lines += [f"| {s} | {k} | {t} | {'; '.join(f)} | {r} | {'✓' if ok else '✗'} |" for s, k, t, f, r, ok in rows]
    out = ROOT / cfg["trace"]["log_dir"] / f"role-bench-{datetime.now():%Y%m%d-%H%M%S}-{args.label}{f'-noise{args.noise}' if args.noise else ''}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:6 + len(kinds) + 1]))
    print(f"已写入 {out}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
