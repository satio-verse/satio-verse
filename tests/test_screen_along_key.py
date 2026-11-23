"""wm/screen_along.py 里'画面大变就立刻看一眼'的钩子 (key_change / key_gap_s; 世界2 用).

1. 默认关 (key_change=None) 时, 行为和改前逐步相同: 拿改前备份 (backups/world2-game-watch/orig/screen_along.py) 和现在的同样的剧本各跑一遍,
   整串 trace、每次问模型的时刻和内容、说的话都必须相等.
2. 开着: 整幅画面大变 -> 下一个 tick 就看一眼, 带 key 标记; 小渐变不触发; 两次大变挨得太近, 第二次记着, 等 key_gap_s 到了再看.
假时钟、假前台窗口、假截图、假模型.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest
from PIL import Image

from wm import screen_along as SA

ORIG = Path(__file__).resolve().parents[1] / "backups" / "world2-game-watch" / "orig" / "screen_along.py"


def load_orig():
    spec = importlib.util.spec_from_file_location("wm.screen_along_orig", ORIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += max(0.0, s)
        await asyncio.sleep(0)


class Rig:
    def __init__(self, gray):
        self.clock, self.gray = Clock(), gray            # gray(t) -> 0..255 此刻的画面灰度
        self.t0 = self.clock.t
        self.events, self.thought, self.thought_t, self.said = [], [], [], []

    def look(self):
        return {"exe": "somegame.exe", "title": "某个游戏", "rect": (0, 0, 1920, 1080), "minimized": False,
                "screen_area": 1920 * 1080}

    def grab(self, rect, width):
        g = self.gray(self.clock.t - self.t0)
        return Image.new("RGB", (320, 180), (g, g, g))

    async def think(self, ctx):
        self.thought.append({k: v for k, v in ctx.items()})
        self.thought_t.append(round(self.clock.t - self.t0, 1))
        return "happy", "这一幕有意思", "看到了"

    async def speak(self, note, reply, emo):
        self.said.append((round(self.clock.t - self.t0, 1), reply))
        return True

    def make(self, mod, **conf):
        async def on_end(s):
            pass
        return mod.ScreenAlong(conf, speak=self.speak, think=self.think, look=self.look, grabber=self.grab, clock_fn=self.clock.now,
                               sleep=self.clock.sleep, on_end=on_end, trace=self.events.append, threaded=False)


def run_until(mod, gray, seconds, **conf):
    rig = Rig(gray)
    w = rig.make(mod, **conf)

    async def go():
        w.start()
        for _ in range(100_000):
            if w.task.done() or rig.clock.t - rig.t0 >= seconds:
                break
            await asyncio.sleep(0)
        await w.stop("user")
    asyncio.run(go())
    return rig


def wavy(t):
    """每个 tick 都在变 (平均差大), 但没有'大变'的设定可看: 默认关时要和改前完全一样."""
    return int(t * 17) % 200 + 20


@pytest.mark.skipif(not ORIG.exists(), reason="没有改前备份")
@pytest.mark.parametrize("conf", [{}, {"tick_s": 1.0, "first_look_s": 2.0, "look_s": 25.0, "min_gap_s": 6.0}])
def test_default_off_is_step_for_step_the_same_as_before(conf):
    old, new = run_until(load_orig(), wavy, 90, **conf), run_until(SA, wavy, 90, **conf)
    assert old.events == new.events and old.thought_t == new.thought_t and old.said == new.said
    assert old.thought == new.thought                           # 连给模型的整段 (含拼图) 都相同
    assert old.events and all("key" not in e for e in new.events) and all("key" not in c for c in new.thought)


def jumpy(t):
    if t < 14:
        return 40
    if t < 30:
        return 120
    if t < 50:
        return 130                                              # 小渐变 (差 10, 比'画面变了'的 4 大, 比'大变'的 28 小)
    if t < 53:
        return 30
    return 200


CONF = {"tick_s": 1.0, "first_look_s": 2.0, "look_s": 25.0, "min_gap_s": 1.0, "first_gap_s": 1.0, "key_change": 28.0, "key_gap_s": 6.0}


def keys(rig):
    return [round(e.get("t", 0), 1) for e in rig.events if e["event"] == "look" and e.get("key")]


def test_key_jump_looks_at_once_small_drift_does_not_and_close_jumps_wait_for_the_gap():
    rig = run_until(SA, jumpy, 70, **CONF)
    key_times = [t for t, c in zip(rig.thought_t, rig.thought) if c.get("key")]
    assert key_times and 14 <= key_times[0] <= 15.5                        # 14 秒那次大变: 下一个 tick 就看
    assert not [t for t in rig.thought_t if 16 <= t <= 38]                 # 30 秒的小渐变 (差 10) 不触发, 定时看还没到 (上次 15 + 25)
    assert [t for t in key_times if 50 <= t <= 53.5]                       # 50 秒那次: 距上次 key 看超过 6 秒 -> 立刻
    late = [t for t in key_times if t > 53.5]
    assert late and 56 <= late[0] <= 58                                    # 53 秒那次挨得太近 (3 秒), 记着, 等 key_gap_s 到了再看
    ev = [e for e in rig.events if e["event"] == "look" and e.get("key")]
    assert ev and all(e["key"] is True for e in ev)


def test_key_off_the_same_jump_gets_no_early_look():
    rig = run_until(SA, jumpy, 40, **{**CONF, "key_change": None})
    assert not [t for t in rig.thought_t if 5 <= t <= 24]                  # 默认: 要等定时看
    assert all(not c.get("key") for c in rig.thought)


def test_key_look_respects_the_hourly_budget():
    rig = run_until(SA, jumpy, 70, **{**CONF, "max_looks_per_hour": 2})
    assert len(rig.thought_t) <= 2
