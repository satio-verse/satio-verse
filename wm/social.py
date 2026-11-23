"""社交后果数据闭环 (第一版): 记录角色每轮做了什么, 以及用户下一轮怎么反应, 给"做这个动作/用这个语气之后
对话会往哪走"的预测器攒数据.

反应分四类 (规则粗标, 以后可以人工校正):
  escalate  用户不满或打断: 下一句有否定/不耐烦的词, 或者这一轮被插话打断
  withdraw  用户退缩: 下一句很短的敷衍 (嗯/哦/好吧), 或隔了 30 s 以上才说话, 或说完不久就断开
  shift     转话题: 下一句和角色的回复几乎没有共同的字
  continue  其他 (接着聊)
预测器: 按 (op, 动作种类, 情绪) 统计四类反应的频率, 拉普拉斯平滑; 数据少时退回全局频率.
现在只把预测值写进延迟埋点 (trace.meta.social), 不参与决策; 攒够数据后再评估它能不能指导动作和语气.

日志: logs/social/YYYYMMDD.jsonl, 每行一轮 (特征 + 反应标签).
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

OUTCOMES = ["continue", "shift", "withdraw", "escalate"]
_NEG = re.compile(r"不要|别(再|说|动|闹)|烦|闭嘴|停下|讨厌|无聊|不对|错了|听不懂|好吵|走开")
_TERSE = re.compile(r"^(嗯+|哦+|噢|好吧|随便|行吧|算了|呵呵|。|…+)[。！!，,.…]*$")


def _chars(s: str) -> set[str]:
    return set(re.findall(r"[一-鿿]", s))


def label(prev: dict, next_text: str | None, gap_s: float | None, interrupted: bool) -> str:
    if interrupted:
        return "escalate"
    if next_text is None:  # 没有下一句: 断开了
        return "withdraw" if gap_s is not None and gap_s < 30 else "continue"
    if _NEG.search(next_text):
        return "escalate"
    if _TERSE.match(next_text.strip()) or (gap_s is not None and gap_s > 30):
        return "withdraw"
    reply = _chars(prev.get("reply", ""))
    nxt = _chars(next_text)
    if reply and nxt and len(reply & nxt) / max(1, min(len(reply), len(nxt))) < 0.1 and len(nxt) >= 4:
        return "shift"
    return "continue"


def key_of(op: str | None, acts: list | None, emo: str | None) -> str:
    kinds = "+".join(sorted({a.get("do", "") for a in acts or []})) or "-"
    return f"{op or '-'}|{kinds}|{emo or '-'}"


class SocialModel:
    """按 (op, 动作种类, 情绪) 的反应频率. 从已有的 JSONL 日志里加载."""

    def __init__(self):
        self.counts: dict[str, Counter] = defaultdict(Counter)
        self.total = Counter()

    def add(self, key: str, outcome: str) -> None:
        self.counts[key][outcome] += 1
        self.total[outcome] += 1

    def predict(self, key: str) -> dict[str, float]:
        prior = {o: (self.total[o] + 1) / (sum(self.total.values()) + len(OUTCOMES)) for o in OUTCOMES}
        c = self.counts.get(key)
        n = sum(c.values()) if c else 0
        k = 4.0  # 先验强度: 某个组合见得越多, 越信它自己的频率
        return {o: round(((c[o] if c else 0) + k * prior[o]) / (n + k), 3) for o in OUTCOMES}


class SocialLog:
    def __init__(self, log_dir: Path):
        self.dir = Path(log_dir) / "social"
        self.model = SocialModel()
        self._lock = threading.Lock()
        try:
            for f in sorted(self.dir.glob("*.jsonl")):
                for line in f.read_text(encoding="utf-8").splitlines():
                    r = json.loads(line)
                    if r.get("outcome"):
                        self.model.add(r["key"], r["outcome"])
        except (OSError, json.JSONDecodeError):
            pass

    def write(self, record: dict) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with (self.dir / f"{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if record.get("outcome"):
                self.model.add(record["key"], record["outcome"])


class SocialTracker:
    """一次连接: 角色这一轮的记录先挂着, 等用户下一句 (或断开) 时补上反应标签再写盘."""

    def __init__(self, log: SocialLog):
        self.log = log
        self.pending: dict | None = None

    def predict(self, op, acts, emo) -> dict:
        return self.log.model.predict(key_of(op, acts, emo))

    def support(self, key: str) -> int:
        """这个组合见过几次 (预测有多少依据)."""
        c = self.log.model.counts.get(key)
        return sum(c.values()) if c else 0

    def on_user_turn(self, text: str, t_ms: float) -> None:
        if self.pending:
            p, self.pending = self.pending, None
            p["outcome"] = label(p, text, (t_ms - p["end_ms"]) / 1000 if p.get("end_ms") else None, False)
            p["next_text"] = text[:60]
            self.log.write(p)

    def on_turn_done(self, record: dict, interrupted: bool) -> None:
        if interrupted:
            record["outcome"] = "escalate"
            self.log.write(record)
            return
        self.pending = record

    def close(self, t_ms: float) -> None:
        if self.pending:
            p, self.pending = self.pending, None
            p["outcome"] = label(p, None, (t_ms - p["end_ms"]) / 1000 if p.get("end_ms") else None, False)
            self.log.write(p)
