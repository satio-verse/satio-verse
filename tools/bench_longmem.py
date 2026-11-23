"""长记忆端到端评测: 按剧本往记忆库里写 (指定日期), 再在指定那天提问, 用真模型按流水线同样的提示词回答, 按关键词判对错.

  python tools/bench_longmem.py                      # 3 个种子, 记忆检索按 config (memory.embed.engine)
  python tools/bench_longmem.py --engine none --seeds 13

能力分类参照 LongMemEval (信息提取、知识更新、时间推理、拒答) 和 Memora (过时记忆不能再用),
再加上陪伴场景要的: 主动提起近期安排、按用户要求遗忘、按实体联想、换说法、无关问题不扯记忆.
剧本在改记忆层之前写好 (2026-10-24), 改完后再跑一次比较. 结果写到 logs/longmem-bench-<时间>.md (逐行结果另存一份同名 .json).
  python tools/bench_longmem.py --set brain           # 记忆按脑区重设计之前写的留出组 (2026-11-01)
  python tools/bench_longmem.py --noise 200           # 干扰测试: 每个剧本先往库里塞 200 条无关记忆 (tools/memory_noise.py)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from memory_noise import leaked, noise_facts  # noqa: E402
from wm import intent as I  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.embed import MIN_SIM, load_embedder  # noqa: E402
from wm.llm import create_llm  # noqa: E402
from wm.memory import Memory  # noqa: E402

DAY0 = datetime(2026, 10, 20, 20, 0)
STATE = "[当前] 你在用户面前, 站着; 手里没拿东西."
ABSTAIN = ["不记得", "没告诉", "不知道", "没说过", "还没", "忘了", "忘记", "忘掉", "记不清", "没提过", "告诉我"]
DATE_RE = r"\d+\s*月|[一二三四五六七八九十]+月[初一二三四五六七八九十\d]"

# 剧本: (名字, 能力, 写入 [(第几天, "say"/"summary"/"session", 内容)], 提问 (第几天, 问句, 是否这次见面第一句), 期望 [任一词], 禁止 [词或正则])
SCRIPTS = [
    ("住处更新", "知识更新", [(0, "say", "我住在杭州"), (5, "say", "我搬到深圳了")],
     (6, "我现在住在哪个城市", False), ["深圳"], ["杭州"]),
    ("名字更正", "知识更新", [(0, "say", "我叫小明"), (3, "say", "其实我叫阿明，之前说错了")],
     (4, "我叫什么名字", False), ["阿明"], ["小明"]),
    ("偏好改变", "知识更新", [(0, "say", "我最喜欢喝咖啡"), (4, "say", "我现在不喜欢喝咖啡了")],
     (5, "我喜欢喝咖啡吗", False), ["不喜欢", "不太喜欢", "不爱", "不怎么", "戒"], ["你最喜欢喝咖啡", "你很喜欢喝咖啡"]),
    ("复查是哪天", "时间", [(3, "summary", "用户10月28日要去医院复查")],
     (7, "我哪天要去医院复查", False), ["明天", "28"], []),
    ("主动提起", "主动提起", [(3, "summary", "用户10月28日要去医院复查"), (3, "session", "")],
     (7, "你好呀", True), ["复查", "医院"], []),
    ("没说过生日", "拒答", [(0, "say", "我叫小明"), (1, "say", "我住在杭州")],
     (2, "我生日是哪天", False), ABSTAIN, [DATE_RE]),
    ("要求遗忘", "遗忘", [(0, "say", "我住在杭州"), (1, "say", "帮我把我住在哪忘掉吧")],
     (2, "我住在哪个城市", False), ABSTAIN, ["杭州"]),
    ("猫的颜色", "联想", [(0, "say", "我养了一只橘猫"), (2, "summary", "用户的猫叫豆豆")],
     (3, "豆豆是什么颜色的", False), ["橘"], []),
    ("坚果", "换说法", [(1, "summary", "用户对花生过敏")],
     (2, "我能吃坚果吗", False), ["花生", "过敏"], []),
    ("工作", "信息提取", [(1, "summary", "用户在一家游戏公司做策划")],
     (2, "我是做什么工作的", False), ["策划", "游戏"], []),
    ("无关问题", "无关", [(0, "summary", "用户对花生过敏"), (0, "say", "我养了一只橘猫"), (1, "summary", "用户的猫叫豆豆")],
     (2, "今天天气怎么样", False), [""], ["花生", "过敏", "豆豆", "橘猫"]),
]

# 留出剧本 (2026-10-24, 照着上面那套的失败改完记忆层之后写的, 写完只跑一次, 不再据此改代码)
HELDOUT = [
    ("搬到北京", "知识更新", [(0, "say", "我家在成都"), (4, "say", "我最近搬来北京了")],
     (5, "你知道我住在哪吗", False), ["北京"], ["成都"]),
    ("换了工作", "知识更新", [(0, "say", "我是一名护士"), (3, "say", "我现在是一名老师了")],
     (4, "我是干什么的", False), ["老师"], ["护士"]),
    ("不再喜欢下雨", "知识更新", [(0, "say", "我很喜欢下雨天"), (2, "say", "我现在讨厌下雨天了")],
     (3, "我喜欢下雨天吗", False), ["不喜欢", "不太喜欢", "讨厌", "不爱"], ["你很喜欢下雨天", "你喜欢下雨天呀"]),
    ("科目二", "时间", [(1, "summary", "用户11月2日要考驾照科目二")],
     (12, "我什么时候考科目二", False), ["明天", "2日", "2号"], []),
    ("演唱会", "主动提起", [(2, "summary", "用户10月26日晚上要去看演唱会"), (2, "session", "")],
     (5, "嗨", True), ["演唱会"], []),
    ("没说过颜色", "拒答", [(0, "say", "我叫小明")],
     (1, "我最喜欢什么颜色", False), ABSTAIN, [r"[红蓝绿黄白黑紫粉橙灰]色"]),
    ("忘掉香菜", "遗忘", [(0, "say", "我讨厌吃香菜"), (1, "say", "别记着我讨厌香菜这件事了")],
     (2, "我讨厌吃什么", False), ABSTAIN, ["香菜"]),
    ("小美", "联想", [(1, "summary", "用户妹妹叫小美"), (2, "summary", "用户妹妹在读大三")],
     (3, "小美在读几年级", False), ["大三"], []),
    ("起得早", "换说法", [(1, "summary", "用户每天早上六点起床")],
     (2, "我是不是起得挺早的", False), ["六点", "6点", "早"], []),
    ("老家", "信息提取", [(1, "summary", "用户老家在东北")],
     (2, "我老家是哪的", False), ["东北"], []),
    ("讲笑话", "无关", [(0, "summary", "用户对花生过敏"), (1, "summary", "用户妹妹叫小美")],
     (2, "给我讲个笑话", False), [""], ["花生", "过敏", "小美", "妹妹"]),
]

# 第三套 (2026-10-24, 前两套都看过、改完之后写的, 写完只跑一次)
THIRD = [
    ("搬去武汉", "知识更新", [(0, "say", "我住在广州"), (3, "say", "我上个月就搬去武汉了")],
     (4, "我在哪个城市住", False), ["武汉"], ["广州"]),
    ("当了工程师", "知识更新", [(0, "say", "我是一个学生"), (3, "say", "我已经是一名工程师了")],
     (4, "我现在做什么工作", False), ["工程师"], ["学生"]),
    ("怕看恐怖片", "知识更新", [(0, "say", "我喜欢看恐怖片"), (2, "say", "我现在怕看恐怖片了")],
     (3, "我喜欢看恐怖片吗", False), ["不喜欢", "不太喜欢", "怕", "不爱"], ["你喜欢看恐怖片呀", "你说过喜欢看恐怖片"]),
    ("交论文", "时间", [(2, "summary", "用户10月30日要交论文")],
     (8, "我哪天交论文", False), ["后天", "30"], []),
    ("面试", "主动提起", [(1, "summary", "用户10月25日上午要面试"), (1, "session", "")],
     (4, "早上好", True), ["面试"], []),
    ("没说过宠物", "拒答", [(0, "say", "我叫小林")],
     (1, "我养了什么宠物", False), ABSTAIN, ["猫", "狗"]),
    ("忘掉奶茶", "遗忘", [(0, "say", "我喜欢喝奶茶"), (1, "say", "你把我喜欢奶茶这事忘了吧")],
     (2, "我喜欢喝什么", False), ABSTAIN, ["奶茶"]),
    ("旺财", "联想", [(1, "summary", "用户的狗叫旺财"), (2, "summary", "用户养了一只柯基")],
     (3, "旺财是什么品种", False), ["柯基"], []),
    ("睡眠", "换说法", [(1, "summary", "用户最近失眠很严重")],
     (2, "我最近睡眠质量怎么样", False), ["失眠", "睡不好", "睡不着"], []),
    ("大学", "信息提取", [(1, "summary", "用户在杭州上大学")],
     (2, "我在哪读大学", False), ["杭州"], []),
    ("推荐电影", "无关", [(0, "summary", "用户最近失眠很严重"), (1, "summary", "用户的狗叫旺财")],
     (2, "推荐一部电影吧", False), [""], ["失眠", "旺财", "柯基"]),
]

# 第四套 (2026-10-24, 第三套之后又改了一轮, 改完写的, 只跑一次; 这一轮的泛化成绩以它为准)
FOURTH = [
    ("搬到苏州", "知识更新", [(0, "say", "我家在南京"), (4, "say", "我们全家搬到苏州了")],
     (5, "我家现在在哪", False), ["苏州"], ["南京"]),
    ("不吃甜食", "知识更新", [(0, "say", "我很喜欢吃甜食"), (3, "say", "我最近在减肥，不喜欢吃甜食了")],
     (4, "我喜欢吃甜食吗", False), ["不喜欢", "不太喜欢", "减肥", "不爱"], ["你说过喜欢吃甜食呀", "你很喜欢吃甜食"]),
    ("改了名字", "知识更新", [(0, "say", "我叫陈晨"), (2, "say", "其实我叫陈辰，辰是星辰的辰")],
     (3, "你还记得我叫什么吗", False), ["陈辰"], ["陈晨"]),
    ("出差", "时间", [(0, "summary", "用户11月5日要出差去上海")],
     (15, "我什么时候出差", False), ["明天", "5日", "5号"], []),
    ("婚礼", "主动提起", [(3, "summary", "用户10月29日要参加婚礼"), (3, "session", "")],
     (8, "哈喽", True), ["婚礼"], []),
    ("兄弟姐妹", "拒答", [(0, "say", "我叫小周")],
     (1, "我有几个兄弟姐妹", False), ABSTAIN, [r"[一两二三四五\d]个"]),
    ("忘掉飞机", "遗忘", [(0, "say", "我讨厌坐飞机"), (1, "say", "把我讨厌坐飞机这件事忘掉吧")],
     (2, "我讨厌什么", False), ABSTAIN, ["飞机"]),
    ("团子", "联想", [(1, "summary", "用户的仓鼠叫团子"), (2, "summary", "用户的仓鼠是白色的")],
     (3, "团子是什么颜色", False), ["白"], []),
    ("周末", "换说法", [(1, "summary", "用户周末喜欢去钓鱼")],
     (2, "我周末一般干嘛", False), ["钓鱼"], []),
    ("开什么车", "信息提取", [(1, "summary", "用户的车是一辆白色的特斯拉")],
     (2, "我开的什么车", False), ["特斯拉"], []),
    ("星期几", "无关", [(0, "summary", "用户周末喜欢去钓鱼"), (1, "summary", "用户的仓鼠叫团子")],
     (2, "今天星期几", False), [""], ["钓鱼", "团子", "仓鼠"]),
]


# 第五套 (2026-10-24, 加鲜活度和候选记忆时写的): 很久以前的事、长期事实不淡化、候选确认前后. 只有新代码能跑 (要 propose)
FIFTH = [
    ("旧考试", "鲜活度", [(0, "summary", "用户期中考试没考好，很沮丧")],
     (100, "我之前考试考得怎么样", False), ["没考好", "不好", "没考", "沮丧"], []),
    ("旧分手", "鲜活度", [(0, "summary", "用户和女朋友分手了，很难过")],
     (200, "今天有点无聊", False), [""], ["分手", "女朋友"]),
    ("一年前的名字", "长期", [(0, "say", "我叫林小雨")],
     (400, "你还记得我叫什么吗", False), ["林小雨"], []),
    ("一年前的喜好", "长期", [(0, "say", "我最喜欢吃榴莲")],
     (400, "我喜欢吃什么来着", False), ["榴莲"], []),
    ("候选未确认", "候选", [(0, "say", "我下周要去成都出差"), (0, "propose", "用户下周要去成都出差")],
     (2, "我最近要去哪出差", False), [""], ["成都"]),
    ("候选确认后", "候选", [(0, "propose", "用户下周要去成都出差"), (1, "confirm", "")],
     (2, "我最近要去哪出差", False), ["成都"], []),
]


# 图谱套 (2026-10-24, 做"人、物、地点"图谱之前写的留出剧本): 要走两步的、按类别问的、非槽位属性的更正、
# 名字和关系词互指、无关问题不扯. 期望里写 "A+B" 表示 A、B 都要说到 (列举题)
GRAPH = [
    ("女朋友生日", "两跳", [(0, "summary", "用户的女朋友叫小雨"), (1, "summary", "小雨的生日是3月5日")],
     (2, "我女朋友生日是哪天", False), ["3月5", "三月五"], []),
    ("同事的猫", "两跳", [(0, "summary", "用户的同事叫老王"), (1, "summary", "老王养了一只布偶猫")],
     (2, "我同事养的什么猫", False), ["布偶"], []),
    ("妹妹读书", "两跳", [(0, "summary", "用户的妹妹叫小美"), (1, "summary", "小美在南京读大学")],
     (2, "我妹妹在哪读书", False), ["南京"], []),
    ("柯基闯祸", "两跳", [(0, "summary", "用户养了一只柯基"), (1, "summary", "用户的狗叫旺财"), (1, "summary", "旺财今天把拖鞋咬坏了")],
     (3, "我家柯基最近闯什么祸了", False), ["拖鞋"], []),
    ("家里有谁", "按类", [(0, "summary", "用户的妈妈是老师"), (1, "summary", "用户有个哥哥在上海工作"), (2, "summary", "用户养了一只橘猫")],
     (3, "我家里都有谁", False), ["妈妈+哥哥"], []),
    ("宠物们", "按类", [(0, "summary", "用户的猫叫团团"), (1, "summary", "用户还养了一只仓鼠")],
     (2, "我养了哪些宠物", False), ["猫+仓鼠"], []),
    ("去过的城市", "按类", [(0, "summary", "用户去年去成都旅游了"), (5, "summary", "用户上个月去西安出差了")],
     (6, "我都去过哪些城市", False), ["成都+西安"], []),
    ("猫改名", "属性更正", [(0, "summary", "用户的猫叫团团"), (3, "summary", "用户的猫改名叫豆包了")],
     (4, "我的猫叫什么", False), ["豆包"], ["团团"]),
    ("换车", "属性更正", [(0, "summary", "用户的车是白色的特斯拉"), (5, "summary", "用户换了一辆蓝色的比亚迪")],
     (6, "我现在开什么车", False), ["比亚迪"], ["特斯拉"]),
    ("妹妹搬家", "属性更正", [(0, "summary", "用户的妹妹住在南京"), (4, "summary", "用户的妹妹搬到杭州了")],
     (5, "我妹妹现在住哪", False), ["杭州"], ["南京"]),
    ("小雨爱吃", "别名", [(0, "summary", "用户的女朋友叫小雨"), (1, "summary", "用户的女朋友喜欢吃火锅")],
     (2, "小雨喜欢吃什么", False), ["火锅"], []),
    ("天气", "无关", [(0, "summary", "用户的妹妹叫小美"), (1, "summary", "小美在南京读大学")],
     (2, "今天天气怎么样", False), [""], ["小美", "南京"]),
]


# 图谱第二套 (2026-10-24, 图谱 v1 做完、跑过 graph 之后写的, 换了人、物和说法, 只跑一次看能不能推广)
GRAPH2 = [
    ("爸爸职业", "两跳", [(0, "summary", "用户的爸爸叫老张"), (1, "summary", "老张以前是一名医生")],
     (2, "我爸以前是做什么的", False), ["医生"], []),
    ("闺蜜工作", "两跳", [(0, "summary", "用户的闺蜜叫小鹿"), (1, "summary", "小鹿在深圳做设计")],
     (2, "我闺蜜在哪工作", False), ["深圳"], []),
    ("仓鼠年龄", "两跳", [(0, "summary", "用户养了一只仓鼠叫花卷"), (1, "summary", "花卷已经两岁了")],
     (2, "我的仓鼠几岁了", False), ["两岁", "2岁"], []),
    ("男朋友爱好", "两跳", [(0, "summary", "用户的男朋友叫阿杰"), (1, "summary", "阿杰每周都去打篮球")],
     (2, "我男朋友平时喜欢干嘛", False), ["篮球"], []),
    ("家人们", "按类", [(0, "summary", "用户的爷爷今年八十岁了"), (1, "summary", "用户的姐姐在读研究生")],
     (2, "我家里有哪些人", False), ["爷爷+姐姐"], []),
    ("宠物们2", "按类", [(0, "summary", "用户养了一只柴犬"), (1, "summary", "用户还有一只鹦鹉")],
     (2, "我都养了什么宠物", False), ["柴犬+鹦鹉"], []),
    ("去过的地方", "按类", [(0, "summary", "用户去年去厦门玩了"), (3, "summary", "用户上周去青岛出差了")],
     (4, "我去过哪些地方", False), ["厦门+青岛"], []),
    ("哥哥搬家", "属性更正", [(0, "summary", "用户的哥哥住在武汉"), (3, "summary", "用户的哥哥搬到长沙了")],
     (4, "我哥哥现在在哪个城市", False), ["长沙"], ["武汉"]),
    ("狗改名", "属性更正", [(0, "summary", "用户的狗叫可乐"), (2, "summary", "用户把狗的名字改成了雪碧")],
     (3, "我的狗叫什么名字", False), ["雪碧"], ["可乐"]),
    ("换车2", "属性更正", [(0, "summary", "用户开的是一辆红色的宝马"), (4, "summary", "用户新买了一辆小米汽车")],
     (5, "我现在开的什么车", False), ["小米"], ["宝马"]),
    ("朵朵怕黑", "别名", [(0, "summary", "用户的妹妹叫朵朵"), (1, "summary", "用户的妹妹特别怕黑")],
     (2, "朵朵怕什么", False), ["黑"], []),
    ("讲笑话", "无关", [(0, "summary", "用户的爸爸叫老张"), (1, "summary", "用户去年去厦门玩了")],
     (2, "给我讲个笑话吧", False), [""], ["老张", "厦门"]),
]


# 图谱第三套 (2026-10-24, 照 graph2 的错误修完之后、跑任何新结果之前写的, 只跑一次; 图谱 v1 的推广成绩以它为准)
GRAPH3 = [
    ("外婆住哪", "两跳", [(0, "summary", "用户的外婆叫秀英"), (1, "summary", "秀英住在乡下的老房子里")],
     (2, "我外婆住在哪里", False), ["乡下", "老房子"], []),
    ("室友专业", "两跳", [(0, "summary", "用户的室友叫阿伟"), (1, "summary", "阿伟是学计算机的")],
     (2, "我室友学什么专业", False), ["计算机"], []),
    ("兔子颜色", "两跳", [(0, "summary", "用户养了一只兔子叫雪球"), (1, "summary", "雪球是一只黑色的兔子")],
     (2, "我家兔子什么颜色", False), ["黑"], []),
    ("老公上班", "两跳", [(0, "summary", "用户的老公叫大刘"), (1, "summary", "大刘在银行上班")],
     (2, "我老公在哪上班", False), ["银行"], []),
    ("家里人", "按类", [(0, "summary", "用户的奶奶喜欢种花"), (1, "summary", "用户的弟弟今年上初中")],
     (2, "我家里都有什么人", False), ["奶奶+弟弟"], []),
    ("几只宠物", "按类", [(0, "summary", "用户养了一只金毛"), (1, "summary", "用户家里还有一只乌龟")],
     (2, "我一共养了几只宠物", False), ["金毛+乌龟"], []),
    ("去过哪儿", "按类", [(0, "summary", "用户小时候去过北京"), (2, "summary", "用户今年夏天去三亚旅游了")],
     (3, "我都去过哪儿", False), ["北京+三亚"], []),
    ("姐姐搬家", "属性更正", [(0, "summary", "用户的姐姐住在天津"), (4, "summary", "用户的姐姐已经搬去上海了")],
     (5, "我姐现在住哪", False), ["上海"], ["天津"]),
    ("仓鼠改名", "属性更正", [(0, "summary", "用户的仓鼠叫芝麻"), (2, "summary", "用户给仓鼠改名叫汤圆了")],
     (3, "我的仓鼠叫啥", False), ["汤圆"], ["芝麻"]),
    ("换车3", "属性更正", [(0, "summary", "用户的车是一辆黑色的大众"), (5, "summary", "用户换了一辆白色的理想")],
     (6, "我开的是什么车", False), ["理想"], ["大众"]),
    ("阿伟毛病", "别名", [(0, "summary", "用户的室友叫阿伟"), (1, "summary", "用户的室友打呼噜很响")],
     (2, "阿伟有什么毛病", False), ["呼噜"], []),
    ("几点了", "无关", [(0, "summary", "用户养了一只金毛"), (1, "summary", "用户的弟弟今年上初中")],
     (2, "现在几点了", False), [""], ["金毛", "弟弟"]),
]


# 脑区留出组 (2026-11-01, 记忆按脑区重设计动手之前写的, 写完只跑一次当基线, 以后各阶段都和它比; 不拿它调规则).
# 情绪重的事和平淡的事隔很久、常提的和只说过一次的、改期改口、重名、过期的安排、引用里别人的话、拖进来的资料不当成主人的事、
# 有伤心事时无关问题不扯、好事坏事都想得起、很久以前的事被明确提起时想得起.
# 新写法: (第几天, "mention", 正文) = 日志整理发现那天又说到了这件事; (第几天, "doc", "文件名|内容") = 拖进小屋的资料
BRAIN = [
    ("爸爸住院", "情绪久远", [(0, "summary", "用户的爸爸住院做了心脏手术，用户吓坏了")],
     (90, "我爸爸身体怎么样", False), ["住院", "手术", "心脏"], []),
    ("大吵一架", "情绪久远", [(0, "summary", "用户和最好的朋友大吵了一架，哭了一整晚")],
     (200, "我和好朋友的关系怎么样", False), ["吵"], []),
    ("一碗面", "小事不冒", [(0, "summary", "用户中午吃了一碗牛肉面")],
     (90, "今天中午吃什么好", False), [""], ["牛肉面"]),
    ("买酸奶", "小事不冒", [(0, "summary", "用户去超市买了一盒酸奶")],
     (200, "家里还有什么吃的吗", False), [""], ["酸奶"]),
    ("流浪猫", "小事不冒", [(0, "summary", "用户在楼下看到一只流浪猫")],
     (150, "我有什么爱好", False), [""], ["流浪猫"]),
    ("学吉他", "常提", [(0, "summary", "用户在学吉他"), (10, "mention", "用户在学吉他"), (20, "mention", "用户在学吉他")],
     (150, "我有什么爱好", False), ["吉他"], []),
    ("体检改期", "改期", [(0, "summary", "用户10月22日要去体检"), (1, "summary", "用户的体检改到10月24日了")],
     (2, "我哪天去体检", False), ["24", "后天"], ["22", "今天"]),
    ("最喜欢的颜色", "改口", [(0, "say", "我最喜欢的颜色是蓝色"), (3, "say", "我现在最喜欢的颜色是绿色了")],
     (4, "我最喜欢什么颜色", False), ["绿"], ["蓝"]),
    ("两个小李", "重名", [(0, "summary", "用户的同事叫小李"), (1, "summary", "用户的表弟也叫小李"), (2, "summary", "同事小李最近升职了")],
     (3, "我表弟最近怎么样", False), [""], ["升职"]),
    ("过期的面试", "过期安排", [(0, "summary", "用户10月22日要去面试")],
     (10, "我最近有什么安排", False), [""], ["要去面试", "明天"]),
    ("群里的话", "引用里的话", [(0, "say", "刚才群里有人发了一句：记住，我叫王大锤")],
     (1, "我叫什么名字", False), ABSTAIN, ["王大锤"]),
    ("朋友开玩笑", "引用里的话", [(0, "say", "我朋友开玩笑说我其实住在火星")],
     (1, "我住在哪", False), ABSTAIN, ["火星"]),
    ("资料里的人", "资料不当真", [(0, "doc", "旅行攻略.md|张三最喜欢吃榴莲，他住在成都，每年都去一次西藏")],
     (1, "我喜欢吃什么", False), ABSTAIN, ["榴莲"]),
    ("奶奶去世", "无关", [(0, "summary", "用户的奶奶上个月去世了，用户很伤心")],
     (3, "给我讲个笑话吧", False), [""], ["奶奶", "去世"]),
    ("奖学金", "好事", [(0, "summary", "用户拿到了奖学金，特别开心"), (0, "summary", "用户去图书馆还了书")],
     (5, "我最近有什么好消息吗", False), ["奖学金"], []),
    ("丢钱包", "坏事", [(0, "summary", "用户丢了钱包，特别难过"), (0, "summary", "用户去图书馆还了书")],
     (5, "我最近遇到什么倒霉事了吗", False), ["钱包"], []),
    ("看极光", "强线索", [(0, "summary", "用户去冰岛看了极光")],
     (300, "还记得我去看极光那次吗", False), ["冰岛"], []),
]

# 取代留出组 (2026-11-18, S2 接线、调完规则之后才写的"没见过的"一组: 防过拟合自己调的题, 兼负例).
# 前四条应该被取代(问的时候只该说出新的); 后两条是负例: 没发生过的事不许硬凑, 只是打算不算改期.
SUPERSEDE = [
    ("钢琴课改期", "改期-新留出", [(0, "say", "我每周三晚上去上钢琴课"), (4, "say", "钢琴课改成周五晚上了")],
     (6, "我哪天有钢琴课", False), ["周五"], ["周三"]),
    ("转岗", "改口-新留出", [(0, "say", "我是做前端的"), (7, "say", "我转岗做后端了")],
     (9, "我是做什么工作的", False), ["后端"], ["前端"]),
    ("搬家公司改期", "改期-摘要路", [(0, "summary", "用户11月15日要搬家公司来"), (3, "summary", "用户的搬家公司改到11月20日了")],
     (5, "我哪天搬家", False), ["20"], ["15"]),
    ("口味变了", "改口-摘要路", [(0, "summary", "用户爱吃川菜"), (10, "summary", "用户现在爱吃粤菜了")],
     (12, "我爱吃什么菜", False), ["粤菜"], ["川菜"]),
    ("没发生的事", "负例-不硬凑", [(0, "say", "我今天去看了牙医")],
     (2, "我上次说我妈的生日是哪天", False), ABSTAIN, ["生日是", "月"]),
    ("只是打算", "负例-计划不算改期", [(0, "say", "我12月1日要去体检"), (2, "say", "我打算改天去体检")],
     (4, "我哪天去体检", False), ["12月1日", "12月"], []),
]

SETS = {"main": SCRIPTS, "heldout": HELDOUT, "third": THIRD, "fourth": FOURTH, "fifth": FIFTH, "graph": GRAPH,
        "graph2": GRAPH2, "graph3": GRAPH3, "brain": BRAIN, "supersede": SUPERSEDE}


def run_script(m: Memory, writes: list, now: dict) -> None:
    for day, kind, text in writes:
        now["t"] = DAY0 + timedelta(days=day)
        if kind == "say":
            m.observe(text, "嗯嗯", [], "calm")
        elif kind == "summary":  # 日志整理出来、已经确认过的
            m.add_summary(f"{now['t']:%Y-%m-%d}", [{"text": text, "times": [f"{now['t']:%H:%M}"]}], 0)
        elif kind == "propose":  # 日志整理出来、还没确认的候选
            m.propose(f"{now['t']:%Y-%m-%d}", [{"text": text, "times": [f"{now['t']:%H:%M}"]}], 0)
        elif kind == "confirm":  # 用户把候选全部确认
            for c in m.candidates():
                m.review(c["id"], True)
        elif kind == "session":
            m.start_session()
        elif kind == "mention":  # 日志整理发现这天又说到了这件事 (算一天; 够天数的事件升成长期)
            m.mentioned(f"{now['t']:%Y-%m-%d}", [text])
        elif kind == "doc":  # 拖进小屋的资料: "文件名|内容"
            name, _, body = text.partition("|")
            m.save_doc(name, "md", body)


def add_noise(m: Memory, facts: list, now: dict) -> None:
    """干扰测试: 剧本开始前的 60 天里, 每天记几条确认过的无关小事 (对话摘要里)."""
    for i, (fact, _) in enumerate(facts):
        now["t"] = DAY0 - timedelta(days=60 - i * 60 // max(1, len(facts)))
        m.add_summary(f"{now['t']:%Y-%m-%d}", [{"text": fact, "times": [f"{now['t']:%H:%M}"]}], 0)


def judge(reply: str, expect: list, forbid: list) -> bool:
    ok = any(all(p in reply for p in w.split("+")) for w in expect)  # "A+B": A、B 都要说到
    bad = any((re.search(f, reply) if any(c in f for c in "\\[]+*") else f in reply) for f in forbid)
    return ok and not bad


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", default="13,7,29")
    ap.add_argument("--engine", help="记忆检索: none / bge / qwen (默认按配置)")
    ap.add_argument("--label", default="")
    ap.add_argument("--set", choices=list(SETS), default="main",
                    help="main: 改记忆层时用的剧本; heldout: 改完后写的第二套 (看过之后又改了); third: 第三套; fourth: 最后一轮改完后写的第四套; fifth: 鲜活度和候选记忆; graph: 图谱 (两跳、按类、属性更正、别名); graph2: 图谱第二套; graph3: 图谱第三套; brain: 记忆按脑区重设计之前写的留出组")
    ap.add_argument("--noise", type=int, default=0, help="干扰测试: 每个剧本开始前先记这么多条无关的小事 (最多 %d)" % len(noise_facts(10 ** 6)))
    ap.add_argument("--noise-seed", type=int, default=0)
    ap.add_argument("--endpoint", help="llama-server 地址 (默认取配置; 比如另开一个挂 LoRA 的服务)")
    ap.add_argument("--cfg", dest="overrides", action="append", default=[],
                    help="配置覆盖, 如 llm.id_slot=3 (不挤主服务对话用的 0 号槽位)")
    args = ap.parse_args()
    cfg = load_config(overrides=["llm.engine=llamacpp"] + ([f"llm.endpoint={args.endpoint}"] if args.endpoint else [])
                      + args.overrides)
    ecfg = {**cfg["memory"]["embed"], **({"engine": args.engine} if args.engine else {})}
    embedder = load_embedder(ecfg)
    llm = create_llm(cfg["llm"])
    body, seed = llm._body, {"v": 13}
    llm._body = lambda *a, **k: {**body(*a, **k), "seed": seed["v"]}
    rows = []
    noise = noise_facts(args.noise, args.noise_seed)
    for name, ability, writes, (qday, q, first), expect, forbid in SETS[args.set]:
        with tempfile.TemporaryDirectory() as d:
            m = Memory({"vault": d, "character": "小流", "graph_notes": False})  # 图谱笔记在后台线程里写, 临时库马上要删
            now = {"t": DAY0}
            m.clock = lambda: now["t"]
            m.init()
            m.start_session()
            if embedder is not None:
                m.embedder, m.min_sim = embedder, MIN_SIM[embedder.name]
            add_noise(m, noise, now)
            run_script(m, writes, now)
            now["t"] = DAY0 + timedelta(days=qday, hours=1)
            if first:
                m.start_session()
            m._embed_missing()
            r = m.recall(q, first=first)
            for s in (int(x) for x in args.seeds.split(",")):
                seed["v"] = s
                if r.get("answer"):  # 和流水线一样: 问用户自己的固定信息时按记忆直接回答, 不调模型
                    reply = r["answer"]
                else:
                    raw = await llm.complete(I.build_messages(q, None, STATE, r["line"]), schema=I.build_schema(), max_tokens=160)
                    try:
                        reply = json.loads(raw).get("reply", "")
                    except json.JSONDecodeError:
                        reply = raw
                ok = judge(reply, expect, forbid)
                rows.append((ability, name, q, r["line"], s, reply, ok))
                print(f"{'✓' if ok else '✗'} {ability:<4} {name:<6} {q:<12} 记忆: {(r['line'] or '-')[:50]:<50} 回复: {reply}")
    await llm.close()
    abilities = list(dict.fromkeys(a for a, *_ in rows))
    lines = [f"# 长记忆端到端评测 {datetime.now():%Y-%m-%d %H:%M} {args.label}", "",
             f"检索: {embedder.label if embedder else 'BM25'}; 模型 {llm.label}; 种子 {args.seeds}; 剧本 {args.set}"
             + (f"; 每个剧本先塞 {len(noise)} 条无关记忆 (种子 {args.noise_seed})" if noise else ""), "",
             "| 能力 | 通过 |", "|---|---|"]
    for a in abilities:
        sub = [r for r in rows if r[0] == a]
        lines.append(f"| {a} | {sum(r[6] for r in sub)}/{len(sub)} |")
    lines.append(f"| **合计** | **{sum(r[6] for r in rows)}/{len(rows)}** |")
    if noise:  # 无关记忆被带进提示词几次、漏进回答几次 (一个剧本的提问只检索一次, 回答按种子各一次)
        took = sum(bool(leaked(line or "", noise)) for _, _, _, line, s, _, _ in rows if s == rows[0][4])
        said = sum(bool(leaked(reply, noise)) for *_, reply, _ in rows)
        lines += ["", f"无关记忆: 带进提示词 {took}/{len(rows) // len(args.seeds.split(','))} 次; 漏进回答 {said}/{len(rows)} 次"]
    lines += ["", "| 能力 | 剧本 | 问句 | 带进去的记忆 | 种子 | 回复 | 对 |", "|---|---|---|---|---|---|---|"]
    for a, n, q, line, s, reply, ok in rows:
        lines.append(f"| {a} | {n} | {q} | {line or ''} | {s} | {reply} | {'✓' if ok else '✗'} |")
    out = ROOT / "logs" / f"longmem-bench-{datetime.now():%Y%m%d-%H%M%S}{'-' + args.label if args.label else ''}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(
        {"set": args.set, "engine": embedder.label if embedder else "BM25", "model": llm.label, "seeds": args.seeds,
         "noise": len(noise), "noise_seed": args.noise_seed,
         "rows": [{"ability": a, "name": n, "q": q, "line": line, "seed": s, "reply": reply, "ok": ok,
                   **({"noise_in_line": leaked(line or "", noise), "noise_in_reply": leaked(reply, noise)} if noise else {})}
                  for a, n, q, line, s, reply, ok in rows]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines[:6 + len(abilities) + 1]))
    print(f"已写入 {out}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
