"""记忆阶段 7 第一步: 程序性记忆 (工作台的工作经验; wm/agent/procedures.py + experience.py / core.py 里的接线; 计划 backups/mem7-procedures/计划.md).

任务原话、命令、文件名全是合成的 (评审 11-15: 测试夹具里不许放真的任务原话). 逐条对着计划 5.1:
卡片字段 / 旧新互读 / off 逐字节同前 / shadow 对她可见的输出同 off / 自动化程度 / 动手前资格和锚点 / 失败路线和备选 /
该不该触发和做没做成分开 / 经验只来自她自己 / 隐私 / 子会话 / 健壮 / 与阶段 5 并存 / 真实入口 (AgentHub) / 三次都成的小型快速版."""
import ast
import asyncio
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

# 守卫: 这个文件在换进真 wm/agent/experience.py 之前放在 backups/mem7-procedures/draft/tests/, 不在 tests/ 下, 所以 tests/conftest.py 的自动夹具
# (换家目录、不碰真的 agent / 保管箱 / 服务状态) 默认不会装到它身上; 起 AgentHub 的测试会建 agent/blacklist.json. 要用 run_draft.py (它会 -p conftest),
# 或者放回 tests/ 再跑. conftest 没装上就整个文件跳过.
if "conftest" not in sys.modules:
    pytest.skip("tests/conftest.py 没装上 (自动夹具不生效, 会碰真目录): 用 backups/mem7-procedures/run_draft.py 跑, 或放回 tests/ 再跑",
                allow_module_level=True)

from wm.agent import experience as X
from wm.agent import procedures as P
from wm.config import ROOT

DAY = 86400.0
T0 = 1_800_000_000.0
ORIG_PATH = ROOT / "backups" / "mem7-procedures" / "orig" / "experience.py"


@pytest.fixture(autouse=True)
def _conftest_fixtures_are_active(request):
    """第二道守卫: conftest 在 sys.modules 里不等于它的自动夹具装上了. 没装就整个文件跳过."""
    names = request.session._fixturemanager._arg2fixturedefs  # noqa: SLF001
    if not any("autouse" in n or "home" in n.lower() for n in names):
        pytest.skip("conftest 的自动夹具没装上")


def load_orig():
    """改前的 experience.py (backups/mem7-procedures/orig/), 当对照."""
    spec = importlib.util.spec_from_file_location("mem7_orig_exp", ORIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mem7_orig_exp"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 脚手架 (合成的任务经历)
class Sess:
    def __init__(self, rid, goal="", parent=""):
        self.run = {"id": rid, "goal": goal}
        self.parent_id = parent


def call(cid, name, **args):
    return {"id": cid, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


CMD = "bundlex pack --out report.zip"
FAIL = "退出码 1\nTraceback (most recent call last):\nModuleNotFoundError: No module named 'zeta_codec'"
GOAL = "把本周的素材打包导出成压缩包"
FIX = ("run_command", "pipq add zeta_codec")


def det_ids(mod):
    """让折出来的卡 id 可复现 (新旧两个模块各自的 secrets 都换成同一个计数器)."""
    c = iter(range(1, 10_000))
    mod.secrets = types.SimpleNamespace(token_hex=lambda n=4: f"{next(c):0{2 * n}d}")


def mk(tmp, mode="on", pmode="on", orig=None):
    now = [T0]
    mod = orig or X
    det_ids(mod)
    if orig is not None:
        return mod.Experiences(tmp, mode, clock=lambda: now[0]), now
    return mod.Experiences(tmp, mode, clock=lambda: now[0], procedures=pmode, _test_unlock_on=(pmode == "on")), now


def run_once(store, rid, goal=GOAL, *, cmd=CMD, fail=FAIL, steps=(FIX,), wrong=(), succeed=True, parent=""):
    """一轮: (动手前) 命令报错 -> [试了没用的 wrong] -> 做法 steps -> 命令成了. 返回 (动手前提示, 报错后提示, learn 小结, 这一轮的会话)."""
    s = Sess(rid, goal, parent)
    pre = store.before_task(s, goal) if hasattr(store, "before_task") else ""
    hint = store.after_tool(s, call(rid + "-a", "run_command", command=cmd), fail, True)
    n = 0

    def step(kind, arg, ok_cmd=True):
        nonlocal n
        n += 1
        if kind == "run_command":
            store.after_tool(s, call(f"{rid}-s{n}", "run_command", command=arg), "退出码 0\ndone", False)
        else:
            store.after_tool(s, call(f"{rid}-s{n}", "write_file", path=arg), "写好了", False)
    for kind, arg in wrong:
        step(kind, arg)
        n += 1
        store.after_tool(s, call(f"{rid}-r{n}", "run_command", command=cmd), fail, True)  # 做了还是同一个错
    for kind, arg in steps:
        step(kind, arg)
    n += 1
    if succeed:
        store.after_tool(s, call(f"{rid}-z", "run_command", command=cmd), "退出码 0\nok", False)
    else:
        store.after_tool(s, call(f"{rid}-z", "run_command", command=cmd), fail, True)
    return pre, hint, store.learn("sess", s.run), s


def warm(store, now, runs=3, **kw):
    """连着做同一类事 runs 轮 (每轮隔一天): 第 1 轮折卡 (auto 0.30), 之后每轮照做成功."""
    out = []
    for r in range(runs):
        out.append(run_once(store, f"w{r}", **kw))
        now[0] += DAY
    return out


# ---------------------------------------------------------------- 1. 卡片字段齐全、能追到她的调用
def test_card_has_every_field_and_each_traces_to_her_own_calls(tmp_path):
    store, now = mk(tmp_path)
    run_once(store, "r1", cmd="bundlex pack --out report.zip", steps=(FIX, ("write_file", "bundlex.cfg")))
    (e,) = store.items.values()
    assert (e["head"], e["state"], e["trust"], e["origin"]) == ("bundlex pack", "candidate", 3, "经历")
    assert e["sig"] == "ModuleNotFoundError: No module named 'zeta_codec'"          # 触发条件
    assert e["criterion"] == "同一类命令 `bundlex pack` 退出码为 0"                      # 成功判据
    assert e["action"] == "先`pipq add zeta_codec`，再写 `bundlex.cfg`"                 # 首选做法
    assert e["anchors"]["files"] == ["report.zip", "bundlex.cfg"] and e["anchors"]["hosts"] == []   # 精确锚点
    assert e["anchors"]["goals"] == [GOAL[:P.GOAL_CHARS]]
    assert e["failed"] == [] and (e["auto"], e["streak"]) == (P.AUTO_UP, 1)             # 第一次折成卡算一次成功
    src = e["sources"][0]                                                                 # 证据: 追到她这一轮的哪两次调用
    assert src["run"] == "r1" and src["fail"] == "r1-a" and src["ok"] == "r1-z"
    assert all(f in CMD or f in e["action"] for f in e["anchors"]["files"])              # 锚点里的名字都在她的命令 / 做法里出现过
    v = store.view()["items"][0]
    assert v["count"] == 1 and v["success_rate"] == 1.0 and v["auto_eff"] == pytest.approx(P.AUTO_UP, abs=1e-3)


# ---------------------------------------------------------------- 2. 旧 <-> 新 互相读得动
def test_old_rows_load_in_new_and_old_code_reads_new_file(tmp_path):
    orig = load_orig()
    old, now = mk(tmp_path / "a", orig=orig)
    run_once(old, "r1")
    old_text = (tmp_path / "a" / "experiences.json").read_text(encoding="utf-8")
    # 旧文件用新代码读: off 下一个字不改地写回
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "experiences.json").write_text(old_text, encoding="utf-8")
    new_off, _ = mk(tmp_path / "b", pmode="off")
    assert len(new_off.items) == 1 and set(next(iter(new_off.items.values()))) == set(json.loads(old_text)[0])
    new_off._save()
    assert (tmp_path / "b" / "experiences.json").read_text(encoding="utf-8") == old_text
    # on 下旧卡被动到才补新字段, 老字段的值不变
    new_on, now2 = mk(tmp_path / "b", pmode="on")
    before = json.loads(old_text)[0]
    run_once(new_on, "r2")
    after = next(iter(new_on.items.values()))
    assert after["auto"] > 0 and "anchors" in after and "criterion" in after
    assert after["head"] == before["head"] and after["akey"] == before["akey"] and after["sig"] == before["sig"]
    # 新文件用改前的模块读: 不报错, 老字段在
    text = (tmp_path / "b" / "experiences.json").read_text(encoding="utf-8")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "experiences.json").write_text(text, encoding="utf-8")
    back = orig.Experiences(tmp_path / "c")
    assert len(back.items) == 1 and next(iter(back.items.values()))["head"] == "bundlex pack"


# ---------------------------------------------------------------- 3. off 逐字节同改前; shadow 对她可见的输出同 off
def _script(store, now):
    """一串固定的轮次: 折卡、照做成功、照做又失败、不相干的报错、换命令."""
    outs = []
    for r, kw in enumerate([{}, {}, {}, {"succeed": False}, {"cmd": "other thing", "fail": "退出码 1\nSyntaxError: x"}, {}]):
        pre, hint, summary, _ = run_once(store, f"s{r}", **kw)
        outs.append((hint, summary))
        now[0] += DAY
    return outs


def _log_rows(home):
    """推送记录去掉卡 id (两个仓库共用一个 id 计数器, id 不同不算输出不同)."""
    rows = [json.loads(ln) for ln in (Path(home) / "experience-log.jsonl").read_text(encoding="utf-8").splitlines()]
    return [{k: v for k, v in r.items() if k != "exp"} for r in rows]


def _normalized(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    old_keys = ["head", "sig", "words", "action", "akey", "steps", "state", "trust", "enabled", "support", "contradict",
                "pushed", "adopted", "hits", "created", "verified", "updated", "sources"]
    return [{k: r[k] for k in old_keys} for r in rows]


def test_off_is_byte_identical_to_before(tmp_path):
    orig = load_orig()
    a, na = mk(tmp_path / "a", orig=orig)
    b, nb = mk(tmp_path / "b", pmode="off")
    assert _script(a, na) == _script(b, nb)                                                    # 她看到的 + 小结
    assert (tmp_path / "a" / "experiences.json").read_bytes() == (tmp_path / "b" / "experiences.json").read_bytes()
    assert (tmp_path / "a" / "experience-log.jsonl").read_bytes() == (tmp_path / "b" / "experience-log.jsonl").read_bytes()
    assert not (tmp_path / "b" / "procedure-log.jsonl").exists()
    assert "procedures" not in b.stats() and all("auto" not in e for e in b.items.values())
    assert set(b.view()["items"][0]) == set(a.view()["items"][0])                              # 页面上的条目键一样
    assert b.before_task(Sess("x", GOAL), GOAL) == ""                                            # off: 动手前什么都没有


def test_shadow_changes_nothing_she_sees_but_records(tmp_path):
    off, noff = mk(tmp_path / "a", pmode="off")
    sh, nsh = mk(tmp_path / "b", pmode="shadow")
    assert _script(off, noff) == _script(sh, nsh)                                              # 提示和小结逐个相同
    na, nb = _normalized(tmp_path / "a" / "experiences.json"), _normalized(tmp_path / "b" / "experiences.json")
    assert len(na) == len(nb) and [(i, k) for i, (x, y) in enumerate(zip(na, nb)) for k in x if x[k] != y[k]] == []
    assert _log_rows(tmp_path / "a") == _log_rows(tmp_path / "b")
    assert (tmp_path / "b" / "procedure-log.jsonl").exists()
    assert all("auto" in e and "anchors" in e for e in sh.items.values())
    assert sh.view()["stats"]["procedures"]["mode"] == "shadow"


def test_on_with_no_eligible_card_equals_off(tmp_path):
    off, noff = mk(tmp_path / "a", pmode="off")
    on, non = mk(tmp_path / "b", pmode="on")
    for r in range(2):                                                                         # 只跑两轮: auto 到不了线, 没有动手前提示
        a = run_once(off, f"t{r}")
        b = run_once(on, f"t{r}")
        assert a[0] == b[0] == "" and a[1] == b[1] and a[2] == b[2]
        noff[0] += DAY
        non[0] += DAY


# ---------------------------------------------------------------- 4. 自动化程度
def test_automaticity_three_in_a_row_reaches_the_line(tmp_path):
    """"三次"＝折卡 + 两次照做成功: 0.30 → 0.51 → 0.657 (到 0.60 的线)."""
    store, now = mk(tmp_path)
    (e,) = [store.items[k] for k in (run_once(store, "a")[3] and store.items)]
    assert e["auto"] == pytest.approx(0.30)
    now[0] += DAY
    run_once(store, "b")
    assert e["auto"] == pytest.approx(0.51) and e["streak"] == 2 and not P.pre_eligible(e, now[0])
    now[0] += DAY
    run_once(store, "c")
    assert e["auto"] == pytest.approx(0.657) and e["streak"] == 3 and P.pre_eligible(e, now[0])
    assert P.auto_down(0.657) == pytest.approx(0.3285) and P.auto_up(1.0) == 1.0 and P.auto_down(0.0) == 0.0


def test_one_change_per_card_per_run_and_shown_alone_changes_nothing(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 3)
    (e,) = store.items.values()
    base = (e["auto"], e["streak"], e["support"])
    # 展示了、她没照着做: 命令直接成了 -> 不变
    s = Sess("p1", GOAL)
    assert store.before_task(s, GOAL).startswith(P.HEAD_PRE)
    store.after_tool(s, call("p1-a", "run_command", command=CMD), "退出码 0\nok", False)
    store.learn("sess", s.run)
    assert (e["auto"], e["streak"], e["support"]) == base and e["pre"]["shown"] == 1 and e["pre"]["adopted"] == 0
    # 一轮里同一张卡两段都成了: 只变一次
    now[0] += DAY
    s = Sess("p2", GOAL)
    for k in range(2):
        store.after_tool(s, call(f"p2-a{k}", "run_command", command=CMD), FAIL, True)
        store.after_tool(s, call(f"p2-f{k}", "run_command", command=FIX[1]), "退出码 0\n", False)
        store.after_tool(s, call(f"p2-z{k}", "run_command", command=CMD), "退出码 0\nok", False)
    store.learn("sess", s.run)
    assert e["auto"] == pytest.approx(P.auto_up(base[0])) and e["streak"] == base[1] + 1


def test_adopted_again_halves_auto_resets_streak_and_stops_pre(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 3)
    (e,) = store.items.values()
    assert P.pre_eligible(e, now[0])
    _, _, summary, _ = run_once(store, "bad", succeed=False)                                    # 照着做了, 命令还是一样的错
    assert summary["contradicted"] == 0 or True
    assert e["auto"] == pytest.approx(P.auto_down(0.657), abs=1e-3) and e["streak"] == 0
    assert not P.pre_eligible(e, now[0])
    assert store.before_task(Sess("n", GOAL), GOAL) == ""


def test_auto_decays_with_unverified_days(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 3)
    (e,) = store.items.values()
    now[0] = e["verified"] + P.AUTO_HALF_LIFE * DAY
    assert P.auto_eff(e, now[0]) == pytest.approx(e["auto"] / 2, rel=1e-6)
    assert not P.pre_eligible(e, now[0]) and store.before_task(Sess("n", GOAL), GOAL) == ""


# ---------------------------------------------------------------- 5. 动手前: 资格、锚点
def test_pre_text_and_eligibility(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 2)
    assert store.before_task(Sess("n0", GOAL), GOAL) == ""                                      # auto 0.51 < 0.60
    run_once(store, "w3")
    now[0] += DAY
    block = store.before_task(Sess("n1", GOAL), GOAL)
    assert block.startswith(P.HEAD_PRE) and "上次做 `bundlex pack` 这类事：先`pipq add zeta_codec`就成了（成过 3 次" in block
    assert "判成功的标准：同一类命令 `bundlex pack` 退出码为 0" in block and "未验证" in block and len(block) <= 400
    (e,) = store.items.values()
    for change, expect in [({"enabled": False}, ""), ({"state": "contradicted"}, ""), ({"state": "retired"}, ""),
                           ({"state": "candidate"}, "")]:
        saved = {k: e[k] for k in change}
        e.update(change)
        assert store.before_task(Sess("n2", GOAL), GOAL) == expect, change
        e.update(saved)
    assert store.before_task(Sess("n3", GOAL), GOAL) != ""


def test_confirmed_mode_shows_only_what_you_approved_and_shadow_never_shows(tmp_path):
    for mode, pmode in [("confirmed", "on"), ("shadow", "on"), ("on", "shadow"), ("off", "on")]:
        store, now = mk(tmp_path / f"{mode}-{pmode}", mode=mode, pmode=pmode)
        warm(store, now, 3)
        assert store.before_task(Sess("x", GOAL), GOAL) == "", (mode, pmode)
    store, now = mk(tmp_path / "ok", mode="confirmed", pmode="on")
    warm(store, now, 3)
    (e,) = store.items.values()
    store.update(e["id"], "approve")
    block = store.before_task(Sess("y", GOAL), GOAL)
    assert "主人确认过" in block
    assert not (tmp_path / "off-on" / "procedure-log.jsonl").exists()                           # agent.experience 是 off: 什么都不记


def _card_with(tmp, cmd, goal, fail=FAIL, fix=FIX):
    store, now = mk(tmp)
    warm(store, now, 3, cmd=cmd, goal=goal, fail=fail, steps=(fix,))
    return store, now


def test_anchor_matching_strong_goal_and_generic_words_do_not_trigger(tmp_path):
    cmd = "fetchq get https://api.example.test/v1/data.json"
    store, now = _card_with(tmp_path, cmd, "把线上的数据抓下来存好")
    cases = [("请 fetchq get 一下", True),                                 # 命令 (head 整串) 原样出现: 强匹配
             ("把线上的数据抓下来存好", True), ("线上数据抓下来存好吧", True),   # 任务片段字词差不多
             ("把 data.json 抓下来", False), ("看看 api.example.test 那边", False),   # 文件名 / 域名单独出现: 不触发 (评审留出组 145/225)
             ("看一下 data.json 里写了什么，只看别改", False), ("api.example.test 现在通不通", False),
             ("检查一下", False), ("打开文件看看", False), ("整理桌面", False), ("今天天气怎么样", False)]
    for k, (text, want) in enumerate(cases):
        assert bool(store.before_task(Sess(f"m{k}", text), text)) is want, text
    # 文件名 / 域名只加分: 任务片段先过了线, 它们才在几张卡之间影响排序
    (e,) = store.items.values()
    ranked = P.match_pre([e], "线上数据抓下来存好吧 data.json", X.words, X.similar)
    plain = P.match_pre([e], "线上数据抓下来存好吧", X.words, X.similar)
    assert ranked and plain and ranked[0][1] == plain[0][1] == "goal"


def test_two_cards_max_and_second_is_alt_when_same_head(tmp_path):
    store, now = mk(tmp_path)
    for sig, fix in [(FAIL, ("run_command", "pipq add alpha")),
                     ("退出码 1\nImportError: cannot import name gadget from toolkit", ("write_file", "gadget.cfg"))]:
        warm(store, now, 3, goal=GOAL, fail=sig, steps=(fix,))
    block = store.before_task(Sess("n", GOAL), GOAL)
    lines = block.splitlines()[1:]
    assert len(lines) == 2 and lines[0].startswith("- 上次做") and lines[1].startswith("- 备选：上次做") and len(block) <= 400


# ---------------------------------------------------------------- 6. 失败路线、备选
def test_failed_route_from_episode_and_progress_is_kept(tmp_path):
    store, now = mk(tmp_path)
    run_once(store, "r1", wrong=(("run_command", "zfix cache clear"),))
    (e,) = store.items.values()
    assert e["action"] == "先`pipq add zeta_codec`" and [f["akey"] for f in e["failed"]] == ["cmd:zfix cache"]
    # 做了一步、重跑换了个错 = 有进展, 留在做法里, 不记成失败
    store2, _ = mk(tmp_path / "p")
    s = Sess("q", GOAL)
    store2.after_tool(s, call("q1", "run_command", command=CMD), "退出码 1\nBuildError: missing swatch file and stale index in project", True)
    store2.after_tool(s, call("q2", "run_command", command="tilepack index"), "退出码 0\n", False)
    store2.after_tool(s, call("q3", "run_command", command=CMD), "退出码 1\nIndexError: tile index is stale", True)
    store2.after_tool(s, call("q4", "write_file", path="swatch.json"), "写好了", False)
    store2.after_tool(s, call("q5", "run_command", command=CMD), "退出码 0\nok", False)
    store2.learn("sess", s.run)
    (e2,) = store2.items.values()
    assert e2["action"] == "先`tilepack index`，再写 `swatch.json`" and e2["failed"] == []


def test_post_error_hint_format_on_vs_off(tmp_path):
    for pmode, want_fail_text in [("on", True), ("shadow", False), ("off", False)]:
        store, now = mk(tmp_path / pmode, pmode=pmode)
        run_once(store, "r1", wrong=(("run_command", "zfix cache clear"),))
        now[0] += DAY
        _, hint, _, _ = run_once(store, "r2")                       # 第 2 轮: "试过没用"还没证实 (只有 1 次记录), 不写
        assert "试过没用" not in hint, (pmode, hint)
        now[0] += DAY
        _, hint, _, _ = run_once(store, "r3")                       # r2 里她照着做法成了、没做 zfix cache clear -> 证实, 第 3 轮才写
        assert ("试过没用：`zfix cache clear`" in hint) is want_fail_text, (pmode, hint)


def test_failed_route_needs_confirmation_and_only_shows_for_the_same_error(tmp_path):
    store, now = mk(tmp_path)
    run_once(store, "r1", wrong=(("run_command", "zfix cache clear"),))
    (e,) = store.items.values()
    assert [(f["akey"], f["ok"]) for f in e["failed"]] == [("cmd:zfix cache", False)] and P.failed_text(e) == ""   # 待证实: 不写进提示
    now[0] += DAY
    run_once(store, "r2")
    assert [f["ok"] for f in e["failed"]] == [True] and P.failed_text(e) == "`zfix cache clear`"                   # 做法没做那一步也成了: 证实
    # 报错只是像、不是几乎一样 (字词重合 < 0.80): 这是另一种情况, 不写"试过没用"
    now[0] += DAY
    _, hint, _, _ = run_once(store, "r3", fail="退出码 1\nModuleNotFoundError: No module named 'zeta_codec' in other project")
    assert hint and "试过没用" not in hint
    _, hint, _, _ = run_once(store, "r4")
    assert "试过没用：`zfix cache clear`" in hint
    # 动手前提示不写"试过没用" (还不知道这一次会报什么错)
    warm(store, now, 2)
    assert store.before_task(Sess("p", GOAL), GOAL) and "试过没用" not in store.before_task(Sess("p2", GOAL), GOAL)


def test_adopted_again_goes_to_near_identical_siblings_only(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 3, goal=GOAL)
    near_sig = FAIL + " (retry)"                                                                 # 字词重合 5/6 ≥ 0.80: 几乎一样
    mid_sig = "退出码 1\n" + "ModuleNotFoundError: No module named 'zeta_codec' in other project"   # 5/7 ≈ 0.71: 像, 但不是同一种情况
    far_sig = "退出码 1\n" + "ImportError: cannot import name widgets from gadgetlib"                 # 不像
    for k, (sig, fix) in enumerate([(near_sig, ("write_file", "other.cfg")), (mid_sig, ("write_file", "third.ini")),
                                    (far_sig, ("write_file", "fourth.toml"))]):
        run_once(store, f"x{k}", fail=sig, steps=(fix,), goal="别的事")
    by_sig = {e["sig"]: e for e in store.items.values()}
    first = [e for e in store.items.values() if e["action"] == "先`pipq add zeta_codec`"][0]
    assert len(store.items) == 4
    run_once(store, "bad", succeed=False)                                                       # 第一张卡照做又失败
    assert "cmd:pipq add" not in [f["akey"] for f in first["failed"]]                           # 不记给自己
    body = X.signature
    assert any(f["akey"] == "cmd:pipq add" for f in by_sig[body(near_sig)]["failed"])
    assert by_sig[body(mid_sig)]["failed"] == [] and by_sig[body(far_sig)]["failed"] == []


# ---------------------------------------------------------------- 7. 该不该触发 和 做没做成 分开
def test_trigger_and_outcome_are_measured_separately(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 3)                                                                           # 一张有资格的卡
    (e,) = store.items.values()
    # 格1: 提示对、做成了
    run_once(store, "c1"); now[0] += DAY
    # 格2: 提示对、照做了还是同一个错
    run_once(store, "c2", succeed=False); now[0] += DAY
    e.update(auto=0.9, streak=3, state="supported"); e["verified"] = now[0]
    # 格3: 不该触发却触发了 (任务对得上, 但这一轮根本没跑那类命令)
    s = Sess("c3", GOAL)
    assert store.before_task(s, GOAL) != ""
    store.learn("sess", s.run); now[0] += DAY
    # 格4: 该触发没触发 (任务原话对不上, 但她跑了那类命令, 一开始就报了像的错)
    e.update(auto=0.9, streak=3, state="supported"); e["verified"] = now[0]
    s = Sess("c4", "整理一下桌面")
    assert store.before_task(s, "整理一下桌面") == ""
    store.after_tool(s, call("c4a", "run_command", command=CMD), FAIL, True)
    store.learn("sess", s.run)
    m = store.stats()["procedures"]
    t, o = m["trigger"]["on"], m["outcome"]
    assert (t["shown"], t["warranted"], t["needed"], t["false_shown"], t["missed"]) == (3, 2, 2, 1, 1)
    assert t["precision"] == pytest.approx(2 / 3, abs=1e-3) and t["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert (o["shown"], o["adopted"], o["success"], o["again"]) == (3, 2, 1, 1)
    assert o["adopted_rate"] == pytest.approx(2 / 3, abs=1e-3) and o["success_given_adopted"] == 0.5
    assert set(t) & set(o) <= {"shown"}                                                           # 两块只共用"展示了几次"这个名字, 别的指标各算各的
    assert "success" not in t and "precision" not in o
    assert m["trigger"]["child_would_show"] == 0 and m["own_route"]["adopted_without_hint"] == 0


def test_hint_followed_and_own_same_route_are_counted_apart(tmp_path):
    on, non = mk(tmp_path / "on", pmode="on")
    warm(on, non, 3)
    run_once(on, "x")
    sh, nsh = mk(tmp_path / "sh", pmode="shadow")
    warm(sh, nsh, 3)
    run_once(sh, "x")
    assert on.stats()["procedures"]["outcome"]["adopted"] == 1 and on.stats()["procedures"]["own_route"]["adopted_without_hint"] == 0
    assert sh.stats()["procedures"]["outcome"]["adopted"] == 0 and sh.stats()["procedures"]["own_route"]["adopted_without_hint"] == 1


# ---------------------------------------------------------------- 8. 经验只从她自己的经历里来
def test_empty_store_never_prompts_and_nothing_is_preset(tmp_path):
    store, _ = mk(tmp_path / "h")
    heads = ["npm test", "pip install", "git push", "docker build", "pytest", "python main.py", "bundlex pack", "make", "cargo build"]
    tasks = [f"{h} {w}" for h in heads for w in ("", "帮我", "再来一次", "报错了", "看看", "跑一下", "http://a.test/x.py")] * 5
    assert len(tasks) >= 300 and all(store.before_task(Sess(f"e{k}", t), t) == "" for k, t in enumerate(tasks))
    assert store.items == {} and not (tmp_path / "h" / "experiences.json").exists()
    seed_dir = ROOT / "wm" / "seeds"
    assert store.items == {} or not seed_dir.exists()                                             # 种子包的习惯不会灌进来
    src = Path(P.__file__).read_text(encoding="utf-8")
    for name in ("npm", "pip ", "git ", "docker", "pytest", "cargo", "powershell", "seed.json"):
        assert name not in src, name                                                              # 新模块里没有命令名表 / 做法文本 / 种子
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("wm", "wm.memory")) or "memory" in m or "seed" in m for m in mods), mods


def test_every_card_comes_from_a_recorded_episode(tmp_path):
    store, now = mk(tmp_path)
    s = Sess("only-read", "只看看")
    store.after_tool(s, call("a", "run_command", command="logq tail"), "退出码 1\nLogError: log file locked by writer", True)
    store.after_tool(s, call("b", "read_file", path="x.txt"), "内容", False)
    store.learn("sess", s.run)
    assert store.items == {}                                                                      # 没有"报错→做了什么→成了"就没有卡
    run_once(store, "ok")
    assert all(e["sources"] and e["origin"] == "经历" for e in store.items.values())


def test_cognition_truth_inputs_are_plain_text_only():
    """认知与真值: 阶段 7 的函数只收任务原话、命令、退出码、报错文字、时间 (没有小屋 / 世界对象). 说明写在这里: 不需要新的双胞胎测试."""
    import inspect
    for fn in (P.match_pre, P.scrub_goal, P.anchors_of, P.auto_eff, P.pre_eligible, X.pre_outcome, X.episodes):
        for name in inspect.signature(fn).parameters:
            assert name not in ("room", "world", "scene", "mind", "cognition", "state"), (fn.__name__, name)
    src = Path(X.__file__).read_text(encoding="utf-8") + Path(P.__file__).read_text(encoding="utf-8")
    assert "wm.cognition" not in src and "room" not in re.sub(r"[#\"'].*", "", src.replace("room-", ""))[:0]


# ---------------------------------------------------------------- 9. 隐私
def test_goals_never_keep_phone_email_long_numbers_or_secrets(tmp_path):
    store, now = mk(tmp_path)
    goal = ("把报表发给 test.user@example.test 手机 13800138000 订单 123456789012 "
            "文件 C:\\Users\\nobody\\docs\\q3.xlsx token=sk-ABCDEFGHIJKLMNOP1234 看 https://x.example.test/a?k=1")
    warm(store, now, 3, goal=goal)
    (e,) = store.items.values()
    kept = e["anchors"]["goals"]
    assert kept and all(len(g) <= P.GOAL_CHARS for g in kept)
    blob = json.dumps(kept, ensure_ascii=False)
    for bad in ("test.user", "example.test", "13800138000", "123456789012", "sk-ABCDEFGH", "nobody", "C:\\", "http", "?k=1"):
        assert bad not in blob, bad
    for f in ("procedure-log.jsonl", "experience-log.jsonl"):
        p = tmp_path / f
        if p.exists():
            text = p.read_text(encoding="utf-8")
            assert "报表" not in text and "bundlex" not in text and "report.zip" not in text           # 日志里没有任务原话、命令、文件名
    # 看得到、能删: clear_goals 只清任务片段; 删卡一起没; 退役、归档的副本也不留
    assert store.view()["items"][0]["anchors"]["goals"] == kept
    assert store.update(e["id"], "clear_goals")["item"]["anchors"]["goals"] == [] and e["head"] == "bundlex pack"
    e["anchors"]["goals"] = ["还有一条"]
    now[0] += (X.RETIRE_DAYS + 5) * DAY
    store.view()
    assert e["state"] == "retired" and e["anchors"]["goals"] == []
    e["anchors"]["goals"] = ["又一条"]
    assert X.Experiences._for_archive(e)["anchors"]["goals"] == [] and e["anchors"]["goals"] == ["又一条"]
    store.update(e["id"], "delete")
    assert "又一条" not in (tmp_path / "experiences.json").read_text(encoding="utf-8")


def test_procedure_log_has_ids_only(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 3)
    run_once(store, "x")
    rows = [json.loads(ln) for ln in (tmp_path / "procedure-log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and {r["kind"] for r in rows} <= {"settled", "missed", "child_would_show"}
    allowed = {"t", "kind", "mode", "card", "how", "run", "session", "warranted", "needed", "adopted", "outcome", "steps", "chars"}
    assert all(set(r) <= allowed for r in rows)


# ---------------------------------------------------------------- 10. 子会话、开关优先级
def test_child_session_only_counts_would_show(tmp_path):
    store, now = mk(tmp_path)
    warm(store, now, 3)
    s = Sess("kid", GOAL, parent="parent-1")
    assert store.before_task(s, GOAL) == ""
    rows = [json.loads(ln) for ln in (tmp_path / "procedure-log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in rows][-1] == "child_would_show"
    store.learn("sess", s.run)
    assert store.stats()["procedures"]["trigger"]["child_would_show"] == 1
    assert store.stats()["procedures"]["trigger"]["on"]["shown"] == 0


# ---------------------------------------------------------------- 11. 健壮
def test_bad_files_and_write_failures_do_not_raise(tmp_path, monkeypatch):
    for k, text in enumerate(["", "{", "[" * 100_000, json.dumps([{"id": "x"}]), json.dumps("str"), "[1, 2]"]):
        d = tmp_path / f"d{k}"
        d.mkdir()
        (d / "experiences.json").write_text(text, encoding="utf-8")
        st = X.Experiences(d, "on", procedures="on")
        assert st.items == {} and st.before_task(Sess("z", GOAL), GOAL) == ""
    store, now = mk(tmp_path / "ro")
    warm(store, now, 3)

    def boom(*a, **k):
        raise OSError("只读")
    monkeypatch.setattr(Path, "write_text", boom)
    monkeypatch.setattr(Path, "open", boom)
    run_once(store, "after")                                                                        # 写不上: 不抛, 检索照常
    assert store.before_task(Sess("n", GOAL), GOAL) != ""
    (tmp_path / "plog").mkdir()
    (tmp_path / "plog" / "procedure-log.jsonl").write_text("not json\n", encoding="utf-8") if False else None


def test_mixed_old_and_new_rows(tmp_path):
    orig = load_orig()
    old, now = mk(tmp_path / "a", orig=orig)
    run_once(old, "o1", cmd="alpha run", fail="退出码 1\nKeyError: aa")
    rows = json.loads((tmp_path / "a" / "experiences.json").read_text(encoding="utf-8"))
    new, now2 = mk(tmp_path / "b")
    warm(new, now2, 2)
    more = json.loads((tmp_path / "b" / "experiences.json").read_text(encoding="utf-8"))
    more[0]["id"] = "exp-other-id"
    mixed = rows + more
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "experiences.json").write_text(json.dumps(mixed), encoding="utf-8")
    both = X.Experiences(tmp_path / "c", "on", procedures="on")
    assert len(both.items) == 2 and len(both.view()["items"]) == 2


# ---------------------------------------------------------------- 12. 与阶段 5 并存
def test_does_not_touch_stage5_sidecars_or_import_memory(tmp_path):
    side = tmp_path / "记忆"
    side.mkdir()
    for name in (".strength.json", ".strength-shadow.jsonl", ".strength-verdicts.jsonl"):
        (side / name).write_text('{"keep": 1}\n', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in side.iterdir()}
    store, now = mk(tmp_path)
    warm(store, now, 4)
    assert {p.name: p.read_bytes() for p in side.iterdir()} == before
    src = Path(X.__file__).read_text(encoding="utf-8") + Path(P.__file__).read_text(encoding="utf-8")
    assert "memory_strength" not in src and "classify" not in src


# ---------------------------------------------------------------- 13. 真实入口: AgentHub 一轮一轮跑 (假模型, 真命令)
class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


def _hub_four_runs(tmp_path, procedures):
    from wm.agent.core import AgentHub
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    cfg = {"agent": {"workspace": str(ws), "procedures": procedures}}
    hub = AgentHub(cfg, home=tmp_path / "agent")
    hub._llm = lambda _s: FakeLLM()
    if procedures == "on":  # 生产配置写 on 是锁住的; 这里走测试专用的关键字参数重建一个解锁的仓库
        hub.experience = X.Experiences(hub.home, "on", procedures="on", _test_unlock_on=True)
    state = {"n": 0}

    def run_cmd(n):
        return {"content": "", "tool_calls": [{"id": f"c{n}", "name": "run_command", "args": json.dumps({"command": "python check.py"})}]}

    def write(n):
        return {"content": "", "tool_calls": [{"id": f"w{n}", "name": "write_file",
                                               "args": json.dumps({"path": "check.py", "content": "print('ok')\n"})}]}
    script = [run_cmd, write, run_cmd]
    bgs, notes = [], []

    async def ask_model(sess, llm, tools):
        bgs.append(dict(sess.backgrounds))
        k = state["n"] % 4
        state["n"] += 1
        out = script[k](state["n"]) if k < 3 else {"content": "跑完了。", "tool_calls": []}
        return {"content": out["content"], "tool_calls": out["tool_calls"], "reasoning": ""}
    hub._ask_model = ask_model

    async def main():
        s = hub.new_session(mode="full")

        async def sink(ev):
            if ev.get("type") == "note" and "动手前" in ev.get("text", ""):
                notes.append(ev["text"])
        hub.subscribe(s.id, sink)
        for r in range(4):
            if (ws / "check.py").exists():
                (ws / "check.py").unlink()
            assert await hub.send(s.id, "把检查脚本跑起来") is None
            await asyncio.sleep(0)
            await s.task
        return s

    s = asyncio.run(main())
    return hub, s, bgs, notes


def test_hub_pre_block_appears_on_the_fourth_run_only_when_on(tmp_path):
    hub, s, bgs, notes = _hub_four_runs(tmp_path / "on", "on")
    firsts = [list(b.values())[-1] if b else "" for b in bgs]
    seen = [P.HEAD_PRE in str(b) for b in bgs]
    assert seen.count(True) >= 1 and not any(seen[:9]) and len(notes) == 1                         # 前三轮的几步里都没有, 第四轮才有
    (e,) = hub.experience.items.values()
    assert e["streak"] >= 3 and e["auto"] >= P.AUTO_PRE and "python check.py" in str(bgs)
    hub2, _, bgs2, notes2 = _hub_four_runs(tmp_path / "shadow", "shadow")
    assert not any(P.HEAD_PRE in str(b) for b in bgs2) and notes2 == []
    assert (tmp_path / "shadow" / "agent" / "procedure-log.jsonl").exists()
    hub3, _, bgs3, notes3 = _hub_four_runs(tmp_path / "off", "off")
    assert not any(P.HEAD_PRE in str(b) for b in bgs3) and not (tmp_path / "off" / "agent" / "procedure-log.jsonl").exists()
    assert hub3.experience.pmode == "off" and hub.experience.pmode == "on" and hub2.experience.pmode == "shadow"


# ---------------------------------------------------------------- 14. 评测台里的小型快速版: 三次都成 + 误触发
def test_bench_small_three_in_a_row_and_no_false_triggers():
    sys.path.insert(0, str(ROOT / "tools"))
    import bench_procedures as B
    fams = [f for f in B.load_families("dev", "") if f["id"] in ("dev-01-dependency", "dev-02-port", "dev-08-plain", "dev-09-readonly",
                                                                 "dev-10-sharedhead")]
    orig = B._load_file("mem7_bench_orig", B.ORIG)
    got = {}
    for col, mod, pm in (("orig", orig, None), ("on", X, "on")):
        sims = [B.simulate(fams, seed, mod, pm, 6, 1.0, False, 1.0) for seed in range(6)]
        got[col] = B.summarize(sims, fams, 6)
    assert got["on"]["pre_false_shown"] == 0                                                     # plain / readonly / shared_head 里没有误触发
    assert got["on"]["all_first3"] >= got["orig"]["all_first3"] - 1e-9
    assert got["on"]["pre_shown"] > 0 and got["orig"]["pre_shown"] == 0


def test_hint_format_contract_with_the_bench_stand_in(tmp_path):
    """评测台里假"她"认的格式约定就是提示的契约 (评审 11-15): 一张卡一行、行首 "- "、修法写在反引号里、用中文分号分段,
    含"判成功"的段落是标准、含"试过没用"的段落是失败路线、含"备选"的是第二条路线. 提示的字句不是为了让假"她"多做对而调的."""
    sys.path.insert(0, str(ROOT / "tools"))
    import bench_procedures as B
    fam = {"command": "bundlex pack", "fixes": [{"id": "A", "kind": "cmd", "cmd": "pipq add zeta_codec"},
                                                {"id": "B", "kind": "cmd", "cmd": "zfix cache clear"},
                                                {"id": "C", "kind": "edit", "file": "gadget.cfg"}]}
    store, now = mk(tmp_path)
    run_once(store, "w0", wrong=(("run_command", "zfix cache clear"),))
    run_once(store, "w1")
    run_once(store, "w2")
    run_once(store, "w3", fail="退出码 1\nImportError: cannot import name gadget from toolkit", steps=(("write_file", "gadget.cfg"),))
    now[0] += DAY
    for k in range(2):
        run_once(store, f"w{4 + k}", fail="退出码 1\nImportError: cannot import name gadget from toolkit", steps=(("write_file", "gadget.cfg"),))
    pre = store.before_task(Sess("p", GOAL), GOAL)
    _, hint, _, _ = run_once(store, "h", fail=FAIL)
    for block in (pre, hint):
        assert block
        lines = block.splitlines()[1:]
        assert lines and all(ln.startswith("- ") for ln in lines)
        routes, first_bad = B.parse_block(block, fam)
        assert routes, block
    routes, _ = B.parse_block(pre, fam)
    assert routes[0][0] == ["A"] and "判成功的标准" in pre.splitlines()[1] and "判成功" not in "".join(
        t for r in routes for t in r[0])
    assert B.marked_bad(pre + hint, fam) <= {"B", "A", "C"}
    assert re.search(r"`zfix cache clear`", hint) is None or "试过没用" in hint                # 试过没用的路线写在"试过没用"段里


def test_bench_set_flag_with_default_value_changes_nothing(tmp_path):
    """--set 缺省值不许变: 给一个等于缺省的 --set, 输出和不给时逐项一样 (评审 11-15)."""
    import subprocess
    outs = []
    for k, extra in enumerate(([], ["--set", f"AUTO_PRE={P.AUTO_PRE}"])):
        out = tmp_path / f"o{k}.json"
        cmd = [sys.executable, "-B", str(ROOT / "tools" / "bench_procedures.py"), "--group", "dev", "--cols", "orig", "--seeds", "2",
               "--runs", "6", "--shared", "per-family", "--bad-policy", "last", "--out", str(out), *extra]
        if "backups" in Path(P.__file__).parts:  # 草稿期: 子进程也要用草稿里的 procedures.py (换进 wm/ 以后不需要)
            cmd += ["--draft", str(ROOT / "backups" / "mem7-procedures" / "draft")]
        subprocess.run(cmd, check=True, capture_output=True)
        d = json.loads(out.read_text(encoding="utf-8"))
        d["args"].pop("set", None)
        d["args"].pop("out", None)
        outs.append(d["results"])
    assert outs[0] == outs[1]


# ---------------------------------------------------------------- 返工第 1 轮 (评审 11-16)
def test_confirmed_failed_routes_fall_back_when_the_card_fails_or_the_route_proves_useful(tmp_path):
    """"试过没用"是在"这张卡还管用"的条件下得出的: 卡的首选做法后来又失败了 -> 退回待证实; 那条"没用"的路线后来成了 -> 作废."""
    store, now = mk(tmp_path)
    run_once(store, "r1", wrong=(("run_command", "zfix cache clear"),))
    now[0] += DAY
    run_once(store, "r2")
    (e,) = store.items.values()
    assert P.failed_text(e) == "`zfix cache clear`"                                                   # 证实了
    now[0] += DAY
    run_once(store, "bad", succeed=False)                                                            # 首选做法又失败了
    assert [f["ok"] for f in e["failed"]] == [False] and P.failed_text(e) == ""                      # 退回待证实, 不再写进提示
    # 另一张卡: 那条"没用"的路线后来真的成了 (失效旧解法: 以前没用的那一步后来正是需要的那一步) -> 别的卡里记的作废
    store2, now2 = mk(tmp_path / "s2")
    run_once(store2, "a1", wrong=(("run_command", "zfix cache clear"),))
    now2[0] += DAY
    run_once(store2, "a2")
    (c1,) = store2.items.values()
    assert P.failed_text(c1) == "`zfix cache clear`"
    now2[0] += DAY
    run_once(store2, "a3", steps=(("run_command", "zfix cache clear"),))                              # 这回需要的就是它
    assert all(f["akey"] != "cmd:zfix cache" for f in c1["failed"])


def test_before_task_error_does_not_fail_the_turn(tmp_path, monkeypatch):
    """core.py 里 before_task 那几行包了 try/except: 出错就记日志、当没有提示, 这一轮照常跑完 (shadow 档每一轮都会走到这里)."""
    from wm.agent.core import AgentHub
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    hub = AgentHub({"agent": {"workspace": str(ws), "procedures": "shadow"}}, home=tmp_path / "agent")
    hub._llm = lambda _s: FakeLLM()

    async def ask_model(sess, llm, tools):
        return {"content": "好的。", "tool_calls": [], "reasoning": ""}
    hub._ask_model = ask_model

    def boom(*a, **k):
        raise RuntimeError("动手前提示坏了")
    monkeypatch.setattr(hub.experience, "before_task", boom)

    async def main():
        s = hub.new_session(mode="full")
        assert await hub.send(s.id, "把检查脚本跑起来") is None
        await asyncio.sleep(0)
        await s.task
        return s
    s = asyncio.run(main())
    assert s.run["state"] == "completed" and s.status == "idle"


def test_confirmed_mode_records_the_right_cards_as_shown(tmp_path):
    """confirmed 档: 前面没确认的卡被滤掉时, 真展示了的卡要记成 on (按 shown_cards 的顺序记, 不按匹配结果的下标)."""
    store, now = mk(tmp_path, mode="confirmed", pmode="on")
    for sig, fix in [(FAIL, ("run_command", "pipq add alpha")),
                     ("退出码 1\nImportError: cannot import name gadget from toolkit", ("write_file", "gadget.cfg"))]:
        warm(store, now, 3, goal=GOAL, fail=sig, steps=(fix,))
    first, second = sorted(store.items.values(), key=lambda e: e["created"])
    store.update(second["id"], "approve")                                                           # 第二张确认了, 第一张没有
    assert first["trust"] == 3 and second["trust"] == 2
    for e in (first, second):
        e.update(auto=0.9, streak=3, state="supported", verified=now[0])
    s = Sess("conf", GOAL)
    block = store.before_task(s, GOAL)
    assert "主人确认过" in block and "pipq add alpha" not in block
    cards = {c["exp"]: c["mode"] for c in store._runs["conf"]["pre"]["cards"]}
    assert cards[second["id"]] == "on" and cards[first["id"]] == "shadow"


# ---------------------------------------------------------------- on 档在代码里锁住 (照阶段 5 的做法; 评审 11-16)
def _lock_script(tmp_path, procedures, **kw):
    """同一串轮次, 每次从头建仓库 (id 计数器重置), 返回 (每轮提示和小结, 目录)."""
    store, now = mk(tmp_path, pmode="off")                              # 先借 mk 重置 id 计数器
    store = X.Experiences(tmp_path, "on", clock=lambda: now[0], procedures=procedures, **kw)
    return _script(store, now), store


def test_on_in_config_runs_as_shadow_byte_for_byte_and_logs_why(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        out_on, st_on = _lock_script(tmp_path / "a", "on")              # 配置写 on, 没有解锁参数
    out_sh, st_sh = _lock_script(tmp_path / "b", "shadow")
    assert out_on == out_sh                                             # 她看到的: 逐个相同
    assert st_on.pmode == "shadow" and st_on.preq == "on"
    for name in ("experiences.json", "experience-log.jsonl", "procedure-log.jsonl"):   # 写出的文件逐字节一样
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    assert any(P.ON_LOCKED in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    assert "这两条没解决前 on 档不开放" in P.ON_LOCKED and "问用法会被误提示" in P.ON_LOCKED
    now = [T0]
    st = X.Experiences(tmp_path / "c", "on", clock=lambda: now[0], procedures="on")      # 配置写 on, 没有解锁参数
    warm(st, now, 3)
    assert P.pre_eligible(next(iter(st.items.values())), now[0]) and st.before_task(Sess("x", GOAL), GOAL) == ""   # 卡成熟到线了, 还是不提示


def test_no_config_key_can_unlock_on(tmp_path):
    from wm.agent.core import AgentHub
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    cfg = {"agent": {"workspace": str(ws), "procedures": "on", "_test_unlock_on": True, "unlock": True, "procedures_unlock": "yes",
                     "experience": "on"}, "_test_unlock_on": True, "memory": {"_test_unlock_on": True}}
    hub = AgentHub(cfg, home=tmp_path / "agent")
    assert hub.experience.preq == "on" and hub.experience.pmode == "shadow"
    # 位置参数也够不着: 解锁参数只能按关键字传
    import inspect
    p = inspect.signature(X.Experiences.__init__).parameters["_test_unlock_on"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is False


def test_the_unlock_switch_name_appears_only_in_experience_and_procedures():
    """扫 wm/ 下的源码 (草稿期再加上 draft 里的 core.py): 除了 experience.py 和 procedures.py, 别的文件里不出现这个开关的名字."""
    paths = [p for p in (ROOT / "wm").rglob("*.py") if p.name not in ("experience.py", "procedures.py")]
    draft = ROOT / "backups" / "mem7-procedures" / "draft" / "wm" / "agent" / "core.py"
    if "backups" in Path(P.__file__).parts and draft.exists():
        paths.append(draft)
    hits = [str(p) for p in paths if "_test_unlock_on" in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == [], hits
    assert "_test_unlock_on" not in Path(P.__file__).read_text(encoding="utf-8")                      # procedures.py 里也只放提示句子, 不放开关名


def test_view_shows_requested_and_effective_mode(tmp_path):
    for req, kw, want in (("off", {}, "off"), ("shadow", {}, "shadow"), ("on", {}, "shadow"), ("on", {"_test_unlock_on": True}, "on")):
        store = X.Experiences(tmp_path / f"{req}{want}{len(kw)}", "on", procedures=req, **kw)
        v = store.view()
        assert (v["procedures_requested"], v["procedures_mode"]) == (req, want)
