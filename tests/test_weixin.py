"""微信 (wm/channels/weixin.py, ClawBot 的 iLink 协议): 用本地假的 iLink 服务, 不连腾讯. 覆盖扫码 (等、扫了、确认; 过期换新的)、
token 加密存盘、只收扫码人的消息、回复带 context_token 和请求头、游标存盘、token 过期歇着、解绑;
管理器里微信算主人档 (你在微信上说一句她回微信、她要跟你说的事发到你最近说话的地方); /api/weixin 只认本机页面."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_channels import OWNER, cloud_model, pmsg, reply_json, run_qq, until
from test_cloud import say
from test_pipeline import make_cfg
from wm.app import ENGINES, create_app
from wm.channels import weixin as X
from wm.channels.manager import WEIXIN

USER = "o9owner@im.wechat"
TOKEN = "tok-secret-0123456789"


class FakeILink:
    """假的 iLink: 扫码按 statuses 依次回 (用完一直回最后一个); getupdates 有消息就给, 没有就等一会儿回空."""

    def __init__(self, statuses=("wait", "scaned", "confirmed"), stale=False):
        self.statuses = list(statuses)
        self.qr_count = 0
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.polls: list[dict] = []
        self.headers: list[dict] = []
        self.stale = stale
        self.server = None
        self.base = ""

    async def qrcode(self, request):
        self.qr_count += 1
        self.headers.append(dict(request.headers))
        return web.json_response({"qrcode": f"qr{self.qr_count}", "qrcode_img_content": f"https://liteapp.weixin.qq.com/q/x{self.qr_count}"})

    async def status(self, request):
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        body = {"status": st}
        if st == "confirmed":
            body.update(bot_token=TOKEN, ilink_bot_id="bot1", baseurl=self.base, ilink_user_id=USER)
        return web.json_response(body)

    async def updates(self, request):
        body = await request.json()
        self.polls.append(body)
        self.headers.append(dict(request.headers))
        if self.stale:
            return web.json_response({"ret": -14, "errcode": -14, "errmsg": "session timeout"})
        msgs = []
        try:
            msgs.append(await asyncio.wait_for(self.inbox.get(), 0.3))
            while not self.inbox.empty():
                msgs.append(self.inbox.get_nowait())
        except asyncio.TimeoutError:
            pass
        return web.json_response({"ret": 0, "msgs": msgs, "get_updates_buf": f"buf{len(self.polls)}", "longpolling_timeout_ms": 500})

    async def send(self, request):
        self.sent.append(await request.json())
        self.headers.append(dict(request.headers))
        return web.json_response({"ret": 0})

    def push(self, text, sender=USER, mtype=1, mid=None, items=None):
        self.inbox.put_nowait({"message_id": mid or f"m{self.inbox.qsize()}-{len(self.polls)}", "from_user_id": sender,
                               "to_user_id": "bot1", "message_type": mtype, "create_time_ms": 1793000000000,
                               "item_list": items or [{"type": 1, "text_item": {"text": text}}], "context_token": f"ctx-{text}"})

    async def start(self):
        app = web.Application()
        app.router.add_post("/ilink/bot/get_bot_qrcode", self.qrcode)
        app.router.add_get("/ilink/bot/get_qrcode_status", self.status)
        app.router.add_post("/ilink/bot/getupdates", self.updates)
        app.router.add_post("/ilink/bot/sendmessage", self.send)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url("")).rstrip("/")
        return self.base

    async def close(self):
        await self.server.close()


def bind(home, base):
    """不扫码, 直接放一个绑好的账号 (token 照样 DPAPI 加密)."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "account.json").write_text(json.dumps({"bot_id": "bot1", "user_id": USER, "base_url": base,
                                                   "token": X.protect(TOKEN), "bound_at": "2026-10-26 04:00:00"}), encoding="utf-8")


def test_scan_login_saves_encrypted_token_and_receives(tmp_path):
    async def main():
        fake = FakeILink()
        base = await fake.start()
        got = []

        async def on_message(m):
            got.append(m)

        wx = X.WeixinClient(tmp_path / "wx", base, on_message=on_message)
        try:
            await wx.begin_login()
            await until(lambda: wx.login.get("state") == "wait", 5)
            svg = wx.status()["login"]["svg"]
            await until(lambda: wx.login.get("state") == "confirmed", 10)
            await until(lambda: wx.online, 5)
            fake.push("你好呀")
            fake.push("别人的消息", sender="someone@im.wechat")
            fake.push("她自己的回显", mtype=2)
            await until(lambda: len(got) == 1, 5)
            await wx.send_text("在呢")
            await until(lambda: (tmp_path / "wx" / "sync.txt").read_text(encoding="utf-8").startswith("buf"), 5)
            return svg, got, fake, wx.status(), wx.context_token
        finally:
            await wx.close()
            await fake.close()

    svg, got, fake, st, ctx = asyncio.run(main())
    assert svg.startswith("<svg") and "path" in svg
    saved = (tmp_path / "wx" / "account.json").read_text(encoding="utf-8")
    assert TOKEN not in saved and X.unprotect(json.loads(saved)["token"]) == TOKEN  # 加密存盘
    assert got[0]["text"] == "你好呀" and ctx == "ctx-你好呀" and st["ignored"] == 2 and st["received"] == 1
    out = fake.sent[0]["msg"]
    assert out["to_user_id"] == USER and out["message_type"] == 2 and out["message_state"] == 2
    assert out["item_list"] == [{"type": 1, "text_item": {"text": "在呢"}}] and out["context_token"] == "ctx-你好呀"
    assert fake.sent[0]["base_info"]["bot_agent"].startswith("WorldModel/")
    h = fake.headers[-1]
    assert h["Authorization"] == f"Bearer {TOKEN}" and h["AuthorizationType"] == "ilink_bot_token" and h["iLink-App-Id"] == "bot"
    assert "Authorization" not in fake.headers[0]  # 拿二维码时还没有 token
    assert fake.polls[1]["get_updates_buf"] == "buf1"  # 游标接着用
    assert TOKEN not in json.dumps(st) and USER not in json.dumps(st)


def test_expired_qr_is_replaced_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "QR_MAX", 2)

    async def main():
        fake = FakeILink(statuses=("expired",))
        base = await fake.start()
        wx = X.WeixinClient(tmp_path / "wx", base)
        try:
            await wx.begin_login()
            await until(lambda: wx.login.get("state") == "expired", 10)
            return fake.qr_count, wx.status()
        finally:
            await wx.close()
            await fake.close()

    n, st = asyncio.run(main())
    assert n == 2 and "再点一次" in st["login"]["error"] and not st["bound"]


def test_stale_token_pauses_and_logout_forgets(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "PAUSE_S", 0.2)

    async def main():
        fake = FakeILink(stale=True)
        base = await fake.start()
        bind(tmp_path / "wx", base)
        wx = X.WeixinClient(tmp_path / "wx", base)
        try:
            wx.start()
            await until(lambda: wx.state == "expired", 5)
            st = wx.status()
            await wx.logout()
            return st, wx.status()
        finally:
            await wx.close()
            await fake.close()

    st, after = asyncio.run(main())
    assert "重新扫码" in st["error"] and st["bound"]
    assert not after["bound"] and after["state"] == "off" and not (tmp_path / "wx" / "account.json").exists()


def test_only_one_poller_and_not_on_test_servers(tmp_path):
    """同一个号同时只能一处收 (poll.lock); 没开记忆库的服务端 (allowed=False) 不收."""
    async def main():
        fake = FakeILink()
        base = await fake.start()
        bind(tmp_path / "wx", base)
        a, b, c = (X.WeixinClient(tmp_path / "wx", base) for _ in range(3))
        c.allowed = False
        try:
            a.start()
            b.start()
            c.start()
            await until(lambda: a.online, 5)
            sb, sc = b.status(), c.status()
            await a.stop()
            b.start()  # a 放手以后 b 就能收了
            await until(lambda: b.online, 5)
            return sb, sc
        finally:
            for x in (a, b, c):
                await x.close()
            await fake.close()

    sb, sc = asyncio.run(main())
    assert sb["state"] == "off" and "只能一处收" in sb["error"]
    assert sc["state"] == "off" and "记忆库" in sc["error"]


def test_base_url_guard():
    assert X.base_ok("https://ilinkai.weixin.qq.com") and X.base_ok("http://127.0.0.1:1234")
    assert not X.base_ok("http://ilinkai.weixin.qq.com") and not X.base_ok("https://evil.example.com")
    with pytest.raises(ValueError):
        X.WeixinClient(X.HOME, "https://evil.example.com")


def test_manager_weixin_is_owner_and_replies_there(tmp_path):
    """你在微信上说一句: 她用主人档回 (提示里有你的记忆和钱包那些), 回到微信, 不发 QQ; 她要跟你说的事也先发微信;
    你后来在 QQ 上说话了, 再要说的事就发 QQ."""
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base)})
        await inbox.get("channel_config_done")
        ch = bot.app[ENGINES].channels
        fake = FakeILink()
        wbase = await fake.start()
        bind(X.HOME, wbase)
        ch.weixin = X.WeixinClient(X.HOME, wbase, on_message=ch._on_weixin)
        try:
            ch.weixin.start()
            await until(lambda: ch.weixin.online, 5)
            qq_before = len(bot.sent)
            fake.push("今天好累呀")
            await until(lambda: len(fake.sent) == 1, 10)
            told = await ch._tell_owner("我学会听语音啦")  # 你刚在微信上找过她: 发微信
            await until(lambda: len(fake.sent) == 2, 5)
            qq_mid = len(bot.sent)
            await bot.push(pmsg(OWNER, "我换到 QQ 上了", 9))  # 你后来在 QQ 上说话: 她回 QQ, 再要说的事也发 QQ
            await until(lambda: len(bot.sent) > qq_mid, 10)
            n = len(bot.sent)
            told2 = await ch._tell_owner("第二件事")
            return fake, cloud.requests, qq_before, qq_mid, n, len(bot.sent), told and told2, ch.status()["weixin"]
        finally:
            await ch.weixin.close()
            await fake.close()

    fake, reqs, qq_before, qq_mid, n, qq_end, told, st = run_qq(
        tmp_path, scenario, cloud_script=[say(reply_json(f"第{i}句")) for i in range(10)])
    texts = [m["msg"]["item_list"][0]["text_item"]["text"] for m in fake.sent]
    assert texts[0].startswith("第") and texts[1] == "我学会听语音啦" and told and len(texts) == 2
    assert qq_mid == qq_before          # 微信那段: QQ 上一句都没发
    assert qq_end == n + 1              # 换到 QQ 以后: "第二件事" 发在 QQ 上
    prompt = next(r for r in reqs if "今天好累呀" in json.dumps(r["body"], ensure_ascii=False))["body"]["messages"]
    last = prompt[-1]["content"]
    assert "主人(微信)" in last and "[你会的本事]" in last
    assert st["bound"] and st["state"] == "online" and st["received"] == 1


def test_manager_weixin_owner_says_become_candidates(tmp_path):
    """微信上你打的"我叫…": 和 QQ 私聊一样只进主人记忆的候选 (来源 微信), 关于用户.md 不变; 微信转好的语音文字不算 (只认你打的字)."""
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base)})
        await inbox.get("channel_config_done")
        ch = bot.app[ENGINES].channels
        fake = FakeILink()
        wbase = await fake.start()
        bind(X.HOME, wbase)
        ch.weixin = X.WeixinClient(X.HOME, wbase, on_message=ch._on_weixin)
        mem = ch.e.memory.dir / "记忆"  # 服务端起来时 小流/ 已经搬成 绫音/
        facts = (mem / "关于用户.md").read_bytes()
        try:
            ch.weixin.start()
            await until(lambda: ch.weixin.online, 5)
            fake.push("", items=[{"type": X.ITEM_TEXT, "text_item": {"text": "我叫小绿"}},
                                 {"type": X.ITEM_VOICE, "voice_item": {"text": "我喜欢吃香菜"}}])
            cand = mem / "候选.md"
            await until(lambda: cand.exists() and "用户叫小绿" in cand.read_text(encoding="utf-8"), 10)
            await until(lambda: len(fake.sent) == 1, 10)  # 她照常回你
            return facts, (mem / "关于用户.md").read_bytes(), cand.read_text(encoding="utf-8")
        finally:
            await ch.weixin.close()
            await fake.close()

    before, after, cand = run_qq(tmp_path, scenario, vault=True, cloud_script=[say(reply_json(f"第{i}句")) for i in range(4)])
    assert before == after
    assert "- [ ] 用户叫小绿 (来源: 微信 [[" in cand and "#私聊" in cand
    assert "香菜" not in cand  # 语音转的字不算


def test_api_weixin_login_and_origin(tmp_path):
    async def main():
        fake = FakeILink(statuses=("wait",))
        base = await fake.start()
        client = TestClient(TestServer(create_app(make_cfg(tmp_path, f"channels.weixin.base_url={json.dumps(base)}"))))
        await client.start_server()
        try:
            bad = await client.post("/api/weixin", json={"action": "login"}, headers={"Origin": "https://evil.example.com"})
            r0 = await (await client.get("/api/weixin")).json()
            await client.post("/api/weixin", json={"action": "login"})
            await until(lambda: fake.qr_count == 1, 5)
            await asyncio.sleep(0.2)
            r1 = await (await client.get("/api/weixin")).json()
            wrong = await client.post("/api/weixin", json={"action": "code", "code": "123"})
            await client.post("/api/weixin", json={"action": "cancel"})
            r2 = await (await client.get("/api/weixin")).json()
            nope = await client.post("/api/weixin", json={"action": "boom"})
            return bad.status, r0, r1, wrong.status, r2, nope.status
        finally:
            await client.close()
            await fake.close()

    bad, r0, r1, wrong, r2, nope = asyncio.run(main())
    assert bad == 403 and r0["bound"] is False and r0["login"]["state"] == "idle"
    assert r1["login"]["state"] == "wait" and r1["login"]["svg"].startswith("<svg") and "qrcode" not in r1["login"]
    assert wrong == 400 and r2["login"]["state"] == "idle" and nope == 400
