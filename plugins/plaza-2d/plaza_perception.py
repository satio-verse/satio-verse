"""2D 广场的感知门: 中继的原始帧 (握着所有人的位置) -> 她此刻感知得到的事件. 样板, 主程序不引用.

这是"真值"到"认知"的唯一一道门 (记忆 cognition-truth-split; 契约 perception.visible / hidden):
  - 视野半径 SIGHT、听力半径 HEAR, 用切比雪夫距离 (格子上 max(|dx|, |dy|));
  - 她只知道: 谁 (名字)、大概多远 (near / mid / far)、在哪一边 (left / right / up / down / here)、说了什么;
  - 绝不交出: 别人的坐标、成员编号、房间名单、房间人数、视野外的任何人和任何动静.

纯函数的类, 不碰网络、不读文件, 所以双胞胎夹具可以离线跑. 一个 Perception 对应一只宠物在一个房间里.
"""
from __future__ import annotations

SIGHT = 6
HEAR = 4
NEAR, MID = 2, 4


def dist(a, b) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def bucket(d: int) -> str:
    return "near" if d <= NEAR else "mid" if d <= MID else "far"


def direction(me, other) -> str:
    dx, dy = other[0] - me[0], other[1] - me[1]
    if dx == 0 and dy == 0:
        return "here"
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def event(type_: str, text: str = "", **fields) -> dict:
    return {"type": type_, "fields": fields, "text": text}


class Perception:
    SIGHT = SIGHT
    HEAR = HEAR

    def __init__(self):
        self.me_id = ""
        self.me_name = ""
        self.pos = (0, 0)
        self.code = ""
        self.grid = {"w": 24, "h": 14}
        self.members: dict[str, dict] = {}   # 真值: 编号 -> {name, pos}
        self.seen: dict[str, tuple] = {}     # 她现在看得见的: 编号 -> (距离档, 方向, 名字)

    # -- 帧进来 ----------------------------------------------------------------
    def feed(self, frame: dict) -> list[dict]:
        """一帧中继下行帧 -> 她感知得到的事件 (可能是空的)."""
        t = frame.get("t")
        if t == "joined":
            return self._joined(frame)
        if t == "ack":
            if frame.get("ok") and isinstance(frame.get("pos"), list):
                self.pos = (int(frame["pos"][0]), int(frame["pos"][1]))
                return self._revisit()
            return []
        if t != "ev":
            return []
        k, mid = frame.get("k"), frame.get("id")
        if k == "left":
            self.members.pop(mid, None)
            return self._refresh(mid)
        pos = tuple(frame.get("pos") or (0, 0))
        self.members[mid] = {"name": str(frame.get("name") or ""), "pos": pos}
        out = self._refresh(mid)
        if k == "said" and mid in self.members and dist(self.pos, pos) <= self.HEAR:
            out.append(event("pet_said", str(frame.get("text") or ""), **self._where(mid)))
        elif k == "emote" and mid in self.seen:
            out.append(event("pet_emoted", pet_name=self.members[mid]["name"], emote=str(frame.get("emote") or "")))
        return out

    def _joined(self, f: dict) -> list[dict]:
        you = f.get("you") or {}
        self.me_id, self.me_name = you.get("id", ""), you.get("name", "")
        self.pos = tuple(you.get("pos") or (0, 0))
        self.code = f.get("code", "")
        self.grid = f.get("grid") or self.grid
        self.members = {m["id"]: {"name": m["name"], "pos": tuple(m["pos"])} for m in f.get("members", [])}
        return self._revisit()  # 重连时 self.seen 还留着: 和新快照比一比, 谁不见了、谁新来了

    # -- 看得见谁 --------------------------------------------------------------
    def _where(self, mid: str) -> dict:
        m = self.members[mid]
        return {"pet_name": m["name"], "distance_bucket": bucket(dist(self.pos, m["pos"])),
                "direction": direction(self.pos, m["pos"])}

    def _refresh(self, mid: str) -> list[dict]:
        """某个人的位置 / 在不在变了: 她看得见的情况有没有变."""
        was = self.seen.get(mid)
        m = self.members.get(mid)
        if m is None or dist(self.pos, m["pos"]) > self.SIGHT:
            if was is None:
                return []
            del self.seen[mid]
            return [event("pet_left", pet_name=was[2])]
        now = (bucket(dist(self.pos, m["pos"])), direction(self.pos, m["pos"]), m["name"])
        self.seen[mid] = now
        if was is None:
            return [event("pet_entered", **self._where(mid))]
        if was[:2] != now[:2]:
            return [event("pet_moved", **self._where(mid))]
        return []

    def _revisit(self) -> list[dict]:
        out: list[dict] = []
        for mid in sorted({*self.members, *self.seen}):
            out += self._refresh(mid)
        return out

    # -- 给宿主 / 面板 ---------------------------------------------------------
    def own_position(self) -> str:
        return f"{self.pos[0]},{self.pos[1]}"

    def look(self) -> dict:
        """plaza_look 的返回: 附近有谁、离多远. 只有她看得见的."""
        pets = [self._where(mid) for mid in self.seen if mid in self.members]
        pets.sort(key=lambda p: (("near", "mid", "far").index(p["distance_bucket"]), p["pet_name"]))
        return {"own_position": self.own_position(), "pets": pets}

    def panel(self) -> dict:
        """旁观面板用的全部真值 (主人看得比她多), 每个人带 seen 标记. 只给面板, 不给她."""
        return {"code": self.code, "grid": self.grid, "sight": self.SIGHT, "hear": self.HEAR,
                "me": {"name": self.me_name, "pos": list(self.pos)},
                "members": [{"name": m["name"], "pos": list(m["pos"]), "seen": mid in self.seen}
                            for mid, m in sorted(self.members.items())]}
