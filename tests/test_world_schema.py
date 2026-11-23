"""跨体 schema (wm/world/schema.py) 的纯单测: 真实 room_* 消息往返一个字段都不丢, 未知类型必须报错.

全部是纯逻辑, 不连 8790、不连模型、不读配置 (真实字段的出处写在每个用例上面). 跑法 (仓库根):
    python -m pytest tests/test_world_schema.py -q
"""
from __future__ import annotations

import pytest

from wm.world.schema import (ACTION_KINDS, WORLD_KINDS, ActionIntent, PerceptionEvent, is_action_msg,
                            is_world_msg, kind_of)

# 世界 -> Core: 都是页面真的会发上来的形状 (字段见 wm/pipeline.py:678-765 的 docstring)
WORLD_MSGS = [
    # 走路上报的一帧 (desktop/web/room-life.js:503-546 的 sight.report)
    {"type": "room_observe", "scene_id": "petal-oak-life-v1", "id": 7, "at": "desk",
     "seen": {"surf_desk": ["IR_Item_Mug_0"], "water_caddy": []}, "hand": [], "open": [],
     "events": [{"kind": "fall", "item": "IR_Item_Mug_1", "where": "floor", "toppled": True, "loud": "loud"}]},
    # 第一帧 (没有 at, 也没有 id): 只进认知, Core 不回
    {"type": "room_observe", "scene_id": "petal-oak-life-v1", "at": None, "seen": {}, "hand": [], "open": [],
     "regions": ["surf_desk", "surf_bed"]},
    # 找东西 (wm/pipeline.py:705-713)
    {"type": "room_search", "scene_id": "petal-oak-life-v1", "id": 12, "item": "IR_Item_Mug_0", "new": True},
    # 端东西之前问这一趟怎么端 (wm/pipeline.py:740-748)
    {"type": "room_carry", "scene_id": "petal-oak-life-v1", "id": 13, "cup": "IR_Item_Espresso_Pink",
     "fill": "espresso", "hot": True, "route": ["desk", "bed"], "source": "coffee_machine", "wait_s": 6},
    # 小屋里点了她 (room_event 自己的 kind 是 poke | feed, 和 schema 的 kind 撞名)
    {"type": "room_event", "scene_id": "petal-oak-life-v1", "kind": "poke", "part": "head", "count": 2},
    # 物理动作的回执 (desktop/web/pet.js:297 把执行结果发回来)
    {"type": "room_action_result", "scene_id": "petal-oak-life-v1", "request_id": "room.41.1",
     "turn": 41, "act": "chair_turn", "started": True, "completed": True, "ok": True,
     "outcome": "completed", "cleanupConfirmed": True, "cancelled": False,
     "detail": {"text": "椅子转了 90 度", "known": True}},
    # 做完一件可供性 (wm/pipeline.py:725-738)
    {"type": "room_afford_done", "scene_id": "petal-oak-life-v1", "id": 22, "act": "coffee", "ctx": "desk",
     "ok": True, "felt": "香", "hurt": False, "skip": False},
    # 端完了 (wm/pipeline.py:750-765)
    {"type": "room_carry_done", "scene_id": "petal-oak-life-v1", "id": 23, "delivered": True, "waited": 3},
    # 买完了 (Hemifuture §16.3 账本闭环, 世界侧 Shop 的回执; 钱和货都在世界那边)
    {"type": "room_trade_done", "scene_id": "petal-oak-life-v1", "id": 24, "ok": True, "item": "chair",
     "price": 30, "balance": 470, "refunded": False, "msg": "买成了「chair」", "tx": ["tx00000002"]},
]

# Core -> 世界: 真的会从 8790 发下来的形状 (wm/pipeline.py:1075, 767-800, 705-713, 1090)
ACTION_MSGS = [
    {"type": "room_action", "scene_id": "petal-oak-life-v1", "request_id": "room.41.1", "turn": 41,
     "act": "chair_turn", "timeout_ms": 180000},
    {"type": "room_action_cancel", "scene_id": "petal-oak-life-v1", "request_id": "room.41.1", "reason": "timeout"},
    {"type": "room_plan", "scene_id": "petal-oak-life-v1", "id": 5, "ok": True, "pick": "coffee",
     "thought": "有点困, 冲一杯", "how": ["slow"], "ms": 812, "engine": "llama"},
    {"type": "room_plan", "scene_id": "petal-oak-life-v1", "id": 6, "ok": False, "reason": "rate"},
    {"type": "room_step", "scene_id": "petal-oak-life-v1", "id": 12, "ok": True, "item": "IR_Item_Mug_0",
     "action": ["go 咖啡角"], "say": "去咖啡角看看", "knows": 1},
    {"type": "room_step", "scene_id": "petal-oak-life-v1", "id": 13, "ok": True, "item": "IR_Item_Mug_0",
     "action": ["open 冰箱"], "say": "", "knows": 0},
    {"type": "room_step", "scene_id": "petal-oak-life-v1", "id": 14, "ok": True, "item": "IR_Item_Mug_0",
     "action": ["close"], "say": "", "knows": 0},
    {"type": "room_step", "scene_id": "petal-oak-life-v1", "id": 15, "ok": False, "item": "IR_Item_Mug_0",
     "action": ["give_up", "不认识"], "say": "", "knows": 0},
]


@pytest.mark.parametrize("msg", WORLD_MSGS, ids=lambda m: m["type"] + str(m.get("id")))
def test_world_msg_roundtrip(msg):
    """真实消息 -> PerceptionEvent -> 真实消息: 一个字段都不丢、也不多出来."""
    ev = PerceptionEvent.from_msg(msg)
    assert ev.to_msg() == msg


@pytest.mark.parametrize("msg", ACTION_MSGS, ids=lambda m: m["type"] + str(m.get("id")))
def test_action_msg_roundtrip(msg):
    """真实消息 -> ActionIntent -> 真实消息: 字段不丢 (step 的 action 从动词和目标拼回去)."""
    intent = ActionIntent.from_msg(msg)
    assert intent.to_msg() == msg


def test_payload_keeps_nested_fields():
    """嵌套结构原样保留 (seen / open / events 是 dict 和 list, 不能被摊平或改成字符串)."""
    msg = WORLD_MSGS[0]
    ev = PerceptionEvent.from_msg(msg)
    assert ev.kind == "observe" and ev.world_id == "petal-oak-life-v1"
    assert ev.payload["seen"] == msg["seen"] and ev.payload["events"] == msg["events"]
    assert ev.payload["id"] == 7
    # 拆出来的 payload 里没有 type / scene_id (它们由 kind 和 world_id 表示)
    assert "type" not in ev.payload and "scene_id" not in ev.payload


def test_scene_id_maps_to_world_id():
    """world_id 就是线上的 scene_id; 没有就不凭空造一个, 也不往线上补."""
    ev = PerceptionEvent.from_msg(WORLD_MSGS[1])
    assert ev.world_id == "petal-oak-life-v1"
    bare = {k: v for k, v in WORLD_MSGS[1].items() if k != "scene_id"}
    ev2 = PerceptionEvent.from_msg(bare)
    assert ev2.world_id == "" and "scene_id" not in ev2.to_msg()


def test_event_kind_does_not_collide_with_room_event_kind():
    """room_event 自己的 kind (poke | feed) 留在 payload 里; schema 的 kind 是消息族."""
    ev = PerceptionEvent.from_msg({"type": "room_event", "scene_id": "s", "kind": "poke", "part": "head", "count": 1})
    assert ev.kind == "event" and ev.payload["kind"] == "poke"


def test_result_keeps_original_wire_type():
    """result 有三种真实类型, 重建时必须还原原来那种 (Core 对三者的处理不一样)."""
    for msg in (WORLD_MSGS[5], WORLD_MSGS[6], WORLD_MSGS[7]):
        ev = PerceptionEvent.from_msg(msg)
        assert ev.kind == "result" and ev.wire == msg["type"]
        assert ev.to_msg()["type"] == msg["type"]


def test_action_verb_and_target():
    """行动意图的动词和目标来自真实字段: act / pick / action[0] 的第一个词; cancel 恒为 cancel."""
    # 两条消息可以共用一个 request_id (取消的是同一件事), 所以按下标取, 不按 id 建字典
    got = [ActionIntent.from_msg(m) for m in ACTION_MSGS]
    assert got[0].kind == "action" and got[0].verb == "chair_turn" and got[0].target == ""
    assert got[0].params["request_id"] == "room.41.1" and got[0].params["timeout_ms"] == 180000
    assert got[2].kind == "plan" and got[2].verb == "coffee" and got[2].target == ""
    assert got[3].kind == "plan" and got[3].verb == "" and got[3].target == ""
    assert got[4].verb == "go" and got[4].target == "咖啡角"
    assert got[5].verb == "open" and got[5].target == "冰箱"
    assert got[6].verb == "close" and got[6].target == ""
    assert got[7].verb == "give_up" and got[7].target == ""
    # give_up 的第二个元素是放弃的原因, 不是动作对象: 它留在 params 里, 不塞进 target
    assert got[7].params["action"] == ["give_up", "不认识"]
    assert got[1].kind == "cancel" and got[1].verb == "cancel" and got[1].params["reason"] == "timeout"


def test_ts_is_local_and_never_sent():
    """ts 是本地时刻, 用来算延迟; 它不写进真实消息 (Core 不认识这个字段)."""
    ev = PerceptionEvent.from_msg(WORLD_MSGS[0], ts=1234.5)
    assert ev.ts == 1234.5 and "ts" not in ev.to_msg()
    assert PerceptionEvent("dummy", "", "observe").ts > 0


def test_unknown_kind_raises():
    """不认识的类型必须报错 (不能悄悄当成别的族, 否则世界侧改了协议没人发现)."""
    with pytest.raises(ValueError):
        PerceptionEvent.from_msg({"type": "room_frobnicate", "scene_id": "s"})
    with pytest.raises(ValueError):
        PerceptionEvent.from_msg({"kind": "poke"})  # 连 type 都没有
    with pytest.raises(ValueError):
        ActionIntent.from_msg({"type": "text", "text": "把椅子搬到窗边"})
    with pytest.raises(ValueError):
        PerceptionEvent("world", "s", "dance")  # 直接构造也要拦
    with pytest.raises(ValueError):
        ActionIntent("s", "coffee", "", kind="dance")
    with pytest.raises(TypeError):
        PerceptionEvent.from_msg("room_observe")
    with pytest.raises(TypeError):
        ActionIntent.from_msg(["room_action"])


def test_wrong_direction_raises():
    """方向反了要说清楚: 把 Core -> 世界 的消息喂给感知, 报错里点出是哪一种."""
    with pytest.raises(ValueError, match="Core -> 世界"):
        PerceptionEvent.from_msg(ACTION_MSGS[0])
    with pytest.raises(ValueError, match="世界 -> Core"):
        ActionIntent.from_msg(WORLD_MSGS[0])


def test_reserved_keys_rejected():
    """payload / params 里带 type、scene_id 就报错: 这两个的归属是固定的, 免得线上出现两份真相."""
    for key in ("type", "scene_id"):
        with pytest.raises(ValueError):
            PerceptionEvent("world", "s", "observe", {key: "x"})
        with pytest.raises(ValueError):
            ActionIntent("s", "coffee", "", {key: "x"})
    # source 不是保留键: room_carry 真的有 source (从哪儿端来的), 必须能留在 payload 里
    per = PerceptionEvent("world", "s", "carry", {"source": "coffee_machine"})
    assert per.to_msg()["source"] == "coffee_machine"
    # 而且它不会被误当成"谁报的"那个 source (那个只来自本地参数)
    assert PerceptionEvent.from_msg(per.to_msg(), source="dummy").source == "dummy"


def test_wire_must_match_kind():
    """保真的原类型不能和消息族串台."""
    with pytest.raises(ValueError):
        PerceptionEvent("world", "s", "observe", wire="room_action_result")
    with pytest.raises(ValueError):
        ActionIntent("s", "coffee", "", kind="plan", wire="room_action")


def test_bypass_helpers_never_raise():
    """旁路日志用 kind_of / is_*: 不认识的消息只记一笔, 不抛异常."""
    assert kind_of({"type": "room_observe"}) == "observe"
    assert kind_of({"type": "room_plan"}) == "plan"
    assert kind_of({"type": "hello"}) is None and kind_of(None) is None and kind_of("room_observe") is None
    assert is_world_msg(WORLD_MSGS[5]) and not is_world_msg(ACTION_MSGS[0])
    assert is_action_msg(ACTION_MSGS[2]) and not is_action_msg(WORLD_MSGS[0])
    assert not is_action_msg({"type": "room_afford_done"})


def test_kind_tables_match_pipeline():
    """消息族覆盖的线上类型必须和服务端的分发白名单一致 (改协议时这里会一起红)."""
    # wm/pipeline.py:400-402 的 scene_replies + 423 的 room_action_result
    assert WORLD_KINDS == ("observe", "search", "carry", "event", "result")
    # wm/pipeline.py:1075 的 room_action、1090 的 room_action_cancel、767-800 的 room_plan、705-713 的 room_step
    assert ACTION_KINDS == ("action", "cancel", "plan", "step")
    kinds = {kind_of(m) for m in WORLD_MSGS}
    assert kinds == {"observe", "search", "carry", "event", "result"}
    assert {kind_of(m) for m in ACTION_MSGS} == {"action", "cancel", "plan", "step"}
