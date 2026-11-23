"""记忆「取代」的测试 (2026-11-17 S2).

这把尺子和别的测试有个不一样的地方: **它的错判比漏判危险**.
判错 = 把一条有效的记忆藏起来, 而藏起来的东西不会报错、只会让她"忽然忘了你的事".
所以下面有一半的用例是在测"**不该取代的时候没有取代**".

对照组直接取 `tools/bench_longmem.py` BRAIN 留出组里那两道现在 0/3 的题
(改期 / 改口), 免得我自己编一套顺手的题自己考自己.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm import memory_supersede as S  # noqa: E402

# 和 BRAIN 里一致的两道题
OLD_DATE = "- 用户10月22日要去体检 #事实"
NEW_DATE = "- 用户的体检改到10月24日了 #事实"
OLD_COLOR = "- 我最喜欢的颜色是蓝色 #事实"
# 真实的改口就是这么说的: "改成绿色了" / "不是蓝色了". 注意「我现在最喜欢」里**没有**「现在是」那种连写,
# 所以别拿它当换说法去测 —— 换说法是逐字匹配的, 这是设计上故意的 (太宽松会把"我现在的打算"也判成改口).
NEW_COLOR = "- 我最喜欢的颜色改成绿色了 #事实"


# ---------------------------------------------------------------- 解析

def test_parse_line_keeps_body_date_tags():
    got = S.parse_line("- 用户住在苏州 (2026-10-27) #事实 #auto-memory")
    assert got["body"] == "用户住在苏州"
    assert got["date"] == "2026-10-27"
    assert got["tags"] == ["#事实", "#auto-memory"]


def test_parse_line_does_not_blow_up_on_plain_text():
    got = S.parse_line("不是列表项的一行")
    assert got["body"] == "不是列表项的一行" and got["date"] == ""


def test_key_of_is_stable_and_ignores_list_marker():
    assert S.key_of("- 你好") == S.key_of("* 你好") == S.key_of("你好")
    assert S.key_of("你好") != S.key_of("你好呀")


# ---------------------------------------------------------------- 单个判定零件

def test_shared_subject_finds_the_event_word():
    shared = S.shared_subject(S.parse_line(NEW_DATE)["body"], S.parse_line(OLD_DATE)["body"])
    assert "体检" in shared


def test_shared_subject_empty_for_unrelated():
    assert S.shared_subject("今天天气不错", "这部分代码要重写") == []


def test_replaced_marker_reads_the_verb():
    assert S.replaced_marker("体检改到10月24日") == "改到"
    assert S.replaced_marker("最喜欢的是绿色了") == ""


def test_not_invalidation_blocks_plans():
    """「打算改天去体检」不是失效 —— 这是"计划没做到"那一类。"""
    assert S.not_invalidation("我打算改天再去体检") == "计划没做到"
    assert S.not_invalidation("提到他说的改期") == "只是提到"
    assert S.not_invalidation("体检改到10月24日") == ""


def test_mentioned_date_normalises():
    assert S.mentioned_date("10月24日去") == "10-24"
    assert S.mentioned_date("10月2号") == "10-2"
    assert S.mentioned_date("没有日期") == ""


# ---------------------------------------------------------------- 该取代的时候取代 (BRAIN 那两道)

def test_brain_reschedule_supersedes():
    """BRAIN「体检改期」：现在 0/3 的那类题。"""
    d = S.judge(NEW_DATE, OLD_DATE)
    assert d.supersedes and d.marker == "改到"


def test_brain_change_of_mind_supersedes():
    """BRAIN「最喜欢的颜色」：改口。"""
    d = S.judge(NEW_COLOR, OLD_COLOR)
    assert d.supersedes and d.marker == "改成"


def test_phrasing_buzaiye_supersedes():
    """「不再是」这种换说法也要认。"""
    assert S.judge("- 我最喜欢的颜色不再是蓝色了 #事实", OLD_COLOR).supersedes


# ---------------------------------------------------------------- 不该取代的时候一条都不许动

def test_no_marker_means_no_supersede():
    assert not S.judge("- 用户今天很开心", OLD_DATE)


def test_unrelated_event_means_no_supersede():
    assert not S.judge("- 用户的房租改成每月3000了", OLD_DATE)


def test_same_date_is_repeat_not_supersede():
    """同样写 10-22 只是又说了一遍，不是改期。"""
    assert not S.judge("- 用户的体检改到10月22日了", OLD_DATE)


def test_plan_is_not_invalidation_even_with_marker():
    """这条最关键：有"改到"这种词、但那是计划，旧记忆必须留着。"""
    d = S.judge("- 我打算把体检改到10月24日", OLD_DATE)
    assert not d.supersedes and d.blocked_by == "计划没做到"


def test_quote_check_rejects_line_not_in_turn():
    """第 4 道校验：模型说改了，但这一轮原文里没这句，不认。"""
    d = S.judge(NEW_DATE, OLD_DATE, turn_text="今天天气不错，我去买了点菜")
    assert not d.supersedes and "这一轮" in d.reason


def test_quote_check_accepts_when_turn_has_it():
    d = S.judge(NEW_DATE, OLD_DATE, turn_text="对了，我的体检改到10月24日了")
    assert d.supersedes


def test_quote_of_ignores_whitespace():
    assert S._quote_of("体检改到10月24日了", "我的 体检 改到 10月24日 了") is not None


# ---------------------------------------------------------------- 旁路 sidecar

@pytest.fixture
def store(tmp_path):
    return S.SupersedeStore(tmp_path / "supersede.json")


def test_end_then_hidden_and_reopen_brings_it_back(store):
    assert store.end(OLD_DATE, by="abc", why="改期")
    assert store.is_ended(OLD_DATE)
    assert store.filter([OLD_DATE, OLD_COLOR]) == [OLD_COLOR]      # 被取代的不再被想起
    assert store.reopen(OLD_DATE)
    assert store.filter([OLD_DATE, OLD_COLOR]) == [OLD_DATE, OLD_COLOR]   # 删掉新的，旧的回来


def test_filter_keeps_order(store):
    store.end(OLD_COLOR)
    assert store.filter([OLD_DATE, OLD_COLOR, "别的"]) == [OLD_DATE, "别的"]


def test_record_is_readable_for_the_memory_page(store):
    store.end(OLD_DATE, by="newkey", why="改期到 24 号", quote="体检改到10月24日了")
    extra = store.overview_extra(OLD_DATE)
    assert extra["ended"] and extra["superseded_by"] == "newkey"
    assert "24" in extra["why"] and extra["manual"] is False
    assert store.overview_extra(OLD_COLOR) == {}


def test_broken_file_means_nothing_hidden(tmp_path):
    """sidecar 读坏了当空的 —— 宁可不取代，也不能拿半个文件藏记忆。"""
    p = tmp_path / "supersede.json"
    p.write_text("{这不是 json", encoding="utf-8")
    st = S.SupersedeStore(p)
    assert st.count() == 0 and not st.is_ended(OLD_DATE)


def test_store_survives_reload(tmp_path):
    p = tmp_path / "supersede.json"
    S.SupersedeStore(p).end(OLD_DATE, by="k", why="w")
    assert S.SupersedeStore(p).is_ended(OLD_DATE)
    assert json.loads(p.read_text("utf-8"))["items"]


# ---------------------------------------------------------------- 一遍过

def test_apply_new_ends_only_the_closest_one(store):
    olds = [OLD_DATE, "- 用户的生日是5月3日 #事实"]
    got = S.apply_new(store, NEW_DATE, olds, turn_text="我的体检改到10月24日了")
    assert got.ended == [OLD_DATE]
    assert not store.is_ended(olds[1]), "一条新的只该关一条最像的，找到就该停"


def test_apply_new_does_nothing_when_unsure(store):
    got = S.apply_new(store, "- 用户今天很开心", [OLD_DATE])
    assert not got.ended and store.count() == 0


def test_close_without_new_works_when_turn_mentions_it(store):
    """独立关闭通道：「我感冒好了」不产生新记忆，也要能关掉旧的。"""
    old = "- 用户感冒了 #状态"
    assert S.close_without_new(store, old, "我感冒好了，不用担心")
    assert store.is_ended(old)
    assert store.ended_record(old)["manual"] is True


def test_close_without_new_refuses_when_turn_is_unrelated(store):
    old = "- 用户感冒了 #状态"
    assert not S.close_without_new(store, old, "今天做了个新菜")
    assert not store.is_ended(old)


def test_store_remembers_body_so_recall_can_match(store):
    """检索回来的一条只有正文 (没有日期、没有标签), 读路径必须也认得出它.

    这是接线时真踩到的坑: key_of 故意不归一化, 整行的 key 和正文的 key 永远不等,
    于是 sidecar 里写着"已取代"、读回来却照旧被想起来 —— 写侧静默失效, 最难查的那种.
    """
    old = "- 用户明天要去体检 (2026-11-16) #事实 #auto-memory"
    assert store.end(old, why="改到后天")
    assert store.is_ended(old)                       # 整行照旧认
    assert store.is_ended_body("用户明天要去体检")      # 正文也认
    assert not store.is_ended_body("用户养了一只猫")    # 无关的不误伤


def test_old_sidecar_without_body_never_hides(tmp_path):
    """老格式的 sidecar (没写 body) 认不出正文时, 一律判"没关闭" —— 宁可漏判."""
    p = tmp_path / "supersede.json"
    p.write_text(json.dumps({"v": 1, "items": {"deadbeefcafe": {"ended_at": "2026-11-17T00:00:00"}}}),
                 encoding="utf-8")
    st = S.SupersedeStore(p)
    assert st.count() == 1
    assert not st.is_ended_body("用户明天要去体检")


def test_is_ended_body_survives_garbage_input(store):
    """正文是空/None 的时候不能抛 —— 它在每一轮对话的读路径上."""
    store.end("- 用户感冒了 (2026-11-17) #状态", why="好了")
    assert not store.is_ended_body("")
    assert not store.is_ended_body(None)
    assert not store.is_ended_body("   ")

# ---------------------------------------------------------------- 两题真基线 (2026-11-17)
# 接线量基线时量到的真缺口: 提炼会吃掉"改到""现在是"这类词, 也会把原话改写掉.
# 所以判据不能只看提炼后的那一行 —— 下面两条就是照 BRAIN 留出组的实际数据写的.

def test_brain_reschedule_even_after_narrowing_ate_the_marker():
    """BRAIN 改期那一题: "用户今天要去体检" -> "用户的体检改到后天了".

    提炼把"我体检改到后天了"的换说法词吃掉一半, 靠原文里还有"改到"才认下来.
    """
    old = "- 用户今天要去体检 (2026-10-22) #事实 #auto-memory"
    new = "- 用户的体检改到后天了 (2026-10-24) #事实 #auto-memory"
    d = S.judge(new, old, turn_text="体检改到后天了")
    assert d.supersedes and "改到" in d.reason


def test_brain_reschedule_recognised_by_two_dates_alone():
    """连"改到"都没有时, 同一件事两个写明的日期就已经是改期 (原文提到旧主体才认)."""
    old = "- 用户10月22日要去体检 (2026-10-22) #事实 #auto-memory"
    new = "- 用户10月24日要去体检 (2026-10-24) #事实 #auto-memory"
    d = S.judge(new, old, turn_text="我10月24日去体检")
    assert d.supersedes and "日期" in d.reason


def test_brain_change_of_taste_without_any_marker_word():
    """BRAIN 改口那一题: 一句里根本没有"改成/现在是", 只有末尾的值换了.

    "用户喜欢的颜色是蓝色" -> "用户喜欢的颜色是绿色了"
    """
    old = "- 用户喜欢的颜色是蓝色 (2026-10-01) #事实 #auto-memory"
    new = "- 用户喜欢的颜色是绿色了 (2026-10-20) #事实 #auto-memory"
    d = S.judge(new, old, turn_text="我现在最喜欢的颜色是绿色了")
    # 认下来的路: 有换说法词(现在) 或 同一处换了值, 两条都算
    assert d.supersedes and ("换了值" in d.reason or "现在" in d.reason)
    assert S.slot_conflict("用户喜欢的颜色是绿色了", "用户喜欢的颜色是蓝色") == ("蓝色", "绿色")


def test_slot_conflict_refuses_when_prefix_short_or_values_overlap():
    """同一处换值这把尺子的边界: 前缀太短不算同一个槽, 一边包含另一边是补充不是取代."""
    assert S.slot_conflict("用户喜欢猫", "用户喜欢狗") is None        # 前缀只有 4 字但值 1 字
    assert S.slot_conflict("用户住在苏州", "用户住在苏州市") is None      # 一边包含另一边
    assert S.slot_conflict("完全不同的一句话", "另一句完全不同") is None


def test_turn_must_mention_the_old_memory_even_with_marker():
    """有换说法词也不算数, 除非这一轮真的提到了旧的这条 —— 这是防"顺手关掉别的事"的最后一道."""
    old = "- 用户喜欢的颜色是蓝色 (2026-10-01) #事实 #auto-memory"
    new = "- 用户喜欢的颜色是绿色了 (2026-10-20) #事实 #auto-memory"
    assert not S.judge(new, old, turn_text="今天天气不错").supersedes

def test_is_ended_body_ignores_trailing_source_note(store):
    """摘要行的正文带 "(来源: [[...]])", 检索回来的不带 —— 归一化必须把括注去掉.

    不修这个: 写侧判对了、sidecar 也写了, 读侧却因为一个括号永远比不中
    (BRAIN 改期/改口一直是 0/3 的最后一层).
    """
    line = "- 用户10月22日要去体检 (来源: [[2026-10-20]] 20:00) #auto-summary"
    assert store.end(line, why="改到10月24日")
    assert store.is_ended_body("用户10月22日要去体检")
    assert store.bodies() == {"用户10月22日要去体检"}

def test_reopen_by_body_brings_the_old_one_back(tmp_path):
    """删掉"新的那条", 被它取代的旧的要能回来 (S2 验收里那条: 记忆页删一条, 旧的就回来)."""
    st = S.SupersedeStore(tmp_path / "supersede.json")
    old = "- 用户明天要去体检 (2026-11-16) #事实 #auto-memory"
    new = "- 用户的体检改到后天了 (2026-11-17) #事实 #auto-memory"
    S.apply_new(st, new, [old], turn_text="体检改到后天了")
    assert st.is_ended_body("用户明天要去体检")
    assert st.reopen_by_body("用户的体检改到后天了") == 1
    assert not st.is_ended_body("用户明天要去体检")
    assert st.count() == 0


def test_memory_page_marks_superseded_and_keeps_the_chain(tmp_path):
    """记忆页要看见「已被取代 → 新的那条」."""
    from wm.memory import Memory

    root = tmp_path
    m = Memory({"vault": str(root), "character": "小流", "graph_notes": False})
    mem = root / "小流" / "记忆"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "关于用户.md").write_text(
        "- 用户明天要去体检 (2026-11-16) #事实 #auto-memory\n", encoding="utf-8")
    (root / "小流" / "关系.md").write_text("# 关系\n\n- 认识天数: 0\n", encoding="utf-8")
    m.add_summary("2026-11-17", [{"text": "用户的体检改到后天了", "times": ["10:00"]}], 0)
    facts = {f["text"]: f for f in m.overview()["facts"]}
    old = facts.get("用户明天要去体检") or {}
    assert old.get("status") == "superseded", facts.keys()
    assert old.get("superseded_by"), "没写是被哪条取代的"