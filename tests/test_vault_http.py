"""保管箱的本机接口 /api/vault 和页面 /vault (wm/channels/vault_http.py): 替身启动器、替身 B 站、临时目录; 不碰真账号."""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import vault_fakes as F
from wm.agent.governance import BlacklistStore
from wm.channels import bili
from wm.channels import vault as V
from wm.channels import vault_http as VH

UIN = 3091211145
PW = "Pa55w0rd-示例-http"
HDR = {VH.PAGE_HEADER: "vault"}


@pytest.fixture(autouse=True)
def _keep_page_url(monkeypatch):
    monkeypatch.setattr(V, "PAGE_URL", V.PAGE_URL)  # register 按端口改它: 测完还原


def run(coro):
    return asyncio.run(coro)


class FakeLauncher:
    def __init__(self):
        self.attached = []
        self.qr = b"\x89PNG fake"
        self.seen = True

    def status(self):
        return {"state": "waiting_qq", "detail": "SnowLuma 在跑；电脑上的 QQ 没开", **({"qr": {"at": 1, "ttl_s": 150}} if self.qr else {})}

    async def attach_now(self, conf):
        self.attached.append(dict(conf))

    def qr_png(self):
        return self.qr

    async def refresh_qr(self):
        return self.seen


def make_world(tmp_path, passport="http://127.0.0.1:9", enabled=True):
    ch = SimpleNamespace(vault=V.Vault(tmp_path / "vault"), snowluma=FakeLauncher(), state="online", error=None,
                         conf={"enabled": enabled, "self_id": UIN, "bili_cookie_file": str(tmp_path / "bili_cookie.txt"),
                               "snowluma_autostart": True},
                         weixin=SimpleNamespace(status=lambda: {"bound": True, "state": "online", "user": "o9cq…abcd"}))
    agent = SimpleNamespace(blacklist=BlacklistStore(tmp_path / "agent", defaults=False))
    cfg = {"vault": {"bili_passport": passport, "bili_api": passport}, "server": {"port": 8790}}
    return ch, agent, cfg


def make_app(ch, agent, cfg, refusal=None):
    app = web.Application()
    VH.register(app, cfg, refusal or (lambda r: "Host 不对" if r.headers.get("X-Bad") else None), SimpleNamespace(channels=ch), agent)
    return app


def test_page_is_served_with_a_tight_policy_and_never_builds_html(tmp_path):
    ch, agent, cfg = make_world(tmp_path)

    async def main():
        async with TestClient(TestServer(make_app(ch, agent, cfg))) as c:
            r = await c.get("/vault")
            return r.status, r.headers, await r.text(), (await c.get("/vault", headers={"X-Bad": "1"})).status
    status, headers, body, bad = run(main())
    assert status == 200 and bad == 403 and headers["Content-Type"].startswith("text/html")
    assert "frame-ancestors 'self'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-store"
    assert "账号保管箱" in body and "innerHTML" not in body and "eval(" not in body and "document.write" not in body


def test_save_never_echoes_the_password_and_protects_the_folder(tmp_path):
    ch, agent, cfg = make_world(tmp_path)

    async def main():
        async with TestClient(TestServer(make_app(ch, agent, cfg))) as c:
            before = await (await c.get("/api/vault")).json()
            r = await c.post("/api/vault", json={"action": "save", "platform": "snowluma", "fields": {"password": PW},
                                                 "purposes": {"qq_attach": True}}, headers=HDR)
            saved_text = await r.text()
            after = await c.get("/api/vault?log=10&qr=1")
            after_text = await after.text()
            await asyncio.sleep(0.05)  # 存了管理页密码: 马上替她挂接一次
            return before, r.status, saved_text, json.loads(after_text), after_text
    before, status, saved_text, after, after_text = run(main())
    assert before["accounts"] == [] and before["protected"] is True  # 服务端一起来就把保管箱目录加进了工作台的黑名单
    assert status == 200 and PW not in saved_text and PW not in after_text
    assert json.loads(saved_text)["account"]["purposes"][0]["state"] == "allow" and "保护名单" in json.loads(saved_text)["protect_note"]
    assert after["protected"] is True and V.protected(agent.blacklist, ch.vault.home)
    assert any(r["kind"] == "command" and r["value"] == "/api/vault" for r in agent.blacklist.list())
    assert [a["id"] for a in after["accounts"]] == ["snowluma"] and after["accounts"][0]["secrets"][0]["set"] is True
    assert after["qq"]["snowluma"]["state"] == "waiting_qq" and after["qq_qr"].startswith("data:image/png;base64,")
    assert after["log"][0]["by"] == "page" and ch.snowluma.attached and ch.snowluma.attached[0]["self_id"] == UIN
    assert {p["id"] for p in after["platforms"]} == {"snowluma", "bilibili", "other"}
    assert "mail" not in after["others"] and after["others"]["weixin"]["bound"] is True
    assert PW not in (ch.vault.home / "accounts" / "snowluma.json").read_text(encoding="utf-8")


def test_posts_need_the_page_header_a_json_object_and_a_known_action(tmp_path):
    ch, agent, cfg = make_world(tmp_path)

    async def main():
        async with TestClient(TestServer(make_app(ch, agent, cfg))) as c:
            out = {}
            out["nohdr"] = (await c.post("/api/vault", json={"action": "forget", "id": "x"})).status
            out["badhost"] = (await c.post("/api/vault", json={"action": "forget"}, headers={**HDR, "X-Bad": "1"})).status
            out["notjson"] = (await c.post("/api/vault", data=b"{oops", headers=HDR)).status
            out["list"] = (await c.post("/api/vault", data=b"[1]", headers=HDR)).status
            out["big"] = (await c.post("/api/vault", data=b"x" * (70 << 10), headers=HDR)).status
            r = await c.post("/api/vault", json={"action": "format-c"}, headers=HDR)
            out["unknown"] = (r.status, (await r.json())["error"])
            r = await c.post("/api/vault", json={"action": "save", "platform": "bilibili", "fields": {"cookie": "SESSDATA=a; bili_jct=b"}}, headers=HDR)
            out["by_hand"] = (r.status, (await r.json())["error"])
            r = await c.post("/api/vault", json={"action": "save", "platform": "nowhere", "fields": {"x": "y"}}, headers=HDR)
            out["nowhere"] = r.status
            r = await c.post("/api/vault", json={"action": "save", "platform": "snowluma", "fields": ["x"]}, headers=HDR)
            out["fields"] = r.status
            r = await c.post("/api/vault", json={"action": "save", "platform": "snowluma", "fields": {"password": "a\nb"}}, headers=HDR)
            out["newline"] = (r.status, (await r.json())["error"])
            out["put"] = (await c.put("/api/vault", json={}, headers=HDR)).status
            return out
    out = run(main())
    assert out["nohdr"] == out["badhost"] == 403 and out["notjson"] == out["list"] == 400 and out["big"] == 413
    assert out["unknown"][0] == 400 and "action 只认" in out["unknown"][1]
    assert out["by_hand"][0] == 400 and "扫码" in out["by_hand"][1] and out["nowhere"] == 400 and out["fields"] == 400
    assert out["newline"][0] == 400 and "换行" in out["newline"][1] and out["put"] == 405
    assert ch.vault.accounts() == []


def test_generic_accounts_purposes_and_forget(tmp_path):
    ch, agent, cfg = make_world(tmp_path)

    async def main():
        async with TestClient(TestServer(make_app(ch, agent, cfg))) as c:
            r = await c.post("/api/vault", json={"action": "save", "platform": "other", "label": "某网站小号", "user": "someone@example.com",
                                                 "fields": {"secret": "tok-123"}}, headers=HDR)
            body = await r.json()
            aid = body["account"]["id"]
            text = json.dumps(body, ensure_ascii=False)
            p = await c.post("/api/vault", json={"action": "purpose", "id": aid, "purpose": "x", "state": "allow"}, headers=HDR)
            f1 = await (await c.post("/api/vault", json={"action": "forget", "id": aid}, headers=HDR)).json()
            f2 = await (await c.post("/api/vault", json={"action": "forget", "id": aid}, headers=HDR)).json()
            return aid, text, p.status, f1, f2
    aid, text, pstatus, f1, f2 = run(main())
    assert aid.startswith("other-") and "tok-123" not in text and "someone" not in text and "s******@example.com" in text
    assert pstatus == 400 and f1 == {"ok": True} and f2 == {"ok": False}


def test_purposes_can_be_changed_from_the_page_and_allowing_snowluma_kicks_a_check(tmp_path):
    ch, agent, cfg = make_world(tmp_path)
    ch.vault.put("snowluma", {"password": PW})

    async def main():
        async with TestClient(TestServer(make_app(ch, agent, cfg))) as c:
            r = await c.post("/api/vault", json={"action": "purpose", "id": "snowluma", "purpose": "qq_attach", "state": "allow"}, headers=HDR)
            ok = await r.json()
            await asyncio.sleep(0.05)
            bad = await c.post("/api/vault", json={"action": "purpose", "id": "snowluma", "purpose": "qq_attach", "state": "perhaps"}, headers=HDR)
            return ok, bad.status, await bad.json()
    ok, bad, body = run(main())
    assert ok["account"]["purposes"][0]["state"] == "allow" and len(ch.snowluma.attached) == 1
    assert bad == 400 and "三种" in body["error"]


def test_qq_buttons(tmp_path):
    ch, agent, cfg = make_world(tmp_path)

    async def main():
        async with TestClient(TestServer(make_app(ch, agent, cfg))) as c:
            a = await (await c.post("/api/vault", json={"action": "qq_attach"}, headers=HDR)).json()
            q = await (await c.post("/api/vault", json={"action": "qq_qr"}, headers=HDR)).json()
            ch.conf["enabled"] = False
            off = await c.post("/api/vault", json={"action": "qq_attach"}, headers=HDR)
            return a, q, off.status, (await off.json())["error"]
    a, q, off, why = run(main())
    assert a["ok"] and len(ch.snowluma.attached) == 1 and q["ok"] and q["seen"] is True
    assert off == 400 and "QQ 渠道没开" in why


def test_bilibili_scan_over_http_then_check_and_expiry(tmp_path):
    async def main():
        fake = F.FakeBili(script=(86101, 86090, 0))
        runner, port = await F.serve(fake.app())
        try:
            ch, agent, cfg = make_world(tmp_path, passport=f"http://127.0.0.1:{port}")
            Path(tmp_path / "bili_cookie.txt").write_text("SESSDATA=dead-old; DedeUserID=20261107\n", encoding="utf-8")  # 旧的明文 (同一个号)
            bili.use_vault(ch.vault)
            async with TestClient(TestServer(make_app(ch, agent, cfg))) as c:
                async def post(**body):
                    r = await c.post("/api/vault", json=body, headers=HDR)
                    return r.status, await r.json()
                st0, none_check = await post(action="bili_check")
                st1, begin = await post(action="bili_begin")
                polls = [(await post(action="bili_poll"))[1]["login"]["state"] for _ in range(3)]
                status = await (await c.get("/api/vault")).json()
                _, chk = await post(action="bili_check")
                fake.valid = False
                _, chk2 = await post(action="bili_check")
                status2 = await (await c.get("/api/vault")).json()
                st3, confirm = await post(action="bili_confirm", replace=True)
                return fake, st0, none_check, st1, begin, polls, status, chk, chk2, status2, st3, confirm
        finally:
            await runner.cleanup()
    fake, st0, none_check, st1, begin, polls, status, chk, chk2, status2, st3, confirm = run(main())
    assert st0 == 200 and none_check["ok"] is False and "还没有" in none_check["text"]
    assert st1 == 200 and begin["login"]["state"] == "waiting" and begin["login"]["qr"].startswith("data:image/png;base64,")
    assert polls == ["waiting", "scanned", "done"]
    acct = next(a for a in status["accounts"] if a["platform"] == "bilibili")
    assert acct["meta"]["mid"] == "20261107" and acct["meta"]["uname"] == "绫音的小号" and status["bili_file"] == "marker"
    assert acct["purposes"][0]["state"] == "allow" and acct["purposes"][1]["state"] == "ask"
    raw = json.dumps(status, ensure_ascii=False)
    assert fake.sess not in raw and fake.jct not in raw and "REFRESH-TOKEN" not in raw
    assert chk["ok"] and chk["valid"] is True
    assert chk2["ok"] and chk2["valid"] is False
    acct2 = next(a for a in status2["accounts"] if a["platform"] == "bilibili")
    assert acct2["flags"]["expired_at"] and acct2["meta"]["verified"] is False
    assert st3 == 400 and "没有在等" in confirm["error"]


def test_real_server_wires_the_vault_and_guards_the_workbench(tmp_path):
    """app.py 挂上了 /vault 和 /api/vault (查 Host 和 Origin); 起来以后保管箱目录进了工作台的黑名单; 和渠道管理器用同一个保管箱."""
    from test_pipeline import make_cfg
    from wm.app import AGENT, ENGINES, create_app

    async def main():
        app = create_app(make_cfg(tmp_path / "logs"))
        async with TestClient(TestServer(app)) as client:
            st = await (await client.get("/api/vault")).json()
            page = await client.get("/vault")
            evil = await client.get("/api/vault", headers={"Origin": "http://evil.example"})
            nohdr = await client.post("/api/vault", json={"action": "forget", "id": "snowluma"})
            return app, st, page.status, evil.status, nohdr.status
    app, st, page, evil, nohdr = run(main())
    assert st["ok"] and page == 200 and evil == 403 and nohdr == 403
    assert app[VH.KEY] is app[ENGINES].channels.vault and app[VH.KEY].home == V.HOME
    assert V.protected(app[AGENT].blacklist, V.HOME) and st["qq"]["snowluma"] is not None


def test_page_script_only_touches_elements_that_exist():
    """页面脚本里 $('xxx') 用到的编号、data-eye 指向的输入框、模板里挂的 id 都得在页面里真的有 (改页面时手滑最常见的错)."""
    import re
    html = VH.PAGE.read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([A-Za-z][\w-]*)"', html))
    used = set(re.findall(r"\$\('([A-Za-z][\w-]*)'\)", html))
    assert not used - ids, sorted(used - ids)
    assert set(re.findall(r'data-eye="([\w-]+)"', html)) <= ids
    assert len(ids) > 40 and "innerHTML" not in html
