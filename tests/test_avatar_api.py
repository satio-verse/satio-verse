import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from wm.app import create_app
from wm.config import load_config


def run(tmp_path, scenario, default="b.vrm"):
    (tmp_path / "a.vrm").write_bytes(b"glTF-a")
    (tmp_path / "b.vrm").write_bytes(b"glTF-bb")
    (tmp_path / "notes.txt").write_text("x")
    cfg = load_config(overrides=["llm.engine=mock", "asr.engine=none", "tts.engine=mock", "face.engine=none", "memory.vault=\"\"",
                                 f"avatar.dir={json.dumps(str(tmp_path))}", f"avatar.default={default}",
                                 f"trace.log_dir={json.dumps(str(tmp_path / 'logs'))}"])

    async def main():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(main())


def test_lists_vrm_files_and_default(tmp_path):
    async def scenario(c):
        return await (await c.get("/api/avatars")).json()

    data = run(tmp_path, scenario)
    assert data == {"default": "b.vrm", "items": [{"name": "a.vrm", "bytes": 6}, {"name": "b.vrm", "bytes": 7}]}


def test_missing_default_falls_back_to_first(tmp_path):
    async def scenario(c):
        return (await (await c.get("/api/avatars")).json())["default"]

    assert run(tmp_path, scenario, default="gone.vrm") == "a.vrm"


def test_serves_vrm_and_rejects_other_paths(tmp_path):
    async def scenario(c):
        ok = await c.get("/avatars/b.vrm")
        body = await ok.read()
        statuses = [(await c.get(p)).status for p in
                    ("/avatars/notes.txt", "/avatars/..%2Fnotes.txt", "/avatars/missing.vrm", "/avatars/%2E%2E%2Fb.vrm")]
        return ok.status, body, ok.headers["Content-Type"], statuses

    status, body, ctype, statuses = run(tmp_path, scenario)
    assert (status, body, ctype) == (200, b"glTF-bb", "model/gltf-binary")
    assert all(s == 404 for s in statuses)
