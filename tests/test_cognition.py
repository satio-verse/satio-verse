"""认知与真值分离 (wm/cognition/, 阶段 1): 她只能靠看、找、问知道东西在哪.

- 真小窝对得上: 地点、抽屉、杯子的编号和名字取自场景表和交互表;
- 她那边的代码碰不到世界 (不 import roomworld);
- 没看到的变化, 认知不变; 当面挪、当面拿走、问来的, 各按来源记;
- 经验: 越新的越算数, 习惯变了改得过来, 可以撤销;
- 双胞胎世界: 老实的做法零泄露, 两种故意作弊的被抓出来; 比对规则本身;
- 回放: 事件流存成 JSONL 再重放, 认知一模一样;
- 找东西: 有经验以后步数下降, 换了习惯能改过来; 问她"杯子在哪"时说的老实;
- 评测可复现 (同样的参数跑两遍一样), 命令行能讲故事、存事件流、重放.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from wm.cognition import cup as U
from wm.cognition import layout as Y
from wm.cognition.belief import Cognition, Experience
from wm.cognition.mind import Mind
from wm.cognition.roomworld import RoomWorld

ROOT = Path(__file__).resolve().parent.parent
PINK = U.TARGET


def _walk(world: RoomWorld, cog: Cognition, *actions):
    for a in actions:
        cog.perceive(world.act(a), a)


def test_layout_matches_real_room():
    if not (Y.SCENE.exists() and Y.INTERACTIONS.exists()):
        pytest.skip("没有真小窝的场景文件")
    assert Y.check_scene() == []


def test_her_side_never_imports_the_world():
    for name in ("belief.py", "mind.py", "events.py", "layout.py"):
        src = (ROOT / "wm/cognition" / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+\S*roomworld", src, re.M), name
    with pytest.raises(ValueError):
        Mind("belief", peek=lambda: {})  # 老实的做法不许拿到读真值的函数
    with pytest.raises(ValueError):
        Mind("oracle")


def test_hidden_move_leaves_belief_alone_until_she_looks():
    world, cog = RoomWorld(U.START, here="coffee"), Cognition()
    cog.perceive(world.observe())
    _walk(world, cog, ["go", "reading"], ["wait", 600])
    before = cog.digest()
    assert world.move(PINK, "IR_Drawer_Desk") is None  # 她在阅读角, 看不到
    _walk(world, cog, ["wait", 600])
    f = cog.belief.fact(PINK)
    assert (f["at"], f["how"]) == ("coffee_machine", "seen")
    text = cog.belief.describe(PINK)
    assert "最后看到" in text and "咖啡机上" in text and "电脑桌文具抽屉" not in text
    assert before != cog.digest()  # 时间在走 (认知摘要里有时刻), 但东西在哪一点没变
    _walk(world, cog, ["go", "coffee"])
    f = cog.belief.fact(PINK)
    assert f["how"] == "missing" and f["at"] is None and f["lost_from"] == "coffee_machine"
    assert {"coffee_machine", "water_caddy"} <= set(f["ruled_out"])
    assert "不在了" in cog.belief.describe(PINK)
    _walk(world, cog, ["go", "desk"], ["open", "IR_Drawer_Desk"])
    f = cog.belief.fact(PINK)
    assert (f["at"], f["how"]) == ("IR_Drawer_Desk", "seen")
    assert [(r["lost_from"], r["found_at"]) for r in cog.exp.records] == [("coffee_machine", "IR_Drawer_Desk")]


def test_witnessed_and_taken_and_told():
    # 当着她的面放到小边桌上: 马上知道
    world, cog = RoomWorld(U.START, here="coffee"), Cognition()
    cog.perceive(world.observe())
    _walk(world, cog, ["go", "reading"])
    cog.perceive(world.move(PINK, "surf_table"))
    assert cog.belief.fact(PINK)["at"] == "surf_table"
    assert "正看着" in cog.belief.describe(PINK)
    # 当着她的面拿走、放到她看不到的地方: 知道被拿走了, 不知道放哪
    world, cog = RoomWorld(U.START, here="coffee"), Cognition()
    cog.perceive(world.observe())
    cog.perceive(world.move(PINK, "IR_Drawer_Bedside_Upper"))
    f = cog.belief.fact(PINK)
    assert f["how"] == "taken" and f["at"] is None and "拿走" in cog.belief.describe(PINK)
    # 问你: 你说的是听来的, 她去看了才算看到
    _walk(world, cog, ["ask", PINK])
    f = cog.belief.fact(PINK)
    assert (f["at"], f["how"]) == ("IR_Drawer_Bedside_Upper", "told")
    assert "还没去看过" in cog.belief.describe(PINK)
    _walk(world, cog, ["go", "bed"], ["open", "IR_Drawer_Bedside_Upper"])
    assert cog.belief.fact(PINK)["how"] == "seen"
    assert cog.exp.records[-1]["found_at"] == "IR_Drawer_Bedside_Upper"


def test_experience_prefers_recent_and_can_be_revoked():
    e = Experience()
    for k in range(5):
        e.record(PINK, U.HOME, U.H1, t=k, ev=k)
    top = lambda: max(e.scores("cup").items(), key=lambda kv: kv[1])[0]  # noqa: E731
    assert top() == U.H1
    e.record(PINK, U.HOME, U.H2, t=10, ev=10)
    assert top() == U.H1  # 一次例外不改
    e.record(PINK, U.HOME, U.H2, t=11, ev=11)
    assert top() == U.H2  # 连着两次才改
    assert e.revoke({10, 11}) == 2 and top() == U.H1 and U.H2 not in e.scores("cup")
    assert "电脑桌文具抽屉找到过 5 次" in e.lines("cup")[0]


@pytest.mark.parametrize("policy", ["belief", "experience", "random"])
def test_twin_worlds_no_leak(policy):
    hist = tuple(U.habit_moves(0, 2, switch=2))
    for a, b in U.twin_pairs():
        for h in ((), hist):
            r = U.twin(policy, a, b, history=h)
            assert not r["leak"], (policy, a, b, h, r)


def test_twin_worlds_catch_the_cheats():
    pairs = U.twin_pairs()
    assert all(U.twin("oracle", a, b)["leak"] for a, b in pairs)
    # 照现在的写法: 一边杯子还在咖啡机上 (没挪) 时, 人没过去就知道另一边不在了 -> 候选不一样
    r = [U.twin("truth_menu", None, b) for b in U.hidden_targets()]
    assert all(x["leak"] and x["field"] == "options" for x in r)
    # 两边都不在咖啡机上: 它都直接放弃, 行为一样, 比不出来 (双胞胎只证明"没用到差别")
    assert not U.twin("truth_menu", "IR_Drawer_Desk", "fridge")["leak"]


def test_compare_rules():
    obs = lambda x: {"kind": "obs", "obs": {"t": x}}  # noqa: E731
    dec = lambda x: {"kind": "decide", "belief": "b", "options": [x], "prompt": "p", "action": ["go", x]}  # noqa: E731
    assert U.compare([obs(1), dec("a")], [obs(2), dec("b")]) == {"leak": False, "at": 0, "field": "obs"}
    r = U.compare([obs(1), dec("a")], [obs(1), dec("b")])
    assert r["leak"] and r["at"] == 1 and r["field"] == "options"
    assert U.compare([obs(1), dec("a")], [obs(1)])["leak"]  # 一边多做了一个决定
    assert not U.compare([obs(1)], [obs(1), obs(2)])["leak"]


def test_replay_from_jsonl(tmp_path):
    for policy in ("experience", "random", "truth_menu"):
        _, mind, _ = U.run_days(policy, U.habit_moves(3, 6, switch=3), seed=3)
        path = mind.cog.log.save(tmp_path / f"{policy}.jsonl")
        again = Cognition.replay(mind.cog.log.load(path))
        assert again.digest() == mind.cog.digest()
        assert again.exp.records == mind.cog.exp.records


def test_learning_and_adapting():
    days, mind, _ = U.run_days("experience", [U.H1] * 5 + [U.H2] * 4)
    steps = [d["steps"] for d in days]
    assert all(d["found"] and not d["asked"] for d in days)
    assert steps[0] > steps[2] and max(steps[2:5]) <= 4  # 第 3 天起: 咖啡机上没有 -> 直接去电脑桌文具抽屉
    assert steps[5] > steps[4]                            # 换了习惯, 头一天又得找
    assert max(steps[7:]) <= 4                            # 两三天以后改过来
    base, _, _ = U.run_days("belief", [U.H1] * 5)
    assert sum(d["steps"] for d in base) > sum(steps[:5])  # 没有经验的天天从头找


def test_answers_are_honest_and_current_code_gives_up():
    for to in U.hidden_targets():
        d = U.run_days("belief", [to])[0][0]
        assert d["honest"] and Y.region_name(to) not in d["answer"], (to, d["answer"])
        assert d["found"] and d["belief_ok"]
        c = U.run_days("truth_menu", [to])[0][0]
        assert not c["found"] and c["gave_up"] == "咖啡杯不在咖啡机上" and c["steps"] == 0
    d = U.run_days("truth_menu", [None])[0][0]
    assert d["found"] and d["steps"] == 2  # 没挪: 走过去拿


def test_bench_reproducible_and_sane():
    kw = dict(seeds=2, habit_days=6, switch=3, twin_history=2)
    r = U.bench(**kw)
    assert r == U.bench(**kw)
    for p in ("belief", "experience", "random"):
        assert r["twin"][p]["leaks"] == 0
    assert r["twin"]["oracle"]["leaks"] == r["twin"]["oracle"]["pairs"]
    assert r["twin"]["truth_menu"]["leaks"] > 0
    assert r["replay"]["mismatch"] == 0
    assert r["single"]["oracle"]["steps"] < r["single"]["belief"]["steps"]
    assert r["single"]["belief"]["honest"] == 1


def test_cli_story_save_replay(tmp_path):
    tool = ROOT / "tools/bench_cognition.py"
    log = tmp_path / "story.jsonl"
    env = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    out = subprocess.run([sys.executable, str(tool), "--story", "--days", "2", "--save", str(log)],
                         capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "趁她不在" in out.stdout and "找到了" in out.stdout and log.exists()
    out = subprocess.run([sys.executable, str(tool), "--replay", str(log)],
                         capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "电脑桌文具抽屉找到过 2 次" in out.stdout
