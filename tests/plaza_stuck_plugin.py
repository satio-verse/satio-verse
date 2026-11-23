"""测试替身: 一个"不肯离场"的插件进程. 说桥协议 v0, 但收到 leave 装作没听见; 还起一个子进程, 用来核对整棵进程树被结束.

只在 tests/test_world_host.py 里被当子进程起. 标准输出第一行 {"ready": true, "port": N, "child": 子进程号}.
"""
import asyncio
import json
import os
import subprocess
import sys

from aiohttp import web


async def main() -> None:
    token = os.environ["WM_PLUGIN_TOKEN"]
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])

    async def host(request):
        if request.headers.get("Authorization") != "Bearer " + token:
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:   # 什么帧都收, 什么都不回: 包括 leave
            pass
        return ws

    app = web.Application()
    app.router.add_get("/host", host)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    print(json.dumps({"ready": True, "port": runner.addresses[0][1], "child": child.pid}), flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
