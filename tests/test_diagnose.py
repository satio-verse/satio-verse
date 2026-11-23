"""诊断信息和环境自检 (wm/diagnose.py, /api/diagnose, /api/selfcheck): 去隐私、自检每项的格式、只认本机页面、不漏 key."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from test_cloud import KEY, FakeCloud, make_cfg, profile, recv, say
from wm import diagnose as D
from wm.app import create_app


def test_scrub_keys_cookies_long_numbers_and_home():
    from pathlib import Path
    home = str(Path.home())
    s = D.scrub(f"{home}\\logs a.log token=abc123def Authorization: Bearer sk-live-xyz9876 api_key: \"k-1\" "
                "SESSDATA=sess01; bili_jct=jct9 QQ 1234567890 群 98765432 端口 8790 版本 1.13.8 日期 2026-10-27 13:45:01")
    assert home not in s and "~\\logs" in s
    for secret in ("abc123def", "sk-live-xyz9876", "k-1", "sess01", "jct9", "1234567890", "98765432"):
        assert secret not in s, secret
    assert "12******90" in s and "98****32" in s  # 打码留头尾, 还认得出是哪一个
    assert "端口 8790" in s and "1.13.8" in s and "2026-10-27 13:45:01" in s  # 端口、版本、时间不动


def test_recent_errors_only_today_and_errors(tmp_path):
    from datetime import datetime
    d = tmp_path / "services"
    d.mkdir()
    today = datetime.now().strftime("%Y%m%d")
    (d / f"llm-{today}.log").write_text("INFO 起来了\n2026 ERROR wm.x: 连不上 token=secret1\nWARNING 慢\n"
                                        "Traceback (most recent call last):\n", encoding="utf-8")
    (d / "llm-20200101.log").write_text("ERROR 很久以前\n", encoding="utf-8")
    errs = D.recent_errors(logs=(d,))
    assert len(errs) == 2 and errs[0].startswith(f"[llm-{today}.log]") and "secret1" not in errs[0]
    assert not any("很久以前" in e for e in errs)


def test_api_diagnose_and_selfcheck_local_only_and_no_key(tmp_path):
    async def main():
        fake = FakeCloud(say('{"op":"chat","acts":[],"emo":"happy","reply":"嗯。"}'))
        base = await fake.start()
        client = TestClient(TestServer(create_app(make_cfg(tmp_path, "billing.balance_every_s=0"))))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            assert json.loads((await ws.receive()).data)["type"] == "hello"
            await ws.send_json(profile(base))  # 带着 key 的云端模型: 诊断里不能有这个 key
            await recv(ws, "profile_done")
            evil = {"Origin": "https://evil.example"}
            blocked = [(await client.get(p, headers=evil)).status for p in ("/api/diagnose", "/api/selfcheck")]
            diag = await (await client.get("/api/diagnose", params={"page": "WebView2 测试"})).json()
            checks = (await (await client.get("/api/selfcheck")).json())["checks"]
            return blocked, diag, checks
        finally:
            await client.close()
            await fake.close()

    blocked, diag, checks = asyncio.run(main())
    assert blocked == [403, 403]
    text = diag["text"]
    assert text.startswith("WorldModel 诊断信息") and "页面: WebView2 测试" in text and "== 本地服务 ==" in text
    assert KEY not in json.dumps(diag) and "== 最近的报错" in text
    ids = {c["id"]: c for c in checks}
    assert {"server", "python", "disk", "gpu", "services", "cloud", "time", "logs", "7zip", "errors"} <= set(ids)
    assert all(c["level"] in ("ok", "warn", "fail", "skip") and c["name"] for c in checks)
    assert ids["cloud"]["level"] == "ok" and ids["logs"]["level"] == "ok" and KEY not in json.dumps(checks)
