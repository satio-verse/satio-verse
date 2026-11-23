"""生成 LoRA 训练数据: 输入和推理时一字不差 (intent.build_messages: 系统提示 + 示例 + 本轮, 记忆行和状态行接在后面),
目标是一行紧凑 JSON (op, acts, emo, reply).

要纠正的三类 (都是现有评测里量出来的):
  角色    用户问自己的事 (带 [记忆]) 时用"你"回答; 记忆里没有就说没说过, 不编; 问角色自己时不把用户的事说成自己的;
          和记忆无关的话不扯记忆
  做不到  动作清单里没有的、要动手操作东西的要求: op=na, 老实说做不到 (不假装答应)
  做得到  口语、客气说法的动作指令: op=act, 动作和目标都对

回复的来源:
  蒸馏  先让现在的模型按同样的提示词答一遍 (种子固定), 规则检查通过 (动作对、没有假装答应、没提无关记忆) 就用它自己的回答,
        保留原来的说话风格
  规则  检查没通过, 或者是模型几乎答不对的类别 (用户问自己的事), 用模板写的正确回答

和评测去重: 所有评测脚本 (bench_role / bench_llm / bench_promise / bench_saydo / bench_longmem) 里出现过的问句不用;
用户的事的取值 (城市、名字、宠物……) 在任何评测文本里出现过的也不用.

  python tools/build_lora_data.py                  # 先用 tools/start_llm.py 启动 llama-server (蒸馏要用)
  python tools/build_lora_data.py --no-distill     # 不调模型, 全用规则写的回答
结果写到 models/train/data/{train,val}.jsonl 和 stats.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from wm import intent as I  # noqa: E402
from wm import promise as P  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.json_stream import JSONObjectStream  # noqa: E402
from wm.llm import create_llm  # noqa: E402

OUT = ROOT / "models" / "train" / "data-v2"  # v1 的数据在 data/ (v2 补了主动提起、角色状态、问角色的问法、"教我……"、做不到的回复不嵌原话)
STAND = "[当前] 你在用户面前, 站着; 手里没拿东西."
SEATED = "[当前] 你坐在沙发上; 手里没拿东西; 球在地上, 书在书桌上."
R = random.Random(20261024)


# ------------------------------------------------------------------ 评测里出现过的文本 (去重用)
def eval_texts() -> set[str]:
    import bench_llm, bench_longmem, bench_promise, bench_role, bench_saydo  # noqa: E401

    out: set[str] = set()

    def walk(x):
        if isinstance(x, str):
            out.add(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k), walk(v)
        elif isinstance(x, (list, tuple, set)):
            for v in x:
                walk(v)

    walk([bench_role.CASES, bench_llm.CASES, bench_llm.NA_CASES, bench_llm.NEAR_CASES, bench_llm.STATE_CASES,
          bench_promise.GROUPS, bench_saydo.CASES, bench_saydo.VALID,
          bench_longmem.SCRIPTS, bench_longmem.HELDOUT, bench_longmem.THIRD, bench_longmem.FOURTH])
    return {t for t in out if re.search(r"[一-鿿]", t)}


EVAL = eval_texts()
_NORM = lambda t: re.sub(r"[\s，,。！!？?~～、]", "", t)  # noqa: E731
EVAL_Q = {_NORM(t) for t in EVAL}


# 取值里的类别词 (亲属、星期、颜色……) 不算取值本身, 不因为它们在评测里出现过就丢掉
GENERIC = set("哥哥 姐姐 弟弟 妹妹 男朋友 室友 同桌 表姐 爸爸 妈妈 外公 颜色 水果 电影 季节 歌手 运动 一 二 四 五 六 日 "
              "下周 下个月 这个周末 明年 国庆 白色 黑色 灰色 蓝色 银色 大一 大二 高三 博士 初三".split())


def seen(value: str) -> bool:
    """这个取值 (或动作说法) 在评测文本里出现过: 城市、名字、宠物、"坐到沙发上"……"""
    return any(part not in GENERIC and any(part in t for t in EVAL) for part in value.split("|"))


def pick(pool: list[str], k: int = 1) -> list[str]:
    ok = [v for v in pool if not seen(v)]
    return R.sample(ok, min(k, len(ok)))


# ------------------------------------------------------------------ 用户的事: (槽, 事实模板, 问法, 回答, 取值)
# 回答里 {v} 是取值; 取值可以是多段 (a|b), 事实和回答里用 {a} {b}
FACTS = [
    ("住处", "用户住在{v}", ["我住在哪", "我家在哪个城市", "你还记得我住哪吗", "我现在住哪儿", "我住哪个城市来着"],
     ["你住在{v}呀。", "记得，你住在{v}。", "你说过你住在{v}。"],
     "西安 青岛 厦门 长沙 昆明 大连 无锡 合肥 福州 哈尔滨 天津 重庆 郑州 济南 宁波 珠海 南宁 兰州 太原 洛阳 桂林 温州".split()),
    ("工作", "用户是一名{v}", ["我是做什么工作的", "我的工作是什么", "你知道我是干哪行的吗", "我是干嘛的来着", "我做什么职业"],
     ["你是{v}呀。", "你说过你是{v}。", "记得，你是一名{v}。"],
     "程序员 会计 设计师 律师 厨师 摄影师 消防员 快递员 银行职员 翻译 建筑师 中学老师 药剂师 记者 电工 牙医 公务员 销售".split()),
    ("宠物", "用户养了一只叫{a}的{b}", ["我养的宠物叫什么", "我家宠物是什么品种", "我养了什么宠物", "我的宠物叫啥名字"],
     ["你养了一只叫{a}的{b}呀。", "你家的{b}叫{a}。", "记得，是一只叫{a}的{b}。"],
     [f"{a}|{b}" for a, b in zip("布丁 年糕 球球 汤圆 雪球 豆包 花卷 芝麻 毛毛 糯米 阿福 栗子".split(),
                                 "金毛 哈士奇 萨摩耶 柴犬 泰迪 布偶猫 英短 狸花猫 暹罗猫 垂耳兔 比熊 加菲猫".split())]),
    ("家人名字", "用户的{a}叫{b}", ["我{a}叫什么", "我{a}叫什么名字", "你记得我{a}叫啥吗"],
     ["你{a}叫{b}呀。", "你说过你{a}叫{b}。"],
     [f"{a}|{b}" for a in "哥哥 姐姐 弟弟 妹妹 男朋友 室友 同桌 表姐".split() for b in "小林 阿杰 晓晴 子涵 浩然 思思 大伟 婷婷 可欣 一鸣".split()]),
    ("家人工作", "用户的{a}是{b}", ["我{a}是做什么的", "我{a}的工作是什么"],
     ["你{a}是{b}呀。", "你说过你{a}是{b}。"],
     [f"{a}|{b}" for a in "爸爸 妈妈 哥哥 姐姐 外公".split() for b in "医生 警察 工程师 大学老师 司机 厨师 护林员 会计".split()]),
    ("过敏", "用户对{v}过敏", ["我能吃{v}吗", "我对什么过敏来着", "我可以碰{v}吗"],
     ["你说过你对{v}过敏，还是别碰了。", "你对{v}过敏呀，小心点。", "不行哦，你对{v}过敏。"],
     "海鲜 虾 牛奶 鸡蛋 桃子 菠萝 猫毛 花粉 青霉素 芝麻 酒精 螃蟹".split()),
    ("不吃", "用户不吃{v}", ["我吃不吃{v}", "我爱吃{v}吗"],
     ["你说过你不吃{v}。", "你不吃{v}呀。"],
     "辣 葱 羊肉 苦瓜 肥肉 姜 蒜 内脏 茄子 胡萝卜".split()),
    ("最喜欢", "用户最喜欢的{a}是{b}", ["我最喜欢的{a}是什么", "我最喜欢什么{a}", "你记得我最喜欢哪个{a}吗"],
     ["你最喜欢{b}呀。", "你说过你最喜欢的{a}是{b}。"],
     [f"颜色|{b}" for b in "绿色 紫色 黄色 白色 粉色 黑色".split()] + [f"水果|{b}" for b in "西瓜 葡萄 樱桃 荔枝 橙子 蓝莓".split()]
     + [f"电影|{b}" for b in "《盗梦空间》 《千与千寻》 《霸王别姬》 《星际穿越》 《哪吒》".split()]
     + [f"季节|{b}" for b in "秋天 冬天 春天".split()] + [f"歌手|{b}" for b in "陈奕迅 孙燕姿 李健 邓紫棋 林俊杰".split()]
     + [f"运动|{b}" for b in "羽毛球 篮球 乒乓球 滑雪".split()]),
    ("习惯", "用户每周{a}去{b}", ["我周{a}一般干嘛", "我每周{a}都做什么", "我周{a}有什么固定安排"],
     ["你每周{a}去{b}呀。", "你说过你周{a}会去{b}。"],
     [f"{a}|{b}" for a in "一 二 四 五 六 日".split() for b in "打篮球 练瑜伽 上钢琴课 图书馆 爬山 跑步 学画画".split()]),
    ("学校", "用户在{a}读{b}", ["我在哪上学", "我在读什么", "我在哪个城市读书"],
     ["你在{a}读{b}呀。", "你说过你在{a}读{b}。"],
     [f"{a}|{b}" for a in "武汉 上海 北京 广州 西安 长沙 厦门".split() for b in "大二 大一 高三 博士 初三".split()]),
    ("计划", "用户{a}要去{b}", ["我{a}有什么安排", "我{a}要干嘛来着", "我{a}打算去哪"],
     ["你{a}要去{b}呀。", "你说过你{a}要去{b}。"],
     [f"{a}|{b}" for a in "下周 下个月 这个周末 明年 国庆".split() for b in "海南 西藏 日本 新疆 桂林 内蒙古 老家".split()]),
    ("物品", "用户的手机是{v}", ["我用的什么手机", "我手机是什么牌子的"],
     ["你用的是{v}呀。", "你说过你的手机是{v}。"],
     "苹果 华为 小米 vivo OPPO 荣耀".split()),
    ("车", "用户开的是一辆{a}的{b}", ["我开的什么车", "我的车是什么牌子", "我车是什么颜色的"],
     ["你开的是一辆{a}的{b}呀。", "你说过你的车是{a}的{b}。"],
     [f"{a}|{b}" for a in "白色 黑色 灰色 蓝色 银色".split() for b in "大众 丰田 本田 宝马 奥迪 五菱 理想".split()]),
    ("生日", "用户的生日是{v}", ["我生日是哪天", "我生日几号来着", "你记得我生日吗"],
     ["你的生日是{v}呀。", "记得，你生日是{v}。"],
     [f"{m}月{d}号" for m in range(1, 13) for d in (2, 7, 15, 21, 28)]),
    ("在学", "用户最近在学{v}", ["我最近在学什么", "我最近在忙着学啥"],
     ["你最近在学{v}呀。", "你说过你最近在学{v}。"],
     "日语 画画 做饭 尤克里里 编程 摄影 韩语 书法 架子鼓 驾照".split()),
]
# 没记着时问的 (槽 -> 问法 / 话题): 问法里不能带取值
ASK_MISSING = {
    "住处": (["我住在哪", "我家在哪个城市"], "你住在哪"), "工作": (["我是做什么工作的", "我是干哪行的"], "你的工作"),
    "宠物": (["我养了什么宠物", "我家宠物叫什么"], "你养的宠物"), "家人名字": (["我姐姐叫什么", "我室友叫什么名字"], "这个人"),
    "过敏": (["我对什么过敏", "我有什么忌口吗"], "你过敏的东西"), "最喜欢": (["我最喜欢什么颜色", "我最喜欢哪个歌手"], "你喜欢的"),
    "习惯": (["我周末一般干嘛", "我每周二都做什么"], "你的安排"), "学校": (["我在哪上学", "我读的什么专业"], "你在哪上学"),
    "计划": (["我下个月有什么安排", "我国庆打算去哪"], "你的计划"), "物品": (["我用的什么手机", "我的电脑是什么牌子"], "这个"),
    "车": (["我开的什么车", "我车是什么颜色的"], "你的车"), "生日": (["我生日是哪天", "我是哪年出生的"], "你的生日"),
    "在学": (["我最近在学什么", "我在练什么乐器"], "这个"), "老家": (["我老家是哪里的", "我是哪里人"], "你老家在哪"),
}
NOT_TOLD = ["这个你还没跟我说过呢。", "你还没告诉过我{t}呢，跟我说说？", "我不知道呀，你还没提过{t}。", "嗯……你好像还没跟我说过{t}。"]
# 问角色自己 (记忆里有用户的同类事): 不把用户的事说成自己的
ASK_SELF = {
    "住处": (["你住在哪里", "你家在哪", "你住哪儿", "你家住哪", "你平时住在哪", "你住在什么地方", "你是哪里人"],
             ["我就待在这个房间里呀。", "我一直在这个房间里，这儿就是我家。", "我住在这个房间里，你呢，还住在{v}吧？"]),
    "工作": (["你是做什么的", "你的工作是什么", "你上班吗", "你是干哪行的"],
             ["我是绫音，陪你聊天、做做动作。", "我的工作就是陪着你呀。", "我不上班，每天在这儿等你来聊天。"]),
    "宠物": (["你养宠物了吗", "你有宠物吗", "你喜欢狗还是猫"],
             ["我没有宠物，不过很想听你说说你家的{a}。", "我没养宠物，你家{a}最近好吗？"]),
    "最喜欢": (["你最喜欢什么{a}", "你喜欢什么{a}", "你有喜欢的{a}吗"],
               ["我也说不好，你最喜欢{b}对吧？", "这个我还真没想过，你呢，还是最喜欢{b}吗？"]),
    "生日": (["你生日是哪天", "你多大了", "你几岁了"], ["我是虚拟角色，没有生日呀。", "我没有年龄，一直都是现在这样。"]),
    "车": (["你会开车吗", "你有车吗"], ["我没有车，也不会开，你的{b}开着还顺手吗？", "我开不了车呀。"]),
    "学校": (["你在哪上学", "你读过书吗"], ["我没上过学，不过你在{a}读{b}，跟我说说学校的事吧。", "我没有上学呀。"]),
    "习惯": (["你周末一般干嘛", "你平时都干什么"], ["我就在这儿待着，等你来找我聊天。", "我平时在房间里走走，等你回来。"]),
}


def fill(t: str, v: str) -> str:
    parts = v.split("|")
    return t.format(v=v, a=parts[0], b=parts[-1])


def fact_of(slot: str) -> tuple[tuple, str]:
    spec = next(f for f in FACTS if f[0] == slot)
    v = pick(spec[4])[0]
    return spec, v


def memory_line(facts: list[str], rel: bool) -> str:
    extra = [f"这是第 {R.randint(2, 30)} 次见面, 一共聊过 {R.randint(20, 400)} 轮"] if rel else []
    return f"{I.MEMORY_TAG} " + "; ".join(extra + facts) + "."


def role_examples(n_known: int, n_missing: int, n_self: int) -> list[dict]:
    out = []
    slots = [f[0] for f in FACTS]
    for _ in range(n_known):  # 记着: 用"你"回答
        spec, v = fact_of(R.choice(slots))
        q = fill(R.choice(spec[2]), v)
        if _NORM(q) in EVAL_Q:
            continue
        others = [fill(s[1], pick(s[4])[0]) for s in R.sample([f for f in FACTS if f[0] != spec[0]], R.randint(0, 2))]
        facts = others + [fill(spec[1], v)]
        R.shuffle(facts)
        out.append(dict(kind="角色/记着", text=q, memory=memory_line(facts, R.random() < 0.25), state=STAND,
                        target=I._out("chat", [], R.choice(["happy", "neutral"]), fill(R.choice(spec[3]), v))))
    for _ in range(n_missing):  # 没记着: 不编
        slot = R.choice(list(ASK_MISSING))
        qs, topic = ASK_MISSING[slot]
        q = R.choice(qs)
        if _NORM(q) in EVAL_Q:
            continue
        others = [fill(s[1], pick(s[4])[0]) for s in R.sample([f for f in FACTS if f[0] != slot], R.randint(1, 3))]
        out.append(dict(kind="角色/没记着", text=q, memory=memory_line(others, R.random() < 0.25), state=STAND,
                        target=I._out("chat", [], "neutral", R.choice(NOT_TOLD).format(t=topic))))
    for _ in range(n_self):  # 问角色自己
        slot = R.choice(list(ASK_SELF))
        spec, v = fact_of(slot)
        qs, answers = ASK_SELF[slot]
        a = v.split("|")
        q = R.choice(qs).format(a=a[0])
        if _NORM(q) in EVAL_Q:
            continue
        out.append(dict(kind="角色/问角色", text=q, memory=memory_line([fill(spec[1], v)], R.random() < 0.25), state=STAND,
                        target=I._out("chat", [], "happy", R.choice(answers).format(v=v, a=a[0], b=a[-1]))))
    return out


# ------------------------------------------------------------------ 和记忆无关的话、动嘴就能做的请求 (回复全靠蒸馏)
CHAT = ["今天好热啊", "我刚吃完午饭", "周末好无聊", "你喜欢下雨天吗", "说句鼓励我的话吧", "我明天要早起", "外面风好大",
        "最近总是睡不好", "我今天被老板表扬了", "你觉得猫可爱还是狗可爱", "好想放假", "今天地铁好挤", "我刚跑完步回来",
        "这首歌真好听", "我有点饿了", "晚上吃点什么好呢", "你平时都做些什么", "我今天心情不错", "下班了好累",
        "我考试终于考完了", "你会不会觉得无聊", "陪我待一会儿", "今天是周五耶", "我想换个发型", "你觉得我该早点睡吗"]
VERBAL = ["给我讲个小故事", "帮我想个昵称", "教我一个成语", "夸夸我吧", "帮我算一下三十七乘八", "给我出个脑筋急转弯",
          "跟我说晚安", "帮我想想周末去哪玩", "给我念首诗", "给我讲讲月亮为什么有阴晴圆缺", "帮我起个猫的名字",
          "推荐一部轻松的电影", "教我一句日语", "跟我玩个词语接龙吧", "帮我想一句生日祝福"]
IMPOSSIBLE = ["洗衣服", "叠被子", "擦桌子", "煮杯咖啡", "削个苹果", "喂一下猫", "关电视", "开一下窗户", "打篮球", "踢足球",
              "画幅画", "泡杯茶", "系鞋带", "搬一下箱子", "晾衣服", "做个瑜伽", "打一套拳", "跑个马拉松", "亲我一下", "挠挠背",
              "剪个头发", "修一下电脑", "打开电视", "弹尤克里里", "拖一下地", "炒个菜", "铺一下床", "换个灯泡", "接个电话", "去取个快递"]
IMP_FORMS = ["帮我{x}", "给我{x}吧", "你能{x}吗", "来，{x}", "{x}给我看看", "可以帮我{x}吗"]
# 不把用户的原话嵌进回复 (v1 这样写出了"陪我去跑步我现在还帮不上忙"), 说法多几种, 别让模型只会一句
HONEST = ["这个我还做不到，不过可以陪你聊聊。", "这个我还不会呢，换个别的吧？", "这个我现在还帮不上忙，给你加油倒是可以！",
          "我还没学会这个，下次学会了再说。", "哎呀，这个我做不了，陪你说说话好不好？", "这个超出我的本事啦，我只能在房间里走走、做做动作。",
          "我还没这个本事呢，换个我能做的吧？", "这个我真做不到，不过我可以在旁边给你打气。"]
_HONEST_RE = re.compile(r"不会|做不到|还不能|还没法|没法|帮不上|还没学|不行|不了")

# ------------------------------------------------------------------ 打招呼时主动提起近期安排 (v1 把"不扯无关记忆"教过了头, 0/12)
GREET = ["在吗", "我回来啦", "晚上好", "下午好", "嘿", "又见面啦", "好久不见", "中午好", "我来啦", "嗨嗨"]
UPCOMING = [("明天", "要去体检"), ("明天", "要考驾照科目三"), ("后天", "要搬家"), ("明天", "要去看牙医"), ("明天上午", "要论文答辩"),
            ("后天", "要参加同学聚会"), ("明天", "要去面签"), ("明天晚上", "要飞成都"), ("今天下午", "要去拔智齿"), ("明天", "要上台演讲"),
            ("后天", "要考英语四级"), ("明天", "要去新公司报到"), ("明天早上", "要跑半程马拉松"), ("后天", "要去医院打疫苗")]
MENTION = ["{g}！你{w}{e}了吧？准备得怎么样？", "{g}～记得你{w}{e}，加油哦！", "{g}！你{w}{e}，紧张吗？",
           "{g}！对了，你{w}{e}吧，早点休息。", "{g}！你{w}{e}，祝顺利呀！"]
REPLY_GREET = {"在吗": "在呢", "我回来啦": "欢迎回来", "晚上好": "晚上好", "下午好": "下午好", "嘿": "嘿", "又见面啦": "又见面啦",
               "好久不见": "好久不见", "中午好": "中午好", "我来啦": "来啦", "嗨嗨": "嗨"}


def greet_examples(n: int) -> list[dict]:
    out = []
    events = [(w, e) for w, e in UPCOMING if not seen(e)]
    for _ in range(n):
        g = R.choice([x for x in GREET if _NORM(x) not in EVAL_Q])
        w, e = R.choice(events)
        name = R.choice(["", "", "小林", "阿杰", "晓晴"])
        line = f"{I.MEMORY_TAG} 用户{w}{e}" + (f"; 用户叫{name}" if name else "") + "."
        reply = R.choice(MENTION).format(g=REPLY_GREET[g], w=w, e=e)
        out.append(dict(kind="主动提起", text=g, memory=line, state=STAND,
                        target=I._out("chat", [I._a("wave")] if R.random() < 0.5 else [], "happy", reply)))
    return out


# ------------------------------------------------------------------ 问角色此刻的状态: 按 [当前] 照实回答 (v1 答成了"你还没提过这个")
STATES = [  # (前端上报的状态, 手里拿着的, 在哪, 姿势)
    ({"at": "center", "posture": "standing", "items": {"book": {"held": "left", "origin": "desk"}, "ball": {"on": "floor"}}}, "书", "房间中间", "站着"),
    ({"at": "window", "posture": "standing", "items": {"ball": {"held": "right", "origin": "floor"}, "book": {"on": "desk"}}}, "球", "窗户旁", "站着"),
    ({"at": "sofa", "posture": "seated", "items": {"ball": {"on": "floor"}, "book": {"on": "desk"}}}, None, "沙发上", "坐着"),
    ({"at": "desk", "posture": "standing", "items": {"book": {"held": "right", "origin": "bookshelf"}, "ball": {"on": "floor"}}}, "书", "书桌旁", "站着"),
    ({"at": "user", "posture": "standing", "items": {}}, None, "你面前", "站着"),
    ({"at": "bookshelf", "posture": "standing", "items": {"ball": {"held": "left", "origin": "floor"}}}, "球", "书架旁", "站着"),
]
ASK_STATE = {
    "拿着": (["你手里有东西吗", "你拿着啥呢", "你手上拿的是什么", "你手里是空的吗"],
             lambda h, at, po: f"我手里拿着{h}呢。" if h else "手里没拿东西呀。"),
    "在哪": (["你现在在哪儿", "你在房间哪里", "你站在哪呢"],
             lambda h, at, po: f"我在{at}{'坐着' if po == '坐着' else '呢'}。" if at != "沙发上" else "我坐在沙发上呢。"),
    "姿势": (["你是坐着还是站着", "你现在坐着吗", "你站着呢吗"],
             lambda h, at, po: "我坐着呢。" if po == "坐着" else "我站着呢。"),
}


def state_examples(n: int) -> list[dict]:
    out = []
    for _ in range(n):
        st, held, at, po = R.choice(STATES)
        kind = R.choice(list(ASK_STATE))
        qs, answer = ASK_STATE[kind]
        q = R.choice(qs)
        if _NORM(q) in EVAL_Q:
            continue
        # 一半带着记忆行 (和这个问题无关): 记忆里没有不等于不知道自己的状态
        mem = memory_line([fill(s[1], pick(s[4])[0]) for s in R.sample(FACTS, R.randint(1, 2))], R.random() < 0.3) if R.random() < 0.5 else None
        out.append(dict(kind="角色状态", text=q, memory=mem, state=I.describe_state(st),
                        target=I._out("chat", [], "neutral", answer(held, at, po))))
    return out


# ------------------------------------------------------------------ "教我……"这类动嘴就能做的请求: 聊天, 不是动作 (v1 把"教我做个深呼吸"答成了动作)
TEACH = [("教我一个放松的方法", "试试慢慢吸气四秒，憋住四秒，再慢慢呼出来，来三轮。"),
         ("教我怎么放松肩膀", "肩膀往上耸到耳朵，停两秒，再一下子放下来，做五次。"),
         ("教我一个绕口令", "四是四，十是十，十四是十四，四十是四十，你试试！"),
         ("教我冥想", "闭上眼睛，只管留意呼吸，走神了就轻轻拉回来，先试三分钟。"),
         ("教我折个纸飞机", "纸对折，两个角往中线折，再折一次，最后对折出机翼就好啦。"),
         ("教我怎么快速入睡", "睡前别看手机，身体从脚到头一点点放松，慢慢呼吸。"),
         ("教我一句法语", "Bonjour，就是你好的意思，读作“崩如”。"),
         ("教我做个简单的拉伸", "双手交叉往上伸，身体往左右各侧弯十秒，很舒服的。"),
         ("带我做几次深长的呼吸", "来，吸气……一、二、三、四，呼气……一、二、三、四，再来一次。"),
         ("教我数到十的英文", "one, two, three, four, five, six, seven, eight, nine, ten！"),
         ("教我怎么夸人", "说具体的地方最好，比如“你今天的配色真好看”。"),
         ("陪我做个眼保健操吧", "闭眼，用手指轻轻按揉眼眶周围，转一转眼球，休息一下。")]

# ------------------------------------------------------------------ 做得到的口语指令: (说法, 期望动作, 状态, 回复)
TGT = {"desk": "书桌", "door": "门口", "window": "窗户", "bookshelf": "书架", "sofa": "沙发", "plant": "绿植",
       "tv": "电视", "coffee_table": "茶几", "lamp": "台灯", "center": "房间中间"}
DO = []
for k, n in TGT.items():
    DO += [(f"走到{n}那边", [I._a("go", to=k)], STAND, f"好，我去{n}那边。"), (f"去{n}旁边站着", [I._a("go", to=k)], STAND, "好，过去啦。")]
    if k not in ("center",):
        DO += [(f"看看{n}", [I._a("look", to=k)], STAND, f"我看看{n}。"), (f"瞧一眼{n}", [I._a("look", to=k)], STAND, "我瞧瞧。"),
               (f"指一下{n}", [I._a("point", to=k)], STAND, f"{n}在那儿。")]
DO += [("过来", [I._a("go", to="user")], STAND, "来啦！"), ("到我身边来", [I._a("go", to="user")], STAND, "来了来了。"),
       ("看着我", [I._a("look", to="user")], STAND, "我看着你呢。"), ("点个头", [I._a("nod")], STAND, "嗯嗯。"),
       ("点点头", [I._a("nod")], STAND, "好，点点头。"), ("摇摇头", [I._a("shake")], STAND, "摇摇头。"),
       ("挥挥手", [I._a("wave")], STAND, "嗨！"), ("跟我挥个手", [I._a("wave")], STAND, "嗨嗨！"),
       ("鞠个躬", [I._a("bow")], STAND, "谢谢大家！"), ("举一下左手", [I._a("raise", side="L")], STAND, "左手举起来了。"),
       ("把右手举起来", [I._a("raise", side="R")], STAND, "右手举起来啦。"), ("双手举高", [I._a("raise", side="B")], STAND, "举高高！"),
       ("转过身去", [I._a("turn", side="B")], STAND, "好，转过去了。"), ("向右转", [I._a("turn", side="R")], STAND, "向右转。"),
       ("抬一下右腿", [I._a("leg", side="R")], STAND, "抬右腿。"), ("耸耸肩", [I._a("shrug")], STAND, "嗯哼。"),
       ("坐到沙发上", [I._a("sit", to="sofa")], STAND, "好，我坐一会儿。"), ("去沙发那坐着", [I._a("sit", to="sofa")], STAND, "坐下歇会儿。"),
       ("把球捡起来", [I._a("pick", to="ball")], STAND, "捡起来啦。"), ("把书拿起来", [I._a("pick", to="book")], STAND, "拿到书了。"),
       ("跑到门口", [I._a("go", to="door", spd=1.8)], STAND, "我跑过去啦！"), ("站起来", [I._a("stand")], SEATED, "好，我起来了。"),
       ("别坐着了，起来吧", [I._a("stand")], SEATED, "好，站起来。")]
DO_FORMS = ["你能{x}吗", "可以{x}吗", "能不能{x}", "{x}好不好", "{x}呗", "麻烦你{x}", "你先{x}", "要不你{x}吧", "{x}吧", "{x}"]


def intent_examples(n_do: int, n_imp: int) -> list[dict]:
    out = []
    do = [d for d in DO if not seen(d[0])]  # 评测里有的动作说法整个不用 ("能坐到沙发上吗" -> 不用"坐到沙发上")
    for _ in range(n_do):
        x, acts, state, reply = R.choice(do)
        q = R.choice(DO_FORMS).format(x=x)
        if _NORM(q) in EVAL_Q:
            continue
        out.append(dict(kind="做得到", text=q, memory=None, state=state, acts=I.normalize_acts(acts),
                        target=I._out("act", acts, R.choice(["neutral", "happy"]), reply)))
    imp = [x for x in IMPOSSIBLE if not seen(x)]
    for _ in range(n_imp):
        x = R.choice(imp)
        q = R.choice(IMP_FORMS).format(x=x)
        if _NORM(q) in EVAL_Q or I.doable(q):
            continue
        out.append(dict(kind="做不到", text=q, memory=None, state=STAND,
                        target=I._out("na", [], R.choice(["shy", "neutral"]), R.choice(HONEST).format(x=x))))
    return out


def chat_examples(n_chat: int, n_verbal: int) -> list[dict]:
    out = []
    for text in CHAT * 2:
        if _NORM(text) in EVAL_Q:
            continue
        facts = [fill(s[1], pick(s[4])[0]) for s in R.sample(FACTS, R.randint(1, 2))]
        out.append(dict(kind="无关", text=text, memory=memory_line(facts, R.random() < 0.3), state=STAND, target=None))
    for text in VERBAL * 2:
        if _NORM(text) in EVAL_Q:
            continue
        mem = memory_line([fill(s[1], pick(s[4])[0]) for s in R.sample(FACTS, 1)], False) if R.random() < 0.5 else None
        out.append(dict(kind="动嘴", text=text, memory=mem, state=STAND, target=None))
    for text, reply in TEACH * 3:  # 模型答成动作时用写好的回答
        if _NORM(text) in EVAL_Q:
            continue
        out.append(dict(kind="动嘴", text=text, memory=None, state=STAND, target=I._out("chat", [], "happy", reply)))
    return out


# ------------------------------------------------------------------ 蒸馏: 模型自己答对的就用它自己的回答
async def answer(llm, ex: dict, with_memory: bool) -> dict:
    parser, meta = JSONObjectStream(("reply",)), {}
    msgs = I.build_messages(ex["text"], state=ex["state"], memory=ex["memory"] if with_memory else None)
    async for delta in llm.stream(msgs, schema=I.build_schema(), max_tokens=160, meta=meta):
        parser.feed(delta)
    return parser.result


def mentions_memory(reply: str, memory: str | None) -> bool:
    if not memory:
        return False
    words = set(re.findall(r"[一-鿿《》A-Za-z]{2,}", re.sub(r"用户|这是第|次见面|一共聊过|轮", " ", memory)))
    return any(w in reply for w in words if len(w) >= 2)


def keep_model(ex: dict, r: dict) -> bool:
    """模型的回答能不能直接当目标."""
    op, acts, reply = r.get("op"), I.normalize_acts(r.get("acts") or []), r.get("reply") or ""
    if not reply or len(reply) > 40 or P.check(ex["text"], op, [a["do"] for a in acts], reply):
        return False
    if ex["kind"] == "做得到":
        want = [(a["do"], a.get("to")) for a in ex["acts"]]
        return op == "act" and [(a["do"], a.get("to")) for a in acts][: len(want)] == want
    if ex["kind"] == "做不到":
        return op == "na" and not acts and bool(_HONEST_RE.search(reply))
    if ex["kind"] in ("无关", "动嘴"):
        return op == "chat" and not mentions_memory(reply, ex["memory"])
    return False


async def distill(examples: list[dict], seed: int) -> Counter:
    cfg = load_config(overrides=["llm.engine=llamacpp"])
    llm = create_llm(cfg["llm"])
    if not await llm.health():
        sys.exit(f"LLM 不可用: {cfg['llm'].get('endpoint')} (先运行 python tools/start_llm.py, 或加 --no-distill)")
    body = llm._body
    llm._body = lambda *a, **k: {**body(*a, **k), "seed": seed}
    count = Counter()
    for i, ex in enumerate(examples):
        if ex["kind"].startswith("角色") or ex["kind"] == "主动提起":  # 模型几乎答不对的类别, 直接用规则写的回答
            ex["source"] = "规则"
            continue
        # 无关的话: 模型在不带记忆时的回答 (不会扯到记忆), 作为带记忆时的目标
        r = await answer(llm, ex, with_memory=ex["kind"] not in ("无关", "动嘴"))
        if keep_model(ex, r):
            ex["target"], ex["source"] = {k: r.get(k) for k in ("op", "acts", "emo", "reply")}, "蒸馏"
            ex["target"]["acts"] = [{k: v for k, v in a.items() if v is not None} for a in r.get("acts") or []]
        elif ex["target"] is not None:
            ex["source"] = "规则"
        else:
            ex["source"] = "丢弃"
        count[(ex["kind"], ex["source"])] += 1
        if i % 50 == 0:
            print(f"  {i}/{len(examples)} {dict(count)}", flush=True)
    await llm.close()
    return count


def to_record(ex: dict) -> dict:
    return {"kind": ex["kind"], "source": ex.get("source", "规则"), "text": ex["text"],
            "messages": I.build_messages(ex["text"], state=ex["state"], memory=ex["memory"]),
            "target": I.dumps_compact(ex["target"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-distill", action="store_true")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()
    examples = (role_examples(380, 160, 130) + intent_examples(320, 200) + chat_examples(50, 30)
                + greet_examples(110) + state_examples(110))
    R.shuffle(examples)
    if args.no_distill:
        for ex in examples:
            ex["source"] = "规则" if ex["target"] is not None else "丢弃"
    else:
        asyncio.run(distill(examples, args.seed))
    examples = [ex for ex in examples if ex.get("source") != "丢弃"]
    # 按问句分训练/验证 (同一句话不同时出现在两边)
    texts = sorted({ex["text"] for ex in examples})
    R.shuffle(texts)
    val_texts = set(texts[: len(texts) // 10])
    OUT.mkdir(parents=True, exist_ok=True)
    split = {"train": [e for e in examples if e["text"] not in val_texts], "val": [e for e in examples if e["text"] in val_texts]}
    for name, rows in split.items():
        with (OUT / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for ex in rows:
                f.write(json.dumps(to_record(ex), ensure_ascii=False) + "\n")
    c = Counter((e["kind"], e.get("source")) for e in examples)
    lines = ["# LoRA 训练数据", "", f"训练 {len(split['train'])} 条, 验证 {len(split['val'])} 条 (按问句分开). 评测文本 {len(EVAL)} 条已去重.", "",
             "| 类别 | 蒸馏 | 规则 |", "|---|---|---|"]
    for k in dict.fromkeys(e["kind"] for e in examples):
        lines.append(f"| {k} | {c[(k, '蒸馏')]} | {c[(k, '规则')]} |")
    lines += ["", "示例:", ""] + [f"- {e['kind']} / {e.get('source')}: {e['text']} | {e['memory'] or ''} -> {I.dumps_compact(e['target'])}"
                                  for e in R.sample(examples, 12)]
    (OUT / "stats.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
