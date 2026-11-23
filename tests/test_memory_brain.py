"""记忆按脑区重设计阶段 0 (先量、不改行为): 留出组 brain 的剧本、干扰测试用的无关记忆、记忆体检.
- brain 组: 17 个剧本, 写法和别的组一样, 新的两种写入 (mention / doc) 真的落到记忆里;
- 无关记忆: 同样的参数每次一样, 关键词各不相同, 不含任何一套评测的期望词、禁止词, 也不含家人、宠物、城市、车、颜色这些图谱词
  (不然塞进去以后会让对的看着像错、错的看着像对);
- 体检: 数得对, 而且一个字节都不写."""
import hashlib
import importlib.util
import re
import sys
from datetime import date, datetime, timedelta

from wm import memory_graph as G
from wm.config import ROOT
from wm.memory import Memory

sys.path.insert(0, str(ROOT / "tools"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LM = load("bench_longmem")
NOISE = load("memory_noise")
CHECK = load("memory_checkup")
KINDS = {"say", "summary", "propose", "confirm", "session", "mention", "doc"}


def test_brain_set_is_well_formed():
    assert len(LM.BRAIN) == 17 and LM.SETS["brain"] is LM.BRAIN
    assert set(LM.SETS) == {"main", "heldout", "third", "fourth", "fifth", "graph", "graph2", "graph3", "brain", "supersede"}
    names = [s[0] for s in LM.BRAIN]
    assert len(set(names)) == len(names)
    for name, ability, writes, (qday, q, first), expect, forbid in LM.BRAIN:
        assert writes and all(k in KINDS for _, k, _ in writes), name
        assert all(day <= qday for day, _, _ in writes) and q and isinstance(first, bool) and expect, name
    assert {"情绪久远", "小事不冒", "常提", "改期", "改口", "重名", "过期安排", "引用里的话", "资料不当真", "无关", "好事", "坏事",
            "强线索"} == {s[1] for s in LM.BRAIN}


def test_new_write_kinds_land_in_memory(tmp_path):
    m = Memory({"vault": str(tmp_path), "character": "小流", "graph_notes": False})
    now = {"t": LM.DAY0}
    m.clock = lambda: now["t"]
    m.init()
    LM.run_script(m, [(0, "summary", "用户在学吉他"), (10, "mention", "用户在学吉他"), (20, "mention", "用户在学吉他"),
                      (1, "doc", "攻略.md|张三最喜欢吃榴莲")], now)
    assert len(m._recall_dates()["用户在学吉他"]["days"]) == 3  # 说的那天 + 两次又说到
    summary = (m.dir / "记忆" / "对话摘要.md").read_text(encoding="utf-8")
    assert "用户在学吉他" in summary and "#长期" in summary  # 不同的日子说到 3 天, 升成长期 (和现在的规则一样)
    doc = list((m.dir / "资料").glob("*.md"))
    assert len(doc) == 1 and "榴莲" in doc[0].read_text(encoding="utf-8")


def test_noise_is_repeatable_and_has_unique_keywords():
    assert NOISE.noise_facts(50, 3) == NOISE.noise_facts(50, 3) != NOISE.noise_facts(50, 4)
    every = NOISE.all_facts()
    assert len(every) >= 200 and len({f for f, _ in every}) == len(every)
    few = NOISE.noise_facts(len(NOISE.OBJECTS), 0)
    assert len({k for _, k in few}) == len(few)  # 不到一百条时, 每个关键词只用一次
    assert len(NOISE.noise_facts(200, 0)) == 200 and NOISE.noise_facts(0) == []
    assert NOISE.leaked("我新买了一个台灯", [("用户的台灯前几天坏了", "台灯")]) == ["台灯"]
    assert all(k in f for f, k in every) and all(sum(k in f for f, _ in every) == len(NOISE.TEMPLATES) for k in NOISE.OBJECTS)
    assert not [(a, b) for a in NOISE.OBJECTS for b in NOISE.OBJECTS if a != b and a in b]  # 关键词互不包含, 漏没漏数得准


def _words(xs):
    out = set()
    for x in xs:
        if not x or any(c in x for c in "\\[]*"):
            continue  # 正则 (按说法判的) 单独查
        out |= {p for p in x.split("+") if p}
    return out


def test_noise_never_contains_what_any_bench_judges():
    judged = set()
    for scripts in LM.SETS.values():
        for _, _, _, _, expect, forbid in scripts:
            judged |= _words(expect) | _words(forbid)
    recall = load("bench_recall")
    judged |= {key for pos, _ in recall.SETS.values() for _, key in pos}
    regexes = [x for scripts in LM.SETS.values() for s in scripts for x in s[4] + s[5] if x and any(c in x for c in "\\[]*")]
    role = load("bench_role")  # 角色混淆那套的期望、禁止本来就是正则 (r"." 是"说什么都行", 不算)
    regexes += [r for case in role.CASES for r in (case[3], case[4]) if r != "."]
    graph_words = set(G.CATEGORY) | set(G.BREED) | set(G.CITIES) | set(G.CAR_BRANDS) | set("红蓝绿黄白黑紫粉橙灰")
    for fact, key in NOISE.all_facts():
        assert not [w for w in judged if w in fact], (fact, [w for w in judged if w in fact])
        assert not [r for r in regexes if re.search(r, fact)], fact
        assert not [w for w in graph_words if w in fact], (fact, [w for w in graph_words if w in fact])
        assert "叫" not in fact and "喜欢" not in fact and "住" not in fact and not re.search(r"\d|月|日", fact)


def test_noise_lands_as_confirmed_summaries_before_the_script(tmp_path):
    m = Memory({"vault": str(tmp_path), "character": "小流", "graph_notes": False})
    now = {"t": LM.DAY0}
    m.clock = lambda: now["t"]
    m.init()
    facts = NOISE.noise_facts(30, 1)
    LM.add_noise(m, facts, now)
    text = (m.dir / "记忆" / "对话摘要.md").read_text(encoding="utf-8")
    assert all(f in text for f, _ in facts)
    days = set(re.findall(r"## (\d{4}-\d{2}-\d{2})", text))
    assert min(days) >= (LM.DAY0 - timedelta(days=60)).strftime("%Y-%m-%d") and max(days) < LM.DAY0.strftime("%Y-%m-%d")


def test_checkup_counts_and_never_writes(tmp_path):
    m = Memory({"vault": str(tmp_path), "character": "小流", "graph_notes": False})
    now = {"t": datetime(2026, 10, 1, 12, 0)}
    m.clock = lambda: now["t"]
    m.init()
    m.observe("我叫小明", "好的", [], None)
    m.add_summary("2026-10-01", [{"text": "用户10月3日要去医院复查", "times": ["12:00"]},
                                 {"text": "用户和室友吵架了", "times": ["12:01"]}], 0)
    m.propose("2026-10-01", [{"text": "用户下周要去北京出差", "times": ["12:02"]}], 0)
    m.add_howto("截图", "PrintScreen 没反应", ok=False, day="2026-10-01")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    result = CHECK.MM.collect({"vault": str(tmp_path), "character": "小流"})
    c = CHECK.checkup(result, date(2026, 12, 30), 30.0)
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert before == after
    assert c["types"]["fact"] == 1 and c["types"]["event"] == 2 and c["types"]["candidate"] == 1
    assert sum(c["event_freshness"].values()) == 2 and c["event_freshness"]["< 0.25 (快淡没了)"] == 2  # 三个月前的事件
    assert c["candidates_waiting_days"]["条数"] == 1 and c["candidates_waiting_days"]["最久"] == 90
    assert c["howto"] == {"成了": 0, "没成": 1, "归档": 0}
    assert all("text" not in x for x in c["nearly_faded"])  # 默认不带原文
    assert "记忆体检" in CHECK.render(c)
