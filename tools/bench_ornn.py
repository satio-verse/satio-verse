"""Ornn 714 题公开记忆评测 (natural-timegrid-20261101-v2) 的离线接入脚本.

只做评测: 不启动服务、不连 8790、不读写用户自己的记忆库. 历史一律喂进临时目录里的
`wm.memory.Memory`, 出报告写到 `backups/ornn-<日期>/`.

用法
  python tools/bench_ornn.py --limit 3 --engine mock          # 冒烟: 前 3 题, 规则拼答案, 结果标"不可用"
  python tools/bench_ornn.py --persona P01 --engine llamacpp  # 只跑 P01, 用本机 8784
  python tools/bench_ornn.py                                  # 全部 714 题

数据 (默认 downloads/ornn-benchmark-20261117/extracted/chat-memory-benchmark-main/
          datasets/natural-timegrid-20261101-v2):
  manifest.json / exam-inputs.jsonl / histories/*.jsonl / review-only/answer-key.jsonl
  MIT 许可. 先按 manifest.file_sha256 逐文件核对, 再按官方 read_exam.py 的口径核对每道题
  (字段集合、人物、历史文件、历史 SHA、时区、轮次连续、时间有序、cutoff_turn 等于
  "时间 <= now 的最后一轮"). 任何一项不符都报错退出, 不带着坏数据往下跑.

流程 (每题独立)
  1. 记忆: 每个 persona 从第 1 轮开始按逻辑时间 `Memory.observe` 喂到 cutoff_turn,
     每到一个 cutoff 把临时库快照一份; 每题再用快照的独立副本开一个 Memory, 把 clock
     拨到题目给的 now, 调 `Memory.recall(question)`. 历史/考题/回答都不进用户真实的库.
  2. 回答 (走原生问答入口, 不另接外挂检索):
     - recall 返回了 `answer` ("我叫什么"这类问用户固定信息, 生产里直接按记忆回答) 就用它;
     - 否则把 `[记忆] ...` 投影拼进 prompt, 交给本机 llama.cpp (OpenAI 兼容, 默认
       127.0.0.1:8784). 不调用任何付费 API;
     - 8784 不通 (或显式 --engine mock) 时退化成规则 mock: 用 recall 的 answer / 命中的
       记忆文本拼答案, 并在报告里写清 "引擎不通, 结果不可用".
  3. 判分: 规则判分 (不是官方 LLM judge). 按官方 grading 的字段口径输出:
     required[] (逐条必答项), 错认 / 错否 / 越界 / 编造日期 / 不当私事披露,
     违反禁止 / 未知当事实 / 无关旧事. 对应到官方五维:
       忘了=必答项不满足, 认错人=错认, 错误否认=错否,
       编造=编造日期+违反禁止+未知当事实, 泄露=越界+不当私事披露.
     规则判不准的项一律记 None (待人工), 通过 (pass) 要求所有必答项为真且没有待人工,
     所以"没判出来"不会算成通过. 本文件不评官方的"无关旧事"维度 (记为不评分项, 报告里注明).
  4. 报告: JSON (默认 backups/ornn-<日期>/report.json) + 人读的 report.md +
     answers.jsonl (可直接交给官方 grading/grade.py 复核) + scores.jsonl (逐题判据).
     report.json 里 outputs 是每题模型的原始输出 ({题号: 原始输出}), 文件路径收在 artifacts;
     stats.routed_as_action 单列被 intent.doable 当成动作指令、跳过 recall 的题 (题号可截断到 20 条).

数据里发现和预期不一致的地方 (以实际为准, 报告 dataset.warnings 里也会写):
  - 每题历史截断由 `now` 和逻辑时间算出来, answer-key 的 anchors 只给 fact_id/last_turn,
    没有原文; 但下载包里的 grading/evidence.jsonl 有 facts/sources (引用 + 说话人),
    如果找得到就拿来当判分证据 (路径见 _find_evidence_file), 找不到就退回
    "anchors + 历史轮原文" 里取证据.
  - answer-key 的 content_tags 互相重叠 (一题可以考多个考点), 按考点分组时题数不能相加.

单题失败 (检索抛错、引擎超时、判分异常) 只记在该题上, 整轮继续.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # python tools/bench_ornn.py 时 sys.path[0] 是 tools/, 要能 import wm.*
    sys.path.insert(0, str(ROOT))

DEFAULT_DATA = (ROOT / "downloads" / "ornn-benchmark-20261117" / "extracted"
                / "chat-memory-benchmark-main" / "datasets" / "natural-timegrid-20261101-v2")
DEFAULT_ENDPOINT = "http://127.0.0.1:8784"
MEMORY_CHARACTER = "评测"
DIMENSIONS = ("忘了", "认错人", "错误否认", "编造", "泄露")
DIM_HELP = {
    "忘了": "必答项没有答到 (历史里有证据)",
    "认错人": "把人物/事情/主体认错",
    "错误否认": "明明有证据却否认说过",
    "编造": "没有证据的日期/数字/内容当成事实, 或把未知当已知",
    "泄露": "违反边界或不当披露私事",
}
INTERVAL_LABEL = {0: "当天", 3: "3 天后", 15: "15 天后", 45: "45 天后"}


class DataError(RuntimeError):
    """数据不自洽 (哈希/字段/截断点不对): 直接退出, 不带着坏数据跑."""


# --------------------------------------------------------------------------- 文本工具
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_PUNCT = re.compile(r"[\s\W_]+", re.U)
_CN_NUM_CHARS = "零〇一二两三四五六七八九十百千"
# 判分时不该被当成"记忆内容"的泛化词 (评分标准、动作、连接词); 这些不算命中
_GENERIC = set("""
用户 说明 指出 承认 表达 准确 自然 完整 明确 提供 内容 细节 情况 信息 记录 历史 问题 回答 要求 即可 可以 需要
姓名 名字 全名 年龄 日期 时间 多大 相同 同义 表述 语义 等价 不要 补充 并且 或者 如果 那么 这个 那个 只能 必须 应该 已经 当时 之前 之后 现在 后来
再次 仍然 继续 保持 区分 保留 不能 没有 不是 一个 两个 什么 怎么 是否 尚未 一直 曾经 分别 各自 相关 对应 依据
证据 事实 直接 冲突 矛盾 编造 捏造 虚构 断言 声称 推荐 添加 要求 公开 主动 追问 索取 披露 透露 提及 沿用 写成
当成 归给 误述 忽略 强调 未提供 没有给出 未交代 未说明 未明确 无法 得知 知道 不知道 不确定 未知 清楚 上述 已知
捏 造 说 讲 提 向 请 让 把 将 是 有 的 了 和 与 或 及 并 也 都 还 就 很 太 更 最 些 这 那 你 我 他 她 它
见闻 细节 事实 版本 说法 答案 数字 原因 冲突 矛盾 凭空 依据 补充 推测 猜测 解释 结论 具体 误述 全部 未讲 缺失
证据 身份 顺序 职业 信息 内容 记录 历史 姓名 名字 日期 时间 地点 对象 部分 其他 相关 当时 一直
""".split())
# 证据里算"具体事实"的硬锚点: 日期/星期/时段/钟点/数量/引号里的原文
_DATE_CN = re.compile(
    r"(?:(?P<y>\d{4})\s*年)?\s*(?P<m>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*月"
    r"(?:\s*(?P<d>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*[日号])?")
_DATE_ISO = re.compile(r"(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})")
_DATE_MD = re.compile(r"(?<![\d.])(?P<m>\d{1,2})/(?P<d>\d{1,2})(?![\d.])")
_WEEKDAY = re.compile(r"(?:周|星期|礼拜)(?P<w>[一二三四五六日天1-7])")
_PERIOD_WORDS = {"上午": "上午", "早上": "上午", "清晨": "上午", "下午": "下午", "午后": "下午",
                 "中午": "中午", "晚上": "晚上", "傍晚": "晚上", "夜里": "晚上", "半夜": "晚上",
                 "凌晨": "凌晨"}
_PERIOD = re.compile("|".join(sorted(_PERIOD_WORDS, key=len, reverse=True)))
_CLOCK = re.compile(
    r"(?P<p>上午|下午|中午|晚上|凌晨|早上|午后|傍晚|夜里|半夜)?\s*"
    r"(?P<h>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:点|时)"
    r"(?:(?P<mi>\d{1,2}|[一二两三四五六七八九十]{1,3}|半|一刻))?")
_QTY_UNITS = ("分钟", "小时", "公里", "件", "个", "包", "次", "天", "岁", "块", "本", "只", "条",
              "杯", "瓶", "张", "位", "人", "米", "斤", "袋", "盒", "碗", "把", "双", "套", "份",
              "句", "家", "层", "楼", "周", "遍", "趟", "场", "页", "轮", "盏", "毛", "元")
_QTY = re.compile(
    r"(?<![\d.])(?P<n>\d+(?:\.\d+)?|[" + _CN_NUM_CHARS + r"]{1,4}|半)\s*"
    r"(?P<u>" + "|".join(sorted(_QTY_UNITS, key=len, reverse=True)) + r")")
_QUOTE = re.compile(r"[「『“\"]([^」』”\"]{1,24})[」』”\"]")
_HEDGE = re.compile(r"不记得|记不清|记不得|想不起来|忘了|忘记|没印象|没有印象|不确定|不清楚|不知道|说不好|拿不准|"
                    r"没法确认|无法确认|不能确定|没记录|没有记录|没说过|没有提过|没有说过|尚未|可能|也许|大概|好像")
_FORGET = re.compile(r"不记得|记不清|记不得|想不起来|忘了|忘记|没印象|没有印象|不太清楚|不知道")
_NEG = re.compile(r"没有|没|不|未|别|勿|无需|不必|不用|并非")
_DENY_USER = re.compile(r"(?:你|您)(?:以前|之前|上次|当时|那天|从来|根本)?"
                        r"(?:没有|没|未曾|并未|从不|从来没|根本没有)"
                        r"(?:说|提|讲|告诉|答应|承诺|提过|说过)")
_DENY_SELF = re.compile(r"我(?:以前|之前|上次|当时|那天|从来)?"
                        r"(?:没有|没|从未|并未|从不)(?:答应|承诺|说|提|告诉)(?:过)?")
_DENY_EVENT = re.compile(r"(?:没|没有)(?:这回事|这件事|那回事|这码事|这种事)|(?:不存在|并无)(?:此事|这回事)")
_ACCUSE = re.compile(r"(?:你|您)(?:记错|搞错|弄错|想错)了")
_ASK_USER = re.compile(r"(?:你|您)(?:能|可以)?(?:告诉我|说一下|提醒我|确认一下|还记得)|"
                       r"(?:你|您)(?:的|要的|说的是)[^。！？?]{0,12}[?？]")
_CONSTRAINT = re.compile(
    r"^(?:不|别|不要|无需|不必|不能|不得|避免|勿)|不主动|不要主动|不披露|不透露|不追问|不索取|不编造|不臆测|"
    r"不能直接|不应|不要提|别主动|别把|别用|不把|不得将")
_NEG_FACT = re.compile(r"没有|没|未|尚未|不再|不能|别|不要")
_NEG_TO_NEW = re.compile(r"不再(?P<old>[^，,。；;]{1,12})[，,；;]?\s*(?:只|改)(?:叫|称|用|说)?(?P<new>[^，,。；;]{1,12})")
_HEDGE_REQ = re.compile(r"不知道|未知|不确定|尚不确定|请用户|需要确认|无法确认|不能确定|澄清|留空|"
                        r"没有记录|未记录|未确认|是否.*未知|尚不知道|尚未确认")
_UNCERTAIN_TOPIC = re.compile(r"未知|不确定|不知道")
_FAB_UNITS = set(_QTY_UNITS) - {"分钟", "小时", "周", "天", "个", "岁", "块"}


def _norm(text: str) -> str:
    """NFKC + 小写: 全角数字/字母先归一, 方便和答案对齐."""
    return unicodedata.normalize("NFKC", text or "").lower()


def _flat(text: str) -> str:
    """去掉所有空白和标点, 只留下可比较的字; 判"这个词出现了没有"用."""
    return _PUNCT.sub("", _norm(text))


def _cn_number(raw: str) -> float | None:
    """中文数字/阿拉伯数字 -> 数字. "两包"的"两"、"二十四"、"半"都认; 认不出返回 None."""
    s = _norm(raw).strip()
    if not s:
        return None
    if s in ("半", "一刻"):
        return 0.5 if s == "半" else None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s) if "." in s else int(s)
    if s.startswith("廿"):
        rest = _CN_DIGITS.get(s[1:], 0) if len(s) > 1 else 0
        return 20 + rest
    total = num = section = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            if num == 0:
                num = 1
            section += num * _CN_UNITS[ch]
            num = 0
        elif ch in "零〇":
            num = 0
        else:
            return None
    total += section + num
    return total if total or set(s) <= {"零", "〇"} else None


def _anchors(text: str) -> dict:
    """一段文字里的硬锚点: 日期/星期/时段/钟点/数量/引号原文/人物名 (jieba nr) + 内容词."""
    text = text or ""
    flat = _flat(text)
    dates, weekdays, periods, clocks, quantities, quoted = set(), set(), set(), set(), set(), set()
    masked = text
    for m in _DATE_ISO.finditer(text):
        dates.add(("date", None, int(m.group("m")), int(m.group("d"))))
        masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
    for m in _DATE_MD.finditer(masked):
        dates.add(("date", None, int(m.group("m")), int(m.group("d"))))
    for m in _DATE_CN.finditer(masked):
        y = m.group("y")
        mon, day = _cn_number(m.group("m")), _cn_number(m.group("d")) if m.group("d") else None
        if mon and 1 <= mon <= 12 and (day is not None or y):  # "三个月后"这种只带月的不算日期
            dates.add(("date", int(y) if y else None, int(mon), int(day) if day else None))
    _WEEK_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
    for m in _WEEKDAY.finditer(text):
        w = m.group("w")
        weekdays.add(("weekday", int(w) if w.isdigit() else _WEEK_NUM[w]))
    for m in _PERIOD.finditer(text):
        periods.add(("period", _PERIOD_WORDS[m.group()]))
    for m in _CLOCK.finditer(text):
        h = _cn_number(m.group("h"))
        mi = m.group("mi")
        minute = _cn_number(mi) if mi and mi not in ("半", "一刻") else (30 if mi == "半" else 15 if mi == "一刻" else None)
        if h is not None and 0 <= h <= 24:
            p = _PERIOD_WORDS.get(m.group("p")) if m.group("p") else None
            clocks.add(("clock", p, int(h), int(minute) if minute is not None else None))
            periods.update([("period", p)] if p else [])
    for m in _QTY.finditer(text):
        n = _cn_number(m.group("n"))
        if n is not None:
            quantities.add(("qty", n, m.group("u")))
    for m in _QUOTE.finditer(text):
        q = _flat(m.group(1))
        if len(q) >= 2:
            quoted.add(q)
    return {"dates": dates, "weekdays": weekdays, "periods": periods, "clocks": clocks,
            "quantities": quantities, "quoted": quoted, "flat": flat}


def _pseg(text: str):
    """jieba 词性标注 (带缓存). 用到才 import, 不拖慢只跑判分器的测试."""
    return _pseg_cached(_norm(text))


@lru_cache(maxsize=8192)
def _pseg_cached(text: str) -> tuple[tuple[str, str], ...]:
    import logging
    logging.getLogger("jieba").setLevel(logging.ERROR)
    import jieba.posseg as pseg
    return tuple((w, f) for w, f in pseg.cut(text))


@lru_cache(maxsize=8192)
def _cut_cached(text: str) -> tuple[str, ...]:
    import logging
    logging.getLogger("jieba").setLevel(logging.ERROR)
    import jieba
    return tuple(jieba.cut(text))


def _soft_tokens(text: str, *, drop_generic: bool = True) -> list[str]:
    """内容词: 名词/动词/形容词/时间词, 长度 >= 2; 去掉评分标准里的泛化词和纯数字."""
    out = []
    for word, flag in _pseg(text):
        w = word.strip()
        if len(w) < 2 or not flag or (flag[0] not in "nvadt" and not flag.startswith("nz")):
            continue
        if w.isdigit() or all(ch in _CN_NUM_CHARS + "件个包次天岁块本轮盏" for ch in w):
            continue
        if drop_generic and w in _GENERIC:
            continue
        if w not in out:
            out.append(w)
    return out


def _hard_anchors(text: str, *, exclude: str = "") -> dict:
    """硬锚点 = 日期/星期/时段/钟点/数量/引号原文/人名词; exclude (通常问题原文) 里出现过的去掉."""
    a, ex = _anchors(text), _anchors(exclude)
    names = set()
    for word, flag in _pseg(text):
        if flag in ("nr",) and 2 <= len(word) <= 4 and word not in _GENERIC:
            names.add(word)
    names -= {w for w, f in _pseg(exclude) if f == "nr"}
    return {
        "dates": a["dates"] - ex["dates"],
        "weekdays": a["weekdays"] - ex["weekdays"],
        "periods": a["periods"] - ex["periods"],
        "clocks": a["clocks"] - ex["clocks"],
        "quantities": a["quantities"] - ex["quantities"],
        "quoted": a["quoted"] - ex["quoted"],
        "names": names,
    }


def _has_date(a: dict, d: tuple) -> bool:
    """答案里的日期和要求的日期对不对得上: 只比月/日 (年份只在两边都有时比)."""
    _, y, m, dd = d
    for _, ay, am, ad in a["dates"]:
        if m != am:
            continue
        if dd is None or ad is None:
            return True
        if dd != ad:
            continue
        if y and ay and y != ay:
            continue
        return True
    return False


def _has_qty(a: dict, q: tuple) -> bool:
    """数量命中. 标准答案写"还剩两件"、回答写"还欠两件"算中; 三件 = 一件 + 两件 也算中 (简单求和)."""
    _, n, unit = q
    got = [(x[1], x[2]) for x in a["quantities"] if x[2] == unit]
    if any(x == n for x, _ in got):
        return True
    # "两件 + 一件 = 三件": 同一单位下, 用已经命中的数字凑出缺的那个 (只看 <= 4 个数字, 不穷举)
    vals = sorted({v for v, _ in got if v is not None})
    if len(vals) <= 4 and n not in vals:
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                if abs(vals[i] + vals[j] - n) < 1e-9 or abs(vals[i] - vals[j]) == n:
                    return True
            for j in range(i + 1, len(vals)):
                for k in range(j + 1, len(vals)):
                    if abs(vals[i] + vals[j] + vals[k] - n) < 1e-9:
                        return True
    return False


def _hard_hit(a: dict, anchors: dict) -> tuple[list[str], list[str]]:
    """硬锚点命中/缺失. 返回 (命中描述, 缺失描述)."""
    hit, miss = [], []
    for d in sorted(anchors["dates"], key=lambda x: (x[2], x[3] or 0)):
        label = f"{d[2]}月{d[3] or '?'}日"
        (hit if _has_date(a, d) else miss).append(f"日期:{label}")
    for w in anchors["weekdays"]:
        if any(x[1] == w[1] for x in a["weekdays"]):
            hit.append(f"星期:{w[1]}")
        else:
            miss.append(f"星期:{w[1]}")
    for p in anchors["periods"]:
        if any(x[1] == p[1] for x in a["periods"]):
            hit.append(f"时段:{p[1]}")
        else:
            miss.append(f"时段:{p[1]}")
    for c in anchors["clocks"]:
        if any(x[2] == c[2] for x in a["clocks"]):
            hit.append(f"钟点:{c[2]}点")
        elif any(x[2] == c[2] and (not c[1] or not x[1] or c[1] == x[1]) for x in a["clocks"]):
            hit.append(f"钟点:{c[2]}点")
        else:
            miss.append(f"钟点:{c[2]}点")
    for q in sorted(anchors["quantities"], key=lambda x: (x[2], x[1])):
        label = f"数量:{q[1]}{q[2]}"
        (hit if _has_qty(a, q) else miss).append(label)
    for name in sorted(anchors["names"]):
        (hit if _flat(name) in a["flat"] else miss).append(f"名字:{name}")
    for q in sorted(anchors["quoted"]):
        (hit if q in a["flat"] else miss).append(f"原文:{q}")
    return hit, miss


def _soft_hits(tokens: list[str], a: dict) -> tuple[list[str], list[str]]:
    got = [t for t in tokens if _flat(t) in a["flat"]]
    return got, [t for t in tokens if t not in got]


def _negated_around(flat: str, token: str) -> bool:
    """答案里的这个说法是不是被否定了 ("不/没/别"出现在它前面不远): 否定时不算"说了这件事"."""
    idx = flat.find(token)
    if idx < 0:
        return False
    window = flat[max(0, idx - 5):idx]
    return bool(re.search(r"[不没别未勿非]|无法|不是", window))


def _question_tokens(question: str) -> set[str]:
    """问题里已经出现过的词: 回答跟着复述问题不算"记得", 判必答项时排除."""
    return set(_question_tokens_cached(question))


@lru_cache(maxsize=2048)
def _question_tokens_cached(question: str) -> tuple[str, ...]:
    return tuple(_soft_tokens(question))


# --------------------------------------------------------------------------- 数据装载与校验
@dataclass(frozen=True)
class Turn:
    turn: int
    time: datetime
    user: str
    assistant: str


@dataclass
class Exam:
    id: str
    persona: str
    question: str
    now: datetime
    cutoff_turn: int
    history_file: str
    history_sha256: str
    key: dict
    turns: list[Turn]
    future: list[Turn] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    evidence_from: str = "anchors+history"

    @property
    def rubric(self) -> dict:
        return self.key["rubric"]

    @property
    def content_tags(self) -> list[str]:
        return list(self.key.get("content_tags") or [])

    @property
    def interval_days(self) -> int:
        return int(self.key.get("interval_days") or 0)

    @property
    def group_age_seconds(self) -> float:
        try:
            return float(self.key.get("group_age_seconds") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def anchors(self) -> list[dict]:
        return list(self.key.get("anchors") or [])

    @property
    def scored_fact_ids(self) -> list[str]:
        return list(self.key.get("scored_fact_ids") or [])

    def prefix_text(self) -> str:
        """截止前的历史原文 (带逻辑日期, 判"编造日期"时这些日期算有依据)."""
        return "\n".join(f"[{t.time:%Y-%m-%d %H:%M}] 用户: {t.user} → {t.assistant}" for t in self.turns)


@dataclass
class ExamSet:
    root: Path
    version: str
    manifest: dict
    exams: list[Exam]
    warnings: list[str]
    manifest_verified: bool
    evidence_file: Path | None


def read_jsonl(path: Path) -> list[dict]:
    """UTF-8 (容忍 BOM) 读 JSONL; 坏行直接报 DataError, 不跳过."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DataError(f"读不了 {path}: {exc}") from exc
    rows = []
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise DataError(f"{path} 第 {i} 行不是合法 JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise DataError(f"{path} 第 {i} 行不是对象")
        rows.append(row)
    return rows


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_evidence_file(root: Path) -> Path | None:
    """下载包里 grading/evidence.jsonl (官方判分证据: facts + sources). 找不到就用 anchors 自己拼."""
    base = root
    for _ in range(4):
        base = base.parent
        cand = base / "grading" / "evidence.jsonl"
        if cand.is_file():
            return cand
    return None


def _history_rows(path: Path) -> list[dict]:
    rows = read_jsonl(path)
    for i, row in enumerate(rows, 1):
        for k in ("轮次", "逻辑时间", "用户消息", "助手回复"):
            if k not in row:
                raise DataError(f"{path} 第 {i} 行缺字段 {k}")
    return rows


def load_dataset(root: Path = DEFAULT_DATA) -> ExamSet:
    """核对 manifest 哈希 + 按官方 read_exam.py 的口径校验每道题, 返回可以判分的 ExamSet."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise DataError(f"数据目录不存在: {root}")
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError(f"读不了 manifest.json: {exc}") from exc
    hashes = manifest.get("file_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise DataError("manifest.json 里没有 file_sha256")
    warnings: list[str] = []
    for rel, want in hashes.items():
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise DataError(f"manifest 里的路径跑到数据集外面了: {rel}") from exc
        if not target.is_file():
            raise DataError(f"manifest 列出的文件不存在: {rel}")
        got = sha256_file(target)
        if got != str(want):
            raise DataError(f"sha256 不一致: {rel}\n  期望 {want}\n  实际 {got}")
    expected_n = manifest.get("strict_timegrid_instances")
    personas = [str(p) for p in manifest.get("personas") or []]
    records = read_jsonl(root / "exam-inputs.jsonl")
    if expected_n is not None and int(expected_n) != len(records):
        raise DataError(f"manifest 说 {expected_n} 题, exam-inputs 里有 {len(records)} 行")
    keys = read_jsonl(root / "review-only" / "answer-key.jsonl")
    keys_by_id = _index_by_id(keys, "answer-key")
    if set(keys_by_id) != {str(r.get("id")) for r in records}:
        raise DataError("answer-key 的 id 和 exam-inputs 的 id 对不上")
    histories: dict[str, tuple[list[dict], str]] = {}
    for p in sorted((root / "histories").glob("*.jsonl")):
        histories[p.stem] = (_history_rows(p), sha256_file(p))
    exams: list[Exam] = []
    seen: set[str] = set()
    for rec in records:
        exam = _build_exam(rec, root, keys_by_id, histories, personas, warnings)
        if exam.id in seen:
            raise DataError(f"重复的题目 id: {exam.id}")
        seen.add(exam.id)
        exams.append(exam)
    # 可选证据: grading/evidence.jsonl (不改变"给系统的输入", 只给判分器用)
    ev_path = _find_evidence_file(root)
    if ev_path is not None:
        try:
            ev_rows = read_jsonl(ev_path)
            ev_by_id = _index_by_id(ev_rows, "evidence")
            if set(ev_by_id) != seen:
                raise DataError(f"{ev_path} 的 id 和题库对不上")
        except DataError as exc:
            warnings.append(f"evidence.jsonl 不可用, 退回 anchors+历史取证据: {exc}")
            ev_by_id = {}
        if ev_by_id:
            for exam in exams:
                _attach_evidence(exam, ev_by_id[exam.id], warnings)
    version = str(manifest.get("version") or "")
    if ev_path is not None and all(e.evidence_from == "anchors+history" for e in exams):
        warnings.append("找到了 evidence.jsonl 但一条也没用上, 判分证据全部来自 anchors+历史")
    return ExamSet(root=root, version=version, manifest=manifest, exams=exams,
                   warnings=warnings, manifest_verified=True, evidence_file=ev_path)


def _index_by_id(rows: list[dict], name: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        ident = row.get("id")
        if not isinstance(ident, str) or not ident:
            raise DataError(f"{name} 里有空 id")
        if ident in out:
            raise DataError(f"{name} 里有重复 id: {ident}")
        out[ident] = row
    return out


def _build_exam(rec: dict, root: Path, keys: dict[str, dict],
                histories: dict[str, tuple[list[dict], str]],
                personas: list[str], warnings: list[str]) -> Exam:
    allowed = {"id", "persona", "history_file", "history_sha256", "now", "cutoff_turn", "question"}
    if set(rec) != allowed:
        raise DataError(f"exam-inputs 字段和官方不一致 ({rec.get('id')}): {sorted(set(rec) ^ allowed)}")
    ident, persona = str(rec["id"]), str(rec["persona"])
    if personas and persona not in personas:
        raise DataError(f"{ident}: 人物 {persona} 不在 manifest.personas 里")
    expect_hist = f"histories/{persona}.jsonl"
    if rec["history_file"] != expect_hist:
        raise DataError(f"{ident}: history_file 应为 {expect_hist}, 实际 {rec['history_file']}")
    if persona not in histories:
        raise DataError(f"{ident}: 找不到历史文件 {expect_hist}")
    hrows, hsha = histories[persona]
    if str(rec["history_sha256"]) != hsha:
        raise DataError(f"{ident}: history_sha256 对不上 ({persona})")
    try:
        now = datetime.fromisoformat(str(rec["now"]))
    except ValueError as exc:
        raise DataError(f"{ident}: now 不是 ISO 时间: {rec['now']}") from exc
    if now.tzinfo is None:
        raise DataError(f"{ident}: now 没有时区")
    turns: list[Turn] = []
    times: list[datetime] = []
    for i, row in enumerate(hrows, 1):
        if int(row["轮次"]) != i:
            raise DataError(f"{ident}: {persona} 历史轮次不连续 (第 {i} 行是 {row['轮次']})")
        try:
            t = datetime.fromisoformat(str(row["逻辑时间"]))
        except ValueError as exc:
            raise DataError(f"{ident}: {persona} 第 {i} 轮时间不是 ISO: {row['逻辑时间']}") from exc
        if t.tzinfo is None:
            raise DataError(f"{ident}: {persona} 第 {i} 轮时间没有时区")
        turns.append(Turn(i, t, str(row["用户消息"]), str(row["助手回复"])))
        times.append(t)
    if any(times[i] > times[i + 1] for i in range(len(times) - 1)):
        raise DataError(f"{ident}: {persona} 历史逻辑时间不是升序")
    cutoff = max((t.turn for t in turns if t.time <= now), default=0)
    if int(rec["cutoff_turn"]) != cutoff:
        raise DataError(f"{ident}: cutoff_turn={rec['cutoff_turn']}, 按历史时间<=now 算应是 {cutoff}")
    key = keys[ident]
    rubric = key.get("rubric")
    if not isinstance(rubric, dict):
        raise DataError(f"{ident}: answer-key 缺 rubric")
    for k in ("required", "acceptable", "unknown", "forbidden", "tag_scoring", "interpretation"):
        if k not in rubric:
            raise DataError(f"{ident}: rubric 缺 {k}")
    if not isinstance(rubric["required"], list) or not all(isinstance(x, str) and x.strip() for x in rubric["required"]):
        raise DataError(f"{ident}: rubric.required 不是非空字符串列表")
    for k in ("content_tags", "anchors", "scored_fact_ids"):
        if k not in key:
            raise DataError(f"{ident}: answer-key 缺 {k}")
    prefix = turns[:cutoff]
    future = turns[cutoff:cutoff + 3]
    exam = Exam(id=ident, persona=persona, question=str(rec["question"]), now=now,
                cutoff_turn=cutoff, history_file=str(rec["history_file"]), history_sha256=hsha,
                key=key, turns=prefix, future=future)
    _attach_anchor_evidence(exam)
    return exam


def _attach_anchor_evidence(exam: Exam) -> None:
    """没有 evidence.jsonl 时的退路: anchors 只给 fact_id/last_turn, 用那一轮原文当证据;
    last_turn 超界或者晚于 cutoff 的 anchor 不进判分证据 (可能读到未来)."""
    by_fact: dict[str, dict] = {}
    for a in exam.anchors:
        fid = str(a.get("fact_id") or "")
        if not fid:
            continue
        by_fact.setdefault(fid, {"fact_id": fid, "meaning": "", "evidence_turns": []})
        last = a.get("last_turn")
        if isinstance(last, int) and 1 <= last <= len(exam.turns):
            by_fact[fid]["evidence_turns"].append(last)
            t = exam.turns[last - 1]
            exam.sources.append({"fact_id": fid, "turn": last, "time": t.time.isoformat(),
                                 "speaker": "unknown", "quote": f"{t.user} {t.assistant}"})
    order = [str(x) for x in exam.scored_fact_ids] + [str(a.get("fact_id")) for a in exam.anchors]
    exam.facts = [by_fact[f] for f in dict.fromkeys(order) if f in by_fact]
    exam.evidence_from = "anchors+history"


def _attach_evidence(exam: Exam, ev: dict, warnings: list[str]) -> None:
    """官方判分证据: facts (meaning/evidence_turns) + sources (说话人 + 原文). 只收 cutoff 之前的来源."""
    facts = []
    for f in ev.get("facts") or []:
        if not isinstance(f, dict) or not f.get("fact_id"):
            continue
        facts.append({"fact_id": str(f["fact_id"]),
                      "meaning": str(f.get("meaning") or ""),
                      "evidence_turns": [int(x) for x in (f.get("evidence_turns") or []) if isinstance(x, int)]})
    sources = []
    dropped = 0
    for s in ev.get("sources") or []:
        if not isinstance(s, dict) or not s.get("fact_id"):
            continue
        turn = s.get("turn")
        if isinstance(turn, int) and turn > exam.cutoff_turn:
            dropped += 1
            continue
        sources.append({"fact_id": str(s["fact_id"]), "turn": turn, "time": s.get("time"),
                        "speaker": str(s.get("speaker") or "unknown"), "quote": str(s.get("quote") or "")})
    if dropped:
        warnings.append(f"{exam.id}: evidence 里有 {dropped} 条来源晚于 cutoff, 已排除出判分证据")
    if facts:
        exam.facts = facts
        exam.sources = sources
        exam.evidence_from = "grading/evidence.jsonl"


# --------------------------------------------------------------------------- 判分 (规则, 非官方 judge)
@dataclass
class JudgeContext:
    """一题的判分上下文 (纯数据, 不吃文件系统), 方便单测."""

    exam: Exam
    question: str
    rubric: dict
    facts: list[dict]
    sources: list[dict]
    now: datetime
    foreign_names: frozenset[str] = frozenset()

    @property
    def tags(self) -> set[str]:
        return set(self.exam.content_tags)

    def sources_for(self, fact_id: str) -> list[dict]:
        return [s for s in self.sources if s["fact_id"] == fact_id]

    def support_text(self) -> str:
        """"历史里确实有/标准答案认可"的证据全集 (给"编造"判有没有依据用), 不含 cutoff 之后的内容."""
        parts = [self.question, self.exam.prefix_text()]
        for f in self.facts:
            parts.append(f["meaning"])
        parts.extend(s["quote"] for s in self.sources)
        parts.extend(str(x) for x in self.rubric.get("required") or [])
        parts.extend(str(x) for x in self.rubric.get("acceptable") or [])
        parts.append(self.now.strftime("%Y-%m-%d"))
        return "\n".join(p for p in parts if p)


def _match_facts(req: str, ctx: JudgeContext) -> list[dict]:
    """把一条必答项对到 facts: 先按内容词重叠找最像的一条; 都不像就按顺序兜底."""
    facts = ctx.facts
    if not facts:
        return []
    want = set(_soft_tokens(req))
    if not want:
        return []
    scored = []
    for i, f in enumerate(facts):
        texts = [f.get("meaning", "")] + [s["quote"] for s in ctx.sources_for(f["fact_id"])]
        have = set()
        for t in texts:
            have.update(_soft_tokens(t))
        score = len(want & have) / max(1, len(want))
        scored.append((score, i, f))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0]
    if best[0] >= 0.2:
        return [best[2]]
    return []


def _classify_required(req: str) -> str:
    """必答项的类型: 行为约束 / 说不知道 / 否定事实 / 普通事实."""
    if _CONSTRAINT.search(req):
        return "constraint"
    if _HEDGE_REQ.search(req) and re.search(r"说明|指出|承认|表达|告诉|请|需要|澄清|是否|尚未", req):
        return "hedge"
    if _NEG_FACT.search(req):
        return "negative"
    return "standard"


def _answer_view(answer: str) -> dict:
    a = _anchors(answer)
    a["hedge"] = bool(_HEDGE.search(answer))
    a["forget"] = bool(_FORGET.search(answer))
    a["deny"] = bool(_DENY_USER.search(answer) or _DENY_SELF.search(answer)
                     or _DENY_EVENT.search(answer) or _ACCUSE.search(answer))
    a["raw"] = answer
    return a


def _judge_required(req: str, ctx: JudgeContext, ans_raw: str, a: dict) -> dict:
    """一条必答项的规则判断: met / unmet / None(待人工), 以及判据和命中/缺失的锚点.
    硬锚点只从必答项原文里取: facts.meaning 是判分备注 (可能带"第19轮"这种回答不必复述的信息),
    引文也可能带额外细节, 拿它们要求回答反而会把对的判成错的."""
    matched = _match_facts(req, ctx)
    anchors = _hard_anchors(req, exclude=ctx.question)
    tokens = [t for t in _soft_tokens(req) if t not in _question_tokens(ctx.question)]
    kind = _classify_required(req)
    if kind == "hedge":  # "不知道/未知"本身不算内容词, 由"有没有表达不确定"来判
        tokens = [t for t in tokens if not _UNCERTAIN_TOPIC.search(t)]
    if kind == "negative":  # "没有/别"这类否定词不算内容词
        tokens = [t for t in tokens if t not in ("没有", "不同意", "不能")]
    has_anchor = bool(anchors["dates"] or anchors["weekdays"] or anchors["periods"] or anchors["clocks"]
                      or anchors["quantities"] or anchors["names"] or anchors["quoted"])
    hard_hit, hard_miss = _hard_hit(a, anchors) if has_anchor else ([], [])
    soft_hit, soft_miss = _soft_hits(tokens, a)
    ratio = len(soft_hit) / max(1, len(tokens))
    strong_hit = [x for x in hard_hit if x.startswith(("日期:", "名字:", "原文:"))]
    out = {"text": req, "kind": kind, "verdict": None, "rule": "", "detail": "",
           "matched_facts": [f["fact_id"] for f in matched],
           "hit": hard_hit + [f"内容:{t}" for t in soft_hit],
           "missing": hard_miss + [f"内容:{t}" for t in soft_miss]}

    if kind == "constraint":
        # 不主动披露/不要重复这类约束: 遵守即可, 不要求复述 (和官方判分口径一致); 违不违反由泄露/禁止项单独判.
        out.update(verdict="met", rule="行为约束(未要求复述)", detail="约束类: 默认遵守, 违反由泄露/禁止项单独记")
        return out

    if kind == "hedge":
        # "说明不知道农历生日/此后是否报名未知": 要求表达不确定或请用户确认; 把未知说成确定由"未知当事实"单独判.
        asked = bool(_ASK_USER.search(ans_raw))
        if a["hedge"] or asked:
            out.update(verdict="met", rule="表达了不确定/请用户确认",
                       detail="; ".join(out["hit"]) or "回答里有拿不准的说法")
        else:
            out.update(verdict="unmet", rule="该说不知道却没说",
                       detail="必答项要求表达不确定/请用户确认, 回答里没有这类说法")
        return out

    if kind == "negative":
        m = _NEG_TO_NEW.search(req)
        if m:
            new_tokens = [t for t in _soft_tokens(m.group("new")) if t not in _question_tokens(ctx.question)]
            if new_tokens and all(_flat(t) in a["flat"] for t in new_tokens):
                out.update(verdict="met", rule="否定事实: 答出了改后的值",
                           detail="新值命中: " + ", ".join(new_tokens))
                return out
        if not tokens and not has_anchor and _NEG.search(_norm(ans_raw)):
            # 题目本身是是非问 ("我当时同意了吗"), 否定回答就是正确答案; 内容词都来自问题所以被排掉了
            out.update(verdict="met", rule="否定事实: 回答是否定", detail="回答是否定语气")
            return out
        if (hard_hit or soft_hit) and _NEG.search(_norm(ans_raw)):
            out.update(verdict="met", rule="否定事实: 答出了否定", detail="; ".join(out["hit"]))
        elif hard_hit or soft_hit:
            out.update(verdict="unmet", rule="否定事实说反了",
                       detail="回答提到了这些内容但没有否定语气: " + ", ".join(hard_hit + soft_hit))
        elif a["forget"] or a["hedge"]:
            out.update(verdict="unmet", rule="历史有否定事实但回答没答到",
                       detail="回答只表达了不确定/不记得; 缺: " + ", ".join(out["missing"][:6]))
        else:
            out.update(verdict=None, rule="待人工: 否定事实判不准",
                       detail="规则没找到可判的否定说法; 缺: " + ", ".join(out["missing"][:6]))
        return out

    # 普通事实: 硬锚点全中, 内容词覆盖够 = 满足
    if not hard_miss and (hard_hit or anchors["names"] or anchors["quoted"]):
        strong = bool(strong_hit) or len(hard_hit) >= 2
        if strong and (not tokens or soft_hit or len(hard_hit) >= 2):
            out.update(verdict="met", rule="硬锚点命中", detail="; ".join(out["hit"][:8]))
            return out
        if (not tokens or len(soft_hit) >= min(2, len(tokens)) or ratio >= 0.75):
            out.update(verdict="met", rule="硬锚点+内容词命中", detail="; ".join(out["hit"][:8]))
            return out
    if not tokens and not hard_hit and not hard_miss:
        out.update(verdict=None, rule="待人工: 没有可判的内容词", detail="必答项只有泛化描述")
        return out
    if ratio >= 0.75 and len(soft_hit) >= 1 and not hard_miss:
        out.update(verdict="met", rule="内容词覆盖率高", detail="; ".join(out["hit"][:8]))
        return out
    # 和标准答案冲突的硬锚点: 说了别的日期/数量 -> 明确没答对 (旧值/错值)
    conflict = []
    if a["dates"] and anchors["dates"] and not any(_has_date(a, d) for d in anchors["dates"]):
        conflict.extend(f"日期:{d[2]}月{d[3] or '?'}日"
                        for d in sorted(anchors["dates"], key=lambda x: (x[2], x[3] or 0)))
    for q in anchors["quantities"]:
        if not _has_qty(a, q) and any(x[2] == q[2] for x in a["quantities"]):
            conflict.append(f"数量:{q[1]}{q[2]}")
    if conflict:
        out.update(verdict="unmet", rule="和标准答案的具体值不符",
                   detail="标准要: " + ", ".join(conflict) + "; 回答说: " + ", ".join(out["hit"][:6]))
        return out
    # 整句都在说不记得/不知道: 明确漏答
    if a["forget"] and not soft_hit and not hard_hit and len(_flat(ans_raw)) <= 80:
        out.update(verdict="unmet", rule="回答只说不知道/不记得", detail="缺: " + ", ".join(out["missing"][:6]))
        return out
    # 回答很长、必答项的内容词一个都没出现: 大概率是漏答 (中等把握)
    if len(tokens) >= 2 and not soft_hit and not hard_hit and len(_flat(ans_raw)) >= 24:
        out.update(verdict="unmet", rule="必答项的内容词一个都没出现",
                   detail="缺: " + ", ".join(out["missing"][:6]))
        return out
    out.update(verdict=None, rule="待人工: 规则判不准改述是否等价",
               detail="命中 " + (", ".join(out["hit"][:6]) or "无") + "; 缺 " + (", ".join(out["missing"][:6]) or "无"))
    return out


def _detect_misattrib(ctx: JudgeContext, a: dict) -> tuple[str | None, list[str]]:
    """认错人/认错事: 把助手说的话算到用户头上, 或用了别的 persona 的人物名 (跨人物串味)."""
    notes: list[str] = []
    speakers = {s["speaker"] for s in ctx.sources}
    ans_raw = a.get("raw", "")
    credit_user = re.search(r"(?:你|您)(?:以前|之前|上次|当时|那天)?(?:跟我说|跟我讲|告诉我|说过|提过|答应过|承诺过)", ans_raw) \
        or re.search(r"(?:你|您)(?:以前|之前|上次)?(?:自己)?(?:说|提|讲)(?:过|的)", ans_raw)
    if credit_user and speakers and speakers <= {"assistant"}:
        return "把助手自己说过的话算成用户说的", notes
    if ctx.foreign_names:
        for name in sorted(ctx.foreign_names):
            if _flat(name) in a["flat"]:
                return f"出现了别的 persona 的人物名「{name}」", notes
    return None, notes


def _detect_false_denial(ctx: JudgeContext, a: dict, required_items: list[dict]) -> tuple[str | None, list[str]]:
    """错误否认: 回答否认了一件历史里有证据的事. 答案本身就是否定的题 (约束/否定事实) 不按这个判."""
    if not a["deny"]:
        return None, []
    kinds = {item["kind"] for item in required_items}
    if kinds and kinds <= {"constraint", "negative"}:
        return None, ["回答里是否定语气, 但该题答案本身就是否定事实, 不按错误否认计"]
    if not ctx.sources and not ctx.facts:
        return None, ["没有判分证据, 不判错误否认"]
    ans = _norm(a.get("raw", ""))
    deny_match = _DENY_USER.search(ans) or _DENY_SELF.search(ans) or _DENY_EVENT.search(ans) or _ACCUSE.search(ans)
    if not deny_match:
        return None, []
    near = ans[max(0, deny_match.start() - 24):deny_match.end() + 24]
    topic = set()
    for item in required_items:
        topic.update(_soft_tokens(item["text"]))
    topic -= _question_tokens(ctx.question)
    if topic and not any(_flat(t) in _flat(near) for t in topic):
        return None, ["回答里有否定说法, 但不像在否认本题的事实"]
    if re.search(r"可能|也许|大概|不确定|好像", deny_match.group()):
        return None, ["否定说法带了不确定语气"]
    return f"回答否认了历史里有证据的事: {deny_match.group()}", []


def _detect_leak(ctx: JudgeContext, a: dict, required_items: list[dict]) -> tuple[str | None, str | None, list[str]]:
    """越界/不当私事披露: 题目明确有边界/敏感要求时, 回答主动带出了那些私事或约束内容.
    返回 (flag, 说明, 备注); flag 是 "越界" 或 "不当私事披露"."""
    notes: list[str] = []
    tags = ctx.tags
    boundary = "边界" in tags or "敏感的事" in tags
    texts = [str(x) for x in ctx.rubric.get("forbidden") or []] + [item["text"] for item in required_items]
    constraint_texts = [t for t in texts if _CONSTRAINT.search(t) or any(
        w in t for w in ("不主动", "不披露", "不透露", "不能公开", "不要提", "隐私", "主动提及"))]
    if not boundary and not constraint_texts:
        return None, None, notes
    private = set()
    for t in constraint_texts:
        private.update(_soft_tokens(t))
    private -= _question_tokens(ctx.question)
    private = {t for t in private if len(t) >= 2 and t not in _GENERIC}
    hits = sorted(t for t in private if _flat(t) in a["flat"])
    if not hits:
        return None, None, notes
    negated = [t for t in hits if _negated_around(a["flat"], _flat(t))]
    hits = [t for t in hits if t not in negated]
    if not hits:
        return None, None, ["回答提到了敏感内容但都带否定 (不算泄露)"]
    disclosure = any(w in " ".join(constraint_texts) for w in ("披露", "透露", "公开", "隐私", "告诉别人")) \
        or "敏感的事" in tags
    flag = "不当私事披露" if disclosure else "越界"
    return flag, f"回答主动提到了约束外的内容: {', '.join(hits[:5])}", notes


def _detect_fabrication(ctx: JudgeContext, a: dict, support: dict) -> dict:
    """编造: 没有依据的日期/数量, 命中具体禁止项, 或把未知内容说成确定.
    返回 {"flag": bool|None, "dates": [...], "quantities": [...], "forbidden": [...], "unknown": [...],
          "notes": [...], "suspect": str|None}."""
    out = {"flag": None, "dates": [], "quantities": [], "forbidden": [], "unknown": [],
           "notes": [], "suspect": None}
    req_anchors = _hard_anchors(" ".join(str(x) for x in ctx.rubric.get("required") or [])
                                + " " + " ".join(str(x) for x in ctx.rubric.get("acceptable") or []),
                                exclude=ctx.question)
    support_qty = support["quantities"] | req_anchors["quantities"]
    topic_units = {q[2] for q in req_anchors["quantities"]}
    for f in ctx.facts:
        topic_units.update(q[2] for q in _anchors(f.get("meaning", ""))["quantities"])
    for s in ctx.sources:
        topic_units.update(q[2] for q in _anchors(s["quote"])["quantities"])
    # 日期: 回答里有具体日期, 但历史/标准答案/当前时间里都没有 -> 编造日期
    now_date = ("date", None, ctx.now.month, ctx.now.day)
    known_dates = set(support["dates"]) | set(req_anchors["dates"]) | {now_date}
    for d in sorted(a["dates"], key=lambda x: (x[2], x[3] or 0)):
        if not any(_same_date(d, k) for k in known_dates):
            out["dates"].append(f"{d[2]}月{d[3] or '?'}日")
    # 数量: 同一单位下标准答案/历史里没有这个数字, 而这个单位又确实是考点 -> 编造数字
    for q in sorted(a["quantities"], key=lambda x: (x[2], x[1])):
        if q[2] in topic_units and not any(_same_qty(q, k) for k in support_qty):
            out["quantities"].append(f"{q[1]}{q[2]}")
    if a["hedge"] and (out["dates"] or out["quantities"]):
        # 带"可能/好像"的推测不算断言成事实, 只记待人工
        out["suspect"] = "带不确定语气的具体值 (可能/好像): " + ", ".join(out["dates"] + out["quantities"])
        out["notes"].append(out["suspect"])
        out["dates"], out["quantities"] = [], []
    # 具体禁止项 (泛化的"编造/矛盾"不管, 由上面的规则兜)
    for text in [str(x) for x in ctx.rubric.get("forbidden") or []]:
        if _is_generic_rule(text):
            continue
        claim = _claim_tokens(text)
        if not claim:
            continue
        hits = [t for t in claim if _flat(t) in a["flat"] and not _negated_around(a["flat"], _flat(t))]
        if len(hits) >= max(1, round(len(claim) * 0.5)):
            out["forbidden"].append(f"命中禁止项「{text}」: {', '.join(hits)}")
        elif hits:
            out["suspect"] = out["suspect"] or f"疑似命中禁止项「{text}」({', '.join(hits)}), 判不准"
    # 未知当事实: 标准明确说不知道的部分, 回答给了没有依据的具体值/细节
    for text in [str(x) for x in ctx.rubric.get("unknown") or []]:
        if _is_generic_rule(text):
            continue
        topic = [t for t in _soft_tokens(text) if t not in _question_tokens(ctx.question) and t not in _GENERIC]
        if not topic:
            continue
        hit = [t for t in topic if _flat(t) in a["flat"]]
        if not hit:
            continue
        if a["hedge"]:
            out["notes"].append(f"未知项「{text}」被提起但带了不确定语气")
            continue
        # 只是提了一嘴常识不算; 有编造日期/数字, 或未知项本身带具体类型且回答给了具体信息, 才算
        if out["dates"] or out["quantities"] or _specific_claims(text, a):
            out["unknown"].append(f"把未知内容说成确定: {text}")
    if out["dates"] or out["quantities"] or out["forbidden"] or out["unknown"]:
        out["flag"] = True
    elif out["suspect"]:
        out["flag"] = False
    return out


def _same_date(x: tuple, y: tuple) -> bool:
    return x[2] == y[2] and (x[3] is None or y[3] is None or x[3] == y[3])


def _same_qty(x: tuple, y: tuple) -> bool:
    return x[1] == y[1] and x[2] == y[2]


def _is_generic_rule(text: str) -> bool:
    """禁止项/未知项是不是"编造/矛盾/未提供"这类泛化说法 (具体判断交给其他规则)."""
    tokens = [t for t in _soft_tokens(text) if t not in _GENERIC]
    return not tokens


def _claim_tokens(text: str) -> list[str]:
    """从一个"禁止的说法"里抽出要检查的内容词 (去掉 说/断言/推荐/把 这类引导词)."""
    out = []
    for word, flag in _pseg(text):
        w = word.strip()
        if len(w) < 2 or not flag or flag[0] not in "nvad" and not flag.startswith("nz"):
            continue
        if w in _GENERIC or w in ("说", "声称", "断言", "推荐", "添加", "要求", "公开", "主动", "追问", "沿用",
                                  "写成", "当成", "归给", "误述", "编造", "捏造", "虚构", "把", "将", "作为", "或者"):
            continue
        if w not in out:
            out.append(w)
    return out


def _specific_claims(text: str, a: dict) -> bool:
    """未知项本身带具体类型 (日期/姓名/金额/地址/电话), 而回答里出现了这种具体信息 -> 算具体化."""
    concrete = ("日期", "生日", "农历", "数字", "金额", "姓名", "名字", "地址", "电话", "号码", "原因", "结果", "内容", "细节")
    if not any(w in text for w in concrete):
        return False
    if any(w in text for w in ("日期", "生日", "农历")) and a["dates"]:
        return True
    if any(w in text for w in ("数字", "金额", "欠款", "余额")) and a["quantities"]:
        return True
    if any(w in text for w in ("姓名", "名字")) and any(len(n) >= 2 and _flat(n) in a["flat"]
                                                      for n, _f in _pseg(text) if _f == "nr"):
        return True
    return False


def judge_answer(ctx: JudgeContext, answer: str) -> dict:
    """规则判一题. 返回官方 grading 口径的字段 (另加 None=待人工 和五维状态)."""
    answer = (answer or "").strip()
    a = _answer_view(answer)
    a["raw"] = answer
    support = _anchors(ctx.support_text())
    items = [_judge_required(str(req), ctx, answer, a) for req in (ctx.rubric.get("required") or [])]
    flags = {"错认": False, "错否": False, "越界": False, "编造日期": False, "不当私事披露": False}
    suspects: list[str] = []
    notes: list[str] = []

    mis, extra = _detect_misattrib(ctx, a)
    if mis:
        flags["错认"] = True
        notes.append(f"错认: {mis}")
    suspects.extend(extra)

    denial, extra = _detect_false_denial(ctx, a, items)
    if denial:
        flags["错否"] = True
        notes.append(f"错否: {denial}")
    suspects.extend(extra)

    leak_flag, why, extra = _detect_leak(ctx, a, items)
    if leak_flag:
        flags[leak_flag] = True
        notes.append(f"{leak_flag}: {why}")
    suspects.extend(extra)

    fab = _detect_fabrication(ctx, a, support)
    if fab["flag"]:
        if fab["dates"]:
            flags["编造日期"] = True
            notes.append("编造日期: " + ", ".join(fab["dates"]))
        if fab["forbidden"]:
            notes.extend(f"违反禁止: {x}" for x in fab["forbidden"])
        if fab["unknown"]:
            notes.extend(f"未知当事实: {x}" for x in fab["unknown"])
    if fab["suspect"]:
        suspects.append(fab["suspect"])
    suspects.extend(fab["notes"])

    forbidden = [x.split(": ", 1)[1] for x in notes if x.startswith("违反禁止: ")]
    unknown = [x.split(": ", 1)[1] for x in notes if x.startswith("未知当事实: ")]
    if flags["编造日期"] or forbidden or unknown:
        notes_for_fab = True
    else:
        notes_for_fab = False

    unmet = [i for i in items if i["verdict"] == "unmet"]
    manual = [i for i in items if i["verdict"] is None]
    dims = {
        "忘了": "hit" if unmet else ("suspect" if manual else "none"),
        "认错人": "hit" if flags["错认"] else "none",
        "错误否认": "hit" if flags["错否"] else "none",
        "编造": "hit" if notes_for_fab else "none",
        "泄露": "hit" if flags["越界"] or flags["不当私事披露"] else "none",
    }
    if suspects:
        for dim in DIMENSIONS:
            if dims[dim] == "none" and _dim_of_suspect(dim, suspects):
                dims[dim] = "suspect"

    failed = bool(unmet or any(flags.values()) or forbidden or unknown)
    if not answer:
        verdict = "error"
    elif failed:
        verdict = "fail"
    elif manual or suspects or any(v == "suspect" for v in dims.values()):
        verdict = "manual"
    else:
        verdict = "pass"
    # 未来内容泄漏哨兵: 回答里出现了只在 cutoff 之后才说过的东西
    future_leak = _future_leak(ctx, a)
    if future_leak:
        notes.append(f"未来泄漏哨兵: {future_leak}")
        if verdict == "pass":
            verdict = "manual"
    if verdict == "pass" and not notes:
        reason = f"必答项 {len(items)}/{len(items)} 规则命中; 硬锚点/内容词有据, 未发现五维问题"
    elif verdict == "fail":
        bits = []
        if unmet:
            bits.append("必答项缺 " + "; ".join(i["text"] for i in unmet[:3]))
        if notes:
            bits.extend(notes[:5])
        reason = "不通过: " + " | ".join(bits)
    elif verdict == "manual":
        bits = [f"必答项待人工: {i['text']} ({i['rule']})" for i in manual[:3]]
        bits.extend(suspects[:4])
        reason = "待人工: " + " | ".join(bits or ["规则判不准"])
    elif verdict == "error":
        reason = "没有回答 (引擎/检索失败), 记录为未答, 不判语义失败"
    else:
        reason = "规则通过 (非官方 judge)"
    out = {
        "id": ctx.exam.id, "persona": ctx.exam.persona, "question": ctx.question,
        "now": ctx.now.isoformat(), "cutoff_turn": ctx.exam.cutoff_turn,
        "content_tags": ctx.exam.content_tags, "interval_days": ctx.exam.interval_days,
        "group_age_seconds": ctx.exam.group_age_seconds,
        "answer": answer,
        "verdict": verdict,
        "required": [{"text": i["text"], "verdict": i["verdict"], "rule": i["rule"],
                      "detail": i["detail"], "matched_facts": i["matched_facts"],
                      "hit": i["hit"], "missing": i["missing"]} for i in items],
        "flags": flags,
        "违反禁止": forbidden, "未知当事实": unknown, "无关旧事": [],
        "dimensions": dims,
        "dimensions_confidence": {k: ("high" if v == "hit" else "low" if v == "suspect" else None)
                                  for k, v in dims.items()},
        "future_leak": bool(future_leak),
        "suspects": suspects,
        "notes": notes,
        "reason": reason,
        "evidence_from": ctx.exam.evidence_from,
    }
    return out


def _dim_of_suspect(dim: str, suspects: list[str]) -> bool:
    if dim == "编造":
        return any("禁止" in s or "未知" in s or "编造" in s for s in suspects)
    if dim == "认错人":
        return any("认错" in s for s in suspects)
    if dim == "泄露":
        return any("泄露" in s or "敏感" in s for s in suspects)
    if dim == "忘了":
        return any("必答" in s or "内容词" in s for s in suspects)
    return False


def _future_leak(ctx: JudgeContext, a: dict) -> str | None:
    """泄漏哨兵: cutoff 之后的轮次里有、而 cutoff 之前没出现过的说法, 出现在回答里 (说明看到了未来)."""
    if not ctx.exam.future:
        return None
    before = _flat(ctx.exam.prefix_text())
    leaked = []
    for t in ctx.exam.future:
        for tok in _soft_tokens(t.user + " " + t.assistant):
            ft = _flat(tok)
            if len(ft) >= 2 and ft not in before and ft in a["flat"]:
                leaked.append(tok)
    leaked = list(dict.fromkeys(leaked))
    return "、".join(leaked[:5]) if len(leaked) >= 2 else None


def score_exam(exam: Exam, answer: str, *, foreign_names: frozenset[str] = frozenset()) -> dict:
    """给一题的答案判分 (供测试和主循环共用)."""
    ctx = JudgeContext(exam=exam, question=exam.question, rubric=exam.rubric,
                       facts=exam.facts, sources=exam.sources, now=exam.now,
                       foreign_names=foreign_names)
    return judge_answer(ctx, answer)


# --------------------------------------------------------------------------- 本机 llama.cpp 引擎
class LlamaEngine:
    """OpenAI 兼容的 /v1/chat/completions. 只连本机/局域网地址, 不碰付费 API."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout_s: float = 120.0,
                 max_tokens: int = 256, temperature: float = 0.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.model = ""
        self.tokens = 0
        self.calls = 0
        self.errors = 0

    def probe(self) -> tuple[bool, str]:
        """问 /v1/models: 通不通, 顺便记下模型名. 不通就退化 mock."""
        url = self.endpoint + "/v1/models"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        models = body.get("data") if isinstance(body, dict) else None
        if isinstance(models, list) and models:
            first = models[0]
            self.model = str(first.get("id") or first.get("model") or "")
        return True, f"ok, model={self.model or '(未报模型名)'}"

    def chat(self, system: str, user: str) -> str:
        """一次对话补全; 失败重试一次 (只重试连接类错误), 仍失败抛 RuntimeError."""
        payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                   "max_tokens": self.max_tokens, "temperature": self.temperature, "stream": False,
                   "chat_template_kwargs": {"enable_thinking": False}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: Exception | None = None
        for attempt in range(2):
            req = urllib.request.Request(self.endpoint + "/v1/chat/completions", data=data,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                self.calls += 1
                usage = body.get("usage") or {}
                self.tokens += int(usage.get("total_tokens") or 0)
                choices = body.get("choices") or []
                content = ""
                if choices:
                    msg = choices[0].get("message") or {}
                    content = str(msg.get("content") or "").strip()
                if not content:
                    raise RuntimeError("模型返回空 content (可能全花在思考上)")
                return content
            except (OSError, ValueError, urllib.error.URLError, RuntimeError) as exc:
                last = exc
                time.sleep(0.5 * (attempt + 1))
        self.errors += 1
        raise RuntimeError(f"调用本机模型失败: {type(last).__name__}: {last}")


class MockEngine:
    """规则 mock: 不连模型, 用记忆里命中的文本拼答案. 结果不可用, 只用来验收流程."""

    kind = "mock"

    def chat(self, system: str, user: str) -> str:  # pragma: no cover - 主循环用 mock_reply 而不是这里
        return "（mock）我不确定。"


def mock_reply(recall: dict) -> str:
    """mock 引擎的答案: 有服务端直接回答就用它, 否则把命中的记忆文本拼起来."""
    answer = (recall or {}).get("answer")
    if answer:
        return str(answer)
    hits = (recall or {}).get("hits") or []
    if not hits:
        return "（mock 引擎，检索没有命中记忆）我这会儿想不起来了。"
    got = [str(h.get("text") or "").strip() for h in hits[:3] if h.get("text")]
    return "（mock 引擎，按检索结果拼的，不是模型回答）我记得：" + "；".join(got)


ANSWER_SYSTEM = (
    "你是一个有长期记忆的聊天助手。[记忆] 是从长期记忆里检索到的内容。"
    "请只根据 [记忆] 和当前对话回答用户；记忆里没有记着、或者拿不准的，就说不知道或记不清，不要编造。"
    "回答简短自然，不要提“记忆”“检索”这类字眼。")


def build_answer_messages(question: str, recall: dict, recent: list[Turn] | None = None) -> list[dict]:
    """拼答题 prompt: system + (可选最近几轮) + 本轮问题 ([记忆] 投影接在问题后面, 和生产一致)."""
    msgs = [{"role": "system", "content": ANSWER_SYSTEM}]
    for t in recent or []:
        msgs.append({"role": "user", "content": t.user})
        msgs.append({"role": "assistant", "content": t.assistant})
    line = (recall or {}).get("line") or "[记忆] （没有检索到相关记忆）"
    msgs.append({"role": "user", "content": question + "\n" + line})
    return msgs


# --------------------------------------------------------------------------- 记忆回放
def _memory_cfg(vault: Path, *, raw_logs: bool, top_k: int = 3) -> dict:
    """评测用的 Memory 配置: 临时库; 关掉会影响可比性的旁路 (强度/情绪/图谱笔记/种子), 日志可选参与检索."""
    return {
        "vault": str(vault), "character": MEMORY_CHARACTER, "top_k": top_k, "min_score": 0.5,
        "rel_ratio": 0.35, "include_logs": bool(raw_logs), "graph_notes": False, "seed": "off",
        "strength": {"mode": "off"}, "affect": {"mode": "off"},
        "freshness": {"half_life_days": 30, "bm25_floor": 0.5, "refresh_on_recall": True, "promote_days": 3},
    }


def _const_time(t: datetime):
    return lambda: t


def _copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)


def _clone_tree(src: Path, dst: Path) -> None:
    """快照 -> 单题视图. 优先硬链接 (快); recall 只会整份替换文件 (.recall.json), 不会就地改日志, 所以安全;
    文件系统不支持硬链接就退回真拷贝."""
    try:
        shutil.copytree(src, dst, copy_function=os.link)
    except OSError:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)


class MemoryReplayer:
    """按 persona 增量喂历史; 每个 cutoff 做一份快照, 每题用独立视图, 互不污染."""

    def __init__(self, persona: str, turns: list[Turn], work_root: Path, *, raw_logs: bool, top_k: int = 3):
        from wm.memory import Memory  # 延迟 import: 只跑判分器的测试不需要 wm.memory
        self._Memory = Memory
        self.persona = persona
        self.turns = turns
        self.work = work_root / persona
        self.work.mkdir(parents=True, exist_ok=True)
        self.live = work_root / f"{persona}-live"
        self.cfg = _memory_cfg(self.live, raw_logs=raw_logs, top_k=top_k)
        self.mem = Memory(dict(self.cfg))
        self.mem.clock = _const_time(turns[0].time) if turns else _const_time(datetime.now())
        self.mem.init()
        self.fed = 0
        self.snapshots: dict[int, Path] = {}
        self.view_seq = 0
        self.observe_s = 0.0

    def _advance(self, cutoff: int) -> None:
        if cutoff > len(self.turns):
            raise DataError(f"{self.persona}: cutoff {cutoff} 超过历史轮数 {len(self.turns)}")
        while self.fed < cutoff:
            t = self.turns[self.fed]
            self.fed += 1
            self.mem.clock = _const_time(t.time)
            t0 = time.perf_counter()
            self.mem.observe(t.user, t.assistant)
            self.observe_s += time.perf_counter() - t0

    def snapshot(self, cutoff: int) -> Path:
        self._advance(cutoff)
        path = self.work / f"snap-{cutoff}"
        if not path.exists():
            _copytree(self.live, path)
        return path

    def open(self, cutoff: int, now: datetime, exam_id: str) -> "object":
        src = self.snapshot(cutoff)
        self.view_seq += 1
        safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", exam_id)[:40]
        dst = self.work / f"view-{self.view_seq:04d}-{safe}"
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        _clone_tree(src, dst)
        mem = self._Memory({**self.cfg, "vault": str(dst)})
        mem.clock = _const_time(now)
        return mem

    @staticmethod
    def drop_view(mem) -> None:
        try:
            root = Path(mem.root)
            shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass


def answer_one(exam: Exam, replayer: MemoryReplayer, engine, *, engine_kind: str,
               recent_turns: int = 0) -> tuple[str, str, dict, dict]:
    """一题: 回放截止前历史 -> recall -> 原生直接回答 / 本机模型. 返回 (答案, 来源, recall 摘要, 计时)."""
    timing = {"replay_s": 0.0, "recall_s": 0.0, "answer_s": 0.0}
    t0 = time.perf_counter()
    replay_before = replayer.observe_s
    mem = replayer.open(exam.cutoff_turn, exam.now, exam.id)
    try:
        t1 = time.perf_counter()
        recall = mem.recall(exam.question)
        timing["recall_s"] = time.perf_counter() - t1
        line = recall.get("line")
        direct = recall.get("answer")
        if direct:
            text, source = str(direct), "memory_answer"
        else:
            recent = exam.turns[-recent_turns:] if recent_turns > 0 else []
            msgs = build_answer_messages(exam.question, recall, recent)
            source = engine_kind
            t2 = time.perf_counter()
            if engine_kind == "mock":
                text = mock_reply(recall)
            else:
                text = engine.chat(msgs[0]["content"], msgs[-1]["content"])
            timing["answer_s"] = time.perf_counter() - t2
        summary = {"mode": recall.get("mode"), "line": line, "answer": direct,
                   "hits": [{"file": h.get("file"), "score": h.get("score"), "text": h.get("text")}
                            for h in (recall.get("hits") or [])],
                   "ms": recall.get("ms"), "faded": recall.get("faded")}
    finally:
        replayer.drop_view(mem)
        timing["replay_s"] = replayer.observe_s - replay_before
    timing["total_s"] = time.perf_counter() - t0
    return text, source, summary, timing


# --------------------------------------------------------------------------- 统计与报告
def _is_action_question(question: str) -> bool:
    """这题本身是不是一句做得到的动作指令 (S0 尾巴, 2026-11-18).

    Memory.recall 开头会用 `intent.doable(query)` 判 "动作指令用不着记忆", 判是就跳过检索
    (wm/memory.py 里 mode="skip"), 这些题实际考的不是记忆, 要在报告里单列出来.
    照 Memory.recall 同样的调用 (can/room 都用默认) 复算一遍; 判定失败就当不是, 不影响判分.
    """
    q = (question or "").strip()
    if not q:
        return False
    try:
        from wm.intent import doable
        return bool(doable(q))
    except Exception:  # noqa: BLE001 - 评测不该因为可选的意图模块导入/判定失败而丢统计字段
        return False


def _new_stats() -> dict:
    return {"total": 0, "pass": 0, "fail": 0, "manual": 0, "error": 0,
            "dims": {d: Counter() for d in DIMENSIONS}}


def _add_stats(stats: dict, row: dict) -> None:
    stats["total"] += 1
    stats[row["verdict"]] = stats.get(row["verdict"], 0) + 1
    for d, status in row["dimensions"].items():
        stats["dims"][d][status] += 1


def _stats_public(stats: dict) -> dict:
    total = stats["total"]
    judgeable = stats["pass"] + stats["fail"]
    return {
        "total": total, "pass": stats["pass"], "fail": stats["fail"],
        "manual": stats["manual"], "error": stats["error"],
        "pass_rate_all": round(stats["pass"] / total, 4) if total else 0.0,
        "pass_rate_judgeable": round(stats["pass"] / judgeable, 4) if judgeable else 0.0,
        "dimensions": {d: {"hit": stats["dims"][d]["hit"], "suspect": stats["dims"][d]["suspect"],
                           "none": stats["dims"][d]["none"]} for d in DIMENSIONS},
    }


def _age_bucket(seconds: float) -> str:
    if seconds <= 3600:
        return "1 小时内"
    if seconds <= 86400:
        return "当天"
    if seconds <= 3 * 86400:
        return "1–3 天"
    if seconds <= 15 * 86400:
        return "4–15 天"
    if seconds <= 45 * 86400:
        return "16–45 天"
    return "45 天以上"


def build_report(exam_set: ExamSet, rows: list[dict], *, config: dict, timing: dict,
                 detail_limit: int = 20) -> dict:
    """把逐题结果汇总成报告 (含按考点/间隔/人物分组和五维计数)."""
    by_persona: dict[str, dict] = {}
    by_tag: dict[str, dict] = {}
    by_interval: dict[str, dict] = {}
    by_age: dict[str, dict] = {}
    overall = _new_stats()
    required_total = Counter()
    engine_errors = [r for r in rows if r["verdict"] == "error"]
    for row in rows:
        _add_stats(overall, row)
        for store, key in ((by_persona, row["persona"]), (by_interval, str(row["interval_days"])),
                           (by_age, _age_bucket(row["group_age_seconds"]))):
            store.setdefault(key, _new_stats())
            _add_stats(store[key], row)
        for tag in row["content_tags"]:
            by_tag.setdefault(tag, _new_stats())
            _add_stats(by_tag[tag], row)
        for item in row["required"]:
            required_total[item["verdict"]] += 1
    action_ids = [r["id"] for r in rows if r.get("routed_as_action")]
    return {
        "tool": "tools/bench_ornn.py",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": {
            "root": str(exam_set.root), "version": exam_set.version,
            "manifest_verified": exam_set.manifest_verified,
            "manifest_files": len(exam_set.manifest.get("file_sha256") or {}),
            "questions_in_dataset": len(exam_set.exams),
            "selected": len(rows),
            "personas": sorted({e.persona for e in exam_set.exams}),
            "evidence_file": str(exam_set.evidence_file) if exam_set.evidence_file else None,
            "evidence_from": sorted({e.evidence_from for e in exam_set.exams}),
            "warnings": exam_set.warnings,
        },
        "config": config,
        "timing": timing,
        "counts": _stats_public(overall),
        "required_items": {"total": sum(required_total.values()),
                           "met": required_total["met"], "unmet": required_total["unmet"],
                           "manual": required_total[None]},
        "stats": {  # 被当成动作指令、recall 被跳过的题 (题号只留前 20 条, 全量看 scores.jsonl)
            "routed_as_action": len(action_ids),
            "routed_as_action_ids": action_ids[:20],
            "routed_as_action_ids_truncated": len(action_ids) > 20,
        },
        "groups": {
            "by_persona": {k: _stats_public(v) for k, v in sorted(by_persona.items())},
            "by_content_tag": {k: _stats_public(v) for k, v in sorted(by_tag.items())},
            "by_interval_days": {k: _stats_public(v) for k, v in sorted(by_interval.items(), key=lambda x: int(x[0]))},
            "by_age_bucket": {k: _stats_public(v) for k, v in by_age.items()},
        },
        "scoring": {
            "method": "规则判分 (非官方 LLM judge); 必答项/五维口径照官方 grading/README.md",
            "dimensions": {d: DIM_HELP[d] for d in DIMENSIONS},
            "manual_policy": "必答项或五维判不准 -> manual (待人工), 不计入 pass",
            "not_scored": ["无关旧事 (官方 relevance 维度, 规则不评分)"],
            "engine_errors": len(engine_errors),
            "engine_errors_questions": [r["id"] for r in engine_errors[:10]],
        },
        "detail": rows[:max(0, detail_limit)],
    }


def render_markdown(report: dict) -> str:
    """人读摘要: 总览 / 五维 / 分组表 / 前几题判据."""
    c, cfg = report["counts"], report["config"]
    lines = []
    lines.append(f"# Ornn 714 题记忆评测报告 ({report['dataset']['version']})")
    lines.append("")
    if not cfg.get("results_valid", True):
        lines.append(f"> ⚠️ **结果不可用**: {cfg.get('invalid_reason', '引擎不可用')}")
        lines.append("")
    lines.append(f"- 数据: `{report['dataset']['root']}`")
    lines.append(f"- manifest 校验: {'通过' if report['dataset']['manifest_verified'] else '失败'}"
                 f" ({report['dataset']['manifest_files']} 个文件); 题数 {report['dataset']['selected']}"
                 f"/{report['dataset']['questions_in_dataset']}")
    lines.append(f"- 判分证据: {report['dataset']['evidence_from']}")
    lines.append(f"- 引擎: {cfg.get('engine')} (可用={cfg.get('engine_ok')}); 记忆: 原始日志参与检索="
                 f"{cfg.get('raw_logs')}, top_k={cfg.get('top_k')}")
    lines.append(f"- 耗时: {report['timing'].get('total_s', 0):.1f}s")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 总题数 | {c['total']} |")
    lines.append(f"| 通过 (规则) | {c['pass']} ({c['pass_rate_all']:.1%} 全部 / {c['pass_rate_judgeable']:.1%} 已判) |")
    lines.append(f"| 不通过 | {c['fail']} |")
    lines.append(f"| 待人工 | {c['manual']} |")
    lines.append(f"| 未答/出错 | {c['error']} |")
    req = report["required_items"]
    lines.append(f"| 必答项 | 命中 {req['met']} / 未中 {req['unmet']} / 待人工 {req['manual']} (共 {req['total']}) |")
    lines.append("")
    st = report.get("stats") or {}
    if st.get("routed_as_action"):
        more = " (只列前 20 条)" if st.get("routed_as_action_ids_truncated") else ""
        lines.append(f"> 被 `intent.doable` 当成动作指令、跳过 recall 的题: **{st['routed_as_action']}** 道{more}: "
                     + "、".join(st.get("routed_as_action_ids") or []))
        lines.append("")
    lines.append("## 五个维度 (规则计数; 单选题可同时命中多项)")
    lines.append("")
    lines.append("| 维度 | 命中 | 待人工 | 未发现 | 含义 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for d in DIMENSIONS:
        x = c["dimensions"][d]
        lines.append(f"| {d} | {x['hit']} | {x['suspect']} | {x['none']} | {DIM_HELP[d]} |")
    lines.append("")
    lines.append("> 规则判分不是官方 judge: `未发现` 只表示规则没抓到, 不等于官方判分通过; "
                 "`待人工` 的题不计入通过.")
    lines.append("")
    lines.append("## 按考点分组 (标签重叠, 题数不能相加)")
    lines.append("")
    lines.append("| 考点 | 题数 | 通过 | 不通过 | 待人工 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for tag, s in report["groups"]["by_content_tag"].items():
        lines.append(f"| {tag} | {s['total']} | {s['pass']} | {s['fail']} | {s['manual']} |")
    lines.append("")
    lines.append("## 按最后一次提及后多少天分组")
    lines.append("")
    lines.append("| 间隔 | 题数 | 通过 | 不通过 | 待人工 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for k, s in report["groups"]["by_interval_days"].items():
        label = INTERVAL_LABEL.get(int(k), k)
        lines.append(f"| {label} ({k} 天) | {s['total']} | {s['pass']} | {s['fail']} | {s['manual']} |")
    lines.append("")
    lines.append("## 按人物分组")
    lines.append("")
    lines.append("| 人物 | 题数 | 通过 | 不通过 | 待人工 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for p, s in report["groups"]["by_persona"].items():
        lines.append(f"| {p} | {s['total']} | {s['pass']} | {s['fail']} | {s['manual']} |")
    lines.append("")
    lines.append("## 前几题的判分依据")
    lines.append("")
    for row in report["detail"]:
        lines.append(f"### {row['id']} — {row['verdict']}")
        lines.append("")
        lines.append(f"- 问: {row['question']!r}")
        lines.append(f"- 答: {row['answer'][:300]!r}")
        for item in row["required"]:
            lines.append(f"- 必答[{item['verdict'] or '待人工'}]: {item['text']} — {item['rule']}")
        dims = ", ".join(f"{d}={s}" for d, s in row["dimensions"].items() if s != "none")
        lines.append(f"- 五维: {dims or '未发现'}")
        if row["notes"]:
            lines.append(f"- 备注: {'; '.join(row['notes'][:4])}")
        lines.append(f"- 判据: {row['reason']}")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- 主循环
def run_benchmark(*, data_root: Path = DEFAULT_DATA, out_dir: Path | None = None,
                  limit: int | None = None, persona: str | None = None,
                  engine_kind: str = "llamacpp", endpoint: str = DEFAULT_ENDPOINT,
                  raw_logs: bool = True, top_k: int = 3, recent_turns: int = 0,
                  detail_limit: int = 20, timeout_s: float = 120.0,
                  keep_work: bool = False, foreign_names: frozenset[str] = frozenset(),
                  work_root: Path | None = None) -> dict:
    """跑一轮评测并写报告; 返回报告 dict. 单题异常只记在该题上."""
    t_start = time.perf_counter()
    exam_set = load_dataset(data_root)
    exams = exam_set.exams
    if persona:
        exams = [e for e in exams if e.persona == persona]
        if not exams:
            raise DataError(f"没有 persona={persona} 的题")
    elif limit is not None and limit > 0:
        exams = exams[:limit]
    if limit is not None and limit > 0 and persona:
        exams = exams[:limit]
    if not exams:
        raise DataError("没有可跑的题")
    out_dir = Path(out_dir) if out_dir else ROOT / "backups" / f"ornn-{date.today():%Y-%m-%d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    engine_ok, engine_detail = False, "mock 引擎 (显式指定)"
    engine = None
    engine_used = "mock"
    invalid_reason = "使用 --engine mock: 答案是规则拼的, 分数不可用"
    if engine_kind == "llamacpp":
        engine = LlamaEngine(endpoint=endpoint, timeout_s=timeout_s)
        engine_ok, engine_detail = engine.probe()
        if engine_ok:
            engine_used = "llamacpp"
            invalid_reason = ""
        else:
            invalid_reason = f"本机 llama.cpp ({endpoint}) 不通 ({engine_detail}), 已退化 mock; 结果不可用"
    work_parent = Path(work_root) if work_root else Path(tempfile.mkdtemp(prefix="ornn-bench-"))
    if not work_parent.exists():
        work_parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    timings = Counter()
    replayers: dict[str, MemoryReplayer] = {}
    try:
        by_persona: dict[str, list[Exam]] = defaultdict(list)
        for exam in exams:
            by_persona[exam.persona].append(exam)
        total = len(exams)
        done = 0
        for p, pexams in by_persona.items():
            replayer = MemoryReplayer(p, _persona_turns(exam_set, p), work_parent,
                                      raw_logs=raw_logs, top_k=top_k)
            replayers[p] = replayer
            for exam in sorted(pexams, key=lambda e: (e.cutoff_turn, e.id)):
                done += 1
                t0 = time.perf_counter()
                routed = _is_action_question(exam.question)  # 这题的 recall 会不会被当成动作指令跳过
                try:
                    answer, source, recall, timing = answer_one(
                        exam, replayer, engine, engine_kind=engine_used, recent_turns=recent_turns)
                    row = score_exam(exam, answer, foreign_names=foreign_names)
                    row["answer_source"] = source
                    row["recall"] = {"mode": recall.get("mode"), "ms": recall.get("ms"),
                                     "line": recall.get("line"),
                                     "hits": recall.get("hits")[:3]}
                    row["routed_as_action"] = bool(routed or recall.get("mode") == "skip")
                    if source == engine_used and engine_used == "llamacpp":
                        row["llm"] = {"model": getattr(engine, "model", ""),
                                      "calls": getattr(engine, "calls", 0),
                                      "tokens": getattr(engine, "tokens", 0)}
                    timings["replay_s"] += timing.get("replay_s", 0.0)
                    timings["recall_s"] += timing.get("recall_s", 0.0)
                    timings["answer_s"] += timing.get("answer_s", 0.0)
                except Exception as exc:  # noqa: BLE001 (单题失败不中断整轮)
                    traceback.print_exc()
                    row = {
                        "id": exam.id, "persona": exam.persona, "question": exam.question,
                        "now": exam.now.isoformat(), "cutoff_turn": exam.cutoff_turn,
                        "content_tags": exam.content_tags, "interval_days": exam.interval_days,
                        "group_age_seconds": exam.group_age_seconds, "answer": "",
                        "answer_source": "error", "verdict": "error",
                        "required": [], "flags": {k: False for k in
                                                  ("错认", "错否", "越界", "编造日期", "不当私事披露")},
                        "违反禁止": [], "未知当事实": [], "无关旧事": [],
                        "dimensions": {d: "none" for d in DIMENSIONS},
                        "dimensions_confidence": {d: None for d in DIMENSIONS},
                        "future_leak": False, "suspects": [], "notes": [],
                        "routed_as_action": routed,
                        "error": f"{type(exc).__name__}: {exc}",
                        "reason": f"单题失败: {type(exc).__name__}: {exc}",
                    }
                timings["question_wall_s"] += time.perf_counter() - t0
                rows.append(row)
                progress = f"[{done}/{total}] {exam.id} {row['verdict']}"
                print(progress, flush=True)
    finally:
        if not keep_work:
            shutil.rmtree(work_parent, ignore_errors=True)
        else:
            print(f"临时记忆库保留在: {work_parent}")
    rows.sort(key=lambda r: (r["persona"], r["now"], r["id"]))
    engine_errors = sum(1 for r in rows if r["verdict"] == "error")
    results_valid = bool(engine_used == "llamacpp" and engine_ok and engine_errors == 0)
    if engine_used == "llamacpp" and engine_errors:
        invalid_reason = f"{engine_errors} 题没答出来 (模型/检索异常), 结果不完整"
    timing_report = {
        "total_s": round(time.perf_counter() - t_start, 1),
        "question_wall_s": round(timings["question_wall_s"], 1),
        "replay_s": round(timings["replay_s"], 1),
        "recall_s": round(timings["recall_s"], 1),
        "answer_s": round(timings["answer_s"], 1),
        "questions": len(rows),
        "avg_question_s": round(timings["question_wall_s"] / len(rows), 2) if rows else 0.0,
    }
    config = {
        "engine": engine_used, "engine_requested": engine_kind, "engine_ok": bool(engine_ok),
        "engine_detail": engine_detail, "endpoint": endpoint if engine_kind == "llamacpp" else None,
        "results_valid": results_valid, "invalid_reason": invalid_reason,
        "limit": limit, "persona": persona, "raw_logs": raw_logs, "top_k": top_k,
        "recent_turns": recent_turns, "detail_limit": detail_limit,
        "dataset_path": str(exam_set.root), "memory_character": MEMORY_CHARACTER,
        "llm_calls": getattr(engine, "calls", 0), "llm_tokens": getattr(engine, "tokens", 0),
        "llm_errors": getattr(engine, "errors", 0),
        "score_method": "rules (非官方 judge)",
    }
    report = build_report(exam_set, rows, config=config, timing=timing_report, detail_limit=detail_limit)
    # S0 尾巴 (2026-11-18): outputs 收每题模型的原始输出 (题号 -> 输出), 写进 report.json;
    # 文件路径原来占着 outputs, 挪到 artifacts (report.json 在盘上从来没见过 outputs 字段, 不算改老字段).
    report["outputs"] = {row["id"]: row.get("answer", "") for row in rows}
    report["artifacts"] = {"report": str(out_dir / "report.json"), "markdown": str(out_dir / "report.md"),
                           "answers": str(out_dir / "answers.jsonl"), "scores": str(out_dir / "scores.jsonl")}
    _write_outputs(out_dir, report, rows)
    return report


def _persona_turns(exam_set: ExamSet, persona: str) -> list[Turn]:
    """从数据里取完整历史 (含 cutoff 之后的部分, 只用于回放截止前的轮次; future 也来自这里)."""
    rows = _history_rows(exam_set.root / "histories" / f"{persona}.jsonl")
    return [Turn(int(r["轮次"]), datetime.fromisoformat(str(r["逻辑时间"])),
                 str(r["用户消息"]), str(r["助手回复"])) for r in rows]


def _write_outputs(out_dir: Path, report: dict, rows: list[dict]) -> None:
    def dump(path: Path, obj) -> None:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    dump(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    with (out_dir / "answers.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps({"id": row["id"], "answer": row["answer"]}, ensure_ascii=False) + "\n")
    with (out_dir / "scores.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _print_summary(report: dict) -> None:
    c = report["counts"]
    cfg = report["config"]
    print("=" * 78)
    print(f"数据: {report['dataset']['version']}  题数: {c['total']}  "
          f"manifest: {'通过' if report['dataset']['manifest_verified'] else '失败'}")
    print(f"引擎: {cfg['engine']} (可用={cfg['engine_ok']})  结果可用: {cfg['results_valid']}"
          + ("" if cfg["results_valid"] else f"  <- {cfg['invalid_reason']}"))
    print(f"通过 {c['pass']} / 不通过 {c['fail']} / 待人工 {c['manual']} / 未答 {c['error']}  "
          f"(规则通过率 {c['pass_rate_judgeable']:.1%} 已判 / {c['pass_rate_all']:.1%} 全部)")
    dims = "  ".join(f"{d}:命中{x['hit']}/待人工{x['suspect']}" for d, x in c["dimensions"].items())
    print(f"五维: {dims}")
    print(f"耗时: {report['timing']['total_s']}s (平均 {report['timing']['avg_question_s']}s/题)")
    print(f"输出: {report.get('artifacts', {}).get('report', '')}")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ornn 714 题公开记忆评测 (离线, 规则判分)")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="自然时间网格数据集目录")
    ap.add_argument("--out-dir", type=Path, help="报告目录 (默认 backups/ornn-<日期>)")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题 (默认全部)")
    ap.add_argument("--persona", choices=["P01", "P12", "P14", "P16", "P17", "P20"],
                    help="只跑一个人物")
    ap.add_argument("--engine", choices=["llamacpp", "mock"], default="llamacpp",
                    help="答题引擎: 本机 llama.cpp (默认, 不通自动退化 mock) / 规则 mock")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="llama.cpp OpenAI 兼容地址")
    ap.add_argument("--timeout", type=float, default=120.0, help="单次模型调用超时 (秒)")
    ap.add_argument("--top-k", type=int, default=3, help="Memory.recall 的 top_k")
    ap.add_argument("--no-raw-logs", action="store_true", help="原始日志不参与检索 (默认参与)")
    ap.add_argument("--recent-turns", type=int, default=0, help="prompt 里带最近几轮对话 (默认 0)")
    ap.add_argument("--detail", type=int, default=20, help="report.json 里每题的判分依据保留前几条")
    ap.add_argument("--keep-work", action="store_true", help="保留临时记忆库 (调试用)")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        report = run_benchmark(data_root=a.data, out_dir=a.out_dir, limit=a.limit, persona=a.persona,
                               engine_kind=a.engine, endpoint=a.endpoint, raw_logs=not a.no_raw_logs,
                               top_k=a.top_k, recent_turns=a.recent_turns, detail_limit=a.detail,
                               timeout_s=a.timeout, keep_work=a.keep_work)
    except DataError as exc:
        print(f"数据校验不通过: {exc}", file=sys.stderr)
        return 2
    _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
