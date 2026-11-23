"""新模型 / 新场景体检 (tools/motion_checkup.py) 的纯计算部分: 汇总成每秒痛觉、找参照、比出明显更差的场景、模型比例."""
import importlib.util
import sys

from wm.config import ROOT

sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("motion_checkup", ROOT / "tools" / "motion_checkup.py")
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

W = {"pushed": 30, "balance": 100, "jump": 0.2, "furniture": 100}


def _run(rows):
    """rows: [(场景, 帧数, {项: 累计}, 像真人, 失败原因)]"""
    return {"results": [{"scenario": s, "frames": f, "pain": p, "natural": n, "ok": not why, "reason": why,
                         "contactBy": {"belly:elbow": 0.1}, "balanceBy": {}} for s, f, p, n, why in rows]}


def test_summarize_rates_per_second_and_skips_jump():
    s = mc.summarize(_run([("walk", 120, {"pushed": 1.0, "jump": 50}, 1.2, None),
                           ("walk", 60, {"pushed": 1.0, "jump": 50}, 1.4, "走不到")]), W)["walk"]
    # 第一次 2 秒 30/2 = 15/秒, 第二次 1 秒 30/秒, 平均 22.5
    assert abs(s["rate"]["pushed"] - 22.5) < 1e-9
    assert "jump" not in s["rate"]
    assert s["fail"] == 1 and s["runs"] == 2 and s["reasons"] == ["走不到"]
    assert abs(s["natural"] - 1.3) < 1e-9 and abs(s["contactBy"]["belly:elbow"] - 0.1) < 1e-9


def test_reference_same_name_then_same_kind():
    ref = mc.summarize(_run([("walk:door", 60, {"balance": 0.1}, 1.3, None), ("walk:window", 60, {"balance": 0.3}, 1.5, None)]), W)
    r, how = mc.ref_for("walk:door", [ref])
    assert how == "同名场景" and abs(r["pain"] - 10) < 1e-9
    r, how = mc.ref_for("walk:bed", [ref])          # 新场景里的目标: 和参照的同类平均比
    assert "同类" in how and abs(r["pain"] - 20) < 1e-9 and abs(r["natural"] - 1.4) < 1e-9
    assert mc.ref_for("dance", [ref]) is None


def test_compare_flags_failures_pain_and_naturalness():
    ref = mc.summarize(_run([("sit", 60, {"balance": 0.1}, 1.5, None), ("walk", 60, {"balance": 0.1}, 1.3, None),
                             ("idle", 60, {"balance": 0.1}, 1.0, None)]), W)
    new = mc.summarize(_run([("sit", 60, {"balance": 0.1}, 1.5, "坐不了"),         # 失败
                             ("walk", 60, {"balance": 0.6}, 1.3, None),            # 60/秒 vs 10/秒
                             ("idle", 60, {"balance": 0.11}, 1.0, None)]), W)     # 差不多
    bad = mc.compare(new, [ref])
    assert [b["scenario"] for b in bad] == ["sit", "walk"]
    assert bad[1]["terms"][0][0] == "balance"
    nat = mc.compare(mc.summarize(_run([("idle", 60, {"balance": 0.1}, 1.3, None)]), W), [ref])
    assert [b["scenario"] for b in nat] == ["idle"]


def test_model_notes():
    refs = [{"hipHeight": 0.85, "armToHip": 0.49, "legToHip": 0.89, "shoulderToHip": 0.2, "ankle": 0.1, "hangDeg": {"left": 15}},
            {"hipHeight": 0.73, "armToHip": 0.45, "legToHip": 0.8, "shoulderToHip": 0.2, "ankle": 0.12, "hangDeg": {"left": 13}}]
    ok = {"hipHeight": 0.8, "armToHip": 0.47, "legToHip": 0.85, "shoulderToHip": 0.2, "ankle": 0.11, "hangDeg": {"left": 14},
          "missingBones": []}
    assert mc.model_notes(ok, refs) == []
    odd = {**ok, "armToHip": 0.7, "hangDeg": {"left": 30, "right": 28}, "missingBones": ["leftToes"]}
    notes = "\n".join(mc.model_notes(odd, refs))
    assert "leftToes" in notes and "手臂长" in notes and "外展" in notes
