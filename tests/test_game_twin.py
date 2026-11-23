"""游戏世界·双胞胎夹具 (世界2 验收 3: 她知道的只有她从画面里看得到的; 记忆 cognition-truth-split).

夹具跑的是真实的 GameWatch (真 ScreenAlong、真门、真世界会话), 只有前台窗口、截图、模型、时钟是假的.
比较的是: 她收到的全部事件、发给模型的整段提示、**拼图的哈希**、她的动作、她说出口的话.
两面都测: 差在她感知不到的地方 (切去的是哪一类被跳过的窗口) -> 必须逐步相同; 差在她感知得到的地方 (游戏画面) -> 必须不同.
还要有"泄露替身": 故意把门弄坏的三种办法, 双胞胎比对必须判得出来, 不然比对是空转.
"""
import asyncio
import dataclasses

import pytest

from game_fakes import PLUGIN, fixture, load, make_fx, run
from wm import screen_along as SA
from wm.world import game_twin as T
from wm.world.game_gate import GameGate

NAMES = [p.stem for p in sorted((PLUGIN / "fixtures").glob("*.json"))]


def twin(name_or_fx, spec=None, env="", **kw):
    _, s, e = load()
    fx = fixture(name_or_fx) if isinstance(name_or_fx, str) else name_or_fx
    return T.run_game_twin(fx, spec or s, env or e, **kw)


@pytest.mark.parametrize("name", NAMES)
def test_fixture_passes_both_ways(name):
    rep = twin(name)
    assert rep.ok, (name, rep.expect, rep.diffs)
    assert len([s for s in rep.steps_a if s.prompt]) >= 1            # 不是空转: 她真的看了、真的问了模型


def test_same_fixtures_actually_exercise_the_hidden_things():
    flicker = twin("twin-private-flicker")
    assert flicker.steps_a[-1].events == flicker.steps_b[-1].events
    away = twin("twin-away-reason")
    types = [e[0] for e in away.steps_a[-1].events]
    assert "game_away" in types and types[-1] == "game_ended"       # 切走够久, 她知道'他看别处去了', 不知道去了哪
    assert away.steps_a[-1].events == away.steps_b[-1].events
    ga = [e for e in away.steps_a[-1].events if e[0] == "game_away"][0]
    assert ga[1] == {} and ga[2] == ""                              # 不带任何原因
    _, spec, env = load()
    a = asyncio.run(T.run_variant(fixture("twin-away-reason"), "A", spec, env))
    b = asyncio.run(T.run_variant(fixture("twin-away-reason"), "B", spec, env))
    assert a.watch.panel()["she_did_not_see"]["not_captured"].keys() != b.watch.panel()["she_did_not_see"]["not_captured"].keys()
    # 面板(主人看)确实知道区别, 她不知道


def test_scene_difference_is_noticed_by_her():
    rep = twin("twin-scene-differs")
    assert rep.diffs and rep.ok
    assert rep.steps_a[1].prompt != rep.steps_b[1].prompt or rep.steps_a[-1].actions != rep.steps_b[-1].actions


def test_comparison_is_not_idle_same_expectation_on_different_pictures_fails():
    fx = fixture("twin-scene-differs")
    fx["expect"] = "same"
    assert not twin(fx).ok


def test_short_flicker_vs_staying_makes_no_event_at_all():
    """不是双胞胎 (切走和没切走, 差在她看得到的画面上): 只验'短于 away_tell_s 的切走不产生任何事件'."""
    tl = [{"at": 0, "window": "game", "scene": "dialog"}]
    away = make_fx(tl + [{"at": 12, "window": "qq"}, {"at": 18, "window": "game"}], 30)
    stay = make_fx(tl, 30)
    ra, rb = run(away), run(stay)
    types = lambda r: [e["type"] for e in r.watch.log if e["k"] == "in"]       # noqa: E731
    assert "game_away" not in types(ra) and "game_back" not in types(ra)
    assert types(ra) == types(rb)


# -- 泄露替身: 门被改坏, 双胞胎必须判出来 -------------------------------------------------------------------------

class LeakyGate(GameGate):
    """game_away 偷偷带上被跳过窗口的东西 (field: reason = 它是哪一类; title = 它的标题)."""

    def __init__(self, field: str):
        super().__init__()
        self.field = field

    def observe(self, info, now):
        out = super().observe(info, now)
        for e in out:
            if e["type"] == "game_away":
                e["fields"][self.field] = SA.watchable(info, info.get("screen_area"))[1] if self.field == "reason" else info["title"]
        return out


def sloppy_spec(spec, field):
    """清单也写松了: game_away 声明了这个字段 (两头都松, 才能漏出去)."""
    ev = spec.events["game_away"]
    return dataclasses.replace(spec, events={**spec.events, "game_away": dataclasses.replace(ev, fields=(field,))})


@pytest.mark.parametrize("field", ["reason", "title"])
def test_leaky_stand_in_gate_is_caught_by_the_away_fixture(field):
    _, spec, env = load()
    leaky_spec = sloppy_spec(spec, field)
    clean = twin("twin-away-reason")
    assert clean.ok
    rep = twin("twin-away-reason", leaky_spec, env, gate_factory=lambda: LeakyGate(field))
    assert not rep.ok and rep.diffs


def test_leaky_stand_in_that_captures_private_windows_is_caught_by_the_flicker_fixture(monkeypatch):
    assert twin("twin-private-flicker").ok
    monkeypatch.setattr(SA, "watchable", lambda info, area=None: (True, "ok"))        # 私密窗口也当成能看
    rep = twin("twin-private-flicker")
    assert not rep.ok and rep.diffs                                                   # 拼图哈希不同
