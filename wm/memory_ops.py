"""记忆的更新、遗忘、时间和联想 (纯规则, wm/memory.py 调用). 对应大脑记忆的几个环节和 2025-2026 的长记忆研究:

  知识更新   同一个"槽" (名字、住处、生日、工作) 只有一个当前值; 偏好"喜欢X"和"不喜欢X"互斥. 新值来了,
             旧的那一行在 Obsidian 里划掉并注明更正日期, 检索不再用它 (Memora 的教训: 现有系统常拿过时的记忆回答).
  主动遗忘   用户说"帮我把…忘掉/别记着…", 把对应的行划掉并注明"用户要求忘掉". 只动程序写的行
             (#auto-memory / #auto-summary), 用户自己写的笔记不动.
  时间       事实里的"10月28日"按当天换算成"明天/3天后/5天前", 近几天的安排在见面第一句就带上 (前瞻记忆).
  联想       事实里的人、宠物、名字作为实体, 命中一条时顺带取出同一实体的其他事实 (HippoRAG/GRAVITY 的简化版:
             只走一步, 保留原句, 不拆成三元组; 拆成实体会丢掉原话措辞).
  问自己     "我叫什么""我住在哪"这类问用户自己固定信息的话, 由服务端按记忆直接答 (0.8B 会把"我"答成角色自己,
             提示词修不好); 没记过就说没告诉过, 不编.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- 槽与知识更新
SLOTS = {
    "名字": re.compile(r"^用户叫(.+)$"),
    "住处": re.compile(r"^用户(?:住在|家在)(.+)$"),
    "生日": re.compile(r"^用户的?生日是(.+)$"),
    "出生年": re.compile(r"^用户是(\d{4})年出生的$"),
    "生肖": re.compile(r"^用户属(.)$"),
    "工作": re.compile(r"^用户(?:在.{0,12}(?:做|上班|工作)|是(?:一名|一个|个)?.{0,8}"
                     r"(?:师|员|策划|医生|老师|学生|工程师|程序员|作家|护士|会计|律师|销售|老板|主播)).*$"),
}
_PREF = re.compile(r"^用户(不)?喜欢(.+?)了?$")


def slot_of(fact: str) -> str | None:
    return next((k for k, p in SLOTS.items() if p.match(fact)), None)


def supersedes(new: str, old: str) -> bool:
    """new 让 old 过时: 同一个槽换了值, 或者对同一样东西从喜欢变成不喜欢 (反过来也是)."""
    if new == old:
        return False
    s = slot_of(new)
    if s and s == slot_of(old):
        return True
    a, b = _PREF.match(new), _PREF.match(old)
    return bool(a and b and a.group(2) == b.group(2) and bool(a.group(1)) != bool(b.group(1)))


def fact_of_line(line: str) -> str | None:
    """记忆文件里的一行 -> 事实正文 (已划掉的、不是列表项的返回 None)."""
    s = line.strip()
    if not s.startswith("- ") or s[2:].startswith("~~"):
        return None
    body = re.sub(r"\s*[（(](?:\d{4}-\d{2}-\d{2}|来源[:：][^)）]*)[)）]", "", s[2:])
    return re.sub(r"#\S+", "", body).strip() or None


def strike(line: str, note: str) -> str:
    """"- 用户住在杭州 (2026-10-20) #事实 #auto-memory" -> "- ~~用户住在杭州 (2026-10-20)~~ 注明 #事实 #auto-memory"."""
    s = line.rstrip("\n")
    tags = " ".join(re.findall(r"#\S+", s))
    body = re.sub(r"\s*#\S+", "", s[2:]).strip()
    return f"- ~~{body}~~ {note}" + (f" {tags}" if tags else "")


def is_auto(line: str) -> bool:
    return "#auto-memory" in line or "#auto-summary" in line


# ---------------------------------------------------------------- 主动遗忘
_FORGET = re.compile(r"(?:把|将)?(.{0,16}?)(?:给我)?(?:忘掉|忘了吧|忘记吧|忘了它|删掉|删了吧|别记(?:着|住)?了?|不要记(?:着|住)?了?)")
_FORGET_AFTER = re.compile(r"(?:别|不要)记(?:着|住)?(.{2,16}?)(?:了|啦)?[。！!]?$")
_FORGOT_SELF = re.compile(r"^(?:我|俺)(?:又|都|也|还|竟然)?忘(?:了|记)")  # "我忘了带钥匙": 用户自己忘了, 不是让角色忘
_IMPERATIVE = re.compile(r"帮我|你|请|麻烦|把|别|不要|给我")
_SLOT_WORDS = {"住处": r"住|家在|城市", "名字": r"名字|叫什么|我叫", "生日": r"生日", "工作": r"工作|上班|职业"}
_HAN = re.compile(r"[一-鿿]")
_FILL = set("的了吧呢啊呀我你他她它把这那个件事一些关于用户在是有")


def forget_target(text: str) -> str | None:
    """用户要角色忘掉什么 ("帮我把我住在哪忘掉吧" -> "我住在哪"). 不是遗忘指令返回 None."""
    t = text.strip()
    if _FORGOT_SELF.search(t) or not _IMPERATIVE.search(t):
        return None
    m = _FORGET_AFTER.search(t) or _FORGET.search(t)  # "别记着我喜欢咖啡的事了" / "帮我把我住在哪忘掉吧"
    if not m:
        return None
    target = re.sub(r"^(?:帮我|请你?|麻烦你?|你)?(?:把|将)?", "", m.group(1)).strip("，, ")
    return target if _HAN.search(target) else None


def forget_matches(target: str, facts: list[str]) -> list[int]:
    """要忘掉的事对应哪几条事实 (下标). 先按槽 ("住在哪" -> 住处), 再按内容字重合 (至少两个实词字)."""
    for slot, words in _SLOT_WORDS.items():
        if re.search(words, target):
            return [i for i, f in enumerate(facts) if slot_of(f) == slot]
    want = {c for c in _HAN.findall(target) if c not in _FILL}
    return [i for i, f in enumerate(facts) if len(want & set(_HAN.findall(f))) >= 2] if len(want) >= 2 else []


# ---------------------------------------------------------------- 时间
_MD = re.compile(r"(\d{1,2})月(\d{1,2})日")


def event_date(text: str, said: date | None) -> date | None:
    """事实里提到的日子 ("10月28日"), 年份按说这话的日期推 (跨年的往后算)."""
    m = _MD.search(text)
    if not m or said is None:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        d = date(said.year, month, day)
    except ValueError:
        return None
    if d < said - timedelta(days=180):
        d = date(said.year + 1, month, day)
    return d


_WEEKDAY = "一二三四五六日天"
_REL_DAY = re.compile(r"大后天|后天|明天|今天|(?<![每逢])(下|这|本)?(?:周|星期|礼拜)([一二三四五六日天])")


def absolute_dates(text: str, said: date) -> str:
    """写入时把相对的日子换成具体日期 ("明天我要去复查" 10月27日说的 -> "10月28日我要去复查"):
    过几天再读, "明天"就不对了. "周五"指这一周还没过的那天, 已经过了就是下周的."""
    def repl(m: re.Match) -> str:
        word = m.group(0)
        if word in ("今天", "明天", "后天", "大后天"):
            d = said + timedelta(days=("今天", "明天", "后天", "大后天").index(word))
        else:
            wd = min(_WEEKDAY.index(m.group(2)), 6)
            ahead = (wd - said.weekday()) % 7
            if m.group(1) == "下":
                ahead = wd - said.weekday() + 7
            d = said + timedelta(days=ahead)
        return f"{d.month}月{d.day}日"
    return _REL_DAY.sub(repl, text)


def relative(d: date, today: date) -> str:
    n = (d - today).days
    return {0: "今天", 1: "明天", 2: "后天", -1: "昨天", -2: "前天"}.get(n, f"{n}天后" if n > 0 else f"{-n}天前")


def with_time(text: str, said: date | None, today: date) -> str:
    """"用户10月28日要去医院复查" -> "用户10月28日要去医院复查 (明天)"; 三天以内的直接说成"用户明天要去医院复查"."""
    d = event_date(text, said)
    if not d:
        return text
    rel = relative(d, today)
    if abs((d - today).days) <= 2:
        return _MD.sub(rel, text, count=1)
    return f"{text.rstrip('。.')} ({rel})"


# ---------------------------------------------------------------- 鲜活度: 长期事实不淡化, 事件越久越难想起
LONG, EVENT = "long", "event"
# 画像、喜好、习惯这类不随时间变的 (日志整理出来的也算长期): "用户周末喜欢去钓鱼"、"用户的仓鼠叫团子"、"用户对花生过敏"
_TRAIT = re.compile(r"喜欢|讨厌|害怕|怕|过敏|不吃|爱吃|吃素|忌口|每天|每周|每月|经常|平时|习惯|总是|左撇子|右撇子|老家|"
                    r"养了|^用户的.{1,8}?(?:是|叫)")


def memory_kind(text: str, tags: str, source: str) -> str:
    """一条记忆是长期 (不淡化) 还是事件 (按鲜活度淡化). tags: 这一行的标签 (#长期 / #事件 手动指定);
    source: facts (关于用户.md, 规则提取的直接陈述) / summary (对话摘要.md) / note (用户自己写的、关系.md) / log."""
    if "#长期" in tags:
        return LONG
    if "#事件" in tags:
        return EVENT
    if source == "note" or slot_of(text):  # 用户手写的不替他淡化; 生日 "3月12日" 带日子但是长期的
        return LONG
    if source == "log" or _MD.search(text):  # 带具体日子的是安排或经历
        return EVENT
    return LONG if source == "facts" or _TRAIT.search(text) else EVENT


def has_date(text: str) -> bool:
    """事实里有没有具体的日子 ("10月28日"): 有的是安排或那天的经历, 跟着日子走, 不会因为常提升成长期."""
    return bool(_MD.search(text))


def age_days(text: str, said: date | None, today: date, recalled: date | None = None) -> int:
    """距这件事过了几天: 从说的那天、事情发生那天 (还没到的安排算 0)、上次被想起那天里取最晚的算."""
    base = said
    d = event_date(text, said)
    if d is not None:
        if d > today:
            return 0
        base = max(base, d) if base else d
    if recalled is not None:
        base = max(base, recalled) if base else recalled
    return max(0, (today - base).days) if base else 0


# 用户在主动回想 ("我之前考试考得怎么样""你还记得…吗"): 被这样提醒时旧事也想得起来, 这一轮不按鲜活度压低
_RECALL_CUE = re.compile(r"之前|以前|上次|那次|那时候|当时|还记得|记不记得|记得吗|说过")


def recall_cue(text: str) -> bool:
    return bool(_RECALL_CUE.search(text))


def freshness(age: int, half_life: float) -> float:
    """鲜活度 0~1: 每过一个半衰期减半. half_life <= 0 时不淡化."""
    return 1.0 if half_life <= 0 else 0.5 ** (age / half_life)


def with_age(text: str, said: date | None, today: date) -> str:
    """没有具体日子的旧事件带上多久以前说的 ("用户最近失眠很严重 (3个月前说的)"), 免得模型当成眼下的事. 一个月以内不带."""
    if said is None or _MD.search(text):
        return text
    n = (today - said).days
    if n < 30:
        return text
    when = "一年多前" if n >= 365 else f"{n // 30}个月前" if n >= 60 else "一个多月前"
    return f"{text.rstrip('。.')} ({when}说的)"


# ---------------------------------------------------------------- 实体 (联想)
_KIN = r"妹妹|姐姐|哥哥|弟弟|爸爸|妈妈|父亲|母亲|爷爷|奶奶|外公|外婆|女朋友|男朋友|老婆|老公|对象|室友|同事|老板|闺蜜|儿子|女儿"
_PET = r"猫|狗|兔|仓鼠|鹦鹉|乌龟"
_BREEDS = {"狗": r"柯基|哈士奇|金毛|泰迪|柴犬|边牧|拉布拉多|萨摩耶|比熊|博美|德牧|田园犬",
           "猫": r"布偶|英短|美短|暹罗|狸花|橘猫|加菲|缅因|蓝猫"}
GENERIC = set(_KIN_WORDS := "妹妹 姐姐 哥哥 弟弟 爸爸 妈妈 父亲 母亲 爷爷 奶奶 外公 外婆 女朋友 男朋友 老婆 老公 对象 室友 同事 老板 闺蜜 儿子 女儿".split()) | {"猫", "狗", "兔", "仓鼠", "鹦鹉", "乌龟"}
_NAMED = re.compile(r"叫([一-鿿]{1,3}?)(?=[，,。！!；;、 ]|$|[^一-鿿])")


def entities(text: str) -> set[str]:
    """一条事实里的人、宠物和名字 ("用户的猫叫豆豆，10月22日它生病了" -> {猫, 豆豆})."""
    out = set(re.findall(_KIN, text)) | set(re.findall(_PET, text))
    out |= {kind for kind, breeds in _BREEDS.items() if re.search(breeds, text)}  # "柯基" 也是狗
    out |= {n for n in _NAMED.findall(text) if not text.startswith(f"用户叫{n}")}
    return out


# ---------------------------------------------------------------- 问用户自己的固定信息
_ASK_SLOT = {
    "名字": re.compile(r"我(?:的名字)?叫(?:什么|啥)|我(?:的)?名字(?:是|叫)?(?:什么|啥)|(?:记得|知道)我(?:叫什么|的名字)"),
    "住处": re.compile(r"我(?:现在|目前)?(?:住|家)(?:在)?(?:哪|什么地方|哪个城市)|我(?:现在|目前)?在(?:哪|哪个城市|什么地方)住|"
                     r"我(?:们)?(?:全家|家)(?:现在|目前)?(?:住)?(?:在)?(?:哪|什么地方|哪个城市)"),
    "生日": re.compile(r"我(?:的)?生日(?:是)?(?:哪天|什么时候|几月几[号日]|几[号日]|多少号)"),
    "出生年": re.compile(r"我(?:是)?(?:哪|几几|哪一)年(?:出生|生)的?"),
    "工作": re.compile(r"我(?:现在|目前)?(?:是)?(?:做|干)(?:什么|啥)(?:工作|的)|我的(?:工作|职业)是(?:什么|啥)|"
                     r"我(?:现在|目前)?在哪(?:上班|工作)"),
}
_SELF_Q = re.compile(r"我.{0,10}(?:什么|啥|哪|几|多少|谁|吗|么|没有|是不是|怎么样)")


# ---------------------------------------------------------------- 出生年和生肖 ("我是90年生人，农历5月初九，属马")
ZODIAC = "鼠牛虎兔龙蛇马羊猴鸡狗猪"


def full_year(yy: str, today: date) -> str:
    """"90" -> "1990", "05" -> "2005" (不超过今年的算 20xx)."""
    if len(yy) == 4:
        return yy
    n = int(yy)
    return str(2000 + n if 2000 + n <= today.year else 1900 + n)


def zodiac_of(year: int) -> str:
    return ZODIAC[(year - 4) % 12]


_ASK_ZODIAC = re.compile(r"我属(?:什么|啥|哪个)")
_ASK_AGE = re.compile(r"我(?:今年)?(?:多大|几岁|多少岁)")


def answer_birth(query: str, facts: list[str], today: date) -> str | None:
    """"我属什么""我今年多大了": 用记下的出生年和生肖回答, 生肖没说过就按年份推 (说明是推的, 不当成用户说过的).
    记下的年份和生肖对不上时照实说 (可能是春节前出生的). 不是这类问题返回 None."""
    if not (_ASK_ZODIAC.search(query) or _ASK_AGE.search(query)):
        return None
    year = next((int(m.group(1)) for f in reversed(facts) if (m := SLOTS["出生年"].match(f))), None)
    said = next((m.group(1) for f in reversed(facts) if (m := SLOTS["生肖"].match(f))), None)
    if _ASK_AGE.search(query):
        return f"你说过是{year}年出生的，按年份算今年{today.year - year}岁。" if year else None
    if said and year and zodiac_of(year) != said:
        return f"你说过你属{said}呀。不过{year}年按年份算是{zodiac_of(year)}年，你是不是春节前出生的？"
    if said:
        return f"你说过你属{said}呀。"
    if year:
        return f"你是{year}年出生的，按年份算属{zodiac_of(year)}（春节前出生的话属{zodiac_of(year - 1)}）。"
    return "这个你还没告诉过我呢。"


def asks_slot(text: str) -> str | None:
    return next((k for k, p in _ASK_SLOT.items() if p.search(text)), None)


def self_question(text: str) -> bool:
    """用户在问自己的事 ("我能吃坚果吗""我叫什么"), 不是问角色."""
    return bool(_SELF_Q.search(text))


_ASK_WHEN = re.compile(r"我(?:是)?(?:哪天|哪一天|什么时候|几号|几月几号|啥时候)(?:要)?(.+)")


def answer_when(query: str, dated: list[tuple[str, date]], today: date) -> str | None:
    """"我哪天要去医院复查" -> 在带日期的事实里找内容对得上的 -> "你明天（10月28日）要去医院复查呀。"; 找不到返回 None."""
    m = _ASK_WHEN.search(query)
    if not m:
        return None
    want = {c for c in _HAN.findall(m.group(1)) if c not in _FILL and c not in "去要"}
    best = max(dated, key=lambda x: len(want & set(_HAN.findall(x[0]))), default=None)
    if not best or len(want & set(_HAN.findall(best[0]))) < 2:
        return None
    text, d = best
    when = f"{relative(d, today)}（{d.month}月{d.day}日）"
    return "你" + _MD.sub(when, text, count=1).replace("用户", "", 1).rstrip("。.") + "呀。"  # 也有"10月28日用户要..."


_ASK_PREF = re.compile(r"我(?:还|现在|到底)?(?:喜欢|爱|讨厌|怕)(.+?)(?:吗|么|嘛|不)[？?]?$")


def answer_pref(query: str, facts: list[str]) -> str | None:
    """"我喜欢喝咖啡吗" -> 记着"用户不喜欢喝咖啡了" -> "你说过不喜欢喝咖啡了呀。"; 没记着这样东西返回 None (交给模型)."""
    m = _ASK_PREF.search(query)
    if not m:
        return None
    want = {c for c in _HAN.findall(m.group(1)) if c not in _FILL}
    for f in reversed(facts):  # 后记的为准
        p = _PREF.match(f)
        if p and len(want & set(_HAN.findall(p.group(2)))) >= min(2, len(want)):
            return "你说过" + f.removeprefix("用户").rstrip("。.") + "呀。"
    return None


_ASK_LIKES = re.compile(r"我(?:最|最近|现在|平时)?(喜欢|爱|讨厌|怕|不喜欢)(吃|喝|玩|看|听|做|去)?(?:的是)?(?:什么|啥|哪)"
                        r"(?:样的|种)?([\u4e00-\u9fff]{0,4}?)(?:呀|呢|啊|吗)?[？?]?$")


# 常见类别 -> 属于这一类的东西的样子 ("我最喜欢什么颜色" 要能对上 "用户喜欢蓝色")
_KINDS = {"颜色": r"色$", "动物": r"猫|狗|兔|鸟|鱼|熊|马|猪|羊|狐狸|仓鼠", "宠物": r"猫|狗|兔|鸟|鱼|仓鼠",
          "运动": r"球|跑|游泳|骑|健身|瑜伽|滑|爬山|跳舞", "音乐": r"歌|曲|乐", "歌": r"歌|曲", "歌手": r"歌",
          "电影": r"影|《|片", "书": r"书|《|小说", "水果": r"莓|果|蕉|瓜|桃|梨|橘|橙|葡萄|柚|樱桃|荔枝",
          "季节": r"春|夏|秋|冬", "天气": r"雨|雪|晴|阴|风"}


def answer_likes(query: str, facts: list[str], forgotten: bool = False) -> str | None:
    """"我喜欢吃什么""我最讨厌什么": 只能从记忆里答, 由服务端回答 (0.8B 会答成它自己喜欢什么).
    记着就说"你说过喜欢吃草莓呀。", 被要求忘掉过就说忘掉了, 都没有就说没告诉过. 不是这类问题返回 None."""
    m = _ASK_LIKES.search(query)
    if not m:
        return None
    positive = m.group(1) in ("喜欢", "爱")
    verb, kind = m.group(2) or "", m.group(3) or ""  # "我最喜欢什么颜色": 没有动词、问的是"颜色"这一类
    hits = []
    for f in facts:
        p = _PREF.match(f)
        if not p or (not p.group(1)) != positive:
            continue
        obj = p.group(2)
        fits_kind = not kind or kind in obj or (kind in _KINDS and re.search(_KINDS[kind], obj))
        if verb and (obj.startswith(verb) or verb in obj) or not verb and fits_kind:
            hits.append(obj.rstrip("了"))
    if hits:
        return f"你说过{'喜欢' if positive else '不喜欢'}" + "、".join(dict.fromkeys(hits[-2:])) + "呀。"
    if forgotten:
        return "你让我把这个忘掉了，我就不记着啦。"
    return "这个你还没告诉过我呢。"


def answer_recall(facts: list[str]) -> str:
    """["用户的狗叫旺财", "用户养了一只柯基"] -> "我记得你说过你的狗叫旺财，你养了一只柯基。" (说明是用户说过的)."""
    return "我记得你说过" + "，".join(f.rstrip("。.").replace("用户", "你", 1) for f in facts) + "。"


def answer_slot(slot: str, fact: str | None, forgotten: bool = False) -> str:
    """服务端直接回答: 记着就照实说 ("用户住在深圳" -> "你住在深圳呀。"), 没记着就说没告诉过,
    用户要求忘掉过的就说已经忘掉了."""
    if not fact and forgotten:
        return "你让我把这个忘掉了，我就不记着啦。"
    if not fact:
        return {"名字": "你还没告诉过我你叫什么呢。", "生日": "你还没告诉过我你的生日呢。"}.get(slot, "这个你还没告诉过我呢。")
    return "你" + fact.removeprefix("用户").rstrip("。.") + "呀。"
