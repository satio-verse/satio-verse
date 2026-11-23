"""Gate v3: E18 型片段覆盖/退步、旧记录迁移和命令行采纳边界, 不启动浏览器或训练."""
import copy
import json
import os
from types import SimpleNamespace

import pytest

from test_motion_growth import tool, growth, growth_with_avatars, _run_args
from wm import motion_growth as MG

SCENE = "pickput:ball>coffee_table"
NAMES = ["cmu69_68_pick", "cmu69_69_pick"]


def result(names=NAMES, seeds=(3, 4), excess=(10, 10), mode="normal", trajectory=None):
    rows = []
    for i, seed in enumerate(seeds):
        name = names[i % len(names)]
        identity = json.dumps(["actions", "pick", name, 0, 100], separators=(",", ":"))
        rows.append({"scenario": SCENE, "seed": seed, "ok": True, "frames": 600, "natural": 1.5, "pain": {"slide": 0.01},
                     "growth": {"excess": {"held.acc": excess[i % len(excess)]}, "faults": {}, "records": [], "coverage": {
                         "version": 1, "mode": mode, "frames": 600,
                         "trajectory": {"scheme": tool.TRAJECTORY_SCHEME, "id": trajectory or f"{i:016x}"},
                         "clips": [{"id": identity, "library": "actions", "kind": "pick", "name": name, "start": 0, "end": 100}],
                         "sequence": [identity]}}})
    return {"params": {}, "results": rows}


def compare(b, a, seeds=(3, 4)):
    return tool.coverage_compare(b, a, seeds, "held.acc", tool.GATES, expected=[SCENE])


def test_three_seeds_with_identical_traces_do_not_hide_missing_69_or_68():
    for name, missing in zip(NAMES, reversed(NAMES)):
        r = result([name], (3, 4, 5), trajectory="00000000000000ab")
        c = compare(r, r, (3, 4, 5))
        assert not c["pass"] and missing in str(c["why"])
        s = c["before"][SCENE]
        assert (s["seed_count"], s["trajectory_count"], s["clip_count"]) == (3, 1, 1)


def test_observed_wendy_pick_mapping_rejects_345_and_accepts_34511_self_comparison():
    """2026-10-28 真页面 smoke 的局部映射; 不推广到别的形象/场景, 不改默认验收种子.
    3/4/5 的最终姿态轨迹摘要可以各不相同, 但都只拿到 68; 多轨迹不等于多 pick 分支.
    """
    narrow = result([NAMES[0]], (3, 4, 5))
    c = compare(narrow, narrow, (3, 4, 5))
    assert not c["pass"] and c["before"][SCENE]["trajectory_count"] == 3
    assert c["before"][SCENE]["clip_count"] == 1
    covered = result([NAMES[0], NAMES[0], NAMES[0], NAMES[1]], (3, 4, 5, 11))
    assert compare(covered, covered, (3, 4, 5, 11))["pass"]


def test_candidate_can_have_different_trajectory_but_same_clip_pairs():
    b, a = result(), result(excess=(2, 2), trajectory="00000000000000aa")
    c = compare(b, a)
    assert c["pass"], c["why"]
    assert len(c["strata"]) == 2 and c["after"][SCENE]["trajectory_count"] == 1


def test_good_average_cannot_hide_one_clip_regression():
    b, a = result(excess=(1, 99)), result(excess=(4, 1))
    assert tool.excess_of(a, "held.acc")[0] < tool.excess_of(b, "held.acc")[0]
    c = compare(b, a)
    assert not c["pass"] and "cmu69_68_pick" in str(c["why"])


@pytest.mark.parametrize("mutation", ["missing", "diagnostic", "version", "frames", "seed", "clip", "duplicate"])
def test_missing_or_incomparable_evidence_is_rejected(mutation):
    b, a = result(), result(excess=(2, 2))
    row = a["results"][0]
    if mutation == "missing": row["growth"].pop("coverage")
    elif mutation == "diagnostic": row["growth"]["coverage"]["mode"] = "diagnostic"
    elif mutation == "version": row["growth"]["coverage"]["version"] = 0
    elif mutation == "frames": row["growth"]["coverage"]["frames"] -= 1
    elif mutation == "seed": row["seed"] = 9
    elif mutation == "clip": a = result(list(reversed(NAMES)), excess=(2, 2))
    elif mutation == "duplicate": a["results"].append(copy.deepcopy(row))
    assert not compare(b, a)["pass"]


def test_coverage_is_not_hardcoded_for_books_or_all_scenarios():
    r = result([NAMES[0]], (3,))
    r["results"][0]["scenario"] = "pickput:book>bookshelf"
    assert tool.coverage_compare(r, r, [3], "held.acc", tool.GATES)["pass"]


def test_default_pickput_requires_both_clips_and_two_incomplete_suites_do_not_pass():
    r = result([NAMES[0]], (3, 4, 5), trajectory="00000000000000ab")
    for row in r["results"]: row["scenario"] = "pickput"
    c = tool.coverage_compare(r, r, [3, 4, 5], "held.acc", tool.GATES, expected=sorted(tool.SUITE))
    assert not c["pass"] and "cmu69_69_pick" in str(c["why"])
    assert "场景覆盖不完整" in str(c["why"])


def test_validation_rejects_legacy_eval_and_preserves_complete_coverage():
    class Fake:
        def __init__(self, legacy=False): self.legacy = legacy
        def run(self, jobs):
            out = []
            for j in jobs:
                r = result(excess=(2, 2) if j["params"] else (10, 10))
                if not j.get("scenarios"):
                    r["results"] = [{**copy.deepcopy(row), "scenario": s} for s in tool.SUITE for row in r["results"]]
                if self.legacy:
                    for row in r["results"]: row["growth"].pop("coverage")
                out.append(r)
            return out
    cand = {"problem": "held.acc@carry/low", "values": {"x": 1}, "scenarios": [SCENE]}
    v = tool.validate(Fake(), cand, [3, 4], ["a.vrm"])
    assert v["pass"], v
    assert tool._brief(v)["avatars"]["a.vrm"]["coverage"]["extra"]["before"][SCENE]["samples"]
    assert not tool.validate(Fake(True), cand, [3, 4], ["a.vrm"])["pass"]


def test_old_gate_triggers_pending_and_candidate_stale_without_code_change(tmp_path):
    g = growth(tmp_path)
    old = tool.make_contract("held.acc@carry/low", [SCENE], [], [3, 4, 5], ["a.vrm"])
    old["gate_version"] = 2
    version = g.adopt({"values": {"x": 1}, "keys": ["x"], "problem": old["problem"], "family": "carry", "acceptance": old})["version"]
    assert g.pending_contract() == [version]
    assert g.needs_work().startswith("补验收清单")
    c = {"scope": {"avatars": [{"file": "a.vrm"}]}, "checks": [{"contract": old, "fingerprint": {"a.vrm": g.fingerprint("a.vrm")}}]}
    assert g.candidate_stale(c)
    upgraded = tool.effective_contract(old, old["problem"])
    assert upgraded["gate_version"] == 3 and upgraded["coverage"]["required"] == tool.COVERAGE_REQUIRED


def passed_file(g):
    contract = tool.effective_contract(tool.make_contract("held.acc@carry/low", [SCENE], [], [3, 4, 5], ["a.vrm"]), "held.acc@carry/low")
    cv = compare(result(), result(excess=(2, 2)))
    v = {"pass": True, "gate_version": 3, "val_seeds": list(tool.VALIDATION_SEEDS), "avatars": {"a.vrm": {
        "pass": True, "coverage": {"pass": True, "suite": cv, "extra": cv}, "full_after": {"x": 1}, "full_before": {},
        "fault": {"gain": .8, "excess": [10, 2], "count": [1, 1]}, "suite_cost": 1., "fails": [0, 0], "slow": {}}}}
    return {"problem": contract["problem"], "keys": ["x"], "values": {"x": 1}, "contract": contract,
            "validation": v, "confirmation": copy.deepcopy(v), "verification": {"contract": copy.deepcopy(contract),
                "values": {"x": 1}, "keys": ["x"], "baseline": g.baseline(), "fingerprint": {"a.vrm": g.fingerprint("a.vrm")}, "pass": True}}


@pytest.mark.parametrize("mutation", ["old", "fingerprint", "contract", "values", "coverage", "confirmation"])
def test_cli_adopt_cannot_rebrand_stale_or_incomplete_evidence(tmp_path, mutation):
    g = growth(tmp_path)
    c = passed_file(g)
    if mutation == "old": c.pop("verification")
    elif mutation == "fingerprint": c["verification"]["fingerprint"]["a.vrm"]["eval"] = "old"
    elif mutation == "contract": c["contract"]["gate_version"] = 2
    elif mutation == "values": c["values"]["x"] = 2
    elif mutation == "coverage": c["validation"]["avatars"]["a.vrm"].pop("coverage")
    elif mutation == "confirmation": c["confirmation"] = None
    with pytest.raises(SystemExit): tool.adopt_file(g, c)
    assert not g.versions()


def test_cli_adopt_preserves_validation_time_fingerprint(tmp_path, monkeypatch):
    g = growth(tmp_path)
    c = passed_file(g)
    monkeypatch.setattr(tool, "_problem", lambda *args: {})
    out = tool.adopt_file(g, c)
    doc = g.version(out["version"])
    assert doc["fingerprint"] == c["verification"]["fingerprint"]
    assert doc["numbers"]["validation"]["a.vrm"]["coverage"]["extra"]["strata"]


def test_evaluator_fingerprint_covers_probe_and_gate_code(tmp_path):
    assert {"web/motion-growth-coverage.js", "tools/motion_growth.py", "wm/motion_growth.py"} <= set(MG.EVAL_FILES)
    assert {"web/lmm.js", "web/avatar-vrm.js", "web/plain-state.js", "web/physics/liquid.js", "web/reach-safety.js", "web/grasp-transform.js", "web/grasp-controller.js", "desktop/web/room-grasps.js", "desktop/web/room-props.js", "desktop/web/room-life.js", "desktop/web/room-acts.js", "desktop/web/room-afford.js", "desktop/web/room-poses.js"} <= set(MG.CODE_FILES)


def test_only_changing_base_slot_invalidates_file_and_candidate_proof(tmp_path):
    g = growth(tmp_path)
    c = passed_file(g)
    original_fp = g.fingerprint("a.vrm")
    candidate = g.add_candidate({"problem": c["problem"], "values": c["values"], "keys": c["keys"],
        "acceptance": c["contract"], "scope": {"avatars": [{"file": "a.vrm", "sha": None}]}, "checks": [{
            "pass": True, "contract": c["contract"], "fingerprint": c["verification"]["fingerprint"],
            "values": c["values"], "baseline": c["verification"]["baseline"]}]})
    g.adopt({"keys": ["x"], "values": {"x": 5}, "full": {"x": 5}, "family": "carry"})
    assert g.fingerprint("a.vrm") == original_fp, "单独换槽位没有改代码/动作资产"
    assert g.candidate_stale(candidate)
    with pytest.raises(SystemExit, match="基础槽位"):
        tool.adopt_file(g, c)
    with pytest.raises(SystemExit, match="基础槽位"):
        tool.candidate_adopt(g, candidate["id"])


def test_motion_asset_contents_add_delete_and_directory_identity_change_fingerprint(tmp_path):
    g = growth(tmp_path)
    folder = g.factory.parent
    before = g.fingerprint()
    binary = folder / "actions.bin"
    binary.write_bytes(b"abcd")
    added = g.fingerprint()
    assert added != before and added["assets"]["files"] == ["actions.bin"]
    # 同 mtime、不同 size 仍失效; 不能只按 mtime 缓存.
    stat = binary.stat()
    binary.write_bytes(b"abcdef")
    os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replaced = g.fingerprint()
    assert replaced != added
    binary.write_bytes(b"ghijkl")
    os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10000000))
    assert g.fingerprint() != replaced, "同大小替换内容, mtime 变化仍要重读"
    (folder / "actions.json").write_text('{"clips": []}', encoding="utf-8")
    assert g.fingerprint() != replaced
    binary.unlink()
    assert "actions.bin" not in g.fingerprint()["assets"]["files"]
    # 不递归训练日志/权重; 另一个实际动作目录本身则是不同依赖.
    baseline = g.fingerprint()
    (folder / "training").mkdir()
    (folder / "training" / "log.json").write_text("{}")
    assert g.fingerprint() == baseline
    other = tmp_path / "other-motion"
    other.mkdir()
    (other / "actions.json").write_text('{"clips": []}', encoding="utf-8")
    cfg = {**g.cfg, "motion": {**g.cfg["motion"], "locomotion_dir": str(other)}}
    assert MG.motion_assets(cfg) != MG.motion_assets(g.cfg)


def test_asset_change_invalidates_candidate_reuse_and_adoption(tmp_path, monkeypatch):
    g = growth(tmp_path)
    c = passed_file(g)
    candidate = g.add_candidate({"problem": c["problem"], "values": c["values"], "keys": c["keys"],
        "acceptance": c["contract"], "scope": {"avatars": [{"file": "a.vrm", "sha": None}]}})
    calls = []
    def check(*args):
        calls.append(args)
        return {"pass": True, "validation": c["validation"], "confirmation": c["confirmation"]}
    monkeypatch.setattr(tool, "check_contract", check)
    tool.candidate_check(None, g, candidate["id"])
    assert tool.candidate_check(None, g, candidate["id"])["reused"] and len(calls) == 1
    (g.factory.parent / "actions.bin").write_bytes(b"new motion")
    with pytest.raises(SystemExit, match="运行栈"):
        tool.candidate_adopt(g, candidate["id"])
    tool.candidate_check(None, g, candidate["id"])
    assert len(calls) == 2


def test_digest_cache_is_scoped_by_root_and_tracks_addition_deletion(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(); two.mkdir()
    p, q = one / "x.js", two / "x.js"
    p.write_text("aa"); q.write_text("bb")
    stat = p.stat(); os.utime(q, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    a = MG.files_digest(one, ["x.js"])
    assert a != MG.files_digest(two, ["x.js"])
    assert a != MG.files_digest(one, ["x.js", "new.js"])
    p.unlink()
    assert a != MG.files_digest(one, ["x.js"])


@pytest.mark.parametrize("change", ["eval", "assets"])
def test_active_stack_change_is_scheduled_then_revalidated_once(tmp_path, monkeypatch, change):
    g = growth(tmp_path)
    # 隔离实现文件的假根, 不写真实源码.
    g.root = tmp_path / "source"
    (g.root / "web").mkdir(parents=True)
    c = passed_file(g)
    monkeypatch.setattr(tool, "_problem", lambda *args: {})
    version = tool.adopt_file(g, c)["version"]
    assert g.needs_work() is None
    if change == "eval": (g.root / "web" / "motion-growth-probe.js").write_text("changed")
    else: (g.factory.parent / "actions.bin").write_bytes(b"changed")
    assert g.needs_work().startswith("运行栈变了")
    monkeypatch.setattr(tool, "_ev", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(tool, "check_contract", lambda *args: {"pass": True, "validation": c["validation"], "confirmation": c["confirmation"]})
    run = tool.cmd_run(_run_args(), g)
    assert run["step"] == "revalidate-code" and run["passed"]
    assert g.version(version)["validated_fingerprint"] == g.version_fingerprint(g.version(version))
    assert g.needs_work() is None


def test_successful_factory_revalidation_removes_old_queue_item(tmp_path, monkeypatch):
    g = growth(tmp_path)
    c = passed_file(g)
    monkeypatch.setattr(tool, "_problem", lambda *args: {})
    version = tool.adopt_file(g, c)["version"]
    g.factory.write_text(json.dumps({"values": {"tune.lockIn": 0.5}}), encoding="utf-8")
    assert g.params_payload() is None and g._revalidate()[0]["version"] == version
    monkeypatch.setattr(tool, "_ev", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(tool, "check_contract", lambda *args: {"pass": True, "validation": c["validation"], "confirmation": c["confirmation"]})
    run = tool.cmd_run(_run_args(), g)
    assert run["step"] == "revalidate" and run["passed"] and run["adopted"] != version
    assert not g._revalidate() and g.needs_work() is None


@pytest.mark.parametrize("entry", ["candidate", "file"])
@pytest.mark.parametrize("change", ["fingerprint", "baseline"])
def test_changes_during_slow_check_do_not_save_confirmation(tmp_path, monkeypatch, entry, change):
    g = growth(tmp_path)
    checked = passed_file(g)
    cand = {"problem": checked["problem"], "keys": ["x"], "values": {"x": 1}, "scenarios": [SCENE], "guard": []}
    registered = g.add_candidate({**cand, "acceptance": checked["contract"], "scope": {"avatars": [{"file": "a.vrm", "sha": None}]}})
    fp = g.fingerprint("a.vrm")
    def check(*args):
        if change == "fingerprint":
            (g.factory.parent / "actions.bin").write_bytes(b"changed while evaluating")
        else:
            g.adopt({"keys": ["x"], "values": {"x": 7}, "full": {"x": 7}, "family": "carry"})
            assert g.fingerprint("a.vrm") == fp, "仅基准变动, 也必须被耗时检查后的复核发现"
        return {"pass": True, "validation": checked["validation"], "confirmation": checked["confirmation"]}
    monkeypatch.setattr(tool, "check_contract", check)
    with pytest.raises(SystemExit, match="过程中运行栈或基础槽位变了"):
        if entry == "candidate": tool.candidate_check(None, g, registered["id"])
        else: tool.verify_file(None, g, cand, [3, 4, 5], ["a.vrm"])
    stored = next(c for c in g.candidates() if c["id"] == registered["id"])
    assert stored["status"] == "pending" and not stored.get("checks")
    assert "verification" not in cand and "validation" not in cand


def test_baseline_remains_below_avatar_slot_and_does_not_mutate_it(tmp_path):
    g, _ = growth_with_avatars(tmp_path)
    g.adopt({"keys": ["x"], "values": {"x": 3}, "full": {"x": 3}, "family": "carry"})
    sha = g.slot_of("a.vrm")
    personal = g.adopt({"keys": ["x"], "values": {"x": 9}, "full": {"x": 9, "avatarOnly": 2}, "family": "carry",
                        "scope": {"avatars": [{"file": "a.vrm", "sha": sha}]}})
    old = tool._slot_base(g, sha, below=True)
    current = g.baseline()
    assert old["x"] == current["values"]["x"] == 3
    assert "avatarOnly" not in current["values"]
    assert g.avatar_params("a.vrm")["version"] == personal["version"]
    assert g.avatar_params("a.vrm")["values"]["x"] == 9


def test_current_seed_groups_are_fixed_disjoint_and_old_contracts_expand():
    assert tool.VALIDATION_SEEDS == [3, 4, 5, 11]
    assert tool.CONFIRM_SEEDS == [6, 7, 8, 9]
    current = tool.make_contract("held.acc@carry/low", [SCENE], [], None, ["a.vrm"], [1, 2])
    assert current["val_seeds"] == tool.VALIDATION_SEEDS and current["confirm"]["seeds"] == tool.CONFIRM_SEEDS
    old = {**current, "gate_version": 2, "val_seeds": [3, 4, 5], "confirm": {"seeds": [6, 7, 8], "variants": []}}
    expanded = tool.effective_contract(old, old["problem"])
    assert expanded["val_seeds"] == tool.VALIDATION_SEEDS
    assert expanded["confirm"]["seeds"] == tool.CONFIRM_SEEDS
    assert expanded["train_seeds"] == [1, 2]
    assert not set(expanded["val_seeds"]) & set(expanded["confirm"]["seeds"])


@pytest.mark.parametrize("validation,training", [([9], None), ([3, 4, 5], [11]), ([3, 4, 5], [6])])
def test_custom_overlapping_seeds_are_rejected_without_search(validation, training):
    with pytest.raises(SystemExit, match="种子重叠"):
        tool.make_contract("held.acc@carry/low", [SCENE], [], validation, ["a.vrm"], training)


def test_stored_seed_conflict_is_not_silently_removed_during_upgrade():
    old = tool.make_contract("held.acc@carry/low", [SCENE], [], [3, 4, 5], ["a.vrm"])
    old["confirm"]["seeds"] = [6, 7, 8, 11]
    with pytest.raises(SystemExit, match="种子重叠"):
        tool.effective_contract(old, old["problem"])
    with pytest.raises(SystemExit, match="种子重叠"):
        tool.check_contract(None, old, {})  # 在评估器启动工作之前拒绝手工冲突清单


def test_cli_requested_conflicting_seed_is_not_ignored_when_contract_already_exists(tmp_path):
    g = growth(tmp_path)
    cand = passed_file(g)
    with pytest.raises(SystemExit, match="种子重叠"):
        tool.verify_file(None, g, cand, [9], ["a.vrm"])
