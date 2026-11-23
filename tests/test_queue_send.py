"""繁忙时的发送: 她正忙的时候把下一句排进队列 (不打断这一轮, 做完再发), 页面收得到 queued / queued_ack."""
import json

from wm.app import AGENT

from test_agent import events_until, run_agent


def test_enqueue_while_running_queues_without_starting_a_turn(tmp_path):
    """在跑的时候 enqueue: 只进队列, 不打断、不立刻调模型; 页面拿到 queued_ack 和最新的 queue."""

    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        hub = client.server.app[AGENT]
        hub.get(s["id"]).status = "running"  # 假装正忙: 这一句只该排队
        ws = await client.ws_connect(f"/agent/ws?id={s['id']}")
        await ws.receive_json()
        await ws.send_json({"type": "enqueue", "text": "下一件事也做了"})
        got = await events_until(ws, lambda m: m.get("type") == "queued_ack")
        view = await (await client.get(f"/api/agent?id={s['id']}")).json()
        await ws.close()
        return got, view, len(fake.requests)

    got, view, reqs = run_agent(tmp_path, [], scenario)
    assert got[-1]["type"] == "queued_ack", got
    assert any(m.get("type") == "queued" and [q["text"] for q in m["queue"]] == ["下一件事也做了"] for m in got), got
    assert [q["text"] for q in view["queue"]] == ["下一件事也做了"], view["queue"]
    assert reqs == 0, "排队的时候不该调模型"


def test_enqueue_rejects_empty_text(tmp_path):
    """空话不占队列: WS 回 refused, 队列不动."""

    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        hub = client.server.app[AGENT]
        hub.get(s["id"]).status = "running"
        ws = await client.ws_connect(f"/agent/ws?id={s['id']}")
        await ws.receive_json()
        await ws.send_json({"type": "enqueue", "text": "   "})
        got = await events_until(ws, lambda m: m.get("type") == "refused")
        view = await (await client.get(f"/api/agent?id={s['id']}")).json()
        await ws.close()
        return got, view

    got, view = run_agent(tmp_path, [], scenario)
    assert got[-1]["type"] == "refused" and got[-1]["text"]
    assert view["queue"] == []
