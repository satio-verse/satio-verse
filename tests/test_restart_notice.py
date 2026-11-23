"""服务端重启 = 她睡一觉 (docs/roadmap/self-evolution.md 4.3), 服务端这边:

- 连上时的 hello 带这次启动的情况 (看守进程 tools/serve.py 用环境变量告诉的: 启动编号、为什么起、给人看的一句、刚合进来的本事);
  直接 python -m wm 起的是 normal;
- /api/supervisor 只认看守进程给的钥匙: 告诉每个连着的桌宠 server_restart (她去睡一会儿, 你的话变便利贴), 再回;
- QQ 那边她在做什么 (channel_doing): 回 QQ、看视频…, 桌面上的小屋里看得出来.
"""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from wm.app import create_app
from wm.config import load_config

KEY = "k" * 32


def run(tmp_path, scenario):
    cfg = load_config(overrides=["llm.engine=mock", "asr.engine=none", "tts.engine=mock", "face.engine=none", "memory.vault=\"\"",
                                 f"trace.log_dir={json.dumps(str(tmp_path / 'logs'))}"])

    async def main():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(main())


async def first(ws, kind, timeout=5.0):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("type") == kind:
            return msg


def test_hello_carries_how_this_boot_started(tmp_path, monkeypatch):
    monkeypatch.setenv("WM_BOOT_ID", "abc12345")
    monkeypatch.setenv("WM_BOOT_MODE", "reverted")
    monkeypatch.setenv("WM_BOOT_NOTE", "刚合进来的「听语音」一上线服务端就退出了, 已经退回去了")
    monkeypatch.setenv("WM_BOOT_LEARNED", "听懂 QQ 语音\n学会用小黑盒")

    async def scenario(c):
        ws = await c.ws_connect("/ws")
        hello = await first(ws, "hello")
        await ws.close()
        return hello

    assert run(tmp_path, scenario)["boot"] == {"id": "abc12345", "mode": "reverted",
                                               "note": "刚合进来的「听语音」一上线服务端就退出了, 已经退回去了",
                                               "learned": ["听懂 QQ 语音", "学会用小黑盒"]}


def test_hello_when_started_directly(tmp_path, monkeypatch):
    for k in ("WM_BOOT_ID", "WM_BOOT_MODE", "WM_BOOT_NOTE", "WM_BOOT_LEARNED"):
        monkeypatch.delenv(k, raising=False)

    async def scenario(c):
        ws = await c.ws_connect("/ws")
        hello = await first(ws, "hello")
        await ws.close()
        return hello

    boot = run(tmp_path, scenario)["boot"]
    assert boot["mode"] == "normal" and boot["learned"] == [] and len(boot["id"]) == 8


def test_supervisor_notice_needs_the_key_and_tells_every_pet(tmp_path, monkeypatch):
    monkeypatch.setenv("WM_SUPERVISOR_KEY", KEY)

    async def scenario(c):
        a, b = await c.ws_connect("/ws"), await c.ws_connect("/ws")
        await first(a, "hello")
        await first(b, "hello")
        statuses = [(await c.post("/api/supervisor", json={"action": "restart"}, headers=h)).status
                    for h in ({}, {"X-Supervisor-Key": "wrong"})]
        ok = await c.post("/api/supervisor", json={"action": "restart", "eta_s": 25}, headers={"X-Supervisor-Key": KEY})
        got = await ok.json()
        notes = [await first(a, "server_restart"), await first(b, "server_restart")]
        stop = await c.post("/api/supervisor", json={"action": "stop"}, headers={"X-Supervisor-Key": KEY})
        last = await first(a, "server_restart")
        await a.close()
        await b.close()
        return statuses, ok.status, got, notes, stop.status, last

    statuses, status, got, notes, stop_status, last = run(tmp_path, scenario)
    assert statuses == [403, 403] and status == 200 and got["ok"] and got["sessions"] == 2
    assert all(n == {"type": "server_restart", "reason": "restart", "eta_s": 25, "stop": False} for n in notes)
    assert stop_status == 200 and last["stop"] is True and last["eta_s"] is None


def test_supervisor_notice_refused_without_a_supervisor(tmp_path, monkeypatch):
    monkeypatch.delenv("WM_SUPERVISOR_KEY", raising=False)  # 不是看守进程起的: 谁调都不认

    async def scenario(c):
        return (await c.post("/api/supervisor", json={"action": "restart"}, headers={"X-Supervisor-Key": ""})).status

    assert run(tmp_path, scenario) == 403


def test_qq_side_doing_reaches_the_pet(tmp_path):
    """主人在 QQ 上私聊她, 她回了: 桌宠收到 channel_doing (qq, "在 QQ 上跟你聊天"), 小屋里她坐到电脑前."""
    from test_channels import OWNER, cloud_model, pmsg, run_qq
    from test_workbench import smart_cloud

    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base), "private": {"min_gap_s": 0}})
        await inbox.get("channel_config_done")
        await bot.push(pmsg(OWNER, "在干嘛呀", 1))
        return await inbox.get("channel_doing", lambda m: m["what"] == "qq", timeout=15)

    doing = run_qq(tmp_path, scenario, vault=True, cloud_script=[smart_cloud()])
    assert doing["text"] == "在 QQ 上跟你聊天" and doing["secs"] > 0
