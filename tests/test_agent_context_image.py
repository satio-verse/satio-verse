"""对话里的图片: GET /api/agent/context/image 把附件图片原样交给页面, 路径只从 ContextStore 拿."""
import base64
import io

from PIL import Image

from test_agent import run_agent


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_context_image_returns_bytes(tmp_path):
    raw = _png()

    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        up = await (await client.post(f"/api/agent/context?id={s['id']}",
                                      json={"name": "a.png", "data": base64.b64encode(raw).decode()})).json()
        item = up["item"]
        resp = await client.get(f"/api/agent/context/image?id={s['id']}&item={item['id']}")
        body = await resp.read()
        return resp.status, resp.headers.get("Content-Type"), body

    status, ctype, body = run_agent(tmp_path, [], scenario)
    assert status == 200, status
    assert ctype == "image/png", ctype
    assert body == raw


def test_unknown_item_is_400(tmp_path):
    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        resp = await client.get(f"/api/agent/context/image?id={s['id']}&item=nope")
        return resp.status

    assert run_agent(tmp_path, [], scenario) == 400


def test_other_method_is_405(tmp_path):
    async def scenario(client, fake, ws_dir, llm):
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        resp = await client.post(f"/api/agent/context/image?id={s['id']}&item=x")
        return resp.status

    assert run_agent(tmp_path, [], scenario) in (405, 404)
