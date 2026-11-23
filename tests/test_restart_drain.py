"""重启前: 看守调 /api/supervisor 时, 先把在跑的工作台会话停在原地, 并留一句"回来接着做"."""
import json

from wm.app import AGENT

from test_agent import run_agent


def test_supervisor_stops_running_session_and_leaves_a_note(tmp_path, monkeypatch):
    monkeypatch.setenv("WM_SUPERVISOR_KEY", "k-test-123")

    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        hub = client.server.app[AGENT]
        session = hub.get(s["id"])
        session.status = "running"  # 假装她正在跑
        resp = await client.post("/api/supervisor", json={"action": "restart", "reason": "test"},
                                 headers={"X-Supervisor-Key": "k-test-123"})
        assert resp.status == 200, await resp.text()
        notes = [e for e in session.events if e.get("type") == "note" and "重启" in str(e.get("text") or "")]
        return session, notes

    session, notes = run_agent(tmp_path, [], scenario)
    assert session.stop_event.is_set(), "在跑的会话应该被停下来"
    assert notes, "应该给会话留一句'我先去重启'"
    assert session.run.get("restart_pending") is True, "记一笔: 这次不是用户按的停"


def test_supervisor_requires_key(tmp_path):
    async def scenario(client, fake, ws_dir, llm):
        resp = await client.post("/api/supervisor", json={"action": "restart"})
        return resp.status

    status = run_agent(tmp_path, [], scenario)
    assert status == 403
