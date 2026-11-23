"""记忆的「取代」：改口、改期之后不再说旧的 (2026-11-17 S2, 11-07 外部调研 P0 第三件).

**为什么要有这个**: 现在改口、改期之后她还会说旧的 —— 台账上这类题是 **0/3**, 而 11-07 那页写的是
"最伤通人性". 小黑盒 3.1 (那篇陪伴型 agent 长期记忆) 给的解法拆开是四条, 这里是它们:

1. **结局栏**: 每条记忆能带"还成不成立 / 被谁关的 / 为什么", 而不是删掉 —— 删掉就没有"她以前以为是 22 号"这段历史了.
2. **独立的关闭通道**: "我感冒好了"不产生新记忆, 也要能关掉旧的"感冒了".
3. **三类不算失效**: 只是提到 / 只是相关 / 计划没做到 —— 这三种情况下旧记忆**照样有效**.
4. **摘原话由程序回原文核对**: 模型说"他改了", 程序回这一轮原文里找那句话; 找不到就不认.

**设计上的两条硬规矩**:
- **宁可漏判**: 判错的代价是"把有效的一条藏起来", 比漏判严重得多. 所以下面所有拿不准的分支都返回"不取代".
- **不改正文**: 这里只写一个旁路 sidecar (`supersede.json`), `wm/memory.py` 一个字不动; 读的时候过滤.
  想撤就删 sidecar, 立刻回到原样 —— 这也是它敢在真机上跑的原因.

接进读取路径的方式 (下一步, 要重启): `Memory.recall` 之后过一遍 `SupersedeStore.filter()`;
本模块现在只有纯规则和旁路读写, **不导入 wm.memory**, 所以单独测、单独跑都不会影响线上.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 换说法: 出现这些词才**可能**是一条"取代". 太宽的词不放进来 (比如单一个"改"字,
# "改天再说"就会被误判成改期).
REPLACE_MARKERS = (
    "改成", "改到", "改期", "改为", "换成了", "换成", "不再是", "不是了",
    "现在是", "变成", "调到", "推迟", "延后", "提前", "取消了", "取消",
    "不去了", "不做了", "好了", "痊愈", "不然了",
)

# 三类不算失效: 命中就**一定不是**取代 (先判这个, 再判换说法).
NOT_INVALIDATION = (
    ("只是提到", ("提到", "说起", "聊到", "谈到", "听他说", "据说")),
    ("只是相关", ("顺便", "顺带", "相关的", "跟这个有关", "同类的")),
    ("计划没做到", ("打算", "准备", "本来要", "想着", "计划", "还没有", "没去成")),
)

# 判"同一件事"时不算数的词: 光靠它们重合不算同一个人/同一件事.
STOP = set("的了是在和就跟也很都我你他她它这那不有个一了要说去来到上下里外后前时天日月年")

# 通用词: 几乎每条记忆里都有, 重合了也不证明"是同一件事".
# 没有这一条的时候, 「用户的房租改成3000」会把「用户的体检10月22日」关掉 —— 因为它们共享「用户」.
GENERIC = {
    "用户", "主人", "自己", "我们", "你们", "他们", "大家",
    "今天", "昨天", "明天", "后天", "最近", "现在", "后来", "以前",
    "然后", "所以", "但是", "因为", "如果", "还是", "已经", "可以",
    "一个", "这个", "那个", "什么", "一下", "有点", "真的", "时候",
}

_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_TAG = re.compile(r"(#\S+)")
_LINE = re.compile(r"^\s*[-*]\s+(?P<body>.+?)\s*$")


# ---------------------------------------------------------------- 解析一条记忆

def parse_line(line: str) -> dict:
    """把 `- 用户住在苏州 (2026-10-27) #事实 #auto-memory` 拆成 body / date / tags.

    拆不出来不算错 —— 返回 body 为原文、date/tags 为空, 调用方照常判.
    """
    raw = str(line or "").rstrip("\n")
    m = _LINE.match(raw)
    body = m.group("body") if m else raw.strip()
    tags = _TAG.findall(body)
    text = _TAG.sub("", body).strip()
    dm = re.search(r"\((\d{4}-\d{1,2}-\d{1,2})\)", text)
    date = dm.group(1) if dm else ""
    if dm:
        text = (text[:dm.start()] + text[dm.end():]).strip()
    return {"body": text, "date": date, "tags": tags, "raw": raw}


def key_of(line: str) -> str:
    """一条记忆的稳定标识: 用原文(去掉列表符号和首尾空白)算, 活多久算多久.

    不做规范化 —— 改写过的行就是另一条, 那正是我们要区分的. 12 位够用, 可读.
    """
    raw = str(line or "").strip().lstrip("-* ").strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def core_of(text: str) -> str:
    """正文的比对核: 只去掉末尾的括注 (来源/日期), 别的一律不动.

    摘要行写成 "- 用户10月22日要去体检 (来源: [[2026-10-20]] 20:00)",
    而检索回来的是没有括注的正文 —— 两边都得过这一道才对得上.
    这是接线时真踩到的坑: 写侧判对了、读侧却因为一个括号永远比不中.
    """
    s = str(text or "").strip()
    while True:
        stripped = re.sub(r"\s*\([^()]*\)\s*$", "", s).strip()
        if stripped == s:
            return s
        s = stripped


# ---------------------------------------------------------------- 判"同一件事"

def _words(text: str) -> set[str]:
    """粗分词: 中文按相邻两字、英文数字按整词. 只用来判重合, 不做语义."""
    out: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text or ""):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def shared_subject(a: str, b: str) -> list[str]:
    """两条记忆是不是在说同一个人/同一件事: 共享名字, 或共享两个"少见词".

    口径照小黑盒 3.1: "共享名字或两个少见词". 少见词 = 2 字以上、且不是"的了是在"这类.
    这里额外排掉两类**看着像重合、其实什么也不证明**的:
      - 通用词 (用户/今天/所以...): 几乎每条都有;
      - 带数字的碎片 (10/0月/月2...): 日期会被拆成这些, 两个不同日期照样重合.
    返回共享的那几个词 (空 = 不像同一件事).
    """
    wa, wb = _words(a), _words(b)
    out = []
    for w in sorted(wa & wb):
        if len(w) < 2 or set(w) <= STOP or w in GENERIC:
            continue
        if any(ch.isdigit() for ch in w):   # 10月22 / 10月24 会共享 "月2" 这种碎片
            continue
        out.append(w)
    return out


# 时间副词本身就是"变了"的信号: "用户爱吃川菜" -> "用户现在爱吃粤菜了" 没有改成/不再是,
# 但"现在"在这里就是在说"和以前不一样了". 单靠它不够 —— judge 还要求共享主体和原文提到旧的那条.
_EXTRA_MARKERS = ("现在", "如今", "目前", "最近改")


def replaced_marker(text: str) -> str:
    """这条新记忆里的换说法 (空 = 没有)."""
    t = str(text or "")
    for marker in REPLACE_MARKERS + _EXTRA_MARKERS:
        if marker in t:
            return marker
    return ""


def not_invalidation(text: str) -> str:
    """三类不算失效: 返回命中的那一类 (空 = 都不是)."""
    t = str(text or "")
    for label, words in NOT_INVALIDATION:
        if any(w in t for w in words):
            return label
    return ""


def mentioned_date(text: str) -> str:
    """文字里写的日期, 统一成 M-D 便于跟旧的那条比 ("10月24日" -> "10-24")."""
    m = _DATE.search(str(text or ""))
    return f"{int(m.group(1))}-{int(m.group(2))}" if m else ""


# ---------------------------------------------------------------- 判一条取代

@dataclass
class Decision:
    supersedes: bool
    reason: str = ""
    shared: list[str] = field(default_factory=list)
    marker: str = ""
    blocked_by: str = ""

    def __bool__(self) -> bool:
        return self.supersedes


def judge(new_line: str, old_line: str, *, turn_text: str = "") -> Decision:
    """新的这条是不是取代了旧的那条.

    顺序是有讲究的:
      1. 三类不算失效  -> 一定不取代 (先挡, 免得"打算改天去体检"把有效的体检日期关掉)
      2. 是不是同一件事 -> 不像就不取代 (新记忆和旧记忆共享主体才算)
      3. 日期类: 新日期不同才算 (同样写 10-24 的是重复, 不是取代)
      4. turn_text 给了就核对: 这一轮得真的提到过旧的这条 (第 4 道校验)
      5. 认下来的两条路: 有换说法的词 / 同一件事换了日期 / 同一处换了值
    """
    newer = parse_line(new_line)
    older = parse_line(old_line)
    if not newer["body"] or not older["body"]:
        return Decision(False, "空行")

    blocked = not_invalidation(newer["body"])
    if blocked:
        return Decision(False, f"{blocked}，不算失效", blocked_by=blocked)

    # 换说法常常被提炼吃掉了 ("我现在喜欢绿色了" -> "用户喜欢的颜色是绿色了"),
    # 所以标记要在两处找: 新记忆本身, 以及这一轮的原文.
    marker = replaced_marker(newer["body"]) or replaced_marker(turn_text)
    shared = shared_subject(newer["body"], older["body"])
    if not shared and not marker:
        return Decision(False, "不像同一件事")
    if not shared:
        return Decision(False, "不像同一件事", marker=marker)

    d_new, d_old = mentioned_date(newer["body"]), mentioned_date(older["body"])
    if d_new and d_old and d_new == d_old:
        return Decision(False, "日期一样，是重复不是取代", shared=shared, marker=marker)

    # 第 5 步: 这一轮得真的说到过旧的这条 (摘原话回原文核对).
    # 比的是**旧记忆的主体**在不在原文里 —— 要求新记忆整句出现在原文里太严:
    # 提炼本来就会改写 ("体检改到后天了" -> "用户的体检改到后天了"), 那不是编造.
    if turn_text and not _turn_touches(older["body"], newer["body"], turn_text):
        return Decision(False, "这一轮原文里没提到这条，不认", shared=shared, marker=marker)

    if marker:
        return Decision(True, f"「{marker}」+ 共享 {'/'.join(shared[:3])}", shared=shared, marker=marker)

    # 没有换说法的词, 还有两条硬证据可以认:
    #   a) 同一件事, 两个不同的日期      -> 改期 ("今天去体检" / "体检改到后天了")
    #   b) 同一处, 换了值                -> 改口 ("喜欢的颜色是蓝色" / "…是绿色了")
    if d_new and d_old:
        return Decision(True, f"同一件事换了日期 {d_old}->{d_new} + 共享 {'/'.join(shared[:3])}",
                        shared=shared, marker="")
    slot = slot_conflict(newer["body"], older["body"])
    if slot:
        return Decision(True, f"同一处换了值「{slot[0]}」->「{slot[1]}」+ 共享 {'/'.join(shared[:3])}",
                        shared=shared, marker="")

    return Decision(False, "没有换说法", shared=shared, marker=marker)


def _turn_touches(old_body: str, new_body: str, turn_text: str) -> bool:
    """这一轮真的说到过旧的这条吗. 三种都算: 引到原话 / 说到新说法 / 提到同一个主体."""
    if _quote_of(old_body, turn_text) or _quote_of(new_body, turn_text):
        return True
    return bool(shared_subject(old_body, turn_text))


# 别把"了""的"这种收尾当值
_SLOT_TAIL = "了啦呀呢吧啊"
_VALUE_MIN, _VALUE_MAX = 2, 6


def slot_conflict(new_body: str, old_body: str) -> tuple[str, str] | None:
    """同一个槽换了值: 两句的前半段一样、结尾那个值不一样.

    "用户喜欢的颜色是蓝色" vs "用户喜欢的颜色是绿色了" -> ("蓝色", "绿色")
    只认**前缀完全相同**的: 前缀不同就不是同一个槽, 宁可漏判.
    """
    a, b = str(old_body or ""), str(new_body or "")
    for adverb in _EXTRA_MARKERS:  # "用户爱吃川菜" vs "用户现在爱吃粤菜了"
        b = b.replace(adverb, "")
        a = a.replace(adverb, "")
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    if n < 3:                       # 前缀太短, 说不上是同一个槽 ("用户爱"就够了, 值至少 2 字)
        return None
    va = a[n:].strip().rstrip(_SLOT_TAIL).strip()
    vb = b[n:].strip().rstrip(_SLOT_TAIL).strip()
    if va == vb or not va or not vb:
        return None
    if not (_VALUE_MIN <= len(va) <= _VALUE_MAX and _VALUE_MIN <= len(vb) <= _VALUE_MAX):
        return None
    if va in b or vb in a:          # 一边包含另一边, 是补充不是取代
        return None
    return va, vb


def _quote_of(body: str, turn_text: str) -> str | None:
    """从这一轮原文里找出这句 (能做多少做多少: 先试整句, 再试 8 字窗口).

    这是"摘原话由程序回原文核对"的落地 —— 不要求模型报置信度, 要求它说的这句原文真的有.
    """
    text = re.sub(r"\s+", "", str(turn_text or ""))
    flat = re.sub(r"\s+", "", str(body or ""))
    if not flat:
        return None
    if flat in text:
        return flat
    for n in (10, 8):
        for i in range(0, max(1, len(flat) - n + 1)):
            frag = flat[i:i + n]
            if len(frag) >= n and frag in text:
                return frag
    return None


# ---------------------------------------------------------------- 旁路 sidecar

class SupersedeStore:
    """结局栏的落盘处. 只写这一个文件, 正文一个字不动.

    结构: {"v": 1, "items": {"<旧的 key>": {"ended_at", "by", "why", "quote", "label", "manual"}}}
    - by: 取代它的那条的 key; manual=True 表示走的是独立关闭通道 ("我感冒好了"这种不产生新记忆的关闭)
    """

    VERSION = 1

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._data: dict = {"v": self.VERSION, "items": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return                       # 读坏了当空的: 宁可不取代, 也不要拿半个文件去藏记忆
        if isinstance(raw, dict) and isinstance(raw.get("items"), dict):
            self._data = {"v": self.VERSION, "items": raw["items"]}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- 写
    def end(self, old_line: str, *, by: str = "", by_body: str = "", why: str = "", quote: str = "",
            label: str = "", manual: bool = False, now: datetime | None = None) -> bool:
        """给旧的那条写下结局栏 (只是标记, 不删)."""
        k = key_of(old_line)
        if not k:
            return False
        self._data["items"][k] = {
            "ended_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "by": by, "by_body": by_body, "why": why, "quote": quote, "label": label, "manual": bool(manual),
            # 检索回来的一条只有正文 (没有日期和标签), 也留一份正文好让读路径认得出它
            "body": core_of(parse_line(old_line)["body"]),
        }
        self._save()
        return True

    def reopen(self, old_line: str) -> bool:
        """删掉取代它的那条之后, 旧的要能回来 —— 这里就是"让它回来". """
        k = key_of(old_line)
        if k in self._data["items"]:
            del self._data["items"][k]
            self._save()
            return True
        return False

    # -- 读
    def is_ended(self, line: str) -> bool:
        return key_of(line) in self._data["items"]

    def is_ended_body(self, text: str) -> bool:
        """按正文判: 检索回来的 hit 只有正文, 没有日期和标签, 用 key_of 永远对不上整行.

        两边都过一遍 core_of: 摘要行带着 "(来源: [[2026-10-20]] 20:00)", 检索回来的不带,
        不归一化就永远比不中 —— 这一层是接线时真踩到的坑.
        老 sidecar 里没有 body 字段的, 这里一律判 False —— 认不出就不藏, 宁可漏判.
        """
        body = core_of(parse_line(text)["body"])
        if not body:
            return False
        return any(core_of(rec.get("body") or "") == body for rec in self._data["items"].values())

    def bodies(self) -> set[str]:
        """被关掉的那些旧记忆的正文, 一次取全 (检索每轮开头载一次, 不进热循环)."""
        return {b for b in (core_of(rec.get("body") or "") for rec in self._data["items"].values()) if b}

    def reopen_by_body(self, text: str) -> int:
        """删掉/划掉"新的那条"时, 把它取代的旧记忆放回来 (记忆页删一条, 旧的要能回来)."""
        body = core_of(parse_line(text)["body"])
        if not body:
            return 0
        keys = [k for k, rec in self._data["items"].items()
                if core_of(rec.get("by_body") or "") == body]
        if not keys:
            return 0
        for k in keys:
            del self._data["items"][k]
        self._save()
        return len(keys)

    def record_for(self, text: str) -> dict | None:
        """按正文找结局栏记录 (记忆页要显示"被谁取代、为什么、什么时候")."""
        body = core_of(parse_line(text)["body"])
        if not body:
            return None
        for rec in self._data["items"].values():
            if core_of(rec.get("body") or "") == body:
                return rec
        return None

    def ended_record(self, line: str) -> dict | None:
        return self._data["items"].get(key_of(line))

    def filter(self, lines: list[str]) -> list[str]:
        """被取代的不再被想起 (读路径的最后一步). 保持原来的顺序. """
        return [ln for ln in lines if not self.is_ended(ln)]

    def overview_extra(self, line: str) -> dict:
        """记忆页要显示的东西: 被谁关的、为什么、原话. 没有结局栏就是空."""
        rec = self.ended_record(line) or {}
        if not rec:
            return {}
        return {"ended": True, "superseded_by": rec.get("by", ""), "why": rec.get("why", ""),
                "quote": rec.get("quote", ""), "at": rec.get("ended_at", ""),
                "manual": bool(rec.get("manual"))}

    def count(self) -> int:
        return len(self._data["items"])


# ---------------------------------------------------------------- 一遍过: 新的一条对上一批老的

@dataclass
class Applied:
    ended: list[str] = field(default_factory=list)      # 被写进结局栏的旧记忆原文
    decisions: list[tuple[str, Decision]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.ended)


def apply_new(store: SupersedeStore, new_line: str, old_lines: list[str], *,
              turn_text: str = "") -> Applied:
    """写进一条新记忆时调用: 找出它取代了谁, 写结局栏. 返回做了什么.

    一个都判不出就什么都不做 —— 这是"宁可漏判"最直接的体现.
    """
    out = Applied()
    new_key = key_of(new_line)
    for old in old_lines:
        if key_of(old) == new_key:          # 同一条, 不是取代
            continue
        d = judge(new_line, old, turn_text=turn_text)
        out.decisions.append((old, d))
        if d.supersedes:
            store.end(old, by=new_key, by_body=core_of(parse_line(new_line)["body"]), why=d.reason, label="superseded")
            out.ended.append(old)
            break                            # 一条新的只关一条最像的, 别一次关一片
    return out


def close_without_new(store: SupersedeStore, old_line: str, turn_text: str, *,
                      why: str = "用户说已经好了") -> bool:
    """独立关闭通道: 这一轮**不产生新记忆**, 但要把旧的关掉 ("我感冒好了").

    turn_text 要给: 关闭也得有原文撑着. 给不出原文就不关.
    """
    if _quote_of(old_line, turn_text) is None and not _mentions_any(old_line, turn_text):
        return False
    return store.end(old_line, why=why, quote="", manual=True)


def _mentions_any(old_line: str, turn_text: str) -> bool:
    """关闭场景放宽一点: 只要这一轮提到了旧记忆里的主体就算 (说"感冒好了"不必复述整句)."""
    body = parse_line(old_line)["body"]
    return bool(shared_subject(body, turn_text))
