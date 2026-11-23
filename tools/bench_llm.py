"""P0 基准: 同一个模型、同一组 few-shot, 对比两种请求方式的意图延迟和准确率.

  baseline  模仿 HAST-SEC v0.1 的请求模式: 回复字段在前、全字段必填、中文长键名、不流式;
            普通聊天要再发第二次请求拿回复.
  stream    本项目: 单次调用, op -> acts -> emo -> reply 顺序, 只写非默认参数, 流式解析,
            acts 一闭合就算意图可执行, 第一个分句出来就算可以开口.

用法:
  python tools/bench_llm.py                       # 用 config 里的 llm 设置 (默认 mock, 只验证脚本本身)
  python tools/bench_llm.py --engine llamacpp     # 先用 tools/start_llm.py 启动 llama-server
  python tools/bench_llm.py --engine llamacpp --repeat 3
结果写到 logs/bench-<时间>.md 和同名 .json.

另有一组桌宠用例 (DESK_CASES, state.place = desktop, 按桌宠声明的技能加 squat): 只跑 stream, 单独成表,
不进上面两种协议的汇总 (和以前的报告可比).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wm import intent as I  # noqa: E402
from wm import promise as P  # noqa: E402
from wm.confidence import field_confidence  # noqa: E402
from wm.clauses import ClauseSplitter  # noqa: E402
from wm.config import ROOT, load_config  # noqa: E402
from wm.json_stream import JSONObjectStream  # noqa: E402
from wm.llm import create_llm  # noqa: E402

# (输入, 允许的 op, 期望动作 [(技能, 侧别或 None[, 目标[, "run"]])] 或 None 表示不检查动作). 都不在 few-shot 示例里.
CASES = [
    ("把左手举起来", {"act"}, [("raise", "L")]),
    ("右手抬到肩膀那么高", {"act"}, [("raise", "R")]),
    ("挥挥手跟大家打个招呼", {"act", "chat"}, [("wave", None)]),
    ("双手举高", {"act"}, [("raise", "B")]),
    ("点点头", {"act"}, [("nod", None)]),
    ("摇摇头表示不同意", {"act"}, [("shake", None)]),
    ("用右手指一下前面", {"act"}, [("point", "R")]),
    ("先举左手再举右手", {"act"}, [("raise", "L"), ("raise", "R")]),
    ("鞠一躬", {"act"}, [("bow", None)]),
    ("耸耸肩", {"act"}, [("shrug", None)]),
    ("快速挥一下右手", {"act"}, [("wave", "R")]),
    ("手放下来吧", {"stop"}, []),
    ("停，别动了", {"stop"}, []),
    ("今天天气真不错", {"chat"}, None),
    ("给我讲个简短的笑话", {"chat"}, None),
    ("我有点累了", {"chat"}, None),
    ("你叫什么名字", {"chat"}, None),
    ("推荐一本书吧", {"chat"}, None),
    # 开放空间行动: 目标来自场景图 (web/scenes/room.json)
    ("去书桌那边", {"act"}, [("go", None, "desk")]),
    ("走到门口去", {"act"}, [("go", None, "door")]),
    ("到我这儿来", {"act"}, [("go", None, "user")]),
    ("回到房间中间", {"act"}, [("go", None, "center")]),
    ("看一下书架", {"act"}, [("look", None, "bookshelf")]),
    ("看看门口", {"act"}, [("look", None, "door")]),
    ("向左转", {"act"}, [("turn", "L")]),
    ("跑到书架那边", {"act"}, [("go", None, "bookshelf", "run")]),
    ("走到绿植旁边然后点点头", {"act"}, [("go", None, "plant"), ("nod", None)]),
    ("指着窗户", {"act"}, [("point", None, "window")]),
    # 物体交互: 坐/站/拿/放
    ("去沙发上坐着", {"act"}, [("sit", None, "sofa")]),
    ("站起来", {"act"}, [("stand", None)]),
    ("拿起那本书", {"act"}, [("pick", None, "book")]),
    ("拿一下那个球", {"act"}, [("pick", None, "ball")]),
    ("把书放到书架上", {"act"}, [("put", None, "bookshelf")]),
    ("把手里的东西放下", {"act"}, [("put", None)]),
]
# 做不到的要求 (留出: 都不在 few-shot 示例里): 应该 op=na, 不拿别的动作凑数
NA_CASES = [
    ("给我劈个叉", {"na"}, []), ("跳个舞吧", {"na"}, []), ("翻个跟头", {"na"}, []), ("来个倒立", {"na"}, []),
    ("做几个仰卧起坐", {"na"}, []), ("把台灯打开", {"na"}, []), ("帮我倒杯水", {"na"}, []), ("弹首钢琴曲听听", {"na"}, []),
    ("飞起来给我看看", {"na"}, []), ("打一套太极拳", {"na"}, []), ("跳一会儿绳", {"na"}, []), ("游个泳吧", {"na"}, []),
]
# 做得到、但说法像"做不到"的 (留出): 量误拒
NEAR_CASES = [
    ("原地转个圈", {"act"}, [("turn", None)]), ("抬一下右腿", {"act"}, [("leg", "R")]),
    ("跑到门口去", {"act"}, [("go", None, "door", "run")]), ("看看电视", {"act"}, [("look", None, "tv")]),
    ("向大家鞠个躬", {"act"}, [("bow", None)]), ("耸耸肩表示无奈", {"act"}, [("shrug", None)]),
    ("把球捡起来", {"act"}, [("pick", None, "ball")]), ("去沙发那坐会儿", {"act"}, [("sit", None, "sofa")]),
    ("跟我打个招呼", {"act", "chat"}, [("wave", None)]), ("指一下电视", {"act"}, [("point", None, "tv")]),
    ("走到茶几旁边", {"act"}, [("go", None, "coffee_table")]), ("双手举过头顶", {"act"}, [("raise", "B")]),
]
# 带角色状态的用例: (输入, 允许的 op, 期望动作, 前端上报的状态, 回复检查: 空格分隔, "!词" 表示不能出现)
_HOLD_BOOK = {"at": "center", "posture": "standing", "items": {"book": {"held": "left", "origin": "desk"}, "ball": {"on": "floor"}}}
_HOLD_BALL = {"at": "bookshelf", "posture": "standing", "items": {"ball": {"held": "right", "origin": "floor"}, "book": {"on": "desk"}}}
_SEATED = {"at": "sofa", "posture": "seated", "items": {"ball": {"on": "floor"}, "book": {"on": "desk"}}}
STATE_CASES = [
    ("你现在拿着什么", {"chat"}, None, _HOLD_BOOK, "书 !没拿 !没有 !书在"),
    ("你手上是什么东西", {"chat"}, None, _HOLD_BALL, "球 !在沙发 !在地上 !在书架 !在书桌 !没拿"),
    ("你在哪呢", {"chat"}, None, _SEATED, "沙发 !书架 !站"),
    ("把它放回原处", {"act"}, [("put", None, "home")], _HOLD_BOOK, None),
    ("放回去", {"act"}, [("put", None, "home")], _HOLD_BALL, None),
    ("别坐着了", {"act"}, [("stand", None)], _SEATED, None),
]
# 桌宠 (state.place = desktop): 提示词里没有房间和家具; 身体只能做手势和蹲下 (桌宠声明的技能 + squat).
# 按服务端实际下发算 (补动作 -> 可行性声明 -> 能力闸门 -> 回复侧检查). 走、坐、拿的要求: 改判 na, 或者老实聊天
# (没有动作, 嘴上没答应) 都算对. 都不在 DESK_EXAMPLES 里.
_DESK = {"place": "desktop"}
_NO_AGREE = "!当然 !好的 !好呀 !好， !来啦 !我来 !我去"
DESK_CAN = frozenset(["wave", "raise", "point", "nod", "shake", "shrug", "think", "bow", "squat"])
DESK_CASES = [
    ("蹲一下", {"act"}, [("squat", None)], _DESK, None),
    ("你能蹲下来吗", {"act"}, [("squat", None)], _DESK, None),
    ("挥挥手跟我打个招呼", {"act", "chat"}, [("wave", None)], _DESK, None),
    ("点点头", {"act"}, [("nod", None)], _DESK, None),
    ("把右手举起来", {"act"}, [("raise", "R")], _DESK, None),
    ("鞠一躬", {"act"}, [("bow", None)], _DESK, None),
    ("能去沙发上吗", {"na", "chat"}, [], _DESK, _NO_AGREE),
    ("走到门口去", {"na", "chat"}, [], _DESK, _NO_AGREE),
    ("把球捡起来", {"na", "chat"}, [], _DESK, _NO_AGREE),
    ("坐下休息一会儿", {"na", "chat"}, [], _DESK, _NO_AGREE),
    ("你现在在哪", {"chat"}, None, _DESK, "桌面 !沙发 !书架 !房间"),
    ("今天好累啊", {"chat"}, None, _DESK, "!沙发 !书架 !电视 !房间"),
]


def check(op, acts, allowed_ops, expected) -> tuple[bool, bool]:
    """返回 (op 是否正确, op 和动作集合是否都正确). 期望里写了侧别/目标/跑的技能才比较这些项."""
    op_ok = op in allowed_ops
    if expected is None:
        return op_ok, op_ok
    exp = [tuple(e) + (None,) * (4 - len(e)) for e in expected]
    sided = {e[0] for e in exp if e[1] is not None}
    targeted = {e[0] for e in exp if e[2] is not None}
    running = {e[0] for e in exp if e[3] is not None}
    got = Counter((a["do"], a["side"] if a["do"] in sided else None, a.get("to") if a["do"] in targeted else None,
                   ("run" if a.get("spd", 1) >= 1.6 else "walk") if a["do"] in running else None) for a in acts)
    return op_ok, op_ok and got == Counter(exp)


def reply_ok(reply: str, rule: str) -> bool:
    """rule: 空格分隔的词, 普通词必须出现, "!词" 不能出现."""
    return all((w[1:] not in reply) if w.startswith("!") else (w in reply) for w in rule.split())


async def run_stream(llm, text, max_tokens, state=None, memory=None):
    meta = {}
    parser = JSONObjectStream(("reply",))
    splitter = ClauseSplitter()
    t0 = time.perf_counter()
    intent_t = speak_t = None
    place = I.place_of(state)
    async for delta in llm.stream(I.build_messages(text, state=I.describe_state(state), memory=memory, place=place),
                                  schema=I.build_schema(place), max_tokens=max_tokens, meta=meta):
        for ev in parser.feed(delta):
            if ev[:2] == ("value", "acts") and intent_t is None:
                intent_t = time.perf_counter()
            if ev[0] == "delta" and speak_t is None and splitter.feed(ev[2]):
                speak_t = time.perf_counter()
    end = time.perf_counter()
    if speak_t is None and splitter.flush():
        speak_t = end
    res = parser.result
    tokens = (meta.get("usage") or {}).get("completion_tokens")
    conf = field_confidence(meta.get("tokens") or [], I.field_enums(place), llm.temperature)
    return {
        "conf": conf["overall"] if conf else None,
        "intent_ms": (intent_t - t0) * 1000 if intent_t else None,
        "speak_ms": (speak_t - t0) * 1000 if speak_t else None,
        "total_ms": (end - t0) * 1000,
        "tokens": tokens,
        "parse_ok": parser.done,
        "op": res.get("op"),
        "acts": I.normalize_acts(res.get("acts", [])),
        "reply": res.get("reply", ""),
    }


async def run_baseline(llm, text, max_tokens, state=None):
    t0 = time.perf_counter()
    raw = await llm.complete(I.build_verbose_messages(text), schema=I.build_verbose_schema(),
                             max_tokens=max_tokens * 3, id_slot=1)
    plan_t = time.perf_counter()
    tokens = ((llm.last_meta or {}).get("usage") or {}).get("completion_tokens") or 0
    try:
        obj = json.loads(raw)
        parse_ok = True
    except json.JSONDecodeError:
        obj, parse_ok = {}, False
    op, acts = I.from_verbose(obj)
    reply, end = obj.get("reply", ""), plan_t
    if op == "chat":
        reply = await llm.complete([{"role": "system", "content": I.VERBOSE_CHAT_PROMPT},
                                    {"role": "user", "content": text}], max_tokens=max_tokens, id_slot=2)
        end = time.perf_counter()
        tokens += ((llm.last_meta or {}).get("usage") or {}).get("completion_tokens") or 0
    return {
        "intent_ms": (plan_t - t0) * 1000,
        "speak_ms": (end - t0) * 1000,
        "total_ms": (end - t0) * 1000,
        "tokens": tokens,
        "parse_ok": parse_ok,
        "op": op,
        "acts": acts,
        "reply": reply,
    }


def pct(values, q):
    values = [v for v in values if v is not None]
    return round(float(np.percentile(values, q)), 1) if values else None


def groups(rows) -> list[str]:
    """按用例组: 做不到 (要 na)、做得到但像做不到 (量误拒)、其余."""
    out = ["", "| 用例组 | 条数 | 正确率 (含规则兜底和能力闸门) | 说明 |", "|---|---|---|---|"]
    na = [r for r in rows if r["group"] == "na"]
    near = [r for r in rows if r["group"] == "near"]
    rest = [r for r in rows if r["group"] in ("base", "state")]
    if na:
        sub = sum(1 for r in na if r["acts"] and r["op"] != "na")
        out.append(f"| 做不到 (留出) | {len(na)} | {sum(r['act_ok_repaired'] for r in na) / len(na):.0%} | "
                   f"模型拿别的动作凑数 {sub} 条 (闸门改判 {sum(1 for r in na if r['gated'])} 条), "
                   f"当成聊天 {sum(1 for r in na if r['op'] == 'chat')} 条 |")
    if near:
        out.append(f"| 做得到但像做不到 (留出) | {len(near)} | {sum(r['act_ok_repaired'] for r in near) / len(near):.0%} | "
                   f"误拒 (模型答成 na {sum(1 for r in near if r['op'] == 'na')} 条, 闸门误改 {sum(1 for r in near if r['gated'])} 条) |")
    if rest:
        out.append(f"| 原有用例 | {len(rest)} | {sum(r['act_ok_repaired'] for r in rest) / len(rest):.0%} | "
                   f"闸门误改 {sum(1 for r in rest if r['gated'])} 条 |")
    return out


def gate_sweep(rows) -> list[str]:
    """能力闸门的阈值: 动作在原话里都找不到对应说法、且置信度低于阈值时改判成做不到. 看各阈值改对和改错多少."""
    out = ["", f"能力闸门 (`intent.unsupported`, 现用阈值 {I.GATE_CONF}):", "",
           "| 阈值 | 做不到组改判 | 其余用例误改 | 没被改判的凑数动作 (置信度) |", "|---|---|---|---|"]
    na = [r for r in rows if r["group"] == "na"]
    rest = [r for r in rows if r["group"] != "na"]
    gate = lambda r, t: I.unsupported(r["text"], *I.repair(r["text"], r["op"], r["acts"]), r.get("conf"), t)  # noqa: E731
    for t in (0.5, 0.6, 0.7, 0.8, 0.9):
        left = [f"{r['text']} {[a['do'] for a in r['acts']]} ({r['conf']:.2f})" for r in na
                if r["op"] == "act" and r["acts"] and not gate(r, t) and r.get("conf") is not None]
        out.append(f"| {t} | {sum(gate(r, t) for r in na)}/{len(na)} | {sum(gate(r, t) for r in rest)}/{len(rest)} | "
                   f"{'; '.join(left[:4]) or '-'} |")
    return out


def calibration(rows) -> list[str]:
    """置信度能不能把答错的挑出来: AUROC (随机挑一对对错各一的样本, 答对的置信度更高的概率),
    以及按阈值升级时会升级多少、能截住多少错误、留下来的正确率."""
    rows = [r for r in rows if r.get("conf") is not None]
    if not rows:
        return ["置信度: 引擎没有返回 token 概率 (llm.logprobs=0 或 mock 引擎)."]
    right = [r["conf"] for r in rows if r["act_ok"]]
    wrong = [r["conf"] for r in rows if not r["act_ok"]]
    pairs = [(a > b) + 0.5 * (a == b) for a in right for b in wrong]
    auroc = sum(pairs) / len(pairs) if pairs else None
    lines = ["", f"置信度 (op 和各动作 do/to 选中概率的乘积): 答对的中位 {np.median(right) if right else float('nan'):.2f}, "
             f"答错的中位 {np.median(wrong) if wrong else float('nan'):.2f}; AUROC {auroc if auroc is not None else float('nan'):.2f} "
             f"({len(right)} 对 / {len(wrong)} 错).", "",
             "| 升级阈值 | 升级比例 | 截住的错误 | 不升级部分的正确率 |", "|---|---|---|---|"]
    for t in (0.3, 0.5, 0.7, 0.9):
        low = [r for r in rows if r["conf"] < t]
        keep = [r for r in rows if r["conf"] >= t]
        caught = sum(not r["act_ok"] for r in low)
        lines.append(f"| {t} | {len(low) / len(rows):.0%} | {caught}/{len(wrong)} | "
                     f"{(sum(r['act_ok'] for r in keep) / len(keep)) if keep else float('nan'):.0%} |")
    return lines


def escalation(rows, label) -> list[str]:
    """按阈值升级: 置信度低于阈值的用复核模型的结果 (都含规则兜底). 和服务端一样, 被能力闸门改判的不再复核.
    复核模型自己在所有输入上的正确率也列出来."""
    rows = [r for r in rows if r.get("conf") is not None and "esc_ok" in r]
    if not rows:
        return []
    n = len(rows)
    base = sum(r["act_ok_repaired"] for r in rows) / n
    lines = ["", f"升级复核 ({label}, 只要 op 和 acts): 复核模型单独在全部 {n} 条上正确率 {sum(r['esc_ok'] for r in rows) / n:.0%}, "
             f"耗时 P50 {pct([r['esc_ms'] for r in rows], 50)} / P95 {pct([r['esc_ms'] for r in rows], 95)} ms; "
             f"不升级 (小模型 + 规则兜底) 正确率 {base:.0%}.", "",
             "| 升级阈值 | 升级比例 | 升级后正确率 | 改对 | 改错 |", "|---|---|---|---|---|"]
    for t in (0.5, 0.7, 0.8, 0.9):
        low = [r for r in rows if r["conf"] < t and not r.get("gated")]
        final = sum((r["esc_ok"] if r in low else r["act_ok_repaired"]) for r in rows) / n
        fixed = sum(1 for r in low if r["esc_ok"] and not r["act_ok_repaired"])
        broke = sum(1 for r in low if not r["esc_ok"] and r["act_ok_repaired"])
        lines.append(f"| {t} | {len(low) / n:.0%} | {final:.0%} | {fixed} | {broke} |")
    by = {g: [r for r in rows if r["group"] in gs] for g, gs in (("做不到", ("na",)), ("近似", ("near",)), ("原有", ("base", "state")))}
    lines += ["", "复核模型单独分组: " + ", ".join(f"{g} {sum(r['esc_ok'] for r in rs)}/{len(rs)}" for g, rs in by.items() if rs)
              + " (小模型 + 规则兜底 + 闸门: " + ", ".join(f"{g} {sum(r['act_ok_repaired'] for r in rs)}/{len(rs)}"
                                                for g, rs in by.items() if rs) + ")"]
    return lines


def desk_served(text, op, acts, conf, reply) -> tuple[str, list, str, str]:
    """桌宠: 服务端实际下发的 (op, 动作, 回复, 怎么处理的). 和流水线一样: 可行性声明 -> 能力闸门 -> 回复侧检查 (都按桌宠的身体)."""
    full = I.DEFAULT_CAN | (DESK_CAN & I.OPT_IN)
    how = "原样"
    if op != "stop" and P.request_kind(text, full, room=False) == "doable" and (not acts or I.off_cue(text, acts)):
        filled = I.fill_acts(text)
        if filled:
            op, acts, how = "act", filled, "补动作"
    kind = P.request_kind(text, DESK_CAN, room=False)
    ok, cant = I.split_can(acts, DESK_CAN, room=False)
    if cant and not ok and (op == "act" or kind == "impossible"):
        return "na", [], I.body_refusal(I.asked_cannot(text, DESK_CAN) or cant, I.DESKTOP), "可行性改判"
    if I.unsupported(text, op, ok, conf, can=DESK_CAN, room=False):
        return "na", [], I.REFUSALS[0], "闸门"
    sp, out = ClauseSplitter(4, 10, 40), []
    for clause in sp.feed(reply) + sp.flush():
        v = P.check(text, op, [a["do"] for a in ok], clause, kind, DESK_CAN, False)
        if v:
            if P.action(v, kind, op, [a["do"] for a in ok], len(out)) == "refuse":
                return "na", [], I.body_refusal(I.asked_cannot(text, DESK_CAN), I.DESKTOP), "回复改判"
            return op, ok, ("".join(out) + v.keep) or P.FALLBACK, "截断"
        out.append(clause)
    return op, ok, reply, how


def desk_report(rows) -> list[str]:
    if not rows:
        return []
    n = len(rows)
    lines = ["", f"桌宠 (state.place = desktop, 身体: {' '.join(sorted(DESK_CAN))}): {n} 次, "
             f"模型自己对 {sum(r['act_ok'] for r in rows)}/{n}, 服务端实际下发对 {sum(r['served_ok'] for r in rows)}/{n}; "
             f"意图 P50 {pct([r['intent_ms'] for r in rows], 50)} ms.", "",
             "| 原话 | 模型 op / 动作 | 模型回复 | 处理 | 下发 op / 动作 | 实际说出口 | 对 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        show = lambda acts: " ".join(a["do"] for a in acts) or "-"  # noqa: E731
        lines.append(f"| {r['text']} | {r['op']} {show(r['acts'])} | {r['reply']} | {r['how']} | {r['sop']} {show(r['sacts'])} | "
                     f"{r['said'] if r['how'] != '原样' else ''} | {'✓' if r['served_ok'] else '✗'} |")
    return lines


def summarize(rows):
    out = {}
    for key in ("intent_ms", "speak_ms", "total_ms"):
        out[key] = {"p50": pct([r[key] for r in rows], 50), "p95": pct([r[key] for r in rows], 95)}
    toks = [r["tokens"] for r in rows if r["tokens"] is not None]
    out["tokens_mean"] = round(sum(toks) / len(toks), 1) if toks else None
    out["parse_ok"] = sum(r["parse_ok"] for r in rows) / len(rows)
    out["op_acc"] = sum(r["op_ok"] for r in rows) / len(rows)
    out["act_acc"] = sum(r["act_ok"] for r in rows) / len(rows)
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", help="mock | llamacpp (默认取配置)")
    ap.add_argument("--endpoint", help="llama-server 地址")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=160)
    ap.add_argument("--escalate", help="复核模型地址 (如 http://127.0.0.1:8785): 每条都请它复核一次, 报告按阈值升级的效果")
    args = ap.parse_args()
    overrides = []
    if args.engine:
        overrides.append(f"llm.engine={args.engine}")
    if args.endpoint:
        overrides.append(f"llm.endpoint={args.endpoint}")
    cfg = load_config(overrides=overrides)
    llm = create_llm(cfg["llm"])
    if not await llm.health():
        sys.exit(f"LLM 不可用: {cfg['llm'].get('endpoint')} (先运行 python tools/start_llm.py)")

    esc = None
    if args.escalate:
        esc = create_llm({**cfg["llm"], "engine": "llamacpp", "logprobs": 0, **(cfg["llm"].get("escalate") or {}),
                          "endpoint": args.escalate})
        if not await esc.health():
            sys.exit(f"复核模型不可用: {args.escalate} (先运行 python tools/start_llm.py --which escalate)")
        await esc.complete(I.build_messages("你好"), schema=I.build_intent_schema(), max_tokens=4)  # 预热前缀

    cases = ([(*c, None, None, "base") for c in CASES] + [(*c, "state") for c in STATE_CASES]
             + [(*c, None, None, "na") for c in NA_CASES] + [(*c, None, None, "near") for c in NEAR_CASES])
    print(f"引擎: {llm.name} ({llm.label}), {len(cases)} 条 x {args.repeat} 轮, 先各预热一次")
    await run_stream(llm, "你好", args.max_tokens)
    await run_baseline(llm, "你好", args.max_tokens)

    results = {"baseline": [], "stream": []}
    for rep in range(args.repeat):
        for text, ops, expected, state, must, group in cases:
            for mode, fn in (("baseline", run_baseline), ("stream", run_stream)):
                r = await fn(llm, text, args.max_tokens, state)
                r["op_ok"], r["act_ok"] = check(r["op"], r["acts"], ops, expected)
                if must:  # 状态题还要回复里说对 (例如拿着的是书, 而且没说成没拿)
                    r["act_ok"] = r["act_ok"] and reply_ok(r["reply"], must)
                if mode == "stream":  # 服务端实际下发的是规则兜底和能力闸门之后的结果, 一并统计
                    fop, facts = I.repair(text, r["op"], r["acts"])
                    r["gated"] = I.unsupported(text, fop, facts, r.get("conf"))
                    if r["gated"]:  # 闸门改判成做不到, 服务端会把回复换成固定的坦白 (r["reply"] 留着模型原话)
                        fop, facts = "na", []
                    _, r["act_ok_repaired"] = check(fop, facts, ops, expected)
                    said = I.REFUSALS[0] if r["gated"] else r["reply"]
                    r["act_ok_repaired"] = r["act_ok_repaired"] and (not must or reply_ok(said, must))
                    if esc is not None:  # 复核: 同样的消息, 只要 op 和 acts, 再过一遍规则兜底 (和服务端一致)
                        t0 = time.perf_counter()
                        raw = await esc.complete(I.build_messages(text, state=I.describe_state(state)),
                                                 schema=I.build_intent_schema(), max_tokens=60)
                        r["esc_ms"] = (time.perf_counter() - t0) * 1000
                        obj = json.loads(raw)
                        eop = obj.get("op") if obj.get("op") in I.OPS else "chat"
                        _, r["esc_ok"] = check(*I.repair(text, eop, I.normalize_acts(obj.get("acts") or [])), ops, expected)
                        r["esc_ok"] = r["esc_ok"] and (not must or reply_ok(r["reply"], must))
                        r["esc_out"] = [eop, [a["do"] for a in I.normalize_acts(obj.get("acts") or [])]]
                r.update({"text": text, "rep": rep, "group": group})
                results[mode].append(r)
            b, s = results["baseline"][-1], results["stream"][-1]
            print(f"  {text:<14} 意图 {b['intent_ms']:7.0f} -> {s['intent_ms'] or float('nan'):7.0f} ms   "
                  f"开口 {b['speak_ms']:7.0f} -> {s['speak_ms'] or float('nan'):7.0f} ms   "
                  f"{'✓' if s['act_ok'] else '✗'} {s['op']} {[a['do'] for a in s['acts']]}" + (f" 「{s['reply']}」" if state else ""))
    desk = []
    for rep in range(args.repeat):
        for text, ops, expected, state, must in DESK_CASES:
            r = await run_stream(llm, text, args.max_tokens, state)
            r["op_ok"], r["act_ok"] = check(r["op"], r["acts"], ops, expected)
            r["act_ok"] = r["act_ok"] and (not must or reply_ok(r["reply"], must))
            r["sop"], r["sacts"], r["said"], r["how"] = desk_served(text, r["op"], r["acts"], r.get("conf"), r["reply"])
            r["served_ok"] = check(r["sop"], r["sacts"], ops, expected)[1] and (not must or reply_ok(r["said"], must))
            r.update({"text": text, "rep": rep, "group": "desk"})
            desk.append(r)
            print(f"  [桌面] {text:<12} {r['op']} {[a['do'] for a in r['acts']]} 「{r['reply']}」 -> {r['how']} "
                  f"{r['sop']} {[a['do'] for a in r['sacts']]} {'✓' if r['served_ok'] else '✗'}")
    await llm.close()

    summary = {mode: summarize(rows) for mode, rows in results.items()}
    ratio = lambda k, q: (round(summary["baseline"][k][q] / summary["stream"][k][q], 2)  # noqa: E731
                          if summary["stream"][k][q] else None)
    lines = [
        f"# LLM 意图延迟基准 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"引擎 `{llm.name}` ({llm.label}), {len(cases)} 条输入 x {args.repeat} 轮 (其中 {len(STATE_CASES)} 条带角色状态, "
        f"{len(NA_CASES)} 条做不到的要求, {len(NEAR_CASES)} 条做得到但像做不到的).",
        "",
        "| 指标 | baseline P50 | baseline P95 | stream P50 | stream P95 | P50 加速 |",
        "|---|---|---|---|---|---|",
    ]
    for key, label in (("intent_ms", "请求→意图可执行 (ms)"), ("speak_ms", "请求→可以开口 (ms)"), ("total_ms", "请求→全部完成 (ms)")):
        b, s = summary["baseline"][key], summary["stream"][key]
        lines.append(f"| {label} | {b['p50']} | {b['p95']} | {s['p50']} | {s['p95']} | {ratio(key, 'p50')}x |")
    lines += [
        "",
        "| | baseline | stream |",
        "|---|---|---|",
        f"| 平均输出 token | {summary['baseline']['tokens_mean']} | {summary['stream']['tokens_mean']} |",
        f"| JSON 解析成功率 | {summary['baseline']['parse_ok']:.0%} | {summary['stream']['parse_ok']:.0%} |",
        f"| op 正确率 | {summary['baseline']['op_acc']:.0%} | {summary['stream']['op_acc']:.0%} |",
        f"| 动作正确率 | {summary['baseline']['act_acc']:.0%} | {summary['stream']['act_acc']:.0%} |",
        f"| 动作正确率 (含空间指令规则兜底, 即服务端实际下发) | - | "
        f"{sum(r['act_ok_repaired'] for r in results['stream']) / len(results['stream']):.0%} |",
    ]
    lines += groups(results["stream"])
    lines += gate_sweep(results["stream"])
    lines += calibration(results["stream"])
    if esc is not None:
        lines += escalation(results["stream"], esc.label)
    lines += desk_report(desk)
    if llm.name == "mock":
        lines += ["", "> mock 引擎按近似 token 数和固定速率模拟耗时, 只能说明脚本能跑通和输出量的差别, 不代表真实模型速度."]
    report = "\n".join(lines) + "\n"
    out_dir = ROOT / cfg["trace"]["log_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"bench-{datetime.now():%Y%m%d-%H%M%S}"
    stem.with_suffix(".md").write_text(report, encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps({"summary": summary, "results": results, "desk": desk}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    print()
    print(report)
    print(f"已写入 {stem.with_suffix('.md')}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
