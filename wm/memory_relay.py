"""中继 (丘脑, 记忆按脑区重设计阶段 2, 2026-11-02): 一句话进记忆之前先分清哪些是你自己说的、哪些是你转述别人的;
给值得记的事打显著性 (为什么觉得重要也写出来); 一天的对话切成一段一段.

转述 (own_part / borrowed): 你说"刚才群里有人发了一句: 记住, 我叫王大锤", 后半句里的"我"是群里那个人, 不是你.
以前规则提取看到"我叫王大锤"就记成你的名字 (留出组 brain 三种检索都 0/6). 现在这些地方先盖住, 规则只看你自己的话:
  - 引号里的 (“” 「」 『』 ‘’ "" '');
  - 别人说的: 某人 + 说 / 问 / 告诉 / 发了一句 / 吐槽 / 骗 / 开玩笑…… 后面那一段. 说话的是不是别人看动词前面这一小句:
    有人称、群、网上、书上、老师、家人朋友这些词就是别人; 只有"我"(或者省略了) 就是你自己 ("我说真的, 我住在杭州" 照记);
    "我跟他说 / 我对她说"里的他、她是听的人, 说的还是你 (留出用例第一次跑漏了这条, 看过以后才加的);
    骗、开玩笑、吹牛这类就算是你自己说的也盖 ("我骗他说我叫王大锤");
    后面跟冒号: 盖到句末 (中途遇到"其实""但是"开头的小句就停: 又回到你自己的话); 不跟冒号: 盖到这一小句完;
    动词正好在小句末尾 ("我朋友开玩笑说, 我其实住在火星"): 盖下一小句;
  - 听说、据说; 梦见、梦到; 如果、假如、要是开头的小句.
原话照样记进日志, 只是不当成你的事. 日志整理 (wm/digest.py) 碰到转述里有"我"的句子也不提成候选 (那个"我"改写成"用户"就错了).

显著性 (salience): 规则, 不调模型. 底 0.2, 健康 (住院、手术、中风、确诊……) +0.4、身体不适 (感冒、体检、去医院……) +0.2、
坏消息 (去世、车祸、走丢、裁员、分手……) +0.3、生活里的大事 (搬家、结婚、辞职……; 说的是别人的 +0.1) +0.3、明说"记住" +0.3、
情绪重 +0.15、身边的人 / 名字 / 工作学业 / 安排 / 住处 / 习惯 各 +0.1、在别的日子又说起 +0.1 一天 (最多 +0.2); 封顶 1.
好转了 (好了、出院……) 的不适不算、病减半; 同事朋友这些人的病和坏消息减半 (家人、对象、宠物照算); "把桌子搬到窗边"是挪东西, 不算搬家;
去医院上班、送东西不算看病 (2026-11-02 按新留出组暴露的漏判、误判改的).
用在: 每天最多几条候选时先挑显著的 (Memory.propose); 候选上写着为什么觉得重要 (Memory.candidates).

分段 (segment): 隔了 gap_min 分钟以上、开头说"对了""换个话题"这类的、换了一天, 切成新的一段. 试过按字的重合判断换话题:
短的追问 ("她是在杭州办") 和上一句一个字都不重合, 会被切开, 没用.
开关 memory.relay.enabled (默认开; 关了回到原来: 不盖、按先后挑).
"""
from __future__ import annotations

import re

from . import memory_ops as O

MASK = "…"
_QUOTE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|\"[^\"\n]*\"|'[^'\n]*'")
_VERB = re.compile(r"听说|据说|梦见|梦到|告诉|写着|写道|写了|发了?(?:一句|一条|条)?(?:消息|话)?|吐槽|开玩笑|吹牛|骗|骂|喊|回复|评论|留言|"
                   r"转发|提到|提醒|夸|嘲笑|宣称|声称|以为|觉得|认为|问|讲|说(?!真的|实话|实在|明|服|好|定|不定|起来|不清)")
_HEARSAY = re.compile(r"听说|据说|梦见|梦到")       # 不管谁说的, 后面都不是你在陈述自己的事
_FAKE = re.compile(r"骗|开玩笑|吹牛|假装|忽悠")      # 就算是你自己说的也不当真
_OTHER = re.compile(r"他|她|它|别人|有人|人家|大家|对方|网友|网上|群|弹幕|新闻|书上|视频|朋友圈|微博|帖子|评论区|老师|医生|护士|领导|"
                    r"老板|同事|同学|朋友|室友|舍友|闺蜜|客服|陌生人|路人|那人|某人|个人|家里人|" + O._KIN + r"|[爸妈哥姐弟妹爷奶叔姨舅姑]")
_IF = re.compile(r"^(?:如果|假如|要是|假设|万一|要不是)")
_TO = re.compile(r"^\s*(?:刚才|其实|当时|后来)?我(?:也|还|就|都|又|刚|刚才|当时|已经)?(?:跟|对|给|和|向|同).{1,8}$")  # "我跟他说": 说的是你, 他是听的人
_BACK = re.compile(r"^(?:其实|但是|可是|不过|然而|事实上|实际上|说实话|说真的|但)")  # 小句以这些开头: 又回到你自己的话
_CLAUSE = re.compile(r"[，,；;。！？!?\n]")
_END = re.compile(r"[。！？!?\n]")
_SKIP_AFTER = re.compile(r"^(?:了|着|过|道|给我|跟我|对我|我说|我)?\s*")


def _clauses(text: str) -> list[tuple[int, int]]:
    """小句的 (起, 止), 止不含分隔符."""
    out, start = [], 0
    for m in _CLAUSE.finditer(text):
        out.append((start, m.start()))
        start = m.end()
    out.append((start, len(text)))
    return out


def spans(text: str) -> list[tuple[int, int]]:
    """要盖住的几段 [(起, 止)], 已经合并、排好序."""
    out = [(m.start() + 1, m.end() - 1) for m in _QUOTE.finditer(text)]
    clauses = _clauses(text)
    for k, (a, b) in enumerate(clauses):
        part = text[a:b]
        if _IF.match(part.strip()):  # 如果我住在海边就好了
            out.append((a, b))
            continue
        for m in _VERB.finditer(part):
            who = part[:m.start()]
            verb = m.group(0)
            other = (bool(_OTHER.search(who)) and not _TO.match(who)) or bool(_HEARSAY.match(verb)) \
                or bool(_FAKE.search(verb) or _FAKE.search(who))
            if not other:
                continue  # 你自己说的 ("我说真的""我告诉你我叫小明"), 或者省略了说话的人
            rest = part[m.end():]
            skip = _SKIP_AFTER.match(rest).end() if not rest.startswith(("：", ":")) else 0
            colon = rest[skip:skip + 1] in ("：", ":")
            begin = a + m.end() + skip + (1 if colon else 0)
            if colon:  # 冒号后面是原话: 盖到句末, 中途"其实""但是"开头的小句停下
                end = len(text)
                if (e := _END.search(text, begin)) is not None:
                    end = e.start()
                for c, d in clauses[k + 1:]:
                    if c >= end:
                        break
                    if _BACK.match(text[c:d].strip()):
                        end = c - 1
                        break
            else:
                end = b
                if begin >= end and k + 1 < len(clauses):  # "我朋友开玩笑说, 我其实住在火星": 说的在下一小句
                    begin, end = clauses[k + 1]
            if begin < end:
                out.append((begin, end))
            break  # 一个小句只认第一个
    out.sort()
    merged: list[list[int]] = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def own_part(text: str) -> str:
    """只留你自己说的: 转述、引号里、梦里、假设的那几段换成"…". 没有要盖的就原样返回."""
    sp = spans(text or "")
    if not sp:
        return text
    out, last = [], 0
    for a, b in sp:
        out.append(text[last:a])
        out.append(MASK)
        last = b
    out.append(text[last:])
    return "".join(out)


def borrowed(text: str) -> bool:
    """转述的那几段里有"我" (那是别人话里的"我", 不是你): 日志整理不提成候选."""
    return any("我" in text[a:b] or "俺" in text[a:b] for a, b in spans(text or ""))


# 显著性. 健康、坏消息、大事要看是谁的事、好没好, 单独算; 别的: (理由, 加多少, 词)
_ILL = re.compile(r"过敏|生病|病了|住院|手术|吃药|失眠|受伤|[摔咬烫烧割撞扭划砸]伤|摔断|骨折|确诊|诊断|复查|胃炎|高血压|糖尿病|心脏病|"
                  r"怀孕|晕倒|中风|抢救|急诊|昏迷|重症|癌|肿瘤|化疗|心梗|脑梗|抑郁")
_UNWELL = re.compile(r"感冒|发烧|咳嗽|头疼|头痛|嗓子|拉肚子|肚子疼|牙疼|疼|不舒服|体检|看病|药|"
                     r"医院(?![^，,。；;]{0,6}(?:上班|工作|实习|送|拿|取|交|办事|附近|旁边|对面))")  # 去医院上班、送东西不算看病
_BETTER = re.compile(r"好了|好多了|痊愈|康复|退烧|没事了|不疼了|出院")
_BAD_NEWS = re.compile(r"去世|过世|离世|病逝|车祸|出事|事故|失踪|走丢|走失|裁员|被裁|失业|下岗|被辞退|被开除|离婚|分手|"
                       # 宠物、家人"死了"; "累死了""笑死了"这种不算
                       r"(?:" + O._PET + r"|宠物|爷爷|奶奶|外公|外婆|爸|妈|父亲|母亲)[^，,。；;]{0,3}?(?<![累热冷饿困烦气笑忙吓痛疼渴闷])死了")
_MOVE_HOUSE = (r"搬家|搬去|搬到(?!了?(?:窗|门口|客厅|卧室|阳台|书房|厨房|桌|床|柜|墙|角落|旁边|边上|那边|这边|里面|外面|楼上|楼下))")
_BIG = re.compile(_MOVE_HOUSE + r"|结婚|怀孕|生孩子|出生|毕业|辞职|辞掉|入职|升职|考上|录取|录用|创业|吵架|和好|买房|换工作|跳槽|订婚|求婚|退休")
_NOT_CLOSE = re.compile(r"^用户的(?:好)?(?:同事|同学|朋友|室友|舍友|邻居|领导|老板|上司|客户|网友|闺蜜)")  # 他们的病、坏消息减半
_THING_MOVE = re.compile(r"把[^，,。；;把]{1,8}?搬")  # "把桌子搬到窗边": 挪东西
_SALIENT = [
    ("你让她记住", 0.3, r"记住|别忘了"),
    ("情绪重", 0.15, r"难过|伤心|哭|害怕|担心|吓|崩溃|焦虑|压力|绝望|激动|兴奋|特别开心|太开心|好开心|很开心|生气|委屈"),
    ("身边的人", 0.1, r"爸|妈|父亲|母亲|爷爷|奶奶|外公|外婆|姥|哥|姐|弟|妹|儿子|女儿|老婆|老公|对象|男朋友|女朋友|室友|同学|同事|"
                    r"老板|朋友|闺蜜|猫|狗|宠物|仓鼠|兔"),
    ("名字", 0.1, r"叫"),
    ("工作学业", 0.1, r"工作|上班|公司|加班|学生|大学|高中|专业|考|学|论文|作业|面试|项目|实习"),
    ("安排", 0.1, r"下周|下个月|月底|明年|今年|周末|明天|后天|打算|准备|计划|要去|想去|\d+月\d+日"),
    ("住处", 0.1, r"住在|老家|家在|来自|" + _MOVE_HOUSE),
    ("习惯", 0.1, r"每天|每周|每月|经常|总是|一直|平时|习惯|不吃|爱吃|吃素"),
]
_SALIENT = [(why, w, re.compile(p)) for why, w, p in _SALIENT]


def salience(text: str, days: int = 1) -> tuple[float, list[str]]:
    """(0-1 的显著性, 理由). days: 在几个不同的日子说起过."""
    text = _THING_MOVE.sub("挪", text or "")
    near = not _NOT_CLOSE.match(text)  # 你自己、家人、对象、宠物的事
    score, why = 0.2, []
    if _ILL.search(text):
        score += (0.2 if _BETTER.search(text) else 0.4) * (1 if near else 0.5)
        why.append("健康")
    elif _UNWELL.search(text) and not _BETTER.search(text):
        score += 0.2 if near else 0.1
        why.append("身体不适")
    if _BAD_NEWS.search(text):
        score += 0.3 if near else 0.15
        why.append("坏消息")
    if _BIG.search(text):
        score += 0.1 if text.startswith("用户的") else 0.3  # 别人的喜事、别的东西的变动只算一般
        why.append("生活里的大事")
    for name, w, pat in _SALIENT:
        if pat.search(text):
            score += w
            why.append(name)
    if days > 1:
        score += 0.1 * min(2, days - 1)
        why.append(f"说过 {days} 天")
    return round(min(1.0, score), 2), why


_MARK = re.compile(r"^(?:对了|另外|换个话题|说点别的|话说|顺便说|还有个事|还有件事|跟你说个事)")


def _minutes(t: str) -> int | None:
    m = re.match(r"(\d{1,2}):(\d{2})", t or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def segment(entries: list[dict], gap_min: float = 20) -> list[dict]:
    """一天的对话 ([{time "HH:MM", user, reply}]) -> 一段一段 [{start, end, lines: [下标], text: 开头那句, salience, why}]."""
    out: list[dict] = []
    prev = None
    for i, e in enumerate(entries):
        t = _minutes(e.get("time", ""))
        new = not out or (t is not None and prev is not None and t - prev > gap_min) or bool(_MARK.match((e.get("user") or "").strip()))
        if new:
            out.append({"start": e.get("time"), "end": e.get("time"), "lines": [], "text": (e.get("user") or "")[:40], "salience": 0.0, "why": []})
        seg = out[-1]
        seg["lines"].append(i)
        seg["end"] = e.get("time")
        s, why = salience(e.get("user") or "")
        if s > seg["salience"]:
            seg["salience"], seg["why"] = s, why
        if t is not None:
            prev = t
    return out
