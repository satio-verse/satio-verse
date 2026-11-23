"""关系记忆层 (慢层): 一个 Obsidian 库里的 Markdown 文件, 人能直接读、改、删.

  <库>/<角色>/
    README.md          这个文件夹的说明 (第一次运行时生成)
    关系.md            关系状态: frontmatter 里是初次见面、上次见面、见面次数、聊过几轮、用户名字、熟悉程度、
                       上次见面时用户的情绪、日志整理进度、小屋里的亲近分 (cared, 摸头、投喂这些加的); 正文可以随便写
    记忆/关于用户.md   提取出来的偏好和事实, 一条一行, 带日期和 #auto-memory 标签 (删掉一行就忘了)
    记忆/对话摘要.md   日志整理出的事实 (确认过的), 按日期分节, 每条带来源 (wm/digest.py)
    记忆/候选.md       日志整理出、还没经用户确认的事 (Obsidian 复选框; 勾上转正、删掉或划掉就不要; 不参与检索)
    记忆/.recall.json  事件记忆上次被想起的日期 (鲜活度用; 点开头, Obsidian 不显示)
    日记/YYYY-MM-DD.md 她写的日记 (桌宠翻开日记本时按当天日志写的几句; 是她的输出, 不参与检索)
    资料/<名字>.md     从桌面拖进小屋的文件 (用户给的资料, 当数据存, 参与检索; 日记本门控时只在翻开时取)
    日志/YYYY-MM-DD.md 每天的对话记录 (用户说了什么、角色怎么回的、做了什么动作), 夹着桌宠小屋里发生的事 ("小屋: 用户摸了摸绫音的头")

写: 每轮结束后在后台线程里追加 (不挡语音), 偏好和事实用规则提取 (我叫/我喜欢/我不喜欢/记住...), 重复的不再写.
防丢: 整份改写的文件先写同目录的临时文件、刷到盘上, 再 os.replace 换上去; 换之前把上一版留在 <角色>/.backup/ 下 (同一个文件只留最近一份).
    日志是追加写, 写完也刷到盘上; 崩溃、断电时最多丢最后一行, 不会把前面的冲掉. .backup/ 不参与检索, 旧事实不会又被想起来.
读: 整个库 (包括用户自己写的笔记; 不含 .obsidian 和 exclude 里的文件夹, 默认也不含日志) 按段落/条目切块,
    每块带上所在的标题, jieba 分词后做 BM25; 库文件的修改时间变了才重建索引. 每轮限时检索 (默认 80 ms), 超时就这一轮不带记忆.
    日志默认不参与检索: 这样在 Obsidian 里删掉一条事实, 角色就真的忘了 (日志只做存档和摘要的原料).
    防误命中: 两边都只是长词拆出的单字只算弱匹配, 不能单独构成命中; 问角色自己的事时 ("你喜欢什么") 谓语词不算;
    分数不到第一名 rel_ratio 的不要. 用户说的是做得到的动作指令 ("去窗户那边看看") 时不检索.
语义检索 (memory.embed, 见 wm/embed.py): 配了嵌入模型时改用向量检索 (余弦相似度过门槛, 问角色自己的事时不取),
    换个说法也能找到 ("我能吃坚果吗" -> "用户对花生过敏"). 向量按记忆内容缓存, 每轮只给新增的记忆算;
    模型载入和全库建索引在后台线程里做, 没算完、嵌入服务没开或出错时这一轮退回 BM25.
注入: 命中的几条拼成一行 "[记忆] ..." 接在用户原话后面、状态行前面 (和状态行一样不进前缀缓存).
    这次见面的第一轮还带上关系概况: 第几次见面、熟悉程度、隔了几天、上次见面时用户的情绪、最近聊到的一件事.
鲜活度 (memory.freshness): 记忆分长期和事件 (memory_ops.memory_kind; 行尾 #长期 / #事件 可以手动改).
    长期 (名字、住处、生日、喜好、习惯、用户手写的笔记) 不淡化; 事件按半衰期 (默认 30 天) 越久越难想起, 不删:
    BM25 分数乘 floor + (1-floor)*鲜活度, 向量门槛抬高 penalty*(1-鲜活度). 问得准的旧事照样想得起, 擦边的不再冒出来;
    用户在主动回想 ("之前""上次""还记得") 时这一轮不压低.
    真的带进提示词的事件记一笔"上次被想起" (检索时再巩固), 鲜活度从那天重新算. 一个月以前的事带上"(3个月前说的)".
    常提升长期 (freshness.promote_days): 同一件事在不同的日子提到或想起 (直接命中, 或日志整理发现又说到了) 够 3 天,
    对话摘要里那一行加上 #长期 #常提, 以后不淡化; 候选提到两天以上标 #常提, 仍要用户确认. 带具体日子的安排不升.
日记本门控 (memory.diary_gate, 桌宠小屋): off 不门控; slots 合着时只取 关于用户.md 和 关系.md (名字、喜好这些),
    对话摘要、资料、你的笔记只在翻开日记本时取; none 合着时什么都不取. recall(scope=...) 由 Session 按日记本开合传.
候选 (memory.confirm): 日志整理 (模型判断) 出来的事先进 记忆/候选.md, 用户确认了才进 对话摘要.md;
    规则提取的直接陈述 (我叫/我喜欢/记住...) 照旧直接记. mode=auto 回到整理完直接写入.
    QQ / 微信私聊里你自己打的字 (propose_said) 用同一套规则认, 但不管 mode 都只进候选 (行尾 #私聊), 确认后和当面说的一样进 关于用户.md.
"""
from __future__ import annotations

import hashlib
import os
import json
import logging
import math
import re
import threading
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta

from . import memory_graph as G
from . import memory_ops as O
from . import memory_relay as R
from . import memory_affect as A
from . import memory_strength as S
from . import memory_seed as MS
from pathlib import Path

log = logging.getLogger("wm.memory")

MEMORY_TAG = "[记忆]"
_STOP = set("的了呢吧啊呀吗嘛哦哈是我你他她它在和就也都还又很太这那么什么怎么一个有没不个要会能想让把给被说做去来到上下里中")
_PUNCT = re.compile(r"[\s\W_]+", re.U)

# 规则提取: (正则, 类别, 模板). 只认用户直接陈述的说法, 宁可漏记也不乱记.
_ADV = (r"(?:还|也|都|其实|一直|平时|现在|目前|已经|最近|刚|刚刚|又|本来|原来|从小|真的|就|早就|"
        r"上个月|上周|上礼拜|去年|今年|前几天|前阵子|昨天|前天){0,3}")
_HOME = r"(?:我们(?:全家|一家|家)?|我家|我)"  # 住处这两条: "我们全家搬到苏州了" 也是用户自己的住处
_RULES = [
    (re.compile(r"我(?:的名字)?叫([\u4e00-\u9fa5A-Za-z]{1,8})"), "名字", "用户叫{0}"),
    (re.compile(r"我" + _ADV + r"(?:很|最|特别|超级|超|挺|还挺|蛮)?喜欢([^，。！？,.!?]{1,14})"), "偏好", "用户喜欢{0}"),
    (re.compile(r"我" + _ADV + r"(?:不喜欢|讨厌|害怕|怕)([^，。！？,.!?]{1,14})"), "偏好", "用户不喜欢{0}"),
    (re.compile(r"我(?:的)?生日(?:是|在)?([^，。！？,.!?]{2,12})"), "事实", "用户生日是{0}"),
    (re.compile(_HOME + _ADV + r"(?:住在|家在)([^，。！？,.!?]{2,12})"), "事实", "用户住在{0}"),
    # 已经搬过去了才算换了住处 ("我下个月要搬到深圳" 是计划, 交给日志整理)
    (re.compile(_HOME + _ADV + r"(?:已经|刚|刚刚)?搬(?:到|去|来)了?([\u4e00-\u9fa5]{2,8}?)(?:了|啦|住)?(?=[，。！？,.!?]|$)"),
     "事实", "用户住在{0}"),
    (re.compile(r"我" + _ADV + r"(?:养了|家有|有)(一只|一条|两只|一个)?([^，。！？,.!?]{1,10}?(?:猫|狗|鸟|鱼|兔|仓鼠|宠物))"),
     "事实", "用户养了{0}{1}"),
    (re.compile(r"我" + _ADV + r"是(?:一名|一个|个)(?!属)([^，。！？,.!?]{1,10}?)了?(?=[，。！？,.!?]|$)"), "事实", "用户是{0}"),
    (re.compile(r"我" + _ADV + r"是(\d{2}|\d{4})年(?:生人|出生的?|生的|的人|的(?=[，。！？,.!?]|$))"), "事实", "用户是{0}年出生的"),
    (re.compile(r"我" + _ADV + r"(?:是)?(?:一个|个)?属([鼠牛虎兔龙蛇马羊猴鸡狗猪])(?:的人|的)?(?![一-龥])"),
     "事实", "用户属{0}"),
    (re.compile(r"(?:你要|请你)?记住[，,: ：]?([^。！？!?]{2,30})"), "事实", "{0}"),
]
# 问句里的"我喜欢吃什么"不是事实: 值里有疑问词, 或这一段是在问角色记不记得, 都不记
_QUESTION = re.compile(r"什么|啥|吗|呢|哪|谁|怎么|(?<![前这那好])几(?![天年个周次])|多少|是不是|记得|[?？]")  # "前几天"不是问句


_BARE = re.compile(r"^((?:所以|而且|但是|但|可是|不过|现在|已经|其实|也|还|都|就)*)"
                   r"(?=(?:不喜欢|讨厌|害怕|最喜欢|很喜欢|特别喜欢|喜欢|住在|搬到|搬去|养了|属[鼠牛虎兔龙蛇马羊猴鸡狗猪]))")
_BIRTHDAY = re.compile(r"((?:农历|阴历|公历|阳历)?(?:\d{1,2}|[一二三四五六七八九十冬腊正]{1,2})月"
                       r"(?:初[一二三四五六七八九十]|二十[一二三四五六七八九]?|廿[一二三四五六七八九]|三十|十[一二三四五六七八九]?|\d{1,2}[日号]?))")
_OTHER = re.compile(r"(?:他|她|它|你|大家|别人|" + O._KIN + r")")
_THEIRS = re.compile(r"我的|我(?:" + O._KIN + "|" + O._PET + r")")


def carry_subject(text: str) -> str:
    """汉语常省略接着说的主语: "我最近在减肥，不喜欢吃甜食了" 的后一小句主语还是"我".
    前面的小句以"我"开头、这一小句直接以喜欢/住在/养了这类谓语开头时补上"我"; 中间换了别的主语 (他/妹妹...) 就不补."""
    parts, mine = re.split(r"([，,；;])", text), False
    for i, p in enumerate(parts):
        if i % 2:
            continue
        if p.startswith("我"):
            mine = not _THEIRS.match(p)  # "我妹妹在读大三，不喜欢早起" 说的是妹妹
        elif mine and _BARE.match(p):
            parts[i] = _BARE.sub(r"\1我", p, count=1)
        elif _OTHER.match(p):
            mine = False
    return "".join(parts)


# 第二组规则 (2026-11-18 提取覆盖专项): 日程/职业/关系/身体/习惯 这几类以前一条都抽不出来,
# 量出来 33 句只中 8 句 (24%). 误抽比漏抽更糟 —— 会把她没说过的事写进记忆, 所以每条都要求
# "我"开头 + 明确的谓语, 不猜.
_RULES_2 = (
    # 日程: 带日期/周期的事 (这些同时喂"日程清单"和"改期"判定)
    (re.compile(r"(我(?:每周|每天|每月|每年)[^，。？!！；;]{2,14})"), "事实", "{0}"),
    (re.compile(r"(我(?:今天|明天|后天|大后天|下周[一二三四五六日天]?|下个?月|"
                r"\d{1,2}月\d{1,2}[日号])(?:要|得|需要|打算)(?:去)?[^，。？!！；;]{1,12})"), "事实", "{0}"),
    # 职业
    (re.compile(r"我(?:是)?做([^，。？!！；;]{2,10})的"), "事实", "用户是做{0}的"),
    (re.compile(r"我转岗做([^，。？!！；;]{2,10})了?"), "事实", "用户转岗做{0}"),
    (re.compile(r"我在([^，。？!！；;]{2,14})上班"), "事实", "用户在{0}上班"),
    (re.compile(r"(我(?:刚|最近)换了工作)"), "事实", "{0}"),
    # 家人与关系
    (re.compile(r"我有个([^，。？!！；;]{2,6})"), "事实", "用户有个{0}"),
    (re.compile(r"我(妈|爸|妈妈|爸爸|女朋友|男朋友|妹妹|姐姐|弟弟|哥哥)(?:是|在做)([^，。？!！；;]{2,10})"),
     "事实", "用户的{0}是{1}"),
    (re.compile(r"我(妈|爸|妈妈|爸爸|女朋友|男朋友|妹妹|姐姐|弟弟|哥哥)(身体[^，。？!！；;]{1,6})"),
     "事实", "用户的{0}{1}"),
    (re.compile(r"我(?:跟|和)(我爸|我妈|我妹|我哥|我姐|我爸我妈)[^，。？!！；;]{0,6}"
                r"(关系一般|关系很好|不太熟|闹翻了)"), "事实", "用户跟{0}{1}"),
    # 身体与属性
    (re.compile(r"我今年(\d{1,2})岁"), "事实", "用户今年{0}岁"),
    (re.compile(r"我身高(\d{2,3})"), "事实", "用户身高{0}"),
    (re.compile(r"我对([^，。？!！；;]{1,8})过敏"), "事实", "用户对{0}过敏"),
    (re.compile(r"我睡眠([^，。？!！；;]{1,8})"), "事实", "用户睡眠{0}"),
    (re.compile(r"我(?:考研考的是|学的是)([^，。？!！；;]{2,10})"), "事实", "用户学的是{0}"),
    # 习惯与口味
    (re.compile(r"我(?:最近在学|最近在练)([^，。？!！；;]{1,8})"), "事实", "用户最近在学{0}"),
    (re.compile(r"我每天([^，。？!！；;]{2,12})"), "事实", "用户每天{0}"),
    (re.compile(r"我不吃([^，。？!！；;]{1,6})"), "偏好", "用户不吃{0}"),
    (re.compile(r"我现在(?:爱|喜欢)喝([^，。？!！；;]{1,8})了?"), "偏好", "用户现在爱喝{0}"),
)


def _rules2_on() -> bool:
    """新六类提取规则 (2026-11-18: 日程/职业/关系/身体/习惯/口味).

    默认关: 关着的时候 extract_facts 和改之前逐字节一样, 一条测试都不动.
    要开: 环境变量 WM_EXTRACT_RULES2=on (或 1/true).
    为什么做成开关: 规则一开, 库里事实变多, 一批按旧覆盖率写的断言会集体失真 ——
    所以先合进主干、默认不生效, 再一条一条放开, 不要一次改六条测试.
    """
    return os.environ.get("WM_EXTRACT_RULES2", "off").strip().lower() in ("on", "1", "true")


def extract_facts(text: str) -> list[tuple[str, str]]:
    """用户的一句话 -> [(类别, 一句话事实)]."""
    text = carry_subject(text)
    out = []
    for pat, kind, tpl in (*_RULES, *(_RULES_2 if _rules2_on() else ())):
        for m in pat.finditer(text):
            groups = [(g or "").strip(" ，,。") for g in m.groups()]
            value = "".join(groups)
            if not value or _QUESTION.search(value) or re.fullmatch(r"(你|这个|那个|它|呀)", value):
                continue
            if kind != "名字" and _QUESTION.search(text[max(0, m.start() - 6):m.end() + 3]):
                continue
            fact = tpl.format(*groups)
            if tpl == "{0}":
                fact = re.sub(r"我(?!们)", "用户", fact)
            out.append((kind, fact))
    # 说了出生年的这句话里, 后面单独的"农历5月初九"就是生日 ("我是90年生人，农历5月初九，属马")
    if any(f.endswith("年出生的") for _, f in out) and not any(k == "事实" and f.startswith("用户生日") for k, f in out):
        m = _BIRTHDAY.search(text[text.find("年") + 1:])
        if m:
            out.append(("事实", f"用户生日是{m.group(1)}"))
    return out


def settle_fact(kind: str, fact: str, today: date) -> str:
    """规则提取出来的事实落笔前定下来: 相对的日子换成具体日期 (偏好不换: "喜欢周日去爬山" 是习惯, 不是哪一天),
    两位数的出生年补全 ("用户是90年出生的" -> 1990年)."""
    if kind != "偏好":
        fact = O.absolute_dates(fact, today)
    return re.sub(r"^用户是(\d{2})年", lambda m: f"用户是{O.full_year(m.group(1), today)}年", fact)


def near_dup(a: str, b: str) -> bool:
    """是不是同一件事的两种说法 (近义重复).

    用户拍板: 同一件事只留一条. "用户下周要去做胃镜" 和 "用户下周要去医院做胃镜"
    指的是同一件, 留两条会让情绪和强度被摊薄.
    判据刻意保守: 只认"短的一边几乎全被长的包含"(双子覆盖 >= 0.8) 且两边都以"用户"开头.
    宁可漏合并, 也不要把两件不同的事并掉 —— 并掉是静默丢记忆.
    """
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b or a == b:
        return False
    if not (a.startswith("用户") and b.startswith("用户")):
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 6:      # 太短的说法不判, 免得把"喜欢猫/喜欢狗"并掉
        return False
    it = iter(long)
    return all(ch in it for ch in short)   # 短的是长的子序列: 只是补了"医院"这类修饰


# 用户在问自己以前说过的事 (记忆里没有时提示模型别编): "你还记得我…吗"、"我喜欢什么…"、"我有没有…"
_ASK_PAST = re.compile(r"记得我|我(?:最|还|现在)?(?:是|叫|住|养|有没有|喜欢|讨厌|怕|的[\u4e00-\u9fff]{1,4}(?:是|叫)).{0,8}(?:什么|啥|哪|几|谁|吗|么)")
_ADVICE = re.compile(r"该|应该|要不要|能不能|可不可以|好不好|怎么办|建议|帮我想|推荐")  # 求建议、做决定, 交给模型
_ASK_DO = re.compile(r"干嘛|干啥|干什么")  # "我男朋友平时喜欢干嘛"
_ASKING = re.compile(r"什么|啥|哪|几|多大|多少|吗|么|怎么样|如何|是不是")
SUMMARY_FILE = "对话摘要.md"
CANDIDATE_FILE = "候选.md"
PRIVATE_TAG = "#私聊"  # 候选和记下的那行里标着: 是你在 QQ / 微信私聊里说的 (propose_said)
URGENT_TAG = "#要紧"  # 候选那行里标着: 这一天名额满了以后因为很要紧加塞进来的 (memory.relay.urgent_extra), 不占名额
RECALL_FILE = ".recall.json"
BACKUP_DIR = ".backup"  # 整份改写的文件在 <角色>/.backup/ 留一份上一版; 不是 .md, 不参与检索
_SOURCE = re.compile(r"\s*[（(](?:\d{4}-\d{2}-\d{2}|来源[:：][^)）]*)[)）]")
_CAND = re.compile(r"^- \[([ xX])\] (.+)$")  # 候选.md 的一行: "- [ ] 用户下周要去北京出差 (来源: [[2026-10-24]] 14:02) #候选"
_LINK_DAY = re.compile(r"\[\[(\d{4}-\d{2}-\d{2})\]\]")
_CAND_HEAD = ("# 候选记忆\n\n<!-- 日志整理出来、她觉得值得记的事, 还没经你确认, 不参与检索. 勾上 [x] 就转正 (移到 对话摘要.md); "
              "不要的整行删掉或划掉. 也可以在桌宠面板里确认. -->\n")
WATCH_FILE = "一起看过的视频.md"  # 陪看完记一节 (append_watch): 看了什么、她说的、你说的; 参与检索
_WATCH_HEAD = ("# 一起看过的视频\n\n<!-- 每次和你一起看完 (桌宠陪看: 你给的链接, 或者她看你的屏幕), 程序记一节: 看了什么、讲了什么、"
               "她说了什么、你当时说了什么. 参与检索 (日记本门控时只在翻开日记本时取); 删掉一节她就不记得那次了. -->\n")


_SUMMARY_HEAD = ("# 对话摘要\n\n<!-- 程序在空闲时用本机小模型把每天的对话日志整理成几条事实, 每条后面是来源 (点日期打开当天日志). "
                 "可以直接改、删; 整理过的日志不会再整理, 删掉的不会被写回来. -->\n")


def _candidate_texts(text: str) -> set[str]:
    """候选.md 里提过的每一条的正文: 等你确认的、你点过"不要"的 (划掉的那行) 都算, 同样的事不再提."""
    out = set()
    for line in text.splitlines():
        m = re.match(r"- (?:\[[ xX]\] )?(?:~~)?(.+?)(?:~~.*)?$", line.strip())
        if m and (fact := _clean_fact(m.group(1))):
            out.add(fact)
    return out


def candidate_id(text: str) -> str:
    """候选的编号: 事实正文的短哈希 (文件里行的顺序变了也不变)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _insert_section(text: str, day: str, lines: list[str]) -> str:
    """几行插到 "## 日期" 那一节的末尾; 没有这一节就在文件末尾新开一节."""
    if f"## {day}" not in text:
        return text.rstrip("\n") + f"\n\n## {day}\n" + "\n".join(lines) + "\n"
    head, sep, rest = text.partition(f"## {day}\n")
    nxt = rest.find("\n## ")
    sec, tail = (rest, "") if nxt < 0 else (rest[:nxt + 1], rest[nxt + 1:])
    return head + sep + sec.rstrip("\n") + "\n" + "\n".join(lines) + "\n" + ("\n" + tail if tail else "")


_SOURCE_TIMES = re.compile(r"[（(]来源[:：]([^)）]*)[)）]")


def _src_times(line: str) -> list[str]:
    """一行记忆的来源里写的时刻 ("(来源: [[2026-11-02]] 14:02, 14:05)" -> ["14:02", "14:05"]): 情绪事件按它连到日志整理的那条."""
    m = _SOURCE_TIMES.search(line)
    return re.findall(r"\d{1,2}:\d{2}", m.group(1)) if m else []


def _clean_fact(line: str) -> str:
    """去掉标签、记录日期和来源 (这些是给人在 Obsidian 里核对的, 不进提示词)."""
    return _SOURCE.sub("", re.sub(r"#auto-memory|#[^\s#]+", "", line)).strip()


# 关系状态: 用户情绪 (只看用户自己说的话, 角色的回复情绪不算) 和熟悉程度. 都是规则, 能在 关系.md 里看到结果
_LOW = re.compile(r"难过|伤心|不开心|不高兴|心情不好|烦|好累|太累|很累|累死|压力|焦虑|郁闷|失眠|难受|生病|想哭|哭了|孤单|孤独|"
                  r"没考好|考砸|失败|委屈|崩溃|丧")
_HIGH = re.compile(r"开心|高兴|快乐|太好了|好耶|哈哈|兴奋|考好了|通过了|成功了|顺利|幸福|激动")


def mood_of(text: str) -> str | None:
    """用户这句话的情绪: 低落 / 开心 / None. 问角色的 ("你累吗") 不算."""
    if "你" in text and not _ME.search(text):
        return None
    if _LOW.search(text):
        return "低落"
    if _HIGH.search(text):
        return "开心"
    return None


def _days_between(stamp: str | None, now: datetime) -> int | None:
    try:
        return (now.date() - datetime.strptime((stamp or "")[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return None


def familiarity(meta: dict, now: datetime) -> str:
    """按见面次数、聊过的轮数 (加上小屋里的亲近分 cared, 一分算一轮)、认识的天数分四档."""
    sessions, turns = int(meta.get("sessions") or 0), int(meta.get("turns") or 0) + int(meta.get("cared") or 0)
    days = _days_between(meta.get("first_met"), now) or 0
    if sessions <= 1 and turns == 0:
        return "第一次见面"
    if turns < 20 or sessions < 3:
        return "刚认识不久"
    if turns < 200 or days < 14:
        return "已经比较熟了"
    return "是老朋友了"


# 桌宠小屋里发生的事 (上行 room_event: room-life.js 里点她、投喂, 同一种事 30 秒内合成一条, count 是几次). 只认这几种;
# 日志、日记、提示词都用这里的说法, 页面发来的字符串只拿来查表. (种类, 部位或东西) -> (说法, 每条加几分亲近);
# 说法里的 {c} 日志里是角色名, 提示词里是"你", 日记里是"我"
ROOM_EVENTS = {
    ("poke", "head"): ("摸了摸{c}的头", 2),
    ("poke", "hand"): ("拉了拉{c}的手", 1),
    ("poke", "body"): ("戳了戳{c}", 1),
    ("feed", "drink"): ("给{c}带了饮料", 2),
    ("feed", "snack"): ("给{c}带了零食", 2),
    ("feed", "milk"): ("给{c}带了牛奶", 2),
    ("feed", "beans"): ("给{c}带了咖啡豆", 2),
}
TEASE = 5  # 一条里戳了这么多下是在逗她 (页面上她会鼓起脸): 不加亲近分, 日记里写"一直戳我"
_ROOM_LINE = re.compile(r"- \d{2}:\d{2} 小屋: 用户(.+?)(?: ×(\d+))?$")  # 日志里的一行: "- 14:02 小屋: 用户摸了摸绫音的头 ×3"
_DIARY_OLD = re.compile(r"- \d{2}:\d{2} 今天你")  # 以前日记一次追加一行的格式
_DIARY_MINE = re.compile(r"- \d{2}:\d{2} ")
MINE_HEAD = "## 我自己的"  # 日记里她自己过日子、看完视频的想法
UNDERSTAND_FILE = "我的理解.md"  # 日记文件夹里, 她看视频、过日子慢慢想通、攒下来的理解 (她的输出, 不进检索)
UNDERSTAND_HEAD = "# 我的理解"
HOWTO_FILE = "操作笔记.md"  # 她操作电脑的经验: 做完一件事写一条, 下次类似的事翻出来 (她的输出, 不进检索)
HOWTO_HEAD = "# 操作笔记"
HOWTO_MAX = 200
HOWTO_ARCHIVE = "归档"  # 日记/归档/操作笔记-年份.md: 超出 HOWTO_MAX 的最早几条挪到这里 (不删; 不进检索, 也不再翻出来)
_UNDERSTAND_LINE = re.compile(r"- \([^)]*\)\s*(.+)$")  # "- (2026-10-25, 看视频) 正文"


def _norm_u(text: str) -> str:
    """去标点、空白, 只留汉字字母数字: 两句理解像不像按这个比."""
    return re.sub(r"[^0-9A-Za-z一-鿿]", "", str(text or ""))


def room_event_of(msg: dict) -> tuple[str, str, int] | None:
    """上行 room_event -> (种类, 部位或东西, 次数 1-99); 不认识的是 None. 次数不是整数时算 1."""
    kind = msg.get("kind")
    what = msg.get("part") if kind == "poke" else msg.get("item") if kind == "feed" else None
    if not isinstance(what, str) or (kind, what) not in ROOM_EVENTS:
        return None
    count = msg.get("count")
    count = max(1, min(99, count)) if isinstance(count, int) and not isinstance(count, bool) else 1
    return kind, what, count


def room_note(events: list[tuple[str, str]]) -> str | None:
    """刚才小屋里的几件事 [(种类, 部位或东西)] -> 状态行里的半句 "用户刚才摸了摸你的头、给你带了零食" (同一件只说一次, 最多三件)."""
    said = list(dict.fromkeys(ROOM_EVENTS[e][0].format(c="你") for e in events if e in ROOM_EVENTS))[-3:]
    return "用户刚才" + "、".join(said) if said else None


def _room_diary(events: list[tuple[str, str, int]]) -> list[str]:
    """当天小屋里的事 -> 日记里的几个说法, 按 摸头、拉手、戳、带东西 排: ["摸了好几次我的头", "给我带了零食和牛奶"]."""
    total, peak, items = Counter(), Counter(), []
    for kind, what, n in events:
        if kind == "feed":
            item = ROOM_EVENTS[(kind, what)][0].rpartition("带了")[2]
            if item not in items:
                items.append(item)
        else:
            total[what] += n
            peak[what] = max(peak[what], n)
    parts = []
    if total["head"]:
        parts.append("摸了好几次我的头" if total["head"] >= 3 else "摸了摸我的头")
    if total["hand"]:
        parts.append("拉了拉我的手")
    if total["body"]:
        parts.append("一直戳我" if peak["body"] >= TEASE else "戳了戳我")
    if items:
        parts.append("给我带了" + ("、".join(items[:-1]) + "和" if len(items) > 1 else "") + items[-1])
    return parts


_CJK = re.compile(r"[一-鿿]")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# 问角色自己的事 ("你喜欢什么颜色"、"你住哪") 时, 这些谓语词不算命中: 否则会捞出"用户喜欢…""用户住在…"
_PRED = set("喜欢叫住家养用户") | {"喜欢", "不喜欢", "讨厌", "害怕", "名字", "生日", "住在", "养了", "家有", "用户"}
_ME = re.compile(r"我|咱|俺")
WEAK = 0.3  # 两边都只是长词拆出来的单字 ("天气"的"天"对"下雨天"的"天"), 只算弱匹配: 能加分, 不能单独构成命中


def split_tokens(text: str) -> tuple[list[str], list[str]]:
    """jieba 搜索模式分词 -> (词, 从多字词里拆出来的单字). 常用虚字去掉."""
    import jieba

    words = [w for w in jieba.lcut_for_search(_PUNCT.sub(" ", text)) if w.strip() and w not in _STOP]
    chars = [c for w in words if len(w) > 1 for c in w if _CJK.match(c) and c not in _STOP]
    return words, chars


def tokenize(text: str) -> list[str]:
    """词再补上单个汉字: "猫" 也能匹配到 "橘猫", 召回率高一些."""
    words, chars = split_tokens(text)
    return words + chars


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.7, tfs: list[Counter] | None = None):
        """tfs: 每篇事先数好的词频 (和 docs 对齐); Memory 在切块时数一次、跟着文件缓存, 写库后重建索引就不用再数."""
        self.k1, self.b = k1, b
        self.len = [len(d) for d in docs]
        self.avg = sum(self.len) / max(1, len(docs))
        self.post: dict[str, list[tuple[int, int]]] = {}  # 倒排表: 词 -> [(文档号, 词频)], 只算含这个词的文档
        for i, d in enumerate(docs):
            for w, f in (tfs[i] if tfs is not None else Counter(d)).items():
                self.post.setdefault(w, []).append((i, f))
        n = len(docs)
        self.idf = {w: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for w, p in self.post.items()}

    def terms(self, w: str):
        """这个词对每个含它的文档贡献的分数: [(文档号, 分数)]."""
        for i, f in self.post.get(w, ()):
            yield i, self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))


class Memory:
    def __init__(self, cfg: dict, *, _test_unlock_strength_on: bool = False):
        self.root = Path(cfg["vault"])
        self.char = cfg.get("character", "绫音")
        # 角色以前的名字 (2026-10-27 小流 -> 绫音): 库里只有旧名的文件夹时启动时搬过来; 旧日志里带旧名的行照样认
        self.old_chars = [x for x in cfg.get("renamed_from") or [] if x and x != self.char]
        self.top_k = int(cfg.get("top_k", 3))
        self.min_score = float(cfg.get("min_score", 0.5))
        self.include_logs = bool(cfg.get("include_logs", False))
        # 不参与检索的文件夹 (相对库根目录): 项目知识库这类资料放在同一个库里, 但不是"关于用户的记忆"
        self.exclude = tuple(x.strip("/") + "/" for x in cfg.get("exclude", ["WorldModel知识库"]) if x)
        self.rel_ratio = float(cfg.get("rel_ratio", 0.35))  # 分数不到第一名这个比例的不要
        self.dir = self.root / self.char
        self._lock = threading.Lock()
        self._sig = None
        self._cache: dict[str, tuple[int, list[dict]]] = {}  # 文件 -> (修改时间, 切块)
        self._chunks: list[dict] = []
        self._bm25: BM25 | None = None
        self._gap_days: int | None = None  # 这次见面距上次隔了几天
        self._mood = Counter()             # 这次见面里用户情绪的计数
        self.clock = datetime.now          # "现在" (测试和评测里可以换成固定时间)
        # 最近几轮想起了什么 (设置里的"记忆"页看: 你说了什么、带进了哪几条、分数和检索方式); 只在内存里, 重启就清
        self.recalls: deque = deque(maxlen=40)
        self.embedder = None               # 语义检索的嵌入模型 (start_embedder 在后台载入)
        self.embed_error: str | None = None
        self.forgotten: list[str] = []      # 最近一次按用户要求忘掉的事实 (日志里记一笔)
        self.min_sim: float | None = None
        self._vec: dict = {}               # 记忆内容 -> 向量
        self._vec_lock = threading.Lock()
        self._vec_job: threading.Thread | None = None
        fr = cfg.get("freshness") or {}
        self.half_life = float(fr.get("half_life_days", 30) or 0)  # 事件记忆的半衰期 (天); 0 不淡化
        self.bm25_floor = float(fr.get("bm25_floor", 0.5))         # BM25 分数最多打到这个折扣
        self.vec_penalty = float(fr.get("vector_penalty", 0.08))   # 向量门槛最多抬高这么多
        self.refresh_on_recall = bool(fr.get("refresh_on_recall", True))
        self.promote_days = int(fr.get("promote_days", 3) or 0)    # 事件在不同的日子提到或想起够这么多天就升成长期; 0 不升
        cf = cfg.get("confirm") or {}
        self.confirm = cf.get("mode", "candidates") != "auto"     # 日志整理出来的先进候选, 用户确认了才记
        self.daily_max = int(cf.get("daily_max", 8))               # 每天 (日志那天) 最多提几条候选
        self.care_daily = int(cfg.get("room_care_daily", 10))      # 小屋里的事每天最多加几分亲近 (room_event)
        self._recalled: dict[str, dict] | None = None              # 记忆正文 -> {"last", "days"} (记忆/.recall.json)
        self._recall_ver = 0
        self._fresh_key = None
        self._fresh: list[tuple[float, int]] = []                  # 和 _chunks 对齐: (鲜活度, 距今天数)
        self._cand_sig = None
        self._graph = G.Graph([])          # 记忆图谱 (人、物、地点), _refresh 时重建
        self._gents: dict[str, list[int]] = {}
        self._gents_by_i: list[set] = []
        self._index_key = self._notes_key = None  # 索引、图谱、图谱笔记是按哪份记忆文字建的 (没变就不重建)
        self._graph_dirty = False
        self.graph_notes = bool(cfg.get("graph_notes", True))  # 把图谱写成 Obsidian 笔记 (<角色>/图谱/), 给人看关系图
        self._notes_lock = threading.Lock()
        self._last_sims = self._last_bm25 = None  # 这一轮检索的分数 (实体回答给同一实体的记忆排序用)
        self._allow: set | None = None            # 这一轮能看到的切块 (日记本门控); None 是都能看
        self.listeners: list = []          # 候选变了时通知 (连接着的 Session; 在别的线程里调, 自己负责切回事件循环)
        # 中继 (wm/memory_relay.py, 记忆按脑区重设计阶段 2): 转述别人的话不当成你的事、候选先挑显著的; 关了回到原来
        self.relay = (cfg.get("relay") or {}).get("enabled", True) is not False
        # 名额满了以后, 显著性到 urgent_min 的 (很要紧的) 还能多进 urgent_extra 条, 标 #要紧、不占名额; 0 是关 (默认)
        self.urgent_extra = int((cfg.get("relay") or {}).get("urgent_extra", 0) or 0) if self.relay else 0
        self.urgent_min = float((cfg.get("relay") or {}).get("urgent_min", 0.6))
        # 出生种子 (wm/memory_seed.py, 阶段 3): memory.seed on / off, 单条停用 memory.seed_off
        self.seed_mode = str(cfg.get("seed", "on") or "on")
        self.seed_off = [str(x) for x in cfg.get("seed_off") or []]
        self._first_today = False  # 这次见面是不是今天头一回 (start_session 算; 种子"早上打招呼"用)
        # 情绪 (wm/memory_affect.py, 阶段 4): 你自己话里的情绪事件存在 记忆/.affect.jsonl; memory.affect.mode off / shadow / on
        self.affect = A.AffectStore(lambda: self.dir / "记忆" / A.FILE, cfg.get("affect") or {})
        # 强度和稳定性 (wm/memory_strength.py, 阶段 5): 旁路 记忆/.strength.json; memory.strength.mode off / shadow / on
        scfg = dict(cfg.get("strength") or {})
        if scfg.get("mode") == "on" and not _test_unlock_strength_on:
            # on 档在代码里挡死 (协调对话 11-15): 纠正的识别还不够准. 配置写 on 也按 shadow 跑; 要开只能改代码.
            # _test_unlock_strength_on 是只给测试 / 评测台用的构造参数, 生产代码 (engines.py / digest.py) 不传, 配置里够不着.
            scfg["mode"] = "shadow"
            log.warning("记忆强度: %s, 按 shadow 档运行", S.ON_BLOCKED)
        self.strength = S.StrengthStore(lambda: self.dir / "记忆" / S.FILE, lambda: self.dir / "记忆" / S.SHADOW_FILE, scfg)
        self._pending: dict | None = None  # 这一轮 recall 带进提示词的命中 (observe 里按"回复用上了没有"结算); 只有一个槽, 并发时只会漏算
        self._prev: dict | None = None     # 上一轮回复里用上了的 (这一句是纠正 / 肯定 / 没事, 在 observe 里结算)
        self._fresh_new: list = []         # shadow 档: 和 _chunks 对齐的新鲜度 (算了不用)
        self._cmp = None                   # shadow 档: 这一轮检索 (旧排序, 新排序) 的记忆正文
        self._sal_cache: dict = {}

    # ------------------------------------------------------------------ 库结构
    def _migrate_dir(self) -> None:
        """角色改了名 (renamed_from): 库里还没有新名字的文件夹、有旧名字的, 整个搬过来 (启动时做, 这时没人在写);
        程序写的说明里的名字跟着改, 日志、记忆的正文不改 (那是当时说的话). 两个都有时不合并, 只提醒;
        搬不动 (文件被占着) 就这次接着用旧文件夹, 下次启动再试, 不新建一个空的."""
        if self.dir.exists():
            for old in self.old_chars:
                if (self.root / old).is_dir():
                    log.warning("记忆库里 %s/ 和 %s/ 都在, 没有合并 (现在用 %s/)", old, self.char, self.char)
            return
        for old in self.old_chars:
            src = self.root / old
            if not src.is_dir():
                continue
            try:
                src.rename(self.dir)
            except OSError as err:
                log.warning("记忆文件夹 %s/ 改名成 %s/ 没成 (%s), 这次先用旧的", old, self.char, err)
                self.dir = src
                return
            log.info("记忆文件夹 %s/ 改名成 %s/", old, self.char)
            readme = self.dir / "README.md"
            if readme.exists():
                text = readme.read_text(encoding="utf-8")
                new = text.replace(f"# {old}的记忆", f"# {self.char}的记忆").replace(f"角色「{old}」", f"角色「{self.char}」")
                if new != text:
                    self._write(readme, new)
            return

    def init(self) -> None:
        """建文件夹和说明; 只新建不存在的文件, 不改用户已有的笔记. 预热 jieba 词典 (约 1 s)."""
        self._migrate_dir()
        (self.dir / "记忆").mkdir(parents=True, exist_ok=True)
        (self.dir / "日志").mkdir(parents=True, exist_ok=True)
        readme = self.dir / "README.md"
        if not readme.exists():
            self._write(readme, (
                f"# {self.char}的记忆\n\n这里是 WorldModel 角色「{self.char}」的长期记忆, 都是普通 Markdown, 可以直接改、删.\n\n"
                "- `关系.md`: 关系状态 (frontmatter 由程序维护, 正文随便写, 角色检索时也会读到)\n"
                "- `记忆/关于用户.md`: 程序从对话里提取的偏好和事实, 每行一条, 带 #auto-memory 标签; 删掉一行它就忘了, "
                "改错的内容它下一轮就按改后的记\n"
                "- `记忆/对话摘要.md`: 空闲时本机小模型把日志整理成的事实, 每条带来源 (点日期打开当天日志), 同样可改可删\n"
                "- `记忆/候选.md`: 整理出来、还没经你确认的事; 勾上就转正, 删掉就不要 (候选不参与检索)\n"
                "- 记忆分长期和事件: 事件越久越难想起 (不删), 行尾写 `#长期` 或 `#事件` 可以改\n"
                "- `日志/`: 每天的对话记录\n"
                "- `.backup/`: 程序每次整份改记忆前留的上一版, 可以手动拷回来\n\n"
                "库里其他地方你自己写的笔记, 角色检索时也会参考 "
                "(`WorldModel知识库/` 这类配置里排除的文件夹除外).\n"), backup=False)
        rel = self.dir / "关系.md"
        if not rel.exists():
            now = self.clock().strftime("%Y-%m-%d %H:%M")
            self._write(rel, f"---\nfirst_met: {now}\nlast_seen: {now}\nsessions: 0\nturns: 0\nuser_name: \n---\n\n"
                             "# 和用户的关系\n\n<!-- 这里可以写你希望角色记住的关系背景, 比如你们怎么认识的. -->\n",
                        backup=False)
        facts = self.dir / "记忆" / "关于用户.md"
        if not facts.exists():
            self._write(facts, "# 关于用户\n\n", backup=False)
        meta, body = self._relation()
        if "digested" not in meta:  # 第一次启用日志摘要: 从现在开始整理, 已有的旧日志不补 (python -m wm.digest 可以手动补)
            meta["digested"] = self._log_end() or f"{self.clock():%Y-%m-%d} 0"
            self._write_relation(meta, body)
        tokenize("预热")

    def _relation(self) -> tuple[dict, str]:
        text = (self.dir / "关系.md").read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
        if not m:
            return {}, text
        meta = {}
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        return meta, m.group(2)

    def _write_relation(self, meta: dict, body: str) -> None:
        head = "\n".join(f"{k}: {v}" for k, v in meta.items())
        self._write(self.dir / "关系.md", f"---\n{head}\n---\n{body}")

    def _write(self, path: Path, text: str, backup: bool = True) -> None:
        """整份替换一个记忆文件: 先写同目录的临时文件、刷到盘上, 再 os.replace 换上去.
        中途断电、崩溃时旧文件不动, Obsidian 也看不到写了一半的内容.
        换之前把上一版留在 <角色>/.backup/ 下 (同一个文件只留最近一份), 程序写坏了可以手动拷回来.
        内容没变就不写 (省得每轮都改修改时间、重建索引)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except (OSError, UnicodeDecodeError):
            pass
        if backup and path.exists():
            self._backup(path)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _backup(self, path: Path) -> None:
        """把文件的上一版按字节复制到 <角色>/.backup/<相对路径>.bak; 备份自己也是先写临时文件再换.
        备份失败只记一笔、不挡这次写入: 新内容比备份要紧."""
        try:
            rel = path.relative_to(self.dir)
        except ValueError:  # 不在角色文件夹里的不备份
            return
        target = self.dir / BACKUP_DIR / rel.with_name(rel.name + ".bak")
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as f:
                f.write(path.read_bytes())
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            log.warning("记忆备份失败 (%s): %s", path, exc)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _append(self, path: Path, text: str, head: str = "") -> None:
        """往日志里追加一行, 写完刷到盘上 (崩溃、断电时最多丢最后一行, 不会把前面的冲掉).
        head 只在新建的空文件开头写一次."""
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8") as f:
            if new and head:
                f.write(head)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

    def start_session(self) -> None:
        """一次连接算一次见面. 记下距上次见面隔了几天, 这次的情绪计数清零."""
        with self._lock:
            meta, body = self._relation()
            now = self.clock()
            meta["sessions"] = str(int(meta.get("sessions") or 0) + 1)
            self._gap_days = _days_between(meta.get("last_seen"), now)
            self._first_today = (meta.get("last_seen") or "")[:10] != f"{now:%Y-%m-%d}"
            meta["last_seen"] = now.strftime("%Y-%m-%d %H:%M")
            meta["familiarity"] = familiarity(meta, now)
            self._mood = Counter()
            self._pending = self._prev = None  # 换了一次见面, 上一次的"用上了没有"不跨过来
            self._write_relation(meta, body)

    def _morning_hello(self) -> list[str]:
        """出厂种子 seed:habit:2 "早上第一次见到主人打个招呼": 今天头一回见面、又是早上 (默认 5–11 点, 按她的时区),
        第一轮的关系概况里提一句, 打不打、怎么打由她. memory.seed_off 里停用了就不提."""
        seed = MS.active(self.char, "seed:habit:2", self.seed_mode, self.seed_off)
        if not seed or not self._first_today:
            return []
        if self.strength.dormant(S.SEED_HELLO):  # 被纠正到休眠 (阶段 5): 不再主动提, 种子本身不删
            return []
        v = seed.get("values") or {}
        return ["现在是早上, 今天头一回见到用户, 先说声早上好"] if int(v.get("from_hour", 5)) <= self.clock().hour < int(v.get("to_hour", 11)) else []

    def relation_line(self, scope: str | None = None) -> str | None:
        """会话第一轮带上的一句关系概况 (之后几轮不带, 提示词保持短): 第几次见面、熟悉程度、隔了多久、
        上次见面时用户的情绪、最近聊到的一件事. 都是低频变化的条件, 不随每句话变."""
        if scope == "none":  # 日记本合着、什么都不取
            return None
        try:
            meta, _ = self._relation()
        except OSError:
            return None
        sessions, turns = int(meta.get("sessions") or 0), int(meta.get("turns") or 0)
        if sessions <= 1 and turns == 0:
            return "第一次见面"
        name = meta.get("user_name")
        hello = self._morning_hello()
        upcoming = self._upcoming(only=f"{self.char}/记忆/关于用户.md" if scope else None)
        if upcoming:
            # 有近几天的安排时只带安排 (和名字): 实测 0.8B 在概况 (第几次见面、聊过几轮) 一起带上时
            # 很少主动提起安排 (10 次里 2 次), 只带安排时 10 次里 7 次. 给小模型的东西越少越要紧, 越用得上
            return "; ".join(hello + upcoming + ([f"用户叫{name}"] if name else []))
        parts = hello + [f"{'用户叫' + name + ', ' if name else ''}这是第 {sessions} 次见面, 一共聊过 {turns} 轮, "
                         f"{familiarity(meta, self.clock())}"]
        if (self._gap_days or 0) >= 3:
            parts.append(f"上次见面是 {self._gap_days} 天前")
        mood = meta.get("mood")
        if mood in ("低落", "开心") and (_days_between(meta.get("mood_date"), self.clock()) or 0) <= 7:
            parts.append(f"上次聊天时用户{'情绪低落' if mood == '低落' else '很开心'}")
        recent = None if scope else self._recent_summary()  # 门控时不带对话摘要里的"最近聊到"
        if recent:
            parts.append(f"最近聊到: {recent}")
        return "; ".join(parts)

    def _upcoming(self, days: int = 3, only: str | None = None) -> list[str]:
        """今天到 days 天后有安排的事实 ("用户10月28日要去医院复查 (明天)"), 最多两条."""
        today = self.clock().date()
        with self._lock:
            self._refresh()
            out = []
            for c in self._chunks:
                d = O.event_date(c["text"], c.get("said"))
                if d and 0 <= (d - today).days <= days and not c["log"] and (only is None or c["file"] == only):
                    out.append((d, O.with_time(c["text"], c.get("said"), today)))
        return [t for _, t in sorted(out)[:2]]

    def _recent_summary(self, days: int = 7) -> str | None:
        """对话摘要里最近一天的最后一条 (7 天以内)."""
        path = self.dir / "记忆" / SUMMARY_FILE
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        day, last = None, None
        for line in text.splitlines():
            if _DAY.fullmatch(line.lstrip("#").strip()) and line.startswith("#"):
                day, last = line.lstrip("#").strip(), None
            elif line.startswith("- ") and day:
                last = line
        if not last or (_days_between(day, self.clock()) or 0) > days:
            return None
        return _clean_fact(last[2:])

    # ------------------------------------------------------------------ 检索
    def _files(self) -> list[Path]:
        """参与检索的文件, 加上 候选.md (_refresh 拿它的修改时间看用户有没有勾选, 不切块).
        遍历时直接跳过不检索的文件夹 (.obsidian、图谱/、日志、exclude), 不进去扫: 每轮检索都要列一遍."""
        skip = {str(self.dir / "图谱"), str(self.dir / "日记"), str(self.dir / BACKUP_DIR)} | (
            set() if self.include_logs else {str(self.dir / "日志")})
        out = []
        for d, dirs, files in os.walk(self.root):
            rel = os.path.relpath(d, self.root).replace(os.sep, "/")
            dirs[:] = [x for x in dirs if x != ".obsidian" and os.path.join(d, x) not in skip
                       and not ((x if rel == "." else f"{rel}/{x}") + "/").startswith(self.exclude)]
            out += [Path(d, f) for f in files if f.lower().endswith(".md") and f not in ("README.md", "欢迎.md")]
        return out

    def _refresh(self) -> None:
        """库文件的修改时间变了才更新; 每个文件的切块和分词结果按修改时间缓存, 只重新切改过的文件
        (每轮都会改 关系.md 和 关于用户.md, 整库重新分词在几千块时要近 1 s, 会让每轮检索都超时).
        候选.md 改过就先看用户在 Obsidian 里勾上了哪些, 勾上的转正; 候选本身不参与检索."""
        cand = str(self.dir / "记忆" / CANDIDATE_FILE)
        stats = [(str(p), p.stat().st_mtime_ns) for p in self._files()]
        if any(p == cand and t != self._cand_sig for p, t in stats) and self._sync_candidates():
            stats = [(str(p), p.stat().st_mtime_ns) for p in self._files()]  # 转正时改了 对话摘要.md
        sig = tuple(sorted(x for x in stats if x[0] != cand))
        if sig == self._sig:
            return
        cache = {}
        for key, mtime in sig:
            hit = self._cache.get(key)
            cache[key] = hit if hit and hit[0] == mtime else (mtime, self._chunk_file(Path(key)))
        self._cache = cache
        chunks = [c for _, cs in cache.values() for c in cs]
        # 索引和图谱只看记忆的文字: 文字没变 (多数轮次只改了 关系.md 的轮数和日志) 就沿用上次建好的, 不重建.
        # 切块本身照样换新的 (标签、日期变了, 长期/事件和鲜活度要跟着变)
        key = tuple((c["text"], c["log"]) for c in chunks)
        if key != self._index_key:
            self._bm25 = BM25([c["tokens"] for c in chunks], tfs=[c["tf"] for c in chunks]) if chunks else None
            self._ents: dict[str, list[int]] = {}
            for i, c in enumerate(chunks):
                for e in c["ents"]:
                    self._ents.setdefault(e, []).append(i)
            # 图谱: 名字、关系词、品种并成同一个实体; 联想按组走 ("柯基"也能走到"旺财今天把拖鞋咬坏了"). 日志不进图谱
            self._graph = G.Graph(["" if c["log"] else c["text"] for c in chunks],
                                  [c["glinks"] for c in chunks], [c["gment"] for c in chunks])
            self._gents: dict[str, list[int]] = {}
            self._gents_by_i = []
            g, words = self._graph, {r: frozenset(ws) for r, ws in self._graph.group.items()}
            for i, c in enumerate(chunks):
                ms = g.mention[i]
                self._gents_by_i.append(c["ents"].union(*(words[g.root[w]] for w in ms)) if ms else c["ents"])
                for e in self._gents_by_i[-1]:
                    self._gents.setdefault(e, []).append(i)
            self._index_key = key
        for c, g in zip(chunks, self._gents_by_i):
            c["gents"] = g
        self._chunks = chunks
        self._sig = sig
        if self.graph_notes:  # 记忆/ 里的事实或图谱变了才重算笔记; 内容在这里 (持锁) 算好, 写文件放到后台线程, 不挡检索
            nkey = (key, tuple((c["file"], c["text"], c.get("said")) for c in chunks if c["file"].startswith(f"{self.char}/记忆/")))
            if nkey != self._notes_key:
                self._notes_key = nkey
                notes = self._graph_notes()
                threading.Thread(target=self._write_notes, args=(notes,), daemon=True, name="memory-graph-notes").start()

    def _graph_notes(self) -> dict[str, str]:
        """图谱 -> {相对 图谱/ 的路径: 笔记内容}. 每个实体一篇 (<类别>/<名字>.md): 类别、别名、提到它的记忆 (带日期链接)、
        相关的实体; 另加 用户.md 总览. 用户自己写的笔记、日志不算."""
        g, notes, used = self._graph, {}, set()
        entities = []
        for root, idx in g.members.items():
            cat = g.category(root)
            facts = [self._chunks[i] for i in idx if not self._chunks[i]["log"]
                     and self._chunks[i]["file"].startswith(f"{self.char}/记忆/")]
            if not cat or not facts:
                continue
            label = re.sub(r"[\/:*?\"<>|#^\[\]]", "", g.label(root)) or "实体"
            while label in used:
                label += "_"
            used.add(label)
            entities.append((root, cat, label, facts))
        name_of = {root: label for root, _, label, _ in entities}
        mine = {root for root, _, _, facts in entities if any(c["text"].startswith("用户") for c in facts)}  # 别人的 (老王的猫) 不连到用户
        for root, cat, label, facts in entities:
            related = {name_of[r] for c in facts for r in g.roots_in(c["text"]) if r != root and r in name_of}
            lines = ["---", "生成: wm", f"类别: {cat}", f"别名: [{', '.join(sorted(g.group[root]))}]", "---", f"# {label}", "",
                     f"#图谱/{cat}", ""]
            lines += [f"- {c['text']}" + (f" · [[{c['said']:%Y-%m-%d}]]" if c.get("said") else "") for c in facts]
            lines += ["", "相关: " + " · ".join(f"[[{x}]]" for x in ["用户"] * (root in mine) + sorted(related))]
            notes[f"{cat.split('/')[0]}/{label}.md"] = "\n".join(lines) + "\n"
        hub = ["---", "生成: wm", "---", "# 用户", "", "#图谱", "",
               "程序从记忆里整理出的人、物、地点 (只读的视图: 要改就改 记忆/ 下的文件, 这里下次会重新生成).", ""]
        for top in ("人物", "宠物", "物品", "地点"):
            items = [(label, cat) for root, cat, label, _ in entities if cat.split("/")[0] == top and root in mine]
            if items:
                hub += [f"## {top}", *[f"- [[{label}]]" + (f" ({cat.split('/')[1]})" if "/" in cat else "") for label, cat in items], ""]
        notes["用户.md"] = "\n".join(hub) + "\n"
        return notes

    def _write_notes(self, notes: dict[str, str]) -> None:
        """写 <角色>/图谱/: 内容变了才写; 不在这次结果里的旧笔记, 只删程序生成的 (frontmatter 有 "生成: wm")."""
        base = self.dir / "图谱"
        with self._notes_lock:
            try:
                for rel, text in notes.items():
                    path = base / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists() or path.read_text(encoding="utf-8") != text:
                        self._write(path, text, backup=False)
                for path in base.rglob("*.md"):
                    rel = path.relative_to(base).as_posix()
                    if rel not in notes and "生成: wm" in path.read_text(encoding="utf-8")[:200]:
                        path.unlink()
            except OSError as exc:
                log.warning("写图谱笔记失败: %s", exc)

    def _chunk_file(self, p: Path) -> list[dict]:
        text = p.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)  # 注释 (占位说明) 不算记忆
        is_log = "日志" in p.parts
        if is_log:  # 记忆的来源决定默认是长期还是事件 (memory_ops.memory_kind)
            source = "log"
        elif p.parent == self.dir / "记忆":
            source = {"关于用户.md": "facts", SUMMARY_FILE: "summary"}.get(p.name, "note")
        else:
            source = "note"
        chunks = []
        # 按段落和列表项切块, 每块带上所在的标题 (没有标题用文件名): "旅行: 明年想去京都看樱花"
        heading, para = p.stem, []

        def flush():
            raw_text = " ".join(para)
            body = _clean_fact(raw_text)
            if len(body) >= 3:
                ctx = "" if heading in body or heading in ("关于用户", "对话摘要") or _DAY.fullmatch(heading) else f"{heading}: "
                text = (ctx + body)[:160]
                words, chars = split_tokens(text)
                said = _DAY.search(raw_text) or (_DAY.fullmatch(heading) and _DAY.search(heading))
                chunks.append({"text": text, "file": p.relative_to(self.root).as_posix(), "log": is_log,
                               "words": set(words), "tokens": words + chars, "tf": Counter(words + chars),
                               "ents": O.entities(body),
                               "said": datetime.strptime(said.group(), "%Y-%m-%d").date() if said else None,
                               "kind": O.memory_kind(body, " ".join(re.findall(r"#\S+", raw_text)), source),
                               # 图谱用的: 跟着文件缓存, 写库后重建图谱时不用再跑正则 (日志不进图谱)
                               "glinks": [] if is_log else G.links(text), "gment": set() if is_log else G.mentions(text),
                               "times": [] if is_log else _src_times(raw_text)})
            para.clear()

        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                flush()
                heading = line.lstrip("#").strip() or heading
            elif not line:
                flush()
            elif re.match(r"[-*] ", line):
                flush()
                if not line[2:].startswith("~~"):  # 划掉的 (已更正、用户要求忘掉) 不再检索
                    para.append(line[2:])
            else:
                para.append(line)
        flush()
        return chunks

    def _scores(self, query: str) -> dict[int, float]:
        """BM25, 但区分强弱匹配: 至少一边是完整的词才算强匹配 ("猫" 对 "橘猫"), 两边都只是拆出来的单字算弱匹配.
        没有强匹配的文档不算命中. 问角色自己的事时 (有"你"没有"我"), 谓语词 (喜欢/叫/住…) 不算."""
        words, chars = split_tokens(query)
        qw = set(words)
        about_you = "你" in query and not _ME.search(query)
        strong: dict[int, float] = {}
        weak: dict[int, float] = {}
        for t in qw | set(chars):
            if about_you and t in _PRED:
                continue
            for i, s in self._bm25.terms(t):
                if t in qw or t in self._chunks[i]["words"]:
                    strong[i] = strong.get(i, 0.0) + s
                else:
                    weak[i] = weak.get(i, 0.0) + s
        return {i: s + WEAK * weak.get(i, 0.0) for i, s in strong.items()}

    # ------------------------------------------------------------------ 鲜活度
    def _recall_dates(self) -> dict[str, dict]:
        """记忆正文 -> {"last": 上次被想起 (或确认) 的日期, "days": [提到或想起过的日子, 说的那天算第一天]}.
        旧格式 (只记了上次被想起的日期) 照认."""
        if self._recalled is None:
            try:
                data = json.loads((self.dir / "记忆" / RECALL_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            out = {}
            for k, v in data.items() if isinstance(data, dict) else ():
                if isinstance(v, str):
                    v = {"last": v, "days": [v]}
                if isinstance(v, dict) and isinstance(v.get("last"), str):
                    out[k] = {"last": v["last"], "days": sorted({d for d in v.get("days") or [] if isinstance(d, str)})}
            self._recalled = out
        return self._recalled

    def _touch(self, text: str, day: str, said=None, count: bool = True) -> int:
        """记一笔: day 想起 (或确认) 了这条, 鲜活度从这天算; count 时 day 也算"提到或想起过的一天".
        第一次记时说的那天 (said) 算第一天. 返回一共几天. 持锁调用, 不写盘 (_save_recalled 写)."""
        e = self._recall_dates().setdefault(text, {"last": day, "days": []})
        if said and not e["days"]:
            e["days"] = [str(said)]
        if count and day not in e["days"]:
            e["days"] = sorted(e["days"] + [day])
        e["last"] = max(e["last"], day)
        return len(e["days"])

    def _save_recalled(self, prune: bool = False) -> None:
        """写 记忆/.recall.json. prune: 库里和候选里都已经没有的条目顺手清掉."""
        if prune:
            alive = {c["text"] for c in self._chunks} | {c["text"] for c in self.candidates()}
            self._recalled = {k: v for k, v in self._recall_dates().items() if k in alive}
            self.strength.prune(alive)
        self._recall_ver += 1
        try:
            self._write(self.dir / "记忆" / RECALL_FILE,
                        json.dumps(self._recalled, ensure_ascii=False, indent=0), backup=False)
        except OSError as exc:
            log.warning("写想起记录失败: %s", exc)

    def _promote(self, texts: list[str]) -> list[str]:
        """确认过的事件在不同的日子提到或想起够 promote_days 天: 对话摘要里那一行加上 #长期 #常提, 以后不淡化.
        带具体日子的、手动标了 #事件 的、本来就算长期的、用户手写的行不动. 持锁调用. 返回升了的."""
        path = self.dir / "记忆" / SUMMARY_FILE
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        want, done = set(texts), []
        for i, line in enumerate(lines):
            fact = O.fact_of_line(line)
            if (fact in want and O.is_auto(line) and "#事件" not in line and not O.has_date(fact)
                    and O.memory_kind(fact, line, "summary") == O.EVENT):
                lines[i] = line.rstrip() + (" #长期" if "#常提" in line else " #长期 #常提")
                done.append(fact)
        if done:
            self._write(path, "\n".join(lines) + "\n")
            log.info("常提的事升成长期: %s", "; ".join(done))
        return done

    def _freshness(self, today: date) -> list[tuple[float, int]]:
        """每块记忆的 (鲜活度, 距今天数), 和 _chunks 对齐; 长期的是 (1, 0). 今天、库、想起记录都没变就用上次算的."""
        key = (today, self._sig, self._recall_ver, self.affect.version(), self.strength.refresh())
        if key == self._fresh_key:
            return self._fresh
        rec = self._recall_dates()
        sm, st = self.strength.mode, self.strength
        out, new = [], []
        for c in self._chunks:
            if c["kind"] != O.EVENT or self.half_life <= 0:
                out.append((1.0, 0))
                if sm != "off":
                    new.append((st.fresh_eff(1.0, c["text"]), 0))  # 长期的不淡; 只有被纠正压低了的 (g < 1) 才会低于 1
                continue
            try:
                last = date.fromisoformat(rec[c["text"]]["last"]) if c["text"] in rec else None
            except ValueError:  # 用户手改坏了
                last = None
            age = O.age_days(c["text"], c.get("said"), today, last)
            x = self.affect.halflife_x(c) if self.affect.mode == "on" else 1.0  # 带情绪的淡得慢 (阶段 4, 最多 max_halflife_x 倍)
            out.append((O.freshness(age, self.half_life * x), age))
            if sm != "off":  # 阶段 5: 稳定性 s = s0 · m (m 里含情绪的 x, 不再另外乘一遍), 再乘强度系数 g
                new.append((st.fresh_eff(O.freshness(age, self.half_life * st.mult(c["text"], self._sal(c["text"]), x, self.affect.max_x)), c["text"]), age))
        self._fresh_new = new if sm == "shadow" else []
        res = new if sm == "on" else out  # off 和 shadow 用旧的
        self._fresh, self._fresh_key = res, key
        return res

    def _sal(self, text: str) -> float:
        """显著性 (memory_relay.salience, 只看正文, 不传"说过几天": 那一项和复习日子重复); 缓存."""
        v = self._sal_cache.get(text)
        if v is None:
            if len(self._sal_cache) > 20000:
                self._sal_cache.clear()
            v = self._sal_cache[text] = R.salience(text)[0]
        return v

    def _strong(self, text: str) -> bool:
        """升长期要"强度过线": on 档下被纠正压低过的 (σ < σ0) 不升; 别的档、没被动过的和现在一样."""
        return self.strength.mode != "on" or self.strength.sigma(text) >= self.strength.p["sigma0"] - 1e-9

    def _eta_mult(self, text: str) -> float:
        """情绪学习率倍率 clamp(1 + 情绪强度, 0.5, 2) (阶段 4 方案里的 η×(0.5–2)); 情绪不是 on 时恒为 1."""
        if self.affect.mode != "on":
            return 1.0
        c = next((c for c in self._chunks if c["text"] == text), None)
        return max(0.5, min(2.0, 1.0 + self.affect.strength(c))) if c else 1.0

    def _faded(self, i: int, fresh: tuple[float, int]) -> dict:
        """本来想得起来、因为太久了没想起来的一条 (埋点里看为什么没想起来)."""
        d = {"text": self._chunks[i]["text"][:40], "fresh": round(fresh[0], 2), "age": fresh[1]}
        if self.strength.mode == "on":
            g = self.strength.g_of(self._chunks[i]["text"])
            if g != 1.0:
                d["g"] = round(g, 2)
        return d

    def _mark_recalled(self, hits: list[dict], today: date) -> list[str]:
        """检索时再巩固: 带进提示词的事件记忆记下今天被想起过, 鲜活度从今天重新算.
        直接命中的 (不是联想带出来的) 还算"提到或想起过的一天", 不同的日子够 promote_days 天就升成长期. 返回升了的."""
        if not self.refresh_on_recall or self.half_life <= 0:
            return []
        rec, day = self._recall_dates(), today.isoformat()
        due, changed = [], False
        for h in hits:
            if h.get("kind") != O.EVENT:
                continue
            old = rec.get(h["text"])
            old = old and (old["last"], len(old["days"]))
            n = self._touch(h["text"], day, h.get("said"), count=not h.get("assoc"))
            changed |= old != (rec[h["text"]]["last"], len(rec[h["text"]]["days"]))
            if n >= self.promote_days > 0 and not h.get("assoc") and self._strong(h["text"]):
                due.append(h["text"])
        if not changed:
            return []
        promoted = self._promote(due) if due else []
        self._save_recalled(prune=True)
        return promoted

    # ------------------------------------------------------------------ 语义检索
    def start_embedder(self, cfg: dict | None) -> None:
        """后台线程: 载入嵌入模型 (bge 要导入 torch, 约 15 s), 再给库里所有记忆算向量. 没算完之前检索用 BM25."""
        from .embed import MIN_SIM, load_embedder

        def run():
            try:
                emb = load_embedder(cfg)
            except Exception as exc:  # 模型文件不在、嵌入服务没开 (engine 指定了 qwen)
                self.embed_error = f"{type(exc).__name__}: {exc}"
                log.warning("嵌入模型载入失败, 记忆检索用 BM25: %s", exc)
                return
            if emb is None:
                return
            sim = (cfg or {}).get("min_sim")
            self.min_sim = float(sim.get(emb.name) if isinstance(sim, dict) else sim or MIN_SIM[emb.name])
            self.embedder = emb
            log.info("记忆检索改用向量 (%s, 门槛 %.2f)", emb.label, self.min_sim)
            self._embed_missing()

        threading.Thread(target=run, daemon=True, name="memory-embed").start()

    def _embed_missing(self) -> int:
        """给还没有向量的记忆算向量 (只算新增的; 在后台线程里调). 返回算了几条."""
        if self.embedder is None:
            return 0
        with self._lock:
            self._refresh()
            texts = list(dict.fromkeys(c["text"] for c in self._chunks if c["text"] not in self._vec))
        if not texts:
            return 0
        try:
            vecs = self.embedder.encode(texts)
        except Exception as exc:
            log.warning("记忆向量计算失败: %s", exc)
            return 0
        with self._vec_lock:
            self._vec.update(zip(texts, vecs))
        return len(texts)

    def _kick_embed(self) -> None:
        if self.embedder is not None and (self._vec_job is None or not self._vec_job.is_alive()):
            self._vec_job = threading.Thread(target=self._embed_missing, daemon=True, name="memory-embed-delta")
            self._vec_job.start()

    def _vector_hits(self, query: str) -> tuple[list[dict], list[dict]] | None:
        """向量检索 -> (命中, 太久了没想起来的); 还有记忆没算向量、或者编码出错时返回 None (这一轮用 BM25).
        用户在问自己的事 ("我是做什么工作的") 时门槛放宽 0.1: 他明确在要记忆, 宁可多带一条.
        事件记忆的门槛按鲜活度抬高 (最多 vector_penalty), 排序也用抬高后的分数."""
        import numpy as np

        with self._vec_lock:
            vecs = [self._vec.get(c["text"]) for c in self._chunks]
        if any(v is None for v in vecs):
            self._kick_embed()
            return None
        if "你" in query and not _ME.search(query):
            return [], []  # 问角色自己的事 ("你喜欢什么颜色"): 不取关于用户的记忆
        try:
            q = self.embedder.encode([query], query=True)[0]
        except Exception as exc:  # 嵌入服务中途关了
            log.warning("问句编码失败, 这一轮用 BM25: %s", exc)
            return None
        if not vecs:
            return [], []
        sims = np.stack(vecs) @ q
        self._last_sims = sims  # 图谱的实体回答要给同一实体的其他记忆排序
        floor = self.min_sim - (0.1 if O.self_question(query) else 0.0)
        fresh = self._freshness(self.clock().date())
        penalty = 0.0 if O.recall_cue(query) else self.vec_penalty  # 用户在主动回想 ("之前""还记得") 时不压低
        adj = sims - penalty * (1 - np.array([f for f, _ in fresh]))  # 长期记忆鲜活度是 1, 分数不变
        if self._allow is not None:  # 日记本门控
            hide = np.array([i not in self._allow for i in range(len(self._chunks))])
            adj, sims = np.where(hide, -9.0, adj), np.where(hide, -9.0, sims)
        order = [int(i) for i in np.argsort(-adj) if adj[i] >= floor][: self.top_k * 3]
        if self.strength.mode == "shadow" and self._fresh_new:  # 影子: 新的新鲜度算一遍排序, 只比较不用
            adj2 = sims - penalty * (1 - np.array([f for f, _ in self._fresh_new]))
            if self._allow is not None:
                adj2 = np.where(hide, -9.0, adj2)
            order2 = [int(i) for i in np.argsort(-adj2) if adj2[i] >= floor][: self.top_k * 3]
            self._cmp = ([self._chunks[i]["text"] for i in order], [self._chunks[i]["text"] for i in order2])
        faded = [self._faded(int(i), fresh[i]) for i in np.argsort(-sims)[: self.top_k] if sims[i] >= floor > adj[i]]
        # 过了门槛的里带情绪的往前挪 (阶段 4: 只重排, 最多 +max_bias; 不是 on 时原样)
        hits = self.affect.rerank([{**self._chunks[i], "score": round(float(adj[i]), 3), "fresh": fresh[i][0]} for i in order])
        return hits[: self.top_k], faded

    def _supersede_filter(self, hits: list) -> list:
        """S2 记忆取代: 结局栏里已被取代的那几条, 不再被想起 (2026-11-17).

        用旁路 sidecar (<角色>/记忆/supersede.json), 正文一个字不动; 没有 sidecar、
        没有这条、读坏了都原样返回. 出错就当没开: 记忆这条路上抛异常, 她会忽然什么都想不起来,
        比少过滤几条严重得多. 关掉: 环境变量 WM_SUPERSEDE=off.
        """
        if not hits or os.environ.get("WM_SUPERSEDE", "on").lower() == "off":
            return hits
        try:
            from .memory_supersede import SupersedeStore

            store = SupersedeStore(self.dir / "记忆" / "supersede.json")
            if not store.count():
                return hits
            return [h for h in hits if not store.is_ended_body(h.get("text") or "")]
        except Exception:  # noqa: BLE001 - 这里绝不能把一轮对话带走
            log.warning("结局栏读取失败，这次照原样返回", exc_info=True)
            return hits

    def recall(self, query: str, first: bool = False, scope: str | None = None) -> dict:
        """-> {"line": "[记忆] ..." 或 None, "hits": [...], "ms": 耗时, "mode": 检索方式}.
        first: 这次见面的第一轮, 带上关系概况."""
        from .intent import doable

        t0 = time.perf_counter()
        mode = "bm25"
        today = self.clock().date()
        self.affect.last = {}
        self._cmp = None
        with self._lock:
            self._refresh()
            self._superseded = self._supersede_bodies()
            hits, faded = [], []
            vh = None
            self._last_sims = self._last_bm25 = None
            self._allow = None
            if scope:  # 日记本合着: slots 只看 关于用户.md 和 关系.md, none 什么都不看
                keep = () if scope == "none" else (f"{self.char}/记忆/关于用户.md", f"{self.char}/关系.md")
                self._allow = {i for i, c in enumerate(self._chunks) if c["file"] in keep}
            if not query.strip() or doable(query):
                mode = "skip"  # 动作指令 ("去窗户那边看看") 用不着记忆, 带上反而是噪声
            elif self.embedder is not None:
                vh = self._vector_hits(query)
            if vh is not None:
                (hits, faded), mode = vh, "vector"
            elif mode == "bm25" and self._bm25:
                fresh = self._freshness(today)
                floor = 1.0 if O.recall_cue(query) else self.bm25_floor  # 用户在主动回想 ("之前""还记得") 时不压低
                self._last_bm25 = self._scores(query)

                def bm25_pass(fr):  # shadow 档要用新旧两套新鲜度各排一遍
                    got, fad = [], []
                    for i, s in self._last_bm25.items():
                        if not self._vis(i):
                            continue
                        s *= 0.6 if self._chunks[i]["log"] else 1.0  # 原始日志比提炼过的记忆权重低
                        adj = s * (floor + (1 - floor) * fr[i][0])  # 长期记忆鲜活度是 1, 分数不变
                        if adj >= self.min_score:
                            got.append({**self._chunks[i], "score": round(adj, 2), "fresh": fr[i][0]})
                        elif s >= self.min_score:  # 本来想得起来, 太久了没想起来
                            fad.append(self._faded(i, fr[i]))
                    got.sort(key=lambda h: -h["score"])
                    return [h for h in got if h["score"] >= self.rel_ratio * got[0]["score"]][: self.top_k * 3], fad

                pre, faded = bm25_pass(fresh)
                if self.strength.mode == "shadow" and self._fresh_new:
                    self._cmp = ([h["text"] for h in pre], [h["text"] for h in bm25_pass(self._fresh_new)[0]])
                hits = self.affect.rerank(pre)
                hits = hits[: self.top_k]
            promoted = []
            if mode != "skip":
                hits = self._fade_past(hits)
                hits += self._associate(query, hits)
                promoted = self._mark_recalled(hits, today)
                hits = self._supersede_filter(hits)  # S2: 已被取代的不再被想起
            listed = self._list_facts(query) if mode != "skip" else []
            if listed:  # "我家里都有谁""我养了哪些宠物": 按类取, 每个实体一条, 由服务端回答
                hits, answer = listed, O.answer_recall([h["text"] for h in listed])
            elif mode != "skip":
                # "我男朋友平时喜欢干嘛": 问的是他, 先按图谱答, 不当成问用户自己的喜好
                answer = self._answer_entity(query) if self._entity_question(query) else None
                if answer is None:
                    answer = self._answer_self(query)
                if answer is None:
                    answer = self._answer_recall(query, hits, mode)
            else:
                answer = None
        # 一个月以前的事件带上多久以前说的; 有具体日子的照旧换成"明天""(7天前)"
        parts = [O.with_time(O.with_age(h["text"], h.get("said"), today) if h.get("kind") == O.EVENT else h["text"],
                             h.get("said"), today) for h in hits]
        if not parts and answer is None and mode != "skip" and _ASK_PAST.search(query):
            # 用户在问自己以前说过的事, 记忆里没有: 直接说没说过 (提示模型"不记得"它还是会答成自己的事)
            answer = "这个你还没跟我说过呢。"
        self._allow = None
        self._superseded = set()
        hello = False
        if first:
            rel = self.relation_line(scope)
            if rel:
                parts.insert(0, rel)
                hello = S.HELLO_LINE in rel  # 种子"早上好"这一轮提了
        line = f"{MEMORY_TAG} " + "; ".join(p.rstrip("。.") for p in parts) + "." if parts else None
        out = {"line": line, "hits": [{"file": h["file"], "score": h["score"], "text": h["text"][:40],
                                       **({"fresh": round(h["fresh"], 2)} if h.get("fresh", 1.0) < 1 else {})} for h in hits],
               "ms": round((time.perf_counter() - t0) * 1000, 1), "mode": mode, "answer": answer}
        if faded:
            out["faded"] = faded[:3]
        if self.affect.last:
            out["affect"] = self.affect.last  # 这一轮乘了 (影子模式: 本来会乘) 多少
        if promoted:
            out["promoted"] = promoted  # 这一轮常提够了天数、升成长期的
        if mode != "skip":
            self.recalls.append({"at": f"{self.clock():%Y-%m-%d %H:%M:%S}", "q": query.strip()[:80], "mode": mode, "first": first,
                                 "hits": [{"file": h["file"], "text": h["text"][:120], "score": h["score"],
                                           **({"fresh": round(h["fresh"], 2)} if h.get("fresh", 1.0) < 1 else {})} for h in hits],
                                 "faded": out.get("faded", []), "promoted": promoted, "answered": answer is not None})
            if self.strength.mode != "off":
                with self._lock:
                    out["strength"] = self.recalls[-1]["strength"] = self._strength_trace(hits)
                    # 登记这一轮带进提示词的 (联想带出的不算); observe 里按"回复用上了没有"结算. 只认主人的话 (pipeline.Session)
                    self._pending = {"q": query.strip(), "texts": [h["text"] for h in hits if not h.get("assoc")], "seed": hello,
                                     "at": self.clock()}
        return out

    def _strength_trace(self, hits: list[dict]) -> dict:
        """追踪里的 strength 项 (只在内存, pipeline 只拷指定的键, 不进 trace 日志). shadow: 新旧排序哪几条名次变了,
        同时往影子日志追加 (只有键的短哈希和数字, 没有正文). on: 被压低 / 抬高的 g."""
        st = self.strength
        if st.mode == "on":
            g = {h["text"][:40]: round(st.g_of(h["text"]), 2) for h in hits if st.g_of(h["text"]) != 1.0}
            return {"mode": "on", **({"g": g} if g else {})}
        if not self._cmp:
            return {"mode": "shadow", "moved": []}
        k = self.top_k
        old = {t: i + 1 for i, t in enumerate(self._cmp[0][:k])}
        new = {t: i + 1 for i, t in enumerate(self._cmp[1][:k])}
        moved, rows = [], []
        for t in dict.fromkeys(list(old) + list(new)):
            o, n = old.get(t, 0), new.get(t, 0)
            if o != n:
                c = next((c for c in self._chunks if c["text"] == t), None)
                x = self.affect.halflife_x(c) if c and self.affect.mode == "on" else 1.0
                moved.append({"text": t[:40], "old": o, "new": n})
                rows.append((t, o, n, st.g_of(t), st.mult(t, self._sal(t), x, self.affect.max_x)))
        st.shadow_append(f"{self.clock():%Y-%m-%d}", rows)
        return {"mode": "shadow", "moved": moved[:6]}

    def _fade_past(self, hits: list[dict]) -> list[dict]:
        """已经过去一天以上的安排 ("用户10月20日要面试", 今天 27 号): 有眼前的事时最多留一条 (相关度最高的), 省注意力.
        顺序照相关度不动: 问的就是过去那件事时它还排第一 (挪到后面的话检索基准"排第一"少了 1-2 条). 显示成"(7天前)"."""
        today = self.clock().date()

        def past(h: dict) -> bool:
            d = O.event_date(h["text"], h.get("said"))
            return d is not None and (today - d).days > 1

        if all(past(h) for h in hits):
            return hits
        kept, seen_past = [], False
        for h in hits:
            if past(h):
                if seen_past:
                    continue
                seen_past = True
            kept.append(h)
        return kept

    def _associate(self, query: str, hits: list[dict]) -> list[dict]:
        """联想一步: 问句里提到的、或命中的事实里的实体 (人、宠物、名字), 带出同一实体的其他事实, 最多补一条.
        "豆豆是什么颜色的" -> 命中"用户的猫叫豆豆" -> 同一只猫 -> "用户养了一只橘猫".
        实体按图谱的组算 (gents): 名字、关系词、品种是同一个 ("柯基" = 狗 = "旺财")."""
        seen = {h["text"] for h in hits}
        names = {e for e in self._gents if e in query}
        for h in hits:
            names |= h.get("gents") or h.get("ents") or set()
        # 问句点了名 ("团子是什么颜色"): 名字所在事实里的实体也算 (团子 -> 仓鼠), 向量一条都没命中时也能联想到
        for e in [e for e in names if e not in O.GENERIC]:
            for i in self._gents.get(e, []):
                if self._vis(i):
                    names |= self._chunks[i]["gents"]
        extra, want = [], 1 if hits else self.top_k
        fresh = self._freshness(self.clock().date())
        for e in sorted(names, key=len, reverse=True):
            for i in self._gents.get(e, []):
                if len(extra) >= want:  # 够用就停: 常见的词 (城市、"妈妈") 在大库里能连着上千块
                    return extra
                if not self._vis(i):
                    continue
                c = self._chunks[i]
                # 联想比直接命中弱: 事件只联想一个半衰期以内的 (长期记忆鲜活度是 1, 照旧)
                if c["text"] not in seen and not c["log"] and fresh[i][0] >= 0.5:
                    seen.add(c["text"])
                    extra.append({**c, "score": 0.0, "fresh": fresh[i][0], "assoc": True})
        return extra

    def _answer_recall(self, query: str, hits: list[dict], mode: str) -> str | None:
        """0.8B 只要涉及用户自己的事就会把"我"答成它自己 (加示例、改写问句都纠正不了, 开放式问题 25 次里对 2-5 次).
        所以记忆把握大的时候由服务端回答, 并说明是"你说过的":
          问到记着的名字 ("旺财是什么品种") -> 这个名字相关的事实 (和联想出来的一条);
          问自己的事 ("我最近睡眠质量怎么样"), 向量命中明显过门槛 -> 最相关的那一条.
        求建议、做决定的问法 ("我该不该辞职") 不在此列, 交给模型."""
        if _ADVICE.search(query) or ("你" in query and not _ME.search(query)):
            return None
        if self._entity_question(query):
            ans = self._answer_entity(query)
            if ans:
                return ans
        named = [e for e in self._ents if e in query and len(e) >= 2 and e not in O.GENERIC]
        if named and _ASKING.search(query):
            # 迷你人物档案: 提到这个名字的事实, 加上和它们有同一个实体的 ("小美" -> 妹妹 -> "妹妹在读大三")
            core = [h for h in hits if not h["log"] and any(e in h["text"] for e in named)]
            ents = set().union(*(h["ents"] for h in core)) if core else set()
            linked = [h for h in hits if h not in core and not h["log"] and h["ents"] & ents]
            if core:
                return O.answer_recall([h["text"] for h in core + linked][:2])
        if mode == "vector" and hits and O.self_question(query) and hits[0]["score"] >= self.min_sim + 0.04:
            return O.answer_recall([hits[0]["text"]])
        return None

    def _entity_question(self, query: str) -> bool:
        """在问用户自己的某个人、某样东西, 或者点了记着的名字 (不是求建议, 也不是问角色)."""
        return (bool(_ASKING.search(query) or _ASK_DO.search(query)) and not _ADVICE.search(query)
                and not ("你" in query and not _ME.search(query))
                and bool(self._graph.roots_in(query)) and G.asks_own(query, self._graph.names))

    def _supersede_bodies(self) -> set[str]:
        """这一轮要藏起来的正文 (S2 记忆取代: 被取代的旧记忆). 每轮载一次, 不进热循环."""
        if os.environ.get("WM_SUPERSEDE", "on").lower() == "off":
            return set()
        try:
            from . import memory_supersede as SP

            store = SP.SupersedeStore(self.dir / "记忆" / "supersede.json")
            return store.bodies() if store.count() else set()
        except Exception:  # noqa: BLE001 - 结局栏读坏了就当没开, 不能让她忽然什么都不记得
            log.warning("结局栏读取失败，这一轮不过滤", exc_info=True)
            return set()

    def _supersede_view(self, text: str) -> dict:
        """记忆页要的"已被取代 → 新的那条": 没有结局栏就是空.\r\n        """
        try:
            from . import memory_supersede as SP

            store = SP.SupersedeStore(self.dir / "记忆" / "supersede.json")
            if not store.count():
                return {}
            rec = store.record_for(text)
            if not rec:
                return {}
            return {"status": "superseded", "superseded_by": rec.get("by", ""),
                    "superseded_why": rec.get("why", ""), "superseded_at": rec.get("ended_at", ""),
                    "manually_closed": bool(rec.get("manual"))}
        except Exception:  # noqa: BLE001 - 记忆页不能因为这个打不开
            return {}

    def _is_superseded(self, chunk: dict) -> bool:
        bodies = getattr(self, "_superseded", None)
        if not bodies:
            return False
        from . import memory_supersede as SP

        return SP.core_of(chunk.get("text") or "") in bodies

    def _vis(self, i: int) -> bool:
        """第 i 块这一轮能不能看 (日记本门控 + 已被取代的旧记忆不看)."""
        if self._allow is not None and i not in self._allow:
            return False
        return not self._is_superseded(self._chunks[i])

    def _vis_chunks(self) -> list[dict]:
        return [c for i, c in enumerate(self._chunks) if self._vis(i)]

    def _answer_entity(self, query: str) -> str | None:
        """问用户自己的某个人、某样东西 ("我女朋友生日是哪天""我家柯基最近闯什么祸了""小雨喜欢吃什么"): 在这个实体
        (图谱里的一组) 的记忆里挑和问句最相关的, 最多两条, 按记下的先后说.
        相关度看问句去掉实体的各种叫法以后剩下的部分 ("生日是哪天") 和记忆对不对得上 (BM25), 向量分数排在后面;
        第二条也要对得上才带; 一条都对不上时按先后带两条 (实体是图谱认出来的, 本身就是线索).
        不是在问名字时, "X叫Y"这种只说名字的往后放. 用了向量而整组都不像相关的, 不答, 交给后面的回答方式."""
        roots = self._graph.roots_in(query)
        idx = list(dict.fromkeys(i for r in roots for i in self._graph.members.get(r, [])
                                 if not self._chunks[i]["log"] and self._vis(i)))
        if not idx:
            return None
        sims = {i: float(self._last_sims[i]) for i in idx} if self._last_sims is not None else {}
        if sims and max(sims.values()) < self.min_sim - 0.1:
            return None
        rest = G.normalize(query)
        for w in sorted({w for r in roots for w in self._graph.group[r]}, key=len, reverse=True):
            rest = rest.replace(w, " ")
        lex = self._scores(rest) if self._bm25 and rest.strip() else {}
        asks_name = "叫" in query or "名字" in query

        def naming(i: int) -> bool:
            a = G.attr_of(self._chunks[i]["text"])
            return not asks_name and a is not None and a[1] == "名字"

        order = sorted(idx, key=lambda i: (naming(i), -lex.get(i, 0.0), -sims.get(i, 0.0), i))
        pick = order[:1] + [i for i in order[1:2] if lex.get(i, 0.0) > 0 or lex.get(order[0], 0.0) == 0]
        return O.answer_recall([self._chunks[i]["text"] for i in sorted(pick)])

    def _list_facts(self, query: str) -> list[dict]:
        """按类问 ("我家里都有谁""我养了哪些宠物""我都去过哪些城市"): 这一类的实体每个取一条用户自己的记忆, 最多 4 条.
        别人的 ("老王养了一只布偶猫")、地点里不是去过的 ("哥哥在上海工作") 不算."""
        cat = G.list_query(query)
        if cat is None:
            return []
        if cat == "日程":  # 列出带日期的事, 按日期排; 问"这周/最近"就只看那一窗 (2026-11-18 S3)
            today = self.clock().date()
            win = None
            if re.search(r"这周|本周|这星期|这个星期|一周", query):
                win = (today, today + timedelta(days=7))
            elif re.search(r"这个月|本月|这月", query):
                win = (today, today + timedelta(days=31))
            elif re.search(r"安排|计划|待办|要办|下次|下一次|接下来", query):
                # 问"安排"只列还没到的: 已经过去的事不是安排 (2026-11-19)
                win = (today, today + timedelta(days=120))
            elif re.search(r"最近|近期", query):
                win = (today - timedelta(days=30), today + timedelta(days=30))
            dated, seen = [], set()
            for i, c in enumerate(self._chunks):
                if c["log"] or not self._vis(i) or not c["text"].startswith("用户"):
                    continue
                if c["text"] in seen:
                    continue
                if re.search(r"每个?周|每个?天|每个?月|每年", c["text"]):
                    seen.add(c["text"])
                    dated.append(("0", c))
                    continue
                d = O.event_date(c["text"], c.get("said"))
                if not d:
                    continue
                try:
                    dd = datetime.strptime(str(d), "%Y-%m-%d").date()
                except ValueError:
                    dd = None
                if win and dd and not (win[0] <= dd <= win[1]):
                    continue
                seen.add(c["text"])
                dated.append((str(d), c))
            dated.sort(key=lambda x: x[0])
            return [{**c, "score": 1.0} for _, c in dated[:4]]
        want = {"家人": ("人物/家人",), "宠物": ("宠物",), "地点": ("地点",)}[cat]
        out, seen = [], set()
        for root, idx in self._graph.members.items():
            if self._graph.category(root) not in want:
                continue
            for i in idx:
                c = self._chunks[i]
                if c["log"] or not self._vis(i) or not c["text"].startswith("用户") or c["text"] in seen:
                    continue
                if cat == "地点" and not G._TRAVEL.search(c["text"]):
                    continue
                seen.add(c["text"])
                out.append({**c, "score": 1.0})
                break
        return out[:4]

    def _answer_self(self, query: str) -> str | None:
        """"我叫什么""我住在哪"这类问用户自己固定信息的话, 按记忆直接回答 (不交给模型); 不是这类问题返回 None."""
        slot = O.asks_slot(query)
        if slot is None:  # "我哪天要去医院复查": 按带日期的事实回答; "我喜欢喝咖啡吗": 按偏好回答
            chunks = self._vis_chunks()
            dated = [(c["text"], d) for c in chunks if not c["log"] and (d := O.event_date(c["text"], c.get("said")))]
            mine = [c["text"] for c in chunks if c["file"].startswith(f"{self.char}/记忆/")]
            return (O.answer_when(query, dated, self.clock().date()) or O.answer_pref(query, mine)
                    or O.answer_birth(query, mine, self.clock().date())
                    or O.answer_likes(query, mine, self._forgot_about(query)))
        facts = [c["text"] for c in self._vis_chunks() if c["file"].startswith(f"{self.char}/记忆/") and O.slot_of(c["text"]) == slot]
        return O.answer_slot(slot, facts[-1] if facts else None, forgotten=not facts and self._forgot(slot))

    def _forgot_about(self, query: str) -> bool:
        """问的东西是不是被用户要求忘掉过 (划掉的行里注明"用户要求忘掉", 内容和问句有两个以上实词字相同)."""
        want = {c for c in O._HAN.findall(query) if c not in O._FILL}
        for name in ("关于用户.md", SUMMARY_FILE):
            path = self.dir / "记忆" / name
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    m = re.match(r"- ~~(.+?)~~.*用户要求忘掉", line.strip())
                    if m and len(want & set(O._HAN.findall(m.group(1)))) >= 2:
                        return True
        return False

    def _forgot(self, slot: str) -> bool:
        """这个槽的事实是不是被用户要求忘掉过 (划掉的行里注明了"用户要求忘掉")."""
        for name in ("关于用户.md", SUMMARY_FILE):
            path = self.dir / "记忆" / name
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    m = re.match(r"- ~~(.+?)~~.*用户要求忘掉", line.strip())
                    if m and O.slot_of(_clean_fact(m.group(1))) == slot:
                        return True
        return False

    # ------------------------------------------------------------------ 写入
    def observe(self, user_text: str, reply: str, acts: list | None = None, emo: str | None = None,
                proactive: bool = False) -> list[str]:
        """一轮对话结束: 记日志, 提取新的偏好和事实, 更新关系状态. 返回新记下的事实.
        proactive=True: 她自己主动开口的一轮 (ADR-0026) —— 不算"聊过一轮": 不加 turns, 也不动 last_seen / familiarity."""
        now = self.clock()
        new = []
        with self._lock:
            self._settle(user_text, reply, now)  # 强度的信号 (阶段 5; memory.strength.mode 是 off 时什么都不做)
            day = self.dir / "日志" / f"{now:%Y-%m-%d}.md"
            act = f" (动作: {', '.join(a['do'] + ('→' + a['to'] if a.get('to') else '') for a in acts)})" if acts else ""
            self._append(day, f"- {now:%H:%M} 用户: {user_text} → {self.char}: {reply}{act}\n",
                         head=f"# {now:%Y-%m-%d} 的对话\n\n")
            facts_file = self.dir / "记忆" / "关于用户.md"
            known = "\n".join(f for f in map(O.fact_of_line, facts_file.read_text(encoding="utf-8").splitlines()) if f)
            meta, body = self._relation()
            own = R.own_part(user_text) if self.relay else user_text  # 只看你自己说的 (转述、引号里、梦里假设的盖掉; 日志照记原话)
            target = O.forget_target(own)
            if target:  # "帮我把我住在哪忘掉吧": 划掉对应的行, 不再检索
                facts = self._active_facts()
                drop = {facts[i] for i in O.forget_matches(target, facts)}
                self.forgotten = self._strike(lambda f: f in drop, f"{now:%Y-%m-%d} 用户要求忘掉")
            said = []  # 这句里提到的事 (情绪事件连到它们)
            for kind, fact in ([] if target else extract_facts(own)):
                fact = settle_fact(kind, fact, now.date())
                said.append(fact)
                same = next((k for k in known.splitlines() if near_dup(fact, k)), "") if _rules2_on() else ""
                if same:  # 近义重复: 记成"又说了一遍", 不新增一行 (用户 2026-11-18 拍板 A)
                    self.strength.reinforce(same, "repeat", f"{now:%Y-%m-%d}", self._eta_mult(same))
                    continue
                if fact in known:
                    self.strength.reinforce(fact, "repeat", f"{now:%Y-%m-%d}", self._eta_mult(fact))  # 你又说了一遍记着的事
                    continue
                # 知识更新: 同一个槽换了值 (搬家、改名) 或喜欢变成不喜欢, 旧的那行划掉
                self._strike(lambda f: O.supersedes(fact, f) or G.supersedes(fact, f), f"{now:%Y-%m-%d} 更正为: {fact}")
                self._supersede_write(fact, f"{now:%Y-%m-%d}", own)  # S2 兜底
                self._append(facts_file, f"- {fact} ({now:%Y-%m-%d}) #{kind} #auto-memory\n")
                known += "\n" + fact
                new.append(fact)
                if kind == "名字":
                    meta["user_name"] = fact.removeprefix("用户叫")
            if not proactive:  # 主动开口不算聊过一轮 (ADR-0026): turns / last_seen / familiarity 都不动
                meta["turns"] = str(int(meta.get("turns") or 0) + 1)
                meta["last_seen"] = now.strftime("%Y-%m-%d %H:%M")
                meta["familiarity"] = familiarity(meta, now)
            # 这次见面的情绪基调: 低落和开心哪个说得多; 都没有就是"平常". 下次见面第一轮会带上
            m = mood_of(own if self.relay else user_text)  # 只数你自己的话: 转述里别人的难过不算你的 (阶段 4 顺手修)
            if m:
                self._mood[m] += 1
            low, high = self._mood["低落"], self._mood["开心"]
            meta["mood"] = "低落" if low > high else "开心" if high > low else "平常"
            meta["mood_date"] = now.strftime("%Y-%m-%d")
            self._write_relation(meta, body)
            self.affect.note(user_text, "桌宠", now, said)  # 你自己话里的情绪 (阶段 4; memory.affect.mode 是 off 时不记)
        self._embed_missing()  # 新记下的事实马上算向量 (observe 本来就在后台线程里), 下一轮检索不用等
        return new

    def _settle(self, user_text: str, reply: str, now: datetime) -> None:
        """强度的信号 (阶段 5), 持锁调用. 先结算上一轮: 这一句是纠正 -> 回复里用上了的那几条减弱; 肯定 -> 加得多; 没事 -> 加一点;
        再登记这一轮: 上一次 recall 带进提示词的 (q 要和这句话逐字相同) 里, 回复用上了哪几条. 隔了 30 分钟的、问句都不算."""
        st = self.strength
        if st.mode == "off":
            self._pending = self._prev = None
            return
        try:
            st.refresh()
            day, text = f"{now:%Y-%m-%d}", (user_text or "").strip()
            win = timedelta(minutes=st.p["window_min"])
            prev, self._prev = self._prev, None
            if prev and text and now - prev["at"] <= win:
                cue, rule = S.classify_why(text)
                st.verdict_append(day, cue, rule, prev["texts"], bool(prev.get("seed")))  # 影子判定记录: 档、规则名、记忆的短哈希, 没有正文
                for k in prev["texts"]:
                    if cue == "纠":
                        st.weaken(k, "correct", day)
                    elif cue == "肯":
                        st.reinforce(k, "affirm", day, self._eta_mult(k))
                    elif cue == "无":
                        st.reinforce(k, "use", day, self._eta_mult(k))
                if prev.get("seed"):
                    if cue in ("纠", "拒"):
                        st.weaken(S.SEED_HELLO, "refuse", day)
                    elif cue == "肯":
                        st.reinforce(S.SEED_HELLO, "affirm", day)
                    elif cue == "无":
                        st.reinforce(S.SEED_HELLO, "use", day)
            pend, self._pending = self._pending, None
            if pend and text and pend["q"] == text and now - pend["at"] <= win:
                got = [t for t in pend["texts"] if S.used(reply, t)]
                greeted = bool(pend.get("seed")) and S.greeted(reply)
                if got or greeted:
                    self._prev = {"texts": got, "seed": greeted, "at": now}
        except Exception:  # noqa: BLE001 (强度出错不耽误记日志)
            log.exception("强度结算失败")

    def reset_seed_strength(self) -> bool:
        """"恢复出厂性格": 种子的强度记录清掉 (休眠的回来). 面板按钮不在这里."""
        with self._lock:
            return self.strength.reset(S.SEED_HELLO)

    def _active_facts(self) -> list[str]:
        """程序写的、还没划掉的事实 (关于用户 + 对话摘要)."""
        out = []
        for name in ("关于用户.md", SUMMARY_FILE):
            path = self.dir / "记忆" / name
            if path.exists():
                out += [O.fact_of_line(l) for l in path.read_text(encoding="utf-8").splitlines() if O.is_auto(l) and O.fact_of_line(l)]
        return out

    def _supersede_write(self, fact: str, day: str, turn: str = "") -> list:
        """S2 记忆取代: 新记下一条时, 看它取代了还活着的哪一条, 把结局写进旁路 sidecar.

        旧的 O/G.supersedes 认得的那几种改口, 前面一步已经用 _strike 划掉了, 不归这里管;
        这一步只补它漏掉的. 一个都判不出就什么都不写 —— 宁可漏判.
        """
        if os.environ.get("WM_SUPERSEDE", "on").lower() == "off":
            return []
        try:
            from . import memory_supersede as SP

            old = []
            for p in (self.dir / "记忆" / "关于用户.md",
                      self.dir / "记忆" / SUMMARY_FILE):
                if not p.exists():
                    continue
                old += [ln for ln in p.read_text(encoding="utf-8").splitlines()
                        if ln.startswith("- ") and (O.is_auto(ln) or "#auto-summary" in ln)]
            if not old:
                return []
            store = SP.SupersedeStore(self.dir / "记忆" / "supersede.json")
            out = SP.apply_new(store, f"- {fact} ({day}) #事实 #auto-memory", old, turn_text=turn)
            _dbg = os.environ.get("WM_SUPERSEDE_DEBUG")
            if _dbg:  # 排查用: 每次判了什么写一行, 默认关
                try:
                    with open(_dbg, "a", encoding="utf-8") as _f:
                        _f.write(f"NEW={fact!r} TURN={turn!r} OLDN={len(old)}\n")
                        for _o, _d in out.decisions:
                            _f.write(f"  vs {_o!r} -> {_d.supersedes} {_d.reason}\n")
                except OSError:
                    pass
            return out.ended
        except Exception:  # noqa: BLE001 - 结局栏坏了不能拖累记这条记忆
            log.warning("结局栏写入失败，这条记忆照常记", exc_info=True)
            return []

    def _strike(self, pred, note: str) -> list[str]:
        """把程序写的、满足 pred(事实正文) 的行划掉并注明原因 (更正、用户要求忘掉). 用户手写的行不动. 返回划掉的事实."""
        done = []
        for name in ("关于用户.md", SUMMARY_FILE):
            path = self.dir / "记忆" / name
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            changed = False
            for i, line in enumerate(lines):
                fact = O.fact_of_line(line)
                if fact and O.is_auto(line) and pred(fact):
                    lines[i] = O.strike(line, note)
                    done.append(fact)
                    changed = True
            if changed:
                self._write(path, "\n".join(lines) + "\n")
        return done

    # ------------------------------------------------------------------ 桌宠小屋: 日记本、拖进来的文件
    def diary_recall(self, intent: str = "", limit: int = 3) -> list[dict]:
        """翻开日记本时她想起来的几条: [{text, day, source}]. 有 intent 按它检索 (不门控);
        没有就取近几天的安排和对话摘要里最近确认的几条."""
        if intent.strip():
            saved = self._pending  # 日记本的 intent 不是主人的话, 不登记也不盖掉原来的
            try:
                hits = self.recall(intent)["hits"]
            finally:
                self._pending = saved
            with self._lock:
                by_text = {c["text"][:40]: c for c in self._chunks}
            out = []
            for h in hits[:limit]:
                c = by_text.get(h["text"], {})
                out.append({"text": c.get("text", h["text"]), "day": f"{c['said']:%Y-%m-%d}" if c.get("said") else None,
                            "source": h["file"]})
            if out:
                return out
        # 没有 intent, 或者按它什么都没想起来: 近几天的安排 + 对话摘要里最近确认的 (一个月以内)
        today = self.clock().date()
        out = [{"text": t, "day": None, "source": f"{self.char}/记忆/{SUMMARY_FILE}"} for t in self._upcoming()]
        with self._lock:
            self._refresh()
            recent = [c for c in self._chunks if c["file"] == f"{self.char}/记忆/{SUMMARY_FILE}" and c.get("said")]
        for c in sorted(recent, key=lambda c: c["said"], reverse=True):
            text = O.with_time(c["text"], c["said"], today)
            if len(out) >= limit or (today - c["said"]).days > 30:
                break
            if all(x["text"] != text for x in out):
                out.append({"text": text, "day": f"{c['said']:%Y-%m-%d}", "source": c["file"]})
        return out[:limit]

    def write_diary(self, mine: list[str] | None = None, opening: bool = True) -> str:
        """一天一篇 (日记/日期.md). 开头一段"今天你…"按今天的日志拼 (固定的说法, 不让模型生成: 0.8B 写摘要会照抄、会编),
        每次写都改这一段, 不往下追加: 小屋里她每次去书桌都写, 以前一天追加十几行差不多的
        (2026-10-25 用户: "为什么ai反复在记录这个作为日记"). mine 是她今天自己的事 ("14:25 看了「…」: 我觉得…",
        QQ 渠道里她过日子、看完视频写的, 管理器给), 记在"## 我自己的"下面, 只添新的, 按时间排 (补记的也归到该在的位置).
        opening: 小屋里翻日记本时 True (开头那段是写日记的本意, 哪怕没聊也写"今天你还没跟我说话呢");
        从 QQ 侧只记她自己的反思时传 False, 当天桌宠上没互动、也没写过开头就不硬拼那句 (免得误导).
        返回这次写的: 添了"我自己的"就是最新那条 (去掉时间), 没有就是开头那段.
        日记是她的输出, 不参与检索 (AI 写的东西不能自己变成记忆, 和候选要确认是一个道理)."""
        now = self.clock()
        day = f"{now:%Y-%m-%d}"
        with self._lock:
            self._refresh()
            log_path = self.dir / "日志" / f"{day}.md"
            entries = self._entries(log_path) if log_path.exists() else []
            facts = [O.fact_of_line(x) for x in (self.dir / "记忆" / "关于用户.md").read_text(encoding="utf-8").splitlines()
                     if f"({day})" in x and O.fact_of_line(x)]
            topics, g = [], self._graph  # 今天提到的人、宠物、地点 (日志里的说法, 图谱里有就用实体的名字)
            for e in entries:
                for w in sorted(G.mentions(e["user"], g.names)):
                    label = g.label(g.root[w]) if w in g.root else w
                    if label not in topics:
                        topics.append(label)
            room = _room_diary(self._room_events(log_path)) if log_path.exists() else []  # 小屋里你做过的事
            meta, _ = self._relation()
        if not entries:
            # 没说话但在小屋里摸过她、给她带过吃的: "今天你摸了摸我的头，还给我带了零食。"
            text = f"今天你{'、'.join(room[:-1]) + '，还' if len(room) > 1 else ''}{room[-1]}。" if room else "今天你还没跟我说话呢。"
        else:
            text = f"今天你跟我说了 {len(entries)} 句话"
            text += f"，聊到了{'、'.join(topics[:3])}。" if topics else "。"
            if facts:
                text += "你说" + "，".join(f.replace("用户", "你", 1) for f in facts[:2]) + "，我记下来了。"
            if room:
                text += f"你还{'、'.join(room[:-1]) + '，' if len(room) > 1 else ''}{room[-1]}。"
            if meta.get("mood_date") == day and meta.get("mood") in ("开心", "低落"):
                text += "你今天好像挺开心的。" if meta["mood"] == "开心" else "你今天好像有点累，早点休息吧。"
        path = self.dir / "日记" / f"{day}.md"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            old = path.read_text(encoding="utf-8").splitlines() if existed else [f"# {day} 的日记"]
            had_open = any(ln.startswith("今天你") for ln in old)
            # 以前追加的"- 14:25 今天你…" (一天十几行差不多的) 换成开头这一段; 你在 Obsidian 里写的别的行照旧留着
            lines = [ln for ln in old if not _DIARY_OLD.match(ln) and not ln.startswith("今天你")]
            if opening or entries or room or had_open:  # QQ 侧只记反思、当天桌宠上没互动也没写过开头: 不硬拼那句
                at = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("# ")), 0)
                lines[at:at] = ["", text, ""] if at < len(lines) and lines[at] != "" else ["", text]
            added = []
            if mine:
                had = {ln[len("- 00:00 "):] for ln in lines if _DIARY_MINE.match(ln)}
                for m in mine:
                    hm, _, said = m.partition(" ")
                    said = " ".join(said.split())[:300]
                    if re.fullmatch(r"\d{2}:\d{2}", hm) and said and said not in had:
                        added.append(f"- {hm} {said}")
                        had.add(said)
                if added and MINE_HEAD not in lines:
                    lines += ["", MINE_HEAD]
                if added:
                    head = lines.index(MINE_HEAD)
                    i = head + 1
                    while i < len(lines) and lines[i].startswith("- "):
                        i += 1
                    lines[i:i] = added
            if MINE_HEAD in lines:
                # 补记、看视频想久一点才写回来的, 时间可能比已经记下的早: "我自己的"这一段按时间排好, 回头翻就是一天的顺序
                head = lines.index(MINE_HEAD)
                end = next((j for j in range(head + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
                slots = [j for j in range(head + 1, end) if _DIARY_MINE.match(lines[j])]
                for j, ln in zip(slots, sorted((lines[j] for j in slots), key=lambda x: x[2:7])):
                    lines[j] = ln
            new = re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip("\n")) + "\n"
            if not path.exists() or new != path.read_text(encoding="utf-8"):
                self._write(path, new)
        return added[-1][len("- 00:00 "):] if added else text

    def append_watch(self, heading: str, lines: list[str]) -> Path:
        """陪看完记一节到 <角色>/记忆/一起看过的视频.md (2026-11-08 用户: "刷视频，也要整理到记忆、知识库中，特别是交互中
        重要的内容"): heading 是这一节的标题 ("2026-11-08 03:17 《…》"), lines 是正文 (每行一条). 只往后加, 整份写
        (先写临时文件再换上去, 上一版留在 .backup/). 这个文件参与检索, 以后能想起"上次一起看的那个"."""
        path = self.dir / "记忆" / WATCH_FILE
        body = [ln.rstrip() for ln in lines if ln and ln.strip()]
        with self._lock:
            try:
                old = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                old = _WATCH_HEAD
            self._write(path, old.rstrip("\n") + f"\n\n## {heading.strip()}\n" + "\n".join(body) + "\n")
        return path

    def day_mine(self, day: str = "") -> list[str]:
        """某天日记"## 我自己的"下面她自己写的几条 (去掉"- HH:MM "), 当天没有就空. 给"每天提炼一句理解"当原料."""
        day = day or f"{self.clock():%Y-%m-%d}"
        path = self.dir / "日记" / f"{day}.md"
        if not path.exists():
            return []
        out, inside = [], False
        with self._lock:
            for ln in path.read_text(encoding="utf-8").splitlines():
                if ln.startswith("## "):
                    inside = ln.strip() == MINE_HEAD
                elif inside and _DIARY_MINE.match(ln):
                    out.append(ln[len("- 00:00 "):])
        return out

    def understanding(self, limit: int = 0) -> list[str]:
        """她这阵子攒下来的"我的理解" (日记/我的理解.md 里的几句, 只取正文, 旧的在前). 载入她的上下文、让她过日子时带上.
        是她自己的输出, 和日记一样不进检索."""
        path = self.dir / "日记" / UNDERSTAND_FILE
        if not path.exists():
            return []
        with self._lock:
            lines = [m.group(1).strip() for ln in path.read_text(encoding="utf-8").splitlines()
                     if (m := _UNDERSTAND_LINE.match(ln)) and m.group(1).strip()]
        return lines[-limit:] if limit else lines

    def add_understanding(self, text: str, source: str = "", day: str = "") -> str:
        """给"我的理解"添一句 (她想通了、攒下来的一点理解). 一行一句, 带日期和来源 ("- (2026-10-25, 看视频) …"),
        方便你在 Obsidian 里看、删 (删一行就是回退这一条). 和已有的太像 (去标点后一样, 或一句包着另一句) 就不添, 返回空.
        每天最多添一句由管理器那边把关 (它一天只提炼一次); 这里只管存和去重. 返回添进去的正文, 没添返回空."""
        said = " ".join(str(text or "").split())[:200]
        if len(_norm_u(said)) < 4:
            return ""
        day = day or f"{self.clock():%Y-%m-%d}"
        path = self.dir / "日记" / UNDERSTAND_FILE
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.read_text(encoding="utf-8").splitlines() if path.exists() else [UNDERSTAND_HEAD, ""]
            have = [m.group(1).strip() for ln in old if (m := _UNDERSTAND_LINE.match(ln))]
            n = _norm_u(said)
            if any(n == _norm_u(h) or (len(n) and (n in _norm_u(h) or _norm_u(h) in n)) for h in have):
                return ""
            line = f"- ({day}{', ' + source if source else ''}) {said}"
            if not old or old[0] != UNDERSTAND_HEAD:
                old = [UNDERSTAND_HEAD, ""] + old
            old.append(line)
            new = re.sub(r"\n{3,}", "\n\n", "\n".join(old).strip("\n")) + "\n"
            self._write(path, new)
        return said

    def remove_understanding(self, source: str) -> int:
        """删掉来源是 source 的"我的理解" (行首是 "- (日期, source) "; 重置小窝学习时删 "小窝" 的). 别的来源、没标来源的不碰.
        返回删了几条."""
        path = self.dir / "日记" / UNDERSTAND_FILE
        mark = re.compile(r"- \(\d{4}-\d{2}-\d{2}, " + re.escape(source) + r"\)\s")
        with self._lock:
            if not path.exists():
                return 0
            old = path.read_text(encoding="utf-8").splitlines()
            keep = [ln for ln in old if not mark.match(ln)]
            if len(keep) != len(old):
                self._write(path, "\n".join(keep).rstrip("\n") + "\n")
            return len(old) - len(keep)

    def add_howto(self, task: str, note: str, ok: bool = True, day: str = "") -> bool:
        """一件电脑上的事做完, 记一条操作笔记 (日记/操作笔记.md): "- (日期, 成了|没成) 任务 → 做法或踩的坑".
        下次类似的事 howto_for 会翻出来, 她就不用从头摸索 (成了的记做法, 没成的记坑). 最多留 200 条, 超了最早的挪进 日记/归档/操作笔记-年份.md (不删);
        任务和笔记都一样的不重复记. 是她的输出, 和日记一样不进检索. 返回是不是新记的."""
        task = " ".join(str(task or "").split())[:60]
        note = " ".join(str(note or "").split())[:160]
        if len(_norm_u(note)) < 2 or not task:
            return False
        day = day or f"{self.clock():%Y-%m-%d}"
        path = self.dir / "日记" / HOWTO_FILE
        line = f"- ({day}, {'成了' if ok else '没成'}) {task} → {note}"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.read_text(encoding="utf-8").splitlines() if path.exists() else [HOWTO_HEAD, ""]
            if any(ln.endswith(f"{task} → {note}") for ln in old):
                return False
            old.append(line)
            rows = [ln for ln in old if ln.startswith("- ")]
            if len(rows) > HOWTO_MAX:  # 超了: 最早的挪进归档 (写不上就这次先不挪, 宁可多留不丢)
                drop = set(rows[:len(rows) - HOWTO_MAX])
                if self._archive_howto([ln for ln in old if ln in drop]):
                    old = [ln for ln in old if ln not in drop]
            self._write(path, "\n".join(old).strip("\n") + "\n")
        return True

    def _archive_howto(self, rows: list[str]) -> bool:
        """超出 HOWTO_MAX 的操作笔记按年份 (行首的日期, 没有就算今年) 追加到 日记/归档/操作笔记-YYYY.md. 写不上返回 False."""
        by_year: dict[str, list[str]] = {}
        for ln in rows:
            y = re.match(r"- \((\d{4})-", ln)
            by_year.setdefault(y.group(1) if y else f"{self.clock():%Y}", []).append(ln)
        try:
            for year, got in by_year.items():
                self._append(self.dir / "日记" / HOWTO_ARCHIVE / f"操作笔记-{year}.md", "\n".join(got) + "\n",
                             head=f"# 操作笔记（归档，{year}）\n\n<!-- 日记/操作笔记.md 超过 {HOWTO_MAX} 条时, 最早的挪到这里 (不删). "
                                  "这里的不再给任务单翻出来; 还想用就剪回 操作笔记.md. -->\n\n")
        except OSError as exc:
            log.warning("操作笔记归档没写上, 这次先不挪: %s", exc)
            return False
        return True

    def howto_for(self, task: str, limit: int = 3) -> list[str]:
        """和这件事最像的几条操作笔记 (按任务文字和笔记的词重合排, 一条都不沾边就空), 近的、成了的排前面. 给任务单带上."""
        path = self.dir / "日记" / HOWTO_FILE
        if not path.exists() or not str(task or "").strip():
            return []
        with self._lock:
            rows = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("- (")]
        want = {t for t in tokenize(task) if len(t) >= 2 and t not in _STOP}
        scored = []
        for i, row in enumerate(rows):
            have = {t for t in tokenize(row) if len(t) >= 2}
            hit = len(want & have)
            if hit:
                scored.append((hit, "成了" in row[:20], i, row))
        scored.sort(reverse=True)
        return [re.sub(r"^- ", "", r[3]) for r in scored[:limit]]

    def room_event(self, kind: str, what: str, count: int = 1) -> dict:
        """小屋里发生的一件事 (room_event_of 认过的; count: 页面 30 秒内合成一条的次数).
        记进当天日志 ("- 14:02 小屋: 用户摸了摸绫音的头 ×3": 不是对话的格式, 日志整理和日记里的"说了几句话"都不算它);
        亲近分 (关系.md 的 cared, 熟悉程度把它当聊过的轮数) 按 ROOM_EVENTS 加, 每天最多 room_care_daily (cared_today 记今天加了多少),
        一条里戳了 TEASE 下以上是在逗她, 不加. 返回 {care: 这次加了几分, cared, familiarity}."""
        now = self.clock()
        said, points = ROOM_EVENTS[(kind, what)]
        today = f"{now:%Y-%m-%d}"
        with self._lock:
            day = self.dir / "日志" / f"{today}.md"
            self._append(day, f"- {now:%H:%M} 小屋: 用户{said.format(c=self.char)}{f' ×{count}' if count > 1 else ''}\n",
                         head=f"# {today} 的对话\n\n")
            meta, body = self._relation()
            d, _, n = (meta.get("cared_today") or "").partition(" ")
            used = int(n or 0) if d == today else 0
            care = 0 if kind == "poke" and count >= TEASE else min(points, max(0, self.care_daily - used))
            meta["cared"] = str(int(meta.get("cared") or 0) + care)
            meta["cared_today"] = f"{today} {used + care}"
            meta["last_seen"] = now.strftime("%Y-%m-%d %H:%M")
            meta["familiarity"] = familiarity(meta, now)
            self._write_relation(meta, body)
        return {"care": care, "cared": int(meta["cared"]), "familiarity": meta["familiarity"]}

    def _room_events(self, log_path: Path) -> list[tuple[str, str, int]]:
        """一天日志里小屋的事: [(种类, 部位或东西, 次数)]. 只认 ROOM_EVENTS 的说法 (在 Obsidian 里改过的行就不认了)."""
        known = {said.format(c=c): key for c in (*self.old_chars, self.char) for key, (said, _) in ROOM_EVENTS.items()}
        out = []
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = _ROOM_LINE.match(line.strip())
            if m and m.group(1) in known:
                out.append((*known[m.group(1)], int(m.group(2) or 1)))
        return out

    def save_doc(self, name: str, kind: str, text: str | None = None) -> str:
        """从桌面拖进小屋的文件存到 资料/<名字>.md, 返回相对库根目录的路径. 名字去掉路径和怪字符; 同名的加 (2)、(3).
        文本类最多存 20000 字; 图片、PDF 只记名字. 内容是外面来的文字, 只当数据存, 不执行、不当指令."""
        now = self.clock()
        base = str(name or "").replace(chr(92), "/").split("/")[-1]  # 去掉路径 (Windows 的反斜杠也算)
        stem = re.sub(r"[^0-9A-Za-z一-鿿 _().-]", "", base.rsplit(".", 1)[0] if "." in base else base).strip(" .")[:40] or "资料"
        folder = self.dir / "资料"
        with self._lock:
            folder.mkdir(parents=True, exist_ok=True)
            path, n = folder / f"{stem}.md", 2
            while path.exists():
                path, n = folder / f"{stem} ({n}).md", n + 1
            body = (text or "")[:20000] if kind in ("md", "txt") else f"({kind}, 只记下了名字, 内容没读)"
            head = ["---", "来源: 拖进小屋", f"原名: {base[:80]}", f"类型: {kind}", f"时间: {now:%Y-%m-%d %H:%M}", "---", ""]
            self._write(path, chr(10).join(head) + body.rstrip() + chr(10), backup=False)
        return path.relative_to(self.root).as_posix()

    # ------------------------------------------------------------------ 日志摘要 (wm/digest.py 在空闲时调用)
    _ENTRY = re.compile(r"- (\d{2}:\d{2}) 用户: (.*?) → [^:：]+[:：] (.*?)(?: \(动作: ([^)]*)\))?$")

    def _log_days(self) -> list[Path]:
        return sorted(p for p in (self.dir / "日志").glob("*.md") if _DAY.fullmatch(p.stem))

    def _entries(self, path: Path) -> list[dict]:
        out = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = self._ENTRY.match(line.strip())
            if m:
                out.append({"time": m.group(1), "user": m.group(2), "reply": m.group(3), "acts": m.group(4)})
        return out

    def _log_end(self) -> str:
        days = self._log_days()
        return f"{days[-1].stem} {len(self._entries(days[-1]))}" if days else ""

    def pending_log(self, batch: int = 12, since: str | None = None) -> tuple[str, int, list[dict]] | None:
        """还没整理过的日志: (日期, 这一批从第几条开始, 条目). 进度记在 关系.md 的 digested: "日期 条数";
        since ("日期 条数") 不为空时用它代替记下的进度 (试跑用)."""
        with self._lock:
            meta, _ = self._relation()
            done_day, _, done_n = (since or meta.get("digested") or "").partition(" ")
            for path in self._log_days():
                if path.stem < done_day:
                    continue
                start = int(done_n or 0) if path.stem == done_day else 0
                entries = self._entries(path)
                if len(entries) > start:
                    return path.stem, start, entries[start:start + batch]
        return None

    def add_summary(self, day: str, facts: list[dict], upto: int) -> list[str]:
        """整理出的事实追加到 记忆/对话摘要.md (按日期分节, 带来源), 进度推进到 day 的第 upto 条.
        只追加, 不改已有的行: 用户在 Obsidian 里改过、删过的不会被写回来 (整理过的日志不会再整理)."""
        path = self.dir / "记忆" / SUMMARY_FILE
        written = []
        with self._lock:
            text = path.read_text(encoding="utf-8") if path.exists() else _SUMMARY_HEAD
            known = text + (self.dir / "记忆" / "关于用户.md").read_text(encoding="utf-8")
            lines = []
            for f in facts:
                if f["text"] in known:
                    continue
                prev = next((O.fact_of_line(ln) for ln in known.splitlines()
                             if near_dup(f["text"], O.fact_of_line(ln) or "")), None) if _rules2_on() else None
                if prev:   # observe 已经记过同一件事了, 摘要层不再记第二遍
                    continue
                lines.append(f"- {f['text']} (来源: [[{day}]] {', '.join(f['times'])}) #auto-summary")
                written.append(f["text"])
                known += f["text"]
            for t in written:  # 同一个实体的同一个属性换了值 ("猫改名叫豆包了"): 旧的那行划掉
                self._strike(lambda old, t=t: old != t and (O.supersedes(t, old) or G.supersedes(t, old)), f"{day} 更正为: {t}")
            for t in written:  # S2 兜底: 旧规则漏掉的改口/改期
                self._supersede_write(t, day)
            if lines:
                text = path.read_text(encoding="utf-8") if path.exists() else _SUMMARY_HEAD
                self._write(path, _insert_section(text, day, lines))
            meta, body = self._relation()
            meta["digested"] = f"{day} {upto}"
            self._write_relation(meta, body)
        return written

    def summary_known(self) -> str:
        """已经记下的和已经提过的事实 (规则提取的 + 摘要 + 候选, 含用户不要的), 给整理时去重用."""
        parts = []
        for name in ("关于用户.md", SUMMARY_FILE, CANDIDATE_FILE):
            try:
                parts.append((self.dir / "记忆" / name).read_text(encoding="utf-8"))
            except OSError:
                pass
        return "\n".join(parts)

    # ------------------------------------------------------------------ 候选 (日志整理出来、等用户确认的)
    def propose(self, day: str, facts: list[dict], upto: int) -> tuple[list[str], list[str]]:
        """日志整理出的事实 -> (写下的, 这一天候选满了没写的). 候选模式写进 候选.md 等用户确认; auto 模式直接进 对话摘要.md.
        每天最多 daily_max 条; 开了 memory.relay.urgent_extra 的话, 名额满了以后很要紧的 (显著性到 urgent_min) 还能多进几条, 标 #要紧."""
        if not self.confirm:
            return self.add_summary(day, facts, upto), []
        if self.relay:  # 每天名额有限: 这一批里先挑显著的 (健康、大事、你让她记住的…); 一样显著的照先后
            facts = sorted(facts, key=lambda f: -R.salience(f["text"])[0])
        path = self.dir / "记忆" / CANDIDATE_FILE
        written, skipped = [], []
        with self._lock:
            text = path.read_text(encoding="utf-8") if path.exists() else _CAND_HEAD
            known = text + self.summary_known()
            _, sep, rest = text.partition(f"## {day}\n")
            had = [x for x in rest.split("\n## ")[0].splitlines()
                   if x.startswith("- ") and PRIVATE_TAG not in x] if sep else []  # 这一天整理已经提过的 (私聊里说的另算)
            extra = sum(URGENT_TAG in x for x in had)  # 其中名额满了以后加塞的 (不占名额)
            n = len(had) - extra
            lines = []
            for f in facts:
                if f["text"] in known:
                    continue
                tag = ""
                if n >= self.daily_max:
                    if extra >= self.urgent_extra or round(R.salience(f["text"])[0], 2) < self.urgent_min:
                        skipped.append(f["text"])
                        continue
                    tag, extra = f" {URGENT_TAG}", extra + 1
                else:
                    n += 1
                lines.append(f"- [ ] {f['text']} (来源: [[{day}]] {', '.join(f['times'])}) #候选{tag}")
                written.append(f["text"])
                known += f["text"]
            if lines:
                self._write(path, _insert_section(text, day, lines))
            meta, body = self._relation()
            meta["digested"] = f"{day} {upto}"
            self._write_relation(meta, body)
        if written:
            self._notify()
        return written, skipped

    def propose_said(self, text: str, via: str) -> list[str]:
        """你在 QQ / 微信私聊里自己打的字 (via: "QQ 私聊" / "微信"; 引用、转发、卡片、图片不算, 渠道管理器只给你打的那部分):
        用和桌宠上当面说一样的规则认出"我叫…""我喜欢…""我住在…", 不管 confirm.mode 设成什么都只进 候选.md 等你确认
        (渠道方案 5.1: 本人·远程"只进候选" —— 号可能被盗、消息经非官方协议转来, 你不在电脑前没法当面确认).
        确认以后和当面说的一样记进 关于用户.md (_confirm_said). 已经记着的、已经在候选里的、你点过"不要"的不再提;
        "帮我把…忘掉"这里不动你的记忆 (远程不能删, 去设置的记忆页). 每天最多提 daily_max 条. 返回这次提的."""
        own = R.own_part(text) if self.relay else text  # 转述别人的话不当成你的事
        if O.forget_target(own):
            return []
        now = self.clock()
        found = [(kind, settle_fact(kind, fact, now.date())) for kind, fact in extract_facts(own)]
        with self._lock:
            self.affect.note(text, via, now, [f for _, f in found])  # 私聊里你自己打的字里的情绪 (阶段 4)
        if not found:
            return []
        day, path = f"{now:%Y-%m-%d}", self.dir / "记忆" / CANDIDATE_FILE
        written = []
        with self._lock:
            cand = path.read_text(encoding="utf-8") if path.exists() else _CAND_HEAD
            known = _candidate_texts(cand)
            for name in ("关于用户.md", SUMMARY_FILE):
                try:
                    known |= {f for f in map(O.fact_of_line, (self.dir / "记忆" / name).read_text(encoding="utf-8").splitlines()) if f}
                except OSError:
                    pass
            _, sep, rest = cand.partition(f"## {day}\n")
            n = sum(1 for x in rest.split("\n## ")[0].splitlines() if x.startswith("- ") and PRIVATE_TAG in x) if sep else 0
            lines = []
            for kind, fact in found:
                if fact in known or n + len(lines) >= self.daily_max:
                    continue
                lines.append(f"- [ ] {fact} (来源: {via} [[{day}]] {now:%H:%M}) #候选 #{kind} {PRIVATE_TAG}")
                written.append(fact)
                known.add(fact)
            if lines:
                self._write(path, _insert_section(cand, day, lines))
                self._cand_sig = path.stat().st_mtime_ns
        if written:
            self._notify()
        return written

    def propose_qq(self, day: str, facts: list[dict], via: str = "QQ 私聊") -> tuple[list[str], list[str]]:
        """QQ / 微信私聊里主人一整天说过的话, 经日志整理那道把关 (wm/owner_qq.py) 挑出来 -> 候选.md, 等他确认.
        和 propose 的区别: 不推进 关系.md 的 digested 进度 (那是日志整理的事), 只追加; 每行标 #私聊;
        每天私聊候选最多 daily_max 条 (和整理出来的候选分开数). 返回 (写下的, 名额满了没写的)."""
        path = self.dir / "记忆" / CANDIDATE_FILE
        written, skipped = [], []
        with self._lock:
            text = path.read_text(encoding="utf-8") if path.exists() else _CAND_HEAD
            known = text + self.summary_known()
            head, sep, rest = text.partition(f"## {day}\n")
            had = [x for x in rest.split("\n## ")[0].splitlines()
                   if x.startswith("- ") and PRIVATE_TAG in x] if sep else []
            n = len(had)
            lines = []
            for f in facts:
                if f["text"] in known:
                    continue
                if n + len(lines) >= self.daily_max:
                    skipped.append(f["text"])
                    continue
                lines.append(f"- [ ] {f['text']} (来源: {via} [[{day}]] {', '.join(f['times'])}) #候选 {PRIVATE_TAG}")
                written.append(f["text"])
                known += f["text"]
            if lines:
                self._write(path, _insert_section(text, day, lines))
        if written:
            self._notify()
        return written, skipped

    def candidates(self) -> list[dict]:
        """还没确认的候选: [{id, text, day, source, days}], 按文件里的顺序 (旧的在前). days: 在几个不同的日子提到过 (2 以上是常提)."""
        try:
            lines = (self.dir / "记忆" / CANDIDATE_FILE).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rec = self._recall_dates()
        out = []
        for line in lines:
            m = _CAND.match(line.strip())
            if not m or m.group(1) != " " or m.group(2).startswith("~~"):
                continue
            text = _clean_fact(m.group(2))
            day, src = _LINK_DAY.search(m.group(2)), re.search(r"[（(]来源[:：]\s*([^)）]*)[)）]", m.group(2))
            out.append({"id": candidate_id(text), "text": text, "day": day.group(1) if day else None,
                        "source": src.group(1).replace("[[", "").replace("]]", "").strip() if src else None,
                        "private": PRIVATE_TAG in line, "urgent": URGENT_TAG in line,
                        "days": max(1, len(rec.get(text, {}).get("days", [])))})
            out[-1]["salience"], out[-1]["why"] = R.salience(text, out[-1]["days"])  # 为什么觉得重要 (记忆页、候选卡片)
        return out

    def affect_delete(self, eid: str) -> bool:
        """记忆页上点删一条情绪事件 (你的决定, 真删)."""
        with self._lock:
            return self.affect.delete(eid)

    def affect_clear(self) -> int:
        """情绪事件整个清空, 返回删了几条."""
        with self._lock:
            return self.affect.clear()

    def _mood_seed(self) -> dict:
        """她心情的基线、回落快慢、起床时刻: 从出生种子读 (情绪-1、情绪-2、习惯-1), 读不到用默认."""
        def values(sid):
            return (MS.active(self.char, sid, self.seed_mode, self.seed_off) or {}).get("values") or {}
        base, back, wake = values("seed:affect:1"), values("seed:affect:2"), values("seed:habit:1")
        return {"mood_base": {"valence": float(base.get("valence", 0.2)), "arousal": float(base.get("arousal", 0.4))},
                "half_life_h": float(back.get("mood_half_life_h", 12)), "wake": str(wake.get("wake", "07:00"))}

    def overview(self) -> dict:
        """设置里的"记忆"页 (2026-11-01 用户要"记忆看得见"): 她记得关于你的每一条 —— 哪来的 (桌宠上聊天时你直接说的 chat /
        QQ·微信私聊里说的、你确认过的 private / 日志整理的 digest / 你自己写的 you)、长期还是事件、哪天说的、上次什么时候想起、在几天里提到过、改过或忘掉了
        没有 (划掉的行和注明的原因); 等你确认的候选、她自己的理解、关系概况、最近几轮想起了什么. 只读, 就是 Obsidian 里那几份文件."""
        facts: list[dict] = []
        understanding: list[dict] = []
        with self._lock:
            rec = self._recall_dates()
            for name, origin in (("关于用户.md", "facts"), (SUMMARY_FILE, "summary")):
                try:
                    lines = (self.dir / "记忆" / name).read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                day = None  # 对话摘要按"## 日期"分节, 行里没写日期就算那一节的
                for raw in lines:
                    line = raw.strip()
                    if head := re.match(r"##\s*(\d{4}-\d{2}-\d{2})", line):
                        day = head.group(1)
                        continue
                    if not line.startswith("- ") or line.startswith("- ["):
                        continue
                    tags = re.findall(r"#\S+", line)
                    struck = re.match(r"- ~~(.+?)~~\s*(.*)$", line)
                    body, note = (struck.group(1), re.sub(r"\s*#\S+", "", struck.group(2)).strip()) if struck else (line[2:], "")
                    text = O.fact_of_line("- " + body)
                    if not text:
                        continue
                    said = re.search(r"[（(](\d{4}-\d{2}-\d{2})[)）]", body) or _LINK_DAY.search(body)
                    src = re.search(r"[（(]来源[:：]\s*([^)）]*)[)）]", body)
                    who = ("private" if PRIVATE_TAG in tags else "chat" if "#auto-memory" in tags
                           else "digest" if "#auto-summary" in tags else "you")
                    seen = rec.get(text, {})
                    facts.append({
                        "text": text, "file": name, "origin": who,
                        "status": "active" if not struck else "forgotten" if "忘" in note else "corrected" if "更正" in note else "struck",
                        "note": note, "said": said.group(1) if said else day,
                        "source": src.group(1).replace("[[", "").replace("]]", "").strip() if src else None,
                        "kind": O.memory_kind(text, " ".join(tags), origin if who != "you" else "note"),
                        "tags": [t[1:] for t in tags if t not in ("#auto-memory", "#auto-summary")],
                        "last": seen.get("last"), "days": len(seen.get("days") or []),
                        **({"strength": self.strength.view(text)} if self.strength.mode != "off" else {}),
                        **self._supersede_view(text)})
            try:
                for ln in (self.dir / "日记" / UNDERSTAND_FILE).read_text(encoding="utf-8").splitlines():
                    if u := re.match(r"- \((\d{4}-\d{2}-\d{2})(?:,\s*([^)]*))?\)\s*(.+)$", ln.strip()):
                        understanding.append({"day": u.group(1), "source": u.group(2) or "", "text": u.group(3).strip()})
            except OSError:
                pass
            try:
                meta, _ = self._relation()
            except OSError:
                meta = {}
            candidates = self.candidates()
        keep = ("user_name", "first_met", "last_seen", "sessions", "turns", "familiarity", "mood", "cared")
        met = int(meta.get("sessions") or 0) > 1 or int(meta.get("turns") or 0) > 0  # 见过主人了: 只给新用户的种子用不上
        return {"facts": facts, "candidates": candidates, "understanding": understanding,
                "relation": {k: meta[k] for k in keep if meta.get(k)}, "recalls": list(self.recalls)[::-1],
                "dir": str(self.dir), "seeds": MS.view(self.char, self.seed_mode, self.seed_off, met),
                "affect": self.affect.view(self.clock(), **self._mood_seed())}

    def forget(self, text: str) -> list[str]:
        """设置里点"忘掉": 程序写的、正文正好是 text 的那行划掉, 注明"用户要求忘掉 (在设置里)" (和你当面说"帮我忘掉"一样: 以后问到她答"你让我忘掉了";
        Obsidian 里看得到、想恢复把划线去掉就行), 检索不再用它. 你自己写的行不动. 返回划掉的."""
        text = str(text or "").strip()
        if not text:
            return []
        with self._lock:
            return self._strike(lambda f: f == text, f"{self.clock():%Y-%m-%d} 用户要求忘掉 (在设置里)")

    def mentioned(self, day: str, texts: list[str]) -> dict:
        """日志整理发现用户在 day 又说到了已有的记忆或候选 (texts 是那几条的正文): 各记一天.
        候选在两个以上不同的日子提到过就标上 #常提 (方便你决定); 确认过的事件够 promote_days 天就升成长期.
        划掉的 (更正过、用户不要的) 不算. 返回 {"counted", "promoted", "tagged"}."""
        out = {"counted": [], "promoted": [], "tagged": []}
        if not texts:
            return out
        with self._lock:
            cands = {c["text"]: c["day"] for c in self.candidates()}
            summary = {}  # 对话摘要里程序写的、没划掉的: 正文 -> 说的那天
            try:
                for line in (self.dir / "记忆" / SUMMARY_FILE).read_text(encoding="utf-8").splitlines():
                    fact = O.fact_of_line(line)
                    if fact and O.is_auto(line):
                        m = _LINK_DAY.search(line)
                        summary[fact] = m.group(1) if m else None
            except OSError:
                pass
            due, tag = [], []
            for t in dict.fromkeys(texts):
                if t in cands:
                    if self._touch(t, day, cands[t]) >= 2:
                        tag.append(t)
                elif t in summary:
                    if self._touch(t, day, summary[t]) >= self.promote_days > 0 and self._strong(t):
                        due.append(t)
                    self.strength.reinforce(t, "repeat", day, self._eta_mult(t))  # 日志整理发现你又说到了 (复述)
                else:
                    continue
                out["counted"].append(t)
            if out["counted"]:
                out["promoted"] = self._promote(due) if due else []
                out["tagged"] = self._tag_candidates(tag) if tag else []
                self._save_recalled()
        if out["tagged"]:
            self._notify()  # 候选列表里的"提到过几天"变了
        return out

    def _tag_candidates(self, texts: list[str]) -> list[str]:
        """候选行加上 #常提 (持锁调用). 返回加了的."""
        path = self.dir / "记忆" / CANDIDATE_FILE
        lines = path.read_text(encoding="utf-8").splitlines()
        want, done = set(texts), []
        for i, line in enumerate(lines):
            m = _CAND.match(line.strip())
            if m and m.group(1) == " " and "#常提" not in line and _clean_fact(m.group(2)) in want:
                lines[i] = line.rstrip() + " #常提"
                done.append(_clean_fact(m.group(2)))
        if done:
            self._write(path, "\n".join(lines) + "\n")
            self._cand_sig = path.stat().st_mtime_ns
        return done

    def review(self, cid: str, ok: bool) -> dict:
        """确认 (ok) 或不要一条候选. 确认的移到 对话摘要.md; 不要的在 候选.md 里划掉 (以后整理到同样的事不再提)."""
        path = self.dir / "记忆" / CANDIDATE_FILE
        with self._lock:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for i, line in enumerate(lines):
                m = _CAND.match(line.strip())
                if m and not m.group(2).startswith("~~") and candidate_id(_clean_fact(m.group(2))) == cid:
                    break
            else:
                return {"ok": False, "id": cid, "error": "没有这条候选 (可能已经确认或删掉了)"}
            text = _clean_fact(m.group(2))
            if ok:
                self._confirm([m.group(2)])
                del lines[i]
            else:
                lines[i] = O.strike("- " + m.group(2), f"{self.clock():%Y-%m-%d} 用户不要")
            self._write(path, "\n".join(lines) + "\n")
            self._cand_sig = path.stat().st_mtime_ns
        self._notify()
        return {"ok": True, "id": cid, "text": text, "confirmed": ok, "left": len(self.candidates())}
    def _confirm(self, raws: list[str]) -> None:
        """候选行 (去掉复选框的部分) 移进 对话摘要.md 那一天的一节 (持锁调用); 私聊里你说的 (#私聊) 交给 _confirm_said.
        确认也算想起了一次, 鲜活度从今天算 (不算"提到过的一天"); 当候选时已经常提够 promote_days 天的直接记成长期."""
        said = [r for r in raws if PRIVATE_TAG in r]
        if said:
            self._confirm_said(said)
            raws = [r for r in raws if PRIVATE_TAG not in r]
            if not raws:
                return
        path = self.dir / "记忆" / SUMMARY_FILE
        today = f"{self.clock():%Y-%m-%d}"
        for raw in raws:  # 确认的事更正了已有的 ("猫改名叫豆包了"): 先把旧的那行划掉, 再读文件
            fact = _clean_fact(raw)
            self._strike(lambda old, t=fact: old != t and (O.supersedes(t, old) or G.supersedes(t, old)), f"{today} 更正为: {fact}")
        text = path.read_text(encoding="utf-8") if path.exists() else _SUMMARY_HEAD
        for raw in raws:
            day, fact = _LINK_DAY.search(raw), _clean_fact(raw)
            n = self._touch(fact, today, day.group(1) if day else None, count=False)
            self.strength.reinforce(fact, "confirm", today, self._eta_mult(fact))  # 你确认了
            tags = " #auto-summary #已确认"
            if (n >= self.promote_days > 0 and "#事件" not in raw and not O.has_date(fact) and self._strong(fact)
                    and O.memory_kind(fact, raw, "summary") == O.EVENT):
                tags += " #长期" if "#常提" in raw else " #长期 #常提"
            text = _insert_section(text, day.group(1) if day else today,
                                   ["- " + re.sub(r"\s*#候选", "", raw).rstrip() + tags])
        self._write(path, text)
        self._save_recalled()

    def _confirm_said(self, raws: list[str]) -> None:
        """确认了的私聊候选: 和桌宠上当面说的一样记进 关于用户.md —— 长期, 同一个槽换了值旧的那行划掉, 名字记进关系,
        QQ / 微信私聊时也认得 (owner_profile 只读这份); 行里留着来源和 #私聊, 记忆页上分得清 (持锁调用)."""
        path = self.dir / "记忆" / "关于用户.md"
        today = f"{self.clock():%Y-%m-%d}"
        meta = body = None
        for raw in raws:
            fact = _clean_fact(raw)
            if not fact:
                continue
            self._strike(lambda old, t=fact: old != t and (O.supersedes(t, old) or G.supersedes(t, old)), f"{today} 更正为: {fact}")
            if fact in {O.fact_of_line(x) for x in path.read_text(encoding="utf-8").splitlines()}:
                continue
            kind = next((k for k in ("名字", "偏好", "事实") if f"#{k}" in raw), "事实")
            day, src = _LINK_DAY.search(raw), re.search(r"[（(]来源[:：]\s*([^)）]*)[)）]", raw)
            via = f" (来源: {src.group(1).strip()})" if src else ""
            self._append(path, f"- {fact} ({day.group(1) if day else today}){via} #{kind} #auto-memory {PRIVATE_TAG} #已确认\n")
            self.strength.reinforce(fact, "confirm", today, self._eta_mult(fact))
            if kind == "名字":
                if meta is None:
                    meta, body = self._relation()
                meta["user_name"] = fact.removeprefix("用户叫")
        if meta is not None:
            self._write_relation(meta, body)

    def _sync_candidates(self) -> bool:
        """用户在 Obsidian 里勾上 [x] 的候选转正 (移到 对话摘要.md); 返回有没有转正的. _refresh 发现 候选.md 改过才调 (持锁)."""
        path = self.dir / "记忆" / CANDIDATE_FILE
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        done = [i for i, x in enumerate(lines)
                if (m := _CAND.match(x.strip())) and m.group(1) in "xX" and not m.group(2).startswith("~~")]
        if done:
            self._confirm([_CAND.match(lines[i].strip()).group(2) for i in done])
            self._write(path, "\n".join(x for i, x in enumerate(lines) if i not in set(done)) + "\n")
            log.info("候选转正 %d 条 (Obsidian 里勾选的)", len(done))
        self._cand_sig = path.stat().st_mtime_ns
        if done:
            self._notify()
        return bool(done)

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                log.exception("通知候选变化失败")
