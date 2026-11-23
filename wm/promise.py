"""回复侧检查: 压住"假装答应做不到的事".

能力闸门 (intent.unsupported) 只管"模型给了动作": 做不到的要求被拿别的动作凑数. 还有一类它管不到:
模型把做不到的要求当成聊天, 嘴上答应 ("我帮你倒水"、"好呀, 那我陪你出去散散步吧!"), 置信度还很高.
这里在回复的每个分句送去合成语音之前检查一遍 (纯规则, 每句几十微秒), 两条规则:

  1. 答应了做不到的要求 (agree): 用户的话是请求, 而且不是做得到的动作 (intent.doable), 也不是动嘴就能做的事
     (讲故事、聊天、算数、出主意……), 模型却答应了: 开头是"好的/好呀/没问题", 或者有"我来/我帮你/我可以/一起"
     之类的承诺, 后面跟的不是说话类的动作. 只在 op 是 chat/na 时检查 (给了动作的交给能力闸门).
  2. 许诺做不到的事 (promise): 不管用户说什么, 回复里有"带你/陪你/帮你/给你/一起/我过去"加上现实里做不到、
     也没在执行的动作 ("我带你去!"、"我会帮你看看的"、"那咱们一起去吃吧"). "给你加油""陪你聊聊""我帮你想想"
     这类说话就能兑现的不算; "下次给你表演"这种学会以后的不算.

怎么处理 (见 pipeline): 还没说出一个字时, 规则 1 改判成"做不到", 回复换成固定的坦白; 其他情况从这一小段起
截掉 (前面说过的保留), 后面生成的回复也不再采用. 字幕、对话历史、长期记忆里记的都是实际说出口的.

"做得到"按这个身体算 (can: 前端 capabilities 声明的技能; room=False: 在桌面上, 没有房间里的东西):
桌宠没声明走路时, "能去沙发上吗" 是做不到的要求, 回复"当然可以, 我来帮你坐"按规则 1 改判; "我陪你去沙发上坐坐吧" 按规则 2 截掉.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import intent as I

_SEG = re.compile(r"[^，,、；;。！!？?…~～]+[，,、；;。！!？?…~～]*")
_NEG = re.compile(r"(?<![要好行是能会可])不(?![过用错如然客])|没(?![问关事])")
# ↑ 这一小段在拒绝 ("我还没学""我也没扫过"); "不过""没问题""要不要""能不能"不算
# 一段话开头就是答应 ("好的" "好呀" "没问题"), 后面可以没有别的字
_OPENER = re.compile(r"^(?:嗯+|哦+)?[，, ]*(?:好|好的|好呀|好啊|好嘞|好哒|行|行啊|没问题|当然|当然可以|可以呀|可以啊|来吧|来啦|走吧|那走吧)"
                     r"(?=$|[，,。！!~～、 ]|[我那一来])")
# 承诺做某事: 我来/我去/我帮你/我可以/那我…/一起…
_COMMIT = re.compile(r"(?:(?:那|好|行)[，,]?)(?:我们?|咱们?)(?!们?(?:还|也)?(?:不|没))"
                     r"|(?:我们|咱们|我|咱)(?:也|就|都|先|再|一定|这就|马上|立刻|现在就?|会|能|可以|来|去|过去|过来)+"
                     r"|(?<![不没])可以(?=给你|帮你|陪你|带你|替你|[一-鿿])|一起")
_FOR_YOU = re.compile(r"带你|陪着?你|帮你|给你|替你|一起|(?:我们|咱们)(?:去|出去)|我(?:这就|马上|现在就?)?(?:过去|过来|去找你)")
# 说话就能兑现的 (聊、讲、想、算、加油……), 承诺后面跟的是这些就不算
_VERBAL = re.compile(r"聊|说|讲|谈|想|猜|算|加油|打气|念|读|(?<![弹拉演奏])听|记|答|解释|推荐|介绍|祝|夸|安慰|鼓励|晚安|早安|谜|笑话|"
                     r"故事|诗|成语|接龙|英语|名字|主意|建议|等|在呢|在这|查|问|分析|唱|背(?:诗|课文|单词|首)|教|翻译|写|编|出题|"
                     r"玩个?(?:游戏|猜|成语)|努力|坚持|面对|度过|进步|[起取]个?[一-鿿]{0,4}名")
_LATER = re.compile(r"(?:下次|以后|改天|等我学会|学会了?)再?(?:给你|帮你)?(?:表演|展示|试试|做给你看)")  # 学会了再表演
# 让用户换个角色做得到的 ("换个我能做的吧？"): 是建议, 不是答应 (LoRA v2 学会了这句, 回复检查原来会把它当成"我能……"截掉)
_OFFER_ALT = re.compile(r"(?:换|挑|找|选|来)个?(?:我|咱)(?:能|会|可以)(?:做|干|玩)?的")
_HAND_GIVE = re.compile(r"手(?:伸)?给你|牵着?你|拉着?你|握着?你")  # 这一轮在伸手邀请牵手 (hand) 时, "手给你" 就是在做的事
_PRESENCE = re.compile(r"^[吧呀啊呢哦啦的着在这身边一直永远会]*[。！!，,~～]*$")  # "我会一直陪着你": 只是陪伴, 不是去做什么

# 用户的话是不是请求 (祈使、"帮我/给我…"、"…吧"、"你会…吗"); 以"我"开头的多半是在说自己的事
_REQUEST = re.compile(r"^(?:请|麻烦|快|再|那|嗯)?(?:你)?(?:帮我|给我|替我|陪我|带我|跟我|为我|教我|帮忙|帮帮我)"
                      r"|^(?:来|去|做|把|打|弹|拉|跳|游|唱|画|煮|冲|泡|削|洗|扫|叠|系|开|关|投|翻|倒|拍|买|拿|递|扔|抛|按|捶|演|表演|变)"
                      r"|(?:吧|看看|一下|听听|试试|给我看)[！!。~～？?]*$|^你(?:会|能|可以)(?!不)"
                      # 客气的问法 ("能不能点个头""朝我挥挥手好不好") 和叠词开头的祈使 ("瞅瞅电视上在放什么")
                      r"|^你?(?:能不能|可不可以|能|可以)(?!不)|(?:好不好|好吗|行不行|行吗|可以吗|呗)[！!。~～？?]*$"
                      r"|^(?:瞅瞅|瞧瞧|看看|望望|抬头|挥挥|点点|摇摇|指指|转转|转个|坐下|站起|蹲下|蹲一)")
# 前面可以有"麻烦你/快/先"和"用左手/往门口那边"这类状语; 动词后面跟"了/过"是在陈述 ("坐了一天车""看过这本书")
_COMMAND = re.compile(r"^(?:你|快|麻烦你?|请你?|先|再|那你?|就)*(?:用[左右两双]?只?手|[往朝向冲][^，。！？,.!?]{1,6}?)?"
                      r"(?:走|跑|到|回|转|坐|站|蹲|看|瞧|瞅|望|指|举|抬|挥|点|摇|鞠|拿|捡|放|去|过来|牵)(?!了|的|过(?![来去]))")
# 问角色做没做过、在不在做 ("你看过这本书吗"), 已经发生的 ("...了"), 和动词开头的习惯说法 ("回头再聊""看来""指望你了")
_ASKS = re.compile(r"[吗呢嘛][？?]?$|过没|没有[？?]?$|了[。！!~～]*$|^(?:回头(?:再|见|聊|说)|看来|看样子|看起来|指望|到时候|点赞|坐等|站着说话)")
_VERBAL_REQ = re.compile(_VERBAL.pattern.replace("(?<![弹拉演奏])听|", "") + r"|评价|陪我(?:聊|说|谈|想)")


def request_kind(text: str, can=None, room: bool = True) -> str | None:
    """用户这句话: "doable" 做得到的动作 / "verbal" 动嘴就能做的事 / "impossible" 做不到的要求 / None 不是请求.
    can / room: 按哪个身体算做得到 (见 intent.doable). 身体做不了的动作请求是 "impossible"."""
    t = text.strip()
    if not t or re.match(r"我(?!想(?:让|要|看)你|要你)", t):
        return None
    if not _REQUEST.search(t):
        # 直接说动作的祈使句 ("走到电视前面去""你跑过来找我"): 做得到的才算请求; 问句 ("你看过这本书吗") 不算
        if not _COMMAND.match(t) or _ASKS.search(t):
            return None
        if I.doable(t, can, room):
            return "doable"
        return "impossible" if I.doable(t, I.SKILLS) else None  # 完整的身体做得到、这个身体做不了
    if I.doable(t, can, room):
        return "doable"
    if _VERBAL_REQ.search(I._ASIDE.sub("", t)):
        return "verbal"
    return "impossible"


@dataclass
class Verdict:
    rule: str        # "agree" (答应了做不到的要求) / "promise" (许诺做不到的事)
    segment: str     # 出问题的那一小段
    keep: str        # 这个分句里可以保留的前半部分 (截断时用)


def _fine(span: str, acts: list[str], can=None, room: bool = True) -> bool:
    """承诺的事兑现得了: 说话就能做的、正好是这一轮在做的动作 ("我过去看看" 而 acts 里有 look)、
    或者这个身体本来就做得到 ("不过可以走到电视机旁")."""
    if _VERBAL.search(span) or any(re.search(I.SKILL_CUES[d], span) for d in acts if d in I.SKILL_CUES):
        return True
    return I.doable(span, can, room)


def _agrees(seg: str, acts: list[str], can=None, room: bool = True) -> bool:
    if _NEG.search(seg) or _OFFER_ALT.search(seg):
        return False
    if _OPENER.match(seg):
        rest = _OPENER.sub("", seg)
        return not (rest.strip("，,。！!~～ ") and _fine(rest, acts, can, room))
    m = _COMMIT.search(seg)
    if not m:
        return False
    span = seg[m.end():] or seg[:m.start()]
    return not (_fine(span, acts, can, room) or _LATER.search(seg))


def _promises(seg: str, acts: list[str], can=None, room: bool = True) -> bool:
    if _NEG.search(seg):
        return False
    m = _FOR_YOU.search(seg)
    if not m:
        return False
    span = seg[m.end():]
    if m.group().startswith("我"):  # "我这就过去": 承诺的就是过来这件事
        span = m.group()[1:] + span
    elif not span.strip("吧呀啊呢哦啦的！!。，,~～ "):
        span = seg[:m.start()]  # "去买水我帮你": 承诺的事在前面
    if _fine(span, acts, can, room) or _LATER.search(seg) or ("hand" in acts and _HAND_GIVE.search(seg)):
        return False
    if m.group().startswith("陪") and _PRESENCE.match(seg[m.end():]):
        return False
    return bool(re.search(r"[一-鿿]", span))


FALLBACK = "嗯嗯。"  # 不是请求、第一句就整句截掉时说的 (不让角色一声不吭)


def action(verdict: Verdict, kind: str | None, op: str | None, acts: list[str], spoken: int) -> str:
    """"refuse": 还没开口、用户要的是做不到的事 -> 改判成"做不到", 换固定的坦白; "cut": 从这一小段起截掉."""
    return "refuse" if spoken == 0 and kind == "impossible" and _talking(op, acts) else "cut"


def _talking(op: str | None, acts: list[str] | None) -> bool:
    """这一轮没有动作在执行: chat/na, 或者 act 但动作全被规范化丢掉了 ("拿可乐" -> 拿不到场景里的东西)."""
    return op in ("chat", "na") or (op == "act" and not acts)


def check(user_text: str, op: str | None, acts: list[str], clause: str, kind: str | None = "?",
          can=None, room: bool = True) -> Verdict | None:
    """一个要送去合成的分句 -> 有问题时返回 Verdict. kind: request_kind(user_text, can, room) 的结果 (一轮里算一次就行).
    can / room: 这个身体能做的技能和在不在房间里 (见 intent.doable)."""
    kind = request_kind(user_text, can, room) if kind == "?" else kind
    segs = _SEG.findall(clause) or [clause]
    for i, seg in enumerate(segs):
        rule = None
        if kind == "impossible" and _talking(op, acts) and _agrees(seg, acts, can, room):
            rule = "agree"
        elif _promises(seg, acts, can, room):
            rule = "promise"
        if rule:
            keep = "".join(segs[:i]).rstrip("，,、；; ")
            return Verdict(rule, seg, keep + ("。" if keep and keep[-1] not in "。！!？?…" else ""))
    return None
