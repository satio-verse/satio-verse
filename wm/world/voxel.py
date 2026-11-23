"""体素房间的权威真值层（体素街区原型 Phase 0，2026-11-20）。

【这个文件是什么】
  一块 16x16 的平地房间：y=0 是地板（不可编辑），墙外不存在。每个格子最多一个 1.0 见方的
  方块；每个玩家是一个半径 BODY 的圆。移动、放置、拆除、视角，全都在这里算出唯一答案——
  它是服务端的"真值"（authoritative world model），浏览器/UE 客户端只是本地预测。
  纯 Python、只用标准库、不碰网络、不读配置、不 import 项目里的其它模块。

【和 wm/world/schema.py 的关系】
  schema.py 定的是跨体消息/意图的**形状**：PerceptionEvent、ActionIntent 有哪些字段、
  怎么映射到 room_* 协议——那是信封。voxel.py 是信封里的**内容**：房间此刻实际长什么样。
  一句话：schema 管"怎么说"，这里管"是什么"。以后真换游戏引擎时，schema 可以保留，
  这个文件才是被整体替换掉的那一层。
  （中继 tools/voxel_relay.py 把这里的 snapshot/patch/events 翻译成线协议；广播由中继做，
  本模块只负责真值和事件流水。）

【为什么 AOI 按 4x4 chunk「进 1 出 2」】
  16x16 切成 4x4 = 16 个 chunk。带宽是硬的：Hemifuture 落地对照 §16.1③ 给的修法是
  "AOI 边缘抖动 → 滞后 + 增量 + 优先级 + InterestSet"，§16.3 带宽闭环要求每玩家
  <= 20 KB/s、tick p95 < 50 ms。如果进入和退出用同一个半径，玩家在 chunk 边界上来回走，
  每跨一次就要重发/撤回一整个 chunk 的方块——8 个人在边界踱步就能把预算打满。所以：
    - 进入：切比雪夫 chunk 距离 <= CHUNK_ENTER = 1（看到 3x3 个 chunk）
    - 退出：切比雪夫 chunk 距离 <= CHUNK_EXIT  = 2（5x5 个 chunk）
  多出来的一圈是"迟滞带"：只要没走出 5x5，已有 chunk 就不会被撤回，增删自然收敛。

【坐标与判定约定】
  pos 只存 (x, z) 平面位置；yaw 弧度只存不管。方块 (x, y, z) 占
  [x, x+1) x [y, y+1) x [z, z+1)，y in [BLOCK_MIN_Y, BLOCK_MAX_Y]。
  玩家身体圆的高度覆盖 y in [1, 6]（所以 y=3 的悬空方块照样挡人，y=0 地板不挡）。
  移动逐轴扫掠（先 x 后 z）：被挡的那个轴停下、另一个轴照走，即"滑墙"。

【增量与事件】
  方块增量 = 版本日志 self.changes（每条成功 place/remove 追加一行）+ 每玩家 sent_version。
  进入新 chunk 时版本号已经"翻篇"的方块靠 self._pending_enter_blocks 补发（entered backfill）。
  Player.known 是"该玩家客户端已知有哪些方块"的影子：玩家离开期间方块被拆掉后，回来时
  仍能补一条 del，否则客户端会一直画着已经不存在的方块。
  事件 self.events 是 200 条的环；事件的 v 是独立单调递增的事件序号（不是房间版本号：
  entered/left/said 不 bump version，用版本号去重会互相吃掉）。
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

__all__ = [
    "GRID",
    "CHUNK",
    "CHUNKS",
    "BODY",
    "SPEED",
    "MAX_PLAYERS",
    "MAX_BLOCKS",
    "BLOCK_MIN_Y",
    "BLOCK_MAX_Y",
    "REACH",
    "CHUNK_ENTER",
    "CHUNK_EXIT",
    "VoxelError",
    "Player",
    "VoxelWorld",
    "chunk_of",
    "chunk_dist",
    "chunk_center_dist",
]

# ---------------------------------------------------------------------------
# 世界观常量：照抄，别改数值
# ---------------------------------------------------------------------------

GRID = 16                          # 房间边长（格）
CHUNK = 4                          # AOI 分块边长（格）
CHUNKS = GRID // CHUNK             # 每边 chunk 数 = 4

BODY = 0.35                        # 玩家身体半径
SPEED = 3.0                        # 移动速度（单位/秒）
MAX_PLAYERS = 8                    # 房间人数上限
MAX_BLOCKS = 512                   # 方块数量上限

BLOCK_MIN_Y, BLOCK_MAX_Y = 1, 6    # 可放方块的高度范围（y=0 是地板）

REACH = 6.0                        # 放置/拆除的最远距离
CHUNK_ENTER, CHUNK_EXIT = 1, 2     # AOI 滞后：进 1 出 2

# 一帧位移拆成不超过 5cm 的小步再逐轴扫掠，防止 dt 抖动时一步穿过一格方块
_MOVE_STEP = 0.05


class VoxelError(Exception):
    """世界模型拒绝的请求：房间满了、出生点被占、查询不存在的玩家等。

    message 一律是可读中文，直接可以回给客户端。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# chunk 工具：AOI 的坐标换算与切比雪夫距离
# ---------------------------------------------------------------------------


def chunk_of(x: float, z: float) -> tuple[int, int]:
    """(x, z) 落在哪个 chunk；房间外的坐标夹到最近的边缘 chunk。"""
    cx = int(math.floor(float(x) / CHUNK))
    cz = int(math.floor(float(z) / CHUNK))
    if cx < 0:
        cx = 0
    elif cx >= CHUNKS:
        cx = CHUNKS - 1
    if cz < 0:
        cz = 0
    elif cz >= CHUNKS:
        cz = CHUNKS - 1
    return (cx, cz)


def chunk_dist(a: tuple[int, int], b: tuple[int, int]) -> int:
    """两个 chunk 的切比雪夫距离；AOI 进出判定用的就是它。"""
    return max(abs(int(a[0]) - int(b[0])), abs(int(a[1]) - int(b[1])))


def chunk_center_dist(pid_pos, chunk: tuple[int, int]) -> float:
    """玩家位置到 chunk 中心的平面距离（排序/优先级用，不参与 AOI 进出判定）。"""
    cx = chunk[0] * CHUNK + CHUNK / 2.0
    cz = chunk[1] * CHUNK + CHUNK / 2.0
    return math.hypot(float(pid_pos[0]) - cx, float(pid_pos[1]) - cz)


# ---------------------------------------------------------------------------
# 玩家
# ---------------------------------------------------------------------------


@dataclass
class Player:
    """一个玩家。pos 是 (x, z)，yaw 弧度只存不管。"""

    id: str
    name: str
    pos: tuple[float, float]
    yaw: float = 0.0
    sent_version: int = 0
    interest: set[tuple[int, int]] = field(default_factory=set)
    # 内部字段，不参与 snapshot 下发：
    last_event_version: int = 0                                  # 已取到的事件序号
    known: dict[tuple[int, int, int], int] = field(default_factory=dict)  # 客户端已知方块


def _is_int(v) -> bool:
    """严格整数判定（bool 是 int 的子类，这里要排除掉）。"""
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------------------
# 世界
# ---------------------------------------------------------------------------


class VoxelWorld:
    """一个房间的权威状态。所有公开方法只改本地内存，不做任何 IO。

    线程模型：假定单线程（中继的 asyncio 协程里串行调用），不要跨线程共享。
    """

    def __init__(self, room: str = "garden") -> None:
        self.room = str(room)
        self.version = 0
        # 方块真值：(x, y, z) -> mat
        self.blocks: dict[tuple[int, int, int], int] = {}
        self.players: dict[str, Player] = {}
        # 事件环：进房/离开/说话/放置/拆除/传送门
        self.events: deque[dict] = deque(maxlen=200)
        self.full_since: float | None = None
        # 方块版本日志：每条成功 place/remove 追加 (version, kind, payload)
        self.changes: deque[tuple[int, str, tuple]] = deque()
        # 进入新 chunk 时要补发的方块（按玩家 id）
        self._pending_enter_blocks: dict[str, set[tuple[int, int, int]]] = {}
        # 事件序号：独立于房间版本号，单调递增
        self._event_seq = 0

    # -- 内部工具 ----------------------------------------------------------

    def _require(self, pid: str) -> Player:
        p = self.players.get(pid)
        if p is None:
            raise VoxelError(f"玩家不存在: {pid}")
        return p

    def _emit(self, kind: str, **fields) -> dict:
        """写一条事件并分配事件序号；广播由中继层按 AOI 决定。"""
        self._event_seq += 1
        ev = {"k": kind, "v": self._event_seq}
        ev.update(fields)
        self.events.append(ev)
        return ev

    def _blocked(self, x: float, z: float) -> bool:
        """玩家身体圆（半径 BODY）是否与任何 y in [1,6] 的方块 AABB 相交。"""
        x_lo = int(math.floor(x - BODY))
        x_hi = int(math.floor(x + BODY))
        z_lo = int(math.floor(z - BODY))
        z_hi = int(math.floor(z + BODY))
        for bx in range(x_lo, x_hi + 1):
            for bz in range(z_lo, z_hi + 1):
                # 圆心到该格 AABB 的最近点
                near_x = bx if x < bx else (bx + 1.0 if x > bx + 1.0 else x)
                near_z = bz if z < bz else (bz + 1.0 if z > bz + 1.0 else z)
                ddx = near_x - x
                ddz = near_z - z
                if ddx * ddx + ddz * ddz >= BODY * BODY:
                    continue
                for by in range(BLOCK_MIN_Y, BLOCK_MAX_Y + 1):
                    if (bx, by, bz) in self.blocks:
                        return True
        return False

    def _slide_axis(self, x: float, z: float, delta: float, along_x: bool) -> float:
        """沿一个轴走 delta（可以越界），返回不撞方块的最终坐标。

        出界夹到 [BODY, GRID-BODY]；中途拆成小步，撞到方块就停在最后一步。
        """
        if delta == 0.0:
            return x if along_x else z
        cur = x if along_x else z
        hi = GRID - BODY
        target = cur + delta
        if target < BODY:
            target = BODY
        elif target > hi:
            target = hi
        if target == cur:
            return cur
        steps = max(1, int(math.ceil(abs(target - cur) / _MOVE_STEP)))
        step = (target - cur) / steps
        for _ in range(steps):
            nxt = cur + step
            blocked = self._blocked(nxt, z) if along_x else self._blocked(x, nxt)
            if blocked:
                break
            cur = nxt
        return cur

    def _reach_dist(self, p: Player, x: int, z: int) -> float:
        """玩家到方块格子中心的平面距离（和客户端准星落点一致）。"""
        return math.hypot((x + 0.5) - p.pos[0], (z + 0.5) - p.pos[1])

    def _body_hits_cell(self, x: int, z: int) -> bool:
        """任意玩家的身体圆是否压到 (x, z) 这一格（用于 y=1 的地板方块）。"""
        for q in self.players.values():
            qx, qz = q.pos
            near_x = x if qx < x else (x + 1.0 if qx > x + 1.0 else qx)
            near_z = z if qz < z else (z + 1.0 if qz > z + 1.0 else qz)
            ddx = near_x - qx
            ddz = near_z - qz
            if ddx * ddx + ddz * ddz < BODY * BODY:
                return True
        return False

    def _free_spawn(self) -> tuple[float, float]:
        """默认出生点：房间中心附近，找一个不撞方块的圆点。"""
        center = GRID / 2.0
        if not self._blocked(center, center):
            return (center, center)
        best: tuple[float, float] | None = None
        best_d2 = float("inf")
        step = 0.25
        hi = GRID - BODY
        x = BODY
        while x <= hi + 1e-9:
            z = BODY
            while z <= hi + 1e-9:
                if not self._blocked(x, z):
                    d2 = (x - center) ** 2 + (z - center) ** 2
                    if d2 < best_d2:
                        best_d2 = d2
                        best = (x, z)
                z += step
            x += step
        if best is None:
            raise VoxelError("房间里没有空位，放不下新玩家")
        return best

    def _checked_spawn(self, spawn) -> tuple[float, float]:
        """校验中继指定的出生点：夹进房间、撞方块就拒绝（不静默换位置）。"""
        try:
            x = float(spawn[0])
            z = float(spawn[1])
        except (TypeError, ValueError, IndexError):
            raise VoxelError("出生点必须是 (x, z) 两个数字")
        if not (math.isfinite(x) and math.isfinite(z)):
            raise VoxelError("出生点必须是有限数字")
        x = min(max(x, BODY), GRID - BODY)
        z = min(max(z, BODY), GRID - BODY)
        if self._blocked(x, z):
            raise VoxelError(f"出生点 ({x:.2f}, {z:.2f}) 被方块挡住了")
        return (x, z)

    def _prune_changes(self) -> None:
        """把所有玩家都已确认的方块增删从日志里丢掉。"""
        if not self.changes:
            return
        if not self.players:
            self.changes.clear()
            return
        floor = min(p.sent_version for p in self.players.values())
        while self.changes and self.changes[0][0] <= floor:
            self.changes.popleft()

    # -- 玩家 --------------------------------------------------------------

    def add_player(
        self,
        pid: str,
        name: str,
        spawn: tuple[float, float] | None = None,
    ) -> Player:
        """加人。满员抛 VoxelError("房间满了")。

        spawn 缺省时在房间中心附近找不撞方块的点；显式 spawn 撞方块也抛 VoxelError，
        不静默换位置（否则中继以为人站在传送门上、客户端却出现在别处）。
        这里只写事件流水，不广播——广播由中继按 AOI 做。
        """
        if pid in self.players:
            raise VoxelError(f"玩家已存在: {pid}")
        if len(self.players) >= MAX_PLAYERS:
            if self.full_since is None:
                self.full_since = time.time()
            raise VoxelError("房间满了")
        pos = self._free_spawn() if spawn is None else self._checked_spawn(spawn)
        p = Player(id=str(pid), name=str(name), pos=(float(pos[0]), float(pos[1])))
        p.sent_version = self.version
        self.players[p.id] = p
        self._emit("entered", id=p.id, name=p.name, pos=[p.pos[0], p.pos[1]])
        p.last_event_version = self._event_seq
        # 初始 AOI + 进入补发（房间里已有的方块不能等他慢慢走过来才知道）
        self.update_interest(p.id)
        if len(self.players) >= MAX_PLAYERS:
            self.full_since = time.time()
        else:
            self.full_since = None
        return p

    def remove_player(self, pid: str) -> None:
        """删人；不存在则静默忽略（中继重连/重复断线时省事）。"""
        p = self.players.pop(pid, None)
        if p is None:
            return
        self._emit("left", id=p.id, name=p.name, pos=[p.pos[0], p.pos[1]])
        self._pending_enter_blocks.pop(pid, None)
        if len(self.players) < MAX_PLAYERS:
            self.full_since = None
        if not self.players:
            # 没人看，增量日志和事件环都没有保留价值
            self.changes.clear()
            self.events.clear()
        else:
            self._prune_changes()

    def player(self, pid: str) -> Player | None:
        return self.players.get(pid)

    def move(self, pid: str, dx: float, dz: float, dt: float) -> tuple[bool, str]:
        """走一步。dx/dz 方向向量（长度 > 1 时归一化），速度是 SPEED 单位/秒。

        逐轴分离：先 x 后 z，撞方块/撞墙的那个轴不动、另一个轴照走——滑墙。
        返回 (是否真的动了, 中文原因)。
        """
        p = self.players.get(pid)
        if p is None:
            return (False, f"玩家不存在: {pid}")
        try:
            dx = float(dx)
            dz = float(dz)
            dt = float(dt)
        except (TypeError, ValueError):
            return (False, "位移和时间必须是数字")
        if not (math.isfinite(dx) and math.isfinite(dz) and math.isfinite(dt)):
            return (False, "位移和时间必须是有限数字")
        if dt <= 0.0:
            return (False, "时间间隔必须大于 0")
        length = math.hypot(dx, dz)
        if length == 0.0:
            return (False, "没有移动方向")
        if length > 1.0:
            dx /= length
            dz /= length
        distance = SPEED * dt
        x0, z0 = p.pos
        new_x = self._slide_axis(x0, z0, dx * distance, True)
        new_z = self._slide_axis(new_x, z0, dz * distance, False)
        moved_x = abs(new_x - x0) > 1e-12
        moved_z = abs(new_z - z0) > 1e-12
        p.pos = (new_x, new_z)
        if moved_x and moved_z:
            return (True, "移动成功")
        if moved_x or moved_z:
            return (True, "沿墙滑动")
        return (False, "被方块或房间边缘挡住了")

    def set_yaw(self, pid: str, yaw: float) -> None:
        """只存不管；未知玩家静默忽略。"""
        p = self.players.get(pid)
        if p is None:
            return
        try:
            value = float(yaw)
        except (TypeError, ValueError):
            return
        if math.isfinite(value):
            p.yaw = value

    # -- 方块 --------------------------------------------------------------

    def place(
        self,
        pid: str,
        x: int,
        y: int,
        z: int,
        mat: int = 0,
    ) -> tuple[bool, str]:
        """放一个方块。校验顺序：玩家 → mat → 整数坐标 → y → (x,z) → 占用 →
        上限 → REACH → y==1 不压玩家身体。

        成功时 version += 1，写一条 ev {"k": "place", ..., "block": [x,y,z,mat]}。
        """
        p = self.players.get(pid)
        if p is None:
            return (False, f"玩家不存在: {pid}")
        if not _is_int(mat) or not (0 <= mat <= 3):
            return (False, "材质必须是 0..3 的整数")
        if not (_is_int(x) and _is_int(y) and _is_int(z)):
            return (False, "方块坐标必须是整数")
        if not (BLOCK_MIN_Y <= y <= BLOCK_MAX_Y):
            return (False, f"高度必须在 {BLOCK_MIN_Y}..{BLOCK_MAX_Y} 之间")
        if not (0 <= x < GRID and 0 <= z < GRID):
            return (False, "方块位置超出房间")
        key = (x, y, z)
        if key in self.blocks:
            return (False, "这个格子已经有方块了")
        if len(self.blocks) >= MAX_BLOCKS:
            return (False, f"方块数量已达上限 {MAX_BLOCKS}")
        dist = self._reach_dist(p, x, z)
        if dist > REACH:
            return (False, f"太远了（最远 {REACH:g}，实际 {dist:.2f}）")
        if y == BLOCK_MIN_Y and self._body_hits_cell(x, z):
            return (False, "这里站着人，不能把地板方块压在他身上")
        self.blocks[key] = mat
        self.version += 1
        self.changes.append((self.version, "block", (x, y, z, mat)))
        self._emit("place", id=p.id, name=p.name, block=[x, y, z, mat])
        return (True, "放置成功")

    def remove(self, pid: str, x: int, y: int, z: int) -> tuple[bool, str]:
        """拆一个方块。校验：玩家 → 整数坐标 → y → (x,z) → 存在 → REACH。

        成功时 version += 1，写一条 ev {"k": "remove", ..., "block": [x,y,z,旧mat]}。
        """
        p = self.players.get(pid)
        if p is None:
            return (False, f"玩家不存在: {pid}")
        if not (_is_int(x) and _is_int(y) and _is_int(z)):
            return (False, "方块坐标必须是整数")
        if not (BLOCK_MIN_Y <= y <= BLOCK_MAX_Y):
            return (False, f"高度必须在 {BLOCK_MIN_Y}..{BLOCK_MAX_Y} 之间")
        if not (0 <= x < GRID and 0 <= z < GRID):
            return (False, "方块位置超出房间")
        key = (x, y, z)
        mat = self.blocks.get(key)
        if mat is None:
            return (False, "这里没有方块")
        dist = self._reach_dist(p, x, z)
        if dist > REACH:
            return (False, f"太远了（最远 {REACH:g}，实际 {dist:.2f}）")
        del self.blocks[key]
        self.version += 1
        self.changes.append((self.version, "del", key))
        self._emit("remove", id=p.id, name=p.name, block=[x, y, z, mat])
        return (True, "拆除成功")

    def block_at(self, x: int, y: int, z: int) -> int | None:
        return self.blocks.get((x, y, z))

    def block_count(self) -> int:
        return len(self.blocks)

    # -- 感知 / 增量 -------------------------------------------------------

    def update_interest(self, pid: str) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        """按「进 1 出 2」更新这个玩家的 AOI，返回 (entered, exited)。

        进入的 chunk 里的现存方块、以及该玩家客户端 known 里可能已经被拆掉的方块，
        都塞进 _pending_enter_blocks，等下一次 patch 补发 / 对账。
        重复调用是幂等的（relay 每 tick 都会调一次）。
        """
        p = self._require(pid)
        here = chunk_of(p.pos[0], p.pos[1])
        keep = {c for c in p.interest if chunk_dist(c, here) <= CHUNK_EXIT}
        entered = {
            (cx, cz)
            for cx in range(CHUNKS)
            for cz in range(CHUNKS)
            if chunk_dist((cx, cz), here) <= CHUNK_ENTER and (cx, cz) not in p.interest
        }
        exited = p.interest - keep
        p.interest = keep | entered
        if entered:
            pending = self._pending_enter_blocks.setdefault(pid, set())
            for key in self.blocks:
                if chunk_of(key[0], key[2]) in entered:
                    pending.add(key)
            # 客户端以前见过、现在可能已经被拆掉的方块也要对账（否则会画幽灵方块）
            for key in p.known:
                if chunk_of(key[0], key[2]) in entered:
                    pending.add(key)
        return (set(entered), set(exited))

    def snapshot(self) -> dict:
        """整个房间的全量快照（进房 welcome 用）。"""
        return {
            "room": self.room,
            "version": self.version,
            "blocks": [
                [x, y, z, self.blocks[(x, y, z)]]
                for (x, y, z) in sorted(self.blocks)
            ],
            "players": [
                {
                    "id": q.id,
                    "name": q.name,
                    "pos": [q.pos[0], q.pos[1]],
                    "yaw": q.yaw,
                }
                for q in sorted(self.players.values(), key=lambda q: q.id)
            ],
        }

    def patch(self, pid: str) -> dict:
        """这个玩家 AOI 内的方块增量。

        做法：self.changes 里版本号 > sent_version 的条目按 chunk AOI 过滤，再加上
        _pending_enter_blocks 里该玩家的补发键；用 self.blocks 的**当前真值**解析成
        set / del（同一格被"拆了又放"多次也只会给最终状态）。
        返回 {"version", "set": [[x,y,z,mat],...], "del": [[x,y,z],...]}，
        并把 player.sent_version 推到 self.version。
        """
        p = self._require(pid)
        touched: set[tuple[int, int, int]] = set()
        for ver, _kind, payload in self.changes:
            if ver <= p.sent_version:
                continue
            if _kind == "block":
                key = (payload[0], payload[1], payload[2])
            else:
                key = (payload[0], payload[1], payload[2])
            if chunk_of(key[0], key[2]) in p.interest:
                touched.add(key)
        for key in self._pending_enter_blocks.pop(pid, set()):
            if chunk_of(key[0], key[2]) in p.interest:
                touched.add(key)
        set_out: list[list[int]] = []
        del_out: list[list[int]] = []
        for key in sorted(touched):
            mat = self.blocks.get(key)
            if mat is None:
                if key in p.known:
                    del p.known[key]
                    del_out.append([key[0], key[1], key[2]])
            elif p.known.get(key) != mat:
                p.known[key] = mat
                set_out.append([key[0], key[1], key[2], mat])
        p.sent_version = self.version
        self._prune_changes()
        return {"version": self.version, "set": set_out, "del": del_out}

    def nearby(self, pid: str) -> list[dict]:
        """AOI 内的其他玩家（不含自己），按实际距离从近到远。"""
        p = self._require(pid)
        out: list[dict] = []
        for q in self.players.values():
            if q.id == pid:
                continue
            if chunk_of(q.pos[0], q.pos[1]) in p.interest:
                out.append(
                    {
                        "id": q.id,
                        "name": q.name,
                        "pos": [q.pos[0], q.pos[1]],
                        "yaw": q.yaw,
                    }
                )
        out.sort(key=lambda d: (math.dist(p.pos, d["pos"]), d["id"]))
        return out

    def _event_in_interest(self, p: Player, ev: dict) -> bool:
        """事件按 AOI 过滤：方块事件看方块所在 chunk，玩家事件看发事人的位置。"""
        kind = ev.get("k")
        if kind in ("place", "remove"):
            block = ev.get("block")
            if isinstance(block, (list, tuple)) and len(block) >= 3:
                return chunk_of(block[0], block[2]) in p.interest
            return False
        pos = ev.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) >= 2:
            return chunk_of(pos[0], pos[1]) in p.interest
        return True  # 没有位置的事件（客户端自定义）就全发

    def events_since(self, pid: str) -> list[dict]:
        """这个玩家 AOI 内、自上次取之后新增的事件。

        事件 v 是独立事件序号；取完后把 last_event_version 推到当前序号。
        AOI 外的事件被跳过但同样记为"已取"，所以走近之后不会补放旧聊天。
        """
        p = self._require(pid)
        out: list[dict] = []
        for ev in self.events:
            if ev.get("v", 0) <= p.last_event_version:
                continue
            if self._event_in_interest(p, ev):
                out.append(dict(ev))
        p.last_event_version = self._event_seq
        return out

    def say(self, pid: str, text) -> dict:
        """说话：产生一条 said 事件（events_since 的 said 生产者）。

        只是事件流水；怎么广播、要不要限频由中继决定。
        """
        p = self._require(pid)
        clean = str(text if text is not None else "")
        return self._emit(
            "said",
            id=p.id,
            name=p.name,
            pos=[p.pos[0], p.pos[1]],
            text=clean,
        )

    def queued_position(self) -> int | None:
        """本模型不做排队（满员直接拒绝，由中继层决定排队策略），永远返回 None。"""
        return None
