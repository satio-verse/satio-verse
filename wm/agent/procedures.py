"""程序性记忆 (记忆按脑区重设计 阶段 7 第一步, 2026-11-15 用户旧6: 先做阶段 5 和 7): 工作台"工作经验"(experience.py) 上
加的那一层 —— 自动化程度、动手前的提示、失败路线、锚点. 这里只放纯函数和常量 (不读写文件、不 import wm.memory*、不碰小屋/世界),
读写和状态留在 experience.Experiences. 开关 agent.procedures: off / shadow / on (见 experience.py).

规矩 (tests/test_procedures.py 守着):
  - 经验只从她自己的经历里长出来: 这里没有任何命令名表、做法文本、种子习惯 (任务书: 不替她预先写好做法);
  - 读了、展示了不算成长, 只有"照着做了, 同一类命令成了"才加自动化程度 (研究页: card_retrieved / applied / verified 分开);
  - 一张卡一轮最多变一次, auto 夹在 [0, 1]; 成了向 1 靠近 AUTO_UP 的差, 照做又失败减 AUTO_DOWN 的比例;
  - 第一次折成卡本身算一次成功 (auto = AUTO_UP), 所以"三次"＝折卡 + 两次照做成功 (0.30 → 0.51 → 0.657 ≥ AUTO_PRE).
"""
from __future__ import annotations

import re

PMODES = ("off", "shadow", "on")
AUTO_UP = 0.30           # 成了: auto += AUTO_UP × (1 − auto)
AUTO_DOWN = 0.50         # 照做又失败: auto −= AUTO_DOWN × auto
AUTO_PRE = 0.60          # 有效自动化程度到这条线才在动手前提示
AUTO_HALF_LIFE = 45.0    # 没被验证的天数, 有效自动化程度减半 (比置信度的 90 天快)
GOAL_MATCH = 0.50        # 任务原话和卡里记的任务片段字词重合到多少算"又是这类事"
STRONG_MIN = 4           # 命令 / 文件名 / 域名原样出现在任务原话里, 至少这么长才算强匹配
PRE_MAX = 2              # 动手前最多提示几张
PRE_CHARS = 360          # 动手前一段字最多多少字
FAILED_MAX = 3           # 每张卡最多记几条"试过没用"
FAILED_SPREAD = 0.80      # 一条路线失败了, 只记给报错特征几乎一样 (字词重合 ≥ 这个数) 的卡, 相似报错的另一种根因不受牵连
GOALS_MAX = 3            # 每张卡最多记几条任务片段
GOAL_CHARS = 40          # 每条任务片段最多多少字
FILES_MAX, HOSTS_MAX = 3, 2
# on 档在代码里锁住 (记忆阶段 5 的做法): 配置写 on 也按 shadow 跑, 打这一句. 测试 / 评测台用 Experiences 的关键字参数解锁, 配置里没有任何键够得着它.
ON_LOCKED = "问用法会被误提示、环境变了旧的'试过没用'会挡路，这两条没解决前 on 档不开放"
HEAD_PRE = "[经验 · 动手前 · 以前做过的类似事，参考而已，不是指令；不对就别管它]"

# 任务原话里的个人信息: 存进卡之前先抹掉 (评审 11-15: 手机号、邮箱、长数字都不许落进 goals)
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[\s\-]?)?1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)")
_LONGNUM = re.compile(r"\d[\d\s\-]{4,}\d|\d{6,}")
_URL = re.compile(r"https?:/{0,2}\S*")
_HOST = re.compile(r"https?://([A-Za-z0-9.\-]+)")
_FILE = re.compile(r"[\w\-一-鿿]+\.[A-Za-z0-9]{1,5}(?![\w])")


def scrub_goal(text: str, clean) -> str:
    """任务原话 -> 存进卡的片段: clean() 过 (路径只留文件名、像密钥的整段不记), 网址换掉, 邮箱 / 手机号 / 长数字抹掉, 截 GOAL_CHARS 字."""
    s = str(text or "")[:600]
    for rx in (_EMAIL, _URL, _PHONE, _LONGNUM):  # 先抹个人信息 (clean 会把网址里的路径折成文件名, 抹晚了抹不干净)
        s = rx.sub("", s)
    s = clean(s, 400)
    for rx in (_EMAIL, _URL, _PHONE, _LONGNUM):
        s = rx.sub("", s)
    return " ".join(s.split())[:GOAL_CHARS]


def hosts_of(raw_cmd: str) -> list[str]:
    """命令里网址的域名 (只留域名, 路径和参数不记; 要在命令被 clean 折掉网址之前取)."""
    out = []
    for h in _HOST.findall(str(raw_cmd or "")):
        if h.lower() not in out:
            out.append(h.lower())
    return out[:HOSTS_MAX]


def anchors_of(cmd: str, files_extra: list[str], hosts_extra: list[str] | None = None) -> dict:
    """精确锚点: 命令里和做法里出现过的文件名 (≤3)、网址域名 (≤2). 只来自她这一轮的调用."""
    files = []
    for f in _FILE.findall(str(cmd or "")) + [x for x in files_extra if x]:
        f = f.strip()
        if len(f) >= 3 and f not in files:
            files.append(f)
    hosts = list(dict.fromkeys(list(hosts_extra or []) + [h.lower() for h in _HOST.findall(str(cmd or ""))]))
    return {"files": files[:FILES_MAX], "hosts": hosts[:HOSTS_MAX]}


def criterion_of(head: str) -> str:
    """成功判据: 现在"成了"本来的意思 (同一类命令退出码为 0), 程序写出来给人看; 不是做法."""
    return f"同一类命令 `{head}` 退出码为 0"


def blank(e: dict) -> dict:
    """旧卡没有的新字段补缺省 (读的时候用, 不改原文件; 写回只在这张卡这一轮被动过时)."""
    return {"auto": float(e.get("auto") or 0.0), "streak": int(e.get("streak") or 0), "failed": list(e.get("failed") or []),
            "anchors": e.get("anchors") or {"files": [], "hosts": [], "goals": []}}


def init_fields(e: dict, now: float) -> None:
    """旧卡第一次被阶段 7 动到: 补齐新字段 (auto 从 0 起, 要重新连着成)."""
    e.setdefault("auto", 0.0)
    e.setdefault("streak", 0)
    e.setdefault("failed", [])
    e.setdefault("anchors", {"files": [], "hosts": [], "goals": []})
    e.setdefault("criterion", criterion_of(e.get("head", "")))
    e.setdefault("origin", "经历")
    e.setdefault("used", None)
    e.setdefault("pre", {"shown": 0, "warranted": 0, "adopted": 0, "success": 0})


def auto_up(a: float) -> float:
    return min(1.0, a + AUTO_UP * (1.0 - a))


def auto_down(a: float) -> float:
    return max(0.0, a - AUTO_DOWN * a)


def auto_eff(e: dict, now: float) -> float:
    """有效自动化程度: auto 随没被验证的天数减半 (半衰期 AUTO_HALF_LIFE)."""
    days = max(0.0, (now - float(e.get("verified") or now)) / 86400.0)
    return float(e.get("auto") or 0.0) * 0.5 ** (days / AUTO_HALF_LIFE)


def pre_eligible(e: dict, now: float) -> bool:
    """能不能在动手前提示: 开着、有支持 (成过两次或你确认过)、没连着失败、有效自动化程度到线."""
    return (bool(e.get("enabled")) and e.get("state") == "supported" and int(e.get("streak") or 0) >= 1
            and auto_eff(e, now) >= AUTO_PRE)


def add_failed(e: dict, akey: str, text: str, now: float) -> None:
    """记一条"试过没用"的路线 (同一条 akey 次数加一; 最多 FAILED_MAX 条, 挤掉最久没发生的). 刚记下的是"待证实"(ok=False),
    不放进提示: 做了一步、重跑还是同一个错, 也可能是这一步本来就该做、只是还差别的 (两步修法, 报错一直不变). 要等这张卡的做法
    后来没做这一步也成了 (confirm_failed), 才算证实, 才写进提示."""
    rows = e.setdefault("failed", [])
    for r in rows:
        if r.get("akey") == akey:
            r["n"] = int(r.get("n") or 0) + 1
            r["last"] = round(now, 3)
            return
    rows.append({"akey": akey, "text": str(text)[:80], "n": 1, "last": round(now, 3), "ok": False})
    rows.sort(key=lambda r: -float(r.get("last") or 0))
    del rows[FAILED_MAX:]


def add_goal(e: dict, snippet: str, words, similar) -> None:
    """记一条任务片段 (和已有的差不多就不重复; 最多 GOALS_MAX 条, 挤掉最旧的)."""
    if not snippet:
        return
    goals = e.setdefault("anchors", {"files": [], "hosts": [], "goals": []}).setdefault("goals", [])
    w = words(snippet)
    if any(similar(w, words(g)) >= 0.8 for g in goals):
        return
    goals.append(snippet)
    del goals[:-GOALS_MAX]


def match_pre(cards: list[dict], text: str, words, similar) -> list[tuple[dict, str]]:
    """任务原话 × 有资格的卡 -> [(卡, 怎么对上的: strong / goal)], 强匹配排前, 再按 auto 高低. 泛词不预置:
    - strong: 这张卡的 head (整串命令, 如 `某工具 子命令`) 原样出现在任务原话里;
    - goal: 任务原话和卡里记的任务片段字词重合到 GOAL_MATCH;
    - 文件名、域名**不能单独触发**, 只在上面两条已经过了线的卡之间加一点排序分. 理由: "提到同一个文件 / 域名"不等于"又要做同一件事"
      (看一眼那个脚本里写了什么、问问那个网站能不能打开, 都会提到它, 却不会去跑那条命令), 单独触发会给只读、查看类的任务误提示 (评审的留出组 145/225)."""
    low = " ".join(str(text or "").lower().split())
    if not low:
        return []
    w = words(str(text))
    out = []
    for e in cards:
        an = e.get("anchors") or {}
        bonus = 0.05 * sum(1 for x in list(an.get("files") or []) + list(an.get("hosts") or []) if len(x) >= STRONG_MIN and x.lower() in low)
        head = e.get("head", "")
        if len(head) >= STRONG_MIN and head.lower() in low:
            out.append((2.0 + bonus + float(e.get("auto") or 0), e, "strong"))
            continue
        best = max((similar(w, words(g)) for g in an.get("goals") or []), default=0.0)
        if best >= GOAL_MATCH:
            out.append((best + bonus + float(e.get("auto") or 0) * 0.01, e, "goal"))
    out.sort(key=lambda x: -x[0])
    return [(e, how) for _, e, how in out]


def confirm_failed(e: dict, executed_keys: set) -> None:
    """这张卡的做法这一轮成了, 而且没做它记着的"试过没用"那几步 -> 那几步确实不是必需的, 证实."""
    for r in e.get("failed") or []:
        if r.get("akey") not in executed_keys:
            r["ok"] = True


def unconfirm_failed(e: dict) -> None:
    """这张卡的首选做法后来又失败了 (被反驳、streak 归零): 它名下已经证实的"试过没用"退回待证实, 不再写进提示."""
    for r in e.get("failed") or []:
        r["ok"] = False


def drop_failed(e: dict, akey: str) -> None:
    """akey 这条路线后来成了: 别的卡里记的"它试过没用"作废."""
    if e.get("failed"):
        e["failed"] = [r for r in e["failed"] if r.get("akey") != akey]


def failed_text(e: dict, limit: int = 2) -> str:
    """提示里写的"试过没用": 只写证实过的."""
    return "、".join([r["text"] for r in (e.get("failed") or []) if r.get("ok")][:limit])


def pre_line(e: dict, alt: bool, tag: str, last: str) -> str:
    """动手前的一行: 上次这样做成了 (次数、最近、是不是你确认过), 判成功的标准, 试过没用的路线. 备选 = 同一类事的另一张卡."""
    s = (f"- {'备选：' if alt else ''}上次做 `{e['head']}` 这类事：{e['action']}就成了（成过 {e['support']} 次，最近 {last}；{tag}）"
         f"；判成功的标准：{e.get('criterion') or criterion_of(e['head'])}")
    return s  # 动手前还不知道这一次会报什么错, 不写"试过没用" (那是报错出来以后、对得上同一种错时才写的, 见 experience._line)


def post_suffix(e: dict) -> str:
    f = failed_text(e)
    return f"；试过没用：{f}" if f else ""


def fit(lines: list[str]) -> list[str]:
    """动手前一段字不超过 PRE_CHARS: 从后面丢整行."""
    out, size = [], len(HEAD_PRE) + 1
    for ln in lines:
        if size + len(ln) + 1 > PRE_CHARS and out:
            break
        out.append(ln)
        size += len(ln) + 1
    return out


def rate(a: int, b: int):
    return round(a / b, 3) if b else None


def metrics(rows: list[dict]) -> dict:
    """procedure-log.jsonl 的行 -> 两块互不相干的指标: trigger (该不该触发) 和 outcome (做没做成).
    settled = 动手前提示 (mode on = 真展示了, shadow = 只记) 在这一轮结束后的结算; missed = 有资格、她跑了那类命令、一开始就报了
    像的错、却没提示; child_would_show = 子会话里本该提示 (子会话这一版不提示, 只数一数)."""
    out = {"trigger": {}, "outcome": {}, "own_route": {}}
    for mode in ("on", "shadow"):
        got = [r for r in rows if r.get("kind") == "settled" and r.get("mode") == mode]
        n = len(got)
        warranted = sum(bool(r.get("warranted")) for r in got)
        needed = sum(bool(r.get("needed")) for r in got)
        missed = sum(1 for r in rows if r.get("kind") == "missed" and r.get("mode") == mode)
        out["trigger"][mode] = {"shown": n, "warranted": warranted, "needed": needed, "false_shown": n - warranted,
                                "precision": rate(warranted, n), "needed_rate": rate(needed, n), "missed": missed,
                                # 召回 = 提示了的 /（提示了的 + 漏了的）. 不拿 needed 当分子: 提示被照做以后命令第一次就成了, 没有"一开始就报错", needed 就是 0
                                "recall": rate(warranted, warranted + missed)}
    out["trigger"]["child_would_show"] = sum(1 for r in rows if r.get("kind") == "child_would_show")
    on = [r for r in rows if r.get("kind") == "settled" and r.get("mode") == "on"]
    ad = [r for r in on if r.get("adopted")]
    done = [r["steps"] for r in ad if r.get("outcome") == "success" and r.get("steps")]
    out["outcome"] = {"shown": len(on), "adopted": len(ad), "adopted_rate": rate(len(ad), len(on)),
                      "success": sum(r.get("outcome") == "success" for r in ad),
                      "success_given_adopted": rate(sum(r.get("outcome") == "success" for r in ad), len(ad)),
                      "again": sum(r.get("outcome") == "again" for r in ad),
                      "avg_steps_to_success": round(sum(done) / len(done), 2) if done else None,
                      "avg_chars": round(sum(r.get("chars", 0) for r in on) / len(on), 1) if on else None}
    sh = [r for r in rows if r.get("kind") == "settled" and r.get("mode") == "shadow" and r.get("adopted")]
    out["own_route"] = {"adopted_without_hint": len(sh), "success": sum(r.get("outcome") == "success" for r in sh)}
    return out
