"""游戏世界测试的小工具 (世界2). 只在 pytest 里用.

PLUGIN / load():  插件包目录、清单、规格、环境提示词.
make_fx():        在插件自带夹具的窗口和场景表上拼一条时间线 (合成画面, 不碰真屏幕).
run():            用 wm.world.game_twin 的假窗口 / 假截图 / 假模型 / 假时钟跑一遍真实的 GameWatch.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from wm.world import game_twin as T
from wm.world.game_watch import load_package

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "game-watch"


def load():
    return load_package(PLUGIN)


def fixture(name: str) -> dict:
    return json.loads((PLUGIN / "fixtures" / (name + ".json")).read_text(encoding="utf-8"))


def make_fx(timeline: list, run_s: float, windows: dict | None = None, **extra) -> dict:
    base = fixture("twin-private-flicker")
    return {"name": "adhoc", "expect": "same", "run_s": run_s, "frame": [320, 180],
            "windows": {**base["windows"], **(windows or {})}, "scenes": base["scenes"], "variants": {"A": {}, "B": {}},
            "timeline": timeline, **extra}


def run(fx: dict, variant: str = "A", **kw) -> T.RunResult:
    _, spec, env = load()
    return asyncio.run(T.run_variant(fx, variant, spec, env, **kw))
