"""外部渠道的纯规则部分: 进来的文字怎么清洗、出去的话怎么检查和分段、限频和每天的额度.

- clean_in: 外部来的文字变成一行"数据": 去控制字符、零宽和双向控制字符, 换行变空格, 方括号和尖括号换成全角.
  这样消息里伪造不出 [系统]、[主人] 这类行头, 也伪造不出 <<外部消息 …>> 边界 (行头和边界都是代码拼的半角);
- clean_out / private_hit: 发出去之前去掉链接和 @全体, 再查手机号、身份证号、邮箱、电脑路径、密钥, 以及主人记忆里
  住处、生日、工作这些值. 查到就不发 (由管理器提示主人). 这是尽力而为的第二道关, 第一道是访客会话根本拿不到主人的记忆;
- segment: 一条回复按句子拆成两三条短消息, 像真人打字 (AstrBot 的分段回复);
- strip_tone: 语气标记 (【语气:轻】) 历史里原样留着, 发出去和界面展示的文本里剥掉 (ADR-0007);
- RateLimiter / DailyBudget: 同一个会话两句的间隔、每小时句数; 每天的云端 token.
"""
from __future__ import annotations

import re
import time
from collections import deque
from datetime import datetime

_ZW = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_CTL = re.compile(r"[\x00-\x1f\x7f]")
_FULL = str.maketrans({"[": "［", "]": "］", "<": "＜", ">": "＞"})
_SPACES = re.compile(r"\s{2,}")


def clean_in(text, limit: int = 300) -> str:
    """外部来的一段文字 (消息正文、昵称、群名) -> 一行可以放进提示词的数据."""
    s = _ZW.sub("", str(text or ""))
    s = _CTL.sub(" ", s).translate(_FULL)
    s = _SPACES.sub(" ", s).strip()
    return s[:limit] + "…" if len(s) > limit else s


_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|cn|net|org|top|xyz|io|me|cc|vip|link|site|info)\b(?:/\S*)?")
_AT_ALL = re.compile(r"@\s*(?:全体成员|全体|all)\b", re.IGNORECASE)
_HEADER = re.compile(r"^\s*\[[^\]]{0,40}\]\s*")  # 模型照抄了行头 "[14:02 #5 某人(1)]"
_MARK = re.compile(r"<</?外部消息[^>]*>>")


def clean_out(text) -> str:
    """要发出去的话: 去控制字符和零宽字符、链接、@全体、照抄的行头和边界标记."""
    s = _ZW.sub("", str(text or ""))
    s = _CTL.sub(" ", s)
    s = _MARK.sub("", s)
    s = _HEADER.sub("", s)
    s = _URL.sub("", s)
    s = _AT_ALL.sub("", s)
    return _SPACES.sub(" ", s).strip()


def is_silent(text: str) -> bool:
    """模型选择不说: 空的、只有标点, 或者写了 [SILENT] / 不说 这类标记."""
    s = re.sub(r"[\s\W_]+", "", str(text or ""))
    return not s or s.upper() in ("SILENT", "NONE", "NULL") or s in ("不说", "沉默", "不回复")


_TONE = re.compile(r"【\s*语气\s*[:：]\s*([^】\r\n]{1,20}?)\s*】")


def strip_tone(text) -> tuple[str, str | None]:
    """去掉 reply 里的语气标记 (【语气:轻】这类), 返回 (剥过的文字, 语气); 没有标记时文字原样、语气是 None.
    ADR-0007: 历史里存模型原文 (guest.GuestSession._ask 原样存), 发出去和界面展示用剥过的这一版."""
    s = str(text or "")
    tones = [m.group(1).strip() for m in _TONE.finditer(s)]
    if not tones:
        return s, None
    return _SPACES.sub(" ", _TONE.sub("", s)).strip(), tones[0]


_PATTERNS = [
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<![\dXx])\d{17}[\dXx](?![\dXx])")),
    ("邮箱", re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    ("电脑上的路径", re.compile(r"(?i)\b[a-z]:[\\/](?:users|用户)[\\/]|/(?:Users|home)/\w")),
    ("密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}|\bBearer\s+[A-Za-z0-9._\-]{8,}")),
]


def private_hit(text: str, secrets: list[tuple[str, str]] = ()) -> str | None:
    """回复里有不该往外说的东西就返回是什么 (给主人看的原因), 没有返回 None. secrets: [(类别, 值)], 值精确匹配."""
    for what, pat in _PATTERNS:
        if pat.search(text):
            return what
    for what, value in secrets:
        if len(value) >= 2 and value in text:
            return f"主人的{what}"
    return None


def cap(text: str, limit: int) -> str:
    """超过 limit 字时截到最后一个句末标点; 没有句末标点就硬截."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(c) for c in "。！？!?~～…")
    return cut[: m + 1] if m >= limit // 3 else cut


_SENTENCE = re.compile(r"[^。！？!?~～…]+[。！？!?~～…]*")


def segment(text: str, chars: int = 40, max_parts: int = 3) -> list[str]:
    """一条回复拆成几条短消息: 按句子攒, 每条不超过 chars 字 (一句本身超长就单独一条), 最多 max_parts 条 (多的并进最后一条)."""
    text = text.strip()
    if chars <= 0 or len(text) <= chars:
        return [text] if text else []
    parts, cur = [], ""
    for s in _SENTENCE.findall(text):
        if cur and len(cur) + len(s) > chars:
            parts.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        parts.append(cur)
    if len(parts) > max_parts:
        parts = parts[: max_parts - 1] + ["".join(parts[max_parts - 1:])]
    return [p.strip() for p in parts if p.strip()]


class RateLimiter:
    """每个会话: 两句之间至少隔 min_gap 秒, 一小时最多 per_hour 句 (0 是不限)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._sent: dict = {}

    def wait_s(self, key, min_gap: float, per_hour: int) -> float | None:
        """还要等几秒才能再说 (0: 现在就能说); None: 这一小时的额度用完了."""
        now = self.clock()
        q = self._sent.get(key)
        if q is None:
            return 0.0
        while q and now - q[0] >= 3600:
            q.popleft()
        if per_hour and len(q) >= per_hour:
            return None
        return max(0.0, q[-1] + min_gap - now) if q and min_gap else 0.0

    def record(self, key) -> None:
        self._sent.setdefault(key, deque()).append(self.clock())


class DailyBudget:
    """每天的 token 额度 (只对云端模型算; limit 0 是不限). 过了零点重新算."""

    def __init__(self, limit: int = 0, clock=datetime.now):
        self.limit = int(limit or 0)
        self.clock = clock
        self.day = None
        self.used = 0

    def _roll(self) -> None:
        today = self.clock().date()
        if today != self.day:
            self.day, self.used = today, 0

    def left(self) -> int | None:
        self._roll()
        return None if not self.limit else max(0, self.limit - self.used)

    def add(self, tokens: int) -> None:
        self._roll()
        self.used += max(0, int(tokens or 0))
