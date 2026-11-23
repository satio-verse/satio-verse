"""工作台无界面跑法 (wm/agent/headless.py, 2026-11-07 底层对标 P0-5): 跑完一件事出结果、一行一个事件、要点头的默认拒绝、
轨迹导出 (会话 / 压缩掉的原文 / 消息 / 这一轮的每步用量). 假云端, 不花钱; 家目录是临时的."""
import asyncio
import json

from test_cloud import FakeCloud, call, say, spec
from test_pipeline import make_cfg
from wm import cloud as C
from wm import ledger as L
from wm.agent import headless as HL


def run(tmp_path, script, **kw):
    async def main():
        fake = FakeCloud(*script)
        base = await fake.start()
        L.use(L.Ledger(tmp_path / "ledger", {}))
        llm = C.CloudLLM(spec(base))
        events = []
        try:
            ws = tmp_path / "ws"
            ws.mkdir(exist_ok=True)
            result = await HL.run_task(kw.pop("prompt", "建个 a.txt"), workspace=str(ws), model=f"custom|{base}|fake-chat",
                                       home=tmp_path / "home", cfg=make_cfg(tmp_path / "logs"), on_event=events.append, **kw)
            return result, events, ws, fake.requests
        finally:
            await llm.close()
            await fake.close()
            L.use(None)
    return asyncio.run(main())


def test_run_task_completes_and_exports_trajectory(tmp_path):
    script = [call(("write_file", json.dumps({"path": "a.txt", "content": "你好\n"}))), say("建好了 a.txt")]
    traj = tmp_path / "out" / "t.jsonl"
    result, events, ws, reqs = run(tmp_path, script, mode="full", trajectory=traj)
    assert result["ok"] and result["state"] == "completed" and result["summary"] == "建好了 a.txt"
    assert (ws / "a.txt").read_text("utf-8") == "你好\n" and result["stats"]["steps"] == 2
    assert [e["type"] for e in events].count("tool") == 1 and events[-1]["type"] == "done"
    rows = [json.loads(x) for x in traj.read_text("utf-8").splitlines()]
    assert rows[0]["type"] == "session" and rows[-1]["type"] == "run"
    assert any(r["type"] == "message" and r.get("tool_calls") for r in rows)
    steps = rows[-1]["steps_log"]
    assert len(steps) == 2 and steps[0]["calls"] == 1 and steps[0]["model"] == "fake-chat"
    assert "sk-test" not in traj.read_text("utf-8")  # key 不进轨迹


def test_asks_are_denied_without_a_person(tmp_path):
    script = [call(("run_command", json.dumps({"command": "python -c \"print(1)\"", "description": "跑一下"}))), say("没让跑")]
    result, events, ws, reqs = run(tmp_path, script, mode="workspace")
    ask_done = next(e for e in events if e["type"] == "ask_done")
    assert ask_done["how"] == "deny" and result["ok"]
    assert any(e["type"] == "tool_result" and not e["ok"] and "拒绝" in e["text"] for e in events)


def test_bad_arguments_exit_3(tmp_path, capsys):
    assert HL.main(["做点事"]) == 3  # 没给 --workspace
    assert HL.main(["做点事", "--workspace", str(tmp_path / "nope")]) == 3
    assert '"ok": false' in capsys.readouterr().out
