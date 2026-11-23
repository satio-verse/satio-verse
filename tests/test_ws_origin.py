"""/ws 的来源检查: 本机页面和没有 Origin 的客户端放行, 别的网页来源和不是本机的 Host (DNS 重绑定) 回 403."""
import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm.app import create_app


def run(cfg, scenario):
    async def main():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            return await scenario(client, client.server.port)
        finally:
            await client.close()
    return asyncio.run(main())


async def handshake(client, headers=None) -> int:
    """握手结果: 连上了 (收到 hello) 是 101, 被拒是对方回的状态码."""
    try:
        ws = await client.ws_connect("/ws", headers=headers or {})
    except aiohttp.WSServerHandshakeError as exc:
        return exc.status
    try:
        assert json.loads((await asyncio.wait_for(ws.receive(), 10)).data)["type"] == "hello"
        return 101
    finally:
        await ws.close()


def test_local_origins_pass(tmp_path):
    async def scenario(client, port):
        return [await handshake(client, {"Origin": f"http://127.0.0.1:{port}"}),
                await handshake(client, {"Origin": f"http://localhost:{port}", "Host": f"localhost:{port}"}),
                await handshake(client, {"Origin": f"http://LOCALHOST:{port}/"})]

    assert run(make_cfg(tmp_path), scenario) == [101, 101, 101]


def test_no_origin_passes(tmp_path):
    async def scenario(client, port):
        return await handshake(client)  # 测试、命令行工具 (aiohttp 客户端默认不带 Origin)

    assert run(make_cfg(tmp_path), scenario) == 101


def test_foreign_origins_forbidden(tmp_path, caplog):
    async def scenario(client, port):
        return [await handshake(client, {"Origin": "http://evil.example"}),
                await handshake(client, {"Origin": f"http://127.0.0.1:{port + 1 if port < 65535 else port - 1}"}),  # 本机别的端口上的网页
                await handshake(client, {"Origin": f"https://127.0.0.1:{port}"}),
                await handshake(client, {"Origin": "null"}),  # file:// 和沙箱 iframe
                await handshake(client, {"Origin": ""})]

    assert run(make_cfg(tmp_path), scenario) == [403] * 5
    assert "拒绝了 /ws 连接" in caplog.text and "evil.example" in caplog.text


def test_foreign_host_forbidden(tmp_path):
    async def scenario(client, port):
        return [await handshake(client, {"Host": f"evil.example:{port}"}),
                # DNS 重绑定: 网页和 Host 都是它的域名, 解析到了 127.0.0.1
                await handshake(client, {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"}),
                await handshake(client, {"Host": "127.0.0.1:1"})]

    assert run(make_cfg(tmp_path), scenario) == [403] * 3


def test_allowed_origins_from_config(tmp_path):
    allowed = 'server.allowed_origins=["http://Pet.example/", "http://192.168.1.5:8790"]'

    async def scenario(client, port):
        return [await handshake(client, {"Origin": "http://pet.example"}),
                # 局域网访问: 页面和 Host 都是写进配置的地址
                await handshake(client, {"Origin": "http://192.168.1.5:8790", "Host": "192.168.1.5:8790"}),
                await handshake(client, {"Origin": "http://other.example"}),
                await handshake(client, {"Host": "192.168.1.6:8790"})]

    assert run(make_cfg(tmp_path, allowed), scenario) == [101, 101, 403, 403]
