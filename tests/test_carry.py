"""端东西的最小后果闭环 (wm/cognition/carry.py + carry_world.py): 行动前预测 -> 执行 -> 感知后果 -> 更新经验 -> 下次提前调整.

- 她那边 (carry.py) 拿不到环境 (不 import carry_world / roomworld);
- 感知门: 精确真值 (毫升、温度、她注意不到的几滴) 不出门, 看得到的液面变化、碰到的刺激要进;
- 双胞胎: 只差感知不到的东西 -> 下一次的选择、提示词一模一样; 一边被烫了 -> 下一次必须不一样;
- 里程碑 (示意环境, 不是物理): 第一次照常端、洒了; 之后她自己挑做法, 洒得少; 清空经验的对照一直照常、一直洒;
  依据说得出是哪一段; 换杯子能迁移, 装量少了不乱用; 经验能撤销.
- 代价算上时间 (2026-10-27 评估): 等凉要等多久、什么时候要、要不要热的都算; 评测的得分防"不走""先倒空""一直等";
  不知道装了多少先算看一眼值不值.
"""
import json
import re
from pathlib import Path

from wm.cognition.carry import DEFAULT, STAGES, CarryMind, options
from wm.cognition.carry_world import VISIBLE_ML, SimCarry, fill_bin, perceive, task_score

ROOT = Path(__file__).resolve().parent.parent
FULL = {"fill": "full", "cup": "mug", "hot": True, "route": "short"}


def raw_trip(spills: dict, contacts: dict | None = None, cap: float = 412.0, ml: float = 380.0, temp: float = 80.0) -> dict:
    """一趟的真值: 每一段洒多少 (ml)、哪一段碰到身上. temp 只在真值里, 感知门不该用到."""
    stages = {}
    for s in STAGES:
        out = spills.get(s, 0.0)
        stages[s] = {"ml0": ml, "ml1": ml - out, "spilled": out, "contacts": (contacts or {}).get(s, {}), "temp": temp}
        ml -= out
    return {"cap": cap, "stages": stages, "delivered": True, "secs": 12.4, "left": ml, "ml": 380.0}


def changed(choice: dict) -> dict:
    return {k: v for k, v in choice.items() if v != DEFAULT[k]}


def test_mind_side_never_imports_the_world():
    src = (ROOT / "wm/cognition/carry.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+\S*(carry_world|roomworld)", src, re.M)


def test_perception_gate():
    o = perceive(raw_trip({"pick": VISIBLE_ML / 2, "lift": 40.0}, {"lift": {"rightHand": {"level": "hot", "severity": 0.1, "ml": 2.0}}}))
    assert o["stages"]["pick"] == {"spill": "none", "drop": 0, "felt": []}  # 挂在杯沿的几滴, 注意不到
    assert o["stages"]["lift"] == {"spill": "splash", "drop": 0, "felt": [["右手", "hot"]]}  # 380 -> 340 ml 还是"接近满杯", 但看得到泼出来一大片
    assert ([perceive(raw_trip({"walk": x}))["stages"]["walk"]["spill"] for x in (0.2, 1.0, 5.0, 15.0, 30.0, 78.0)]
            == ["none", "drops", "little", "some", "stream", "splash"])
    big = perceive(raw_trip({"walk": 200.0}))
    assert big["stages"]["walk"]["drop"] == 1 and big["end_fill"] == "half"
    text = json.dumps(o, ensure_ascii=False)
    assert "ml" not in text and "temp" not in text and "severity" not in text and "80" not in text
    assert [fill_bin(x) for x in (0.9, 0.75, 0.5, 0.35, 0.2, 0.05, 0.01)] == ["full", "full", "half", "half", "low", "low", "empty"]


def test_twins_same_perception_same_next_choice():
    """两趟只差她感知不到的: 洒了 0.1 还是 0.4 ml、咖啡 70 还是 90 ℃ (没碰到身上) -> 下一次的选择和提示词一样."""
    a, b = CarryMind(), CarryMind()
    for mind, spill, temp in ((a, 0.1, 70.0), (b, 0.4, 90.0)):
        mind.choose(FULL)
        mind.learn(perceive(raw_trip({"pick": spill, "lift": spill}, temp=temp)))
    assert a.choose(FULL) == b.choose(FULL)
    assert a.prompt(FULL) == b.prompt(FULL)


def test_twins_burned_one_must_react():
    """一边拿起时洒了还烫到右手, 一边只有注意不到的几滴: 下一次必须不一样 (不能被烫了还"什么都不知道")."""
    burned, calm = CarryMind(), CarryMind()
    for mind, raw in ((burned, raw_trip({"pick": 12.0}, {"pick": {"rightHand": {"level": "burn"}}})), (calm, raw_trip({"pick": 0.1}))):
        mind.choose(FULL)
        mind.learn(perceive(raw))
    c1, c0 = burned.choose(FULL), calm.choose(FULL)
    assert changed(c0["choice"]) == {}
    assert c1["choice"]["hand"] == "slow" and "拿起时洒过" in c1["why"]
    assert c1["predict"] != c0["predict"]


def run(ctx, trips: int, seed: int, cleared: bool = False):
    """端 trips 趟. cleared: 每趟都换一个没经历过的她 (清空经验的对照). 返回每趟的结果和最后的她."""
    env, mind, rows = SimCarry(seed), CarryMind(), []
    for k in range(trips):
        if cleared:
            mind = CarryMind()
        c = mind.choose(ctx)
        raw = env.run(ctx, c["choice"])
        rows.append({**mind.learn(perceive(raw), ev=k), "left": raw["left"] / raw["ml"], "delivered": raw["delivered"]})
    return rows, mind


def test_milestone_experience_changes_the_next_choice():
    lived = [run(FULL, 6, seed)[0] for seed in range(4)]
    cleared = [run(FULL, 6, seed, cleared=True)[0] for seed in range(4)]
    for rows in lived:
        assert changed(rows[0]["choice"]) == {}            # 第一次照常端
        assert rows[0]["spilled"]                           # 洒了 (示意环境里接近满杯照常端几乎都会洒)
        assert any(changed(r["choice"]) for r in rows[1:])  # 之后她自己改了做法
        assert all(r["delivered"] for r in rows)
    for rows in cleared:
        assert all(changed(r["choice"]) == {} for r in rows)  # 清空经验: 一直照常
    later = lambda runs: sum(len(r["spilled"]) for rows in runs for r in rows[2:]) / (4 * 4)  # noqa: E731
    assert later(lived) < 0.5 * later(cleared)
    keep = lambda runs: sum(r["left"] for rows in runs for r in rows[2:]) / (4 * 4)  # noqa: E731
    assert keep(lived) > keep(cleared)


def test_transfer_and_no_overreach_and_revoke():
    rows, mind = run(FULL, 4, 1)
    espresso = mind.choose({**FULL, "cup": "espresso"})
    assert changed(espresso["choice"])  # 换了杯子: 马克杯的经验照着用
    assert "马克杯" in espresso["why"] and "这回换了杯子" in espresso["why"]
    half = mind.predict({**FULL, "fill": "half"}, DEFAULT)
    full = mind.predict(FULL, DEFAULT)
    assert all(half[s]["spill"] < full[s]["spill"] for s in STAGES)  # 半杯的风险没有被满杯的经验抬到一样高
    assert mind.revoke(range(4)) == 4
    assert changed(mind.choose(FULL)["choice"]) == {}  # 撤掉经验: 回到照常端


def test_predictions_are_kept_and_scored():
    rows, mind = run(FULL, 5, 2)
    assert len(mind.ledger) == 5 and all(set(r["predict"]) == set(STAGES) for r in mind.ledger)
    assert all(0 <= r["brier"] <= 1 for r in mind.ledger)
    # 有经历的她预测得比没经历过的准 (第 3 趟起, 6 个种子平均; 示意环境里约 0.08 对 0.21)
    lived = [r["brier"] for s in range(6) for r in run(FULL, 6, s)[0][2:]]
    fresh = [r["brier"] for s in range(6) for r in run(FULL, 6, s, cleared=True)[0][2:]]
    assert sum(lived) / len(lived) < 0.6 * sum(fresh) / len(fresh)


RELAXED = {**FULL, "wait_s": 480, "relaxed": True, "hot_pref": False}  # 你说了不急、凉一点也行; 等凉要 8 分钟


def burned_mind(ctx=RELAXED, trips=6):
    """每趟挪到身前、走、放下都烫伤 (等了只是烫一下) 的她, 和每趟选没选等."""
    mind, picks = CarryMind(), []
    burn = {s: {"rightHand": {"level": "burn"}} for s in ("lift", "walk", "put")}
    hot_only = {s: {"rightHand": {"level": "hot"}} for s in ("lift", "walk", "put")}
    for k in range(trips):
        c = mind.choose(ctx)
        picks.append(c["choice"]["wait"])
        mind.learn(perceive(raw_trip({"lift": 25.0}, hot_only if c["choice"]["wait"] else burn)), ev=k)
    return mind, picks


def test_burns_teach_waiting_and_waiting_is_judged_by_how_it_felt():
    """每趟都烫伤 -> 你说了不急、凉一点也行的时候, 她选"等它凉一点再端", 依据说得出烫到哪; 等了以后只是烫一下 -> 接着等
    (等凉的折扣是试出来的)."""
    mind, picks = burned_mind()
    assert picks[0] == 0 and 1 in picks
    first = picks.index(1)
    assert all(picks[first:]), picks  # 等了以后只是烫一下: 接着等
    assert "烫到过右手" in mind.ledger[first]["why"]
    assert not any(c["wait"] for c in [CarryMind().choose({**FULL, "hot": False})["choice"]])  # 不烫的不用等


def test_time_and_wishes_decide_whether_to_wait():
    """同一个被烫过、学会等凉的她: 你要热的、5 分钟以内要, 都不等, 说得出为什么; 等凉要 22 分钟又没说不急, 也不白等."""
    mind, _ = burned_mind()
    assert mind.choose(RELAXED)["choice"]["wait"] == 1
    hot = mind.choose({**RELAXED, "hot_pref": True})
    assert hot["choice"]["wait"] == 0 and "你要热的" in hot["why"]
    soon = mind.choose({**RELAXED, "relaxed": False, "by": 300})
    assert soon["choice"]["wait"] == 0 and "来不及" in soon["why"]
    long = mind.choose({**FULL, "wait_s": 1320})
    assert long["choice"]["wait"] == 0 and "这次不等" in long["why"]


def test_half_cup_is_not_judged_by_the_full_cup():
    """只端过满杯: 半杯的风险按装量的倍数折算 (常识: 约三成), 不是和满杯一样高; 满杯学来的等凉, 半杯不照搬."""
    mind, _ = burned_mind({**RELAXED, "relaxed": False, "hot_pref": None})
    full = mind.predict(FULL, DEFAULT)
    half = mind.predict({**FULL, "fill": "half"}, DEFAULT)
    assert all(half[s]["burn"] < 0.5 * full[s]["burn"] for s in ("lift", "walk", "put"))
    assert mind.choose({**FULL, "fill": "half", "wait_s": 480})["choice"]["wait"] == 0


def test_task_score_guards_against_gaming():
    """评测的得分: 没送到 0 分 (不走); 少装就少送 (先倒空); 等凉按分钟扣, 晚了、要热的却凉了再扣 (一直等)."""
    raw = raw_trip({"walk": 20.0})
    raw.update(target=380.0, wait=0)
    ok = task_score(raw)
    assert task_score({**raw, "delivered": False}) == 0.0
    assert task_score({**raw, "left": 100.0}) < ok - 0.5
    waited = {**raw, "wait": 1320}
    assert task_score(waited) < ok - 0.2
    assert task_score(waited, {"relaxed": True}) > task_score(waited)
    assert task_score(waited, {"by": 300}) < task_score(waited) - 0.4
    assert task_score(waited, {"hot_truth": True}) < task_score(waited) - 0.2


def test_look_first_when_it_pays():
    """不知道装了多少 (你倒的, 没看到杯口): 没端过的她装多少都照常端, 看了也白看, 不看; 端过、知道满杯要端稳半杯不用的她,
    先看一眼; 看了以后按看到的挑. 不看就端的, 经历按端起来时看到的装量记."""
    fresh = CarryMind().choose({**FULL, "fill": None, "source": "user"})
    assert not fresh["look"] and fresh["choice"] == DEFAULT
    _, mind = run(FULL, 6, 0)
    poured = mind.choose({**FULL, "fill": None, "source": "user"})
    assert poured["look"] and "先看一眼" in poured["why"]
    assert mind.choose({**FULL, "fill": "half", "source": "user"})["choice"] == DEFAULT
    blind = CarryMind()
    blind.choose({**FULL, "fill": None})
    blind.learn(perceive(raw_trip({}, ml=150.0)))
    assert blind.records[-1]["ctx"]["fill"] == "half"


def test_not_walking_has_no_walk_stage():
    """不用走 (小屋里站在咖啡机前喝): 没有"先端到身前再走"这一项, "走"那一段不算风险."""
    ctx = {**FULL, "route": "none"}
    assert all(c["settle"] == 0 for c in options(ctx))
    assert CarryMind().predict(ctx, DEFAULT)["walk"]["spill"] == 0


def test_jerk_is_not_learned_as_how_to_carry():
    """动作本身的毛病 (她感觉到手抖了一下, felt jerk): 那一段洒了、烫了不拿去学端法. 每趟拿起时都抖一下、洒一大股 ->
    她不会因此一直放慢 (那是动作的毛病, 不是端法), 依据里说得出; 没抖却洒了的照样学."""
    jerky, plain = CarryMind(), CarryMind()
    for k in range(5):
        for mind, felt in ((jerky, {"pick": {"rightHand": {"level": "jerk"}}}), (plain, None)):
            mind.choose(FULL)
            mind.learn({**perceive(raw_trip({"pick": 30.0}, felt)), "stages": {
                **perceive(raw_trip({"pick": 30.0}))["stages"],
                "pick": {"spill": "stream", "drop": 0, "felt": [["右手", "jerk"]] if felt else []}}}, ev=k)
    assert jerky.choose(FULL)["choice"]["hand"] == "normal" and "手抖了一下" in jerky.choose(FULL)["why"]
    assert plain.choose(FULL)["choice"]["hand"] == "slow"
