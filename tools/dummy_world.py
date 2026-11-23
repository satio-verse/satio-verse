"""假世界进程: 扮演「小屋/游戏」那一侧的身体, 只连本机 8790 的 /ws, 不写任何文件.

它**不新造协议**: 说的每一句都是桌宠页面已经在说的 room_* 消息 (字段逐条对照过):
  连上先收 hello / memory_candidates / channel_status ... (wm/app.py:273-287)
  → room_capabilities {scene_id, activities:[{id, enabled}]}    (页面见 desktop/web/pet.js:309)
      回 room_capabilities_done; 只认 wm/room_action.py:44-55 的 16 个 id
  → room_observe {scene_id, id, at, seen, hand, open, events?}   (desktop/web/room-life.js:503-546)
      回 room_observe_done {id, ok, changes, learned}
  ← room_action {request_id, turn, scene_id, act, timeout_ms}    (wm/pipeline.py:1075-1076)
      → 真改自己的世界状态, 回 room_action_result {request_id, scene_id, act, started, completed, ok, detail}
        (Core 按 request_id + scene_id + act 三样对上, wm/pipeline.py:423-428)
  ← room_plan {id, ok, pick, thought, how}                       (wm/pipeline.py:767-800)
      → 执行 pick, 回 room_afford_done {id, act, ctx, ok, felt, hurt, skip} (wm/pipeline.py:725-738)
        这条**没有 request_id**, 所以不能拿 room_action_result 回它 (那样 Core 找不到待办)
  ← room_step {id, ok, item, action:[动词, ...]}                 (wm/pipeline.py:705-713)
      → 按 action 走路/开抽屉/拿东西, 再报一条带 act/result 的 room_observe
  ← room_action_cancel / 任何不认识的消息: 只记一行, 不崩

用法 (cwd 必须是仓库根):
    python tools\\dummy_world.py --rounds 10 --i-fell 4
    python tools\\dummy_world.py --rounds 10 --interval 1.5 --think
    python tools\\dummy_world.py --dry-run            # 不连网, 只把要发的消息打出来
    python tools\\dummy_world.py --port 8790 --path /ws --verbose

安全: 只允许连回环地址; 只读不写文件; 不改配置; 不碰服务进程. 它跑在一个**自己的** ws 连接上,
而 8790 每条连接一个 Session (wm/app.py:271), 所以不会动正在跑的页面那一份会话状态.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 没装包时也能 import wm (和 tools/bench_room_plan.py 一样)
    sys.path.insert(0, str(ROOT))

from wm.cognition import layout  # noqa: E402
from wm.room_action import NAMES as ROOM_ACTS  # noqa: E402  (16 个动作 id 的唯一定义处, 不抄一份)
from wm.world.schema import PerceptionEvent  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_PATH = "/ws"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# 她做完这个动作以后站在哪个站位 (None = 原地不动); 只用来让观察更像页面, 不影响 Core 的判断
ACT_ZONE = {"coffee": "coffee", "phone": "desk", "computer": "desk", "chair_turn": None,
            "inspect_temperature": "desk", "rest": "bed", "window": "window", "books": "reading",
            "visit": "desk", "sleep": "bed", "yoga": "yoga", "read": "reading", "explore": "fridge",
            "drink": "fridge", "milk": "fridge", "snack": "fridge"}
# Core → 客户端 这一侧的话 (回合、音频、渠道、回执): 世界不认识它们, 但也不是"未知" —— 只记一笔
CORE_TYPES = frozenset({"hello", "memory_candidates", "channel_status", "channel_draft", "trace",
                        "turn_start", "backchannel", "intent", "reply_delta", "clause", "audio_end",
                        "turn_server_done", "interrupted", "error", "pong", "room_observe_done",
                        "room_known", "room_capabilities_done", "room_carry_plan", "room_carry_saved"})


def print_json(obj: dict) -> None:
    """打一行 JSON (Windows 控制台是 GBK, 编不出来的字符换成 \\u 转义, 不让日志把进程弄崩)."""
    try:
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    except UnicodeEncodeError:
        print(json.dumps(obj, ensure_ascii=True), flush=True)


@dataclass
class WorldState:
    """假世界自己的状态: 椅子朝哪儿、杯子在哪、她摔了几次 —— 这些**一条都不进** Core 的消息.

    Core 只知道页面/假世界报上去的观察和结果; 世界只知道自己执行了什么. 这就是「Core 不认识世界、
    世界不持有自我」的分界: 两个 dataclass 之间的字段 (PerceptionEvent / ActionIntent) 是唯一的通道.
    """

    scene_id: str = layout.DEFAULT_SCENE_ID
    zone: str = "desk"
    open: list[str] = field(default_factory=list)
    hand: list[str] = field(default_factory=list)
    # 杯子 -> 它所在的 REGIONS 编号 (layout.REGIONS 里真实存在的区域)
    cups: dict[str, str] = field(default_factory=lambda: {"IR_Item_Espresso_Pink": "surf_desk",
                                                          "IR_Item_Mug_0": "surf_desk"})
    chair_deg: int = 0          # 椅子朝向 (chair_turn 一次 90 度)
    falls: int = 0              # 摔了几次
    hurts: list[str] = field(default_factory=list)   # 摔完身上哪儿疼 (只留在世界侧)
    done: dict[str, int] = field(default_factory=dict)   # 每个动作做了几次
    observe_seq: int = 0        # room_observe 的 id
    # 商店 (Hemifuture §16.3 第三个闭环): 世界侧持有账本, Core 只看回执里的余额
    shop: "Shop | None" = None

    def buy(self, item: str, idem_key: str, *, place_failed: bool = False) -> dict:
        """在世界里买一件东西: 冻结 → 摆放 → 交付, 摆放失败全额退回 (回执带余额)."""
        from wm.world.economy import Ledger, Shop

        if self.shop is None:
            self.shop = Shop(Ledger())
            self.shop.grant()  # 演示用: 真实世界由运营发放, 这里发 500 点
        return self.shop.buy(item, idem_key=idem_key, place_failed=place_failed)

    def balance(self) -> int:
        return self.shop.ledger.balance(self.shop.payer) if self.shop else 0

    def act(self, verb: str) -> tuple[bool, str]:
        """真的执行一个动作, 返回 (成没成, 一句人话的结果). 不认识的动词算没成."""
        if verb not in ROOM_ACTS:
            return False, f"这个世界没有「{verb}」这个动作"
        zone = ACT_ZONE.get(verb)
        if zone:
            self.zone = zone
        self.done[verb] = self.done.get(verb, 0) + 1
        if verb == "chair_turn":
            self.chair_deg = (self.chair_deg + 90) % 360
            return True, f"{ROOM_ACTS[verb]}：椅子转到 {self.chair_deg} 度，椅背现在朝" + ("窗边" if self.chair_deg == 90 else "屋里")
        if verb == "coffee":
            return True, f"{ROOM_ACTS[verb]}：咖啡冲好了"
        if verb == "visit":
            return True, f"{ROOM_ACTS[verb]}：走到你这边来了"
        return True, f"{ROOM_ACTS[verb]}：做完了"

    def step(self, actions: list) -> list[str]:
        """执行 Core 给的找东西步骤 room_step.action; 返回做了哪几步 (人话)."""
        did: list[str] = []
        for raw in actions if isinstance(actions, list) else []:
            if not isinstance(raw, list) or not raw:
                continue
            do, args = str(raw[0]), raw[1:]
            if do == "go" and args and args[0] in layout.ZONES:
                self.zone = args[0]
                did.append(f"走到{layout.zone_name(args[0])}")
            elif do == "open" and args and args[0] in layout.REGIONS:
                if args[0] not in self.open:
                    self.open.append(args[0])
                did.append(f"打开{layout.region_name(args[0])}")
            elif do == "close" and args:
                self.open = [r for r in self.open if r != args[0]]
                did.append("关上")
            elif do == "pick" and args and args[0] in layout.ITEMS:
                self.cups.pop(args[0], None)
                self.hand = [args[0]]
                did.append(f"拿起{layout.item_name(args[0])}")
            elif do == "look":
                did.append("看了看")
        return did

    def fall(self) -> list[dict]:
        """她在这个世界里摔了一跤: 手里的杯子摔到地上, 膝盖疼. 返回 room_observe.events 里的两条."""
        self.falls += 1
        self.hurts.append("膝盖")

        item = self.hand[0] if self.hand else next(iter(self.cups), None)
        self.hand = []
        if item:
            self.cups[item] = "surf_bed" if self.zone == "bed" else "surf_desk"
        # 词表见 wm/cognition/live.py:107-111: kind=fall 要 where ∈ floor|surface, loud ∈ soft|clack|bang
        return [{"kind": "fall", "item": item or "thing", "where": "floor", "toppled": True, "loud": "bang"}]

    def observe(self, events: list[dict] | None = None) -> dict:
        """拼一条 room_observe 的负载: 看见什么、手里有什么、开着什么 (字段同 desktop/web/room-sight.js:94-106)."""
        self.observe_seq += 1
        seen: dict[str, list[str]] = {}
        for item, region in self.cups.items():
            seen.setdefault(region, []).append(item)
        payload = {"id": self.observe_seq, "at": self.zone, "seen": seen, "hand": list(self.hand),
                   "open": list(self.open)}
        if events:
            payload["events"] = events
        return payload


class DummyWorld:
    """一个假世界: 连 8790, 按轮次报观察、执行 Core 发下来的动作、把结果报回去."""

    def __init__(self, url: str, state: WorldState, *, rounds: int = 0, interval: float = 2.0,
                 fell_rounds: tuple[int, ...] = (), think: bool = False, say: str = "",
                 verbose: bool = False, trade: str = "") -> None:
        self.url = url
        self.state = state
        self.rounds = rounds
        self.interval = interval
        self.fell_rounds = fell_rounds
        self.think = think
        self.say = say
        self.verbose = verbose
        self.trade = trade
        self.stats = {"rounds": 0, "action": 0, "plan": 0, "step": 0, "cancel": 0, "unknown": 0, "results": 0}

    async def _connect(self, ws) -> None:
        """握手: 收掉 hello 那几条, 报一次能力 (16 个动作全开), 关掉语音 (这条连接只测文字)."""
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.receive(), 0.5)
            except asyncio.TimeoutError:
                break
            if raw.type == aiohttp.WSMsgType.CLOSE:
                raise ConnectionError("8790 在握手时就关掉了连接")
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            msg = json.loads(raw.data)
            if msg.get("type") == "hello":
                break
        await ws.send_json({"type": "output", "audio": False})   # 只下发文字, 不合成语音
        await ws.send_json({"type": "room_capabilities", "scene_id": self.state.scene_id,
                            "activities": [{"id": act, "enabled": True} for act in ROOM_ACTS]})
        if self.verbose:
            print_json({"bind": "room_capabilities", "scene_id": self.state.scene_id, "activities": len(ROOM_ACTS)})
        if self.say:  # 桌面那一侧替主人说一句 (只在这个假世界的连接上; Core 会发 room_action 下来)
            await ws.send_json({"type": "text", "text": self.say})
            print_json({"say": self.say})

    async def _reply(self, ws, msg: dict) -> dict:
        """处理一条 Core 发下来的消息: 该动的动作真的动, 该回的回. 返回日志要记的东西."""
        typ = msg.get("type")
        if typ == "room_action":
            act = str(msg.get("act") or "")
            ok, detail = self.state.act(act)
            self.stats["action"] += 1
            await ws.send_json(PerceptionEvent(
                source="dummy-world", world_id=self.state.scene_id, kind="result", wire="room_action_result",
                payload={"request_id": msg.get("request_id"), "act": act,
                         # 三样都要 true 才算完成 (wm/room_action.py:64-69 的 terminal)
                         "started": True, "completed": True, "ok": ok,
                         "reason": "" if ok else detail,
                         "detail": {"text": detail, "known": True}}).to_msg())
            self.stats["results"] += 1
            return {"act": act, "ok": ok, "detail": detail, "back": "room_action_result"}
        if typ == "room_action_cancel":
            self.stats["cancel"] += 1
            return {"cancel": msg.get("request_id"), "reason": msg.get("reason")}
        if typ == "room_plan":
            pick = str(msg.get("pick") or "")
            if msg.get("ok") is not True or not pick:
                return {"plan": "skipped", "reason": msg.get("reason")}
            ok, detail = self.state.act(pick)
            self.stats["plan"] += 1
            # 她"接下来做什么"由 world 执行完, 按页面的做法回一条可供性结果 (wm/pipeline.py:725-738)
            await ws.send_json(PerceptionEvent(
                source="dummy-world", world_id=self.state.scene_id, kind="result", wire="room_afford_done",
                payload={"id": msg.get("id"), "act": pick, "ctx": self.state.zone, "ok": ok,
                         "felt": detail, "hurt": False, "skip": False}).to_msg())
            self.stats["results"] += 1
            return {"plan": pick, "ok": ok, "detail": detail, "back": "room_afford_done"}
        if typ == "room_step":
            actions = msg.get("action")
            did = self.state.step(actions if isinstance(actions, list) else [])
            self.stats["step"] += 1
            head = str(msg.get("item") or "")
            # 页面拿到 room_step 会去走/开, 然后报一条带 act 的观察 (wm/cognition/live.py:67-79 收这几种动词)
            payload = self.state.observe()
            payload["act"] = [actions[0][0], *actions[0][1:]] if actions and isinstance(actions[0], list) else ["look"]
            payload["result"] = {"ok": bool(did)}
            await ws.send_json(PerceptionEvent(source="dummy-world", world_id=self.state.scene_id,
                                               kind="observe", payload=payload).to_msg())
            return {"step": head, "did": did, "back": "room_observe"}
        if typ in CORE_TYPES:  # Core 这一侧的话 (回合、音频、渠道...): 世界不解释, 记一笔就好
            return {} if not self.verbose else {"core": typ}
        self.stats["unknown"] += 1
        return {"unknown": typ}

    async def run(self) -> int:
        """跑完这么多轮; 返回进程退出码 (0 正常, 2 连不上)."""
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(self.url, heartbeat=None) as ws:
                await self._connect(ws)
                if self.trade:  # 世界侧买一件东西, 把回执报给 Core (Hemifuture §16.3 账本闭环)
                    receipt = self.state.buy(self.trade, f"trade:{datetime.now():%Y%m%d%H%M%S}")
                    await ws.send_json({"type": "room_trade_done", "id": 1, "scene_id": self.state.scene_id,
                                        **{k: v for k, v in receipt.items() if k != "placed"},
                                        "msg": (f"买成了「{receipt['item']}」" if receipt.get("ok")
                                                else f"没买成：{receipt.get('why', '')}")})
                    print_json({"trade": receipt})
                rnd = 0
                while self.rounds <= 0 or rnd < self.rounds:
                    rnd += 1
                    t0 = time.perf_counter()
                    fell = rnd in self.fell_rounds
                    events = self.state.fall() if fell else None
                    obs = self.state.observe(events=events)
                    await ws.send_json(PerceptionEvent(source="dummy-world", world_id=self.state.scene_id,
                                                       kind="observe", payload=obs).to_msg())
                    if self.think and rnd == 1:  # 只问一次: 生产 room.think.min_interval_s=150, 问多了是 rate
                        await ws.send_json({"type": "room_think", "id": 1, "scene_id": self.state.scene_id,
                                            "options": list(layout.ZONES)})
                    got: list[str] = []
                    effects: list[dict] = []
                    deadline = time.perf_counter() + self.interval
                    while True:
                        left = deadline - time.perf_counter()
                        if left <= 0:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.receive(), left)
                        except asyncio.TimeoutError:
                            break
                        if raw.type == aiohttp.WSMsgType.CLOSE:
                            raise ConnectionError("8790 关掉了连接")
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            msg = json.loads(raw.data)
                        except json.JSONDecodeError:
                            got.append("bad-json")
                            continue
                        got.append(str(msg.get("type")))
                        effects.append(await self._reply(ws, msg))
                    self.stats["rounds"] += 1
                    line = {"round": rnd, "t": datetime.now().isoformat(timespec="seconds"),
                            "sent": ["room_observe"], "got": got, "fell": fell,
                            "effect": [e for e in effects if e], "ms": int((time.perf_counter() - t0) * 1000)}
                    print_json(line)
        return 0


def parse_fell(text: str) -> tuple[int, ...]:
    """--i-fell 3,7 -> (3, 7); 空串 = 不摔."""
    if not text.strip():
        return ()
    out = []
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise argparse.ArgumentTypeError(f"--i-fell 只收轮次数字, 收到 {part!r}")
        out.append(int(part))
    return tuple(sorted(set(out)))


def build_url(host: str, port: int, path: str) -> str:
    """只许连回环地址 (这个世界不许往外说话)."""
    if host not in LOOPBACK:
        raise SystemExit(f"只允许连 127.0.0.1 / localhost, 收到 {host!r}")
    return f"ws://{host}:{int(port)}{path if path.startswith('/') else '/' + path}"


def dry_run(args, url: str) -> int:
    """不连网: 把前几轮真正会发/会回的字段打出来, 让人先看一眼 (生产上也安全)."""
    state = WorldState()
    print("# dry-run: 不连 " + url)
    print_json({"connect": url, "scene_id": state.scene_id, "rounds": args.rounds or "无限",
                "interval_s": args.interval, "i_fell": list(args.i_fell), "think": args.think})
    print_json({"send": {"type": "output", "audio": False}})
    print_json({"send": {"type": "room_capabilities", "scene_id": state.scene_id,
                         "activities": [{"id": a, "enabled": True} for a in ROOM_ACTS]}})
    for r in range(1, min(args.rounds or 3, 3) + 1):
        events = state.fall() if r in args.i_fell else None
        print_json({"send": PerceptionEvent(source="dummy-world", world_id=state.scene_id, kind="observe",
                                            payload=state.observe(events=events)).to_msg()})
    print_json({"on room_action": "room_action_result {request_id, act, started:true, completed:true, ok:true, detail}"})
    print_json({"on room_plan": "room_afford_done {id, act, ctx, ok, felt, hurt:false, skip:false}"})
    print_json({"on room_step": "room_observe {act, result:{ok:true}}"})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="假世界进程: 只连本机 8790, 扮演小屋/游戏那一侧")
    ap.add_argument("--host", default=DEFAULT_HOST, help="只接受 127.0.0.1 / localhost / ::1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--path", default=DEFAULT_PATH, help="ws 路径 (默认 /ws)")
    ap.add_argument("--rounds", type=int, default=0, help="跑多少轮 (0 = 一直跑到 Ctrl+C)")
    ap.add_argument("--interval", type=float, default=2.0, help="每轮等多久的 Core 消息 (秒)")
    ap.add_argument("--i-fell", type=parse_fell, default=(), help="在第几轮让她摔一跤, 例如 3,7")
    ap.add_argument("--think", action="store_true", help="第一轮额外发一次 room_think (会被 150s 限流, 只发一次)")
    ap.add_argument("--say", default="", help="连上以后先替主人说一句 (例如 \"帮我转一下椅子\"), 用来验证动作闭环")
    ap.add_argument("--trade", default="", help="连上以后在世界侧买一件东西 (chair/table/plant), 把回执报给 Core")
    ap.add_argument("--verbose", action="store_true", help="把每一条收到的消息都记下来")
    ap.add_argument("--dry-run", action="store_true", help="不连网, 只打印将要发什么")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]  # GBK 控制台别因为一个字符崩
    except (AttributeError, ValueError):
        pass
    url = build_url(args.host, args.port, args.path)
    if args.dry_run:
        return dry_run(args, url)
    world = DummyWorld(url, WorldState(), rounds=max(0, args.rounds), interval=max(0.2, args.interval),
                       fell_rounds=args.i_fell, think=args.think, say=args.say, verbose=args.verbose,
                       trade=args.trade)
    print_json({"connect": url, "note": "只连本机 8790; 不写文件、不改配置、不碰服务进程"})
    try:
        code = asyncio.run(world.run())
    except ConnectionError as exc:
        print_json({"error": str(exc), "hint": "8790 不在跑, 或者 ws_refusal 拒了这条连接"})
        return 1
    except Exception as exc:  # noqa: BLE001  连不上/协议出错时给人一句人话, 不吞堆栈
        print_json({"error": f"{type(exc).__name__}: {exc}"})
        return 2
    print_json({"done": True, **world.stats, "state": {"zone": world.state.zone, "chair_deg": world.state.chair_deg,
                                                       "falls": world.state.falls, "done": world.state.done}})
    return code


if __name__ == "__main__":
    raise SystemExit(main())
