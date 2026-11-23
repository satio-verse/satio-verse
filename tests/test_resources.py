"""实时资源监测和动作执行评估日志."""
import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from test_pipeline import make_cfg, run as run_ws
from wm import resources
from wm.resources import ResourceMonitor


def test_resource_sample_and_snapshot():
    m = ResourceMonitor(interval=2.0, keep=5)
    s = m.sample()
    assert "t" in s and "gpu" in s  # 没有 nvidia-smi 时 gpu 为 None
    if m.psutil is not None:
        assert 0 <= s["cpu"] <= 100 and s["ram_total_mb"] > 0
        assert any(p["self"] for p in s["ours"])  # 至少有服务自己
    for _ in range(7):
        m.samples.append(m.sample())
    snap = m.snapshot(history=3)
    assert len(m.samples) == 5 and len(snap["history"]) == 3 and snap["latest"] is m.samples[-1]


def test_census_finds_top_processes():
    m = ResourceMonitor()
    if m.psutil is None:
        return
    m._last_cpu, m.census_at = {}, None
    m.census()  # 第一次只记基线
    assert m.census_at is not None and m.others == []


@pytest.mark.skipif(os.name != "nt", reason="只有 Windows 走一次取全部进程")
def test_census_takes_one_snapshot_not_psutil_per_process(monkeypatch):
    """以前 psutil.process_iter 一次 4-7 秒、期间拖慢主线程 (约 200 个打不开的进程各拍一次全系统快照, 握着 GIL).
    现在一次系统调用拿全: 不碰 process_iter, 结果和 psutil 对得上."""
    m = ResourceMonitor()
    if m.psutil is None:
        return
    ps = m.psutil

    def walk(*a, **k):
        raise AssertionError("普查不该逐个进程走 psutil")
    monkeypatch.setattr(ps, "process_iter", walk)
    t0 = time.perf_counter()
    m.census()
    took = time.perf_counter() - t0
    assert took < 1.0, took  # 这台机器上约 800 个进程十几毫秒
    snap, me = m._last_cpu, ps.Process()
    assert snap[me.pid][0].lower() == me.name().lower() and snap[0][0] == "System Idle Process" and snap[4][0] == "System"
    t = me.cpu_times()
    assert 0 <= (t.user + t.system) - snap[me.pid][1] < 1.0
    pids = set(ps.pids())
    assert len(pids & set(snap)) >= 0.95 * len(pids)  # 两次取之间有进程进出
    m.census()
    assert m.others_span_s is not None and all(set(r) == {"pid", "name", "cpu"} for r in m.others)


def test_census_ranks_others_and_schedule(monkeypatch):
    m = ResourceMonitor(top=2)
    if m.psutil is None:
        return
    n = m.psutil.cpu_count() or 1
    snaps = iter([{0: ("System Idle Process", 100.0), 10: ("a.exe", 1.0), 11: ("b.exe", 5.0), 12: ("c.exe", 2.0), 13: ("old.exe", 1.0)},
                  {0: ("System Idle Process", 900.0), 10: ("a.exe", 1.0 + 3 * n), 11: ("b.exe", 5.0 + 0.5 * n),
                   12: ("c.exe", 2.0 + 2 * n), 13: ("new.exe", 9.0 * n), 14: ("fresh.exe", 50.0 * n)}])
    monkeypatch.setattr(m, "_processes", lambda: next(snaps))
    m.census()  # 第一次只记基线, 第二次隔 60 秒
    assert m.others == [] and m.others_span_s is None and m._census_gap() == 60
    m.census_at -= 100  # 假装过了 100 秒
    m.census()
    # a 3%、c 2% 进前两名; b 0.5% 不到 1%; pid 13 换了名字 (pid 复用)、14 新来的、空闲进程都不算
    assert m.others == [{"pid": 10, "name": "a.exe", "cpu": 3.0}, {"pid": 12, "name": "c.exe", "cpu": 2.0}]
    assert m.others_span_s == 100 and m._census_gap() == m.census_s
    assert m.sample()["others"] == m.others and m.sample()["others_span_s"] == 100


def test_census_falls_back_to_psutil(monkeypatch):
    m = ResourceMonitor()
    if m.psutil is None:
        return

    def boom():
        raise OSError("测试: 系统调用失败")

    def proc(pid, name, cpu):  # psutil.process_iter 给的对象只用到 .info
        return SimpleNamespace(info={"pid": pid, "name": name, "cpu_times": cpu and SimpleNamespace(user=cpu, system=0.5)})
    monkeypatch.setattr(resources, "_nt_processes", boom)
    monkeypatch.setattr(m.psutil, "process_iter", lambda attrs: [proc(10, "a.exe", 1.0), proc(11, None, 2.0), proc(12, "x", None)])
    m._nt = True  # 别的系统上也走一遍 "失败 -> 退回"
    assert m._processes() == {10: ("a.exe", 1.5), 11: ("", 2.5)} and m._nt is False
    assert m._processes() == {10: ("a.exe", 1.5), 11: ("", 2.5)}  # 失败过就不再试


def test_motion_eval_is_logged(tmp_path):
    async def scenario(ws, hello):
        await ws.send_json({"type": "motion_eval", "label": "sit:sofa", "ok": True, "seconds": 3.2,
                            "min_static_margin_cm": 1.5, "foot_slide_mm": 12.0, "evil": object.__name__})
        await ws.send_json({"type": "ping", "id": 1, "t_client": 0})
        while True:  # 等到 pong, 说明前一条已经处理完
            msg = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
            if msg.get("type") == "pong":
                return

    run_ws(make_cfg(tmp_path), scenario)
    files = list(tmp_path.glob("motion-eval-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["label"] == "sit:sofa" and rec["foot_slide_mm"] == 12.0 and "time" in rec and "type" not in rec
