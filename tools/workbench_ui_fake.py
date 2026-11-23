"""工作台界面测试里"边读边问"那一段要在种子跑完以后再问她: 种子进程退出时它的假模型也关了, 这里在同一个端口 (种子打印的 fake_port,
已登记的模型指向它) 重新起一个, 用同一个 route(), 不花钱. 第一行打印 {"base": ...}, 然后等标准输入关掉 (node 脚本结束时关) 再退出.
用法: python workbench_ui_fake.py <端口>"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "wm").is_dir() and (p / "tests").is_dir())
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "tests"), str(ROOT)]
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from test_cloud import FakeCloud  # noqa: E402
from workbench_ui_seed import reply  # noqa: E402


async def main() -> None:
    fake = FakeCloud(reply)
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.chat)
    app.router.add_get("/v1/models", fake.models)
    fake.server = TestServer(app, port=int(sys.argv[1]))
    await fake.server.start_server()
    base = str(fake.server.make_url("/v1"))
    print(json.dumps({"base": base}), flush=True)
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
    await fake.close()


asyncio.run(main())
