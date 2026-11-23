"""记忆阶段 5 强度和稳定性 (wm/memory_strength.py + wm/memory.py 里的接线; 计划 backups/mem5-strength/计划.md).

剧本和词表在 tools/bench_strength.py (写于改代码之前, 哈希发给了评审); 这里把它们当回归测试, 再加上计划 5.1 的逐条检查:
off 零痕迹 / 等价 / shadow = off / 复习日子只数正向信号 / 信号 / 一天一次 / 没有前一轮不结算 / 词表 / 升长期差集 / 与阶段 4 合并 /
种子休眠 / 健壮 / 认知与真值 / 真实入口 / 记忆页."""
import asyncio
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta

import pytest

# 守卫: 这个文件在换进真 wm/memory.py 之前放在 backups/mem5-strength/draft/tests/, 不在 tests/ 下, 所以 tests/conftest.py 的自动夹具
# (换家目录、不碰真的 agent / 保管箱 / 服务状态)默认不会装到它身上; 和 tests/ 下的文件混在一条命令里跑时更不会 (11-15 评审在换过的家目录里
# 复现过: 冒出 .worldmodel/agent/blacklist.json 和 resident.sqlite3). 要用 run_draft.py (它会 -p conftest), 或者放回 tests/ 再跑.
if "conftest" not in sys.modules:
    pytest.skip("tests/conftest.py 没装上 (自动夹具不生效, 会碰真目录): 用 backups/mem5-strength/run_draft.py 跑, 或放回 tests/ 再跑",
                allow_module_level=True)

from test_pipeline import collect, make_cfg, run as run_ws
from wm import memory_ops as O
from wm import memory_strength as S
from wm.config import ROOT
from wm.memory import Memory

sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("bench_strength", ROOT / "tools" / "bench_strength.py")
BS = importlib.util.module_from_spec(_spec)
sys.modules["bench_strength"] = BS
_spec.loader.exec_module(BS)

DAY0 = datetime(2026, 10, 20, 20, 0)
SCRIPTS = BS.STRENGTH_DEV + BS.STRENGTH_HELD


@pytest.fixture(autouse=True)
def _conftest_fixtures_are_active(request):
    """第二道守卫: conftest 在 sys.modules 里不等于它的自动夹具装上了 (没当插件也没在上层目录). 没装就整个文件跳过."""
    if "_vault_home" not in request.fixturenames or "_agent_home" not in request.fixturenames:
        pytest.skip("tests/conftest.py 的自动夹具没装到这个文件 (_vault_home / _agent_home): 不跑, 免得碰真目录")


def _mem(tmp_path, mode="on", now=None, char="小流", **cfg):
    now = now if now is not None else {"t": DAY0}
    # on 档在代码里挡死了; 测试用只给测试的构造参数才跑得出 on
    m = Memory({"vault": str(tmp_path), "character": char, "graph_notes": False, "strength": {"mode": mode}, **cfg},
               **({"_test_unlock_strength_on": True} if mode == "on" else {}))
    m.clock = lambda: now["t"]
    m.init()
    m.start_session()
    return m, now


def _side(m):
    p = m.dir / "记忆" / S.FILE
    return json.loads(p.read_text(encoding="utf-8"))["items"] if p.exists() else {}


def _play(tmp_path, sc, mode):
    """跑一个剧本, 返回 (逐步 recall 的结果, 最后一问的结果, Memory)."""
    now = {"t": DAY0}
    m, _ = _mem(tmp_path, mode, now, sc.get("char", "小流"))
    BS.run_writes(m, sc["writes"], now, sc.get("hour", 20))
    qday, q, first = sc["q"]
    now["t"] = (DAY0 + timedelta(days=qday)).replace(hour=sc.get("hour", 20) + 1, minute=0)
    if first:
        m.start_session()
    return m, m.recall(q, first=first)


def _tree(m):
    return {str(p.relative_to(m.dir)): p.read_bytes() for p in sorted(m.dir.rglob("*")) if p.is_file() and not p.name.startswith(".strength")}


# ---------------------------------------------------------------------------------------------- 剧本 (开发组 + 留出组)
@pytest.mark.parametrize("sc", SCRIPTS, ids=lambda s: s["name"] + ("" if s in BS.STRENGTH_DEV else "(留出)"))
def test_scripts_pass_when_on(tmp_path, sc):
    out = BS.run_strength_one(Memory, sc, "on", None, [])
    assert out["ok"], out


@pytest.mark.parametrize("sc", SCRIPTS, ids=lambda s: s["name"] + ("" if s in BS.STRENGTH_DEV else "(留出)"))
def test_off_leaves_no_trace_and_shadow_equals_off(tmp_path, sc):
    """off 档不建旁路、检索结果和没有这项配置时一样; shadow 档检索结果、提示词、.recall.json、Markdown 都逐字节和 off 一样."""
    res = {}
    for mode in ("none", "off", "shadow"):
        d = tmp_path / mode
        d.mkdir()
        now = {"t": DAY0}
        cfg = {} if mode == "none" else {"strength": {"mode": mode}}
        m = Memory({"vault": str(d), "character": sc.get("char", "小流"), "graph_notes": False, **cfg})
        m.clock = lambda now=now: now["t"]
        m.init()
        m.start_session()
        BS.run_writes(m, sc["writes"], now, sc.get("hour", 20))
        qday, q, first = sc["q"]
        now["t"] = (DAY0 + timedelta(days=qday)).replace(hour=sc.get("hour", 20) + 1, minute=0)
        if first:
            m.start_session()
        r = m.recall(q, first=first)
        res[mode] = ({k: r[k] for k in ("line", "hits", "answer", "mode")}, _tree(m), m)
    assert res["off"][:2] == res["none"][:2]
    assert not [p for p in res["off"][2].dir.rglob(".strength*")] and not [p for p in res["none"][2].dir.rglob(".strength*")]
    assert res["shadow"][0] == res["off"][0]
    assert res["shadow"][1] == res["off"][1]  # 包括 .recall.json 和所有 Markdown


# ---------------------------------------------------------------------------------------------- 等价
def _fresh_pair(tmp_path, texts, strength_b=None):
    a, na = _mem(tmp_path / "a", "off")
    b, nb = _mem(tmp_path / "b", "on", **({"strength": {"mode": "on", "b": strength_b}} if strength_b is not None else {}))
    for m, now in ((a, na), (b, nb)):
        for i, text in enumerate(texts):
            now["t"] = DAY0 - timedelta(days=15 * i)
            m.add_summary(f"{now['t']:%Y-%m-%d}", [{"text": text, "times": ["12:00"]}], 0)
    return a, na, b, nb


def _same(a, na, b, nb):
    for days in (0, 1, 7, 30, 61, 120, 250, 400):
        for m, now in ((a, na), (b, nb)):
            now["t"] = DAY0 + timedelta(days=days)
            m._refresh()
        fa, fb = a._freshness(DAY0.date() + timedelta(days=days)), b._freshness(DAY0.date() + timedelta(days=days))
        if fa != fb:
            return False
    return len(fa) > 0


def test_untouched_items_score_exactly_like_before(tmp_path):
    """没被动过、没复习、显著性在基线 (0.2)、没情绪: on 档的新鲜度和现在逐位相同 (==, 不是 approx).
    显著的事 (健康、大事、带日期的安排…, 显著性 > 0.2) 即使没复习过也会因为 b 项淡得慢一点 —— 那是公式里的 b·显著性, 不是等价;
    b=0 时所有条目都逐位相同, b 取多少由 A/B 定 (计划 5.3)."""
    plain = ["用户去超市买了一盒酸奶", "用户最近在整理书架", "用户中午吃了一碗牛肉面", "用户的台灯前几天坏了"]
    for t in plain:
        assert S.StrengthStore is not None and __import__("wm.memory_relay", fromlist=["salience"]).salience(t)[0] == 0.2
    assert _same(*_fresh_pair(tmp_path / "p", plain))
    salient = plain + ["用户10月3日去了美术馆", "用户在学画画"]
    assert _same(*_fresh_pair(tmp_path / "s0", salient, strength_b=0.0))           # b=0: 全部逐位相同
    assert not _same(*_fresh_pair(tmp_path / "s1", salient))                       # b>0: 显著的会不一样 (说明白, 不假装等价)
    b, _ = _mem(tmp_path / "m", "on")
    for text in plain:
        assert b.strength.mult(text, 0.2, 1.0) == 1.0 and b.strength.g_of(text) == 1.0
    assert S.s0_days(30) == pytest.approx(43.2808, abs=1e-3)  # 方案页写的 43 是它的近似


def test_only_retrieved_never_used_does_not_get_stabler(tmp_path):
    """只被检索带进提示词、回复没用上, 带进 10 个不同的日子: 复习日子为空, m 仍是 1, 新鲜度和 off 一样 (召回就加是评审点名不要的)."""
    outs = {}
    for mode in ("off", "on"):
        m, now = _mem(tmp_path / mode, mode)
        m.add_summary("2026-10-20", [{"text": "用户10月21日去了水族馆", "times": ["20:00"]}], 0)
        for d in range(1, 11):
            now["t"] = DAY0 + timedelta(days=d * 2)
            assert m.recall("水族馆那次好玩吗")["hits"]
            m.observe("水族馆那次好玩吗", "是啊挺好玩的", [], "calm")
        outs[mode] = (m, now)
    m, now = outs["on"]
    assert _side(m) == {} or all(not e.get("rd") for e in _side(m).values())
    assert m.strength.mult("用户10月21日去了水族馆", 0.2, 1.0) == 1.0
    assert m._freshness(now["t"].date()) == outs["off"][0]._freshness(now["t"].date())


# ---------------------------------------------------------------------------------------------- 强度公式
def test_store_formulas_bounds_and_once_per_day(tmp_path):
    st = S.StrengthStore(lambda: tmp_path / S.FILE, lambda: tmp_path / S.SHADOW_FILE, {"mode": "on"})
    p = st.p
    assert st.sigma("x") == 0.5 and st.g_of("x") == 1.0
    assert st.reinforce("x", "use", "2026-10-20") and st.sigma("x") == pytest.approx(0.5 + p["eta_use"] * 0.5)
    s1 = st.sigma("x")
    assert not st.reinforce("x", "use", "2026-10-20") and st.sigma("x") == s1  # 同一天同一种只加一次
    st.reinforce("x", "affirm", "2026-10-20")  # 当天稍后被肯定: 按较大的 η 补差, 不是叠加
    assert st.sigma("x") == pytest.approx(0.5 + p["eta_confirm"] * 0.5)
    assert not st.reinforce("x", "use", "2026-10-20") and st.sigma("x") == pytest.approx(0.5 + p["eta_confirm"] * 0.5)
    for d in range(1, 200):  # 日复一日地加也到不了 1; g 不超过 g_max
        st.reinforce("x", "affirm", f"2026-11-{1 + d % 28:02d}-{d}")
    assert st.sigma("x") < 1.0 and st.g_of("x") <= p["g_max"] + 1e-12
    assert st.weaken("y", "correct", "2026-10-20") and st.sigma("y") == pytest.approx(0.5 * (1 - p["eta_down"]))
    assert not st.weaken("y", "correct", "2026-10-20")  # 一天最多减一次
    for d in range(60):
        st.weaken("y", "correct", f"d{d}")
    assert st.g_of("y") == p["g_min"] and st.sigma("y") > 0
    assert st.mult("x", 0.2, 1.0) <= p["max_x"] and st.mult("x", 1.0, 3.0) <= p["max_x"]
    assert st.rd("x") <= 60
    # 复习日子数只增加在有正向信号的那天
    assert S.StrengthStore(lambda: tmp_path / "n.json", lambda: tmp_path / "n.jsonl", {"mode": "on"}).mult("z", 0.2, 1.0) == 1.0


def test_stability_grows_with_positive_days_and_is_capped(tmp_path):
    st = S.StrengthStore(lambda: tmp_path / S.FILE, lambda: tmp_path / S.SHADOW_FILE, {"mode": "on"})
    ms = []
    for d in (1, 2, 4, 7, 15, 30, 60):  # 1/2/4/7/15/30/60 天的复习间隔
        st.reinforce("k", "repeat", f"2026-10-{d:02d}")
        ms.append(st.mult("k", 0.2, 1.0))
    assert ms == sorted(ms) and ms[0] == pytest.approx(1.35) and ms[-1] <= st.p["max_x"]
    assert st.mult("k", 0.2, 3.0) <= st.p["max_x"]  # 情绪 ×3 也封顶
    assert st.mult("k", 0.9, 1.0) > st.mult("k", 0.2, 1.0)  # 显著的稳定得更久 (只吃超过基线 0.2 的部分)


# ---------------------------------------------------------------------------------------------- 信号
def _two(tmp_path, mode="on"):
    m, now = _mem(tmp_path, mode)
    m.add_summary("2026-10-20", [{"text": "用户10月21日去了水族馆", "times": ["20:00"]}], 0)
    m.add_summary("2026-10-20", [{"text": "用户10月21日去了植物园", "times": ["20:01"]}], 0)
    return m, now


def _turn(m, now, user, reply, minutes=1):
    now["t"] += timedelta(minutes=minutes)
    m.recall(user)
    m.observe(user, reply, [], "calm")


def test_use_then_nothing_wrong_adds_a_little_affirm_adds_more_correct_removes(tmp_path):
    for follow, check in (("好的", lambda s: 0.5 < s < 0.56), ("对，没错", lambda s: s > 0.6), ("不对，不是这样", lambda s: s < 0.5)):
        d = tmp_path / follow
        m, now = _two(d)
        now["t"] = DAY0 + timedelta(days=5)
        _turn(m, now, "我10月21日去了哪里", "你10月21日去了植物园呀")
        assert _side(m) == {}  # 还没有下一句: 不加不减 (用上不等于顺利)
        _turn(m, now, follow, "嗯")
        side = _side(m)
        assert check(side["用户10月21日去了植物园"]["s"]), (follow, side)
        assert "用户10月21日去了水族馆" not in side  # 没用上的一条不动 (也不罚)


def test_no_previous_turn_late_or_question_do_nothing(tmp_path):
    m, now = _two(tmp_path)
    now["t"] = DAY0 + timedelta(days=5)
    _turn(m, now, "不对，不是这样", "嗯")                          # 前面没有带记忆的一轮
    _turn(m, now, "我10月21日去了哪里", "你10月21日去了植物园呀")
    _turn(m, now, "不对，不是这样", "好的", minutes=40)            # 隔了 40 分钟
    _turn(m, now, "我10月21日去了哪里", "你10月21日去了植物园呀")
    _turn(m, now, "这样不对吗？", "嗯")                           # 问句: 当"没事"算, 不当纠正
    s = _side(m).get("用户10月21日去了植物园", {"s": 0.5})["s"]
    assert s >= 0.5


def test_association_is_neither_counted_nor_used_for_signal(tmp_path):
    m, now = _mem(tmp_path)
    m.add_summary("2026-10-20", [{"text": "用户的猫叫豆豆，10月22日它生病了", "times": ["20:00"]}], 0)
    m.add_summary("2026-10-20", [{"text": "用户的猫10月23日去了医院", "times": ["20:01"]}], 0)
    for d in range(1, 8):
        now["t"] = DAY0 + timedelta(days=d)
        _turn(m, now, "豆豆最近怎么样", "豆豆去了医院，已经好多了")
        _turn(m, now, "对，没错", "嗯")
    assert "用户的猫10月23日去了医院" not in _side(m)
    assert len(m._recall_dates()["用户的猫10月23日去了医院"]["days"]) == 1  # 联想带出不加"提到过的日子" (本来就这样)


def test_signals_only_from_the_owners_turn_pipeline(tmp_path):
    """只有 recall 带进了记忆 (q 非空、mode 不是 skip) 才登记; observe 的话和 q 逐字相同才结算; 日记本检索不登记也不盖掉."""
    m, now = _two(tmp_path)
    now["t"] = DAY0 + timedelta(days=5)
    assert m.recall("")["hits"] == [] and m._pending is None
    m.recall("举起右手")  # 动作指令: skip
    assert m._pending is None
    m.recall("我10月21日去了哪里")
    keep = m._pending
    assert keep and keep["q"] == "我10月21日去了哪里"
    m.diary_recall("植物园")
    assert m._pending is keep
    m.observe("另一句话", "你10月21日去了植物园呀", [], "calm")  # q 对不上: 不结算 (并发的别的连接串不进来)
    assert m._prev is None and _side(m) == {}
    off, _ = _two(tmp_path / "off", "off")
    off.recall("我10月21日去了哪里")
    assert off._pending is None


def test_every_memory_entry_point_is_known():
    """Memory.recall / observe / mentioned / diary_recall 在 wm/ 里的调用点恰好是这几处; 以后有人新接一个入口这条会红, 提醒去想
    它是不是主人本人的话 (群、访客、陪看、别人的话一律不该结算)."""
    pat = re.compile(r"\b(?:self\.e\.memory|self\.mem|mem|memory)\.(?:observe|recall|mentioned|diary_recall)\b")
    found = {}
    for p in (ROOT / "wm").rglob("*.py"):
        if p.name.startswith("memory"):
            continue
        n = len(pat.findall(p.read_text(encoding="utf-8", errors="replace")))
        if n:
            found[p.relative_to(ROOT).as_posix()] = n
    assert found == {"wm/pipeline.py": 3, "wm/digest.py": 1}, found


# ---------------------------------------------------------------------------------------------- 词表
def test_cue_lists():
    """验收 (评审第 2 轮改成三条): ① 没事的句子 (标"无") 不能判成纠正 / 拒绝 / 肯定: 只能是"无"或"疑"; ② 纠正的句子最后不能被加强: 只能是
    "纠"或"疑" (落进"无"或"肯"都不行); 拒绝的只能是"拒"或"疑"; ③ 召回 (有标签的精确判对) 照实报, 不设线."""
    ok = {"无": {"无", "疑"}, "纠": {"纠", "疑"}, "肯": {"肯", "疑", "无"}, "拒": {"拒", "疑"}}
    report = {}
    for name, cues in (("开发组", BS.CUES_DEV), ("留出组(看过)", BS.CUES_HELDOUT), ("评审的24句(开发)", BS.CUES_DEV2), ("补的负例", BS.CUES_DEV3)):
        bad = [(t, lab, S.classify(t)) for t, lab in cues if S.classify(t) not in ok[lab]]
        assert bad == [], (name, bad)
        pos = [(t, lab) for t, lab in cues if lab != "无"]
        report[name] = (sum(S.classify(t) == lab for t, lab in pos), len(pos))
    print("精确判对 / 有标签:", report)
    for t in ("不是，我是说……", "不对啊我想想", "对了，换个话题", "对不起", "错过了", "对面那家店", "好的", "", "不是很懂", "对手太强了",
              "不对啊，这题怎么做来着"):
        assert S.classify(t) in ("无", "疑"), t
    for t in ("你又记混了", "错错错，是周五", "哪有，我是去年才去的", "不是这个，是另一家", "你记成别人了吧", "瞎说，我没说过要去"):
        assert S.classify(t) in ("纠", "疑"), t  # 漏掉的纠正至少不能落进"无"被当成"用上、没事"去加强


def test_only_plain_acknowledgements_count_as_nothing_wrong_and_corrections_must_be_tight():
    """规则一: "无"(加一点)只给应声 / 道谢 / 笑 / 道别, 其余一律"疑"; 规则二: "纠"要窄 —— 句子长、带转折 / 条件、说的是第三个人的 -> "疑"."""
    for t in ("好的", "嗯嗯", "谢谢", "哈哈哈", "知道了", "晚安"):
        assert S.classify(t) == "无", t
    for t in ("我觉得你说得不太准", "今天天气不错", "那个人好像说的是周三", "有点怪怪的", "你这样说我就不太舒服了"):
        assert S.classify(t) == "疑", t  # 不是应声: 不动 (漏判的纠正不会落进"无")
    assert S.classify("不对，我说的是上海") == "纠" and S.classify("你记错了") == "纠"
    for t in ("不对，他说的是周三", "不对，但是我也不确定", "你记错了吧，如果是去年的话", "不对" + "，是这样的" * 8):
        assert S.classify(t) == "疑", t


def test_rule1_any_negation_char_blocks_nothing_wrong_and_rule2_needs_tight_budui():
    """规则一 (协调对话 11-15): 带否定字 (没、不、别、错、无、非) 的句子不能判成"无"; 规则二: "不对"后面紧跟标点 / 句末 / 语气字, 或者再跟一个
    "不对", 才算纠正, 后面接别的字归"疑"."""
    for t in ("没问题的", "不错", "别担心", "谢谢你，没事", "无所谓", "非常好", "好的，不用了", "行，别客气"):
        assert S.classify(t) != "无", t
    for t in ("好的", "嗯嗯", "谢谢", "哈哈哈", "知道了"):
        assert S.classify(t) == "无", t
    for t in ("不对", "不对，是上海", "不对哦，是下午三点", "不对啊，是周三", "不对不对，不是杭州", "这不对。", "不对！"):
        assert S.classify(t) == "纠", t
    for t in ("不对劲", "不对称", "不对头", "不对啊我想想", "不对我记得是周三", "不对的地方我改一下"):
        assert S.classify(t) == "疑", t
    for name, cues in (("开发", BS.CUES_DEV), ("留出", BS.CUES_HELDOUT), ("评审24句", BS.CUES_DEV2), ("补", BS.CUES_DEV3)):
        for t, _ in cues:
            tier, rule = S.classify_why(t)
            assert tier in ("纠", "肯", "拒", "无", "疑") and rule and S.classify(t) == tier, (name, t)  # 每句都报得出命中的规则名


def test_on_is_blocked_in_code_and_production_config_cannot_unlock_it(tmp_path, caplog):
    """on 档在代码里挡死: 配置写 on, 按 shadow 跑, 日志有那句话, 检索结果和 shadow 逐字节一样; 解锁只能用只给测试的构造参数,
    生产配置 (含各种想绕过的键) 打不开, 生产代码也不传它."""
    import inspect
    import logging

    sc = BS.STRENGTH_DEV[0]
    res = {}
    for name, cfgmode, kw in (("shadow", "shadow", {}), ("blocked", "on", {}), ("sneaky", "on", {"x": 1})):
        d = tmp_path / name
        d.mkdir()
        now = {"t": DAY0}
        scfg = {"mode": cfgmode} if name != "sneaky" else {"mode": "on", "_test_unlock_strength_on": True, "unlock": True, "allow_on": True}
        with caplog.at_level(logging.WARNING, logger="wm.memory"):
            caplog.clear()
            m = Memory({"vault": str(d), "character": "小流", "graph_notes": False, "strength": scfg,
                        **({"_test_unlock_strength_on": True} if name == "sneaky" else {})})
            logged = [r.getMessage() for r in caplog.records]
        m.clock = lambda now=now: now["t"]
        m.init()
        m.start_session()
        BS.run_writes(m, sc["writes"], now, 20)
        qday, q, first = sc["q"]
        now["t"] = (DAY0 + timedelta(days=qday)).replace(hour=21, minute=0)
        r = m.recall(q, first=first)
        res[name] = ({k: r[k] for k in ("line", "hits", "answer", "mode")}, _tree(m), m.strength.mode, logged,
                     (d / "小流" / "记忆" / S.FILE).read_bytes())
    assert res["shadow"][2] == res["blocked"][2] == res["sneaky"][2] == "shadow"
    assert res["blocked"][:2] == res["shadow"][:2] and res["sneaky"][:2] == res["shadow"][:2]  # 检索结果、Markdown 逐字节一样
    assert res["blocked"][4] == res["shadow"][4]  # 连 σ 的旁路也一样 (影子档照算照记)
    assert any(S.ON_BLOCKED in x for x in res["blocked"][3]) and any(S.ON_BLOCKED in x for x in res["sneaky"][3])
    assert not res["shadow"][3]  # 本来就是 shadow 的不报
    # 解锁参数: 只能关键字传, 名字带下划线; 生产代码里没有一处传它
    p = inspect.signature(Memory.__init__).parameters["_test_unlock_strength_on"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is False
    for f in (ROOT / "wm").rglob("*.py"):
        if f.name not in ("memory.py", "memory_strength.py"):  # memory_strength.py 里只有文档字符串提到它的名字
            assert "_test_unlock_strength_on" not in f.read_text(encoding="utf-8", errors="replace"), f
    # 评测台 / 测试里的 on 确实靠这个参数: 不给就按 shadow
    plain, _ = _mem(tmp_path / "plain", "off")
    on, _ = _mem(tmp_path / "on", "on")
    assert plain.strength.mode == "off" and on.strength.mode == "on"


def test_verdict_log_has_tier_rule_and_hashes_but_no_text(tmp_path):
    """影子判定记录: 每结算一次 (纠 / 肯 / 无 / 疑 / 拒) 记一行 —— 日期、档、规则名、涉及记忆的短哈希、是不是种子; 不记正文和用户的话; 有上限."""
    m, now = _two(tmp_path / "v", "shadow")
    now["t"] = DAY0 + timedelta(days=5)
    follows = ["好的", "对，没错", "不对，不是这样", "你又记混了", "那水族馆呢"]
    for f in follows:
        _turn(m, now, "我10月21日去了哪里", "你10月21日去了植物园呀")
        _turn(m, now, f, "嗯")
        now["t"] += timedelta(days=1)
    log = (m.dir / "记忆" / S.VERDICT_FILE).read_text(encoding="utf-8")
    rows = [json.loads(x) for x in log.splitlines()]
    assert len(rows) == len(follows)  # 每个“下一句”结算一次
    assert all(set(r) == {"day", "tier", "rule", "k", "seed"} and r["rule"] and r["tier"] in "纠肯拒无疑" for r in rows)
    assert {r["tier"] for r in rows} >= {"无", "肯", "纠", "疑"}
    assert all(re.fullmatch(r"[0-9a-f]{8}", k) for r in rows for k in r["k"])
    for banned in ("植物园", "水族馆", "用户", "10月", "不对，不是这样", "对，没错", "你又记混了", "那水族馆呢", "好的"):
        assert banned not in log, banned
    m.strength.p["verdict_log"] = 4
    for _ in range(10):
        m.strength.verdict_append("2026-10-20", "无", "无:应声", ["a"], False)
    assert len((m.dir / "记忆" / S.VERDICT_FILE).read_text(encoding="utf-8").splitlines()) == 4
    off, noff = _two(tmp_path / "voff", "off")
    _turn(off, noff, "我10月21日去了哪里", "你10月21日去了植物园呀")
    _turn(off, noff, "好的", "嗯")
    assert not (off.dir / "记忆" / S.VERDICT_FILE).exists()


def test_doubtful_next_sentence_changes_nothing(tmp_path):
    """"疑"这一档什么都不动: 带纠正意味又判不准的句子 (评审给的例子), 记错的那条不能反而被加强."""
    for follow in ("你又记混了", "错错错，是周五", "哪有，我是去年才去的", "不是这个，是另一家", "你记成别人了吧", "不对啊，这题怎么做来着", "那水族馆呢"):
        m, now = _two(tmp_path / f"d{abs(hash(follow))}")
        now["t"] = DAY0 + timedelta(days=5)
        _turn(m, now, "我10月21日去了哪里", "你10月21日去了植物园呀")
        _turn(m, now, follow, "嗯")
        assert _side(m).get("用户10月21日去了植物园", {"s": 0.5})["s"] <= 0.5, follow


def test_salience_and_affect_together_never_exceed_the_affect_cap(tmp_path):
    """b·显著性 和 4b 的情绪乘数是同一类效果 (让某些事淡得慢): 合起来不超过 affect.max_halflife_x (3); 复习天数项不受这个限制,
    总上限还是 max_x. 最坏情形: 显著性 1.0、情绪最强 (x=3)、affect 开着."""
    st = S.StrengthStore(lambda: tmp_path / S.FILE, lambda: tmp_path / S.SHADOW_FILE, {"mode": "on"})
    assert st.mult("k", 1.0, 3.0, cap=3.0) == 3.0                 # 1.4 × 3 = 4.2 -> 封在 3
    assert st.mult("k", 1.0, 1.0, cap=3.0) == pytest.approx(1.4)  # affect 关 (x=1): 只有显著性, 没碰上限
    assert st.mult("k", 0.2, 3.0, cap=3.0) == 3.0                 # 基线显著性: 就是阶段 4 的 x, 一个字不变
    for sal in (0.2, 0.5, 1.0):
        for x in (1.0, 1.7, 3.0):
            assert st.mult("k", sal, x, cap=3.0) <= 3.0
    for d in (1, 2, 4):
        st.reinforce("k", "repeat", f"2026-10-0{d}")
    assert st.mult("k", 1.0, 3.0, cap=3.0) == pytest.approx(min(st.p["max_x"], 3.0 * (1 + 0.35 * 3)))  # 复习项在上限之外相乘
    # 接到 Memory: 显著的事 + 情绪最强 + affect 开着, 新鲜度和"半衰期 × 3"一样, 不会更慢
    m, now = _mem(tmp_path / "m", "on", affect={"mode": "on", "max_halflife_x": 3})
    text = "用户的爸爸住院做了心脏手术，用户吓坏了"
    assert m._sal(text) >= 0.6
    m.add_summary("2026-10-20", [{"text": text, "times": ["20:00"]}], 0)
    m.affect.halflife_x = lambda c: 3.0
    now["t"] = DAY0 + timedelta(days=90)
    m._refresh()
    assert m._freshness(now["t"].date())[0][0] == O.freshness(90, m.half_life * 3.0)
    off, noff = _mem(tmp_path / "o", "off", affect={"mode": "on", "max_halflife_x": 3})
    off.add_summary("2026-10-20", [{"text": text, "times": ["20:00"]}], 0)
    off.affect.halflife_x = lambda c: 3.0
    noff["t"] = DAY0 + timedelta(days=90)
    off._refresh()
    assert off._freshness(noff["t"].date())[0][0] == m._freshness(now["t"].date())[0][0]  # 和阶段 4 现状一样


def test_used_ignores_dates_and_fillers():
    a, b = "用户10月21日去了植物园", "用户10月21日去了水族馆"
    reply = "你10月21日去了植物园呀"
    assert S.used(reply, a) and not S.used(reply, b)  # 日期相同不能算"用上"
    assert S.used("你叫小明呀", "用户叫小明") and not S.used("你好呀", "用户叫小明")
    assert not S.used("", a) and not S.used(reply, "用户")


# ---------------------------------------------------------------------------------------------- 升长期
def test_promotion_requires_strength_over_the_line(tmp_path):
    text = "用户在学画画"

    def run(mode):
        m, now = _mem(tmp_path / mode, mode)
        m.add_summary("2026-10-20", [{"text": text, "times": ["20:00"]}], 0)
        now["t"] = DAY0 + timedelta(days=1)
        _turn(m, now, "我学画画的事", "你在学画画呀")
        _turn(m, now, "不对，不是这样", "好的")          # 被纠正过: σ < σ0
        for d in (2, 3):
            m.mentioned(f"2026-10-2{d + 1}", [text])    # 又说到两天, 加上说的那天 = 3 天
        return "#长期" in (m.dir / "记忆" / "对话摘要.md").read_text(encoding="utf-8")

    assert run("off") is True and run("shadow") is True   # 旧条件: 够 3 天就升
    assert run("on") is False                              # 新条件: 强度没过线 -> 不升 (旧条件能升、新条件不升的只有这一类)

    def plain(mode):  # 没被动过的: 升不升和现在完全一样
        m, now = _mem(tmp_path / ("p" + mode), mode)
        m.add_summary("2026-10-20", [{"text": text, "times": ["20:00"]}], 0)
        for d in (1, 2):
            m.mentioned(f"2026-10-2{d}", [text])
        return "#长期" in (m.dir / "记忆" / "对话摘要.md").read_text(encoding="utf-8")

    assert plain("off") is plain("shadow") is plain("on") is True


# ---------------------------------------------------------------------------------------------- 与阶段 4 合并
def test_affect_factor_enters_stability_once_and_only_when_affect_is_on(tmp_path):
    text = "用户去超市买了一盒酸奶"
    res = {}
    for aff in ("off", "on"):
        for sm in ("off", "on"):
            d = tmp_path / f"{aff}{sm}"
            m, now = _mem(d, sm, affect={"mode": aff})
            m.add_summary("2026-10-20", [{"text": text, "times": ["20:00"]}], 0)
            m.affect.halflife_x = lambda c: 2.5  # 这块连着一条强情绪
            now["t"] = DAY0 + timedelta(days=60)
            m._refresh()
            res[aff, sm] = m._freshness(now["t"].date())[0][0]
    assert res["off", "off"] == res["off", "on"]                 # 情绪不是 on: 乘数为 1, strength 开不开都一样
    assert res["on", "off"] == res["on", "on"]                   # 情绪 on: 半衰期 × x 只乘一遍 (没有重复乘)
    assert res["on", "on"] > res["off", "on"]
    m, _ = _mem(tmp_path / "eta", "on", affect={"mode": "on"})
    m.affect.strength = lambda c: 0.8
    m.add_summary("2026-10-20", [{"text": text, "times": ["20:00"]}], 0)
    m._refresh()
    assert m._eta_mult(text) == pytest.approx(1.8)
    m.affect.strength = lambda c: 3.0
    assert m._eta_mult(text) == 2.0                              # 上限 2
    off, _ = _mem(tmp_path / "eta2", "on", affect={"mode": "off"})
    off.add_summary("2026-10-20", [{"text": text, "times": ["20:00"]}], 0)
    off._refresh()
    assert off._eta_mult(text) == 1.0


# ---------------------------------------------------------------------------------------------- 种子
def test_seed_hello_goes_dormant_when_corrected_and_comes_back_on_reset(tmp_path):
    sc = next(s for s in BS.STRENGTH_DEV if s["name"] == "种子被纠正")
    m, r = _play(tmp_path, sc, "on")
    assert S.HELLO_LINE not in (r["line"] or "") and m.strength.dormant()
    assert m.reset_seed_strength() and not m.strength.dormant()
    r2 = m.recall("早上好呀", first=True)
    assert S.HELLO_LINE in (r2["line"] or "")
    # 没被纠正的: 一直提; 纠正一次 (σ=0.325) 还没到休眠线
    one = {**sc, "writes": sc["writes"][:6]}
    m1, r1 = _play(tmp_path / "one", one, "on")
    assert not m1.strength.dormant() and 0.25 < m1.strength.sigma(S.SEED_HELLO) < 0.5
    assert S.HELLO_LINE in (r1["line"] or "")


# ---------------------------------------------------------------------------------------------- 健壮
def test_bad_sidecar_files_are_treated_as_empty_and_write_failure_is_silent(tmp_path):
    m, now = _two(tmp_path)
    path = m.dir / "记忆" / S.FILE
    for body in ("", "not json", "[1,2]", '{"items": 3}', '{"items": {"a": 1, "b": {"s": "x"}}}', "[" * 100000):
        path.write_text(body, encoding="utf-8")
        m.strength._sig = None
        assert m.strength.items() == {} and m.strength.g_of("a") == 1.0
        now["t"] = DAY0 + timedelta(days=3)
        assert m.recall("我10月21日去了哪里")["hits"]  # 检索照常
    path.unlink()
    path.mkdir()  # 路径被占成目录: 写不上, 不抛
    m.strength._sig = None
    m.strength.reinforce("k", "use", "2026-10-20")
    assert m.strength.prune({"a"}) in (0, 1)
    path.rmdir()
    m.strength._sig = None
    m.strength.reinforce("k", "use", "2026-10-20")
    m.strength.prune(set())
    assert "k" not in _side(m)
    m.strength.reinforce("seed:habit:2", "use", "2026-10-20")
    m.strength.prune(set())
    assert "seed:habit:2" in _side(m)  # 种子的键不清


def test_shadow_log_has_no_memory_text_and_is_capped(tmp_path):
    sc = BS.STRENGTH_DEV[0]
    m, r = _play(tmp_path, sc, "shadow")
    assert r["strength"]["mode"] == "shadow" and r["strength"]["moved"]
    log = (m.dir / "记忆" / S.SHADOW_FILE).read_text(encoding="utf-8")
    assert log.strip() and "植物园" not in log and "水族馆" not in log and "用户" not in log
    row = json.loads(log.splitlines()[-1])
    assert set(row) == {"day", "k", "old", "new", "g", "m"} and re.fullmatch(r"[0-9a-f]{8}", row["k"])
    m.strength.p["shadow_log"] = 5
    for _ in range(20):
        m.strength.shadow_append("2026-10-20", [("a", 1, 2, 1.0, 1.0)])
    assert len((m.dir / "记忆" / S.SHADOW_FILE).read_text(encoding="utf-8").splitlines()) == 5


def test_strength_uses_only_what_she_heard():
    """认知与真值分开: 强度只收文本、回复、检索命中, 不 import 小屋 / 世界的任何东西 (这一阶段不碰小屋事件, 所以不需要新的双胞胎测试)."""
    src = (ROOT / "wm" / "memory_strength.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+.*(?:cognition|room|world|belief|roomworld)", src, re.M)
    assert S.classify.__code__.co_argcount == 1 and S.used.__code__.co_argcount == 3


# ---------------------------------------------------------------------------------------------- 记忆页、检索限时
def test_overview_has_strength_only_when_not_off(tmp_path):
    m, now = _two(tmp_path / "on")
    now["t"] = DAY0 + timedelta(days=5)
    _turn(m, now, "我10月21日去了哪里", "你10月21日去了植物园呀")
    _turn(m, now, "对，没错", "嗯")
    f = {x["text"]: x for x in m.overview()["facts"]}
    assert f["用户10月21日去了植物园"]["strength"]["sigma"] > 0.5 and f["用户10月21日去了水族馆"]["strength"]["sigma"] == 0.5
    off, _ = _two(tmp_path / "off", "off")
    assert all("strength" not in x for x in off.overview()["facts"])


def test_unrelated_question_still_brings_no_memory_after_reinforcement(tmp_path):
    m, now = _two(tmp_path)
    now["t"] = DAY0 + timedelta(days=5)
    for _ in range(3):
        _turn(m, now, "我10月21日去了哪里", "你10月21日去了植物园呀")
        _turn(m, now, "对，没错", "嗯")
    now["t"] += timedelta(days=30)
    r = m.recall("今天天气怎么样")
    assert r["line"] is None and r["hits"] == []


# ---------------------------------------------------------------------------------------------- 真实入口 (流水线, 假模型)
def test_pipeline_turns_write_strength_sidecar(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = make_cfg(tmp_path, f"memory.vault={json.dumps(str(vault))}", "memory.strength.mode=shadow")

    async def scenario(ws, hello):
        for i, t in enumerate(["我喜欢吃草莓", "我喜欢吃什么", "对，没错"], 1):
            await ws.send_json({"type": "text", "text": t})
            await collect(ws, i)
        for _ in range(30):  # observe 在后台线程里写
            await asyncio.sleep(0.1)
            side = list(vault.rglob(S.FILE))
            if side and "用户喜欢吃草莓" in side[0].read_text(encoding="utf-8"):
                return side[0]
        return None

    side = run_ws(cfg, scenario)
    assert side is not None
    e = json.loads(side.read_text(encoding="utf-8"))["items"]["用户喜欢吃草莓"]
    assert e["s"] > 0.5 and e["rd"]
