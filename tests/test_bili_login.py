"""B 站扫码登录 (wm/channels/bili_login.py) 和 bili.py 从保管箱取 Cookie: 替身的 B 站 passport / api 在本机临时端口上,
不连真的 B 站、不用真的账号."""
import asyncio
import base64
import contextlib
import functools
import io
import json
import logging
from pathlib import Path

import pytest
from aiohttp import web

import vault_fakes as F
from wm.channels import bili
from wm.channels import bili_login as BL
from wm.channels import vault as V

LEGACY = "SESSDATA=dead-old-cookie; bili_jct=dead-jct; DedeUserID=20261107\n"


def async_test(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


@contextlib.asynccontextmanager
async def world(tmp_path, legacy: str | None = LEGACY, **fake_kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = F.FakeBili(**fake_kw)
    runner, port = await F.serve(fake.app())
    vault = V.Vault(tmp_path / "vault")
    path = tmp_path / "bili_cookie.txt"
    if legacy is not None:
        path.write_text(legacy, encoding="utf-8")
    login = BL.BiliLogin(vault, lambda: path, passport=f"http://127.0.0.1:{port}", api=f"http://127.0.0.1:{port}")
    bili.use_vault(vault)
    try:
        yield fake, vault, login, path
    finally:
        bili.use_vault(None)
        await runner.cleanup()


async def scan(login, polls=5):
    view = await login.begin()
    states = [view["state"]]
    for _ in range(polls):
        view = await login.poll()
        states.append(view["state"])
        if view["state"] not in ("waiting", "scanned"):
            break
    return states, view


@async_test
async def test_scan_login_stores_the_cookie_encrypted_and_leaves_a_marker(tmp_path):
    async with world(tmp_path) as (fake, vault, login, path):
        begin = await login.begin()
        assert begin["state"] == "waiting" and begin["expires_at"] > 0
        png = base64.b64decode(begin["qr"].split(",", 1)[1])
        assert png.startswith(b"\x89PNG")  # 二维码是服务端画好的图, 页面不拼 HTML
        view = None
        states = [begin["state"]]
        for _ in range(4):
            view = await login.poll()
            states.append(view["state"])
        assert states == ["waiting", "waiting", "scanned", "done", "done"]  # 没扫、扫了没确认、成了; 成了以后不再去问 B 站
        assert view["state"] == "done" and view["uname"] == "绫音的小号" and view["mid"] == "20261107"
        assert fake.calls.count("spi") == 1 and "nav" in fake.calls
        # 加密存着, 公开的东西里没有它
        acct = vault.get("bilibili")
        assert acct["meta"]["mid"] == "20261107" and acct["meta"]["saved_via"] == "scan"
        assert [p["state"] for p in acct["purposes"]] == ["allow", "ask"]  # 只读的算扫码点过头, 发评论要另外打开
        everything = json.dumps([acct, login.view(), vault.log_tail(20)], ensure_ascii=False) + "".join(
            p.read_text(encoding="utf-8", errors="replace") for p in vault.home.rglob("*.json*") if "legacy" not in p.parts)
        for secret in (fake.sess, fake.jct, "REFRESH-TOKEN-xyz", "BUVID3-FAKE", "dead-old-cookie"):
            assert secret not in everything, secret
        # 旧的明文文件换成记号, 原文件挪进保管箱留着
        assert BL.is_marker(path.read_text(encoding="utf-8")) and "SESSDATA" not in path.read_text(encoding="utf-8")
        kept = list((vault.home / "legacy").glob("bili_cookie.txt.*"))
        assert len(kept) == 1 and "dead-old-cookie" in kept[0].read_text(encoding="utf-8")
        assert "扫码登录" in path.read_text(encoding="utf-8") and "旧的明文 Cookie 文件已经换成记号" in view["text"]


@async_test
async def test_bili_reads_the_cookie_through_the_vault_by_purpose(tmp_path):
    async with world(tmp_path) as (fake, vault, login, path):
        await scan(login)
        # 读字幕、看消息: read 用途 (扫码时点过头)
        header = bili.read_cookie(path)
        assert header == f"SESSDATA={fake.sess}; buvid3=BUVID3-FAKE-0001infoc; buvid4=BUVID4-FAKE-0002infoc"
        # 发评论、点赞、回私信: write 用途, 没点头就拿不到, 并弹申请
        asked = []
        vault.on_want = lambda acct, purpose, by: asked.append((acct["id"], purpose["id"], by))
        assert bili.read_post_cookie(path) == ("", "") and asked == [("bilibili", "write", "bili")]
        vault.set_purpose("bilibili", "write", "allow", by="card")
        cookie, csrf = bili.read_post_cookie(path)
        assert csrf == fake.jct and cookie.startswith(f"SESSDATA={fake.sess}; bili_jct={fake.jct}") and "DedeUserID=20261107" in cookie
        vault.set_purpose("bilibili", "read", "deny")
        assert bili.read_cookie(path) == ""  # 收回以后就读不到
        log = [(r["purpose"], r["by"], r["ok"]) for r in vault.log_tail(30) if r["account"] == "bilibili" and r["kind"] == "use"]
        assert ("read", "bili", True) in log and ("write", "bili", True) in log and ("read", "bili", False) in log
        assert fake.sess not in json.dumps(vault.log_tail(50))


@async_test
async def test_plain_cookie_files_still_work_and_a_marker_without_a_vault_gives_nothing(tmp_path):
    async with world(tmp_path, legacy="SESSDATA=plain1; buvid3=b3\n") as (fake, vault, login, path):
        assert bili.read_cookie(path) == "SESSDATA=plain1; buvid3=b3"  # 老办法照样能用
        path.write_text(BL.marker_text(), encoding="utf-8")
        assert bili.read_cookie(path) == ""  # 记号文件但保管箱里还没有: 当没存
        bili.use_vault(None)
        assert bili.read_cookie(path) == "" and bili.read_post_cookie(path) == ("", "")


@async_test
async def test_a_different_account_is_confirmed_before_it_replaces_the_old_one(tmp_path):
    async with world(tmp_path, mid="777", uname="别人的号") as (fake, vault, login, path):  # 旧文件里是 20261107
        states, view = await scan(login)
        assert states[-1] == "confirm" and view["mid"] == "777" and view["old_mid"] == "20261107" and "不是以前存的那个号" in view["text"]
        assert vault.get("bilibili") is None and "dead-old-cookie" in path.read_text(encoding="utf-8")  # 还没动
        assert (await login.poll())["state"] == "confirm"
        kept = await login.confirm(False)
        assert kept["state"] == "idle" and vault.get("bilibili") is None
        states, view = await scan(login)
        assert states[-1] == "confirm"
        done = await login.confirm(True)
        assert done["state"] == "done" and vault.get("bilibili")["meta"]["mid"] == "777"
        with pytest.raises(BL.BiliLoginError):
            await login.confirm(True)
    # 保管箱里已经有一个号: 新扫出来的不一样也要问
    async with world(tmp_path / "again", legacy=None) as (fake, vault, login, path):
        await scan(login)
        fake.mid, fake.script = "888", [0]
        states, view = await scan(login)
        assert states[-1] == "confirm" and view["old_mid"] == "20261107"
        fake.mid = "20261107"  # 同一个号再扫 (比如过期了重新登录): 直接换成新的
        states, view = await scan(login)
        assert states[-1] == "done"


@async_test
async def test_expired_codes_and_risk_control_and_odd_replies(tmp_path):
    async with world(tmp_path, script=(86038,)) as (fake, vault, login, path):
        states, view = await scan(login)
        assert states[-1] == "expired" and "过期" in view["text"] and vault.get("bilibili") is None
        assert (await login.poll())["state"] == "expired"  # 没在等的时候 poll 不乱问 B 站
        n = fake.calls.count("poll")
        await login.poll()
        assert fake.calls.count("poll") == n
    async with world(tmp_path / "t", script=(86101,)) as (fake, vault, login, path):
        clock = [1000.0]
        login.clock = lambda: clock[0]
        await login.begin()
        clock[0] += BL.QR_TTL_S + 30  # 自己的钟也认过期 (B 站那边没回话也不一直等)
        assert (await login.poll())["state"] == "expired"
    async def risky(request):
        return web.json_response({"code": -412, "message": "请求被拦截"})

    async def generate(request):
        return web.json_response({"code": 0, "data": {"url": "https://x.test/q", "qrcode_key": "K"}})
    app = web.Application()
    app.router.add_get("/x/passport-login/web/qrcode/generate", generate)
    app.router.add_get("/x/passport-login/web/qrcode/poll", risky)
    runner, port = await F.serve(app)
    try:
        login = BL.BiliLogin(V.Vault(tmp_path / "v2"), lambda: tmp_path / "c.txt", passport=f"http://127.0.0.1:{port}", api=f"http://127.0.0.1:{port}")
        await login.begin()
        with pytest.raises(BL.BiliLoginError) as exc:
            await login.poll()
        assert exc.value.kind == "risk" and "风控" in exc.value.text
    finally:
        await runner.cleanup()


@async_test
async def test_login_that_nav_does_not_confirm_or_without_credentials_is_not_saved(tmp_path):
    async with world(tmp_path) as (fake, vault, login, path):
        fake.valid = False  # 扫码"成功"了但拿凭证去问 B 站说没登录
        with pytest.raises(BL.BiliLoginError) as exc:
            await scan(login)
        assert "没登录" in exc.value.text and vault.get("bilibili") is None and "dead-old-cookie" in path.read_text(encoding="utf-8")
    async with world(tmp_path / "n") as (fake, vault, login, path):
        fake.set_cookie_headers = False  # Set-Cookie 里没有: 回复里带的链接上有, 照样能登
        states, view = await scan(login)
        assert states[-1] == "done" and bili.read_cookie(path).startswith("SESSDATA=")
    async def bare(request):
        return web.json_response({"code": 0, "data": {"url": "", "refresh_token": "", "code": 0}})

    async def generate(request):
        return web.json_response({"code": 0, "data": {"url": "https://x.test/q", "qrcode_key": "K"}})
    app = web.Application()
    app.router.add_get("/x/passport-login/web/qrcode/generate", generate)
    app.router.add_get("/x/passport-login/web/qrcode/poll", bare)
    runner, port = await F.serve(app)
    try:
        login = BL.BiliLogin(V.Vault(tmp_path / "v3"), lambda: tmp_path / "c.txt", passport=f"http://127.0.0.1:{port}", api=f"http://127.0.0.1:{port}")
        await login.begin()
        with pytest.raises(BL.BiliLoginError) as exc:
            await login.poll()
        assert exc.value.kind == "protocol" and "没给登录凭证" in exc.value.text
    finally:
        await runner.cleanup()


@async_test
async def test_check_tells_a_valid_login_from_a_dead_one(tmp_path):
    async with world(tmp_path) as (fake, vault, login, path):
        assert (await login.check())["valid"] is False  # 还没登录
        await scan(login)
        ok = await login.check()
        assert ok["ok"] and ok["valid"] is True and vault.get("bilibili")["meta"]["verified"] is True
        assert fake.nav_cookie.startswith("SESSDATA=") and "refresh" not in fake.nav_cookie.lower()
        fake.valid = False
        dead = await login.check()
        assert dead["ok"] and dead["valid"] is False and "失效" in dead["text"]
        info = vault.get("bilibili")
        assert info["meta"]["verified"] is False and info["flags"]["expired_at"]
        fake.valid = True
        await scan(login)  # 重新登录: 过期的标记没了
        assert "expired_at" not in vault.get("bilibili")["flags"]


@async_test
async def test_only_bilibili_hosts_or_this_computer(tmp_path):
    v = V.Vault(tmp_path / "v")
    for bad in ("https://evil.example.com", "http://passport.bilibili.com", "https://bilibili.com.evil.com", "ftp://127.0.0.1"):
        with pytest.raises(ValueError):
            BL.BiliLogin(v, lambda: tmp_path / "c", passport=bad)
    BL.BiliLogin(v, lambda: tmp_path / "c", passport="https://passport.bilibili.com", api="https://api.bilibili.com")
    BL.BiliLogin(v, lambda: tmp_path / "c", passport="http://127.0.0.1:1234", api="http://localhost:1234")


@async_test
async def test_nothing_secret_reaches_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with world(tmp_path) as (fake, vault, login, path):
        await scan(login)
        bili.read_cookie(path)
        await login.check()
    text = caplog.text
    for secret in (fake.sess, fake.jct, "REFRESH-TOKEN-xyz", "BUVID3-FAKE", "dead-old-cookie"):
        assert secret not in text
