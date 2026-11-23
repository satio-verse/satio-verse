"""体素房间世界模型的测试（体素街区 Phase 0）。

覆盖：常量、加人/满员、移动（归一化/出界夹紧/滑墙/悬空方块）、放置校验（压人/距离/重复/
上限/材质/整数坐标）、版本号、AOI「进 1 出 2」滞后、patch 增量与 entered 补发（含离开
期间被拆掉方块的 del 对账）、nearby、events_since、snapshot、出生点避障。

运行：python -m pytest tests/test_world_voxel.py -q
"""

from __future__ import annotations

import math

import pytest

from wm.world import voxel as V


def make_world(*names: str, room: str = "test"):
    """建一个世界并依次加入玩家，返回 (world, [Player, ...])。"""
    world = V.VoxelWorld(room=room)
    players = [world.add_player(f"p{i}", name) for i, name in enumerate(names)]
    return world, players


def teleport(world, player, x: float, z: float):
    """测试专用：直接挪位置并刷新 AOI（不做行走物理）。"""
    player.pos = (float(x), float(z))
    return world.update_interest(player.id)


def drain(world, player) -> list[dict]:
    """把某玩家待取事件清空（进房事件会污染后续断言）。"""
    return world.events_since(player.id)


# ---------------------------------------------------------------------------
# 1. 常量 / chunk 工具
# ---------------------------------------------------------------------------


def test_constants():
    assert V.GRID == 16
    assert V.CHUNK == 4
    assert V.CHUNKS == 4
    assert V.BODY == 0.35
    assert V.SPEED == 3.0
    assert V.MAX_PLAYERS == 8
    assert V.MAX_BLOCKS == 512
    assert (V.BLOCK_MIN_Y, V.BLOCK_MAX_Y) == (1, 6)
    assert V.REACH == 6.0
    assert (V.CHUNK_ENTER, V.CHUNK_EXIT) == (1, 2)


def test_chunk_helpers():
    assert V.chunk_of(0.0, 0.0) == (0, 0)
    assert V.chunk_of(3.99, 15.5) == (0, 3)
    assert V.chunk_of(4.0, 8.0) == (1, 2)
    # 房间外坐标夹到最近的边缘 chunk
    assert V.chunk_of(-5.0, 100.0) == (0, 3)
    assert V.chunk_dist((0, 0), (2, 3)) == 3
    assert V.chunk_dist((1, 2), (1, 2)) == 0
    assert V.chunk_center_dist((6.0, 6.0), (1, 1)) == 0.0
    assert V.chunk_center_dist((8.0, 8.0), (0, 0)) == pytest.approx(math.hypot(6.0, 6.0))


# ---------------------------------------------------------------------------
# 2. 加人 / 满员
# ---------------------------------------------------------------------------


def test_add_player_and_room_full():
    world = V.VoxelWorld()
    for i in range(V.MAX_PLAYERS):
        player = world.add_player(f"p{i}", f"玩家{i}")
        assert world.player(player.id) is player
    with pytest.raises(V.VoxelError) as excinfo:
        world.add_player("p8", "第九个")
    assert str(excinfo.value) == "房间满了"
    assert excinfo.value.message == "房间满了"
    # 删人：不存在则静默忽略；删完腾出位置
    world.remove_player("p0")
    world.remove_player("p0")
    assert world.player("p0") is None
    assert world.add_player("p8", "第九个") is not None
    assert world.queued_position() is None


# ---------------------------------------------------------------------------
# 3-6. 移动
# ---------------------------------------------------------------------------


def test_move_normalizes_direction():
    world, (a, b) = make_world("a", "b")
    start = a.pos
    assert world.move(a.id, 3.0, 4.0, 0.1)[0] is True
    assert world.move(b.id, 1.0, 1.0, 0.1)[0] is True
    dist_a = math.dist(start, a.pos)
    dist_b = math.dist(start, b.pos)
    assert dist_a == pytest.approx(V.SPEED * 0.1, abs=1e-9)
    assert dist_b == pytest.approx(V.SPEED * 0.1, abs=1e-9)
    assert abs(dist_a - dist_b) < 1e-6


def test_move_clamps_at_room_edge():
    world, (a,) = make_world("a")
    ok, _ = world.move(a.id, -100.0, 0.0, 5.0)
    assert ok is True
    assert a.pos[0] == pytest.approx(V.BODY, abs=1e-9)
    assert a.pos[1] == pytest.approx(8.0)
    ok, _ = world.move(a.id, 100.0, 0.0, 10.0)
    assert ok is True
    assert a.pos[0] == pytest.approx(V.GRID - V.BODY, abs=1e-9)


def test_move_slides_along_wall():
    world, (a,) = make_world("a")
    assert world.place(a.id, 6, 1, 8)[0] is True  # 正前方（x 负方向）一堵 y=1 的墙
    ok, _ = world.move(a.id, -1.0, -1.0, 0.5)
    assert ok is True
    assert 7.30 < a.pos[0] < 7.45  # x 被方块挡住
    assert a.pos[1] < 7.2  # z 方向照走：滑墙
    # 只沿 x 走时同样是贴着墙停下（不会穿过去）
    world, (b,) = make_world("b")
    assert world.place(b.id, 6, 1, 8)[0] is True
    ok, why = world.move(b.id, -1.0, 0.0, 0.5)
    assert ok is True
    assert why == "沿墙滑动"
    assert b.pos[0] == pytest.approx(7.35, abs=0.02)
    # 贴着墙再走一次：完全动不了
    ok, why = world.move(b.id, -1.0, 0.0, 0.5)
    assert ok is False
    assert "挡住" in why
    assert b.pos[0] == pytest.approx(7.35, abs=0.02)


def test_move_blocked_by_hanging_block():
    world, (a,) = make_world("a")
    # y=3 的悬空方块：身体高度覆盖 y 1..6，所以也算撞
    assert world.place(a.id, 6, 3, 8)[0] is True
    ok, _ = world.move(a.id, -1.0, 0.0, 0.5)
    assert ok is True
    assert a.pos[0] == pytest.approx(7.35, abs=0.02)


# ---------------------------------------------------------------------------
# 7-12. 放置 / 拆除校验
# ---------------------------------------------------------------------------


def test_place_cannot_crush_player():
    world, (a,) = make_world("a")
    ok, why = world.place(a.id, 8, 1, 8)  # 正好在玩家脚下
    assert ok is False
    assert "站" in why
    assert world.block_count() == 0
    assert world.version == 0
    # 只有 y=1 的地板层检查身体；头顶的方块不检查
    assert world.place(a.id, 8, 2, 8)[0] is True


def test_place_out_of_reach():
    world, (a,) = make_world("a")
    ok, why = world.place(a.id, 15, 1, 8)  # 中心距离 7.5 > REACH
    assert ok is False
    assert "太远" in why
    assert world.place(a.id, 13, 1, 8)[0] is True  # 5.5 <= 6


def test_place_duplicate_and_remove_errors():
    world, (a,) = make_world("a")
    assert world.place(a.id, 8, 1, 9, mat=1)[0] is True
    ok, why = world.place(a.id, 8, 1, 9, mat=1)
    assert ok is False
    assert "已经" in why
    ok, why = world.remove(a.id, 9, 1, 9)  # 没有方块
    assert ok is False
    assert "没有" in why
    assert world.remove(a.id, 8, 1, 9)[0] is True
    assert world.place(a.id, 8, 1, 9, mat=1)[0] is True
    teleport(world, a, 1.5, 1.5)  # 走远，超 REACH
    ok, why = world.remove(a.id, 8, 1, 9)
    assert ok is False
    assert "太远" in why


def test_place_block_limit():
    world, (a,) = make_world("a")
    candidates: list[tuple[int, int, int]] = []
    for y in range(V.BLOCK_MIN_Y + 1, V.BLOCK_MAX_Y + 1):  # y=2..6，避开地板压人检查
        for x in range(V.GRID):
            for z in range(V.GRID):
                if math.hypot(x + 0.5 - 8.0, z + 0.5 - 8.0) <= V.REACH:
                    candidates.append((x, y, z))
    assert len(candidates) > V.MAX_BLOCKS
    for x, y, z in candidates[: V.MAX_BLOCKS]:
        ok, why = world.place(a.id, x, y, z)
        assert ok is True, why
    assert world.block_count() == V.MAX_BLOCKS
    ok, why = world.place(a.id, *candidates[V.MAX_BLOCKS])
    assert ok is False
    assert "上限" in why
    assert world.block_count() == V.MAX_BLOCKS


def test_place_rejects_bad_material_and_coords():
    world, (a,) = make_world("a")
    for mat in (-1, 4, 1.5, True):
        ok, why = world.place(a.id, 8, 2, 9, mat=mat)
        assert ok is False
        assert "材质" in why
    for coords in ((8.0, 2, 9), (8, 2.0, 9), (8, 2, 9.0)):
        ok, why = world.place(a.id, *coords)
        assert ok is False
        assert "整数" in why
    assert world.place(a.id, 8, 0, 9)[0] is False  # y=0 是地板
    assert world.place(a.id, 8, 7, 9)[0] is False
    assert world.place(a.id, 16, 2, 9)[0] is False
    assert world.place(a.id, -1, 2, 9)[0] is False


def test_version_only_moves_on_success():
    world, (a,) = make_world("a")
    assert world.version == 0
    assert world.place(a.id, 8, 1, 9)[0] is True
    assert world.version == 1
    assert world.place(a.id, 8, 1, 9)[0] is False
    assert world.version == 1
    assert world.remove(a.id, 8, 1, 9)[0] is True
    assert world.version == 2
    assert world.remove(a.id, 8, 1, 9)[0] is False
    assert world.version == 2
    assert world.place(a.id, 8, 1, 9)[0] is True
    assert world.version == 3


# ---------------------------------------------------------------------------
# 13. AOI 滞后：进 1 出 2
# ---------------------------------------------------------------------------


def test_aoi_enter_exit_hysteresis():
    world, (a,) = make_world("a")
    # 出生 chunk 是 (2,2)，AOI = 3x3。往西跨一格到 chunk (1,2)：进 3 个 chunk，不退出任何
    entered, exited = teleport(world, a, 7.9, 8.0)
    assert exited == set()
    assert entered == {(0, 1), (0, 2), (0, 3)}
    # 重复调用幂等
    assert world.update_interest(a.id) == (set(), set())
    # 在边界（x=8）来回走：没走出迟滞带，不该反复增删
    entered, exited = teleport(world, a, 8.1, 8.0)
    assert (entered, exited) == (set(), set())
    entered, exited = teleport(world, a, 7.9, 8.0)
    assert (entered, exited) == (set(), set())
    # 真正走出退出阈值（跑到 chunk (0,0)）才出现 exited
    entered, exited = teleport(world, a, 2.0, 2.0)
    assert entered == {(0, 0), (1, 0)}
    assert exited == {(0, 3), (1, 3), (2, 3), (3, 1), (3, 2), (3, 3)}
    assert not (entered & exited)


# ---------------------------------------------------------------------------
# 14-15. patch 增量 / entered 补发 / sent_version
# ---------------------------------------------------------------------------


def test_patch_incremental_and_entered_backfill():
    world, (a, b) = make_world("a", "b")
    teleport(world, a, 1.5, 1.5)  # A 在西南角 chunk (0,0)
    teleport(world, b, 15.5, 15.5)  # B 在东北角 chunk (3,3)，看不到 (0,0)
    assert world.patch(b.id) == {"version": 0, "set": [], "del": []}
    assert world.place(a.id, 2, 1, 1)[0] is True
    # A 自己能看到
    assert [2, 1, 1, 0] in world.patch(a.id)["set"]
    # 远处的 B 看不到
    patch_b = world.patch(b.id)
    assert patch_b["set"] == []
    assert patch_b["del"] == []
    # B 走近，chunk (0,0) 进入 AOI → 即使版本号没变也必须补发
    teleport(world, b, 5.5, 5.5)
    patch_b2 = world.patch(b.id)
    assert [2, 1, 1, 0] in patch_b2["set"]
    assert patch_b2["del"] == []


def test_patch_advances_sent_version():
    world, (a,) = make_world("a")
    assert world.place(a.id, 8, 1, 9)[0] is True
    first = world.patch(a.id)
    assert first["set"] == [[8, 1, 9, 0]]
    assert a.sent_version == world.version == 1
    second = world.patch(a.id)
    assert second == {"version": 1, "set": [], "del": []}


def test_patch_backfills_deletions_while_away():
    world, (a, b) = make_world("a", "b")
    teleport(world, a, 4.5, 4.5)
    teleport(world, b, 2.5, 2.5)
    assert world.place(a.id, 2, 1, 1)[0] is True
    assert [2, 1, 1, 0] in world.patch(b.id)["set"]
    # B 走远（(0,0) 退出 AOI），方块在 B 看不见时被拆掉
    teleport(world, b, 15.5, 15.5)
    world.patch(b.id)
    assert world.remove(a.id, 2, 1, 1)[0] is True
    # B 回来：(0,0) 重新进入 AOI，必须补一条 del，否则客户端会画幽灵方块
    teleport(world, b, 2.5, 2.5)
    back = world.patch(b.id)
    assert back["del"] == [[2, 1, 1]]
    assert back["set"] == []


# ---------------------------------------------------------------------------
# 16-18. nearby / events_since / snapshot
# ---------------------------------------------------------------------------


def test_nearby_only_aoi_without_self():
    world, (a, b, c) = make_world("a", "b", "c")
    teleport(world, b, 4.0, 4.0)  # chunk (1,1)：在 A 的 3x3 AOI 内
    teleport(world, c, 1.0, 1.0)  # chunk (0,0)：在 A 的 AOI 外
    seen = world.nearby(a.id)
    assert [entry["id"] for entry in seen] == [b.id]
    assert set(seen[0]) == {"id", "name", "pos", "yaw"}
    assert seen[0]["pos"] == [4.0, 4.0]
    # A 自己挪到西南角后，B 和 C 都在 AOI 内；仍然不含自己
    teleport(world, a, 2.0, 2.0)
    ids = {entry["id"] for entry in world.nearby(a.id)}
    assert ids == {b.id, c.id}
    assert a.id not in ids


def test_events_since_aoi_and_dedup():
    world, (a, b, c) = make_world("a", "b", "c")
    teleport(world, a, 1.5, 1.5)  # chunk (0,0)
    teleport(world, b, 2.5, 2.5)  # chunk (0,0)：听得到 A
    teleport(world, c, 15.5, 15.5)  # chunk (3,3)：听不到（0,0）
    for player in (a, b, c):
        drain(world, player)  # 清掉进房事件
    world.say(a.id, "你们好")
    heard_b = [ev for ev in world.events_since(b.id) if ev["k"] == "said"]
    assert len(heard_b) == 1
    assert heard_b[0]["id"] == a.id
    assert heard_b[0]["text"] == "你们好"
    assert heard_b[0]["pos"] == [1.5, 1.5]
    heard_c = [ev for ev in world.events_since(c.id) if ev["k"] == "said"]
    assert heard_c == []
    # 第二次取不会重复
    assert world.events_since(b.id) == []


def test_snapshot_shape():
    world, (a, b) = make_world("a", "b", room="garden")
    assert world.place(a.id, 8, 1, 9, mat=2)[0] is True
    snap = world.snapshot()
    assert set(snap) == {"room", "version", "blocks", "players"}
    assert snap["room"] == "garden"
    assert snap["version"] == 1
    assert snap["blocks"] == [[8, 1, 9, 2]]
    assert len(snap["players"]) == 2
    for entry in snap["players"]:
        assert set(entry) == {"id", "name", "pos", "yaw"}
        assert isinstance(entry["pos"], list) and len(entry["pos"]) == 2
        assert "sent_version" not in entry
        assert "interest" not in entry


# ---------------------------------------------------------------------------
# 19-21. 出生点 / 未知玩家 / 事件内容
# ---------------------------------------------------------------------------


def test_spawn_avoids_blocks_and_explicit_spawn_rejected():
    world = V.VoxelWorld()
    a = world.add_player("a", "a")
    a.pos = (4.5, 8.5)
    world.update_interest("a")
    assert world.place("a", 8, 1, 8)[0] is True  # 占住房间中心
    b = world.add_player("b", "b")  # 默认出生点必须避开中心
    assert b.pos != (8.0, 8.0)
    assert world._blocked(b.pos[0], b.pos[1]) is False  # 白盒：确认真的不撞
    assert V.BODY <= b.pos[0] <= V.GRID - V.BODY
    assert V.BODY <= b.pos[1] <= V.GRID - V.BODY
    with pytest.raises(V.VoxelError):
        world.add_player("c", "c", (8.0, 8.0))  # 显式出生点撞方块 → 拒绝
    c = world.add_player("c", "c", (12.0, 3.0))
    assert c.pos == (12.0, 3.0)


def test_unknown_player_queries():
    world, _ = make_world("a")
    assert world.move("nope", 1.0, 0.0, 0.1)[0] is False
    assert world.place("nope", 1, 1, 1)[0] is False
    assert world.remove("nope", 1, 1, 1)[0] is False
    for call in (
        lambda: world.patch("nope"),
        lambda: world.update_interest("nope"),
        lambda: world.nearby("nope"),
        lambda: world.events_since("nope"),
        lambda: world.say("nope", "喂"),
    ):
        with pytest.raises(V.VoxelError):
            call()


def test_place_and_remove_events():
    world, (a,) = make_world("a")
    drain(world, a)
    assert world.place(a.id, 8, 1, 9, mat=2)[0] is True
    place_events = [ev for ev in world.events_since(a.id) if ev["k"] == "place"]
    assert len(place_events) == 1
    assert place_events[0]["block"] == [8, 1, 9, 2]
    assert place_events[0]["id"] == a.id
    assert place_events[0]["v"] > 0
    assert world.remove(a.id, 8, 1, 9)[0] is True
    remove_events = [ev for ev in world.events_since(a.id) if ev["k"] == "remove"]
    assert len(remove_events) == 1
    assert remove_events[0]["block"] == [8, 1, 9, 2]
