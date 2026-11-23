"""心跳: 她自己醒过来 (2026-11-17 开工; 来源 11-07 外部调研 P0 第 2 件 / dsh-heartbeat / Generative Agents).

为什么做: 17 个 B 站视频的评论区里最热的一条说这类东西"主观能动性基本为 0"，还有一条讲得更准 ——
给 AI 一小时自由时间、权限全开，它 20 分钟就没事干了，最后画了张画说一直在等你敲门。
"她要的不是自由时间，是能和你互动的自由时间。" 所以心跳不是放她自己空转, 是给她"想做的事" +
攒着等主人在的时候说。

做什么 (规格照 11-07 页面第六节第 2 件):
- 每 11–26 分钟随机一拍, 写一行"平时" (时间、主人在不在、她在干什么), 存用户数据区, 工作台能看。
- 叫醒: 每拍只读工作台的"定时提醒"和"待办"存的文件, 到期的经派活线叫醒她的会话。
- 唤醒卡: 没答完的问题、抽屉里攒的、今天做过什么、空转了几拍; 醒来先看这张纸。
- 自己出题: 一轮少于 20 字退回、少于 3 轮不收、跨度不到 20 分钟不算想过; 想通的放"抽屉", 等主人在时说。
- 当天打算 / 睡前回顾。
- 黑盒: 她自己的文件 + 近 7 天记忆, 每天封一次 sha256 哈希链 (防自己记错, 不防人看)。
- 花钱闸: 思考单独一个小额度 (resident 单任务预算, 默认 0.5 元/天), 用完就停、第二天说, 不叫醒主人。

这一份是**纯的**: 不碰文件、不网络、不调模型、不看时钟 (时间都从参数进来), 所以能穷举测。
真正读写的地方在 `wm/agent/heartbeat.py`。

三处别踩的坑 (11-17 摸代码时确认的):
1. 别把拍子塞进 `wm/channels/manager.py` 的 `_tick_loop` —— 那个循环跟 QQ 配置和暂停绑着, QQ 一停心跳就停。
   要挂 `wm/app.py` 的 `_startup` / `_cleanup`。
2. `~/.worldmodel/serve/beat.json` 是**看守**的心跳, 名字别抢; 我们用 `~/.worldmodel/heartbeat/`。
3. 唤醒卡**不能塞进 `_system` / `_memory_block`**, 那会让系统提示逐字变化、毁掉前缀缓存 (现在命中 87–94%)。
   它是给她看的一轮上下文, 不是系统提示的一部分。
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 拍子: 11–26 分钟随机 (照 dsh-heartbeat 0.4.0)
MIN_GAP_S = 11 * 60.0
MAX_GAP_S = 26 * 60.0

# 自己出题的三道闸
MIN_ROUND_CHARS = 20
MIN_ROUNDS = 3
MIN_SPAN_S = 20 * 60.0

# 抽屉最多攒这么多条, 免得越攒越说个没完
DRAWER_MAX = 40

# 唤醒卡最多列几条 (太多了她自己看不完, 也白花钱)
CARD_MAX_ITEMS = 8


def next_gap_s(rng: random.Random | None = None) -> float:
    """下一拍隔多久。随机, 不给外部定节奏的机会。"""
    r = rng or random
    return r.uniform(MIN_GAP_S, MAX_GAP_S)


def owner_present(user_texts, now: datetime, window_s: float = 15 * 60.0) -> bool:
    """主人这会儿在不在。

    只认**他自己真打的字** —— 不认文件修改时间、不认她的工具调用、不认渠道自动消息。
    这是心跳唯一能拿到"你在不在"的可靠信号; 认错了她会以为你在, 然后一直自说自话。
    `user_texts` 是 [(时间, 文本)] 或 [(时间, 文本, 来源)], 来源里带 "auto"/"system" 的不算。
    """
    for item in user_texts or ():
        try:
            at, text = item[0], item[1]
            src = item[2] if len(item) > 2 else ""
        except (TypeError, IndexError):
            continue
        if src in ("auto", "system", "agent"):
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        if _as_dt(at) is None:
            continue
        if 0 <= (now - _as_dt(at)).total_seconds() <= window_s:
            return True
    return False


def _as_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(v[:26], fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


@dataclass
class Beat:
    """一拍。写进平时日志的一行。"""

    at: datetime
    owner_present: bool
    doing: str = ""
    idle_beats: int = 0
    note: str = ""

    def line(self) -> dict:
        return {
            "at": self.at.isoformat(timespec="seconds"),
            "owner": bool(self.owner_present),
            "doing": self.doing,
            "idle_beats": int(self.idle_beats),
            "note": self.note,
        }

    def text(self) -> str:
        """给人看的一行。默认是"什么也没发生" —— 大多数拍本来就该是空的。"""
        who = "主人在" if self.owner_present else "主人不在"
        what = self.doing or "什么也没发生"
        tail = f" ({self.note})" if self.note else ""
        return f"{self.at:%m-%d %H:%M} {who}, {what}{tail}"


@dataclass
class WakeCard:
    """唤醒卡: 她醒来先看的那张纸。**不是系统提示**, 见文件头第 3 个坑。"""

    unanswered: list[str] = field(default_factory=list)
    drawer: list[str] = field(default_factory=list)
    today: list[str] = field(default_factory=list)
    idle_beats: int = 0

    def render(self) -> str:
        parts: list[str] = []
        if self.idle_beats:
            parts.append(f"已经空转了 {self.idle_beats} 拍。")
        if self.unanswered:
            parts.append("还没答完的:\n" + _bullets(self.unanswered))
        if self.today:
            parts.append("今天做过:\n" + _bullets(self.today))
        if self.drawer:
            parts.append("之前想通、攒着等他来的:\n" + _bullets(self.drawer))
        if not parts:
            return "（这张纸是空的。）"
        return "\n\n".join(parts)

    def empty(self) -> bool:
        return not (self.unanswered or self.drawer or self.today or self.idle_beats)


def _bullets(items) -> str:
    return "\n".join(f"- {str(x).strip()}" for x in list(items)[:CARD_MAX_ITEMS] if str(x).strip())


@dataclass
class Thinking:
    """一轮"自己想事"。三道闸都在这里, 免得各写一遍。"""

    rounds: list[tuple[datetime, str]] = field(default_factory=list)

    def add(self, text: str, at: datetime) -> bool:
        """收一轮。少于 20 字退回 (不算一轮)。"""
        t = (text or "").strip()
        if len(t) < MIN_ROUND_CHARS:
            return False
        self.rounds.append((at, t))
        return True

    def ready(self, min_rounds: int = MIN_ROUNDS, min_span_s: float = MIN_SPAN_S) -> bool:
        """够不够算"想过": 至少 3 轮, 而且跨度至少 20 分钟 (三轮挤在 20 分钟里不算)。"""
        if len(self.rounds) < min_rounds:
            return False
        span = (self.rounds[-1][0] - self.rounds[0][0]).total_seconds()
        return span >= min_span_s

    def harvest(self) -> str:
        """想通的收成: 取最后一轮 (她说得最清楚的那句), 不给它再总结一遍的机会。"""
        if not self.rounds:
            return ""
        return self.rounds[-1][1].strip()

    def reset(self) -> None:
        self.rounds.clear()


@dataclass
class Budget:
    """思考的每日额度。用完就停, 第二天说, **不叫醒主人**。

    resident 单任务预算 (2026-11-17 用户选 B): 不碰共用的账本桶, 一个任务一份。
    """

    cap_cny: float = 0.5
    spent_cny: float = 0.0

    def allow(self, cost_cny: float = 0.0) -> bool:
        if self.cap_cny <= 0:
            return False
        return self.spent_cny + max(0.0, cost_cny) <= self.cap_cny + 1e-9

    def spend(self, cost_cny: float) -> bool:
        """记一笔。超了就**不记**并返回 False (调用方据此停下)。"""
        if not self.allow(cost_cny):
            return False
        self.spent_cny += max(0.0, cost_cny)
        return True

    def exhausted(self) -> bool:
        """一分钱都不剩了。注意不能写成 `not allow(0)`: 成本 0 永远是允许的,
        于是"已用满"会被判成"没耗尽", 心跳该停的时候不停。"""
        return self.left_cny() <= 1e-9

    def left_cny(self) -> float:
        return max(0.0, self.cap_cny - self.spent_cny)


def due_schedule(items, now: datetime) -> list[dict]:
    """工作台"定时提醒"里到期的。

    数据在 `~/.worldmodel/agent/state/workbench-schedule/schedule.json`,
    形如 {"items": [{"id", "text", "due_at", "enabled", "status", ...}]}。
    只读不写; 到没到点只按 `due_at`, 不按 `repeat` 自己算 —— 算错了会重复叫她。
    """
    out = []
    for it in items or ():
        if not isinstance(it, dict):
            continue
        if it.get("enabled") is False or it.get("status") in ("done", "cancelled", "fired"):
            continue
        at = _as_dt(it.get("due_at"))
        if at is None or at > now:
            continue
        out.append(it)
    out.sort(key=lambda x: str(x.get("due_at") or ""))
    return out


def open_todos(items) -> list[dict]:
    """工作台"待办"里还没做完的。数据在 `.../workbench-todo/todos.json`。"""
    return [
        it
        for it in (items or ())
        if isinstance(it, dict) and str(it.get("status") or "todo") == "todo"
    ]


def hash_chain(prev: str, parts) -> str:
    """黑盒: 把这一天的东西接进哈希链。

    `parts` 是 [(名字, bytes)] —— 她的文件 + 近 7 天记忆。只做加法, 不改原文, 所以
    "哪句什么时候被改过"看得出来, 而内容仍然只有她自己能读。
    """
    h = hashlib.sha256((prev or "").encode("utf-8"))
    for name, data in sorted(parts or (), key=lambda kv: str(kv[0])):
        h.update(str(name).encode("utf-8"))
        h.update(b"\x00")
        h.update(data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def drawer_push(drawer, text: str, cap: int = DRAWER_MAX) -> list[str]:
    """收成进抽屉。同样的不重复塞; 超了丢最老的。"""
    t = (text or "").strip()
    if not t:
        return list(drawer or ())
    out = [x for x in (drawer or ()) if x != t]
    out.append(t)
    return out[-cap:]


# ---------------------------------------------------------------- 平时日志 (只算, 不写)

def summarize_beats(beats, days: int = 7) -> dict:
    """给工作台看的小结: 这几天空了多久、主人在的比例。不花钱, 纯统计。"""
    rows = [b for b in (beats or ()) if isinstance(b, dict)]
    rows = rows[-max(1, days) * 96:]  # 一拍最多 11 分钟, 一天约 96 拍
    if not rows:
        return {"beats": 0, "owner_ratio": 0.0, "idle_max": 0}
    owner = sum(1 for b in rows if b.get("owner"))
    idle = [int(b.get("idle_beats") or 0) for b in rows]
    return {
        "beats": len(rows),
        "owner_ratio": round(owner / len(rows), 3),
        "idle_max": max(idle) if idle else 0,
    }


def dumps(rec: dict) -> str:
    """平时日志一行。不转义中文, 免得日志里一堆 \\u。"""
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))


def loads(line: str) -> dict | None:
    try:
        v = json.loads(line)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None
