"""自动调优 (tools/tune_motion.py) 的纯计算部分: 参数空间、单位化、代价归一、CMA-ES."""
import importlib.util

import numpy as np

from wm.config import ROOT

_spec = importlib.util.spec_from_file_location("tune_motion", ROOT / "tools" / "tune_motion.py")
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)


def test_param_space_and_unit_roundtrip():
    space = tm.param_space()
    assert len(space) >= 20 and {"filter.arm.acc", "tune.lockIn", "tune.blendIdle"} <= {p["key"] for p in space}
    mid = {p["key"]: (p["lo"] * p["hi"]) ** 0.5 if p["log"] else (p["lo"] + p["hi"]) / 2 for p in space}
    x = tm.to_unit(space, mid)
    assert np.allclose(x, 0.5)
    back = tm.from_unit(space, x)
    assert all(abs(back[k] - v) / v < 1e-3 for k, v in mid.items() if v)
    lo = tm.from_unit(space, np.full(len(space), -1.0))  # 出界的夹回范围
    assert all(abs(lo[p["key"]] - p["lo"]) < 1e-9 for p in space)


def test_cost_is_one_at_baseline_and_counts_failures():
    w = tm.pain_weights()
    assert w["furniture"] > 0 and "jerk" in w
    res = {"results": [
        {"scenario": "walk", "ok": True, "natural": 1.5, "pain": {"slide": 0.02, "jump": 100, "jerk": 1.0}},
        {"scenario": "sit", "ok": True, "natural": 2.0, "pain": {"furniture": 0.05}},
    ]}
    base = tm.per_scenario(res, w)
    assert base["walk"]["pain"] == 0.02 * w["slide"] + 1.0 * w["jerk"]  # jump (随平滑层上限变) 不算
    assert "jump" not in base["walk"]["terms"]
    assert abs(tm.cost(base, base) - 1) < 1e-9
    worse = tm.per_scenario({"results": [dict(res["results"][0], ok=False), res["results"][1]]}, w)
    assert tm.cost(worse, base) > 1.5
    # 各项各自和默认比: 一项减半、另一项不变, 代价下降; 默认时就是 0 的项保持 0 不影响
    half = tm.per_scenario({"results": [dict(res["results"][0], pain={"slide": 0.01, "jerk": 1.0}), res["results"][1]]}, w)
    assert tm.cost(half, base) < 1


def test_cma_finds_minimum():
    cma = tm.CMA(np.full(5, 0.5), 0.2, seed=1)
    target = np.array([0.2, 0.8, 0.4, 0.6, 0.3])
    for _ in range(60):
        xs = cma.ask()
        assert all(((x >= 0) & (x <= 1)).all() for x in xs)
        cma.tell(xs, [float(np.sum((x - target) ** 2)) for x in xs])
    assert np.abs(cma.mean - target).max() < 0.03
