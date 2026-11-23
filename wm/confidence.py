"""意图置信度: 从 llama-server 返回的 token 概率算出每个决策字段 (op, 每个动作的 do / to, emo) 选中值的概率.

两种概率都支持:
  - post_sampling_probs (默认): 采样之后的前 K 个候选 ("prob"/"top_probs"), 已经过 JSON 约束, 几乎不增加耗时;
    它带着采样温度 T, 按 p^T 再归一化还原成温度 1 的比例 (在候选集合内部是精确的).
  - logprobs: 约束之前的原始分布 ("logprob"/"top_logprobs"), 要对整个词表排序, 每轮多 100-150 ms, 只做对照.

做法: 找到字段值在输出文本里的起点, 逐个 token 往后走. 每一步只看"接上去以后还能拼成某个合法枚举值"的候选,
把选中那个的概率在这些候选里重新归一化, 各步连乘, 直到值的结束引号.

整体置信度 = op 和所有动作 do/to 的乘积 (把各个决策当作独立的联合概率), 用来决定要不要走升级路径.
云端模型拿不到 token 概率, 改用 rule_confidence 按原话里有没有技能的说法估.
"""
from __future__ import annotations

import math
import re

_FIELD = re.compile(r'"(op|do|to|emo)"\s*:\s*"')


def _weight(entry: dict, temperature: float) -> float:
    if "logprob" in entry:
        return math.exp(entry["logprob"])
    p = entry.get("prob", 0.0)
    return p ** temperature if p > 0 else 0.0


def _value(tokens: list[dict], offsets: list[int], start: int, enum: list[str], temperature: float) -> tuple[str, float] | None:
    """从字符位置 start (值的第一个字符) 开始, 返回 (选中的值, 置信度)."""
    i = next((k for k in range(len(tokens)) if offsets[k] <= start < offsets[k] + len(tokens[k]["token"])), None)
    if i is None:
        return None
    pre = tokens[i]["token"][:start - offsets[i]]
    acc, conf = "", 1.0
    while i < len(tokens):
        tok = tokens[i]

        def extend(text: str):
            if not text.startswith(pre):
                return None
            rest = text[len(pre):]
            q = rest.find('"')
            v = acc + (rest if q < 0 else rest[:q])
            if q >= 0:
                return (v, True) if v in enum else None
            return (v, False) if any(e.startswith(v) for e in enum) else None

        cands = {c["token"]: _weight(c, temperature) for c in (tok.get("top_logprobs") or tok.get("top_probs") or [])}
        cands[tok["token"]] = _weight(tok, temperature)
        mass = sum(w for t, w in cands.items() if extend(t) is not None)
        chosen = extend(tok["token"])
        if chosen is None:
            return None
        conf *= cands[tok["token"]] / mass if mass > 0 else 1.0
        acc, closed = chosen
        if closed:
            return acc, conf
        i += 1
        pre = ""
    return None


def field_confidence(tokens: list[dict], enums: dict[str, list[str]], temperature: float = 1.0) -> dict | None:
    """tokens: [{"token", "prob", "top_probs"}] 或 [{"token", "logprob", "top_logprobs"}]; temperature: 采样温度
    (只对 prob 格式有用). 返回
    {"op": p, "acts": [{"do": p, "to": p}], "emo": p, "overall": 乘积} (只含已经出现的字段); 没有概率时返回 None."""
    if not tokens:
        return None
    offsets, text = [], ""
    for t in tokens:
        offsets.append(len(text))
        text += t["token"]
    out: dict = {"acts": []}
    for m in _FIELD.finditer(text):
        key = m.group(1)
        r = _value(tokens, offsets, m.end(), enums.get(key, []), temperature)
        if r is None:
            continue
        p = round(r[1], 4)
        if key == "do":
            out["acts"].append({"do": p})
        elif key == "to":
            if out["acts"]:
                out["acts"][-1]["to"] = p
        else:
            out.setdefault(key, p)
    if "op" not in out:
        return None
    overall = out["op"]
    for a in out["acts"]:
        overall *= a.get("do", 1.0) * a.get("to", 1.0)
    out["overall"] = round(overall, 4)
    return out


def rule_confidence(text: str, op: str, acts: list[dict], can=None, room: bool = True) -> dict:
    """拿不到 token 概率时 (云端模型) 按规则估置信度, 格式和 field_confidence 一样 (另带 rule: True):
    op 是 act 时, 原话是做得到的动作 (intent.doable) 记 1, 不是记 0.5; 每个动作的技能在原话里有说法 (SKILL_CUES) 记 1, 没有记 0.5.
    整体是乘积. 原话里没有做得到的动作却要做动作时整体不超过 0.5, 低于能力闸门的阈值, 和小模型没把握时一样改判成做不到."""
    from . import intent as I

    said = set(I.asked_cannot(text, frozenset()))  # 按"什么都做不了的身体"问: 原话里说到的所有技能
    out = {"op": 1.0 if op != "act" or I.doable(text, can, room) else 0.5,
           "acts": [{"do": 1.0 if a["do"] in said else 0.5} for a in acts], "rule": True}
    overall = out["op"]
    for a in out["acts"]:
        overall *= a["do"]
    out["overall"] = round(overall, 4)
    return out
