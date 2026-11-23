"""HAST-SEC 运动层的文件服务. 没装 third_party/hast-sec 时整组跳过."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wm import hast
from wm.app import create_app
from wm.config import load_config

CFG = load_config(overrides=["llm.engine=mock", "asr.engine=none", "tts.engine=mock", "face.engine=none", "memory.vault=\"\""])
pytestmark = pytest.mark.skipif(not hast.available(CFG), reason="未安装 HAST-SEC 运动层")


def run(scenario, *extra):
    cfg = load_config(overrides=["llm.engine=mock", "asr.engine=none", "tts.engine=mock", "face.engine=none", "memory.vault=\"\"", *extra])

    async def main():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(main())


def test_library_merges_v15_skills():
    lib = hast.build_library(hast.hast_dir(CFG))
    keys = {c["key"] for c in lib["classes"]}
    assert lib["version"] == 15 and len(lib["bones"]) == 53
    assert {"right-挥手打招呼", "left-抬腿", "right-向上举过头部", "body-自然活动"} <= keys


def test_serves_modules_configs_models_and_trajectories():
    async def scenario(c):
        status = (await (await c.get("/api/status")).json())["motion"]
        boot = await (await c.get("/v15-api/bootstrap")).json()
        src = boot["sources"][0]["id"]
        checks = {}
        for path in ("/hast/motion/v9/controller.mjs", "/hast/web/v14_generation.mjs", "/configs/v9_control.json",
                     "/configs/skeleton_profile.json", "/v15-data/base.json", "/baseline-v9-filtered-data/model.json",
                     "/feasible-history/index.json", "/v14-policy/model.json", "/semantic-api/source-filter",
                     f"/v13-sources/{src}.bin"):
            r = await c.get(path)
            checks[path] = (r.status, r.headers.get("Content-Type", "").split(";")[0])
        return status, checks

    status, checks = run(scenario)
    assert status["engine"] == "hast"
    assert all(s == 200 for s, _ in checks.values()), checks
    assert checks["/hast/motion/v9/controller.mjs"][1] == "text/javascript"


def test_rejects_paths_outside_and_wrong_types():
    async def scenario(c):
        return [(await c.get(p)).status for p in (
            "/hast/../wm/app.py", "/hast/service/start.mjs", "/hast/models/body/weights.bin",
            "/configs/..%2FLICENSE", "/v15-data/..%2F..%2FLICENSE", "/v13-sources/nope.bin")]

    assert all(s == 404 for s in run(scenario))


def test_disabled_falls_back_to_procedural():
    async def scenario(c):
        return (await (await c.get("/api/status")).json())["motion"]["engine"], (await c.get("/v15-api/bootstrap")).status

    assert run(scenario, "motion.engine=procedural") == ("procedural", 404)
