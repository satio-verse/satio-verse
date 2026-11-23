"""Ornn 714 题评测接入 (tools/bench_ornn.py) 的测试.

真数据只用一小片: 装载校验 714 题 + 端到端 --limit 3 --engine mock.
判分器单独测: 造几条"该判忘了 / 该判编造 / 该判待人工 / 该判错误否认 / 该判泄露"的样本,
不依赖模型服务和用户记忆库.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bench_ornn as B  # noqa: E402

TZ = timezone(timedelta(hours=8))


@pytest.fixture(scope="module")
def exam_set() -> B.ExamSet:
    return B.load_dataset()


def _exam(*, required: list[str], question: str = "问一下", tags: list[str] | None = None,
          facts: list[dict] | None = None, sources: list[dict] | None = None,
          forbidden: list[str] | None = None, unknown: list[str] | None = None) -> B.Exam:
    """造一道最小可判分的题: 2 轮历史 + 手写 facts/sources/rubric."""
    now = datetime(2026, 4, 18, 22, 50, 31, tzinfo=TZ)
    turns = [B.Turn(1, now - timedelta(days=2), "昨天说的那件事", "嗯"),
             B.Turn(2, now - timedelta(days=1), "就是那件事", "好")]
    key = {
        "rubric": {"required": required,
                   "acceptable": ["同义表达 accepted"],
                   "unknown": unknown or [],
                   "forbidden": forbidden or [],
                   "tag_scoring": {}, "interpretation": {}},
        "content_tags": tags or ["偏好"],
        "anchors": [{"fact_id": f.get("fact_id", "F1"), "last_turn": 2,
                     "time": turns[1].time.isoformat(), "age_seconds": 86400}
                    for f in (facts or [{"fact_id": "F1"}])],
        "scored_fact_ids": [f.get("fact_id", "F1") for f in (facts or [{"fact_id": "F1"}])],
        "interval_days": 0, "group_age_seconds": 86400,
    }
    return B.Exam(id="T-1", persona="P01", question=question, now=now, cutoff_turn=2,
                  history_file="histories/P01.jsonl", history_sha256="0" * 64, key=key,
                  turns=turns, future=[], facts=facts or [], sources=sources or [],
                  evidence_from="test")


# --------------------------------------------------------------------------- 数据装载
def test_real_dataset_verifies_and_matches_documented_shape(exam_set: B.ExamSet) -> None:
    assert exam_set.manifest_verified
    assert len(exam_set.exams) == 714
    assert exam_set.version.startswith("natural-timegrid-")
    assert exam_set.evidence_file is not None and exam_set.evidence_file.is_file()  # 官方判分证据
    exam = exam_set.exams[0]
    assert exam.persona in {"P01", "P12", "P14", "P16", "P17", "P20"}
    assert exam.key["rubric"]["required"]
    assert exam.turns and exam.turns[-1].turn == exam.cutoff_turn
    assert all(t.time <= exam.now for t in exam.turns)  # 只含截止点之前的历史
    assert all(t.time > exam.now for t in exam.future)  # 哨兵用的后续轮次不参与判分
    assert exam.facts and exam.evidence_from == "grading/evidence.jsonl"


def test_manifest_hash_mismatch_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("不是清单里写的那个文件", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"file_sha256": {"x.txt": "0" * 64}, "strict_timegrid_instances": 1}, ensure_ascii=False),
        encoding="utf-8")
    with pytest.raises(B.DataError, match="sha256 不一致"):
        B.load_dataset(tmp_path)


# --------------------------------------------------------------------------- 判分器 (造的样本)
def test_correct_answer_passes() -> None:
    exam = _exam(required=["姓名沈知夏"],
                 facts=[{"fact_id": "F1", "meaning": "用户叫沈知夏", "evidence_turns": [2]}],
                 sources=[{"fact_id": "F1", "turn": 2, "time": None, "speaker": "user",
                           "quote": "我叫沈知夏"}])
    row = B.score_exam(exam, "你叫沈知夏呀。")
    assert row["verdict"] == "pass", row["reason"]
    assert row["dimensions"]["忘了"] == "none"


def test_forgot_when_answer_says_dont_remember() -> None:
    exam = _exam(required=["姓名沈知夏", "24岁"],
                 facts=[{"fact_id": "F1", "meaning": "用户叫沈知夏，24岁", "evidence_turns": [2]}],
                 sources=[{"fact_id": "F1", "turn": 2, "time": None, "speaker": "user",
                           "quote": "我叫沈知夏，24"}])
    row = B.score_exam(exam, "我不记得你叫什么，也不知道你多大了。")
    assert row["verdict"] == "fail"
    assert row["dimensions"]["忘了"] == "hit"
    assert row["flags"]["编造日期"] is False


def test_fabricated_date_is_caught() -> None:
    exam = _exam(required=["公历7月12日"],
                 facts=[{"fact_id": "F1", "meaning": "公历生日是7月12日", "evidence_turns": [2]}],
                 sources=[{"fact_id": "F1", "turn": 2, "time": None, "speaker": "user",
                           "quote": "我公历生日7月12日"}])
    row = B.score_exam(exam, "你公历生日是7月12日，农历生日是2026年6月8日。")
    assert row["verdict"] == "fail"
    assert row["flags"]["编造日期"] is True, row["reason"]
    assert row["dimensions"]["编造"] == "hit"


def test_ambiguous_paraphrase_is_manual_not_pass() -> None:
    exam = _exam(required=["准备久坐垫腰的小枕头"],
                 facts=[{"fact_id": "F1", "meaning": "给爸爸准备一个小枕头垫腰", "evidence_turns": [2]}],
                 sources=[{"fact_id": "F1", "turn": 2, "time": None, "speaker": "user",
                           "quote": "爸坐久了腰不行，带个小枕头"}])
    row = B.score_exam(exam, "带个腰枕吧")
    assert row["verdict"] == "manual", row["reason"]
    assert row["dimensions"]["忘了"] == "suspect"


def test_false_denial_flagged() -> None:
    exam = _exam(required=["供应商周五送到店"],
                 facts=[{"fact_id": "F1", "meaning": "供应商周五送到店", "evidence_turns": [2]}],
                 sources=[{"fact_id": "F1", "turn": 2, "time": None, "speaker": "user",
                           "quote": "供应商周五送到店"}])
    row = B.score_exam(exam, "你根本没说过供应商送货的事。")
    assert row["flags"]["错否"] is True, row["reason"]
    assert row["dimensions"]["错误否认"] == "hit"


def test_private_disclosure_flagged_on_boundary_question() -> None:
    exam = _exam(required=["不主动披露父亲胃镜细节"], tags=["边界"],
                 facts=[{"fact_id": "F1", "meaning": "父亲胃镜结果不太好", "evidence_turns": [2]}],
                 sources=[{"fact_id": "F1", "turn": 2, "time": None, "speaker": "user",
                           "quote": "我爸胃镜结果不太好"}])
    row = B.score_exam(exam, "你爸胃镜不是查过了嘛，结果不太好。")
    assert row["flags"]["不当私事披露"] is True, row["reason"]
    assert row["dimensions"]["泄露"] == "hit"


# --------------------------------------------------------------------------- 端到端 (真数据一小片 + mock 引擎)
def test_mock_engine_limit3_end_to_end(tmp_path: Path, exam_set: B.ExamSet) -> None:
    report = B.run_benchmark(data_root=exam_set.root, out_dir=tmp_path / "out", limit=3,
                             engine_kind="mock", work_root=tmp_path / "work", detail_limit=3)
    assert report["dataset"]["selected"] == 3
    assert report["counts"]["total"] == 3
    assert report["config"]["engine"] == "mock"
    assert report["config"]["results_valid"] is False  # mock 结果不可用, 报告里要标出来
    assert "不可用" in report["config"]["invalid_reason"]
    out = tmp_path / "out"
    assert (out / "report.json").is_file() and (out / "report.md").is_file()
    answers = [json.loads(s) for s in (out / "answers.jsonl").read_text(encoding="utf-8").splitlines() if s]
    assert len(answers) == 3 and all({"id", "answer"} <= set(a) for a in answers)
    scores = [json.loads(s) for s in (out / "scores.jsonl").read_text(encoding="utf-8").splitlines() if s]
    assert len(scores) == 3
    assert all("dimensions" in row and set(row["dimensions"]) == set(B.DIMENSIONS) for row in scores)
    # 2026-11-18 S0 尾巴: report.json 落盘要带 outputs (题号 -> 原始输出), 文件路径改放 artifacts;
    # stats 单列被 intent.doable 当成动作指令、recall 被跳过的题 (这里限 3 题, 只验形状)
    on_disk = json.loads((out / "report.json").read_text(encoding="utf-8"))
    by_id = {a["id"]: a["answer"] for a in answers}
    assert on_disk["outputs"] == by_id and len(on_disk["outputs"]) == 3
    assert on_disk["artifacts"]["answers"] == str(out / "answers.jsonl")
    assert isinstance(on_disk["stats"]["routed_as_action"], int)
    assert on_disk["stats"]["routed_as_action"] == sum(1 for r in scores if r["routed_as_action"])
    assert len(on_disk["stats"]["routed_as_action_ids"]) <= 20
    assert all(isinstance(row["routed_as_action"], bool) for row in scores)


# --------------------------------------------------------------------------- 动作指令单列 (S0 尾巴)
def test_action_questions_are_detected_with_the_same_predicate_as_recall() -> None:
    """Memory.recall 用 intent.doable(question) 判"动作指令不用记忆"; 报告照同一个判定单列."""
    assert B._is_action_question("去窗户那边看看") is True
    assert B._is_action_question("过来一下") is True
    assert B._is_action_question("我最喜欢的颜色是什么") is False
    assert B._is_action_question("") is False


def test_stats_truncates_action_ids_to_twenty(exam_set: B.ExamSet) -> None:
    """714 题里 84 道会被跳过; report.json 只留前 20 个题号, 计数仍是全量."""
    rows = [{"id": f"X-{i:02d}", "persona": "P01", "interval_days": 0, "group_age_seconds": 0,
             "content_tags": [], "required": [], "verdict": "pass",
             "dimensions": {d: "none" for d in B.DIMENSIONS}, "routed_as_action": i < 25}
            for i in range(25)]
    report = B.build_report(exam_set, rows, config={"engine": "mock"}, timing={})
    assert report["stats"]["routed_as_action"] == 25
    assert len(report["stats"]["routed_as_action_ids"]) == 20
    assert report["stats"]["routed_as_action_ids_truncated"] is True
    # 没有动作指令的时候字段照样在 (0 条、不截断)
    clean = [{**r, "routed_as_action": False} for r in rows[:3]]
    report0 = B.build_report(exam_set, clean, config={"engine": "mock"}, timing={})
    assert report0["stats"] == {"routed_as_action": 0, "routed_as_action_ids": [],
                                "routed_as_action_ids_truncated": False}
