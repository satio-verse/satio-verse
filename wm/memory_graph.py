"""记忆图谱 (人、物、地点): 从记忆的原话里认出实体, 把名字、关系词、品种并成同一个实体, 分类, 按"实体.属性"更正.

原话照存, 图谱只当索引: 拆成三元组会丢掉原来的措辞 (见知识库"长记忆研究与设计"). 程序生成的实体笔记只放 <角色>/图谱/,
给人在 Obsidian 里看关系图, 不参与检索.

  实体    关系词 (妈妈、女朋友、同事)、宠物 (猫、狗、品种)、"叫X"的名字 (认出来以后别处出现也算)、城市、车
  并组    "用户的狗叫旺财" -> 狗=旺财; "用户养了一只柯基" -> 柯基=狗; "小美是用户的妹妹" -> 小美=妹妹.
          同事、朋友这类关系词连着两个以上不同的名字时不并 (老王和小李不是一个人); "改名叫" 算同一个
  分类    人物/家人、人物/伴侣、人物/朋友、宠物、物品/车、地点
  更正    同一个实体的同一个属性换了值: 名字 ("猫改名叫豆包了")、住处 ("妹妹搬到杭州了")、用户的车 ("换了一辆比亚迪")
  列举    "我家里都有谁""我养了哪些宠物""我都去过哪些城市": 按类取, 每个实体一条
"""
from __future__ import annotations

import re

from . import memory_ops as O

CATEGORY: dict[str, str] = {}
for _cat, _words in (("人物/家人", "爸爸 妈妈 父亲 母亲 爷爷 奶奶 外公 外婆 姥姥 姥爷 哥哥 姐姐 弟弟 妹妹 儿子 女儿 老婆 老公"),
                     ("人物/伴侣", "女朋友 男朋友 对象"),
                     ("人物/朋友", "同事 室友 老板 闺蜜 朋友 同学"),
                     ("宠物", "猫 狗 兔子 仓鼠 鹦鹉 乌龟 金鱼"),
                     ("物品/车", "车")):
    CATEGORY.update(dict.fromkeys(_words.split(), _cat))
MANY = set("同事 室友 闺蜜 朋友 同学".split())  # 常有好几个的关系词: 连着不同的名字时不并成一个人
BREED = {b: kind for kind, pat in O._BREEDS.items() for b in pat.split("|")}  # 柯基 -> 狗
CAR_BRANDS = set("特斯拉 比亚迪 宝马 奔驰 奥迪 大众 丰田 本田 日产 理想 蔚来 小鹏 问界 吉利 长安 五菱 保时捷".split())
CITIES = set("北京 上海 天津 重庆 广州 深圳 杭州 南京 苏州 成都 西安 武汉 长沙 郑州 青岛 济南 厦门 福州 大连 沈阳 哈尔滨 长春 "
             "昆明 贵阳 南宁 海口 三亚 拉萨 乌鲁木齐 兰州 西宁 银川 呼和浩特 太原 石家庄 合肥 南昌 宁波 无锡 温州 佛山 东莞 "
             "珠海 汕头 桂林 丽江 大理 洛阳 开封 扬州 绍兴 嘉兴 烟台 威海 秦皇岛 香港 澳门 台北 东京 大阪 京都 首尔 曼谷 "
             "新加坡 巴黎 伦敦 纽约".split())

_ENTITY_WORDS = sorted(set(CATEGORY) | set(BREED), key=len, reverse=True)
_ALT = "|".join(_ENTITY_WORDS)
_MENTION = re.compile("|".join(sorted(set(_ENTITY_WORDS) | CITIES | CAR_BRANDS, key=len, reverse=True)))
_NAME = r"(?!了)([一-鿿]{1,3}?)(?=[，,。！!；;、 ]|$|[^一-鿿]|了|啦)"
_RENAME = r"(?:的名字)?(?:现在)?(改名|改)?(?:叫|为|成)"  # 叫X、改名叫X、改名为X
_LINK = re.compile(r"(" + _ALT + r")" + _RENAME + _NAME)
_LINK_BA = re.compile(r"(?:把|给)(" + _ALT + r")的?(名字|改名)(?:改成|改为|改叫|叫)了?" + _NAME)  # 把狗的名字改成了雪碧
_IS = re.compile(r"([一-鿿]{2,3})是用户的(" + _ALT + ")")
_ANOTHER = re.compile(r"另一|还有一|也有一|又养了")


def links(text: str) -> list[tuple[str, str, bool]]:
    """一条记忆里"是同一个"的两样东西: [(甲, 乙, 是不是改名)]. 名字 <-> 关系词 / 宠物, 品种 <-> 宠物."""
    text = normalize(text)
    out = [(m.group(1), m.group(3), bool(m.group(2))) for m in _LINK.finditer(text) if not m.group(0).endswith(("为", "成"))
           or m.group(2)]
    out += [(m.group(1), m.group(3), True) for m in _LINK_BA.finditer(text)]
    out += [(m.group(2), m.group(1), False) for m in _IS.finditer(text)]
    out += [(b, kind, False) for b, kind in BREED.items() if b in text]
    return out


_SHORT = re.compile(r"(?<=[我老])(爸|妈|哥|姐|弟|妹)(?!\1)")  # 我爸 -> 我爸爸, 老妈 -> 老妈妈


def normalize(text: str) -> str:
    """口语里的亲属简称补全: "我爸以前是做什么的" -> "我爸爸以前是做什么的"."""
    return _SHORT.sub(lambda m: m.group(1) * 2, text)


def mentions(text: str, names=()) -> set[str]:
    """一条记忆 (或问句) 提到的实体词. 长词优先 ("女朋友"不再算"朋友"), 车的品牌和"一辆"算"车"."""
    text = normalize(text)
    out = {"车" if m.group() in CAR_BRANDS else m.group() for m in _MENTION.finditer(text)}
    if "一辆" in text:
        out.add("车")
    return out | {n for n in names if n in text}


# ---------------------------------------------------------------- 属性更正
_ROLE_ONE = "|".join(w for w in _ENTITY_WORDS if w not in MANY and w != "车")
_ATTRS = [
    (re.compile(r"(" + _ROLE_ONE + r")(?:的名字)?(?:现在)?(?:改名|改)?叫" + _NAME), "名字"),
    (re.compile(r"(" + _ROLE_ONE + r")改名(?:为|成)" + _NAME), "名字"),
    (re.compile(r"(?:把|给)(" + _ROLE_ONE + r")的?(?:名字|改名)(?:改成|改为|改叫|叫)了?" + _NAME), "名字"),
    (re.compile(r"(" + _ROLE_ONE + r")(?:现在|已经|最近)?(?:住在|搬到|搬去|搬来)了?([一-鿿]{2,5}?)(?:了|啦|住)?(?=[，,。！!；;]|$)"), "住处"),
    (re.compile(r"^用户(?:的车是|(?:现在)?开的?是|换了一?辆|买了一?辆|新买了一?辆)(.+?)了?$"), "车"),
]


def attr_of(fact: str) -> tuple[str, str, str] | None:
    """"用户的猫改名叫豆包了" -> ("猫", "名字", "豆包"); "用户换了一辆蓝色的比亚迪" -> ("用户", "车", "蓝色的比亚迪")."""
    fact = normalize(fact)
    if _ANOTHER.search(fact):  # "另一只猫叫豆包": 不是同一只
        return None
    for pat, attr in _ATTRS:
        m = pat.search(fact)
        if m:
            return (m.group(1), attr, m.group(2)) if attr != "车" else ("用户", attr, m.group(1))
    return None


def supersedes(new: str, old: str) -> bool:
    """同一个实体的同一个属性换了值 (旧的那行该划掉)."""
    a, b = attr_of(new), attr_of(old)
    return bool(a and b and a[:2] == b[:2] and a[2] != b[2])


# ---------------------------------------------------------------- 按类问
_LIST_Q = {
    "家人": re.compile(r"我家(?:里)?(?:都|一共)?有(?:谁|哪些人|几口人|什么人)|我(?:的)?家人(?:都)?(?:有|是)"),
    "宠物": re.compile(r"我(?:都|一共)?养(?:了|过)?(?:哪些|什么|几只|几个)(?:宠物|动物)|我(?:的|家的?)宠物(?:都)?(?:有|是)"),
    "地点": re.compile(r"我(?:都)?去过(?:哪些|哪儿|哪里|什么|哪)"),
    # 聚合型问句: 要的是"列出全部日程", 不是某一个槽位 (2026-11-18 S3)
    "日程": re.compile(r"我(?:最近的?)?(?:安排|日程|计划)(?:都|是)?(?:是?什么|有哪些|怎么样)|"
                      r"我(?:这|本|下)(?:周|个月|月)(?:都|还)?有(?:什么|哪些)?(?:事|安排)|"
                      r"我(?:最近|接下来)(?:都|还)?有(?:什么|哪些)(?:事|安排)|"
                      r"下?(?:一次|一件)(?:要|得)?(?:办|做)(?:的)?(?:事|事情)(?:是|有)?(?:什么|啥)|"
                      r"我(?:最近|这几天)?忙(?:不忙|吗)"),
}
_TRAVEL = re.compile(r"去|旅游|旅行|出差|玩")
# 问用户自己的某个人、某样东西 ("我女朋友…""我家柯基…""我现在开什么车"), 或者点了记着的名字
_OWN = re.compile(r"我(?:的|家的?|家里的?|现在|们家的?)?(?:" + _ALT + r"|车)|我(?:现在|平时)?开的?(?:是)?(?:什么|啥|哪)")


def list_query(text: str) -> str | None:
    return next((k for k, p in _LIST_Q.items() if p.search(text)), None)


def asks_own(text: str, names=()) -> bool:
    return bool(_OWN.search(normalize(text))) or any(n in text for n in names)


class Graph:
    """一组记忆 (texts, 和检索的切块对齐) 上的实体图: 实体词 -> 组, 组 -> 提到它的记忆, 组的类别."""

    def __init__(self, texts: list[str], pre_links: list | None = None, pre_mentions: list | None = None):
        """pre_links / pre_mentions: 每条记忆事先算好的 links(t) 和 mentions(t) (不含名字), 和 texts 对齐.
        Memory 在切块时算一次、跟着文件缓存, 写库后重建图谱就不用把几千块再跑一遍正则."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        all_links = [lk for ls in (pre_links if pre_links is not None else map(links, texts)) for lk in ls]
        per_role: dict[str, set] = {}
        for a, b, rename in all_links:
            if a in MANY and not rename:
                per_role.setdefault(a, set()).add(b)
        self.names = {b for a, b, _ in all_links if b not in CATEGORY and b not in BREED} | \
                     {a for a, b, _ in all_links if a not in CATEGORY and a not in BREED}
        self.name_cat: dict[str, str] = {}  # 没并组的名字 (老王、小李) 的类别
        for a, b, rename in all_links:
            if a in MANY and len(per_role.get(a, ())) > 1:
                self.name_cat[b] = CATEGORY[a]
                continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        if pre_mentions is None:
            self.mention = [mentions(t, self.names) for t in texts]
        else:  # 事先算好的不含名字, 这里只补上名字 (名字要等所有记忆都看过才知道)
            self.mention = [m | {n for n in self.names if n in t} for t, m in zip(texts, pre_mentions)]
        words = set().union(*self.mention) if self.mention else set()
        self.root = {w: find(w) for w in words | set(parent)}
        self.group: dict[str, set] = {}
        for w, r in self.root.items():
            self.group.setdefault(r, set()).add(w)
        self.members: dict[str, list[int]] = {}
        for i, ms in enumerate(self.mention):
            for r in dict.fromkeys(self.root[w] for w in ms):
                self.members.setdefault(r, []).append(i)

    def category(self, root: str) -> str | None:
        for w in self.group.get(root, ()):
            if w in CITIES:
                return "地点"
            cat = CATEGORY.get(w) or ("宠物" if w in BREED else None) or self.name_cat.get(w)
            if cat:
                return cat
        return None

    def label(self, root: str) -> str:
        """实体的显示名: 有名字用名字, 没有用关系词 (妈妈、车)."""
        g = self.group.get(root, {root})
        named = sorted(w for w in g if w in self.names)
        return named[-1] if named else max(sorted(g), key=lambda w: (w in CATEGORY, len(w)))

    def roots_in(self, text: str) -> list[str]:
        return list(dict.fromkeys(self.root[w] for w in mentions(text, self.names) if w in self.root))
