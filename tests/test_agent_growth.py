"""Deterministic Creator regression evaluation, entirely inside temporary folders."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from wm.agent.extensions import ExtensionRegistry
from wm.agent.growth import GrowthStore, MAX_REPORT_AGE_S, _assertions


def package(value, *, delay=0, error=False):
    source = f'''import json, sys, time
from pathlib import Path
VALUE = {value!r}
for line in sys.stdin:
    q = json.loads(line)
    if 'id' not in q:
        continue
    if q['method'] == 'initialize':
        result = {{'protocolVersion': '2025-11-25', 'capabilities': {{'tools': {{}}}}}}
    elif q['method'] == 'tools/list':
        result = {{'tools': [{{'name': 'answer', 'inputSchema': {{'type': 'object'}}}}]}}
    else:
        Path('executed.txt').write_text('test wrote only to temporary copy')
        assert json.loads(Path('manifest.json').read_text(encoding='utf-8'))['id'] == 'growth-fixture'
        time.sleep({delay})
        result = {{'content': [{{'type': 'text', 'text': VALUE}}], 'isError': {error!r}}}
    print(json.dumps({{'jsonrpc': '2.0', 'id': q['id'], 'result': result}}), flush=True)
'''
    return {"id": "growth-fixture", "name": "回归用例", "files": {"server.py": source},
        "mcp": {"command": sys.executable, "args": ["server.py"]}}


def setup(tmp_path, old="wrong", new="correct"):
    r = ExtensionRegistry(tmp_path / "agent", settings_path=False, skill_roots=[])
    baseline = r.create(package(old))
    r.set_enabled("growth-fixture", True, authorized=True)
    candidate = r.create(package(new))
    g = GrowthStore(tmp_path / "agent", r)
    g.save_suite({"id": "answer-suite", "name": "答案回归", "package_id": "growth-fixture",
        "cases": [{"tool": "answer", "arguments": {}, "expect": {"equals": "correct"}}]})
    return r, g, baseline, candidate


def evaluate(g, candidate):
    return asyncio.run(g.evaluate("growth-fixture", candidate["revision"], "answer-suite", authorized=True))


def test_fixed_baseline_candidate_comparison_and_controlled_adoption(tmp_path):
    r, g, baseline, candidate = setup(tmp_path)
    report = evaluate(g, candidate)
    assert report["status"] == "passed" and report["comparison"] == "improved_on_suite"
    assert report["baseline"]["passed"] == 0 and report["candidate"]["passed"] == 1
    assert report["candidate"]["cases"][0]["assertions"][0]["kind"] == "equals"
    assert report["candidate"]["elapsed_ms"] > 0
    assert report["suite_hash"] == g.suites()[0]["hash"]
    assert r.list()["extensions"][0]["active_revision"] == baseline["revision"], "Evaluating must not activate the candidate"
    assert not list(r.root.glob("**/executed.txt")), "Version directories must not receive execution output"
    assert list((g.root / "runs").iterdir()) == [], "Owned temporary test copies are cleaned up"
    assert g.validate(report["id"])["eligible"]
    assert g.reports()[0]["id"] == report["id"]
    with pytest.raises(PermissionError):
        g.adopt(report["id"])
    adopted = g.adopt(report["id"], authorized=True)
    assert adopted["extension"]["active_revision"] == candidate["revision"]
    with pytest.raises(ValueError, match="已经改变"):
        g.adopt(report["id"], authorized=True)
    r.rollback("growth-fixture", baseline["revision"], authorized=True)
    assert r.list()["extensions"][0]["active_revision"] == baseline["revision"]


def test_equal_success_is_regression_pass_not_claimed_improvement(tmp_path):
    _, g, _, candidate = setup(tmp_path, old="correct", new="correct")
    report = evaluate(g, candidate)
    assert report["comparison"] == "regression_passed"
    assert g.validate(report["id"])["eligible"]


def test_failing_candidate_cannot_be_adopted(tmp_path):
    _, g, _, candidate = setup(tmp_path, old="correct", new="wrong")
    report = evaluate(g, candidate)
    assert report["status"] == "failed" and report["comparison"] == "regression_failed"
    assert not g.validate(report["id"])["eligible"]
    with pytest.raises(ValueError, match="不能采用"):
        g.adopt(report["id"], authorized=True)


def test_no_active_baseline_is_reported_without_comparison_claim(tmp_path):
    r, g, _, candidate = setup(tmp_path)
    r.set_enabled("growth-fixture", False, authorized=True)
    report = evaluate(g, candidate)
    assert report["baseline"] is None and report["comparison"] == "candidate_passed_no_baseline"
    assert r.list()["extensions"][0]["enabled"] is False
    assert g.validate(report["id"])["eligible"]


def test_model_cannot_create_or_rewrite_fixed_suites(tmp_path):
    _, g, _, _ = setup(tmp_path)
    before = g.suites()[0]
    spec = {"id": "answer-suite", "name": "Model edited", "package_id": "growth-fixture",
        "cases": [{"tool": "answer", "expect": {"not_contains": "impossible text"}}]}
    with pytest.raises(PermissionError):
        g.save_suite(spec, by="assistant")
    assert g.suites()[0] == before


def test_resaving_suite_does_not_reuse_tampered_history(tmp_path):
    _, g, _, _ = setup(tmp_path)
    suite = g.suites()[0]
    spec = json.loads(json.dumps({k: suite[k] for k in ("id", "name", "package_id", "cases")}))
    path = g.root / "suites" / suite["id"] / "revisions" / (suite["version"] + ".json")
    suite["cases"][0]["expect"]["equals"] = "tampered"
    path.write_text(json.dumps(suite), encoding="utf-8")
    with pytest.raises(ValueError, match="历史版本已损坏"):
        g.save_suite(spec)


def test_candidate_rejects_manifest_revision_label_tampering(tmp_path):
    r, _, _, candidate = setup(tmp_path)
    path = r.root / "growth-fixture" / "revisions" / candidate["revision"] / "manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["revision"] = "0" * 24
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="版本编号"):
        r.candidate_server("growth-fixture", candidate["revision"])


def test_user_suite_update_invalidates_report_and_preserves_old_suite(tmp_path):
    _, g, _, candidate = setup(tmp_path)
    report = evaluate(g, candidate)
    previous = g.suites()[0]
    current = g.save_suite({"id": "answer-suite", "name": "用户新验收标准", "package_id": "growth-fixture",
        "cases": [{"tool": "answer", "arguments": {}, "expect": {"equals": "new expected"}}]})
    assert current["hash"] != previous["hash"]
    assert len(list((g.root / "suites" / "answer-suite" / "revisions").glob("*.json"))) == 2
    assert not g.validate(report["id"])["eligible"]
    with pytest.raises(ValueError, match="题集已经改变"):
        g.adopt(report["id"], authorized=True)


def test_changed_current_baseline_rejects_old_comparison(tmp_path):
    r, g, _, candidate = setup(tmp_path)
    report = evaluate(g, candidate)
    changed = r.create(package("different baseline"))
    r.set_enabled("growth-fixture", True, authorized=True, revision=changed["revision"])
    assert not g.validate(report["id"])["eligible"]
    with pytest.raises(ValueError, match="启用版本已经改变"):
        g.adopt(report["id"], authorized=True)


def test_expired_report_requires_re_evaluation(tmp_path, monkeypatch):
    from wm.agent import growth
    _, g, _, candidate = setup(tmp_path)
    report = evaluate(g, candidate)
    monkeypatch.setattr(growth.time, "time", lambda: report["finished"] + MAX_REPORT_AGE_S + 1)
    assert not g.validate(report["id"])["eligible"]
    with pytest.raises(ValueError, match="过期"):
        g.adopt(report["id"], authorized=True)


def test_tampered_suite_candidate_or_report_is_rejected(tmp_path):
    r, g, _, candidate = setup(tmp_path)
    report = evaluate(g, candidate)
    candidate_path = r.root / "growth-fixture" / "revisions" / candidate["revision"] / "server.py"
    original = candidate_path.read_bytes()
    candidate_path.write_text("print('unverified')", encoding="utf-8")
    assert not g.validate(report["id"])["eligible"]
    candidate_path.write_bytes(original)
    path = g.root / "reports" / (report["id"] + ".json")
    changed = dict(report, status="failed")
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="校验"):
        g.validate(report["id"])
    suite = g.suites()[0]
    suite_path = g.root / "suites" / suite["id"] / "revisions" / (suite["version"] + ".json")
    suite["cases"][0]["expect"]["equals"] = "easy answer"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")
    with pytest.raises(ValueError, match="题集内容"):
        evaluate(g, candidate)


def test_evaluation_requires_authorization_and_matching_package(tmp_path):
    _, g, _, candidate = setup(tmp_path)
    with pytest.raises(PermissionError):
        asyncio.run(g.evaluate("growth-fixture", candidate["revision"], "answer-suite"))
    with pytest.raises(ValueError, match="不属于"):
        asyncio.run(g.evaluate("other", candidate["revision"], "answer-suite", authorized=True))
    assert not (g.root / "reports").exists()


def test_error_response_cannot_pass_text_assertion(tmp_path):
    r, g, _, _ = setup(tmp_path)
    candidate = r.create(package("correct", error=True))
    report = evaluate(g, candidate)
    case = report["candidate"]["cases"][0]
    assert case["assertions"][0]["passed"] and not case["passed"]
    assert not g.validate(report["id"])["eligible"]


def test_timeout_is_failure_not_a_passing_truncated_result(tmp_path):
    r, g, _, _ = setup(tmp_path)
    candidate = r.create(package("correct", delay=20))
    r.set_enabled("growth-fixture", False, authorized=True)
    g.save_suite({"id": "answer-suite", "name": "限时", "package_id": "growth-fixture",
        "cases": [{"tool": "answer", "arguments": {}, "expect": {"equals": "correct"}, "timeout_s": 1}]})
    report = evaluate(g, candidate)
    assert report["status"] == "failed" and report["candidate"]["cases"][0]["error"]
    assert list((g.root / "runs").iterdir()) == []


def test_cancellation_records_incomplete_report_and_cleans_processes(tmp_path):
    r, g, _, _ = setup(tmp_path)
    candidate = r.create(package("correct", delay=20))
    r.set_enabled("growth-fixture", False, authorized=True)
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(g.evaluate("growth-fixture", candidate["revision"], "answer-suite", stop, authorized=True))
        for _ in range(100):
            if list((g.root / "runs").glob("**/executed.txt")):
                break
            await asyncio.sleep(.02)
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert g.reports()[0]["status"] == "cancelled"
        assert not g.reports()[0]["adoption"]["eligible"]
        assert list((g.root / "runs").iterdir()) == []
    asyncio.run(run())


@pytest.mark.parametrize("cases", [[], [{"tool": "answer", "expect": {}}], [{"tool": "answer", "expect": {"contains": ""}}],
    [{"tool": "answer", "expect": {"execute": "assert True"}}], [{"tool": "answer", "expect": {"equals": "x"}, "timeout_s": 0}],
    [{"tool": "answer", "expect": {"equals": "x"}, "timeout_s": 61}], [{"tool": "answer", "expect": {"equals": "x"}, "timeout_s": 60}] * 3])
def test_suite_bounds_and_declarative_assertions_only(tmp_path, cases):
    _, g, _, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        g.save_suite({"id": "invalid-suite", "name": "Invalid", "package_id": "growth-fixture", "cases": cases})


def test_output_assertions_are_exact_and_refuse_oversized_results():
    assert _assertions({"content": [{"type": "text", "text": "hello world"}]}, {"contains": ["hello"], "not_contains": ["error"]})["passed"]
    assert _assertions({"structuredContent": {"n": 3}}, {"equals": {"n": 3}})["passed"]
    assert not _assertions({"structuredContent": {"n": True}}, {"equals": {"n": 1}})["passed"]
    large = _assertions({"content": [{"type": "text", "text": "x" * 70000}]}, {"contains": ["x"]})
    assert not large["passed"] and "截断" in large["error"]
