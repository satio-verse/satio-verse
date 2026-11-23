"""身份一致的判据 (Hemifuture D6, tools/cross_body_check.py): 题目固定、答案归一化、阈值 0.8.

只测判据本身 (纯函数 + 报告字段), 不连 8790: 跑一次真链路太慢, 而且会往她的日志里写测试对话.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for extra in (str(ROOT), str(TOOLS)):
    if extra not in sys.path:
        sys.path.insert(0, extra)


def _load_tool():
    """按路径加载 tools/cross_body_check.py (tools 不是包, 也没有 __init__.py)."""
    spec = importlib.util.spec_from_file_location("cross_body_check_tool", TOOLS / "cross_body_check.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


CB = _load_tool()


# -- 题池 ---------------------------------------------------------------------------
def test_question_pool_is_fixed_six():
    """六道题固定成题池: 历次 JSON 要能横向比, 加减题就不可比了."""
    assert len(CB.IDENTITY_QUESTIONS) == 6
    assert all(q.strip() for q in CB.IDENTITY_QUESTIONS)
    assert len(set(CB.IDENTITY_QUESTIONS)) == 6


# -- 归一化 -------------------------------------------------------------------------
def test_normalize_drops_punctuation_space_and_case():
    a = CB.normalize("我 喜欢 蓝色。")
    b = CB.normalize("我喜欢蓝色")
    assert a == b


def test_similarity_is_one_for_punctuation_only_difference():
    assert CB.similarity("我 喜欢 蓝色。", "我喜欢蓝色") == 1.0


def test_similarity_is_zero_when_one_side_empty():
    assert CB.similarity("", "我喜欢蓝色") == 0.0
    assert CB.similarity("。。。", "我喜欢蓝色") == 0.0


# -- 阈值 ---------------------------------------------------------------------------
def test_threshold_is_tightened_to_0_8():
    assert CB.SIM_THRESHOLD == 0.8


def test_same_answer_uses_default_threshold():
    """同一句换个标点算一致; 只沾几个字的不算."""
    assert CB.same_answer("我觉得蓝色最舒服，看着能安静下来。", "我觉得蓝色最舒服 看着能安静下来") is True
    assert CB.same_answer("我觉得蓝色最舒服，看着能安静下来。", "我今天挺开心的，吃了面。") is False


def test_same_answer_needs_self_claim_on_both_sides():
    """两句话一模一样, 但都只是在反问/没说自己, 不算同一个自我 (真链路里先 template_strip 再判)."""
    a = CB.template_strip("你好像还没跟我说过这个话题呢")
    assert a == ""
    assert CB.is_self_claim(a) is False
    assert CB.same_answer(a, a) is False


def test_low_threshold_would_pass_middle_pair():
    """收紧前后的差别: 一对中等相似的回答, 0.6 过、默认 0.8 不过 (这就是 D6 想要 stricter 的地方)."""
    a = "我今天挺开心的，吃了面。"
    b = "我今天很开心，吃了饭。"
    sim = CB.similarity(a, b)
    assert 0.6 <= sim < CB.SIM_THRESHOLD
    assert CB.same_answer(a, b, threshold=0.6) is True
    assert CB.same_answer(a, b) is False


# -- 报告字段 -----------------------------------------------------------------------
def test_report_records_actual_threshold():
    """报告里的 threshold 记的是这次实际用的值, 别拿旧 JSON 的数字直接比."""
    ch = CB.Checker("ws://127.0.0.1:1/ws", rounds=0, fell_round=None, turn_timeout=5.0,
                    contradict=False, do_identity=True, sim_threshold=0.8)
    card = ch.summary()["identity"]
    assert card["threshold"] == 0.8
    assert "0.8" in card["gate"]["rule"]


def test_threshold_can_be_overridden_for_comparison():
    ch = CB.Checker("ws://127.0.0.1:1/ws", rounds=0, fell_round=None, turn_timeout=5.0,
                    contradict=False, do_identity=True, sim_threshold=0.6)
    assert ch.summary()["identity"]["threshold"] == 0.6
