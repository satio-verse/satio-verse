"""微信发图 (wm/channels/weixin.py 的 send_image, 照腾讯插件的写法) 和 AES-128-ECB (wm/channels/aes_ecb.py):
用本地假的 iLink 和假的 CDN, 不连腾讯. 注意: 假服务是照插件源码的写法搭的, 没有对真的微信验证过."""
import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from test_weixin import TOKEN, USER, bind
from wm.channels import aes_ecb as A
from wm.channels import weixin as X
from wm.channels.vault_bridge import Bridge

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3 + b"IEND"


def test_aes_ecb_matches_the_standard_vectors():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")  # FIPS-197 附录 C.1
    assert A.encrypt(key, bytes.fromhex("00112233445566778899aabbccddeeff"))[:16].hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    k2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")  # NIST SP 800-38A F.1.1 (ECB-AES128)
    pt = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a" "ae2d8a571e03ac9c9eb76fac45af8e51"
                       "30c81c46a35ce411e5fbc1191a0a52ef" "f69f2445df4f9b17ad2b417be66c3710")
    want = ("3ad77bb40d7a3660a89ecaf32466ef97" "f5d3d58503b9699de785895a96fdbaaf"
            "43b1cd7f598ece23881b00e3ed030688" "7b0c785e27e8ad3f8223207104725dd4")
    got = A.encrypt(k2, pt)
    assert got[:64].hex() == want and len(got) == 80  # 正好 16 的倍数时再补一整块
    # 和 .NET 的 Aes (ECB + PKCS7) 对过: key = 01..10, 明文 "hello world, this is a test of ecb!"
    assert A.encrypt(bytes(range(1, 17)), b"hello world, this is a test of ecb!").hex() == (
        "2f93f09b469954500b375fb43957efdfbccfabc0ff575a05f19efca8b2becd21e06c8434c92a51ad97286daf0841f8f2")
    assert [A.padded_size(n) for n in (0, 1, 15, 16, 17, 32)] == [16, 16, 16, 32, 32, 48]
    assert len(A.encrypt(key, b"")) == 16
    with pytest.raises(ValueError):
        A.encrypt(b"short", b"x")


class FakeCloud:
    """假的 iLink (getuploadurl、sendmessage) + 假的 CDN (/c2c/upload)."""

    def __init__(self, full_url=False, cdn_status=200, cdn_header=True, upload_code=0, send_code=0, evil_url=False):
        self.full_url, self.cdn_status, self.cdn_header, self.upload_code, self.send_code, self.evil_url = \
            full_url, cdn_status, cdn_header, upload_code, send_code, evil_url
        self.upload_req: dict | None = None
        self.cdn: dict | None = None
        self.sent: list[dict] = []
        self.auth: list[str] = []
        self.server = None
        self.base = ""

    async def getuploadurl(self, request):
        self.auth.append(request.headers.get("Authorization", ""))
        self.upload_req = await request.json()
        if self.upload_code:
            return web.json_response({"ret": self.upload_code, "errmsg": "不让传"})
        if self.evil_url:
            return web.json_response({"ret": 0, "upload_full_url": "https://evil.example.com/steal"})
        if self.full_url:
            return web.json_response({"ret": 0, "upload_full_url": f"{self.base}/c2c/upload?encrypted_query_param=FULL&filekey=zz"})
        return web.json_response({"ret": 0, "upload_param": "UP/PARAM+=&x"})

    async def cdn_upload(self, request):
        self.cdn = {"query": dict(request.query), "body": await request.read(), "ctype": request.headers.get("Content-Type"),
                    "auth": request.headers.get("Authorization")}
        headers = {"x-encrypted-param": "DOWNLOAD-PARAM"} if self.cdn_header else {}
        return web.Response(status=self.cdn_status, headers=headers, text="")

    async def sendmessage(self, request):
        self.auth.append(request.headers.get("Authorization", ""))
        self.sent.append(await request.json())
        return web.json_response({"ret": self.send_code, "errmsg": "x"} if self.send_code else {"ret": 0})

    async def start(self):
        app = web.Application()
        app.router.add_post("/ilink/bot/getuploadurl", self.getuploadurl)
        app.router.add_post("/c2c/upload", self.cdn_upload)
        app.router.add_post("/ilink/bot/sendmessage", self.sendmessage)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url("")).rstrip("/")
        return self.base

    async def close(self):
        await self.server.close()


def client(tmp_path, base):
    bind(tmp_path / "wx", base)
    wx = X.WeixinClient(tmp_path / "wx", base)
    wx.cdn_base = f"{base}/c2c"
    wx.context_token = "ctx-last-message"
    return wx


def test_image_goes_up_encrypted_then_is_sent_with_the_cdn_parameter(tmp_path):
    async def main():
        cloud = FakeCloud()
        base = await cloud.start()
        wx = client(tmp_path, base)
        try:
            await wx.send_image(PNG, caption="QQ 要扫码")
            return cloud, wx.sent
        finally:
            await wx.close()
            await cloud.close()
    cloud, sent = asyncio.run(main())
    req = cloud.upload_req
    key = bytes.fromhex(req["aeskey"])
    cipher = A.encrypt(key, PNG)
    assert len(req["aeskey"]) == 32 and len(req["filekey"]) == 32 and req["media_type"] == 1 and req["to_user_id"] == USER
    assert req["rawsize"] == len(PNG) and req["rawfilemd5"] == hashlib.md5(PNG).hexdigest() and req["filesize"] == len(cipher)
    assert req["no_need_thumb"] is True and req["base_info"]["channel_version"]
    assert cloud.cdn["query"] == {"encrypted_query_param": "UP/PARAM+=&x", "filekey": req["filekey"]}  # 参数编过码, 到那边原样
    assert cloud.cdn["body"] == cipher and PNG[:20] not in cloud.cdn["body"] and cloud.cdn["ctype"] == "application/octet-stream"
    assert cloud.cdn["auth"] is None  # CDN 不要 token
    caption, image = cloud.sent  # 说明文字照插件的做法先单独发一条
    assert caption["msg"]["item_list"][0]["text_item"]["text"] == "QQ 要扫码"
    msg = image["msg"]
    assert msg["to_user_id"] == USER and msg["message_type"] == 2 and msg["message_state"] == 2 and msg["context_token"] == "ctx-last-message"
    item = msg["item_list"][0]
    assert item["type"] == 2 and item["image_item"]["mid_size"] == len(cipher)
    media = item["image_item"]["media"]
    assert media["encrypt_query_param"] == "DOWNLOAD-PARAM" and media["encrypt_type"] == 1
    assert base64.b64decode(media["aes_key"]).decode("ascii") == req["aeskey"]  # 十六进制的串再 base64, 照插件
    assert all(a == f"Bearer {TOKEN}" for a in cloud.auth) and sent == 2
    assert TOKEN not in json.dumps([req, cloud.sent])


def test_server_given_full_url_is_used_but_only_for_tencent_or_this_computer(tmp_path):
    async def main():
        cloud = FakeCloud(full_url=True)
        base = await cloud.start()
        wx = client(tmp_path, base)
        try:
            await wx.send_image(PNG)
            ok = cloud.cdn["query"]
            cloud.full_url, cloud.evil_url, cloud.cdn = False, True, None
            with pytest.raises(X.WeixinError, match="上传地址不对"):
                await wx.send_image(PNG)
            return ok, cloud.cdn, len(cloud.sent)
        finally:
            await wx.close()
            await cloud.close()
    ok, cdn_after_evil, sent = asyncio.run(main())
    assert ok == {"encrypted_query_param": "FULL", "filekey": "zz"} and cdn_after_evil is None and sent == 1


@pytest.mark.parametrize("kwargs,match", [
    ({"upload_code": 5}, "不给上传地址"),
    ({"cdn_status": 500}, "CDN 没收"),
    ({"cdn_header": False}, "CDN 没收"),
    ({"send_code": -14}, "没收图"),
])
def test_failures_are_named_and_nothing_is_half_sent(tmp_path, kwargs, match):
    async def main():
        cloud = FakeCloud(**kwargs)
        base = await cloud.start()
        wx = client(tmp_path, base)
        try:
            with pytest.raises(X.WeixinError, match=match) as exc:
                await wx.send_image(PNG)
            return cloud, wx, exc.value
        finally:
            await wx.close()
            await cloud.close()
    cloud, wx, err = asyncio.run(main())
    if "send_code" not in kwargs:
        assert cloud.sent == []  # 图没传上去就不发消息
    else:
        assert wx.state == "expired" and err.code == -14
    assert TOKEN not in str(err)


def test_bad_input_and_unbound_client(tmp_path):
    async def main():
        wx = X.WeixinClient(tmp_path / "none", "http://127.0.0.1:9")
        with pytest.raises(X.WeixinError, match="还没绑"):
            await wx.send_image(PNG)
        wx = client(tmp_path, "http://127.0.0.1:9")
        for bad in (b"", None, b"x" * (X.IMAGE_MAX + 1)):
            with pytest.raises(X.WeixinError, match="空的或者太大"):
                await wx.send_image(bad)
        await wx.close()
    asyncio.run(main())


# -- 提醒主人: 能发图就发图, 发不出去退回文字 ---------------------------------------------------
class FakeManager:
    channel = "qq"

    def __init__(self, via="weixin", allow=True, image_fails=False):
        self.notices, self.told, self.images = [], [], []
        self.via, self.allow, self.image_fails = via, allow, image_fails
        self.weixin = SimpleNamespace(send_image=self._send_image)

    async def _send_image(self, png, caption=""):
        if self.image_fails:
            raise X.WeixinError("微信没收图")
        self.images.append((png, caption))

    def _owner_conv(self):
        return SimpleNamespace(key=("weixin", 0) if self.via == "weixin" else ("private", 1)) if self.via else None

    def _outbound_allowed(self, *a, **k):
        return self.allow

    async def notify(self, msg):
        self.notices.append(msg)

    async def _tell_owner(self, text):
        self.told.append(text)
        return True


@pytest.mark.parametrize("kwargs,sent_image", [
    ({}, True), ({"via": "qq"}, False), ({"via": None}, False), ({"allow": False}, False), ({"image_fails": True}, False)])
def test_qr_reminder_prefers_the_wechat_image_and_falls_back_to_text(kwargs, sent_image):
    async def main():
        mgr = FakeManager(**kwargs)
        bridge = Bridge(SimpleNamespace(on_want=None), mgr)
        assert await bridge.notice("qq_scan", "扫码", tell=True, image=PNG) is True
        return mgr
    mgr = asyncio.run(main())
    assert [n["kind"] for n in mgr.notices] == ["qq_scan"]  # 桌宠上总是提醒
    if sent_image:
        assert mgr.images == [(PNG, "扫码")] and mgr.told == []
    else:
        assert mgr.images == [] and mgr.told == ["扫码"]  # 图发不出去: 文字照发 (真的管理器里没有可发的会话时 _tell_owner 自己返回 False)
