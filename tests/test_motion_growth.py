"""动作成长 (wm/motion_growth.py、tools/motion_growth.py): 数据在哪、收失败片段、换上与退回、出厂参数变了先停用、
日常失败率变差自动退回、什么时候练、接口的来源检查; 问题聚类、场景对照、验证的门槛."""
import asyncio
import importlib.util
import json
import re
from datetime import datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm import motion_growth as MG
from wm.app import create_app
from wm.config import ROOT, load_config

_spec = importlib.util.spec_from_file_location("motion_growth_tool", ROOT / "tools" / "motion_growth.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


def growth(tmp_path, factory=None):
    fac = tmp_path / "motion"
    fac.mkdir(exist_ok=True)
    (fac / "params.json").write_text(json.dumps(factory or {"values": {"tune.lockIn": 0.13, "filter.arm.acc": 145}, "source": "x"}),
                                     encoding="utf-8")
    cfg = load_config(overrides=[f"motion.growth.data_dir={json.dumps(str(tmp_path / 'g'))}",
                                 f"motion.locomotion_dir={json.dumps(str(fac))}"])
    return MG.Growth(cfg)


def rec(kind="put.slam", cls="motion", family="carry", **kw):
    return {"kind": kind, "cls": cls, "family": family, "sev": 1.5, "peak": 1.8, "ctx": {"action": {"kind": "put", "y": 0.5}, "held": []},
            "ring": [{"t": 0}] * 60, **kw}


def test_data_dir_rules(tmp_path):
    assert MG.data_dir(load_config(overrides=[f"motion.growth.data_dir={json.dumps(str(tmp_path / 'x'))}"])) == tmp_path / "x"
    assert MG.data_dir(make_cfg(tmp_path)) == tmp_path / "motion-growth"  # 测试改了 log_dir: 跟着它, 不写用户的
    assert MG.data_dir(load_config(overrides=[])).as_posix().endswith(".worldmodel/motion-growth")


def test_ingest_validates_and_caps(tmp_path):
    g = growth(tmp_path)
    out = g.ingest({"params": "factory", "session": "s1", "exposure": {"carry": 12, "bogus": 3, "walk": -1},
                    "records": [rec(), rec(kind="BAD KIND"), rec(cls="weird"), "x"]})
    assert out == {"stored": 1, "dropped": 3, "gen": 0}
    f = g.faults()
    assert len(f) == 1 and f[0]["code"] == MG.code_version() and len(f[0]["ring"]) == 45 and f[0]["params"] == "factory"
    assert g.rates()["factory"]["carry"] == {"exposure": 12, "motion": 1, "env": 0}
    try:
        g.ingest({"records": [rec()] * (MG.MAX_RECORDS + 1)})
        raise AssertionError("超过条数要拒")
    except ValueError:
        pass


def test_adopt_overlay_rollback_and_line(tmp_path):
    g = growth(tmp_path)
    assert g.params_payload() is None and g.active_version() == "factory"
    v = g.adopt({"keys": ["tune.lockIn"], "values": {"tune.lockIn": 0.05}, "full": {"tune.lockIn": 0.05, "filter.arm.acc": 145, "tune.riseTau": 0.3},
                 "before": {"tune.lockIn": 0.13, "filter.arm.acc": 145, "tune.riseTau": 0.25}, "family": "carry", "problem": "put.slam@carry/phys",
                 "why": "测试", "line": MG.line_for("carry")})["version"]
    assert v == "v0001" and g.active_version() == "v0001"
    p = g.params_payload()
    assert p["values"]["tune.lockIn"] == 0.05 and p["values"]["filter.arm.acc"] == 145 and p["growth"]["version"] == "v0001" and p["source"] == "x"
    a = g.announce()
    assert a["line"] == "我最近把拿杯子练稳了"
    g.ack(a["id"])
    assert g.announce() is None
    # 退回出厂: 页面不重新载入时用的完整值 = 出厂值 + 代码默认值 (第一版的 before)
    g.rollback("factory")
    assert g.active_version() == "factory" and g.params_payload() is None
    assert g.full_values("factory") == {"tune.lockIn": 0.13, "filter.arm.acc": 145, "tune.riseTau": 0.25}
    assert [c["action"] for c in g.changes()] == ["adopt", "rollback"]
    assert g.versions() == ["v0001"], "旧版本留着"
    g.rollback("v0001")
    assert g.active_version() == "v0001"


def test_factory_change_suspends(tmp_path):
    g = growth(tmp_path)
    g.adopt({"keys": ["tune.lockIn"], "values": {"tune.lockIn": 0.05}, "full": {"tune.lockIn": 0.05}, "family": "walk", "problem": "p@walk/-"})
    g.factory.write_text(json.dumps({"values": {"tune.lockIn": 0.2}}), encoding="utf-8")  # 开发重调了出厂参数
    assert g.params_payload() is None
    assert g.active_version() == "factory" and [r["version"] for r in g.state["revalidate"]] == ["v0001"]
    assert g.changes()[-1]["action"] == "suspend"
    assert g.needs_work().startswith("重新验证")
    # 开发跑纯出厂值: overlay=false
    g.adopt({"keys": [], "values": {}, "full": {"tune.lockIn": 0.07}, "family": "walk", "problem": "p@walk/-"})
    g.cfg = {**g.cfg, "motion": {**g.cfg["motion"], "growth": {**g.cfg["motion"]["growth"], "overlay": False}}}
    assert g.params_payload() is None


def test_watch_rolls_back_when_daily_rate_worse(tmp_path):
    g = growth(tmp_path)
    t0 = datetime.now() - timedelta(days=2)
    # 换上前: v0001 之前用出厂参数, 拿放 200 次失败 4 次
    for i in range(4):
        MG._append(g.dir / "faults.jsonl", {**rec(), "params": "factory", "got": (t0 + timedelta(minutes=i)).isoformat(timespec="seconds")})
    MG._append(g.dir / "exposure.jsonl", {"at": (t0 + timedelta(hours=1)).isoformat(timespec="seconds"), "params": "factory", "exposure": {"carry": 200}})
    g.adopt({"keys": ["tune.lockIn"], "values": {"tune.lockIn": 0.05}, "full": {"tune.lockIn": 0.05}, "family": "carry", "problem": "put.slam@carry/phys"})
    assert g.watch()["rolled_back"] is False, "换上后还没数据"
    # 换上后: 60 次失败 12 次 -> 明显变差, 自动退回, 她说一句
    g.ingest({"params": "v0001", "exposure": {"carry": 60}, "records": [rec()] * 12})
    w = g.watch()
    assert w["rolled_back"] and w["checks"]["carry"]["worse"]
    assert g.active_version() == "factory"
    assert g.changes()[-1]["action"] == "auto-rollback" and "换回原来的" in g.announce()["line"]


def test_worse_needs_enough_data_and_significance():
    assert MG.worse(3, 10, 1, 100)["worse"] is False  # 换上后才 10 次
    assert MG.worse(6, 100, 5, 100)["worse"] is False  # 差不多
    assert MG.worse(20, 100, 5, 100)["worse"] is True
    assert abs(MG.binom_sf(0, 10, 0.3) - 1) < 1e-12 and abs(MG.binom_sf(10, 10, 0.5) - 0.5 ** 10) < 1e-12


def test_tick_respects_switch_gate_and_work(tmp_path, monkeypatch):
    g = growth(tmp_path)
    started = []
    monkeypatch.setattr(g, "start", lambda why: started.append(why))
    monkeypatch.setattr(g, "can_run", lambda: None)
    ok = lambda s: {"ok": True, "why": []}  # noqa: E731
    assert g.tick(gate=ok) == {"auto": False, "started": False}, "默认关"
    g._save_state(auto=True)
    assert g.tick(gate=ok)["why"] == "没有要练的"
    g.ingest({"records": [rec()] * 6})
    assert g.tick(gate=lambda s: {"ok": False, "why": ["在玩游戏 (全屏)"]})["why"] == "在玩游戏 (全屏)"
    assert g.tick(gate=ok)["started"] and started == ["新记了 6 条动作缺陷"]
    g._save_state(last_start=datetime.now().isoformat(timespec="seconds"))
    assert "不到两小时" in g.tick(gate=ok)["why"]
    monkeypatch.setattr(g, "can_run", lambda: "这台电脑上没有 node")  # 装好的版本现在没带 node
    assert "练不了" in g.tick(gate=ok)["why"]
    monkeypatch.setattr(g, "can_run", lambda: None)
    g._save_state(last_start=None)
    assert g.tick(gate=ok, page_busy=True)["why"] == "她在和你说话"
    g.busy = lambda: "她在做电脑任务单"
    assert g.tick(gate=ok)["why"] == "她在做电脑任务单"


def test_session_busy():
    class Turn:
        def __init__(self, fin): self.finalized = fin

    class S:
        def __init__(self, cur=None, listening=False): self.current, self.listening = cur, listening

    class Ch:
        def __init__(self, tasks): self.tasks = tasks
    assert MG.session_busy([S(), S(Turn(True))], Ch({})) is None
    assert "说话" in MG.session_busy([S(Turn(False))], None)
    assert "听" in MG.session_busy([S(listening=True)], None)
    assert MG.session_busy([], Ch({"t1": {"state": "done"}})) is None
    assert "任务单" in MG.session_busy([], Ch({"t1": {"state": "running"}}))


def run_app(cfg, scenario):
    async def main():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            return await scenario(client, client.server.port)
        finally:
            await client.close()
    return asyncio.run(main())


def test_http_routes_and_origin(tmp_path):
    cfg = make_cfg(tmp_path)

    async def scenario(client, port):
        bad = await client.post("/api/motion/faults", json={"records": [rec()]}, headers={"Origin": "http://evil.example"})
        good = await client.post("/api/motion/faults", json={"records": [rec()], "exposure": {"carry": 3}},
                                 headers={"Origin": f"http://127.0.0.1:{port}"})
        st = await (await client.get("/api/motion/growth")).json()
        sw = await (await client.post("/api/motion/growth", json={"auto": True})).json()
        params = await client.get("/motiondb/params.json")
        return bad.status, good.status, await good.json(), st, sw, params.status
    bad, good, body, st, sw, pstatus = run_app(cfg, scenario)
    assert bad == 403 and good == 200 and body == {"stored": 1, "dropped": 0, "gen": 0}
    assert st["active"] == "factory" and st["faults_30d"] == {"put.slam|motion": 1}
    assert sw["auto"] is True
    assert (tmp_path / "motion-growth" / "faults.jsonl").exists(), "测试的数据跟着测试的日志目录"
    assert pstatus in (200, 404)


# -- 练习脚本的纯计算部分 -------------------------------------------------------------------------
def test_problem_naming_and_scenarios():
    r = rec(kind="spill", cls="motion", cause="put.slam", ctx={"action": {"kind": "put", "y": 0.51}, "held": [{"physical": True}]})
    assert tool.problem_of(r) == "put.slam@carry/phys"
    assert tool.problem_of(rec(cls="env", kind="spill")) is None, "环境约束不进调参"
    assert tool.problem_of(rec(kind="grab.gap", ctx={"action": {"kind": "pick", "y": 0.0}, "held": []})) == "grab.gap@carry/floor"
    assert tool.scenarios_for("put.slam@carry/phys") == ["carry:full>normal>hot>phys=1"]
    assert tool.scenarios_for("put.slam@carry/low", [rec()]) == ["pickput:book>coffee_table"]
    assert tool.scenarios_for("grab.gap@carry/floor", [rec(ctx={"action": {"kind": "pick"}})]) == ["pickput:ball>coffee_table"]
    assert tool.scenarios_for("put.slam@carry/mid", [rec()]) == ["pickput:ball>desk"]
    assert tool.scenarios_for("foot.slide@walk/-") == ["walk"]


def test_every_fault_kind_has_keys_entry():
    src = (ROOT / "web" / "motion-faults.js").read_text(encoding="utf-8")
    block = src.split("export const FAULTS = {", 1)[1].split("\n};", 1)[0]
    kinds = re.findall(r"^  '?([\w.]+)'?: \{ cls: '(\w+)'", block, re.M)
    assert len(kinds) >= 10
    space = {p["key"] for p in tool.tm.param_space()}
    for kind, cls in kinds:
        assert kind in tool.KEYS, f"{kind} 没写和哪些参数有关 (不是参数能调的写空列表)"
        ks = tool.KEYS[kind]
        for k in (sum(ks.values(), []) if isinstance(ks, dict) else ks):
            assert any(p.startswith(k) for p in space), f"{kind} 的 {k} 不在 web/motion-params.js 里"
    for her in ("tune.carryCare", "tune.handSpeed"):
        assert not any(her.startswith(k) for v in tool.KEYS.values() for k in (sum(v.values(), []) if isinstance(v, dict) else v)), \
            "她自己挑的端法不许被调参改"


def _coverage(scenario, seed=3, frames=600):
    """纯假评估的覆盖证据; 与真实种子映射无关, 不把单测 fixture 当作选片研究."""
    clip = f"cmu69_{68 if seed % 2 else 69}_pick" if scenario == "pickput" or scenario.startswith("pickput:ball>") else "test_idle"
    identity = json.dumps(["actions", "pick", clip, 0, 100], separators=(",", ":"))
    return {"version": tool.COVERAGE_VERSION, "mode": "normal", "frames": frames,
            "trajectory": {"scheme": tool.TRAJECTORY_SCHEME, "id": f"{seed:016x}"},
            "clips": [{"id": identity, "library": "actions", "kind": "pick", "name": clip, "start": 0, "end": 100}],
            "sequence": [identity]}


def _res(scen, excess, frames=600, pain=0.01, ok=True, nat=1.5, n=1):
    if set(scen) == {"idle", "walk"}:
        scen = sorted(tool.SUITE)
    return {"params": {"tune.lockIn": 0.1}, "results": [
        {"scenario": s, "seed": seed, "ok": ok, "frames": frames, "natural": nat, "pain": {"slide": pain},
         "growth": {"excess": {"put.slam": excess}, "faults": {"put.slam": {"n": n}}, "records": [],
                    "coverage": _coverage(s, seed, frames)}} for s in scen for seed in [3, 4]]}


def test_train_cost_and_validation_gates():
    w = tool.tm.pain_weights()
    base = _res(["carry"], 1.0)
    assert abs(tool.train_cost(base, base, "put.slam", w) - 1) < 1e-9
    assert tool.train_cost(_res(["carry"], 0.2), base, "put.slam", w, "blend") < 0.6
    assert tool.train_cost(_res(["carry"], 0.2), base, "put.slam", w) < 0.65
    # hinge: 毛病少了但别的痛觉变差, 比现在还差 (第一版 blend 会接受)
    worse_other = _res(["carry"], 0.2, pain=0.05)
    assert tool.train_cost(worse_other, base, "put.slam", w) > 1 > tool.train_cost(worse_other, base, "put.slam", w, "blend")
    assert tool.train_cost(_res(["carry"], 0.2, frames=800), base, "put.slam", w) > tool.train_cost(_res(["carry"], 0.2), base, "put.slam", w), \
        "变慢要罚"

    class Fake:
        def __init__(self, cand_suite, cand_fault):
            self.cs, self.cf = cand_suite, cand_fault

        def run(self, jobs):
            out = []
            for j in jobs:
                cand = "x" in j["params"]
                if j.get("scenarios"):
                    out.append(self.cf if cand else _res(["carry"], 1.0))
                else:
                    out.append(self.cs if cand else _res(["idle", "walk"], 0))
            return out
    cand = {"problem": "put.slam@carry/phys", "values": {"x": 1}, "scenarios": ["carry"]}
    good = tool.validate(Fake(_res(["idle", "walk"], 0), _res(["carry"], 0.3, n=0)), cand, [3, 4], ["a.vrm", "b.vrm"])
    assert good["pass"], good
    slower = tool.validate(Fake(_res(["idle", "walk"], 0, frames=700), _res(["carry"], 0.3, n=0)), cand, [3, 4], ["a.vrm"])
    assert not slower["pass"] and any("变慢" in x for x in slower["avatars"]["a.vrm"]["why"])
    worse_suite = tool.validate(Fake(_res(["idle", "walk"], 0, pain=0.5), _res(["carry"], 0.3, n=0)), cand, [3, 4], ["a.vrm"])
    assert not worse_suite["pass"]
    no_gain = tool.validate(Fake(_res(["idle", "walk"], 0), _res(["carry"], 0.9)), cand, [3, 4], ["a.vrm"])
    assert not no_gain["pass"] and any("只少了" in x for x in no_gain["avatars"]["a.vrm"]["why"])


def test_validation_also_checks_guard_scenarios():
    """拿放的候选: 拿放本身变好了, 但守门的端刚体杯子洒得多 (交接调太短) —— 不许换上."""
    def res(spec):  # spec: {场景: (平滑量, 痛觉)}
        if set(spec) == {"idle", "walk"}:
            spec = {s: spec["idle"] for s in tool.SUITE}
        return {"params": {}, "results": [{"scenario": k, "seed": s, "ok": True, "frames": 600, "natural": 1.5, "pain": {"slide": p},
                                           "growth": {"excess": {"held.acc": ex}, "faults": {}, "records": [],
                                                      "coverage": _coverage(k, s)}} for k, (ex, p) in spec.items() for s in [3, 4]]}

    class Fake:
        def __init__(self, carry_pain): self.cp = carry_pain

        def run(self, jobs):
            out = []
            for j in jobs:
                cand = "x" in j["params"]
                if j.get("scenarios"):
                    assert j["scenarios"] == ["pickput:ball>coffee_table", "carry:full>normal>hot>phys=1"]
                    out.append(res({"pickput:ball>coffee_table": (2 if cand else 10, 0.01),
                                    "carry:full>normal>hot>phys=1": (0, self.cp if cand else 0.01)}))
                else:
                    out.append(res({"idle": (0, 0.01), "walk": (0, 0.01)}))
            return out
    cand = {"problem": "held.acc@carry/low", "values": {"x": 1}, "scenarios": ["pickput:ball>coffee_table"],
            "guard": ["walk", "carry:full>normal>hot>phys=1"]}
    ok = tool.validate(Fake(0.01), cand, [3, 4], ["a.vrm"])
    assert ok["pass"] and ok["avatars"]["a.vrm"]["fault"]["gain"] > 0.7, ok
    bad = tool.validate(Fake(0.5), cand, [3, 4], ["a.vrm"])
    assert not bad["pass"] and "carry:full>normal>hot>phys=1" in str(bad["avatars"]["a.vrm"]["why"])


# -- 验收清单: 参数和它必须通过的检查一起存, 重验用"原清单 ∪ 现在的强制检查" (2026-10-27 评审) ---------------------------------
PICK = "pickput:ball>coffee_table"
CARRY = "carry:full>normal>hot>phys=1"


class CarryWorse:
    """假评估: 候选 (参数里有 x) 让拿放的毛病少很多, 但端满杯的痛觉大很多."""

    def __init__(self):
        self.jobs = []

    def run(self, jobs):
        out = []
        for j in jobs:
            self.jobs.append(j)
            cand = "x" in j["params"]
            scen = j.get("scenarios") or sorted(tool.SUITE)
            out.append({"params": dict(j["params"]), "results": [
                {"scenario": k, "seed": s, "ok": True, "frames": 600, "natural": 1.5,
                 "pain": {"slide": 0.5 if (cand and k.startswith("carry")) else 0.01},
                 "growth": {"excess": {"held.acc": (2.0 if cand else 10.0) if k.startswith("pickput") else 0.0},
                             "faults": {"held.acc": {"n": 1}} if k.startswith("pickput") else {}, "records": [], "coverage": _coverage(k, s)}}
                for k in scen for s in j["seeds"]]})
        return out

    def close(self):
        pass

    def kill(self):
        pass


def _adopted_with_contract(tmp_path, contract):
    """一版拿放的参数, 当初的验证结果是通过的 (那时候端杯子没变差), 带着验收清单换上."""
    g = growth(tmp_path)
    cand = {"problem": "held.acc@carry/low", "keys": ["tune.blendAction"], "values": {"x": 1, "tune.blendAction": 0.3},
            "scenarios": [PICK], "guard": ["walk", CARRY],
            "validation": {"pass": True, "val_seeds": list(tool.VALIDATION_SEEDS), "avatars": {"a.vrm": {
                "full_after": {"x": 1, "tune.blendAction": 0.3}, "full_before": {"tune.blendAction": 0.195}, "suite_cost": 1.0,
                "fault": {"gain": 0.8, "count": [3, 3], "excess": [10, 2]}, "fails": [0, 0], "slow": {}}}}}
    d = tool.adopt_doc(cand, None, g, contract)
    v = g.adopt(d)["version"]
    return g, v


def _run_args(**kw):
    import argparse
    return argparse.Namespace(**{"force": True, "seeds": "1,2", "val_seeds": ",".join(map(str, tool.VALIDATION_SEEDS)), "avatars": "a.vrm", "val_avatars": "", "url": None,
                                 "port": 0, "tabs": 1, "no_mirror": True, "problem": None, "global_scope": False, "gens": 1, "popsize": 0,
                                 "sigma": 0.2, "cost": "hinge", "no_guard": False, **kw})


def test_contract_is_saved_and_union_never_shrinks():
    c = tool.make_contract("held.acc@carry/low", [PICK], ["walk", CARRY], [3, 4, 5], ["a.vrm"])
    assert CARRY in c["guard"] and c["confirm"]["seeds"] == tool.CONFIRM_SEEDS and c["avatars"] == ["a.vrm"]
    # E12 那种: 当初的守门只有走路和拿放, 现在的强制检查有端刚体杯子 —— 并集里要有
    narrow = tool.make_contract("held.acc@carry/low", [PICK], ["walk", "pickput"], [3, 4, 5], ["wendy.vrm"])
    narrow["gates"]["gain"] = 0.4  # 当初更严的门槛留着
    eff = tool.effective_contract(narrow, "held.acc@carry/low")
    assert set(tool.CARRY_GUARD) <= set(eff["guard"]) and "pickput" in eff["guard"] and eff["avatars"] == ["wendy.vrm"]
    assert eff["gates"]["gain"] == 0.4 and not eff["legacy"]
    # 旧格式 (没存清单): 按现在的规矩补全, 标 legacy
    old = tool.effective_contract(None, "held.acc@carry/low", ["a.vrm"])
    assert old["legacy"] and set(tool.CARRY_GUARD) <= set(old["guard"])


def test_carry_regression_rejected_after_save_reload_and_code_change(tmp_path, monkeypatch):
    """拿放变好、端满杯变差的候选: 保存、重新加载、代码更新以后走重新验证, 还是被拒, 而且自动退回."""
    contract = tool.make_contract("held.acc@carry/low", [PICK], ["walk", CARRY], [3, 4, 5], ["a.vrm"])
    g, v = _adopted_with_contract(tmp_path, contract)
    doc = g.version(v)
    assert doc["acceptance"]["guard"] == ["walk", CARRY], "验收清单和参数一起存"
    g2 = MG.Growth(g.cfg)  # 重新加载 (重启)
    assert g2.version(v)["acceptance"] == doc["acceptance"]
    doc["base"]["code"] = "old-code"  # 代码更新了
    MG._write_json(g2.dir / "params" / f"{v}.json", doc)
    assert g2.needs_work().startswith("代码变了")
    fake = CarryWorse()
    monkeypatch.setattr(tool, "_ev", lambda a, gg, run_dir: fake)
    res = tool.cmd_run(_run_args(), g2)
    assert res["step"] == "revalidate-code" and res["passed"] is False
    assert any(CARRY in (j.get("scenarios") or []) for j in fake.jobs), "重新验证跑了端刚体杯子"
    assert g2.active_version() == "factory" and g2.changes()[-1]["action"] == "auto-rollback"


def test_legacy_version_without_contract_is_revalidated_with_current_guards(tmp_path, monkeypatch):
    """旧格式 (没存验收清单) 的版本: 不当成满足过, 下次按现在的强制检查补验 —— 端满杯变差就退回."""
    g, v = _adopted_with_contract(tmp_path, None)
    doc = g.version(v)
    doc.pop("acceptance")
    MG._write_json(g.dir / "params" / f"{v}.json", doc)
    assert g.pending_contract() == [v] and g.needs_work().startswith("补验收清单")
    fake = CarryWorse()
    monkeypatch.setattr(tool, "_ev", lambda a, gg, run_dir: fake)
    res = tool.cmd_run(_run_args(), g)
    assert res["step"] == "revalidate-legacy" and res["passed"] is False and res["contract"]["legacy"]
    assert g.active_version() == "factory"


def test_stale_factory_revalidation_keeps_the_contract(tmp_path, monkeypatch):
    """出厂参数变了停用的那一版: 重新验证也按原清单 ∪ 现在的强制检查, 端满杯变差就不再用."""
    contract = tool.make_contract("held.acc@carry/low", [PICK], ["walk", "pickput"], [3, 4, 5], ["a.vrm"])  # 当初没带端杯子
    g, v = _adopted_with_contract(tmp_path, contract)
    g.factory.write_text(json.dumps({"values": {"tune.lockIn": 0.2}}), encoding="utf-8")
    assert g.params_payload() is None and g._revalidate()[0]["version"] == v
    fake = CarryWorse()
    monkeypatch.setattr(tool, "_ev", lambda a, gg, run_dir: fake)
    res = tool.cmd_run(_run_args(), g)
    assert res["step"] == "revalidate" and res["passed"] is False and CARRY in res["contract"]["guard"]
    assert g.active_version() == "factory" and g.changes()[-1]["action"] == "drop" and not g._revalidate()


# -- 按形象的槽位 ------------------------------------------------------------------------------------------------
def growth_with_avatars(tmp_path):
    vrm = tmp_path / "vrm"
    vrm.mkdir()
    (vrm / "a.vrm").write_bytes(b"body A")
    (vrm / "b.vrm").write_bytes(b"body B")
    g = growth(tmp_path)
    g.cfg = {**g.cfg, "avatar": {"dir": str(vrm), "default": "a.vrm"}}
    return g, vrm


def test_avatar_slots_apply_fall_back_and_roll_back_separately(tmp_path):
    g, vrm = growth_with_avatars(tmp_path)
    sa = MG.avatar_sha(g.cfg, "a.vrm")
    assert sa and sa != MG.avatar_sha(g.cfg, "b.vrm")
    out = g.adopt({"keys": ["tune.lockIn"], "values": {"tune.lockIn": 0.05}, "full": {"tune.lockIn": 0.05, "filter.arm.acc": 145},
                   "family": "carry", "problem": "held.acc@carry/low", "line": MG.line_for("carry", "lighter"),
                   "scope": {"avatars": [{"file": "a.vrm", "sha": sa}]}})
    assert out["slots"] == [sa]
    assert g.params_payload() is None, "形象那一槽不进所有形象共用的 params.json"
    assert g.avatar_params("a.vrm")["version"] == out["version"] and g.avatar_params("a.vrm")["values"]["tune.lockIn"] == 0.05
    assert g.avatar_params("b.vrm")["version"] == "factory", "别的形象照旧"
    assert g.announce("a.vrm")["line"].endswith("还没完全好") and g.announce("b.vrm") is None, "只对那个形象说"
    # 模型内容变了 (同名换了身体): 找不到自己那一槽, 回到基础参数
    (vrm / "a.vrm").write_bytes(b"body A, retouched")
    assert g.avatar_params("a.vrm")["version"] == "factory"
    (vrm / "a.vrm").write_bytes(b"body A")
    # 退回只退那一槽
    g.adopt({"keys": [], "values": {}, "full": {"tune.lockIn": 0.09}, "family": "walk", "problem": "foot.slide@walk/-"})  # 基础槽位
    assert g.avatar_params("b.vrm")["values"]["tune.lockIn"] == 0.09 and g.avatar_params("a.vrm")["values"]["tune.lockIn"] == 0.05
    g.rollback(True, slot=sa)
    assert g.avatar_params("a.vrm")["values"]["tune.lockIn"] == 0.09, "退回到基础那一版"
    assert g.active_version() != "factory", "基础槽位不受影响"


def test_reset_default_keeps_history_and_history_reset_drops_stale_generation(tmp_path):
    g = growth(tmp_path)
    g._save_state(auto=True)
    g.ingest({"records": [rec()], "exposure": {"carry": 3}})
    v = g.adopt({"keys": [], "values": {}, "full": {"tune.lockIn": 0.07}, "family": "carry", "problem": "p@carry/-"})["version"]
    g.add_candidate({"problem": "p@carry/-", "values": {}})
    # 默认: 只退回出厂参数, 历史留着 (评审第 6 节)
    out = g.reset("测试")
    assert out["history"] is False and out["gen"] == 0 and any(v in x for x in out["cleared"])
    assert any("faults" in x for x in out["kept"]) and any("params/" in x for x in out["kept"])
    assert g.params_payload() is None and g.slots() == {} and g.versions() == [v] and len(g.faults()) == 1 and len(g.candidates()) == 1
    assert g.state["auto"] is True
    # 连历史一起清
    out = g.reset("测试", history=True)
    assert out["gen"] == 1 and {"faults.jsonl", "params/", "candidates/"} <= set(out["cleared"])
    assert g.faults() == [] and g.versions() == [] and g.candidates() == [] and g.params_payload() is None
    assert g.state["auto"] is True, "用户的开关是设置, 不是学到的东西, 留着"
    assert g.ingest({"gen": 0, "records": [rec()]})["stored"] == 0, "重置之前攒着没发的不收"
    assert g.ingest({"gen": 1, "records": [rec()]})["stored"] == 1
    g2 = MG.Growth(g.cfg)  # 重启以后也不会回来
    assert len(g2.faults()) == 1 and g2.versions() == [] and g2.gen == 1


def test_scenario_gate_compares_failures_with_baseline():
    """基线自己也做不成的场景, 候选照样做不成: 不算候选变差 (E14: wendy 的 pickput:ball>sofa 改前改后都是 3 次全失败)."""
    w = tool.tm.pain_weights()
    both_fail = _res(["sofa"], 0, ok=False)
    b, c = tool.tm.per_scenario(both_fail, w), tool.tm.per_scenario(both_fail, w)
    assert tool.tm.cost(c, b) > 3, "调优的代价按候选失败直接罚 (1 次失败加 3)"
    assert abs(tool.rel_cost(c, b) - 1) < 1e-9
    worse = tool.tm.per_scenario(_res(["sofa"], 0, ok=False), w)
    ok = tool.tm.per_scenario(_res(["sofa"], 0, ok=True), w)
    assert tool.rel_cost(worse, ok) > 3, "多出来的失败照样重罚"


def test_candidate_adopt_needs_current_contract_and_stack(tmp_path):
    """候选要换上: 最近一次核对通过、运行栈指纹和现在一样、核对用的验收清单就是现在的清单 (加了强制检查就要重核)."""
    import pytest
    g, _ = growth_with_avatars(tmp_path)
    f = tmp_path / "cand.json"
    f.write_text(json.dumps({"problem": "held.acc@carry/low", "keys": ["x"], "values": {"x": 1}, "scenarios": [PICK],
                             "guard": ["walk"], "validation": {"val_seeds": [3, 4, 5], "avatars": {}}}), encoding="utf-8")
    c = tool.candidate_add(g, f, ["a.vrm"], "测试")
    assert c["status"] == "pending" and c["scope"]["avatars"][0]["sha"] == MG.avatar_sha(g.cfg, "a.vrm")
    with pytest.raises(SystemExit, match="还没核对通过"):
        tool.candidate_adopt(g, c["id"])
    fp = {"a.vrm": g.fingerprint("a.vrm")}
    cv = {"version": tool.COVERAGE_VERSION, "pass": True}
    v = {"pass": True, "gate_version": tool.GATE_VERSION, "avatars": {"a.vrm": {"pass": True,
         "coverage": {"pass": True, "suite": cv, "extra": cv}, "fault": {"count": [6, 6], "gain": 0.3}}}}
    old = {"fingerprint": fp, "contract": c["acceptance"], "pass": True, "validation": v, "confirmation": v,
           "baseline": g.baseline(), "values": dict(c["values"]), "full_after": {"a.vrm": {"x": 1}}, "full_before": {"a.vrm": {}}}
    g.update_candidate(c["id"], checks=[old], status="confirmed")
    with pytest.raises(SystemExit, match="验收清单变了"):  # 当初的清单没有端刚体杯子, 现在是强制检查
        tool.candidate_adopt(g, c["id"])
    eff = tool.effective_contract(c["acceptance"], c["problem"], ["a.vrm"])
    g.update_candidate(c["id"], checks=[{**old, "contract": eff, "fingerprint": {"a.vrm": {**fp["a.vrm"], "code": "old"}}}])
    with pytest.raises(SystemExit, match="运行栈变了"):
        tool.candidate_adopt(g, c["id"])
    assert g.status()["candidates"][0]["stale"] is True, "核对以后代码改了: 设置页要说换上前要重核, 不能只写核对过了"
    g.update_candidate(c["id"], checks=[{**old, "contract": eff}])
    assert g.status()["candidates"][0]["stale"] is False
    out = tool.candidate_adopt(g, c["id"])
    assert g.status()["candidates"][0]["stale"] is False, "换上了就不算过期"
    assert g.avatar_params("a.vrm")["version"] == out["version"] and g.avatar_params("b.vrm")["version"] == "factory"
    assert g.announce("a.vrm")["line"].endswith("还没完全好"), "次数没少: 只说轻了"


def test_new_avatar_gets_profiled_first_with_base_params(tmp_path, monkeypatch):
    """没见过的形象 (按内容摘要): 先体检 (基础参数下的基线、身体比例), 不调参; 之后第一次调预算有限."""
    g, vrm = growth_with_avatars(tmp_path)
    g.ingest({"avatar": {"file": "b.vrm"}, "records": [], "exposure": {"walk": 1}})
    assert g.needs_work() == "新形象 b.vrm 先体检" and g.status()["unprofiled"] == ["b.vrm"]

    class Fake(CarryWorse):
        def run(self, jobs):
            out = super().run(jobs)
            for j, o in zip(jobs, out):
                assert j["params"] == {}, "体检用基础参数"
                if j.get("checks"):
                    o["checks"] = {"model": {"armToHip": 1.02, "legToHip": 1.0}}
            return out
    monkeypatch.setattr(tool, "_ev", lambda a, gg, run_dir: Fake())
    res = tool.cmd_run(_run_args(), g)
    assert res["step"] == "calibrate" and res["avatar"] == "b.vrm" and res["profile"]["body"]["armToHip"] == 1.02
    sha = MG.avatar_sha(g.cfg, "b.vrm")
    assert g.profile(sha)["carry"] and g.unprofiled() == [] and g.needs_work() is None
    assert tool.CALIB_GENS <= 4

