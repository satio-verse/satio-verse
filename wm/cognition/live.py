"""服务端常驻的小窝认知 (阶段 2). 桌宠小屋页面是环境, 只上报她这一刻看得到什么 (上行 room_observe);
她以为东西在哪、找东西的下一步 (上行 room_search -> 下行 room_step)、被问"杯子在哪"时说什么, 都在这里算, 只凭收到的观察.

- 页面发来的只认固定编号 (layout.py 的几处、放东西的地方、杯子) 和固定的动作词, 别的丢掉; 时刻用服务端的钟.
  页面可以声明这个小屋有哪些放东西的地方 (regions; 桌宠小屋还没有抽屉), 找东西只往那些地方找.
- 事件流追加写在 <trace.log_dir>/cognition/room-events.jsonl; 服务端重启时整串重放, 认知和经验都回来.
- 经验攒够 (同一种东西不见时, 在同一处找到 learn_after 次) 时调 on_learn 写一句 (服务端接到「我的理解」).
- 每一处的说法都是服务端按词表拼的, 不用页面的字符串.
- 端东西 (carry.py 的 CarryMind): 页面端起杯子之前问一次 (room_carry -> room_carry_plan: 这一趟怎么端、为什么), 端完报一次
  (room_carry_done). 中间每一段感知到的 (room_observe 的 events: 洒了多大一股、哪里烫了) 记进这一趟, 端完变成一条经验,
  追加写在 carry-events.jsonl, 重启时读回来. 喝的时候烫嘴不算端东西的后果 (咖啡本来就烫, 不是洒出来的).
- 可供性的发现簿 (afford.py): room_observe 里的 affords (声明) / things / puddles / visited, 做完一件报 room_afford_done;
  下行 room_known 是她现在的了解 (页面按它挑下一件事). 追加写在 afford-events.jsonl.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from . import layout as Y
from .afford import AffordMind
from .belief import Cognition
from .carry import CHOICE_ZH, DEFAULT, MAG, STAGE_ZH, CarryMind
from .carry import STAGES as CARRY_STAGES
from .events import EventLog
from .mind import Mind

ACTS = ("go", "open", "close", "pick", "put", "look", "wait")
# 感知门的固定词表 (2026-10-27; 和小屋液体 desktop/web/room-liquid.js 的 perceive()、体感 room-body.js 的 feelings() 同一套):
# 液面粗档、身体部位 (web/physics/body-hit.js 的 PART_NAMES, 加上体感那边的部位)、感觉、在哪一段、洒了多大一股
FILLS = ("empty", "low", "half", "full")
PARTS = ("右手", "左手", "肚子下面", "肚子", "胸口", "左腿", "右腿", "左小腿", "右小腿", "左脚", "右脚", "嘴",
         "膝盖", "小腿", "脚", "屁股", "身子", "头", "胳膊肘", "腿", "身上")
# 湿了 / 烫一下 / 烫伤 / 摔疼了 / 抓着的东西滑了一下 / 撞了一下 (刚体 room-rigid.js) / 手抖了一下 (动作本身的毛病, 动作成长那边报)
FELT = ("wet", "hot", "burn", "bruise", "slip", "bump", "jerk")
STAGES = ("pick", "lift", "walk", "put", "drink", "other")
AMOUNTS = ("drops", "little", "some", "stream", "splash")
STAGE_SAY = {"pick": "拿起", "lift": "拿到身前", "walk": "端着走", "put": "放下", "drink": "喝", "other": "端着"}
AMOUNT_SAY = {"drops": "几滴", "little": "一点", "some": "一些", "stream": "一大股", "splash": "一大片"}
FELT_SAY = {"wet": "弄湿了", "hot": "被烫了一下", "burn": "烫伤了, 还在疼", "bruise": "摔疼了", "slip": "抓着的东西滑了一下",
            "bump": "撞了一下", "jerk": "抖了一下"}
# 刚体那边过了感知门的 (room-rigid.js 的 perceive): 手里的东西滑了 / 脱手了 / 看见东西掉了、倒了 / 只听见响. 响声只分三档
HANDS = {"left": "左手", "right": "右手"}
LOUD = ("soft", "clack", "bang")
LOUD_SAY = {"soft": "轻轻的一声", "clack": "啪的一声", "bang": "咚的一声"}
FAVORITE = "IR_Item_Espresso_Pink"  # 她最喜欢的杯子: 聊天里只说"杯子"时指它
# 聊天里怎么叫这几个杯子 (先长后短)
ALIASES = [("粉色马克杯", "IR_Item_Mug_1"), ("雾粉", "IR_Item_Mug_1"), ("奶油白", "IR_Item_Mug_0"), ("马克杯", "IR_Item_Mug_0"),
           ("白色", "IR_Item_Espresso_White"), ("白杯", "IR_Item_Espresso_White"), ("粉色", FAVORITE), ("粉杯", FAVORITE),
           ("咖啡杯", FAVORITE), ("杯子", FAVORITE)]
_WHERE = re.compile(r"在哪|哪儿|哪里|哪去了|放哪|去哪|找到.{0,3}吗|不见了|丢了")
_SHORT_WHERE = re.compile(r"^.{0,8}呢[?？]?$")  # "杯子呢"
LEARN_AFTER = 2
JERK_QUIET_S = 120  # 手抖了一下 (动作成长那边报, 两分钟能有十几次): 没跟着洒、烫的, 这么久只说一次
# 小屋里的阶段 -> 端东西的四段: 送到嘴边 (drink) 算挪到身前 (2026-10-27 小屋自检: 洒在拿起和刚往嘴边送的那一下)
ROOM_STAGE = {"pick": "pick", "lift": "lift", "drink": "lift", "walk": "walk", "put": "put"}
ROUTES = ("none", "short", "turns")


def cup_kind(item: str) -> str:
    return "espresso" if "Espresso" in item else "mug"


def _act(a, regions, layout=Y.LEGACY) -> list | None:
    """页面报的她刚做的动作: 只认固定的动作词和编号."""
    if not isinstance(a, list) or not a or a[0] not in ACTS:
        return None
    do, args = a[0], a[1:]
    if do == "go" and len(args) == 1 and args[0] in layout.ZONES:
        return [do, args[0]]
    if do in ("open", "close") and len(args) == 1 and args[0] in regions and layout.is_box(args[0]):
        return [do, args[0]]
    if do == "pick" and len(args) == 1 and args[0] in layout.ITEMS:
        return [do, args[0]]
    if do == "put" and len(args) == 2 and args[0] in layout.ITEMS and args[1] in regions:
        return [do, args[0], args[1]]
    if do == "look" and not args:
        return [do]
    if do == "wait" and len(args) == 1 and isinstance(args[0], (int, float)) and not isinstance(args[0], bool):
        return [do, max(0.0, min(86400.0, float(args[0])))]
    return None


def _event(ev, regions, layout=Y.LEGACY) -> dict | None:
    """一件她看到、感觉到的事: 只认固定的种类和词表, 其余丢掉."""
    if not isinstance(ev, dict):
        return None
    kind, stage = ev.get("kind"), ev.get("stage") if ev.get("stage") in STAGES else "other"
    if kind == "moved" and ev.get("item") in layout.ITEMS:
        return {"kind": "moved", "item": ev["item"], "from": ev.get("from") if ev.get("from") in regions else None,
                "to": ev.get("to") if ev.get("to") in regions else None, "by": "你"}
    if kind == "felt" and ev.get("part") in PARTS and ev.get("level") in FELT:
        return {"kind": "felt", "part": ev["part"], "level": ev["level"], "stage": stage}
    if kind == "spill" and (ev.get("item") in layout.ITEMS or ev.get("item") == "machine") and ev.get("amount") in AMOUNTS:
        # 洒了: 她碰到了 (touched) 或者看得到落点 (seen) 页面才报; 落在哪只认几处、地上、咖啡机, 别的台面都算"台面"
        where = ev.get("where")
        where = where if where in regions or where in ("floor", "machine") else "surface"
        return {"kind": "spill", "item": ev["item"], "amount": ev["amount"], "where": where, "stage": stage,
                "touched": ev.get("touched") is True, "seen": ev.get("seen") is True}
    if kind == "empty_sip" and ev.get("item") in layout.ITEMS:
        return {"kind": "empty_sip", "item": ev["item"]}  # 喝的时候喝不到: 杯子是空的
    thing = ev.get("item") if ev.get("item") in layout.ITEMS else "thing"  # 杯子以外的东西 (书、球...) 只知道是"东西"
    if kind in ("slip", "drop") and ev.get("hand") in HANDS:
        return {"kind": kind, "item": thing, "hand": ev["hand"], "stage": stage}
    if kind == "fall" and ev.get("where") in ("floor", "surface") and ev.get("loud") in LOUD:
        return {"kind": "fall", "item": thing, "where": ev["where"], "toppled": ev.get("toppled") is True, "loud": ev["loud"]}
    if kind == "noise" and ev.get("loud") in LOUD:
        return {"kind": "noise", "loud": ev["loud"]}  # 没看见, 只听见响: 不知道是哪件
    return None


def sanitize(msg: dict, regions, t: float, layout=Y.LEGACY) -> dict | None:
    """上行 room_observe -> 一份观察 (roomworld.observe 的格式). 不在任何一处 (走在路上) 时只收看到、感觉到的事 (events),
    没有事就返回 None; 格式不对也返回 None."""
    lst = lambda k: msg.get(k) if isinstance(msg.get(k), list) else []  # noqa: E731
    events = [e for e in (_event(ev, regions, layout) for ev in lst("events")) if e]
    at = msg.get("at")
    if at not in layout.ZONES:
        if not events:
            return None
        at = None
    raw = msg.get("seen") if isinstance(msg.get("seen"), dict) and at else {}
    seen = {r: sorted({i for i in items if i in layout.ITEMS}) for r, items in raw.items()
            if r in regions and isinstance(items, list)}
    obs = {"world": "room", "t": round(t, 1), "at": at, "seen": seen,
           "hand": sorted({i for i in lst("hand") if i in layout.ITEMS}),
           "open": sorted({r for r in lst("open") if r in seen and layout.is_box(r)})}
    visible = {i for items in seen.values() for i in items} | set(obs["hand"])
    fill = msg.get("fill") if isinstance(msg.get("fill"), dict) else {}
    fill = {i: b for i, b in fill.items() if i in visible and b in FILLS}  # 只收看得到杯口的
    if fill:
        obs["fill"] = dict(sorted(fill.items()))
    if events:
        obs["events"] = events
    act = _act(msg.get("act"), regions, layout)
    if act:
        res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        obs["act"], obs["result"] = act, {"do": act[0], "ok": res.get("ok") is not False, "why": ""}
    return obs


def say_for(action: list, layout=Y.LEGACY) -> str:
    """找东西这一步, 状态栏写什么 (按词表拼)."""
    do = action[0]
    if do == "go":
        return f"她去{layout.zone_name(action[1])}找找"
    if do == "open":
        return f"她打开{layout.region_name(action[1]).removesuffix('里')}看看"
    if do == "close":
        return f"她关上{layout.region_name(action[1]).removesuffix('里')}"
    if do == "pick":
        return f"找到{layout.item_name(action[1])}了!"
    if do == "ask":
        return f"你看到我的{layout.item_name(action[1])}了吗?"
    if do == "give_up":
        return "找不着…"
    return "她仔细看了看"


class LiveRoom:
    def __init__(self, path: str | Path | None = None, policy: str = "experience", learn_after: int = LEARN_AFTER,
                 on_learn=None, clock=time.time, carry_path: str | Path | None = None, afford_path: str | Path | None = None,
                 layout=None, generation_dir: str | Path | None = None):
        self.layout = layout or Y.LEGACY
        self.scene_id = self.layout.scene_id
        self.world = "room" if self.scene_id == Y.DEFAULT_SCENE_ID else "room:" + self.scene_id
        self.path = Path(path) if path else None
        self.clock = clock
        self.lock = threading.Lock()
        self.regions = list(self.layout.REGIONS)
        self.policy = policy
        self.mind = Mind(policy, trace=False, world=self.world, layout=self.layout)
        self.learn_after, self.on_learn = learn_after, on_learn
        self.loaded = 0
        if self.path and self.path.exists():
            events = EventLog.load(self.path)
            if self.scene_id != Y.DEFAULT_SCENE_ID and any(e.get("world") != self.world for e in events):
                raise ValueError("场景事件流归属不一致")
            self.mind.cog = Cognition.replay(events, world=self.world, layout=self.layout)
            self.loaded = len(events)
        self.learned = {k for k, n in self._counts().items() if n >= learn_after}  # 重放出来的: 以前已经写过了
        # 端东西的经验 (每一趟一行: 情境、做法、感知到的后果), 重启时读回来
        self.carry_path = Path(carry_path) if carry_path else None
        self.carry = CarryMind()
        self.trip: dict | None = None
        # 这一趟要什么 (什么时候要、要不要热的、说没说不急): 只能来自你告诉她的; 聊天里还没接, 先空着 (都当"不知道")
        self.wishes: dict = {}
        if self.carry_path and self.carry_path.exists():
            for line in self.carry_path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line) if line.strip() else None
                if rec and self.scene_id != Y.DEFAULT_SCENE_ID and rec.get("scene_id") != self.scene_id:
                    raise ValueError("端物经验的场景归属不一致")
                if rec and rec.get("kind") == "trip":
                    self.carry.records.append({k: rec[k] for k in ("ctx", "choice", "outcome", "ev")})
        self.carry_said = {r["ev"] for r in self.carry.records if r["choice"] != DEFAULT}  # 改过做法的: 写过"我的理解"了
        self.jerk_said = None  # 上次在状态行说"手抖了一下"是什么时候
        self.afford_path = Path(afford_path) if afford_path else None
        self.afford = AffordMind(self.afford_path, clock=clock, scene_id=self.scene_id if self.scene_id != Y.DEFAULT_SCENE_ID else None)  # 可供性的发现簿
        from .store import generation
        self.generation_dir = Path(generation_dir) if generation_dir else (self.path.parent if self.path else None)
        self.generation = generation(self.generation_dir) if self.generation_dir else 0  # 删除代次 (页面带着旧代次发来的不收)

    @property
    def belief(self):
        return self.mind.belief

    def _counts(self) -> dict[tuple, int]:
        out: dict[tuple, int] = {}
        for r in self.mind.cog.exp.records:
            out[(r["kind"], r["found_at"])] = out.get((r["kind"], r["found_at"]), 0) + 1
        return out

    def set_regions(self, regions) -> None:
        if isinstance(regions, list):
            ok = [r for r in self.layout.REGIONS if r in regions]
            if ok:
                self.regions = ok
                self.mind.regions = ok

    def observe(self, msg: dict) -> dict | None:
        """收一份观察. 返回 {changes, notes (聊天状态行能说的发现), learned (这次攒够的经验)}; 不认识的返回 None."""
        if self.stale(msg):
            return None
        with self.lock:
            if self.stale(msg):
                return None
            self.afford.declare(msg.get("affords"))
            news = self.afford.observe(msg)  # 走在路上看见的东西也算
            self.set_regions(msg.get("regions"))
            obs = sanitize(msg, self.regions, self.clock(), self.layout)
            if obs is None:
                return {"changes": [], "notes": [], "learned": [], "afford_news": news} if news else None
            obs["world"] = self.world
            ch = self.mind.perceive(obs)
            self._trip_saw(obs)
            ev = self.mind.cog.log.events[-1]
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
            notes, learned = [], []
            counts = self._counts()
            for c in ch:
                if c[0] == "missing":
                    notes.append(f"你刚发现{self.layout.item_name(c[1])}不在{self.layout.region_name(c[2])}了")
                elif c[0] == "found" and c[2]:
                    notes.append(f"你刚在{self.layout.region_name(c[3])}找到了{self.layout.item_name(c[1])} (原来以为在{self.layout.region_name(c[2])})")
                    key = (self.layout.kind_of(c[1]), c[3])
                    if counts.get(key, 0) >= self.learn_after and key not in self.learned:
                        self.learned.add(key)
                        learned.append(f"{self.layout.KINDS.get(key[0], '东西')}不在原来的地方时, 好几次都是在{self.layout.region_name(c[3])}找到的"
                                       f" (找到过 {counts[key]} 次)")
                elif c[0] == "taken":
                    notes.append(f"你刚看着用户把{self.layout.item_name(c[1])}拿走了")
                elif c[0] == "felt" and c[2] == "jerk":
                    hurt = any(x[0] == "spill" or (x[0] == "felt" and x[2] in ("hot", "burn")) for x in ch)
                    if hurt or self.jerk_said is None or obs["t"] - self.jerk_said > JERK_QUIET_S:
                        notes.append(f"你的{c[1]}刚才{FELT_SAY[c[2]]}")
                        self.jerk_said = obs["t"]
                elif c[0] == "felt":
                    notes.append(f"你的{c[1]}刚才{FELT_SAY[c[2]]}")
                elif c[0] == "spill":
                    notes.append(self._spill_note(*c[1:]))
                elif c[0] == "empty":
                    notes.append(f"你刚才喝{self.layout.item_name(c[1])}, 是空的")
                elif c[0] == "slip":
                    notes.append(f"你{HANDS[c[2]]}里的{self._thing(c[1])}刚才滑了一下")
                elif c[0] == "dropped":
                    notes.append(f"{self._thing(c[1])}刚才从你{HANDS[c[2]]}里掉下去了")
                elif c[0] == "fell":
                    notes.append(f"你刚看见{self._thing(c[1])}{'倒了, ' if c[3] else ''}掉在{'地上' if c[2] == 'floor' else '台面上'}, "
                                 f"{LOUD_SAY[c[4]]}")
                elif c[0] == "noise":
                    notes.append(f"你刚听见{LOUD_SAY[c[1]]}, 没看见是什么")
        for text in learned:  # 写文件, 不占着锁
            if self.on_learn:
                self.on_learn(text)
        return {"changes": ch, "notes": notes, "learned": learned, "afford_news": news}

    def afford_done(self, msg: dict) -> dict | None:
        """做完一件可供性 (room_afford_done): 记进发现簿. 不认识的事、旧代次发来的返回 None."""
        with self.lock:
            return None if self.stale(msg) else self.afford.done(msg)

    def stale(self, msg: dict) -> bool:
        """页面带着旧的删除代次 (gen) 发来的: 重置之前的经历, 不收. 不带 gen 的照收 (老页面)."""
        gen = msg.get("gen") if isinstance(msg, dict) else None
        if not isinstance(msg, dict) or msg.get("scene_id", Y.DEFAULT_SCENE_ID) != self.scene_id:
            return True
        return isinstance(gen, int) and not isinstance(gen, bool) and gen < self.generation

    def known(self) -> list[dict]:
        """给 room_plan 的提示词: 每件可供性一共试过几次、每个情境下觉得怎么样 (她的经验, 只有编号和数字)."""
        return self.afford.known()

    def known_state(self) -> dict:
        """下行 room_known: 她对小窝的了解 (看见过什么、试过什么、觉得怎么样; 时刻是多久以前)."""
        return self.afford.state()

    # -- 端东西 ------------------------------------------------------------------------------------
    def _trip_saw(self, obs: dict) -> None:
        """端着的这一趟里感知到的: 这只杯子洒了多大一股 (每一段取最大的)、身上哪里烫了湿了 (嘴不算)、看到的液面."""
        t = self.trip
        if not t:
            return
        if (obs.get("fill") or {}).get(t["item"]):
            t["fill_seen"] = obs["fill"][t["item"]]
        for e in obs.get("events", []):
            st = t["stages"].get(ROOM_STAGE.get(e.get("stage")))
            if st is None:
                continue
            if e["kind"] == "spill" and e["item"] == t["item"] and MAG[e["amount"]] > MAG[st["spill"]]:
                st["spill"] = e["amount"]
            elif e["kind"] == "felt" and e["part"] != "嘴" and [e["part"], e["level"]] not in st["felt"]:
                st["felt"].append([e["part"], e["level"]])
            elif e["kind"] == "drop" and e["item"] == t["item"]:
                t["dropped"] = True  # 脱手了: 这一趟没送到

    def carry_plan(self, msg: dict) -> dict | None:
        """端起杯子之前: 这一趟怎么端 ({look} 是先看一眼杯口再来; 不然 {choice, why, say}). 只认固定编号和词表."""
        cup = msg.get("cup")
        if cup not in self.layout.ITEMS or self.stale(msg):
            return None
        wait_s = msg.get("wait_s") if isinstance(msg.get("wait_s"), (int, float)) and not isinstance(msg.get("wait_s"), bool) else 0
        ctx = {"fill": msg.get("fill") if msg.get("fill") in FILLS else None, "cup": cup_kind(cup), "hot": msg.get("hot") is True,
               "route": msg.get("route") if msg.get("route") in ROUTES else "none", "can_fill": False,
               "source": "user" if msg.get("source") == "user" else "self", "wait_s": max(0.0, min(3600.0, float(wait_s))),
               **self.wishes}
        learned = []
        with self.lock:
            if self.stale(msg):
                return None
            if self.trip:  # 上一趟没收尾 (活动被打断、页面关了): 按没送到记下, 感知到的不丢
                learned = self._close_trip({"delivered": False})["learned"]
            c = self.carry.choose(ctx)
        for text in learned:
            if self.on_learn:
                self.on_learn(text)
        with self.lock:
            if self.stale(msg):
                return None
            if c.get("look"):
                return {"look": True, "why": c["why"]}
            self.trip = {"id": msg.get("id"), "item": cup, "t0": self.clock(), "fill_seen": ctx["fill"],
                         "stages": {s: {"spill": "none", "drop": 0, "felt": []} for s in CARRY_STAGES}}
            names = [CHOICE_ZH[(k, v)] for k, v in c["choice"].items() if (k, v) in CHOICE_ZH]
            return {"look": False, "choice": c["choice"], "why": c["why"], "say": f"她{'、'.join(names)}" if names else ""}

    def carry_done(self, msg: dict) -> dict | None:
        """端完了 (放下了): 这一趟感知到的变成一条经验. 返回 {note (状态行能说的), learned}; 没在端的返回 None."""
        with self.lock:
            if not self.trip or self.stale(msg):
                return None
            res = self._close_trip(msg)
        for text in res["learned"]:  # 写文件, 不占着锁
            if self.on_learn:
                self.on_learn(text)
        return res

    def _close_trip(self, msg: dict) -> dict:
        """(拿着锁) 这一趟收尾: 记一条经验、写盘, 返回 {note, learned, spilled}."""
        t, self.trip = self.trip, None
        waited = msg.get("waited") if isinstance(msg.get("waited"), (int, float)) and not isinstance(msg.get("waited"), bool) else 0
        secs = max(0.0, self.clock() - t["t0"] - max(0.0, float(waited)))
        outcome = {"stages": t["stages"], "delivered": msg.get("delivered") is not False and not t.get("dropped"), "secs": round(secs),
                   "end_fill": t.get("fill_seen")}
        ev = f"room-{len(self.carry.records) + 1}"
        pending = self.carry.pending or {}
        row = self.carry.learn(outcome, ev=ev)
        rec = self.carry.records[-1]
        if self.carry_path:
            self.carry_path.parent.mkdir(parents=True, exist_ok=True)
            with self.carry_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"kind": "trip", "t": round(self.clock(), 1), **rec,
                                    **({"scene_id": self.scene_id} if self.scene_id != Y.DEFAULT_SCENE_ID else {})}, ensure_ascii=False, sort_keys=True) + "\n")
        spilled = [f"{STAGE_ZH[s]}时洒了{AMOUNT_SAY[t['stages'][s]['spill']]}" for s in row["spilled"]]
        felt = sorted({f"{p}{FELT_SAY[lv]}" for st in t["stages"].values() for p, lv in st["felt"]})
        note = ("你刚才端咖啡, " + ", ".join(spilled + felt)) if spilled or felt else ""
        learned = []
        if rec["choice"] != DEFAULT and "没经历过" not in (pending.get("why") or "") and not self.carry_said:
            learned.append(f"端刚冲好的咖啡: {pending.get('why')}")  # 第一次因为自己的经历改了端法
        if rec["choice"] != DEFAULT:
            self.carry_said.add(ev)
        return {"note": note, "learned": learned, "spilled": row["spilled"]}

    def _thing(self, item) -> str:
        return self.layout.item_name(item) if item in self.layout.ITEMS else "东西"

    def _spill_note(self, item, amount, where, stage, touched) -> str:
        on = {"floor": ", 洒在地上", "machine": ", 洒在咖啡机上", "surface": ", 洒在台面上"}.get(where) or f", 洒在{self.layout.region_name(where)}"
        if item == "machine":
            return f"咖啡机出水洒出来{AMOUNT_SAY[amount]}{on}"
        return f"你刚才{STAGE_SAY[stage]}的时候, {self.layout.item_name(item)}洒出来{AMOUNT_SAY[amount]}{on}" + (", 溅到你身上了" if touched else "")

    def search(self, item: str, new: bool = False) -> dict:
        """找 item 的下一步: {action, say (状态栏), knows (她记得的)}."""
        with self.lock:
            if item not in self.layout.ITEMS:
                return {"action": ["give_up", "不认识"], "say": "找不着…", "knows": ""}
            if new or self.mind.search is None or self.mind.search["item"] != item:
                self.mind.begin(item)
            a = self.mind.step(item)
            return {"action": a, "say": say_for(a, self.layout), "knows": self.belief.describe(item, self.clock())}

    def answer(self, text: str) -> str | None:
        """聊天里问"杯子在哪": 按她的认知说 (带凭什么、多久以前). 不是这种问题, 或者从没收到过小屋的观察, 返回 None."""
        t = str(text or "")
        if not (_WHERE.search(t) or _SHORT_WHERE.match(t)):
            return None
        aliases = ALIASES if self.scene_id == Y.DEFAULT_SCENE_ID else sorted(
            [(name, item) for item, (name, _) in self.layout.ITEMS.items()] +
            [("杯子", self.layout.favorite)], key=lambda pair: -len(pair[0]))
        item = next((i for k, i in aliases if k in t), None)
        if item is None:
            return None
        with self.lock:
            if not self.mind.cog.log.events:
                return None
            return self.belief.describe(item, self.clock()) + "。"

    def forget(self, generation: int | None = None) -> list[str]:
        """删掉她的经历 (你在面板上点了删除、重置): 三个文件、服务端里记着的认知和经验都清掉. generation: 新的删除代次
        (之后页面带着旧代次发来的不收). 返回删了哪几个文件."""
        self.afford.close()  # 先停发现簿的写 (它有自己的锁), 排队的写不会把删掉的文件又建起来
        with self.lock:
            if generation is not None:
                self.generation = generation
            gone = []
            for f in (self.path, self.carry_path, self.afford_path):
                if f and f.exists():
                    f.unlink()
                    gone.append(f.name)
            self.mind = Mind(self.policy, trace=False, world=self.world, layout=self.layout)
            self.mind.regions = self.regions
            self.carry, self.trip = CarryMind(), None
            self.learned, self.carry_said, self.loaded = set(), set(), 0
            spec, self.afford = self.afford.spec, AffordMind(self.afford_path, clock=self.clock, scene_id=self.scene_id if self.scene_id != Y.DEFAULT_SCENE_ID else None)
            self.afford.spec = spec  # 声明的东西还在, 她的了解清空
            return gone

    def status(self) -> dict:
        with self.lock:
            return {"events": len(self.mind.cog.log.events), "loaded": self.loaded, "regions": len(self.regions),
                    "experience": len(self.mind.cog.exp.records), "learned": len(self.learned), "carry": len(self.carry.records),
                    "afford": self.afford.status(), "generation": self.generation,
                    "dir": str(self.path.parent) if self.path else None}
