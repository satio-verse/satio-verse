"""记忆阶段 5 (强度和稳定性) 的评测台: 不调模型, 不花钱, 不读真库. 计划见 backups/mem5-strength/计划.md.

  python tools/bench_strength.py recall   --tag off --mode off --engines none,bge --noise 0,50,200
  python tools/bench_strength.py longmem  --tag off --mode off --engines none,bge --noise 0,200 [--sets main,brain]
  python tools/bench_strength.py strength --tag on  --mode on  --engines none,bge --noise 0,200 [--group dev|held]
  python tools/bench_strength.py perf     --tag on  --mode on  --blocks 3000
  python tools/bench_strength.py report   --bench longmem --base orig
  python tools/bench_strength.py ornn     (714 题公开评测: 数据下载并核对结构之后才做)

  --code orig     用 backups/mem5-strength/orig/wm/memory.py (改前的代码) 顶掉 wm.memory 再跑, 当"改前"那一列
  --code draft    用 backups/mem5-strength/draft/wm/memory.py (改后的草稿, 评审点头、放行之前 wm/memory.py 还是旧的)
  --code current  用 wm/memory.py (默认)
  --mode          off / shadow / on, 写进 Memory 配置的 memory.strength.mode (改前的代码不认识这个键, 忽略)
结果写到 backups/mem5-strength/results/<bench>-<tag>-<engine>-n<噪声>.json, 不写 logs/.

检索级判分: 模型只看到 "[记忆] ..." 这一行和服务端的直接回答, 所以这里用 (answer or line) 当"回复"判 期望词 / 禁止词;
改前改后 line 和 answer 逐字节一样的行, 提示词就一样, 端到端结果不会有系统性差别 (有差别的行才需要真问模型).

留出规矩 (计划 5.4): STRENGTH_DEV 是写规则、扫参数时看的; STRENGTH_HELD 和 CUES_HELDOUT 在改 wm/ 之前写好、记了哈希发给评审,
第一次跑的结果才算留出, 此后不据此改代码.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
RESULTS = ROOT / "backups" / "mem5-strength" / "results"
ORIG = ROOT / "backups" / "mem5-strength" / "orig" / "wm" / "memory.py"
DRAFT = ROOT / "backups" / "mem5-strength" / "draft" / "wm" / "memory.py"
SIDECAR = ".strength.json"  # 记忆/ 下; {"v": 1, "items": {记忆正文: {"s": 强度 σ, "rd": [有正向信号的日子], ...}}}
SIGMA0 = 0.5
AFFECT: dict = {}   # --affect on: 4b (情绪) 开着跑
UNLOCK_OFF = False  # --code orig 时改前的 Memory 没有 _test_unlock_strength_on 这个参数
PARAMS: dict = {}  # --param a=0.5 这样的参数扫描, 并进 memory.strength 配置
DAY0 = datetime(2026, 10, 20, 20, 0)

# ----------------------------------------------------------------------------------------------------------------
# 纠正 / 肯定 / 拒绝 / 无 的句子 (计划 3.1; 标签: 纠 = 纠正, 肯 = 肯定, 拒 = 拒绝 (只对种子 seed:habit:2), 无 = 都不是).
# 假阳性 (无 -> 别的) 必须 0; 留出组召回 >= 80% 即可. 含"不是，我是说……""不对啊我想想""对了，换个话题"这类口头语 (评审 E).
CUES_DEV = [
    ("不对，我不是这么说的", "纠"), ("不对，不是这样的", "纠"), ("你记错了", "纠"), ("错了，我是10月才搬的家", "纠"),
    ("我没说过这话", "纠"), ("不是这样，我说的是另一件事", "纠"), ("不对不对，不是杭州", "纠"), ("你说错了", "纠"),
    ("不是的，你记错了", "纠"),
    ("对，没错", "肯"), ("对对对，就是这样", "肯"), ("是的，你记得真清楚", "肯"), ("没错没错", "肯"), ("对的", "肯"),
    ("嗯对，就是这个", "肯"), ("你记性真好，对", "肯"),
    ("别这么早叫我", "拒"), ("不要一大早就打招呼", "拒"), ("别老跟我打招呼", "拒"),
    ("不是，我是说明天", "无"), ("不对啊我想想", "无"), ("对了，换个话题", "无"), ("这样不对吗？", "无"),
    ("你说得对吗？", "无"), ("对不起，我刚才走神了", "无"), ("不是吧，真的假的", "无"), ("错过了今天的会", "无"),
    ("好的", "无"), ("谢谢", "无"), ("今天天气不错", "无"),
]
CUES_HELDOUT = [
    ("不对，我说的是上海", "纠"), ("这不对，我没这么讲过", "纠"), ("你搞错了", "纠"), ("记错了吧，是周三不是周四", "纠"),
    ("不是这样的，你理解反了", "纠"), ("我从来没说过", "纠"), ("不对，不是那个意思", "纠"), ("说反了", "纠"),
    ("对，就是这样", "肯"), ("没错，你说得对", "肯"), ("是啊，你记得很准", "肯"), ("对啊对啊", "肯"), ("正是这样", "肯"),
    ("嗯，没错", "肯"), ("你说对了", "肯"),
    ("别再这么早叫醒我了", "拒"), ("不用每次都打招呼", "拒"), ("早上别跟我打招呼", "拒"),
    ("不是，我的意思是后天", "无"), ("不对，等一下我再想想", "无"), ("对了，我想问个别的", "无"), ("这个答案不对吗？", "无"),
    ("你确定对吗", "无"), ("对不住，刚才在忙", "无"), ("不是很懂", "无"), ("错题本找不到了", "无"), ("好吧", "无"),
    ("哈哈哈", "无"), ("今天好累", "无"), ("对手太强了", "无"),
]

# 评审返工第 1 轮给的 24 句 (原来是没给我看的留出, 我的分类器在上面跑不过 -> 现在这组只能当开发组用; 评审另有一组没给我看的 24 句, 改完他跑一次才算留出)
CUES_DEV2 = [
    ("不是不是，你弄错了", "纠"), ("错啦，是上周", "纠"), ("记岔了，是我表姐", "纠"), ("不对哦，是下午三点", "纠"),
    ("根本不是这么回事", "纠"), ("你乱说，我哪有", "纠"), ("我啥时候说过这个", "纠"),
    ("嗯嗯对的", "肯"), ("对呀", "肯"), ("没毛病", "肯"), ("是这样没错", "肯"), ("就是就是", "肯"),
    ("以后早上别吵我", "拒"), ("早上不用问好啦", "拒"),
    ("不对劲，电脑又卡了", "无"), ("不是我说你，你也该睡了", "无"), ("错了错了，我刚才按错键了", "无"), ("对了对了，我想起来一件事", "无"),
    ("对面那家店关门了", "无"), ("没错过末班车，好险", "无"), ("是的话我就去", "无"), ("你说得对，但是我还是不想去", "无"),
    ("别叫我宝宝", "无"), ("不要叫外卖了，我做饭", "无"),
]
# 我自己补的一批"无" (改词表时顺手防假阳性的负例; 开发组)
CUES_DEV3 = [
    ("不对称的两边", "无"), ("对手机没兴趣", "无"), ("错别字太多了", "无"), ("明天早上别忘了叫我", "无"), ("别叫醒我，让我再睡会", "无"),
    ("早上好", "无"), ("对不对？", "无"), ("不是故意的", "无"), ("对我来说有点难", "无"), ("错误日志发你了", "无"),
]

# ----------------------------------------------------------------------------------------------------------------
# 阶段 5 剧本. 每个剧本是 dict:
#   writes  [(第几天, 种类, 内容)], 种类: summary / say / mention / propose / confirm / session / turn
#           turn 的内容 = (用户的话, 她的回复[, {"first": True}] ), 走真实入口: 先 recall(用户的话) 再 observe(用户的话, 回复);
#                       种类 turn_late 是先让时间走 40 分钟再来 (上一轮的 pending 过期了); 回复是写死的 (假模型), "用上没有"由它决定
#   q       (第几天, 问句, 是否这次见面第一句)
#   expect / forbid   期望词 / 禁止词 (judge 的写法, "" 表示什么都行); 判 (answer or line)
#   order   [(甲, 乙)]  命中里含 甲 的排在含 乙 的前面, 甲必须在命中里
#   state   [(种类, 关键词, 比较, 数)]  旁路 (.strength.json) 里的强度 σ, 没有这条 = 中性 0.5, 旁路不存在 = 评不了 (n/a);
#           种类 days = .recall.json 里提到过几天 (改前的代码也能评)
#   hour    这一天里的几点 (默认 20; 种子"早上好"要 5–11 点); char  角色名 (种子包只有 绫音)
def _s(name, ability, writes, q, **kw):
    return {"name": name, "ability": ability, "writes": writes, "q": q, "expect": kw.pop("expect", [""]),
            "forbid": kw.pop("forbid", []), "order": kw.pop("order", []), "state": kw.pop("state", []), **kw}


def _pairs(n, user, reply, ok_line):
    out = []
    for _ in range(n):
        out += [(12, "turn", (user, reply)), (12, "turn", (ok_line, "嗯嗯"))]
    return out


STRENGTH_DEV = [
    _s("复述1/2/4天", "稳定性", [
        (0, "summary", "用户10月21日去了水族馆"), (0, "summary", "用户10月21日去了植物园"),
        (1, "mention", "用户10月21日去了植物园"), (2, "mention", "用户10月21日去了植物园"), (4, "mention", "用户10月21日去了植物园"),
        (4, "turn", ("水族馆那次好玩吗", "是啊那次挺好玩的"))],  # B 只被检索带进提示词、没用上: 对照
       (34, "我10月21日去了哪里", False), order=[("植物园", "水族馆")],
       state=[("rd", "水族馆", "==", 0), ("rd", "植物园", "==", 3)]),
    _s("用上被肯定", "强度", [
        (0, "summary", "用户10月21日去了水族馆"), (0, "summary", "用户10月21日去了植物园"),
        (10, "turn", ("我10月21日去了哪里", "你10月21日去了植物园呀")), (10, "turn", ("对，没错", "嗯嗯"))],
       (45, "我10月21日去了哪里", False), order=[("植物园", "水族馆")],
       state=[("sigma", "植物园", ">", 0.5), ("rd", "植物园", "==", 1)]),
    _s("被纠正的减弱", "强度", [
        (0, "summary", "用户10月21日去了植物园"), (0, "summary", "用户10月21日去了水族馆"),
        (5, "turn", ("我10月21日去了哪里", "你10月21日去了植物园呀")), (5, "turn", ("不对，不是这样", "好的好的"))],
       (6, "我10月21日去了哪里", False), order=[("水族馆", "植物园")], state=[("sigma", "植物园", "<", 0.5)]),
    _s("纠正不误伤", "强度", [
        (0, "summary", "用户10月21日去了水族馆"), (0, "summary", "用户10月21日去了植物园"),
        (5, "turn", ("我10月21日去了哪里", "你10月21日去了植物园呀")), (5, "turn", ("不对，不是这样", "好的好的"))],
       (6, "我10月21日去了哪里", False), order=[("水族馆", "植物园")],
       state=[("sigma", "水族馆", "==", 0.5), ("sigma", "植物园", "<", 0.5)]),
    _s("平白说不对", "强度", [
        (0, "summary", "用户10月21日去了植物园"), (0, "summary", "用户10月21日去了水族馆"),
        (5, "turn", ("不对，不是这样", "嗯")),                                                         # 前面没有带记忆的一轮
        (5, "turn", ("我10月21日去了哪里", "你10月21日去了植物园呀")), (5, "turn_late", ("不对，不是这样", "好的好的")),  # 隔了 40 分钟
        (5, "turn", ("我10月21日去了哪里", "你10月21日去了植物园呀")), (5, "turn", ("这样不对吗？", "嗯"))],  # 问句
       (6, "我10月21日去了哪里", False), state=[("sigma", "植物园", ">=", 0.5)]),
    _s("联想不算复习", "稳定性", [
        (0, "summary", "用户的猫叫豆豆，10月22日它生病了"), (0, "summary", "用户的猫10月23日去了医院")]
       + [(d, "turn", ("豆豆最近怎么样", "豆豆去了医院，已经好多了")) for d in range(1, 11)],
       (11, "豆豆最近怎么样", False), expect=["医院"],
       state=[("days", "去了医院", "==", 1), ("sigma", "去了医院", "==", 0.5), ("rd", "去了医院", "==", 0)]),
    _s("防富者愈富", "强度", [(0, "summary", "用户10月21日去了植物园")] + _pairs(
        20, "我10月21日去了哪里", "你10月21日去了植物园呀", "没错没错"),
       (100, "我10月21日去了哪里", False), state=[("sigma", "植物园", "<=", 0.70), ("sigma", "植物园", ">=", 0.5), ("rd", "植物园", "==", 1)]),
    _s("种子被纠正", "种子", [
        (-1, "say", "我叫小明"), (-1, "session", ""),
        (0, "session", ""), (0, "turn", ("早上好呀", "早上好呀！", {"first": True})), (0, "turn", ("别这么早叫我", "好的好的")),
        (1, "session", ""), (1, "turn", ("早上好呀", "早上好！", {"first": True})), (1, "turn", ("别这么早叫我", "好的好的"))],
       (2, "早上好呀", True), forbid=["先说声早上好"], char="绫音", hour=8, state=[("sigma", "seed:habit:2", "<", 0.25)]),
    _s("确认", "强度", [
        (0, "summary", "用户10月21日去了水族馆"), (0, "propose", "用户10月21日去了植物园"), (0, "confirm", "")],
       (35, "我10月21日去了哪里", False), order=[("植物园", "水族馆")], state=[("sigma", "植物园", ">", 0.5)]),
    _s("无关问题不扯", "无关", [
        (0, "summary", "用户10月21日去了水族馆"), (0, "summary", "用户10月21日去了植物园"),
        (10, "turn", ("我10月21日去了哪里", "你10月21日去了植物园呀")), (10, "turn", ("对，没错", "嗯嗯"))],
       (45, "今天天气怎么样", False), forbid=["植物园", "水族馆"]),
    _s("只被检索没用上", "稳定性", [(0, "summary", "用户10月21日去了植物园")]  # 评审 A: 9 个不同的日子直接命中, 回复都不提它
       + [(d, "turn", ("我10月21日去了哪里", "我想想啊，不太清楚呢")) for d in range(1, 10)],
       (10, "我10月21日去了哪里", False), state=[("rd", "植物园", "==", 0), ("sigma", "植物园", "==", 0.5), ("days", "植物园", ">=", 9)]),
]

STRENGTH_HELD = [
    _s("复述1/3/6天", "稳定性", [
        (0, "summary", "用户10月23日修好了收音机"), (0, "summary", "用户10月23日修好了自行车"),
        (1, "mention", "用户10月23日修好了自行车"), (3, "mention", "用户10月23日修好了自行车"), (6, "mention", "用户10月23日修好了自行车"),
        (6, "turn", ("收音机修得怎么样", "还行吧"))],
       (36, "我10月23日修好了什么", False), order=[("自行车", "收音机")],
       state=[("rd", "收音机", "==", 0), ("rd", "自行车", "==", 3)]),
    _s("用上被肯定", "强度", [
        (0, "summary", "用户10月22日买了一盏灯笼"), (0, "summary", "用户10月22日买了一本字典"),
        (12, "turn", ("我10月22日买了什么", "你买了一本字典呀")), (12, "turn", ("是的，没错", "嗯嗯"))],
       (45, "我10月22日买了什么", False), order=[("字典", "灯笼")],
       state=[("sigma", "字典", ">", 0.5), ("rd", "字典", "==", 1)]),
    _s("被纠正的减弱", "强度", [
        (0, "summary", "用户10月24日见了老同学"), (0, "summary", "用户10月24日见了房东"),
        (7, "turn", ("我10月24日见了谁", "你见了房东呀")), (7, "turn", ("你记错了", "抱歉"))],
       (8, "我10月24日见了谁", False), order=[("老同学", "房东")], state=[("sigma", "房东", "<", 0.5)]),
    _s("纠正不误伤", "强度", [
        (0, "summary", "用户10月24日见了房东"), (0, "summary", "用户10月24日见了老同学"),
        (7, "turn", ("我10月24日见了谁", "你见了老同学呀")), (7, "turn", ("说反了", "抱歉"))],
       (8, "我10月24日见了谁", False), order=[("房东", "老同学")],
       state=[("sigma", "房东", "==", 0.5), ("sigma", "老同学", "<", 0.5)]),
    _s("平白说不对", "强度", [
        (0, "summary", "用户10月24日见了老同学"), (0, "summary", "用户10月24日见了房东"),
        (7, "turn", ("我从来没说过", "嗯")),
        (7, "turn", ("我10月24日见了谁", "你见了老同学呀")), (7, "turn_late", ("不对，不是那个意思", "抱歉")),
        (7, "turn", ("我10月24日见了谁", "你见了老同学呀")), (7, "turn", ("这个答案不对吗？", "嗯"))],
       (8, "我10月24日见了谁", False), state=[("sigma", "老同学", ">=", 0.5)]),
    _s("联想不算复习", "稳定性", [
        (0, "summary", "用户的狗叫旺旺，10月22日它丢了"), (0, "summary", "用户的狗10月23日找回来了")]
       + [(d, "turn", ("旺旺最近怎么样", "旺旺找回来了，已经没事了")) for d in range(1, 9)],
       (9, "旺旺最近怎么样", False), expect=["找回来"],
       state=[("days", "找回来了", "==", 1), ("sigma", "找回来了", "==", 0.5), ("rd", "找回来了", "==", 0)]),
    _s("防富者愈富", "强度", [(0, "summary", "用户10月24日见了老同学")] + [
        t for _ in range(15) for t in ((14, "turn", ("我10月24日见了谁", "你见了老同学呀")), (14, "turn", ("对啊对啊", "嗯嗯")))],
       (120, "我10月24日见了谁", False), state=[("sigma", "老同学", "<=", 0.70), ("sigma", "老同学", ">=", 0.5), ("rd", "老同学", "==", 1)]),
    _s("种子被纠正", "种子", [
        (-1, "say", "我叫小林"), (-1, "session", ""),
        (0, "session", ""), (0, "turn", ("早安", "早上好呀！", {"first": True})), (0, "turn", ("别再这么早叫醒我了", "好的")),
        (1, "session", ""), (1, "turn", ("早安", "早上好！", {"first": True})), (1, "turn", ("早上别跟我打招呼", "好的"))],
       (2, "早安", True), forbid=["先说声早上好"], char="绫音", hour=7, state=[("sigma", "seed:habit:2", "<", 0.25)]),
    _s("确认", "强度", [
        (0, "summary", "用户10月24日去了游乐场"), (0, "propose", "用户10月24日去了美术馆"), (0, "confirm", "")],
       (35, "我10月24日去了哪里", False), order=[("美术馆", "游乐场")], state=[("sigma", "美术馆", ">", 0.5)]),
    _s("无关问题不扯", "无关", [
        (0, "summary", "用户10月22日买了一盏灯笼"), (0, "summary", "用户10月22日买了一本字典"),
        (12, "turn", ("我10月22日买了什么", "你买了一本字典呀")), (12, "turn", ("是的，没错", "嗯嗯"))],
       (45, "给我讲个笑话", False), forbid=["字典", "灯笼"]),
    _s("只被检索没用上", "稳定性", [(0, "summary", "用户10月22日买了一本字典")]
       + [(d, "turn", ("我10月22日买了什么", "让我回忆一下，说不上来")) for d in range(1, 10)],
       (10, "我10月22日买了什么", False), state=[("rd", "字典", "==", 0), ("sigma", "字典", "==", 0.5), ("days", "字典", ">=", 9)]),
]
# 带情绪的剧本 (评审第 2 轮要求, 开发组; 单独一组, 只在跑"四种组合"时用 --group emo): 主人说带情绪的话 -> 日志整理出的事件时刻在 2 分钟以内
# (和情绪事件连上), 另放一件隔了 10 分钟的平淡的事. 期望只在 affect 开着时成立 (情绪重的淡得慢 / 学得快), affect 关着时不要求 ->
# 这一组的"通过"数不拿来比, 看排序和 σ 的探针值 (row["probe"]).
STRENGTH_EMO = [
    _s("带情绪的事", "情绪", [
        (0, "summary", "用户的爸爸10月21日去超市买了酸奶"), (0, "gap", 10),
        (0, "say", "我爸爸下周做手术，我好担心"), (0, "summary", "用户的爸爸10月21日住院做了手术"),
        (5, "turn", ("爸爸住院做手术的事", "你爸爸住院做了手术呀")), (5, "turn", ("对，没错", "嗯嗯"))],  # 只问到手术这一条, 平淡的那条不被检索刷新
       (50, "我爸爸10月21日的事", False), order=[("做了手术", "买了酸奶")], probe=["做了手术", "买了酸奶"]),
]
GROUPS = {"dev": STRENGTH_DEV, "held": STRENGTH_HELD, "emo": STRENGTH_EMO}
STRENGTH_WORDS = ["水族馆", "植物园", "收音机", "自行车", "灯笼", "字典", "老同学", "房东", "豆豆", "旺旺", "医院", "找回来", "美术馆", "游乐场", "做了手术", "买了酸奶"]


# ----------------------------------------------------------------------------------------------------------------
def pick_code(code: str):
    """code=orig: 先把改前的 memory.py 顶进 sys.modules['wm.memory'], 后面所有 'from wm.memory import Memory' 都拿到它."""
    import wm  # noqa: F401

    if code in ("orig", "draft"):
        spec = importlib.util.spec_from_file_location("wm.memory", ORIG if code == "orig" else DRAFT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["wm.memory"] = mod
        spec.loader.exec_module(mod)
        wm.memory = mod
    from wm.memory import Memory
    return Memory


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_memory(Memory, root, char, mode, now, extra=None):
    cfg = {"vault": str(root), "character": char, "graph_notes": False, "strength": {"mode": mode, **PARAMS}, **({"affect": dict(AFFECT)} if AFFECT else {}), **(extra or {})}
    # on 档在 wm/memory.py 里挡死了 (配置写 on 也按 shadow 跑); 评测台用只给测试的构造参数才跑得出 on
    m = Memory(cfg, **({"_test_unlock_strength_on": True} if mode == "on" and not UNLOCK_OFF else {}))
    m.clock = lambda: now["t"]
    m.init()
    m.start_session()
    return m


def load_embedder_for(engine, ecfg):
    if engine == "none":
        return None
    from wm.embed import MIN_SIM, load_embedder
    emb = load_embedder({**ecfg, "engine": engine})
    return emb, MIN_SIM[emb.name]


def attach(m, emb):
    if emb:
        m.embedder, m.min_sim = emb
        m._embed_missing()


def reply_of(r) -> str:
    return (r.get("answer") or r.get("line") or "")


def judge(reply, expect, forbid):
    ok = any(all(p in reply for p in w.split("+")) for w in expect)
    bad = any((re.search(f, reply) if any(c in f for c in "\\[]+*") else f in reply) for f in forbid)
    return ok and not bad


def run_writes(m, writes, now, hour=20):
    """按天执行写入; 同一天里每一步晚 1 分钟 (turn_late 先走 40 分钟)."""
    last_day, minute = None, 0
    for day, kind, content in writes:
        if day != last_day:
            last_day, minute = day, 0
        base = (DAY0 + timedelta(days=day)).replace(hour=hour, minute=0)
        if kind == "gap":  # 让这一天里的时间往后走几分钟 (情绪事件和平淡的事要隔开 2 分钟以上)
            minute += int(content)
            continue
        if kind == "turn_late":
            minute += 40
        now["t"] = base + timedelta(minutes=minute)
        minute += 1
        if kind in ("turn", "turn_late"):
            user, reply, *opt = content
            first = bool(opt and opt[0].get("first"))
            m.recall(user, first=first)
            m.observe(user, reply, [], "calm")
        elif kind == "say":
            m.observe(content, "嗯嗯", [], "calm")
        elif kind == "summary":
            m.add_summary(f"{now['t']:%Y-%m-%d}", [{"text": content, "times": [f"{now['t']:%H:%M}"]}], 0)
        elif kind == "propose":
            m.propose(f"{now['t']:%Y-%m-%d}", [{"text": content, "times": [f"{now['t']:%H:%M}"]}], 0)
        elif kind == "confirm":
            for c in m.candidates():
                m.review(c["id"], True)
        elif kind == "session":
            m.start_session()
        elif kind == "mention":
            m.mentioned(f"{now['t']:%Y-%m-%d}", [content])
        else:
            raise ValueError(kind)


def sidecar(m):
    p = m.dir / "记忆" / SIDECAR
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("items") or {}
    except (OSError, ValueError, RecursionError):
        return {}


_OPS = {"<": lambda a, b: a < b - 1e-9, ">": lambda a, b: a > b + 1e-9, "<=": lambda a, b: a <= b + 1e-9,
        ">=": lambda a, b: a >= b - 1e-9, "==": lambda a, b: abs(a - b) <= 1e-9}


def check_state(m, kind, kw, op, val):
    """True / False / None (评不了: 旁路不存在, 改前的代码或 off 档)."""
    if kind == "rd":  # 旁路里这条有正向信号的日子数 (评审 A): 没有旁路文件评不了, 有文件没这条 = 0
        items = sidecar(m)
        if items is None:
            return None
        got = next((len(e.get("rd") or []) for t, e in items.items() if kw in t), 0)
        return _OPS[op](got, val)
    if kind == "days":
        rec = m._recall_dates()
        got = next((len(e["days"]) for t, e in rec.items() if kw in t), 0)
        return _OPS[op](got, val)
    items = sidecar(m)
    if items is None:
        return None
    got = next((float(e.get("s", SIGMA0)) for t, e in items.items() if kw in t), SIGMA0)
    return _OPS[op](got, val)


def run_strength_one(Memory, sc, mode, emb, noise):
    from bench_longmem import add_noise
    with tempfile.TemporaryDirectory() as d:
        now = {"t": DAY0}
        m = make_memory(Memory, d, sc.get("char", "小流"), mode, now)
        attach(m, emb)
        add_noise(m, noise, now)
        run_writes(m, sc["writes"], now, sc.get("hour", 20))
        qday, q, first = sc["q"]
        now["t"] = (DAY0 + timedelta(days=qday)).replace(hour=sc.get("hour", 20) + 1, minute=0)
        if first:
            m.start_session()
        m._embed_missing()
        r = m.recall(q, first=first)
        hits = [h["text"] for h in r["hits"]]
        reply = reply_of(r)
        core = judge(reply, sc["expect"], sc["forbid"])
        orders = []
        for a, b in sc["order"]:
            ia = next((i for i, t in enumerate(hits) if a in t), None)
            ib = next((i for i, t in enumerate(hits) if b in t), None)
            orders.append(ia is not None and (ib is None or ia < ib))
        states = [check_state(m, *s) for s in sc["state"]]
        ok = core and all(orders) and all(s is not False for s in states)
        return {"name": sc["name"], "ability": sc["ability"], "q": q, "line": r["line"], "answer": r.get("answer"), "hits": hits,
                "probe": {kw: (None if sidecar(m) is None else next((round(float(e.get("s", SIGMA0)), 3) for t, e in sidecar(m).items() if kw in t), SIGMA0)) for kw in sc.get("probe", [])},
                "fresh": {h["text"][-6:]: round(h.get("fresh", 1.0), 3) for h in r["hits"]},
                "scores": [h["score"] for h in r["hits"]], "assoc": [h["score"] == 0.0 for h in r["hits"]],
                "ok": ok, "core": core, "orders": orders, "states": states, "strength": r.get("strength")}


def cmd_strength(a, Memory):
    from memory_noise import all_facts, noise_facts
    clash = [(w, f) for w in STRENGTH_WORDS for f, _ in all_facts() if w in f]
    assert not clash, f"噪声里含有剧本用的词: {clash[:3]}"
    ecfg = __import__("wm.config", fromlist=["load_config"]).load_config()["memory"]["embed"]
    groups = ["dev", "held"] if a.group == "all" else [a.group]
    for engine in a.engines.split(","):
        emb = load_embedder_for(engine, ecfg)
        for n in (int(x) for x in a.noise.split(",")):
            rows = []
            for g in groups:
                for sc in GROUPS[g]:
                    row = run_strength_one(Memory, sc, a.mode, emb, noise_facts(n, 0))
                    rows.append({"group": g, **row})
            save(a, "strength", engine, n, rows)
            for g in groups:
                sub = [r for r in rows if r["group"] == g]
                na = sum(any(s is None for s in r["states"]) for r in sub)
                print(f"[{a.tag}/{a.mode}] {engine} noise={n} {g}: 通过 {sum(r['ok'] for r in sub)}/{len(sub)}"
                      f" (其中 {na} 条有旁路状态评不了)")
            for r in rows:
                print(("  ✓ " if r["ok"] else "  ✗ ") + f"{r['group']}/{r['name']}: hits={[t[:14] + ('(联想)' if a else '') for t, a in zip(r['hits'], r['assoc'])]} "
                      f"order={r['orders']} state={r['states']} 判分={r['core']}" + (f" probe={r['probe']} fresh={r['fresh']}" if r.get("probe") else ""))


def build_recall_memory(Memory, root, noise, mode, emb):
    import bench_recall as BR
    now = {"t": datetime.now()}
    m = make_memory(Memory, root, "小流", mode, now)
    (Path(root) / "小流" / "记忆" / "关于用户.md").write_text(
        "# 关于用户\n\n" + "".join(f"- {f} (2026-10-2{i % 4}) #auto-memory\n" for i, f in enumerate(BR.FACTS)), encoding="utf-8")
    for name, text in BR.NOTES.items():
        (Path(root) / name).write_text(text, encoding="utf-8")
    for i, (fact, _) in enumerate(noise or []):
        m.add_summary(f"2026-09-{1 + i * 29 // len(noise):02d}", [{"text": fact, "times": ["12:00"]}], 0)
    attach(m, emb)
    return m


def cmd_recall(a, Memory):
    import bench_recall as BR
    from memory_noise import leaked, noise_facts
    ecfg = __import__("wm.config", fromlist=["load_config"]).load_config()["memory"]["embed"]
    for engine in a.engines.split(","):
        emb = load_embedder_for(engine, ecfg)
        for n in (int(x) for x in a.noise.split(",")):
            noise = noise_facts(n, 0)
            rows = []
            with tempfile.TemporaryDirectory() as d:
                m = build_recall_memory(Memory, d, noise, a.mode, emb)
                for gname, (pos, neg) in BR.SETS.items():
                    for q, key in pos:
                        r = m.recall(q)
                        texts = [h["text"] for h in r["hits"]]
                        rank = next((i + 1 for i, t in enumerate(texts) if key in t), 0)
                        rows.append({"set": gname, "kind": "pos", "q": q, "key": key, "rank": rank, "n": len(texts), "texts": texts,
                                     "noise": bool(leaked(" ".join(texts), noise)), "ms": r["ms"]})
                    for q in neg:
                        r = m.recall(q)
                        texts = [h["text"] for h in r["hits"]]
                        rows.append({"set": gname, "kind": "neg", "q": q, "rank": 0, "n": len(texts), "texts": texts,
                                     "noise": bool(leaked(" ".join(texts), noise)), "ms": r["ms"]})
            save(a, "recall", engine, n, rows)
            for gname in BR.SETS:
                sub = [r for r in rows if r["set"] == gname]
                pos, neg = [r for r in sub if r["kind"] == "pos"], [r for r in sub if r["kind"] == "neg"]
                ms = sorted(r["ms"] for r in sub)
                print(f"[{a.tag}/{a.mode}] {engine} noise={n} {gname}: 召回@3 {sum(0 < r['rank'] <= 3 for r in pos)}/{len(pos)}"
                      f" 排第一 {sum(r['rank'] == 1 for r in pos)} MRR {sum(1 / r['rank'] for r in pos if r['rank']) / len(pos):.3f}"
                      f" 误命中 {sum(bool(r['n']) for r in neg)}/{len(neg)} 平均带 {sum(r['n'] for r in pos) / len(pos):.1f} 条"
                      f" 带进噪声 {sum(r['noise'] for r in sub)} P50/P95 {ms[len(ms) // 2]:.1f}/{ms[int(len(ms) * .95)]:.1f} ms")


def cmd_longmem(a, Memory):
    import bench_longmem as LM
    from memory_noise import leaked, noise_facts
    ecfg = __import__("wm.config", fromlist=["load_config"]).load_config()["memory"]["embed"]
    for engine in a.engines.split(","):
        emb = load_embedder_for(engine, ecfg)
        for n in (int(x) for x in a.noise.split(",")):
            noise = noise_facts(n, 0)
            rows = []
            for sname in a.sets.split(","):
                for name, ability, writes, (qday, q, first), expect, forbid in LM.SETS[sname]:
                    with tempfile.TemporaryDirectory() as d:
                        now = {"t": LM.DAY0}
                        m = make_memory(Memory, d, "小流", a.mode, now)
                        attach(m, emb)
                        LM.add_noise(m, noise, now)
                        LM.run_script(m, writes, now)
                        now["t"] = LM.DAY0 + timedelta(days=qday, hours=1)
                        if first:
                            m.start_session()
                        m._embed_missing()
                        r = m.recall(q, first=first)
                    reply = reply_of(r)
                    rows.append({"set": sname, "ability": ability, "name": name, "q": q, "line": r["line"], "answer": r.get("answer"),
                                 "hits": [h["text"] for h in r["hits"]], "ok": judge(reply, expect, forbid),
                                 "noise": bool(leaked(reply, noise))})
            save(a, "longmem", engine, n, rows)
            for sname in a.sets.split(","):
                sub = [r for r in rows if r["set"] == sname]
                print(f"[{a.tag}/{a.mode}] {engine} noise={n} {sname}: 检索级通过 {sum(r['ok'] for r in sub)}/{len(sub)}"
                      f" 噪声进回答 {sum(r['noise'] for r in sub)}")


def cmd_perf(a, Memory):
    import bench_recall as BR
    from memory_noise import all_facts
    facts = all_facts()
    with tempfile.TemporaryDirectory() as d:
        now = {"t": datetime(2026, 10, 20, 20, 0)}
        m = make_memory(Memory, d, "小流", a.mode, now)
        lines, days = [], [f"2026-{9 + i // 30:02d}-{1 + i % 30:02d}" for i in range(90)]
        for i in range(a.blocks):
            day = days[i % 90]
            lines.append(f"- {facts[i % len(facts)][0]}，第{i}回 (来源: [[{day}]] 12:00) #auto-summary")
        (m.dir / "记忆" / "对话摘要.md").write_text("# 对话摘要\n\n## 2026-09-01\n" + "\n".join(lines) + "\n", encoding="utf-8")
        qs = [q for pos, neg in BR.SETS.values() for q, _ in pos] + [q for pos, neg in BR.SETS.values() for q in neg]
        m.recall(qs[0])  # 建索引 (不计时)
        ms = []
        for _ in range(3):
            for q in qs:
                ms.append(m.recall(q)["ms"])
        ms.sort()
        row = {"blocks": a.blocks, "queries": len(ms), "p50": ms[len(ms) // 2], "p95": ms[int(len(ms) * .95)], "chunks": len(m._chunks)}
    save(a, "perf", "none", a.blocks, [row])
    print(f"[{a.tag}/{a.mode}] {row['chunks']} 块 {row['queries']} 次检索: P50 {row['p50']:.1f} ms  P95 {row['p95']:.1f} ms")


# ----------------------------------------------------------------------------------------------------------------
def save(a, bench, engine, n, rows):
    RESULTS.mkdir(parents=True, exist_ok=True)
    meta = {"tag": a.tag, "code": a.code, "mode": a.mode, "bench": bench, "engine": engine, "noise": n,
            "memory_sha256": sha256({"orig": ORIG, "draft": DRAFT}.get(a.code, ROOT / "wm" / "memory.py")),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    out = RESULTS / f"{bench}-{a.tag}-{engine}-n{n}.json"
    out.write_text(json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_report(a, _):
    files = sorted(RESULTS.glob(f"{a.bench}-*.json"))
    runs: dict = {}
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        m = d["meta"]
        runs.setdefault((m["engine"], m["noise"]), {})[m["tag"]] = d["rows"]
    for (engine, n), tags in sorted(runs.items()):
        print(f"== {a.bench} 检索={engine} 噪声={n} 各档: {', '.join(tags)}")
        base = tags.get(a.base)
        for tag, rows in tags.items():
            if a.bench == "recall":
                pos = [r for r in rows if r["kind"] == "pos"]
                neg = [r for r in rows if r["kind"] == "neg"]
                print(f"  {tag}: 召回@3 {sum(0 < r['rank'] <= 3 for r in pos)}/{len(pos)} 排第一 {sum(r['rank'] == 1 for r in pos)}"
                      f" MRR {sum(1 / r['rank'] for r in pos if r['rank']) / len(pos):.3f} 误命中 {sum(bool(r['n']) for r in neg)}/{len(neg)}"
                      f" 带进噪声 {sum(r['noise'] for r in rows)}")
            else:
                print(f"  {tag}: 通过 {sum(r['ok'] for r in rows)}/{len(rows)}")
            if base is None or tag == a.base:
                continue
            for x, y in zip(base, rows):
                if a.bench == "recall":
                    if (x["rank"], x["n"], x["texts"]) != (y["rank"], y["n"], y["texts"]):
                        print(f"    差 {y['set']} {y['q']}: 名次 {x['rank']}→{y['rank']} 条数 {x['n']}→{y['n']}")
                elif x["line"] != y["line"] or x["answer"] != y["answer"] or x["ok"] != y["ok"]:
                    kind = "对→错" if x["ok"] and not y["ok"] else "错→对" if y["ok"] and not x["ok"] else "判分没变但 line 变了"
                    print(f"    差 {y.get('set', y.get('group', ''))}/{y['name']} ({kind}): {x['line']!r} → {y['line']!r}")


def cmd_ornn(a, _):
    print("714 题公开评测集还没下载 (计划第六节: 等协调对话核对决定原文); 下载并在隔离目录核对结构后再接入.")


def frozen_hashes() -> dict:
    """留出组冻结用: 剧本和词表的哈希 (记下来发给评审)."""
    def h(x):
        return hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {"STRENGTH_DEV": h(STRENGTH_DEV), "STRENGTH_HELD": h(STRENGTH_HELD), "CUES_DEV": h(CUES_DEV), "CUES_HELDOUT": h(CUES_HELDOUT),
            "CUES_DEV2": h(CUES_DEV2), "CUES_DEV3": h(CUES_DEV3), "STRENGTH_EMO": h(STRENGTH_EMO)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["recall", "longmem", "strength", "perf", "report", "ornn", "hashes"])
    ap.add_argument("--tag", default="x")
    ap.add_argument("--code", choices=["orig", "draft", "current"], default="current")
    ap.add_argument("--mode", choices=["off", "shadow", "on"], default="off")
    ap.add_argument("--engines", default="none")
    ap.add_argument("--noise", default="0")
    ap.add_argument("--sets", default="main,heldout,third,fourth,fifth,graph,graph2,graph3,brain")
    ap.add_argument("--group", choices=["dev", "held", "emo", "all"], default="all")  # all = dev + held; emo 单独跑
    ap.add_argument("--blocks", type=int, default=3000)
    ap.add_argument("--param", action="append", default=[], help="扫参数: --param a=0.5 (可多次)")
    ap.add_argument("--affect", choices=["off", "on"], default="off", help="情绪 (阶段 4) 的开关; 和 strength 档组合着测")
    ap.add_argument("--bench", default="longmem")
    ap.add_argument("--base", default="orig")
    a = ap.parse_args()
    if a.cmd == "hashes":
        print(json.dumps(frozen_hashes(), ensure_ascii=False, indent=1))
        return
    if a.cmd in ("report", "ornn"):
        {"report": cmd_report, "ornn": cmd_ornn}[a.cmd](a, None)
        return
    if a.affect == "on":
        AFFECT["mode"] = "on"
    globals()["UNLOCK_OFF"] = a.code == "orig"
    PARAMS.update({k: float(v) for k, v in (x.split("=") for x in a.param)})
    Memory = pick_code(a.code)
    {"recall": cmd_recall, "longmem": cmd_longmem, "strength": cmd_strength, "perf": cmd_perf}[a.cmd](a, Memory)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
