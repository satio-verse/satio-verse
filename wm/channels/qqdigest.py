"""QQ 上的重要消息: 分拣、攒着、整理好了跟主人说 (2026-11-01 用户: "桌宠能不能自行读取邮箱内容、QQ重要消息呢？并根据邮箱内容
进行整理、汇报，垃圾邮箱会忽略之类的". 邮箱那边见 mailbox*.py; 这里是 QQ 这边, 汇报的规矩照着邮箱的来).

她只看得到发到她自己 QQ 号上的: 白名单里的群、加了她好友的人跟她私聊. 你自己 QQ 号上的消息她看不到.

分拣 (classify: 只用规则, 不调模型, 不花钱):
- 要紧 urgent (读到就单独说一句): 群里有人 @ 你 (at 段的 QQ 号是你的) 或叫你 (my_names); 有人让她转告你 ("跟你主人说"
  "转告你主人"…); 全体通知、好友私聊里写着 紧急、急事、马上、尽快 这些.
- 要看 important (攒着一起说): 全体通知 (@全体成员); 群文件; 写着 截止、开会、考试、报名、缴费、放假、面试… (keywords 能加);
  你标了重要的人 (important_from) 说的; 好友私聊她的; 有人问起你 ("你主人…").
- 普通 low: 别的群聊 (只有表情、图片的也算), 只数每个群几条.
- 不管 ignored: 广告 (加微信、兼职、刷单、返利、优惠券、扫码进群…)、红包、同一句话刷屏、你标了不看的人 (ignore_from) 和群 (ignore_groups).
  你自己在群里说的不算 (你知道), 她自己说的渠道本来就不收.
汇报 (flush, 渠道每分钟的 tick 里调; 和邮箱一样低打扰):
- 要紧的单独说 (一小时最多 URGENT_PER_HOUR 次, 多的并进汇总); 要看的攒着, 两份汇总至少隔 report_gap_min 分钟, 一份最多念 max_items 件
  (要紧的在前, 同一档新的在前), 末尾带一句上次说完以后各群闲聊几条、广告几条. 什么要看的都没有就不说话.
- 免打扰时段 (proactive.quiet) 只攒不说; 要紧的照说.
- 你在哪个群里说话了 (你在场): 那个群里攒着的不再说.
- 发到哪 (deliver, 渠道给的): 你最近在的私聊 (微信 / QQ) + 桌宠对话栏. 一处都没发出去的留着下次.
- 不进知识库: 攒着的每条只留 SNIPPET 字 (手机号、邮箱、链接、长号码先打码), 存在渠道的状态目录 (~/.worldmodel, 不在 Obsidian 库里),
  说完就删; 汇报说的话会进你们私聊的记录 (和她平时跟你说的话一样).
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

URGENT_PER_HOUR = 3   # 要紧的一小时最多单独说几次
PENDING_MAX = 40      # 攒着没说的最多留几条 (多了先丢最早的要看的)
SNIPPET = 80          # 每条留多少字
MERGE_S = 1800        # 同一个人在同一处半小时内又说的要看的: 并成一条
FLOOD_S, FLOOD_N = 600, 3  # 同一处 10 分钟里同一句话出现 3 次: 刷屏

LABELS = re.compile(r"［[^］]*］")  # ［图片#1］［表情：笑］［文件#2 a.docx］这些标签
URGENT_WORDS = re.compile(r"紧急|急事|十万火急|速回|马上|尽快|立刻|立即|今天之内|今晚之前|赶紧")
IMPORTANT_WORDS = re.compile(r"截止|deadline|ddl|开会|会议|考试|报名|缴费|交费|放假|调休|调课|停课|作业|面试|签到|打卡|提交|"
                             r"交表|填表|投票|值班|聚餐|快递|取件|停水|停电|发工资|报销|合同|通知|公告", re.I)
RELAY = re.compile(r"(转告|告诉|通知|提醒)(一下)?(你|绫音)?(的|家)?主人|(跟|和|给)(你|绫音)(的|家)?主人(说|讲|带个话|带句话)")
ASKED = re.compile(r"你(的|家)?主人")
SPAM = re.compile(r"加\s*(微信|威信|vx|v信|薇)|兼职|日结|刷单|返利|返现|推广|优惠券|领券|扫码|进群|拉群|招代理|躺赚|稳赚|理财|贷款|"
                  r"网贷|彩票|博彩|棋牌|裸聊|红包|代刷|互赞|互粉|免费领|福利群|点击链接|低价出|"
                  # 查别人身份、户籍、手机号的黑产广告 (2026-11-07 鱼塘群里"公益免费开户工具……获取户籍信息"被当成"要看"报给了主人)
                  r"开户工具|户籍信息|社工库|开盒|查档|四要素|身份证查询|手机号查询|定位查人", re.I)
_MASKS = [(re.compile(rx, re.I), rep) for rx, rep in [
    (r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[邮箱]"),
    (r"(?:https?://|www\.)\S+", "[链接]"),
    (r"(验证码|校验码|动态码|code)(\D{0,8})\d{4,8}", r"\1\2[码]"),
    (r"(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])", "[证件号]"),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]"),
    (r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)", "[长号码]"),
]]


def snippet(text: str, owner_id: int = 0) -> str:
    """一条消息留给汇报的几十个字: @你 写成"@你", 全体通知的 @ 拿掉 (汇报里另说), 【】换成〔〕 (渠道发消息时把【表情包:…】当成
    发表情包的写法), 手机号、邮箱、链接、长号码打码, 合并空白, 截断."""
    t = str(text or "")
    if owner_id:
        t = t.replace(f"@{owner_id}", "@你")
    t = t.replace("@全体成员", "").replace("【", "〔").replace("】", "〕")
    for rx, rep in _MASKS:
        t = rx.sub(rep, t)
    t = " ".join(t.split())
    return t if len(t) <= SNIPPET else t[:SNIPPET - 1] + "…"


def classify(m: dict, conf: dict, private: bool) -> tuple[str, str]:
    """一条消息 -> (档, 为什么). 档: urgent / important / low / ignored. m 是渠道整理好的消息 (manager._parse 的结果);
    private: 好友跟她私聊 (不是你). conf: 渠道配置的 digest 那一段."""
    plain = LABELS.sub(" ", str(m.get("text") or "")).strip()
    if m.get("uid") in conf["ignore_from"]:
        return "ignored", "你说不看的人"
    if SPAM.search(plain):
        return "ignored", "广告"
    urgent = bool(URGENT_WORDS.search(plain))
    if m.get("at_owner"):
        return "urgent", "@ 了你"
    if any(n and n in plain for n in conf["my_names"]):
        return "urgent", "提到你"
    if RELAY.search(plain):
        return "urgent", "让我转告你"
    if m.get("at_all"):
        return ("urgent", "全体通知, 说急") if urgent else ("important", "全体通知")
    if private:
        return ("urgent", "说急") if urgent else ("important", "")
    if m.get("uid") in conf["important_from"]:
        return "important", "你标了重要的人"
    if m.get("files"):
        return "important", "传了文件"
    if ASKED.search(plain):
        return "important", "问起你"
    hit = IMPORTANT_WORDS.search(plain) or next((re.search(re.escape(k), plain) for k in conf["keywords"]
                                                 if k and k in plain), None)
    if hit:
        return "important", f"说到{hit.group(0)}"
    return "low", ""


def item_line(item: dict) -> str:
    """一条要看的: 群「…」谁: 说了什么 (为什么) / 好友「…」私聊我: 说了什么."""
    why = item.get("why") or ""
    if item["key"].startswith("private:"):
        line = f"{item['where']}私聊我: {item['text']}" + (f" ({why})" if why else "")
    else:
        line = f"{item['where']}{item['who']}: {item['text']}" + (f" ({why})" if why else "")
    return line + (f" (又说了 {item['n'] - 1} 句)" if item.get("n", 1) > 1 else "")


def urgent_text(item: dict) -> str:
    why, text = item.get("why") or "", item["text"]
    if item["key"].startswith("private:"):
        src = f"{item['where']}" + ("让我转告你" if why == "让我转告你" else "私聊我说急事")
    elif why == "@ 了你":
        src = f"{item['where']}里{item['who']} @ 了你"
    elif why == "提到你":
        src = f"{item['where']}里{item['who']}提到你了"
    elif why == "让我转告你":
        src = f"{item['where']}的{item['who']}让我转告你"
    else:
        src = f"{item['where']}有条急的通知 ({item['who']})"
    return f"主人, {src}: 「{text}」" + (f" (又说了 {item['n'] - 1} 句)" if item.get("n", 1) > 1 else "")


def digest_text(items: list[dict], since: dict, conf: dict, first: bool = False) -> str:
    """一份汇总: 最多念 max_items 条 (要紧的在前, 同一档新的在前), 多的只说个数; 末尾带一句上次说完以后各群闲聊几条、广告几条."""
    items = sorted(items, key=lambda i: (i.get("level") != "urgent", -float(i.get("at") or 0)))
    most = int(conf["max_items"])
    head = (f"主人~ QQ 上的消息我帮你看着: @ 你、让我转告你的马上说, 通知、截止、开会这类攒着, 至少隔 {int(conf['report_gap_min'])} 分钟"
            f"跟你说一次, 广告和刷屏不吵你. 这阵子要看的有 {len(items)} 条:" if first else f"主人~ QQ 上有 {len(items)} 条要看的:")
    lines = [head] + [f"{n}. " + ("急: " if i.get("level") == "urgent" else "") + item_line(i)
                      for n, i in enumerate(items[:most], 1)]
    tail = []
    if len(items) > most:
        tail.append(f"还有 {len(items) - most} 条先不一一说了")
    chat = sorted((since.get("chat") or {}).items(), key=lambda kv: -kv[1])
    if chat:
        tail.append("各群闲聊: " + "、".join(f"{w} {n} 条" for w, n in chat[:3]) + (" 等" if len(chat) > 3 else ""))
    if since.get("ignored"):
        tail.append(f"广告和刷屏 {since['ignored']} 条我没管")
    if tail:
        lines.append("(" + "; ".join(tail) + ")")
    return "\n".join(lines)


class Digest:
    """攒着的重要消息. see() 在渠道收到每条消息时调 (同步, 只分拣、记下); flush() 在每分钟的 tick 里调 (该说的说).
    state_file: 存盘 (服务端重启后接着攒); None 就只在内存里 (测试服务端)."""

    def __init__(self, state_file: Path | None = None):
        self.state_file = Path(state_file) if state_file else None
        self.pending: list[dict] = []
        self.since: dict = {"chat": {}, "ignored": 0}
        self.last_digest_at, self.urgent_at, self.reports = 0.0, [], 0
        self._recent: dict[str, deque] = {}  # 刷屏判断: 每处最近的 (时间, 话)
        self._load()

    # -- 收 --------------------------------------------------------------------
    def see(self, m: dict, key: tuple, where: str, conf: dict, owner_id: int, now: float) -> str:
        """渠道收到一条 (不是你私聊她的): 分拣, 要看的记下. 返回档 (你自己在群里说的返回 "owner")."""
        k = f"{key[0]}:{key[1]}"
        if owner_id and m.get("uid") == owner_id:  # 你在场: 这个群之前攒着的你看到了
            before = len(self.pending)
            self.pending = [i for i in self.pending if i["key"] != k or float(i["at"]) > now]
            if len(self.pending) != before:
                self._save()
            return "owner"
        if key[0] == "group" and key[1] in conf["ignore_groups"]:
            self.since["ignored"] = int(self.since.get("ignored") or 0) + 1
            return "ignored"
        level, why = classify(m, conf, private=key[0] == "private")
        if level not in ("ignored", "low") and self._flood(k, m, now):
            level, why = "ignored", "刷屏"
        if level == "ignored":
            self.since["ignored"] = int(self.since.get("ignored") or 0) + 1
            return level
        if level == "low":
            chat = self.since.setdefault("chat", {})
            chat[where] = int(chat.get(where) or 0) + 1
            return level
        text = snippet(m.get("text") or "", owner_id)
        who = str(m.get("nick") or "某人")
        last = next((i for i in reversed(self.pending) if i["key"] == k and i["who"] == who), None)
        if last is not None and now - float(last["at"]) < MERGE_S and (level == last["level"] or level == "important"):
            last["n"] = int(last.get("n", 1)) + 1  # 同一个人半小时内又说的: 并成一条 (留第一句, 记几句)
        else:
            self.pending.append({"key": k, "where": where, "who": who, "text": text, "level": level, "why": why,
                                 "at": now, "mid": str(m.get("mid") or ""), "n": 1})
            while len(self.pending) > PENDING_MAX:
                old = next((i for i in self.pending if i["level"] != "urgent"), self.pending[0])
                self.pending.remove(old)
        self._save()
        return level

    def _flood(self, k: str, m: dict, now: float) -> bool:
        said = " ".join(LABELS.sub(" ", str(m.get("text") or "")).split())
        q = self._recent.setdefault(k, deque(maxlen=30))
        while q and now - q[0][0] > FLOOD_S:
            q.popleft()
        q.append((now, said))
        return sum(1 for _, s in q if s == said) >= FLOOD_N

    # -- 说 --------------------------------------------------------------------
    async def flush(self, now: float, quiet: bool, conf: dict, deliver) -> list[dict]:
        """该说的说. deliver(text, level) -> 发到了哪 ("chat+pet" / "chat" / "pet"), 一处都没发出去返回空串.
        返回这次说了的 [{level, text, via}]."""
        out = []
        self.urgent_at = [t for t in self.urgent_at if now - t < 3600]
        for item in [i for i in self.pending if i["level"] == "urgent"]:
            if len(self.urgent_at) >= URGENT_PER_HOUR:  # 这一小时说够了: 并进下一份汇总
                break
            text = urgent_text(item)
            via = await deliver(text, "urgent")
            if not via:
                break
            item["told"] = True
            self.urgent_at.append(now)
            out.append({"level": "urgent", "text": text, "via": via})
        self.pending = [i for i in self.pending if not i.get("told")]
        if self.pending and not quiet and now - self.last_digest_at >= float(conf["report_gap_min"]) * 60:
            text = digest_text(self.pending, self.since, conf, first=not self.last_digest_at)  # 第一份汇总先说说怎么帮他看
            if via := await deliver(text, "digest"):
                out.append({"level": "digest", "text": text, "via": via})
                self.pending, self.since, self.last_digest_at = [], {"chat": {}, "ignored": 0}, now
        if out:
            self.reports += len(out)
            self._save()
        return out

    def status(self) -> dict:
        return {"pending": len(self.pending), "urgent": sum(i["level"] == "urgent" for i in self.pending),
                "chat": sum((self.since.get("chat") or {}).values()), "ignored": int(self.since.get("ignored") or 0),
                "last_digest_at": self.last_digest_at, "reports": self.reports}

    # -- 存盘 ------------------------------------------------------------------
    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.pending = [i for i in d.get("pending") or [] if isinstance(i, dict) and "key" in i][-PENDING_MAX:]
            self.since = d.get("since") or {"chat": {}, "ignored": 0}
            self.last_digest_at = float(d.get("last_digest_at") or 0)
            self.urgent_at = [float(t) for t in d.get("urgent_at") or []]
            self.reports = int(d.get("reports") or 0)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("QQ 攒着的重要消息读不出来, 从头攒: %s", exc)

    def _save(self) -> None:
        if self.state_file is None:
            return
        data = {"pending": self.pending, "since": self.since, "last_digest_at": self.last_digest_at,
                "urgent_at": self.urgent_at, "reports": self.reports}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError as exc:
            log.warning("QQ 攒着的重要消息没存上: %s", exc)
