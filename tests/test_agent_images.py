"""工作台也能看图: 这一轮材料里的图片挂到发给模型的消息上 (多模态), 不只是存个文件路径."""
import base64
import io

from PIL import Image

from test_agent import events_until, run_agent
from test_cloud import say


def test_material_image_becomes_image_url(tmp_path):
    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 20, 20)).save(buf, "PNG")
        up = await (await client.post(
            f"/api/agent/context?id={s['id']}",
            json={"name": "a.png", "data": base64.b64encode(buf.getvalue()).decode()})).json()
        item = up["item"]
        ws = await client.ws_connect(f"/agent/ws?id={s['id']}")
        await ws.receive_json()
        await ws.send_json({"type": "send", "text": "看看这张图", "context_ids": [item["id"]]})
        await events_until(ws, lambda m: m.get("type") == "done")
        await ws.close()
        return fake.requests[0]["body"]["messages"][-1]["content"]

    content = run_agent(tmp_path, [say("看到了，是一张红色的图。")], scenario)
    assert isinstance(content, list), content
    text = next(p["text"] for p in content if p["type"] == "text")
    image = next(p for p in content if p["type"] == "image_url")
    assert "看看这张图" in text
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_no_image_keeps_plain_text(tmp_path):
    """没有附图的一轮: 消息还是普通字符串, 不多加东西."""

    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        ws = await client.ws_connect(f"/agent/ws?id={s['id']}")
        await ws.receive_json()
        await ws.send_json({"type": "send", "text": "你好"})
        await events_until(ws, lambda m: m.get("type") == "done")
        await ws.close()
        return fake.requests[0]["body"]["messages"][-1]["content"]

    content = run_agent(tmp_path, [say("在呐。")], scenario)
    assert content == "你好", content
