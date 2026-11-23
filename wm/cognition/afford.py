"""她对小窝的了解 (可供性的发现簿, 服务端这份是准的): 看见过哪些东西、看到的粗档、看见过的那几摊、去过哪、每件事在什么情境下
试过几次、觉得怎么样、哪一件喜不喜欢、够不着多高、哪件走不到跟前; 单件东西 (哪本书、手机) 最后看见在哪 (认识"书"不等于
知道这一本在哪); 每件事是哪个动词 (同一个动词的经验能迁移, 页面算). 和页面的 desktop/web/room-known.js 是同一套 (那边以前存在
本机, 卸载会删; 现在以这里为准, 下行 room_known 发给页面).

- 只收页面报的她感知到的: 编号、固定词表的粗档、她自己需求的变化 (数字). 书好不好看、土里还有多少水这些环境真值不进来.
- 页面先声明一次有哪些东西、哪些地方、哪些事 (affords), 只认声明过的编号.
- 时刻用服务端的钟; 下行给"多久以前" (秒), 页面按自己的钟换算 (小屋快进时两边的钟不一样).
- 变化追加写在 afford-events.jsonl (和认知的事件流放一起, ~/.worldmodel/experience), 重启时重放. 天天看见的不每次都记:
  东西只记第一次看见, 粗档只记变了的, 去过哪隔 10 分钟以上才记.
- 不 import 环境 (测试查).
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

NEEDS = ("energy", "food", "water", "fun", "social")
LEVELS = ("dry", "ok", "wet", "on", "off")      # 看到的粗档: 花土干 / 正好 / 湿, 灯开 / 关
KINDS = ("coffee", "water")                    # 看见的那一摊是什么
ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
CTX = re.compile(r"^[a-z][a-z\-]{0,23}$")      # 情境 (页面的 situation: liked / new / dry / night-off / lonely / stale ...); 动词也用它
WINDOW = 5          # 经验按最近几次平均 (和页面一样: 书看完了、花浇多了, 东西会变)
LIKE_WINDOW = 3
VISIT_LOG_S = 600   # 去过哪、单件东西在原处又看见一次: 隔这么久才记一笔
SPILLS = 8
LIMIT = 256         # 声明的东西、节点、地方、事最多几个


def _num(x, lo: float, hi: float) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return max(lo, min(hi, float(x)))
    return None


def _r1(x: float) -> float:
    """粗到 0.1 (和页面的 Math.round(x * 10) / 10 一样: .5 往大的取)."""
    return math.floor(x * 10 + 0.5) / 10


def _round(g: dict) -> dict:
    return {k: round(v, 1) for k, v in g.items() if abs(v) >= 0.05}


class AffordMind:
    def __init__(self, path: str | Path | None = None, clock=time.time, scene_id=None):
        self.scene_id = scene_id
        self.path = Path(path) if path else None
        self.clock = clock
        self.lock = threading.Lock()
        self.spec = {"things": {}, "spots": set(), "acts": set()}  # 声明过的: 东西 -> 节点, 地方, 事
        self.things: dict[str, dict] = {}   # 东西 -> {first, last, n}
        self.states: dict[str, dict] = {}   # 东西 -> {level, at}
        self.spills: list[dict] = []        # 看见过的一摊 {id, kind, at: [x, z], y, seen}
        self.tries: dict[str, dict] = {}    # 事 -> 情境 -> {n, ok, v: {需求: 平均}, last}
        self.skips: dict[str, dict] = {}    # 事 -> 情境 -> 走到跟前看清了、按经验没做的次数
        self.likes: dict[str, dict] = {}    # 单件 (哪本书) -> {n, v}
        self.visits: dict[str, float] = {}
        self.high: float | None = None      # 够不着的最低高度 (米)
        self.stuck: dict[str, float] = {}   # 走不到跟前的那件 -> 什么时候
        self.where: dict[str, dict] = {}    # 单件 -> {at: [x, y, z] 最后看见在哪 (0.1 m), seen}
        self.verbs: dict[str, str] = {}     # 事 -> 动词
        self.loaded = 0
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    op = json.loads(line)
                    if self.scene_id is not None and op.get("scene_id") != self.scene_id:
                        raise ValueError("可供性经验的场景归属不一致")
                    self._apply(op)
                    self.loaded += 1

    # -- 声明 -----------------------------------------------------------------------------------------
    def declare(self, decl) -> None:
        """页面声明的 (affords): {things: {东西: [节点]}, spots: [地方], acts: [事]}. 只收合格的编号."""
        if not isinstance(decl, dict):
            return
        with self.lock:
            things = decl.get("things") if isinstance(decl.get("things"), dict) else {}
            for k, nodes in list(things.items())[:LIMIT]:
                if ID.match(str(k)):
                    self.spec["things"][k] = {n for n in (nodes if isinstance(nodes, list) else [])[:LIMIT] if isinstance(n, str) and ID.match(n)}
            for key in ("spots", "acts"):
                vals = decl.get(key) if isinstance(decl.get(key), list) else []
                self.spec[key] |= {v for v in vals[:LIMIT] if isinstance(v, str) and ID.match(v)}

    def _thing(self, x) -> bool:
        return isinstance(x, str) and x in self.spec["things"]

    def _node(self, x) -> bool:
        return isinstance(x, str) and any(x in nodes for nodes in self.spec["things"].values())

    # -- 记一笔 / 重放 ----------------------------------------------------------------------------------
    def _log(self, op: dict) -> None:
        op = {"t": round(self.clock(), 1), **op, **({"scene_id": self.scene_id} if self.scene_id is not None else {})}
        self._apply(op)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(op, ensure_ascii=False, sort_keys=True) + "\n")

    def _apply(self, op: dict) -> None:
        t, kind = op["t"], op["op"]
        if kind == "saw":
            self.things.setdefault(op["id"], {"first": t, "last": t, "n": 0})
            k = self.things[op["id"]]
            k["last"], k["n"] = t, k["n"] + 1
        elif kind == "see":
            self.states[op["id"]] = {"level": op["level"], "at": t}
        elif kind == "spill":
            if not any(s["id"] == op["id"] for s in self.spills):
                self.spills = (self.spills + [{"id": op["id"], "kind": op["kind"], "at": op["at"], "y": op["y"], "seen": t}])[-SPILLS:]
        elif kind == "forget":
            self.spills = [s for s in self.spills if s["id"] != op["id"]]
        elif kind == "visit":
            self.visits[op["spot"]] = t
        elif kind == "tried":
            k = self.tries.setdefault(op["act"], {}).setdefault(op["ctx"], {"n": 0, "ok": 0, "v": {}})
            obs = dict(op.get("felt") or {})
            if not op["ok"]:
                obs["fun"] = obs.get("fun", 0) - 2  # 没做成: 有点扫兴 (和页面一样)
            if op.get("hurt"):
                obs["fun"] = obs.get("fun", 0) - op["hurt"]
            k["n"] += 1
            k["ok"] += 1 if op["ok"] else 0
            w = min(k["n"], WINDOW)
            k["v"] = _round({key: k["v"].get(key, 0) + (obs.get(key, 0) - k["v"].get(key, 0)) / w for key in set(k["v"]) | set(obs)})
            k["last"] = t
        elif kind == "skip":
            d = self.skips.setdefault(op["act"], {})
            d[op["ctx"]] = d.get(op["ctx"], 0) + 1
        elif kind == "liked":
            k = self.likes.setdefault(op["node"], {"n": 0, "v": 0.0})
            k["n"] += 1
            k["v"] = round(k["v"] + (op["fun"] - k["v"]) / min(k["n"], LIKE_WINDOW), 1)
        elif kind == "high":
            self.high = op["y"] if self.high is None else min(self.high, op["y"])
        elif kind == "stuck":
            self.stuck[op["node"]] = t
        elif kind == "where":
            self.where[op["node"]] = {"at": op["at"], "seen": t}
        elif kind == "lost":
            self.where.pop(op["node"], None)
        elif kind == "verb":
            self.verbs[op["act"]] = op["verb"]

    # -- 页面报的 ---------------------------------------------------------------------------------------
    def observe(self, msg: dict) -> list[str]:
        """room_observe 里的这几项: things [{id, state?}] 这一刻看得见的东西 (粗档只在看得出时带), puddles [{id, kind, at, y}]
        这一刻看得见的一摊 (粗位置), gone [一摊的 id] 走到记得的那一处、看见那一摊不在了 (干了、被谁擦了; 不算她擦的, 不进经验),
        visited [地方], nodes [{id, at: [x, y, z]}] 这一刻看得见的单件和它在哪 (哪本书、手机; 0.1 m), lost [单件] 走到记得的地方
        没看见它 (忘掉在哪). 返回第一次看见的东西."""
        news = []
        with self.lock:
            now = self.clock()
            for th in (msg.get("things") if isinstance(msg.get("things"), list) else [])[:LIMIT]:
                tid = th.get("id") if isinstance(th, dict) else th
                if not self._thing(tid):
                    continue
                if tid not in self.things:
                    self._log({"op": "saw", "id": tid})
                    news.append(tid)
                else:
                    k = self.things[tid]
                    k["last"], k["n"] = now, k["n"] + 1  # 天天看见的不每次都记
                level = th.get("state") if isinstance(th, dict) else None
                if level in LEVELS and (self.states.get(tid) or {}).get("level") != level:
                    self._log({"op": "see", "id": tid, "level": level})
                elif level in LEVELS:
                    self.states[tid]["at"] = now
            for p in (msg.get("puddles") if isinstance(msg.get("puddles"), list) else [])[:SPILLS]:
                if not isinstance(p, dict) or not isinstance(p.get("id"), (str, int)) or not ID.match(str(p["id"])):
                    continue
                at = p.get("at") if isinstance(p.get("at"), list) and len(p["at"]) == 2 else None
                xz = [_num(v, -50, 50) for v in at] if at else None
                if not xz or None in xz or any(s["id"] == str(p["id"]) for s in self.spills):
                    continue
                y = _num(p.get("y"), 0, 3) or 0.0
                self._log({"op": "spill", "id": str(p["id"]), "kind": p.get("kind") if p.get("kind") in KINDS else "water",
                           "at": [_r1(v) for v in xz], "y": _r1(y)})
            for th in (msg.get("nodes") if isinstance(msg.get("nodes"), list) else [])[:LIMIT]:  # 这一刻看得见的单件 (在哪)
                at = th.get("at") if isinstance(th, dict) and isinstance(th.get("at"), list) and len(th["at"]) == 3 else None
                xyz = [_num(v, -50, 50) for v in at] if at else None
                if not self._node(th.get("id") if isinstance(th, dict) else None) or not xyz or None in xyz:
                    continue
                xyz, old = [_r1(v) for v in xyz], self.where.get(th["id"])
                if old is None or old["at"] != xyz or now - old["seen"] > VISIT_LOG_S:
                    self._log({"op": "where", "node": th["id"], "at": xyz})
                else:
                    old["seen"] = now
            for node in (msg.get("lost") if isinstance(msg.get("lost"), list) else [])[:LIMIT]:  # 走到记得的地方, 没看见它
                if node in self.where:
                    self._log({"op": "lost", "node": node})
            for pid in (msg.get("gone") if isinstance(msg.get("gone"), list) else [])[:SPILLS]:
                if any(s["id"] == str(pid) for s in self.spills):
                    self._log({"op": "forget", "id": str(pid), "why": "gone"})
            for spot in (msg.get("visited") if isinstance(msg.get("visited"), list) else [])[:LIMIT]:
                if isinstance(spot, str) and spot in self.spec["spots"]:
                    if now - self.visits.get(spot, -1e18) > VISIT_LOG_S:
                        self._log({"op": "visit", "spot": spot})
                    else:
                        self.visits[spot] = now
        return news

    def done(self, msg: dict) -> dict | None:
        """做完一件 (room_afford_done): {act, ctx, verb?, ok, felt: {需求: 变化}, hurt, skip, node?, liked?: 开心多少,
        high?: 够不着的高度, stuck?: 走不到的那件 (或几件), forget?: [擦掉了 / 已经干了的那一摊]}. 返回这一趟记成什么; 不认识的事返回 None."""
        act, ctx = msg.get("act"), msg.get("ctx")
        if act not in self.spec["acts"] or not (isinstance(ctx, str) and CTX.match(ctx)):
            return None
        with self.lock:
            verb = msg.get("verb")
            if isinstance(verb, str) and CTX.match(verb) and self.verbs.get(act) != verb:
                self._log({"op": "verb", "act": act, "verb": verb})
            if msg.get("skip") is True:  # 走到跟前看清了, 按经验没做
                self._log({"op": "skip", "act": act, "ctx": ctx})
            else:
                felt = msg.get("felt") if isinstance(msg.get("felt"), dict) else {}
                felt = {k: round(v, 1) for k, v in ((k, _num(felt.get(k), -50, 50)) for k in NEEDS) if v is not None and abs(v) >= 0.05}
                self._log({"op": "tried", "act": act, "ctx": ctx, "ok": msg.get("ok") is not False, "felt": felt,
                           "hurt": _num(msg.get("hurt"), 0, 20) or 0})
            node = msg.get("node")
            fun = _num(msg.get("liked"), -20, 20)
            if fun is not None and self._node(node):
                self._log({"op": "liked", "node": node, "fun": round(fun, 1)})
            y = _num(msg.get("high"), 0.5, 3)
            if y is not None:
                self._log({"op": "high", "y": round(y, 2)})
            stuck = msg.get("stuck")  # 一件, 或者看得出在同一个角落、一起拿不到的几件
            for node in ([stuck] if isinstance(stuck, str) else stuck if isinstance(stuck, list) else [])[:LIMIT]:
                if self._node(node) or self._thing(node):
                    self._log({"op": "stuck", "node": node})
            for pid in (msg.get("forget") if isinstance(msg.get("forget"), list) else [])[:SPILLS]:
                if any(s["id"] == str(pid) for s in self.spills):
                    self._log({"op": "forget", "id": str(pid), "why": "done"})
            k = (self.tries.get(act) or {}).get(ctx) or {}
            return {"act": act, "ctx": ctx, "n": k.get("n", 0), "v": k.get("v", {}), "skips": (self.skips.get(act) or {}).get(ctx, 0)}

    # -- 给页面、给提示词 --------------------------------------------------------------------------------
    def state(self) -> dict:
        """下行 room_known: 页面的 room-known.js 那一套, 时刻换成"多久以前" (秒)."""
        with self.lock:
            now = self.clock()
            ago = lambda t: round(now - t, 1)  # noqa: E731
            return {"things": {k: {"n": v["n"], "first_ago": ago(v["first"]), "last_ago": ago(v["last"])} for k, v in self.things.items()},
                    "states": {k: {"level": v["level"], "ago": ago(v["at"])} for k, v in self.states.items()},
                    "spills": [{**{k: v for k, v in s.items() if k != "seen"}, "ago": ago(s["seen"])} for s in self.spills],
                    "tries": {a: {c: {"n": k["n"], "ok": k["ok"], "v": k["v"], "ago": ago(k["last"])} for c, k in d.items()}
                              for a, d in self.tries.items()},
                    "skips": {a: dict(d) for a, d in self.skips.items()}, "likes": {k: dict(v) for k, v in self.likes.items()},
                    "visits": {k: ago(t) for k, t in self.visits.items()}, "high": self.high,
                    "stuck": {k: ago(t) for k, t in self.stuck.items()},
                    "where": {k: {"at": v["at"], "ago": ago(v["seen"])} for k, v in self.where.items()}, "verbs": dict(self.verbs)}

    def known(self) -> list[dict]:
        """给 room_plan 的提示词: 每件事一共试过几次、每个情境下觉得怎么样 (只有编号和数字)."""
        with self.lock:
            return [{"act": a, "n": sum(k["n"] for k in d.values()),
                     "ctx": {c: {"n": k["n"], "v": k["v"]} for c, k in d.items()}} for a, d in sorted(self.tries.items())]

    def close(self) -> None:
        """不再写盘 (删她的经历时: 之后排队进来的这个实例的写都丢掉, 不会把删掉的文件又建起来)."""
        with self.lock:
            self.path = None

    def status(self) -> dict:
        with self.lock:
            return {"things": len(self.things), "tries": sum(k["n"] for d in self.tries.values() for k in d.values()),
                    "spills": len(self.spills)}
