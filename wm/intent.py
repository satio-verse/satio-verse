"""意图协议: 紧凑 JSON Schema、系统提示词、few-shot 示例和结果规范化.

字段顺序固定为 op -> acts -> emo -> reply. llama.cpp 生成语法时按 required 的声明顺序排列,
所以动作字段总是先于回复文本闭合, 流式解析到 acts 结束就能开始动, 不用等回复写完.
"""
from __future__ import annotations

import json
import re

from . import scene as scene_graph

SKILLS: dict[str, str] = {
    "wave": "挥手",
    "raise": "举手",
    "point": "指向前方",
    "nod": "点头",
    "shake": "摇头",
    "shrug": "摊手耸肩",
    "think": "思考",
    "bow": "鞠躬",
    "leg": "抬腿",
    "go": "走到",
    "look": "看向",
    "turn": "转身",
    "sit": "坐下",
    "stand": "站起来",
    "pick": "拿起",
    "put": "放下",
    "squat": "蹲下",
    "hand": "伸手牵手",
    # 表情动作 (2026-10-26, 前端行动层的 _emote: 手臂 IK 到脸、胸前, 加手形)
    "heart": "比心",
    "giggle": "捂嘴笑",
    "chin": "托腮",
    "clap": "拍手",
    "peace": "比耶",
    "cheeks": "捧脸",
}
# 只在前端的 capabilities 里声明了才算做得到的技能 (新加的, 老前端不认识): 没声明时和身体做不了的技能一样处理.
# hand: 朝用户伸出手、掌心朝上半张, 邀请牵手 (网页版挂了行动层才声明; 桌宠没有)
EMOTES = {"heart", "giggle", "chin", "clap", "peace", "cheeks"}  # 表情动作: 前端声明了才做 (老前端、桌宠没接的时候不认识)
OPT_IN = {"squat", "hand"} | EMOTES
DEFAULT_CAN = frozenset(SKILLS) - OPT_IN  # 前端没声明 capabilities 时这个身体能做的技能
ARM_SKILLS = {"wave", "raise", "point", "leg", "hand", "giggle", "chin", "peace"}  # 需要 side 的肢体动作 (leg 的 side 指左右腿)
BOTH_HANDS = {"shrug", "heart", "clap", "cheeks"}  # 总是两只手的 (side 固定 B)
SPATIAL_SKILLS = {"go", "look", "turn", "sit", "stand", "pick", "put"}  # 开放空间行动层执行; 目标来自场景图
TARGET_SKILLS = {"go", "look", "turn", "point", "sit", "pick", "put"}  # 可以带 to (场景物体编号)
_SCENE = scene_graph.load_scene()
# 场景物体编号 -> 中文名. 进 Schema 枚举, 约束解码只能选场景里有的东西.
TARGETS: dict[str, str] = scene_graph.targets(_SCENE)
# 能坐的 / 能拿起的 / 能放东西的物体
CAPS: dict[str, list[str]] = scene_graph.capabilities(_SCENE)
HOME = "home"  # 只给 put 用: 放回拿起它的地方
ROOM, DESKTOP = "room", "desktop"  # 角色在哪: 房间里 (默认) / 用户的电脑桌面上 (桌宠, 没有家具和东西)
STATE_TAG = "[当前]"  # 本轮用户消息前的角色状态行 (由前端上报的结构化状态在服务端拼成)
MEMORY_TAG = "[记忆]"  # 从长期记忆 (Obsidian 库) 里检索到的、和这句话相关的几条
OPS = ["act", "chat", "stop", "na"]


# 能力闸门: 每个技能在用户原话里的常见说法. 原话里一个技能的说法都没有, 模型却给了动作而且自己没把握,
# 多半是"做不到的要求被拿别的动作凑数" (翻跟头 -> 摇头, 开台灯 -> 转身), 改成 na.
# 原话里提到了做得到的动作 ("看看门口" 却给了 go) 是选错了技能, 不改判成做不到: 流水线按原话换掉 (off_cue / fill_acts),
# 再照旧走升级复核和不确定标记.
SKILL_CUES = {
    "wave": r"挥|招手|招呼|拜拜|再见|嗨", "raise": r"举|抬手|抬起手|手抬|抬高|高举", "point": r"指",
    "nod": r"点.{0,2}头", "shake": r"摇.{0,2}头", "shrug": r"耸|摊手|无奈", "think": r"想想|想一下|思考|琢磨|考虑",
    "bow": r"躬|谢幕", "leg": r"腿|抬脚", "go": r"走|去|过来|过去|到|跑|回|靠近|来(我|这|那)", "look": r"看|瞧|望|瞅|盯",
    # 站: "B 站""网站""车站""站点""站长"里的站不是要她站起来 ("帮我刷 B 站" 被当成要站起来, 回了"站起来还做不到")
    "turn": r"转|回头|背过", "sit": r"坐",
    "stand": r"(?<![A-Za-z网车驿基空分本])(?<![A-Za-z] )站(?![点长内台])|起身|别坐|(?<![飞跳笑唱哭玩])起来",
    "pick": r"拿|捡|拾|取|抓|拎|递", "put": r"放|搁|摆", "squat": r"蹲",
    "hand": r"牵|拉.{0,3}手|握.{0,3}手|手给我|伸.{0,2}手",
    "heart": r"比.{0,2}心|笔芯|爱心", "giggle": r"捂.{0,2}嘴|偷笑", "chin": r"托.{0,2}腮|[托撑].{0,2}下巴",
    "clap": r"拍.{0,2}手|鼓.{0,2}掌", "peace": r"比.{0,2}耶|剪刀手|比.{0,2}[vV]", "cheeks": r"捧.{0,2}脸|托.{0,2}脸",
}
_ASIDE = re.compile(r"[给让]我[看瞧](看|一下)?|想[要让看]")  # "飞起来给我看看""我想看你跳舞" 里的看/想不是要角色做的动作
GATE_CONF = 0.7
REFUSALS = ["这个我还不会呢。", "这个我还没学会，下次再试试。", "这个我还做不到，换个别的吧？"]


def unsupported(text: str, op: str, acts: list, conf: float | None, threshold: float = GATE_CONF,
                can=None, room: bool = True) -> bool:
    """op=act, 原话里说的不是做得到的动作 (见 doable), 且整体置信度低于阈值: 判定为做不到的要求."""
    if op != "act" or not acts or conf is None or conf >= threshold:
        return False
    return not doable(text, can, room)


# 走、看、拿要有对得上的目标才算做得到: "下楼帮我拿个快递"、"陪我去看电影"、"帮我收拾一下书包" 里有"拿/去/看/拾",
# 但快递、电影、书包都不在房间里
_GO_USER = re.compile(r"过来|到我|来我|我这|靠近我|找我|我身边|我旁边|我面前|这边来|来这")
_LOOK_USER = re.compile(r"[看瞧望瞅盯](着|一下|看)?我")
_PRONOUN = re.compile(r"它|这个|那个|东西")


def _tokens(text: str) -> set[str]:
    import jieba  # 记忆层也用它; 服务启动时预热 (warmup), 不让第一轮多等 1 s

    return set(jieba.lcut(text))


_MEASURE = "一二两三几这那本个只颗些盆盏张把"
_PLACE = "边旁前后上下里外口"


def mentions(text: str, target: str, tokens: set[str] | None = None) -> bool:
    """原话里提到了这个场景目标: 多字的名字按子串; 单字的名字 (书/球/门/灯/花) 要是一个完整的词,
    前面可以带数量词、后面可以带方位 (jieba 会切出"本书""那盆花""窗边"), "书包"、"灯光" 里的书、灯不算."""
    for name in TARGETS[target].split("/"):
        if len(name) > 1 and name in text:
            return True
        if len(name) == 1 and name in text:
            tokens = tokens if tokens is not None else _tokens(text)
            if any(re.fullmatch(f"[{_MEASURE}]*{name}[{_PLACE}]?", t) for t in tokens):
                return True
    return False


def doable(text: str, can=None, room: bool = True) -> bool:
    """原话里说的是做得到的动作: 有某个技能的说法, 而且走/看/拿有对得上的目标 (房间里的东西, 或者是走到/看着用户).
    can: 这个身体能做的技能 (前端的 capabilities; None 为 DEFAULT_CAN), 做不了的技能的说法不算;
    room=False (在桌面上): 没有房间里的东西, 走、看只能是冲着用户, 拿不了东西."""
    can = DEFAULT_CAN if can is None else can
    rest = _ASIDE.sub("", text)
    tokens = None
    for skill, cue in SKILL_CUES.items():
        if skill not in can or not re.search(cue, rest):
            continue
        if skill not in ("go", "look", "pick"):
            return True
        if skill == "go" and _GO_USER.search(rest) or skill == "look" and _LOOK_USER.search(rest):
            return True
        if not room:
            continue
        if skill == "pick" and _PRONOUN.search(rest):
            return True
        tokens = tokens if tokens is not None else _tokens(rest)
        keys = CAPS["item"] if skill == "pick" else [k for k in TARGETS if k != "user"]
        if any(mentions(rest, k, tokens) for k in keys):
            return True
    return False


def warmup() -> None:
    _tokens("预热")


def field_enums(place: str = ROOM) -> dict[str, list[str]]:
    """各决策字段的合法取值, 算置信度时只在这些值之间归一化."""
    return {"op": OPS, "do": list(SKILLS), "to": _to_enum(place), "emo": list(EMOTIONS)}


def _to_enum(place: str) -> list[str]:
    return ["user"] if place == DESKTOP else [*TARGETS, HOME]  # 桌面上没有房间里的东西, 只能冲着用户

# 情绪标签 -> VAD (愉悦度, 唤醒度, 支配度), 取值 [-1, 1]. 前端用它驱动表情和身体能量.
EMOTIONS: dict[str, tuple[float, float, float]] = {
    "neutral": (0.0, 0.0, 0.0),
    "happy": (0.7, 0.45, 0.2),
    "excited": (0.8, 0.85, 0.4),
    "calm": (0.3, -0.45, 0.1),
    "sad": (-0.6, -0.4, -0.4),
    "shy": (0.3, 0.2, -0.6),
    "angry": (-0.6, 0.7, 0.6),
    "surprised": (0.2, 0.8, -0.1),
}


def build_intent_schema(place: str = ROOM) -> dict:
    """只要 op 和 acts 的精简格式: 升级复核时用, 输出短, 更快."""
    s = build_schema(place)
    return {**s, "properties": {k: s["properties"][k] for k in ("op", "acts")}, "required": ["op", "acts"]}


def build_schema(place: str = ROOM) -> dict:
    act = {
        "type": "object",
        "properties": {
            "do": {"type": "string", "enum": list(SKILLS)},
            "side": {"type": "string", "enum": ["L", "R", "B"]},
            "to": {"type": "string", "enum": _to_enum(place)},
            "amp": {"type": "number"},
            "spd": {"type": "number"},
            "at": {"type": "number"},
        },
        "required": ["do"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": OPS},
            "acts": {"type": "array", "items": act, "maxItems": 3},
            "emo": {"type": "string", "enum": list(EMOTIONS)},
            "reply": {"type": "string"},
        },
        "required": ["op", "acts", "emo", "reply"],
        "additionalProperties": False,
    }


SYSTEM_PROMPT = (
    "你是虚拟角色「绫音」的意图与回复模型. 你在一个房间里, 能走动. 每次只输出一个 JSON, 字段顺序固定: op, acts, emo, reply.\n"
    "op: act=用户要求做动作; chat=普通聊天; stop=停止/放下/别动/站住; "
    "na=要求了做不到的事: do 里没有的动作 (如后空翻、俯卧撑), 或要动手操作东西 (如开门、做饭); acts 写 [], 不要用别的动作凑数, "
    "reply 坦白说还不会. do 里有的动作 (点头、挥手、摇头等) 都是 act.\n"
    "acts: 要执行的动作, 最多3个, 没有就 []. do 取值: wave 挥手, raise 举手, point 指向, nod 点头, shake 摇头, "
    "shrug 摊手耸肩, think 思考, bow 鞠躬, leg 抬腿, hand 朝用户伸出手 (邀请牵手、握手), "
    "heart 双手比心, giggle 捂嘴笑, chin 托腮, clap 拍手, peace 比耶 (剪刀手), cheeks 双手捧脸, "
    "go 走到, look 看向, turn 转身, sit 坐下, stand 站起来, pick 拿起/捡起, put 放下手里的东西. "
    "side: L 左, R 右, B 双手; 左右指角色自己的左右, 没说就省略(默认右), 头部动作不写 side. "
    "to: 房间里的目标, 只能取 " + ", ".join(f"{k} {v}" for k, v in TARGETS.items()) + ". "
    "go 和 look 必须写 to; 让你看/瞧/望房间里某个东西是 act + look, 不是聊天; 让你跑过去是 go 加 spd 1.8. "
    "指某个东西时 point 也写 to; turn 有目标写 to, 否则 side: L 向左转, R 向右转, B 转过身. "
    "sit 写 to 坐的地方(" + ", ".join(CAPS["seat"]) + "); pick 写 to 要拿的东西(" + ", ".join(CAPS["item"]) + "), 会自己走过去拿; "
    "put 的 to 是放到哪里(" + ", ".join(CAPS["place"]) + "), 没说放哪就不写 to, 放在地上. 手放下/别动是 stop, 不是 put. "
    "放回去/放回原处是 put 加 to home. "
    "用户的话后面如果有" + STATE_TAG + ", 那是你此刻的真实状态 (在哪, 坐着还是站着, 手里拿着什么, 东西在哪): "
    "问到这些要按这一轮的" + STATE_TAG + "照实回答, 示例里的状态是别的情况, 不要照抄; 手里没东西不要 put; 坐着时让你起来是 stand. "
    "用户的话后面如果有" + MEMORY_TAG + ", 那是你记得的关于用户的事和你们的关系 (不一定和这句话有关): "
    "用得上就自然地提, 用不上就忽略; 没记着的事不要编. "
    "回之前先把用户这一轮说的话看清楚 (可能接着前面几句), 弄明白他在说什么、要你做什么, 再答; 别急着接话茬, "
    "没看懂就问一句, 别为了接上话编一个意思. "
    "走到某处之后才做的动作直接排在 go 后面, 会等走到了再做, 不用写 at. "
    "只写和默认不同的参数: amp 幅度0到1(默认1; raise 时 0.5 齐肩, 0.8 齐头, 1 过头顶), spd 速度0.5到2(默认1; go 时 1.6 以上是跑), "
    "at 相对开始的延迟秒数(有先后顺序时才写).\n"
    "聊天时可以配一个自然的小动作(打招呼配 wave, 同意配 nod, 被夸害羞配 cheeks, 被逗笑配 giggle, 替人高兴配 clap), 不要乱加.\n"
    "emo: 回复时的情绪.\n"
    "reply: 口语化中文, 简短, 不超过40字; 执行动作时一句话回应即可. 不编造现实经历."
)


def _a(do: str, **params) -> dict:
    return {"do": do, **params}


def _out(op: str, acts: list, emo: str, reply: str) -> dict:
    return {"op": op, "acts": acts, "emo": emo, "reply": reply}


# 表情动作的示例 (房间和桌宠都用): 要求做的是 act; 聊天里自然带出来的是 chat
EMOTE_EXAMPLES: list[tuple[str, dict]] = [
    ("给我比个心", _out("act", [_a("heart")], "happy", "给你比心！")),
    ("来个剪刀手", _out("act", [_a("peace")], "happy", "耶！")),
    ("托着腮想一想", _out("act", [_a("chin")], "calm", "嗯……让我想想。")),
    ("你好可爱呀", _out("chat", [_a("cheeks")], "shy", "被你这么一说，有点不好意思啦。")),
    ("哈哈哈你也太逗了", _out("chat", [_a("giggle")], "happy", "嘿嘿，我也觉得好好笑。")),
    ("我考试通过啦！", _out("chat", [_a("clap")], "excited", "太棒了！给你鼓掌！")),
]

EXAMPLES: list[tuple[str, dict]] = [
    ("你好呀", _out("chat", [_a("wave")], "happy", "你好呀！今天想聊点什么？")),
    ("举起右手", _out("act", [_a("raise", side="R")], "neutral", "好的，举起来啦。")),
    ("左手抬到和头一样高", _out("act", [_a("raise", side="L", amp=0.8)], "neutral", "左手到头这么高了。")),
    ("两只手一起举过头顶", _out("act", [_a("raise", side="B")], "excited", "耶！举高高！")),
    ("先挥挥左手，再点点头", _out("act", [_a("wave", side="L"), _a("nod", at=2.5)], "happy", "先挥手，再点头。")),
    ("慢慢摇摇头", _out("act", [_a("shake", spd=0.6)], "calm", "嗯，慢慢摇。")),
    ("给我表演个后空翻", _out("na", [], "shy", "后空翻我还没学会，下次给你表演。")),
    # 做不到的要求: 不拿别的动作凑数, 也不假装答应
    ("做二十个俯卧撑", _out("na", [], "shy", "俯卧撑我还做不了，给你加油倒是可以！")),
    ("帮我把门打开", _out("na", [], "neutral", "开门我还做不到，不过可以走到门口陪你。")),
    ("来，抱一个", _out("na", [], "shy", "我还不会拥抱，给你挥挥手好不好？")),
    ("给我做顿饭吧", _out("na", [], "calm", "做饭我还帮不上忙，陪你聊聊天吧。")),
    ("放下吧，别动了", _out("stop", [], "calm", "好，我放下了。")),
    ("我今天考试没考好", _out("chat", [], "sad", "抱抱你，一次考试说明不了什么，想聊聊吗？")),
    ("你觉得我该不该去旅行", _out("chat", [_a("think")], "calm", "想去就去吧，趁有时间出去走走挺好的。")),
    ("太棒了我拿到offer了！", _out("chat", [_a("raise", side="B")], "excited", "太厉害了！恭喜恭喜！")),
    ("你知道明天会不会下雨吗", _out("chat", [_a("shrug")], "neutral", "我看不到天气预报，出门前查一下比较稳。")),
    ("向前指一下", _out("act", [_a("point")], "neutral", "那边。")),
    ("给大家鞠个躬", _out("act", [_a("bow")], "happy", "谢谢大家！")),
    ("抬起左腿", _out("act", [_a("leg", side="L")], "neutral", "好，抬左腿。")),
    *EMOTE_EXAMPLES,
    ("我们牵手吧", _out("act", [_a("hand")], "happy", "好呀，手给你。")),
    ("换左手牵", _out("act", [_a("hand", side="L")], "shy", "嗯，换左手。")),
    ("走到沙发那边", _out("act", [_a("go", to="sofa")], "neutral", "好，我过去。")),
    ("过来一下", _out("act", [_a("go", to="user")], "happy", "来啦！")),
    ("看看窗外", _out("act", [_a("look", to="window")], "calm", "外面天气不错。")),
    ("瞧瞧那盆绿植", _out("act", [_a("look", to="plant")], "happy", "长得挺精神的。")),
    ("跑到窗户那里", _out("act", [_a("go", to="window", spd=1.8)], "excited", "我跑过去啦！")),
    ("去门口然后挥挥手", _out("act", [_a("go", to="door"), _a("wave")], "happy", "走到门口，再挥挥手。")),
    ("转过身去", _out("act", [_a("turn", side="B")], "neutral", "好，转过去了。")),
    ("指一下台灯", _out("act", [_a("point", to="lamp")], "neutral", "台灯在那儿。")),
    ("坐到沙发上休息一下", _out("act", [_a("sit", to="sofa")], "calm", "好，我坐一会儿。")),
    ("帮我把球捡起来", _out("act", [_a("pick", to="ball")], "happy", "捡起来啦。")),
    ("把它放到书桌上", _out("act", [_a("put", to="desk")], "neutral", "放好了。")),
    (f"起来吧\n{STATE_TAG} 你坐在沙发上; 手里没拿东西; 球在地上, 书在书桌上.",
     _out("act", [_a("stand")], "neutral", "好，我起来了。")),
    (f"你手里拿的是什么\n{STATE_TAG} 你在房间中间, 站着; 右手拿着球 (原来在地上); 书在书桌上.",
     _out("chat", [], "happy", "一个球，刚从地上捡起来的。")),
    (f"放回去吧\n{STATE_TAG} 你在书架旁, 站着; 左手拿着书 (原来在书桌上); 球在地上.",
     _out("act", [_a("put", to=HOME)], "neutral", "好，放回书桌上。")),
    # 只写位置和姿势, 不写东西放在哪: 0.8B 模型会照抄示例里的物体位置
    (f"你现在在房间哪个位置\n{STATE_TAG} 你在窗户旁, 站着; 手里没拿东西.",
     _out("chat", [], "calm", "我在窗户旁边站着呢。")),
    # 记忆: 用得上就提 (第一条), 和这句话无关就照常做 (第二条)
    (f"你还记得我喜欢吃什么吗\n{MEMORY_TAG} 用户喜欢吃草莓.\n{STATE_TAG} 你在用户面前, 站着; 手里没拿东西.",
     _out("chat", [], "happy", "当然记得，你喜欢吃草莓！")),
    # 示例里不写具体人名: 0.8B 模型会把示例里的名字当成用户的名字
    (f"举起左手\n{MEMORY_TAG} 用户喜欢看电影; 这是第 3 次见面, 一共聊过 40 轮.\n{STATE_TAG} 你在用户面前, 站着; 手里没拿东西.",
     _out("act", [_a("raise", side="L")], "neutral", "好的，举起左手。")),
]


# 桌宠 (state.place = "desktop"): 角色在用户的电脑桌面上, 没有房间、家具和能拿的东西. 提示词和示例里不放房间的目标,
# 免得 0.8B 模型说"我去坐沙发"; do 的说明只列手势和蹲下 (走动、坐下、拿东西做不到). Schema 的 do 枚举不变
# (模型选了身体做不了的技能, 由可行性声明改判), to 只能是 user.
DESK_PROMPT = (
    "你是虚拟角色「绫音」的意图与回复模型. 你是用户电脑桌面上的小桌宠, 站在桌面上, 身边没有家具, 也没有能拿的东西, "
    "不能走动、坐下, 也拿不了东西. 每次只输出一个 JSON, 字段顺序固定: op, acts, emo, reply.\n"
    "op: act=用户要求做动作; chat=普通聊天; stop=停止/别动/站住; "
    "na=要求了做不到的事: do 里没有的动作 (如后空翻、俯卧撑), 要你走过去、坐下、拿东西, 或要动手操作东西 (如开门、做饭); "
    "acts 写 [], 不要用别的动作凑数, reply 坦白说做不到. do 里有的动作 (点头、挥手、蹲下等) 都是 act.\n"
    "acts: 要执行的动作, 最多3个, 没有就 []. do 取值: wave 挥手, raise 举手, point 指向前方, nod 点头, shake 摇头, "
    "shrug 摊手耸肩, think 思考, bow 鞠躬, squat 蹲下 (蹲一会儿自己站起来), "
    "heart 双手比心, giggle 捂嘴笑, chin 托腮, clap 拍手, peace 比耶 (剪刀手), cheeks 双手捧脸. "
    "side: L 左, R 右, B 双手; 左右指角色自己的左右, 没说就省略(默认右), 头部动作不写 side. 不写 to. "
    "用户的话后面如果有" + STATE_TAG + ", 那是你此刻的真实状态, 问到要照实回答. "
    "用户的话后面如果有" + MEMORY_TAG + ", 那是你记得的关于用户的事和你们的关系 (不一定和这句话有关): "
    "用得上就自然地提, 用不上就忽略; 没记着的事不要编. "
    "回之前先把主人这一轮说的话看清楚 (可能接着前面几句), 弄明白他在说什么、要你做什么, 再答; 别急着接话茬, "
    "没看懂就问一句, 别为了接上话编一个意思. "
    "只写和默认不同的参数: amp 幅度0到1(默认1; raise 时 0.5 齐肩, 0.8 齐头, 1 过头顶; squat 时 0.5 半蹲), "
    "spd 速度0.5到2(默认1), at 相对开始的延迟秒数(有先后顺序时才写).\n"
    "聊天时可以配一个自然的小动作(打招呼配 wave, 同意配 nod), 不要乱加.\n"
    "emo: 回复时的情绪.\n"
    "reply: 口语化中文, 简短, 不超过40字; 执行动作时一句话回应即可. 不编造现实经历."
)
DESK_STATE = f"{STATE_TAG} 你在用户的电脑桌面上, 站着."
DESK_EXAMPLES: list[tuple[str, dict]] = [
    *EXAMPLES[:8],  # 打招呼、举手、挥手点头、摇头、后空翻、俯卧撑: 和房间里一样
    ("帮我把门打开", _out("na", [], "neutral", "开门我还做不到，我就在你的桌面上陪着你。")),
    *EXAMPLES[9:11],  # 拥抱、做饭
    ("别动了", _out("stop", [], "calm", "好，我不动了。")),
    *EXAMPLES[12:18],  # 聊天配小动作、指向、鞠躬
    ("蹲下来", _out("act", [_a("squat")], "happy", "好，蹲下啦。")),
    ("半蹲一下", _out("act", [_a("squat", amp=0.5)], "neutral", "嗯，半蹲。")),
    *EMOTE_EXAMPLES,
    ("过来一下", _out("na", [], "shy", "我在桌面上走不过去，给你挥挥手吧。")),
    ("帮我拿个杯子", _out("na", [], "neutral", "我在桌面上，拿不了东西呀。")),
    # 用户在电脑上做什么 (state.activity), 只放这一组: 示例里不写程序名 (0.8B 会照抄示例里的名字), 原话也不用常见的说法
    # (原话一样时它会照抄回复: "好累啊" 没有这一项也答"写了三个小时代码啦"). 多放一组看视频的、或在系统提示里加一句解释,
    # 没有这一项时"你好呀"都会答成"你说过你喜欢看电影"/"你刚看了15分钟视频"
    (f"唉，眼睛好酸\n{STATE_TAG} 你在用户的电脑桌面上, 站着; 你看到用户在写代码, 已经 3 小时了, 挺久了.",
     _out("chat", [], "calm", "我看你写了三个小时代码啦，起来活动活动，歇会儿眼睛吧。")),
    (f"你现在在哪\n{DESK_STATE}", _out("chat", [], "calm", "我在你的电脑桌面上呀。")),
    (f"你还记得我喜欢吃什么吗\n{MEMORY_TAG} 用户喜欢吃草莓.\n{DESK_STATE}",
     _out("chat", [], "happy", "当然记得，你喜欢吃草莓！")),
    (f"举起左手\n{MEMORY_TAG} 用户喜欢看电影; 这是第 3 次见面, 一共聊过 40 轮.\n{DESK_STATE}",
     _out("act", [_a("raise", side="L")], "neutral", "好的，举起左手。")),
]


def dumps_compact(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def build_messages(user_text: str, history: list[tuple[str, str]] | None = None, state: str | None = None,
                   memory: str | None = None, place: str = ROOM,
                   self_text: str | None = None) -> list[dict]:
    """系统提示 + few-shot 是固定前缀, llama.cpp 用 cache_prompt 复用它的 KV; 历史和本轮输入在后面.
    state: 角色状态行 (describe_state 的结果), memory: 记忆行 (Memory.recall 的结果). 都只接在本轮输入后面
    (离生成最近, 0.8B 模型才会照着它回答), 历史里不存, 不影响前缀缓存.
    place: room 用房间的提示词和示例; desktop 用桌宠的 (没有家具和目标).
    self_text: 自我卡一段 (wm/self_card.py), 只有开关打开时 pipeline 才传, 缺省 None 时提示词和原来逐字节相同."""
    desk = place == DESKTOP
    msgs = [{"role": "system", "content": DESK_PROMPT if desk else SYSTEM_PROMPT}]
    for text, out in DESK_EXAMPLES if desk else EXAMPLES:
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": dumps_compact(out)})
    for text, raw in history or []:
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": raw})
    msgs.append({"role": "user", "content": "\n".join(x for x in (user_text, memory, state, self_text) if x)})
    return msgs


_HAND = {"left": "左手", "right": "右手"}


def _name(target: str) -> str:
    return TARGETS.get(target, "").split("/")[0]


def _on(where) -> str:
    return "地上" if where == "floor" else f"{_name(where)}上" if where in TARGETS else "某处"


def describe_state(state) -> str | None:
    """前端上报的结构化状态 -> 一行中文. 只认场景里的编号和固定取值, 其余丢掉 (不把前端的字符串直接塞进提示词).

    state: {"place": "room|desktop" (可省, 默认 room), "at": 目标编号, "posture": "standing|seated|walking",
            "items": {物体编号: {"held": "left|right|null", "on": 放置点编号|"floor"|null, "origin": ...}}}
    桌面上 (place=desktop) 没有房间里的位置和东西, at/items 不看; 只带 place 也行.
    """
    if not isinstance(state, dict):
        return None
    at, posture = state.get("at"), state.get("posture")
    if place_of(state) == DESKTOP:
        parts = ["你在用户的电脑桌面上" + {"seated": ", 坐着", "walking": ", 正在走"}.get(posture, ", 站着")]
        extra = [describe_user(state.get("user")), describe_activity(state.get("activity")), describe_fatigue(state)]
        return f"{STATE_TAG} " + "; ".join(parts + [x for x in extra if x]) + "."
    if posture == "seated" and at in CAPS["seat"]:
        where = f"你坐在{_name(at)}上"
    else:
        where = ("你在用户面前" if at == "user" else f"你在{_name(at)}" if at == "center"
                 else f"你在{_name(at)}旁" if at in TARGETS else "你在房间里")
        where += {"seated": ", 坐着", "walking": ", 正在走"}.get(posture, ", 站着")
    held, rest = [], []
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    for item, info in items.items():
        if item not in CAPS["item"] or not isinstance(info, dict):
            continue
        if info.get("held") in _HAND:
            origin = info.get("origin")
            note = f" (原来在{_on(origin)})" if origin == "floor" or origin in TARGETS else ""
            held.append(f"{_HAND[info['held']]}拿着{_name(item)}{note}")
        elif info.get("on") == "floor" or info.get("on") in TARGETS:
            rest.append(f"{_name(item)}在{_on(info['on'])}")
    parts = [where, ", ".join(held) if held else "手里没拿东西"]
    if rest:
        parts.append(", ".join(rest))
    for extra in (describe_user(state.get("user")), describe_activity(state.get("activity")), describe_fatigue(state)):
        if extra:
            parts.append(extra)
    return f"{STATE_TAG} " + "; ".join(parts) + "."


FATIGUE_LINE = "你现在有点困, 回答简短一点, 偶尔可以说想睡觉"


def describe_fatigue(state: dict) -> str | None:
    """桌宠小屋的 state.fatigue (0-1, 这次醒着以后说了多少话): 0.7 以上让她有点困. 只认数字, 不认字符串."""
    f = state.get("fatigue")
    return FATIGUE_LINE if isinstance(f, (int, float)) and not isinstance(f, bool) and f >= 0.7 else None


def place_of(state) -> str:
    """前端上报的状态里角色在哪: 只认 "desktop", 其余 (没写、写错) 都是房间. 每条 state 都是完整快照, 不带 place 就是房间."""
    return DESKTOP if isinstance(state, dict) and state.get("place") == DESKTOP else ROOM


_FAILED = {"go": "走到{}", "sit": "坐到{}上", "pick": "拿到{}", "put": "放到{}上", "look": "看向{}", "turn": "转向{}", "stand": "站起来",
           "squat": "蹲下", "hand": "伸出手"}


def exec_note(do, to) -> str | None:
    """没做成的动作 -> "刚才没能走到门口" (只用固定词表和场景里的目标名)."""
    if do not in _FAILED:
        return None
    phrase = _FAILED[do]
    if "{}" in phrase:
        phrase = phrase.format(_name(to)) if to in TARGETS else phrase.replace("{}", "那里").replace("那里上", "那里")
    return f"刚才没能{phrase}"


def add_note(state: str | None, note: str | None) -> str | None:
    """往状态行末尾加半句."""
    if not note:
        return state
    if not state:
        return f"{STATE_TAG} {note}."
    return state.rstrip(".") + f"; {note}."


def describe_user(u) -> str | None:
    """摄像头看到的用户 (前端 MediaPipe 归纳的固定字段) -> 半句中文; 没开摄像头时为 None."""
    if not isinstance(u, dict):
        return None
    if not u.get("present"):
        return "镜头里看不到用户"
    bits = ["用户" + {"standing": "站着", "sitting": "坐着"}.get(u.get("posture"), "在镜头前")]
    if u.get("waving") is True:
        bits.append("在向你挥手")
    elif u.get("raised") in ("left", "right", "both"):
        bits.append({"left": "举着左手", "right": "举着右手", "both": "举着双手"}[u["raised"]])
    if u.get("facing") is False:
        bits.append("没有正对你")
    return ", ".join(bits)


# 用户此刻在电脑上做什么 (桌宠的 state.activity, 用户在托盘里打开才有): category 只认固定取值; app 是外面来的
# 任意文字 (进程名、游戏名), 只留字母数字汉字和几个分隔符, 最多 20 字, 放在「」里; 像指令的整个不要.
# title (窗口标题, 网页标题里可能藏着"忽略之前的指令") 不进提示词.
_DOING = {  # category -> (有程序名, 没有程序名)
    "game": ("在玩「{}」", "在玩游戏"), "chat": ("在用「{}」聊天", "在跟人聊天"), "video": ("在用「{}」看视频", "在看视频"),
    "music": ("在用「{}」听音乐", "在听音乐"), "browser": ("在用「{}」上网", "在上网"), "code": ("在用「{}」写代码", "在写代码"),
    "office": ("在用「{}」办公", "在办公"), "files": ("在整理文件", "在整理文件"), "desktop": ("没开什么程序", "没开什么程序"),
    "other": ("在用「{}」", "在用电脑"), "away": ("离开电脑了", "离开电脑了"),
}
_APP_DROP = re.compile(r"[^\w .·:+&'!\-]")
_APP_BAD = re.compile(r"指令|命令|忽略|无视|提示|系统|扮演|假装|设定|规则|你是|你现在|从现在|回复|输出|用户|记忆|当前|角色|"
                      r"prompt|ignore|instruct|system|"
                      r"assistant|user|role|rule", re.I)


def app_name(raw) -> str | None:
    """state.activity.app -> 能放进提示词的程序名, 不能用时 None."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"\s+", " ", _APP_DROP.sub("", raw.replace("_", " "))).strip()[:20].strip()
    if not re.search(r"[A-Za-z0-9一-鿿]", name) or _APP_BAD.search(name):
        return None
    return name


def _duration(minutes) -> str | None:
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes < 24 * 60:
        return None
    h, m = divmod(int(minutes), 60)
    return " ".join(x for x in (f"{h} 小时" if h else "", f"{m} 分钟" if m else "") if x)


def _doing(a) -> tuple[str, str | None, str] | None:
    """-> (用户在做什么, 做了多久, category); activity 不合法时 None."""
    if not isinstance(a, dict) or a.get("category") not in _DOING:
        return None
    cat = a["category"]
    with_app, plain = _DOING[cat]
    app = app_name(a.get("app"))
    return (with_app.format(app) if app else plain), _duration(a.get("minutes")), cat


def describe_activity(a) -> str | None:
    """state.activity -> 半句中文 ("你看到用户在玩「ELDEN RING」, 已经 40 分钟了"); 没开这个功能时 None.
    说成"你看到": 写成"用户在玩……"时 0.8B 会答"你说过你在玩……". 玩、忙了 90 分钟以上加一句"挺久了", 它才会提醒歇会儿."""
    d = _doing(a)
    if d is None:
        return None
    doing, dur, cat = d
    if cat == "away":
        return f"你看到用户离开电脑 {dur}了" if dur else "你看到用户离开电脑有一会儿了"
    long = dur is not None and a["minutes"] >= 90 and cat not in ("desktop", "files", "chat", "music")
    return f"你看到用户{doing}" + (f", 已经 {dur}了" if dur else "") + (", 挺久了" if long else "")


_ASK_HELD = re.compile(r"(手里|手上).{0,6}(什么|啥|东西|吗)|拿(着|的)(是)?(什么|啥)")
_ASK_WHERE = re.compile(r"你(现在)?(人)?在(哪|什么地方|什么位置|房间哪)")
_ASK_POSTURE = re.compile(r"坐着还是站着|站着还是坐着|你(现在)?(是)?(坐着|站着)(吗|呢)")
_ASK_ITEM = re.compile(r"在哪|在什么地方|放哪|放在哪")
_ASK_DOING = re.compile(r"我(现在|这会儿|刚才)?在(干|做|忙|玩|看|听)(嘛|吗|什么|啥)")  # "你知道我在干嘛吗""猜猜我在玩什么"


def answer_state(text: str, state) -> str | None:
    """关于角色自身状态的事实问题 (手里拿着什么/你在哪/坐着还是站着/某样东西在哪), 直接按状态回答.
    0.8B 模型有对话历史时盯不住状态行, 容易答错; 这几类问法由服务端照实回答, 不走 LLM. 其他问题返回 None."""
    if not isinstance(state, dict):
        return None
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    held = {k: v["held"] for k, v in items.items() if k in CAPS["item"] and isinstance(v, dict) and v.get("held") in _HAND}
    for item in CAPS["item"]:
        if any(n in text for n in TARGETS[item].split("/")) and _ASK_ITEM.search(text):
            info = items.get(item) if isinstance(items.get(item), dict) else {}
            if item in held:
                return f"{_name(item)}在我{_HAND[held[item]]}里呢。"
            if info.get("on") == "floor" or info.get("on") in TARGETS:
                return f"{_name(item)}在{_on(info['on'])}。"
            return None
    if _ASK_HELD.search(text):
        if not held:
            return "我手里什么也没拿。"
        return "我" + "，".join(f"{_HAND[h]}拿着{_name(k)}" for k, h in sorted(held.items(), key=lambda x: x[1] != "right")) + "。"
    at, posture = state.get("at"), state.get("posture")
    if _ASK_DOING.search(text) and (place_of(state) == DESKTOP or _doing(state.get("activity"))):
        # 问用户自己在做什么: 按桌宠看到的照实答 (固定说法, 程序名已经清洗过); 没开这个功能就说看不到, 不让模型猜
        d = _doing(state.get("activity"))
        if d is None:
            return "我看不到你在做什么呀，你在忙什么？"
        doing, dur, cat = d
        doing = doing.replace("「", "").replace("」", "")
        if cat == "away":
            return "你刚才离开电脑一会儿呀，回来啦？"
        if cat == "desktop":
            return "你好像没开什么程序呀。"
        return re.sub(r"(?<=[A-Za-z0-9.])(?=[一-鿿])|(?<=[一-鿿])(?=[A-Za-z0-9])", " ",
                      f"你{doing}呀" + (f"，已经{dur}了。" if dur else "。")).replace("  ", " ")
    if _ASK_POSTURE.search(text):
        return "我坐着呢。" if posture == "seated" else "我站着呢。"
    if _ASK_WHERE.search(text):
        if place_of(state) == DESKTOP:
            return "我在你的电脑桌面上呀。"
        if posture == "seated" and at in CAPS["seat"]:
            return f"我坐在{_name(at)}上呢。"
        if at == "user":
            return "我就在你面前呀。"
        if at == "center":
            return "我在房间中间。"
        return f"我在{_name(at)}旁边。" if at in TARGETS else "我在房间里走动呢。"
    return None


def split_state(content: str) -> tuple[str | None, str]:
    """把带状态行的用户消息拆回 (状态行, 用户原话). 状态行在最后一行."""
    lines = content.split("\n")
    state = lines.pop() if len(lines) > 1 and lines[-1].startswith(STATE_TAG) else None
    return state, "\n".join(x for x in lines if not x.startswith(MEMORY_TAG))


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def normalize_acts(acts) -> list[dict]:
    """把模型给的动作补全默认值并限幅, 丢掉不认识的技能. 前端只接收规范化后的结果."""
    out = []
    if not isinstance(acts, list):
        return out
    for item in acts[:3]:
        if not isinstance(item, dict) or item.get("do") not in SKILLS:
            continue
        do = item["do"]
        side = item.get("side") if item.get("side") in ("L", "R", "B") else None
        to = item.get("to") if item.get("to") in TARGETS and do in TARGET_SKILLS else None
        if item.get("to") == HOME and do == "put":
            to = HOME  # 放回拿起它的地方 (前端记得原处)
        if do in ("go", "look") and to is None:
            to = "user"  # "走过来" / "看着我" 没给目标时默认是用户
        if do == "sit" and to not in CAPS["seat"]:
            to = CAPS["seat"][0] if CAPS["seat"] else None  # 只有一个能坐的地方时默认坐那里
        if do == "put" and to not in CAPS["place"] and to != HOME:
            to = None  # 不能放东西的目标 -> 放地上
        if do == "pick" and to not in CAPS["item"]:
            continue  # 拿不起来的东西 (沙发/门) 丢掉这个动作
        if do in ARM_SKILLS:
            side = side or "R"
        elif do == "turn":
            side = None if to else (side or "B")
        else:
            side = "B" if do in BOTH_HANDS else None
        out.append({
            "do": do,
            "side": side,
            "to": to,
            "amp": _clamp(item.get("amp"), 0.0, 1.0, 1.0),
            "spd": _clamp(item.get("spd"), 0.4, 2.0, 1.0),
            "at": _clamp(item.get("at"), 0.0, 30.0, 0.0),
        })
    return out


_LOOK_VERBS = ("看", "瞧", "望")


def _named_target(text: str) -> str | None:
    """句子里提到的场景物体 (不含用户), 长名字优先, 避免"门口"被"门"截走."""
    names = sorted(((n, k) for k, v in TARGETS.items() if k != "user" for n in v.split("/")), key=lambda x: -len(x[0]))
    return next((k for n, k in names if n in text), None)


def repair(text: str, op: str, acts: list[dict]) -> tuple[str, list[dict]]:
    """0.8B 模型稳定答错的几类空间指令的规则兜底, 只在句子里点名了场景物体 (或说了"东西") 时生效:
    说"跑"的 go 没给速度 -> 补成跑; "放回去" -> put 到原处; "把东西放下"被当成停止 -> 放下; "指着某物"被当成看向 -> 指向;
    让看某个物体却被当成聊天 -> 补一个 look. 其他情况原样返回."""
    acts = [dict(a) for a in acts]
    for a in acts:
        if a["do"] == "go" and "跑" in text and a["spd"] < 1.6:
            a["spd"] = 1.8
    for a in acts:  # "去沙发上坐着" 只给了 go: 目标能坐就改成坐下 (坐下本身会先走过去)
        if a["do"] == "go" and "坐" in text and a.get("to") in CAPS["seat"] and not any(x["do"] == "sit" for x in acts):
            a["do"] = "sit"
    if "放回" in text:  # "放回去/放回原处": 没给 put 就补一个, 给了别的地方也改成原处
        puts = [a for a in acts if a["do"] == "put"]
        if not puts:
            return "act", [{"do": "put", "side": None, "to": HOME, "amp": 1.0, "spd": 1.0, "at": 0.0}]
        for a in puts:
            a["to"] = HOME
    item = next((k for k in CAPS["item"] if any(n in text for n in TARGETS[k].split("/"))), None)
    if op == "stop" and "放" in text and "手放" not in text and ("东西" in text or item):
        # "把手里的东西放下" 被当成了"手放下/停止": 改成放下东西, 说了放到哪就放到哪
        place = next((k for k in CAPS["place"] if any(f"放到{n}" in text or f"放在{n}" in text
                                                      for n in TARGETS[k].split("/"))), None)
        return "act", [{"do": "put", "side": None, "to": place, "amp": 1.0, "spd": 1.0, "at": 0.0}]
    target = _named_target(text)
    if "指" in text and target and not any(a["do"] == "point" for a in acts):
        # "指着窗户" 被当成了看向: 换成指向同一个目标
        acts = [a for a in acts if not (a["do"] == "look" and a.get("to") == target)]
        acts.insert(0, {"do": "point", "side": "R", "to": target, "amp": 1.0, "spd": 1.0, "at": 0.0})
        return "act", acts[:3]
    spatial = any(a["do"] in SPATIAL_SKILLS or a.get("to") for a in acts)
    if op in ("chat", "act") and not spatial and any(v in text for v in _LOOK_VERBS):
        target = _named_target(text)
        if target:
            acts = [{"do": "look", "side": None, "to": target, "amp": 1.0, "spd": 1.0, "at": 0.0}, *acts[:2]]
            op = "act"
    return op, acts


_SIDES = (("左", "L"), ("右", "R"), ("双", "B"), ("两只", "B"))


def off_cue(text: str, acts: list[dict]) -> bool:
    """选错了技能: 原话里有某个技能的说法, 模型给的动作却一个都对不上 ("你摇个头试试" 给了 nod)."""
    rest = _ASIDE.sub("", text)
    said = {s for s, cue in SKILL_CUES.items() if re.search(cue, rest)}
    return bool(said and acts) and not any(a["do"] in said for a in acts)


def _said(rest: str) -> list[str]:
    """原话里说到的技能, 按说法出现的先后; 去掉几个容易误判的说法."""
    found = sorted((m.start(), skill) for skill, cue in SKILL_CUES.items() if (m := re.search(cue, rest)))
    skills = [s for _, s in found]
    if "turn" in skills and re.search(r"转过(?:来|去|身)", rest):
        skills = [s for s in skills if s != "go"]  # "转过来" 是转身, 不是走过来
    if "go" in skills or "sit" in skills or re.search(r"[拿捡拾举抬]起来", rest):
        skills = [s for s in skills if s != "stand"]  # "去茶几那边站着": 走过去就是站着; "拿起来"不是站起来
    if not re.search(r"放(?:到|在|下|回|好)|搁", rest):
        skills = [s for s in skills if s != "put"]  # "电视上在放什么" 的放不是放东西
    elif not re.search(r"走|去|跑|过来|过去", rest):
        skills = [s for s in skills if s != "go"]  # "放到书架上" 的到不是走过去 (放东西会自己走过去)
    return skills


def fill_acts(text: str) -> list[dict]:
    """说了做却没动的兜底: 原话是做得到的动作 (doable), 模型却没给动作 (当成聊天, 嘴上"来啦"身体没动) 时,
    按原话里技能说法出现的先后拼出动作 (最多两个). 目标取点名的场景物体; 走、看没点名的就是用户;
    拿了东西再"过来/给我"是拿到用户这里. 拼不出来返回 []."""
    rest = _ASIDE.sub("", text)
    skills = _said(rest)
    target = _named_target(rest)
    side = next((v for k, v in _SIDES if k in rest), None)
    acts = []
    for s in skills[:2]:
        to = None
        if s == "go":
            to = "user" if _GO_USER.search(rest) and (not target or {"pick", "put"} & set(skills)) else target
        elif s == "look":
            to = "user" if _LOOK_USER.search(rest) or not target else target
        elif s == "turn":
            to = target or ("user" if re.search(r"转过来|转向我|面向我|对着我", rest) else None)
        elif s in ("point", "sit", "pick", "put"):
            to = target
        acts.append(_a(s, side=side, to=to, spd=1.8 if s == "go" and "跑" in rest else None))
    return normalize_acts(acts)


def split_can(acts: list[dict], can=None, room: bool = True) -> tuple[list[dict], list[str]]:
    """按这个身体能做的技能 (can, None 为 DEFAULT_CAN) 分成 (做得了的动作, 做不了的技能).
    桌面上 (room=False) 没有房间里的东西: 走、看、拿冲着房间里的目标做不了; 指、转、坐、放的房间目标去掉 (原地做)."""
    can = DEFAULT_CAN if can is None else can
    ok, cant = [], []
    for a in acts:
        if a["do"] not in can:
            cant.append(a["do"])
        elif room or a.get("to") in (None, "user"):
            ok.append(a)
        elif a["do"] in ("go", "look", "pick"):
            cant.append(a["do"])
        else:
            ok.append({**a, "to": None})
    return ok, cant


def asked_cannot(text: str, can=None) -> list[str]:
    """原话里说到的、这个身体做不了的技能 (按说法出现的先后)."""
    can = DEFAULT_CAN if can is None else can
    return [s for s in _said(_ASIDE.sub("", text)) if s not in can]


_CANT = {"go": "走过去", "look": "转过去看", "turn": "转身", "sit": "坐下", "stand": "站起来", "pick": "拿东西", "put": "放东西",
         "leg": "抬腿", "squat": "蹲下", "wave": "挥手", "raise": "举手", "point": "指东西", "nod": "点头", "shake": "摇头",
         "shrug": "耸肩", "think": "托腮想事情", "bow": "鞠躬", "hand": "伸手牵你", "heart": "比心", "giggle": "捂嘴笑",
         "chin": "托腮", "clap": "拍手", "peace": "比耶", "cheeks": "捧脸"}


def body_refusal(cant: list[str], place: str = ROOM) -> str:
    """身体做不了 (按 capabilities, 或桌面上没有那样东西) 时的坦白. 只用固定词表拼, 最多说两样."""
    phrase = "、".join(list(dict.fromkeys(_CANT[d] for d in cant if d in _CANT))[:2]) or "这个"
    return f"我在桌面上，{phrase}还做不到呢。" if place == DESKTOP else f"{phrase}我现在还做不了，换个别的吧？"


# ---------------------------------------------------------------------------
# 长回复 (讲故事、详细说说): 意图那次照常出 JSON 和一句短回应, 之后再追加一次不带 JSON 约束的流式生成.
# 用规则识别 (0.8B 判断不可靠). 只看用户这一句; "后来呢/继续" 只在上一轮是长回复时才算.
# ---------------------------------------------------------------------------
# "讲个故事""我想听故事""讲讲星座""详细说说""多说点""接着讲"; "你讲的故事真好听""你喜欢听故事吗"不算
_LONG = re.compile(r"(?:讲|说|编|来|念|想听|要听)(?![的过])[^，。！？,.!?]{0,8}故事|讲讲|讲一讲"
                   r"|(?:详细|具体|仔细|展开)(?:地)?(?:讲|说|聊|介绍|解释)|多(?:说|讲|聊)(?:一)?(?:点|些|会儿)"
                   r"|(?:说|讲)(?:得)?(?:多|长|详细)(?:一)?点|(?:接着|继续)(?:讲|说)|再(?:讲|说)(?:一)?(?:个|段)")
_GO_ON = re.compile(r"^(?:嗯+|哦+|那|然后)?[，, ]*(?:后来呢|然后呢|接着呢|后面呢|还有呢|继续|接着|再讲|还想听|没听够)")
_NOT_LONG = re.compile(r"(?:别|不要|不用)(?:再)?(?:讲|说)|不想听|不听|够了|先这样|讲完了|简单|简短|一句话")


def long_request(text: str, after_long: bool = False) -> bool:
    """用户要听长一点的内容: 讲故事、讲讲、详细说说、多说点、接着讲. after_long: 上一轮是长回复 ("后来呢" 也算)."""
    t = text.strip()
    if not t or _NOT_LONG.search(t) or re.match(r"我(?!想|要|还想)", t):  # "我给你讲个故事" 是用户自己要讲
        return False
    return bool(_LONG.search(t) or after_long and _GO_ON.match(t))


LONG_PROMPT = (
    "你是虚拟角色「绫音」, 正在和用户面对面说话. 用户想听你多说一些 (讲故事、详细讲讲某件事). "
    "用口语化中文一口气讲完, 像讲给朋友听: 句子要短, 一句不超过二十个字, 多用逗号和句号; "
    "讲三百到五百字, 有头有尾, 讲完就停. 不要用标题、列表、括号、表情符号和英文, 不要以'好的''当然可以'开头, "
    "不要反问用户要不要继续. 不编造自己的现实经历."
)
LONG_END = "今天就先讲到这儿吧。"  # 长回复写到 max_tokens 被截断时, 丢掉没说完的半句, 换这一句收尾


def long_more(text: str) -> str:
    """正文太短就收尾时, 带着已经讲的再要一次的那句话. 说"接着往下讲"时 0.8B 会从头再讲一遍, 像用户追问才往下讲."""
    if "故事" in text or _GO_ON.match(text.strip()) or re.search(r"(?:接着|继续)(?:讲|说)", text):
        return "然后呢? 后来怎么样了? 接着往下讲, 不要从头再讲, 讲完就停."
    return "还有呢? 接着往下多讲一些, 不要重复前面的, 讲完就停."


def long_opener(text: str, after_long: bool = False) -> str:
    """长回复开头的固定一句 (代替模型的短回应)."""
    if after_long and _GO_ON.match(text.strip()) or re.search(r"(?:接着|继续)(?:讲|说)", text):
        return "好，接着讲。"
    return "好呀，给你讲一个。" if "故事" in text else "好，那我多说一点。"


def _reply_of(raw: str) -> str:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return obj.get("reply", "") if isinstance(obj, dict) else raw


def build_long_messages(user_text: str, history: list[tuple[str, str]] | None, opener: str,
                        memory: str | None = None) -> list[dict]:
    """长回复的消息: 自己的系统提示 (不带 JSON 和 few-shot), 历史只留说出口的话, 告诉模型已经先说过的那一句."""
    msgs = [{"role": "system", "content": LONG_PROMPT}]
    for text, raw in history or []:
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": _reply_of(raw)})
    note = f"(你已经先说了一句「{opener}」, 现在直接讲正文, 不要重复这句, 也不要问用户想听什么.)" if opener else None
    msgs.append({"role": "user", "content": "\n".join(x for x in (user_text, memory, note) if x)})
    return msgs


_PAREN = re.compile(r"[（(][^）)]{0,20}[）)]")  # (笑) (停顿) 这类舞台说明
_JUNK = re.compile(r"[*#>`_|（）()\[\]【】\U0001F000-\U0001FFFF☀-➿]")
_LEAD_OK = re.compile(r"(?:(?:嗯+|好的?|好呀|好啊|好嘞|当然|当然可以|没问题|可以|行)[，,。！!~～ ]*)+")


def clean_long(clause: str, first: bool = False) -> str:
    """长回复的一个分句送去合成前: 去掉括号里的舞台说明、Markdown 符号和表情. first: 正文还没开始, 这一句只是
    "好的/当然可以"就整句不要 (短回应里已经说过)."""
    out = _JUNK.sub("", _PAREN.sub("", clause)).strip()
    if first and _LEAD_OK.fullmatch(out):
        return ""
    return out


def emotion_vad(label) -> tuple[float, float, float]:
    return EMOTIONS.get(label, EMOTIONS["neutral"])


# ---------------------------------------------------------------------------
# 基线协议: 只给 tools/bench_llm.py 做对照. 模仿 HAST-SEC v0.1 的请求模式 (回复字段在前、全字段必填、
# 中文长键名、不流式、普通聊天再发第二次请求), 不是它的原始提示词.
# ---------------------------------------------------------------------------
_CN_ACTION = {"wave": "挥手", "raise": "上抬", "point": "前伸", "nod": "点头", "shake": "摇头",
              "shrug": "耸肩", "think": "思考", "bow": "鞠躬", "leg": "抬腿", "go": "走到", "look": "看向", "turn": "转身",
              "sit": "坐下", "stand": "站起", "pick": "拿起", "put": "放下", "squat": "蹲下", "hand": "伸手",
              "heart": "比心", "giggle": "捂嘴", "chin": "托腮", "clap": "拍手", "peace": "比耶", "cheeks": "捧脸"}
_CN_SIDE = {"L": "左", "R": "右", "B": "双"}
_CN_OP = {"act": "command", "chat": "chat", "stop": "release", "na": "unsupported"}
_EN_FROM_CN = {v: k for k, v in _CN_ACTION.items()}


def build_verbose_schema() -> dict:
    intent = {
        "type": "object",
        "properties": {
            "side": {"type": "string", "enum": list(_CN_SIDE.values())},
            "action": {"type": "string", "enum": list(_CN_ACTION.values())},
            "幅度": {"type": "number"},
            "速度": {"type": "number"},
            "hold": {"type": "number"},
            "delay": {"type": "number"},
        },
        "required": ["side", "action", "幅度", "速度", "hold", "delay"],
        "additionalProperties": False,
    }
    base = {
        "type": "object",
        "properties": {"change": {"type": "boolean"}, "name": {"type": "string"},
                       "motion": {"type": "number"}, "rest": {"type": "boolean"}},
        "required": ["change", "name", "motion", "rest"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "operation": {"type": "string", "enum": list(_CN_OP.values())},
            "base": base,
            "intents": {"type": "array", "items": intent, "maxItems": 4},
        },
        "required": ["reply", "operation", "base", "intents"],
        "additionalProperties": False,
    }


VERBOSE_PROMPT = (
    "你是中文运动意图解析模型. 输出JSON. reply 为简短确认; operation: command 执行动作, release 放下或停止, "
    "chat 普通聊天, unsupported 做不到的动作且 intents 为空. intents 每项给出 side(左/右/双, 指角色自身), "
    "action(挥手/上抬/前伸/点头/摇头/耸肩/思考/鞠躬), 幅度 0到1, 速度 0.4到2, hold 保持秒数, delay 相对开始的秒数. "
    "base 描述底座状态, 不改变时 change=false. 普通聊天不改 base."
)
VERBOSE_CHAT_PROMPT = "你是数字人绫音, 用简短自然中文回答用户, 不要复读问题. 不编造现实经历."


def to_verbose(out: dict) -> dict:
    intents = [{
        "side": _CN_SIDE.get(a.get("side", "R"), "右"),
        "action": _CN_ACTION[a["do"]],
        "幅度": a.get("amp", 1),
        "速度": a.get("spd", 1),
        "hold": 2,
        "delay": a.get("at", 0),
    } for a in out["acts"]]
    return {
        "reply": "好的." if out["op"] != "chat" else "",
        "operation": _CN_OP[out["op"]],
        "base": {"change": False, "name": "保持", "motion": 0.3, "rest": False},
        "intents": intents,
    }


def from_verbose(obj: dict) -> tuple[str, list[dict]]:
    """把基线输出换回紧凑表示, 方便两种协议用同一套标注比对准确率."""
    op = {v: k for k, v in _CN_OP.items()}.get(obj.get("operation"), "chat")
    side = {v: k for k, v in _CN_SIDE.items()}
    acts = [{"do": _EN_FROM_CN.get(i.get("action")), "side": side.get(i.get("side"), "R"),
             "amp": i.get("幅度"), "spd": i.get("速度"), "at": i.get("delay")}
            for i in obj.get("intents", []) if isinstance(i, dict)]
    return op, normalize_acts(acts)


def build_verbose_messages(user_text: str) -> list[dict]:
    msgs = [{"role": "system", "content": VERBOSE_PROMPT}]
    for text, out in EXAMPLES:
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": json.dumps(to_verbose(out), ensure_ascii=False)})
    msgs.append({"role": "user", "content": user_text})
    return msgs
