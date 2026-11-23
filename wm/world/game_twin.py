"""游戏世界的双胞胎夹具运行器, 加合成画面用的替身. 样板: 主程序不引用, 宿主第 1 步可以替换. 契约 perception.twin_fixtures;
记忆 cognition-truth-split.

夹具 (JSON) 是一条时间线: 什么时候前台是哪个窗口、游戏画面是哪一幕 (windows / scenes 里登记, 画面是纯色合成帧).
A、B 两个世界各自跑一遍**真实的** GameWatch (真 ScreenAlong、真门、真世界会话; 只有前台窗口、截图、模型、时钟、说话是假的),
然后逐步比较: 她收到的事件、发给模型的整段提示、**拼图的哈希**、她要做的动作、她说出口的话.
  expect = "same":   两个世界只差她感知不到的地方 (比如切去的是哪一类被跳过的窗口) -> 每一步都必须完全相同;
  expect = "differ": 差在她感知得到的地方 (游戏画面) -> 至少有一步不同.
不碰真屏幕、不联网、不调真模型.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
from dataclasses import dataclass, field

from .envelope import WorldSpec
from .game_gate import GameGate
from .game_watch import GameWatch
from .twin import Step, TwinReport, _diff

FRAME = (320, 180)       # 合成帧的大小 (小于拼图的格子上限, 拼图里每格就是这个大小)
BG = (16, 16, 16)        # SA.sheet 拼图的底色
PALETTE = [((20, 30, 120), "夜晚的对话场景"), ((240, 200, 60), "明亮的战斗场景"),
           ((40, 160, 60), "绿色的选项画面"), ((0, 0, 0), "黑屏转场")]


def _near(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def scene_label(color) -> str:
    return min(PALETTE, key=lambda p: _near(p[0], color))[1]


def sheet_labels(content) -> list[str]:
    """消息里那张拼图, 逐格看一眼是哪一幕 (从左到右、从上到下); 没有图返回空."""
    if not isinstance(content, list):
        return []
    url = next((p["image_url"]["url"] for p in content if isinstance(p, dict) and p.get("type") == "image_url"), "")
    if not url.startswith("data:image/jpeg;base64,"):
        return []
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
    cw, ch = FRAME
    out = []
    for r in range(max(1, im.height // ch)):
        for c in range(max(1, im.width // cw)):
            px = im.getpixel((c * cw + cw // 2, r * ch + ch // 2))
            if _near(px, BG) >= 10:
                out.append(scene_label(px))
    return out


class SceneLLM:
    """确定的假模型: 只看拼图 (和文字里的事件), 回什么只取决于它们. 画面没变就不说话 (只可能记一笔)."""

    def __init__(self):
        self.last: str | None = None
        self.calls: list[list] = []

    async def stream(self, messages, schema=None, max_tokens=0, id_slot=None, meta=None):
        self.calls.append(messages)
        content = messages[-1]["content"]
        text = content[0]["text"] if isinstance(content, list) else str(content)
        labels = sheet_labels(content)
        acts: list = []
        if labels:
            label = labels[-1]
            first = "first=true" in text
            if "owner_said" in text:
                said = text.split("owner_said] ", 1)[1].splitlines()[0].lstrip(": ") if "owner_said] " in text else ""
                acts.append({"name": "game_say", "args": {"text": f"嗯，{said[:12]}，我看着{label}呢", "emo": "happy"}})
            elif first:
                acts.append({"name": "game_say", "args": {"text": f"开局了，是{label}", "emo": "happy"}})
            elif label != self.last:
                acts.append({"name": "game_say", "args": {"text": f"这一幕换成了{label}", "emo": "surprised"}})
            if label != self.last:
                acts.append({"name": "game_note", "args": {"text": label}})
            self.last = label
        yield json.dumps({"actions": acts[:2]}, ensure_ascii=False)


class VClock:
    def __init__(self):
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += max(0.0, s)
        await asyncio.sleep(0)


class ScriptedScreen:
    """假的前台窗口和假的截图. 记下每次取景 (矩形, 当时的前台 exe 和标题)."""

    def __init__(self, fixture: dict, variant: str, clock: VClock):
        self.fx, self.clock, self.t0 = fixture, clock, clock.t
        self.script = [e for e in fixture["timeline"] if variant in e.get("only", ["A", "B"])]
        self.looks = 0
        self.grabs: list[tuple] = []

    def state(self) -> tuple[dict, tuple]:
        el = self.clock.t - self.t0
        win, scene = None, None
        for e in self.script:
            if e["at"] <= el:
                win, scene = e.get("window", win), e.get("scene", scene)
        w = self.fx["windows"][win]
        color = w.get("color") or self.fx["scenes"][scene]
        return w, tuple(color)

    def look(self) -> dict:
        self.looks += 1
        w, _ = self.state()
        return {"exe": w["exe"], "title": w["title"], "rect": tuple(w["rect"]), "minimized": False, "screen_area": 1920 * 1080}

    def grab(self, rect, width):
        from PIL import Image
        w, color = self.state()
        self.grabs.append((tuple(rect), w["exe"], w["title"]))
        return Image.new("RGB", FRAME, color)


@dataclass
class RunResult:
    watch: GameWatch
    screen: ScriptedScreen
    clock: VClock
    said: list = field(default_factory=list)      # (第几秒, 说的话)
    steps: list = field(default_factory=list)
    llm: object = None
    calls: list = field(default_factory=list)     # fixture 里 owner_calls 的结果: (第几秒, 他说的话, 有没有处理, 回他的话)


def build(fixture: dict, variant: str, spec: WorldSpec, env_prompt: str = "", *, gate_factory=GameGate, llm_factory=SceneLLM,
          watch_factory=GameWatch, module_capture: bool = False, **kw) -> RunResult:
    """装好一个 GameWatch 和它的假窗口、假截图、假模型、假时钟, 还没开始. module_capture=True: 不把假的 look / grabber 传进去,
    调用方自己把 SA.foreground / SA.grab 换成 result.screen.look / .grab (测"默认就走已有通路")."""
    clock = VClock()
    screen = ScriptedScreen(fixture, variant, clock)
    said: list = []

    async def speak(note, reply, emo) -> bool:
        said.append((round(clock.t - screen.t0, 1), reply))
        return True

    ids = iter(range(1, 100_000))
    llm = llm_factory()
    w = watch_factory(spec, llm, speak, env_prompt=env_prompt, look=None if module_capture else screen.look,
                      grabber=None if module_capture else screen.grab, clock_fn=clock.now, sleep=clock.sleep, threaded=False,
                      boundary=lambda: f"{next(ids):06d}", gate=gate_factory(), **kw)
    return RunResult(w, screen, clock, said, [], llm)


async def drive(r: RunResult, fixture: dict, owner_text: str = "陪我玩游戏") -> RunResult:
    """叫她陪, 按时间线走到 run_s (或她自己结束), 没结束的叫她回来; 收集每一步."""
    w, clock, screen = r.watch, r.clock, r.screen
    ok, _ = await w.owner_call(owner_text)
    assert ok, "叫她陪没有开始"
    owner_says = sorted(fixture.get("owner_says", []), key=lambda o: o["at"])
    owner_calls = sorted(fixture.get("owner_calls", []), key=lambda o: o["at"])
    for _ in range(400_000):
        el = clock.t - screen.t0
        while owner_says and owner_says[0]["at"] <= el:
            w.heard(owner_says.pop(0)["text"])
        while owner_calls and owner_calls[0]["at"] <= el:
            c = owner_calls.pop(0)
            r.calls.append((round(el, 1), c["text"], *(await w.owner_call(c["text"]))))
        if el >= fixture["run_s"] or not w.alive:
            break
        await asyncio.sleep(0)
    if w.alive:
        await w.leave(speak_line=False)
    r.steps = [Step("look", [], tr["user"] + "\n#image:" + tr.get("image", ""), [tuple(a) for a in tr["actions"]])
               for tr in w.session.trace]
    events = [(i["type"], i["fields"], i["text"]) for i in w.log if i["k"] == "in"]
    r.steps.append(Step("end", events, None, [("said", t) for _, t in r.said]))
    return r


async def run_variant(fixture: dict, variant: str, spec: WorldSpec, env_prompt: str = "", *, owner_text: str = "陪我玩游戏",
                      **kw) -> RunResult:
    return await drive(build(fixture, variant, spec, env_prompt, **kw), fixture, owner_text)


def run_game_twin(fixture: dict, spec: WorldSpec, env_prompt: str = "", **kw) -> TwinReport:
    async def both():
        return (await run_variant(fixture, "A", spec, env_prompt, **kw), await run_variant(fixture, "B", spec, env_prompt, **kw))

    a, b = asyncio.run(both())
    return TwinReport(fixture.get("name", ""), fixture.get("expect", "same"), a.steps, b.steps, _diff(a.steps, b.steps))
