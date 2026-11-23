"""跨体闭环的事件 schema (2026-11-20, Hemifuture Phase 0 的第一步): 世界 <-> Core 的最小共同语言.

铁律 (docs/v5 语音评估与 Hemifuture 落地第一步（2026-11-20）.md §16.1):
  Core 不认识世界 —— 它只收到 PerceptionEvent, 只说出 ActionIntent, 消息里没有世界的常识;
  世界不持有自我 —— 它只执行 ActionIntent、只上报 PerceptionEvent, 不解释她是谁.

这里**不新造协议**: 两种消息一一映射到桌宠小屋已经在用的 room_* 消息 (分发见 wm/pipeline.py:398-403,
现场动作请求见 wm/pipeline.py:1075, 回执落回 wm/pipeline.py:423-428):
  世界 -> Core: room_observe / room_search / room_carry / room_event / room_action_result
                (外加 room_afford_done / room_carry_done 这两种"做完了"的回执, 都算 result)
  Core -> 世界: room_action / room_action_cancel / room_plan / room_step
字段表在 docs/跨体闭环（2026-11-20）.md, payload 里每个字段都是真实消息里的字段名, 一个不臆造.

为什么不叫 wm/world.py: wm/world/ 是已经存在的包 (世界插件宿主, 见 wm/world/__init__.py), 同名的 wm/world.py
会被包遮蔽、import 不到, 所以 schema 作为包里的一块 (wm.world.schema). 零 IO、不 import wm 别的模块:
没有网络、没有模型、没有配置文件也能测.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# 世界 -> Core 的消息族. 和真实 room_* 消息一一对应 (见 _TYPE_KIND).
WORLD_KINDS = ("observe", "search", "carry", "event", "result")
# Core -> 世界的消息族 (见 _ACTION_DEFAULT).
ACTION_KINDS = ("action", "cancel", "plan", "step")

# kind -> 默认的线上类型. 只有 result 有多个真实类型 (见 _TYPE_KIND), 其余一对一.
_DEFAULT_MSG = {"observe": "room_observe", "search": "room_search", "carry": "room_carry",
                "event": "room_event", "result": "room_action_result"}
_TYPE_KIND = {"room_observe": "observe", "room_search": "search", "room_carry": "carry",
              "room_event": "event", "room_action_result": "result",
              # 这两种也是"世界报了结果": Core 一个记进发现簿 (wm/pipeline.py:725-738),
              # 一个记一趟端东西 (wm/pipeline.py:750-765), 处理方式不同, 所以靠 wire 保真原类型
              "room_afford_done": "result", "room_carry_done": "result",
              # 买东西的回执 (2026-11-20, Hemifuture §16.3 账本闭环): 世界侧扣款/摆放/交付之后报
              # {ok, item, price, balance, msg, tx}; 钱和货都在世界那边, Core 只看这一条就知道还剩多少点
              "room_trade_done": "result"}
_DEFAULT_MSG_ACTION = {"action": "room_action", "cancel": "room_action_cancel",
                       "plan": "room_plan", "step": "room_step"}
_TYPE_ACTION = {v: k for k, v in _DEFAULT_MSG_ACTION.items()}
# payload / params 里不许出现的键: type 由 kind 与 wire 决定, scene_id 由 world_id 决定.
# 别的字段 (包括 room_carry 自带的 source = 从哪儿端来的) 都是真实字段, 原样留在 payload 里.
_RESERVED = ("type", "scene_id")


def now() -> float:
    """本地时刻 (秒). 只是一条消息在本进程里被收/发的时刻, 不上线."""
    return time.time()


def kind_of(msg) -> str | None:
    """这条消息属于哪个消息族; 不是跨体消息 (或根本不是 dict) 返回 None.

    旁路日志用这个: 不认识的消息只记一笔, 不抛异常. 要"必须认识"的地方用 from_msg, 它会报错."""
    if not isinstance(msg, dict):
        return None
    typ = msg.get("type")
    return _TYPE_KIND.get(typ) or _TYPE_ACTION.get(typ)


def is_world_msg(msg) -> bool:
    """是不是世界 -> Core 的感知消息."""
    return isinstance(msg, dict) and msg.get("type") in _TYPE_KIND


def is_action_msg(msg) -> bool:
    """是不是 Core -> 世界的行动意图."""
    return isinstance(msg, dict) and msg.get("type") in _TYPE_ACTION


def _payload_of(msg: dict) -> dict:
    """把真实消息里除 type / scene_id 以外的字段原样搬出来 (一个不改, 一个不漏)."""
    return {k: v for k, v in msg.items() if k not in _RESERVED}


@dataclass(slots=True)
class PerceptionEvent:
    """世界 -> Core 的一条感知事件: 世界此刻是什么样, 或者刚发生了什么.

    source    谁报的 (本地标注, 不上线): 日志里区分桌面 / 假世界 / 以后的游戏
    world_id  哪个世界: 就是真实消息里的 scene_id (wm/cognition/layout.py 的场景编号, 默认 petal-oak-life-v1).
              空串 = 这条消息没带 scene_id (Core 按这条连接的当前场景处理)
    kind      WORLD_KINDS 之一: observe 看见 / search 找东西 / carry 端东西 / event 小屋里的事 / result 做完了的回执.
              注意 room_event 自己的 kind (poke | feed) 在 payload 里, 名字撞了但不是一个东西
    payload   真实消息里除 type / scene_id 以外的所有字段, 原样保留:
              id / at / seen / hand / open / events / affords / request_id / act / started / completed / ok / ...
    ts        本地时刻 (秒), 不上线
    wire      线上原类型. 一个 kind 对应多个真实类型时靠它保真 (result 可以是 room_action_result /
              room_afford_done / room_carry_done); 空 = 用 kind 的默认类型
    """

    source: str
    world_id: str
    kind: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=now)
    wire: str = ""

    def __post_init__(self) -> None:
        if self.kind not in WORLD_KINDS:
            raise ValueError(f"不认识的世界消息族: {self.kind!r} (只认 {WORLD_KINDS})")
        typ = self.wire or _DEFAULT_MSG[self.kind]
        if _TYPE_KIND.get(typ) != self.kind:
            raise ValueError(f"线上类型 {typ!r} 不属于 {self.kind!r}")
        if not isinstance(self.payload, dict):
            raise TypeError(f"payload 要是 dict, 收到 {type(self.payload).__name__}")
        bad = [k for k in _RESERVED if k in self.payload]
        if bad:
            raise ValueError(f"payload 里不能带 {bad}: type 由 kind 与 wire 决定, scene_id 由 world_id 决定")

    def to_msg(self) -> dict:
        """拼回一条真实的 room_* 消息 (可以直接 send 给 8790)."""
        msg = dict(self.payload)
        msg["type"] = self.wire or _DEFAULT_MSG[self.kind]
        if self.world_id:
            msg["scene_id"] = self.world_id
        return msg

    @classmethod
    def from_msg(cls, msg: dict, *, source: str = "world", ts: float | None = None) -> "PerceptionEvent":
        """从一条真实的 room_* 消息拆出感知事件; 不是世界 -> Core 的消息就报错 (旁路日志请用 kind_of)."""
        if not isinstance(msg, dict):
            raise TypeError(f"消息要是 dict, 收到 {type(msg).__name__}")
        typ = msg.get("type")
        kind = _TYPE_KIND.get(typ)
        if kind is None:
            if typ in _TYPE_ACTION:
                raise ValueError(f"{typ!r} 是 Core -> 世界的消息, 不是感知事件")
            raise ValueError(f"不是世界 -> Core 的感知消息: type={typ!r}")
        return cls(source=str(source), world_id=str(msg.get("scene_id") or ""),
                   kind=kind, payload=_payload_of(msg), ts=now() if ts is None else float(ts), wire=typ)


@dataclass(slots=True)
class ActionIntent:
    """Core -> 世界的一条行动意图: 要这个身体去做一件事, 不说"为什么".

    world_id  哪个世界: 真实消息里的 scene_id. 空串 = 不指定 (Core 按连接的当前场景处理)
    verb      动作. action: room_action 的 act (wm/room_action.py 的 16 个 id); plan: room_plan 的 pick
              (场景表里的活动 id); step: room_step 的 action[0] 里第一个词 (go / open / close / look / pick / ask /
              give_up); cancel: 恒为 "cancel" (线上没有这个字段, 只是给日志和比较一个名字)
    target    step 的 action[0] 里动词后面那段 ("go 冰箱" 的 "冰箱", "close" 的 ""); 别的类型线上没有目标字段, 留空
    params    真实消息里除 type / scene_id / 上面用掉的那个字段以外的所有字段, 原样保留:
              id / request_id / turn / timeout_ms / item / thought / how / ms / engine / knows / say / reason / ...
    ts        本地时刻 (秒), 不上线
    kind      ACTION_KINDS 之一: action 做一件被声明的活动 / plan 小屋"接下来做什么" / step 找东西的下一步 / cancel 取消
    wire      线上原类型 (目前每个 kind 只有一个, 留给以后)
    """

    world_id: str
    verb: str
    target: str
    params: dict = field(default_factory=dict)
    ts: float = field(default_factory=now)
    kind: str = "action"
    wire: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"不认识的行动意图族: {self.kind!r} (只认 {ACTION_KINDS})")
        typ = self.wire or _DEFAULT_MSG_ACTION[self.kind]
        if _TYPE_ACTION.get(typ) != self.kind:
            raise ValueError(f"线上类型 {typ!r} 不属于 {self.kind!r}")
        if not isinstance(self.params, dict):
            raise TypeError(f"params 要是 dict, 收到 {type(self.params).__name__}")
        bad = [k for k in _RESERVED if k in self.params]
        if bad:
            raise ValueError(f"params 里不能带 {bad}: type / scene_id 由 kind 与 world_id 决定")

    def to_msg(self) -> dict:
        """拼回一条真实的 room_* 消息 (这条能直接 send 给 8790 的身体)."""
        msg = dict(self.params)
        if self.kind == "action":
            # 只在这个键原本就在场时才写: 失败的 plan 没有 pick, 别替线上补一个空值
            if self.verb or "act" in msg:
                msg["act"] = self.verb
        elif self.kind == "plan":
            if self.verb or "pick" in msg:
                msg["pick"] = self.verb
        elif self.kind == "step":
            raw = self.params.get("action")
            tail = [str(x) for x in raw][1:] if isinstance(raw, list) else []
            first = f"{self.verb} {self.target}".strip()
            if first or tail:
                msg["action"] = ([first] if first else []) + tail
            elif "action" in self.params:
                msg["action"] = []
        # cancel: 线上没有动词字段, params 里已经是全部
        msg["type"] = self.wire or _DEFAULT_MSG_ACTION[self.kind]
        if self.world_id:
            msg["scene_id"] = self.world_id
        return msg

    @classmethod
    def from_msg(cls, msg: dict, *, ts: float | None = None) -> "ActionIntent":
        """从一条真实的 room_* 消息拆出行动意图; 不是 Core -> 世界的消息就报错 (旁路日志请用 kind_of)."""
        if not isinstance(msg, dict):
            raise TypeError(f"消息要是 dict, 收到 {type(msg).__name__}")
        typ = msg.get("type")
        kind = _TYPE_ACTION.get(typ)
        if kind is None:
            if typ in _TYPE_KIND:
                raise ValueError(f"{typ!r} 是世界 -> Core 的消息, 不是行动意图")
            raise ValueError(f"不是 Core -> 世界的消息: type={typ!r}")
        body = {k: v for k, v in msg.items() if k not in _RESERVED}
        verb = target = ""
        if kind == "action":
            if body.get("act"):  # 空的 act 不拆出来 (失败的 plan 没有 pick 这类), 原样留在 params 里
                verb = str(body.pop("act"))
        elif kind == "plan":
            if body.get("pick"):
                verb = str(body.pop("pick"))
        elif kind == "step":
            raw = body.get("action")
            head = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], str) else ""
            verb, _, target = head.partition(" ")
            target = target.strip()
        else:  # cancel
            verb = "cancel"
        return cls(world_id=str(msg.get("scene_id") or ""), verb=verb, target=target, params=body,
                   ts=now() if ts is None else float(ts), kind=kind, wire=typ)
