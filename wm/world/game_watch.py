"""游戏世界·只看画面. 样板: 主程序不引用, 宿主第 1 步可以替换. 决定: 等你拍板的事"世界2" (先只做看画面, 文字多、节奏慢的单机游戏).

把现有的屏幕陪看 (wm/screen_along.py) 包成 game 档的世界, 一个字不新开口子:
  前台窗口 / 能不能看 / 截图 / 拼图 / 节奏 / 说话 / 暂停 / 结束, 全是 ScreenAlong 本体和它用的 SA.foreground、SA.watchable、
  SA.grab、SA.sheet; 上云的口径和陪看一样 (前台窗口的小拼图, 只在内存, 看完就删). 这里只做三件事:
    1. 门 (game_gate): ScreenAlong 看到的真值 -> 她感知得到的事件 (清单里声明过的字段);
    2. 世界会话 (game_session): 事件加拼图交给模型, 模型只能返回清单里声明过的动作 (game_say / game_note / game_recall);
    3. 叫停 (leave): 立刻不再取新画面, 在时限内确认.
不碰游戏: 没有往游戏里发任何东西的办法, 不读游戏的内存、存档、日志, 不注入, 不装驱动; 也不写任何记忆 (世界里的经历只经 on_end 交给宿主).
只在主人叫她陪的时候开始: start() 要一个 OwnerCall 令牌, 令牌只有 owner_call() 在 watch_along.command 判定"陪我玩游戏"这类话时才发.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from wm import screen_along as SA
from wm import watch_along as WA
from wm.channels.outbound import clean_in

from .envelope import Audit, Envelope, WorldSpec, make_envelope
from .game_gate import GameGate
from .game_session import GameSession

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "game-watch"

# 游戏版的陪看节奏 (只在这里覆盖, 日常陪看的默认不动): 开局约 2 秒第一眼; 画面大变 (换幕、转场) 不等 look_s 立刻看一眼;
# 离开游戏窗口 45 秒就收尾. 一眼只是一次小拼图, 模型没话说就不说 (也不另花钱).
GAME_CONF = {"tick_s": 1.0, "first_look_s": 2.0, "first_gap_s": 3.0, "min_gap_s": 6.0, "look_s": 25.0, "away_s": 45.0,
             "key_change": 28.0, "key_gap_s": 6.0, "max_tokens": 300}
START_LINE = "好呀，我陪你玩～我只看你游戏那个前台窗口，聊天、密码这些我不看，也不会碰游戏；不想让我看了就说“不看了”。"
ACK_TIMEOUT_S = 2.0


def load_package(path=None) -> tuple[dict, WorldSpec, str]:
    """清单、规格、环境提示词."""
    base = Path(path) if path else PLUGIN_DIR
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    env = (base / manifest["plugin"]["world"].get("env_prompt", "ENV_PROMPT.md")).read_text(encoding="utf-8")
    return manifest, WorldSpec.from_manifest(manifest), env


class OwnerCall:
    """"主人叫她陪"的令牌: 只有 GameWatch.owner_call 发. 程序、定时、插件自己都没有办法开始看屏幕."""
    __slots__ = ("text", "_owner")

    def __init__(self, text: str, owner: "GameWatch"):
        self.text, self._owner = text, owner


@dataclass(frozen=True)
class GameLeave:
    status: str      # acknowledged: 在时限内停好; forced: 超时, 强行丢掉任务; gone: 本来就没在看
    elapsed: float   # 真实秒数
    line: str        # 收尾那句
    spoken: bool     # 这句是不是这里说出口的 (owner_call 的"不看了"由调用方当回复说, 不重复说)


class GameWatch:
    def __init__(self, spec: WorldSpec, llm, speak, *, name: str = "绫音", persona: str = "", env_prompt: str = "",
                 conf: dict | None = None, look=None, grabber=None, clock_fn=time.monotonic, sleep=asyncio.sleep,
                 on_panel=None, on_log=None, on_end=None, trace=None, blocked=None, threaded: bool = True,
                 audit: Audit | None = None, ack_timeout: float | None = None, gate: GameGate | None = None, boundary=None):
        """llm: 有 stream(messages, schema=, max_tokens=, id_slot=, meta=) 的模型 (生产里是陪看用的那个云端模型, 不带工具);
        speak(note, reply, emo) -> 说出去了没有 (桌宠会话的 aside). look / grabber 不给就用 SA.foreground / SA.grab (按名字现取, 测试可换)."""
        self.spec, self.name = spec, name
        self.audit = audit if audit is not None else Audit()
        self.gate = gate if gate is not None else GameGate()
        self.session = GameSession(spec, name, llm, persona=persona, env_prompt=env_prompt, audit=self.audit, clock=clock_fn,
                                   boundary=boundary, batch_s=0.0, max_calls_per_min=15, max_tokens=int((conf or {}).get("max_tokens", 300)))
        self.conf = {**GAME_CONF, **(conf or {})}
        self.now, self.sleep, self.threaded = clock_fn, sleep, threaded
        self._speak_real, self._look, self._grabber = speak, look, grabber
        self.on_panel, self.on_log, self.on_end, self.user_trace, self.blocked = on_panel, on_log, on_end, trace, blocked
        self.ack_timeout = float(ack_timeout if ack_timeout is not None else ACK_TIMEOUT_S)
        self.eye: SA.ScreenAlong | None = None
        self.log: deque = deque(maxlen=400)
        self.notes: list[str] = []
        self.summary: dict | None = None
        self.result: GameLeave | None = None
        self._inbox: deque = deque()       # 巡视线程里产生的事件先放这里, 回到事件循环再进会话
        self._last_info: dict | None = None
        self._pending_say: str | None = None
        self._leaving = False
        self._seq = 0
        self._started = False

    # -- 状态 ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.eye is not None and self.eye.alive and not self._leaving

    @property
    def state(self) -> str:
        if self.eye is None:
            return "idle"
        return "ended" if not self.eye.alive else self.eye.state

    # -- 主人叫她陪 / 叫她回来 -------------------------------------------------------
    async def owner_call(self, text: str) -> tuple[bool, str]:
        """主人这句话是不是"陪我玩游戏"/"不看了": (有没有处理, 要回他的话). 短语表是 watch_along.command 那一份, 不另写;
        带链接 / 视频的"陪我看"走原来的视频通路, 这里不接."""
        cmd = WA.command(text, active=self.alive, waiting=False, video_now=False)
        if cmd is None:
            return False, ""
        if cmd[0] == "stop":
            r = await self.leave(speak_line=False)
            return True, r.line
        if cmd[0] == "start" and cmd[1] is None and not self.alive and not self._started:
            await self.start(OwnerCall(text, self))
            return True, START_LINE
        return False, ""

    async def start(self, call: OwnerCall) -> None:
        if not isinstance(call, OwnerCall) or call._owner is not self:
            raise PermissionError("只有主人叫她陪的时候才开始看画面")
        if self._started:
            return
        self._started = True
        self.eye = SA.ScreenAlong(self.conf, speak=self._speak, think=self._think, look=self._observe, grabber=self._grab,
                                  clock_fn=self.now, sleep=self.sleep, on_end=self._ended, on_change=self._changed,
                                  trace=self._trace, threaded=self.threaded, blocked=self.blocked)
        self._publish()
        self.eye.start()

    async def leave(self, speak_line: bool = True) -> GameLeave:
        """叫她回来 / 主人说"不看了": 立刻不再取新画面, 在 ack_timeout 内确认; 然后收尾一句 (现成的话, 不用模型)."""
        if self.result is not None:
            return self.result
        t0 = time.monotonic()
        self._leaving = True
        eye = self.eye
        if eye is None or not eye.alive:
            self.result = GameLeave("gone", 0.0, "好，不看了。", False)
            return self.result
        title = eye.title
        status = "acknowledged"
        try:
            await asyncio.wait_for(eye.stop("leave"), self.ack_timeout)
        except asyncio.TimeoutError:
            status = "forced"
            self.audit.add("leave_timeout_forced", timeout_s=self.ack_timeout)
            eye.state, eye.frames = "done", []
            if eye.task is not None and not eye.task.done():
                eye.task.cancel()
        elapsed = time.monotonic() - t0
        line = f"好，不看《{title}》了～" if title else "好，不看了。"
        spoken = False
        if speak_line:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                spoken = bool(await asyncio.wait_for(self._speak_real("(这不是主人说的话, 是提醒) 主人叫你回来, 你不看了", line, "neutral"), 2.0))
        self.result = GameLeave(status, elapsed, line, spoken)
        self._log({"k": "left", "status": status, "elapsed": round(elapsed, 3)})
        self._publish()
        return self.result

    def heard(self, text: str, reply: str = "") -> None:
        """陪玩的时候他跟她说的话: 进事件 (piggyback, 跟下一眼一起带), 也给 ScreenAlong 记一笔."""
        if self.eye is not None and self.alive and text:
            self.eye.heard(text, reply)
            self._emit(self.gate.owner_said(str(text)))
            self._publish()

    # -- 给 ScreenAlong 的零件 (截图只有这一条路) ---------------------------------------
    def _observe(self) -> dict | None:
        """前台窗口. 真值留在门里, 她只会从事件里知道清单声明过的那几样."""
        info = (self._look or SA.foreground)()
        self._last_info = dict(info) if info else None
        for e in self.gate.observe(info, self.now()):
            self._inbox.append(e)
        return info

    def _grab(self, rect, width):
        """只截刚刚看过的那个前台窗口的矩形, 而且那个窗口得是能看的; 叫停之后一张都不再取."""
        info = self._last_info
        if self._leaving or info is None or tuple(rect) != tuple(info.get("rect") or ()) \
                or not SA.watchable(info, info.get("screen_area"))[0]:
            self.audit.add("grab_refused")
            return None
        img = (self._grabber or SA.grab)(rect, width)
        if img is not None:
            self.gate.captures += 1
        return img

    async def _speak(self, note: str, reply: str, emo: str) -> bool:
        if self._leaving:
            return False
        return bool(await self._speak_real(note, reply, emo))

    async def _changed(self, info: dict) -> None:
        for e in self.gate.window(info.get("app") or "", info.get("title") or ""):
            self._emit(e)
        self._drain()
        self._publish()

    async def _think(self, ctx: dict):
        """一眼: 事件加拼图交给世界会话, 合格的动作映射回 ScreenAlong 要的 (情绪, 要说的话, 看到了什么)."""
        if self._leaving:
            return "neutral", "", ""
        self._drain()
        self._emit(self.gate.scene(ctx))
        image = ctx.get("image")
        if image:
            self.gate.uploads += 1
        self.session.attach(image)
        try:
            actions = await self.session.step()
        finally:
            self.session.drop_frame()
        emo, reply, saw, say = "neutral", "", "", None
        for a in actions:
            if a.name == "game_say":
                self._log({"k": "act", "name": a.name, "args": a.args, **({"blocked": a.blocked} if a.blocked else {})})
                if a.blocked:
                    self._emit(self.gate.receipt("say_result", False, "blocked"))
                elif say is None:
                    say = a
                else:
                    self._emit(self.gate.receipt("say_result", False, "extra"))
            elif a.name == "game_note":
                saw = clean_in(a.args.get("text", ""), 40)
                self.notes.append(saw)
                self._log({"k": "act", "name": a.name, "args": a.args})
                self._emit(self.gate.receipt("note_result", True, "noted"))
            elif a.name == "game_recall":
                self._log({"k": "act", "name": a.name, "args": {}})
                self._recall()
        if say is not None:
            reply, emo = say.args["text"], say.args.get("emo", "neutral")
            self._pending_say = say.id
        self._publish()
        return emo, reply, saw

    def _recall(self) -> None:
        """game_recall: 她问自己刚才记过什么. 返回的是她自己记的, 不是画面."""
        env = Envelope(self._next_seq(), self.spec.prefix, round(self.now(), 3), "game_recall", "receipt", "batch", {},
                       clean_in(json.dumps({"notes": self.notes[-5:]}, ensure_ascii=False), 400))
        self.session.push(env)
        self._log({"k": "in", "seq": env.seq, "type": env.type, "sense": env.sense, "fields": {}, "text": env.text})

    def _trace(self, ev: dict) -> None:
        if self.user_trace is not None:
            with contextlib.suppress(Exception):
                self.user_trace(ev)
        if ev.get("event") == "look" and self._pending_say:
            self._pending_say = None
            res = str(ev.get("result"))
            self._emit(self.gate.receipt("say_result", res == "said", "spoken" if res == "said" else res))
            if res == "said":
                self._log({"k": "line", "text": ev.get("reply", "")})
        self._publish()

    async def _ended(self, summary: dict) -> None:
        self._drain()
        self._emit(self.gate.ended(summary.get("reason") or ""))
        self.summary = {**summary, "world": self.spec.id, "events": len([1 for i in self.log if i["k"] == "in"]),
                        "uploads": self.gate.uploads}
        self._publish()
        if self.on_end is not None:
            with contextlib.suppress(Exception):
                r = self.on_end(self.summary)
                if asyncio.iscoroutine(r):
                    await r

    # -- 事件进会话 / 记录 / 面板 -----------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _drain(self) -> None:
        while self._inbox:
            self._emit(self._inbox.popleft())

    def _emit(self, e: dict) -> Envelope | None:
        env = make_envelope(self.spec, e, self._next_seq(), self.audit, now=self.now())
        if env is None:
            return None
        self.session.push(env)
        self._log({"k": "in", "seq": env.seq, "type": env.type, "sense": env.sense, "fields": env.fields, "text": env.text})
        return env

    def _log(self, item: dict) -> None:
        item = {"ts": round(time.time(), 3), **item}
        self.log.append(item)
        if self.on_log is not None:
            with contextlib.suppress(Exception):
                self.on_log(item)

    def panel(self) -> dict:
        """旁观面板用的全部真值 (主人看得比她多): 她收到的、她说的、她记的, 以及"她没看到"的 (被跳过的窗口只有类别计数, 没有标题).
        不含任何画面."""
        eye = self.eye
        truth = self.gate.panel_truth()
        stats = dict(eye.stats) if eye is not None else {}
        return {"world": self.spec.id, "state": self.state, "mode": "picture",
                "app": eye.app if eye else "", "title": eye.title if eye else "",
                "she_got": [i for i in self.log if i["k"] == "in"][-30:],
                "she_said": [s for _, s in (eye.lines if eye else [])][-10:],
                "she_noted": self.notes[-10:],
                "stats": {"looks": stats.get("looks", 0), "said": stats.get("said", 0), "silent": stats.get("silent", 0),
                          "errors": stats.get("errors", 0), "uploads": truth["uploads"], "captures": truth["captures"]},
                "she_did_not_see": {"not_captured": truth["not_captured"], "episodes": truth["episodes"]}}

    def _publish(self) -> None:
        self._drain()
        if self.on_panel is not None:
            with contextlib.suppress(Exception):
                self.on_panel(self.panel())
