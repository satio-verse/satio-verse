"""S3「推/看分开」的尺子: 闲聊时她会不会主动翻你的日程.

为什么量这个: 现在的记忆行是直接拼在本轮用户那句话后面的 (wm/intent.py:372 的注释自己写着
「离生成最近, 0.8B 模型才会照着它回答」), 所以 recall 到日程她就会顺口说出来.
像人一样的标准是: 闲聊时别说, 被问到才说 —— 而且被问到时得答对.

用法:
    python tools/bench_bother.py                  # 改前/改后各跑一次, 比两个数
    python tools/bench_bother.py --split on       # 走"推/看分开"那一档
    python tools/bench_bother.py --endpoint http://127.0.0.1:8784

量两件事:
    1. 主动提日程: 50 句闲聊里, 她的回复提到你哪天要去干嘛的次数 (越低越好)
    2. 被问会答: 10 句问日程的话, 答对的题数 (越高越好)

只读 + 只用临时记忆库, 不碰她真正的库, 不写任何东西到线上.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm import intent as I  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.llm import create_llm  # noqa: E402
from wm.memory import Memory  # noqa: E402

STATE = "[当前] 你在用户面前, 站着; 手里没拿东西."

# 灌给临时库的几件事: 前四条是"日程/安排", 后两条是"关于你的稳定事实".
# 故意混在一起 —— 像人一样看的是: 闲聊时不提日程, 但可以提别的.
SEED = [
    ("2026-12-01", "summary", "用户12月1日要去体检"),
    ("2026-11-01", "summary", "用户每周三晚上去上钢琴课"),
    ("2026-12-05", "summary", "用户12月5日要交房租"),
    ("2026-12-10", "summary", "用户12月10日要去机场接朋友"),
    (0, "say", "我喜欢喝美式咖啡"),
    (0, "say", "我养了一只橘猫叫豆包"),
]

# 50 句闲聊: 日常、情绪、天气、在干嘛、吃什么. 一句日历相关都没有.
CHATTER = [
    "今天好累啊", "我刚吃完晚饭", "你在干嘛呀", "外面下雨了", "好困想睡觉",
    "今天上班有点烦", "我刚健完身回来", "你吃饭了吗", "有点无聊", "我在沙发上躺着",
    "刚看完一集剧", "今天心情不错", "你平时喜欢干什么", "我又点外卖了", "好想吃火锅",
    "今天风好大", "刚洗完澡", "我周末一般都在家", "你困不困", "我手机快没电了",
    "刚打完一局游戏", "今天地铁好挤", "你在想什么", "我有点饿了", "咖啡喝多了睡不着",
    "今天好热", "我刚把衣服洗了", "你冷不冷", "我妈刚给我打电话", "有点心累",
    "刚收拾完房间", "我在听歌", "今天月亮好圆", "你无聊吗", "我买了个新杯子",
    "今天开会开好久", "刚吃完饭有点撑", "你喜欢猫吗", "我今天不想动", "外面好安静",
    "我刚看完一部电影", "有点想出去玩", "今天醒得特别早", "你累不累呀", "我在阳台吹风",
    "刚刚差点睡着", "今天没什么事", "我想喝点热的", "你在家吗", "我再刷会儿手机就睡",
]

# 10 句问日程的话: 该答对 (时间/事情对得上, 且不编造别的).
ASK = [
    ("我哪天去体检", ["12月1日", "12月1"], ["房租", "吉他", "机场接朋友"]),
    ("我什么时候上钢琴课", ["周三", "星期三"], ["12月1", "12月5"]),
    ("我哪天要交房租", ["12月5日", "12月5"], ["12月1", "周三"]),
    ("我什么时候要去接朋友", ["12月10日", "12月10"], ["体检", "房租"]),  # 别用日期当禁用词: "12月10日" 里含 "12月1"
    ("我最近的安排是什么", ["体检", "钢琴", "房租"], []),
    ("我这周有事吗", ["钢琴", "周三"], []),
    ("我12月要干嘛", ["体检", "房租", "接朋友"], []),
    ("我哪天不用上班", [], []),
    ("下一次要办的事是什么", ["体检", "房租", "接朋友"], []),
    ("我最近忙不忙", [], []),
]

# 算"她主动提了日程"的词: 只用灌进去的那四件事里的独有词, 免得把"今天"这种日常词算进来.
SCHEDULE_WORDS = ("体检", "钢琴课", "房租", "机场", "接朋友", "12月1", "12月5", "12月10")


def make_memory(vault: Path) -> Memory:
    m = Memory({"vault": str(vault), "character": "小流", "graph_notes": False})
    mem = vault / "小流" / "记忆"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "关于用户.md").write_text("", encoding="utf-8")
    (vault / "小流" / "关系.md").write_text("# 关系\n\n- 认识天数: 0\n", encoding="utf-8")
    for day, kind, text in SEED:
        if kind == "say":
            m.observe(text, "嗯嗯", [], "calm")
        else:
            m.add_summary(day, [{"text": text, "times": ["10:00"]}], 0)
    return m


async def say(llm, text: str, mem_line: str, answer: str | None = None) -> str:
    """和流水线一致: 问到自己固定信息时按记忆直接回答, 不调模型; 其余交给意图模型."""
    if answer:
        return answer
    raw = await llm.complete(I.build_messages(text, None, STATE, mem_line),
                             schema=I.build_schema(), max_tokens=160)
    try:
        return json.loads(raw).get("reply", "") or raw
    except json.JSONDecodeError:
        return raw


async def run(endpoint: str | None, split: str) -> dict:
    cfg = load_config(overrides=["llm.engine=llamacpp"] + ([f"llm.endpoint={endpoint}"] if endpoint else []))
    llm = create_llm(cfg["llm"])

    vault = Path(tempfile.mkdtemp())
    m = make_memory(vault)

    bother = []
    for text in CHATTER:
        r = m.recall(text) if split == "off" else m.recall(text, scope="slots")
        reply = await say(llm, text, r["line"], r.get("answer"))
        hit = [w for w in SCHEDULE_WORDS if w in reply]
        bother.append({"text": text, "memory": r["line"], "reply": reply, "hit": hit})

    answered, details = 0, []
    for q, expect, forbid in ASK:
        r = m.recall(q)
        reply = await say(llm, q, r["line"], r.get("answer"))
        ok = (not expect or any(e in reply for e in expect)) and not any(f in reply for f in forbid)
        answered += ok
        details.append({"q": q, "reply": reply, "ok": ok})

    n_bother = sum(1 for b in bother if b["hit"])
    report = {
        "split": split,
        "endpoint": endpoint or "config",
        "chatter": len(CHATTER),
        "bother": n_bother,
        "bother_rate": round(n_bother / max(1, len(CHATTER)), 3),
        "ask": len(ASK),
        "answered": answered,
        "answered_rate": round(answered / max(1, len(ASK)), 3),
        "bother_samples": [b for b in bother if b["hit"]][:8],
        "ask_samples": details,
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--split", choices=["off", "on"], default="off",
                    help="off = 现在这样(记忆直接拼进本轮输入); on = 只取 slots")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    rep = asyncio.run(run(a.endpoint or None, a.split))
    out = Path(a.out) if a.out else ROOT / "logs" / f"bench-bother-{a.split}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"# 闲聊里主动提日程  {rep['bother']}/{rep['chatter']}  ({rep['bother_rate']:.1%})")
    print(f"# 被问住日程答对    {rep['answered']}/{rep['ask']}  ({rep['answered_rate']:.1%})")
    for s in rep["bother_samples"][:6]:
        print(f"  提了 [{','.join(s['hit'])}]  <- {s['text']}  => {s['reply'][:40]}")
    for s in rep["ask_samples"]:
        print(f"  {'✓' if s['ok'] else '✗'} {s['q']}  => {s['reply'][:44]}")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
