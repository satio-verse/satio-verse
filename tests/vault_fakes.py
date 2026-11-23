"""账号保管箱 / QQ 自动挂接 / B 站扫码登录 的替身服务 (测试和 tools/vault_preview.py 共用). 不连真的 SnowLuma、QQ、B 站,
不用任何真账号密码; 一切都在本机的临时端口上."""
from __future__ import annotations

import asyncio
import time
from aiohttp import web


def qq_proc(pid: int, uin: str | int | None = None, injected: bool = False, nickname: str = "绫音") -> dict:
    """SnowLuma 管理页 /api/processes 里的一项 (字段照 2.2.0 的 toInfo + account)."""
    return {"pid": pid, "name": "QQ.exe", "path": "", "injected": injected, "connected": injected, "loggedIn": injected and bool(uin),
            "uin": str(uin) if (injected and uin) else None, "status": "connected" if injected else "unloaded", "error": None,
            "method": "loadModuleManual" if injected else None,
            "account": {"uin": str(uin), "nickname": nickname} if uin else None}


class FakeSnowLuma:
    """SnowLuma 2.2.0 的管理页: /api/login (5 次输错锁 429)、/api/ui/public、/api/processes、load / refresh / logout.
    onebot_up: 挂接成功以后 OneBot 的端口就开了 (测试里让 snowluma._listening 看它)."""

    def __init__(self, password: str = "correct-horse-battery", procs: list[dict] | None = None):
        self.password = password
        self.procs = procs if procs is not None else []
        self.tokens: set[str] = set()
        self.bad = 0
        self.totp = False
        self.consent_pending = False
        self.must_change = False
        self.load_fails = False
        self.calls: list[tuple[str, str]] = []
        self.logins = self.logouts = 0
        self.onebot_up = False
        self.opens_port = True   # 挂接成功以后 OneBot 的端口会不会开 (False: 模拟"挂上了可端口没开")
        self.loads: list[int] = []
        self.refreshes: list[int] = []
        self.last_body: dict | None = None

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._mw])
        app.router.add_post("/api/login", self._login)
        app.router.add_get("/api/ui/public", self._public)
        app.router.add_get("/api/processes", self._list)
        app.router.add_post("/api/processes/{pid}/load", self._load)
        app.router.add_post("/api/processes/{pid}/refresh", self._refresh)
        app.router.add_post("/api/logout", self._logout)
        return app

    @web.middleware
    async def _mw(self, request: web.Request, handler):
        self.calls.append((request.method, request.path))
        if request.path in ("/api/login", "/api/ui/public"):
            return await handler(request)
        token = request.headers.get("x-snowluma-token") or ""
        if token not in self.tokens:
            return web.json_response({"status": "failed", "message": "Token expired or invalid"}, status=401)
        if self.consent_pending and request.path not in ("/api/logout",):
            return web.json_response({"status": "failed", "message": "请先阅读并同意用户协议与隐私政策", "consentRequired": True}, status=403)
        if self.must_change and request.path not in ("/api/logout",):
            return web.json_response({"status": "failed", "message": "请先修改密码", "mustChangePassword": True}, status=403)
        return await handler(request)

    async def _public(self, request):
        return web.json_response({"appearance": {"mode": "system"}})

    async def _login(self, request):
        if "json" not in request.headers.get("Content-Type", ""):
            return web.json_response({"success": False, "message": "Content-Type 必须为 application/json"}, status=415)
        if self.bad >= 5:
            return web.json_response({"success": False, "message": "登录尝试过多，请 897 秒后重试"}, status=429)
        body = await request.json()
        self.last_body = {k: ("<pw>" if k == "password" else v) for k, v in body.items()}
        if body.get("password") != self.password:
            self.bad += 1
            return web.json_response({"success": False, "message": "密码错误"}, status=401)
        if self.totp:
            return web.json_response({"success": False, "needsTotp": True})
        self.bad = 0
        self.logins += 1
        token = f"tok-{self.logins}-{time.time_ns()}"
        self.tokens.add(token)
        return web.json_response({"success": True, "token": token, "mustChangePassword": self.must_change})

    async def _list(self, request):
        return web.json_response({"list": [dict(p) for p in self.procs]})

    def _find(self, request):
        pid = int(request.match_info["pid"])
        return next((p for p in self.procs if p["pid"] == pid), None)

    async def _load(self, request):
        p = self._find(request)
        if p is None or self.load_fails:
            return web.json_response({"success": False, "message": "操作失败，请检查服务器日志"}, status=500)
        uin = (p.get("account") or {}).get("uin")
        p.update(injected=True, connected=True, loggedIn=bool(uin), uin=uin, status="connected", method="loadModuleManual")
        self.loads.append(p["pid"])
        self.onebot_up = self.opens_port
        return web.json_response({"success": True, "process": dict(p)})

    async def _refresh(self, request):
        p = self._find(request)
        if p is None:
            return web.json_response({"success": False, "message": "操作失败，请检查服务器日志"}, status=500)
        p.update(connected=True, status="connected")
        self.refreshes.append(p["pid"])
        self.onebot_up = self.opens_port
        return web.json_response({"success": True, "process": dict(p)})

    async def _logout(self, request):
        self.tokens.discard(request.headers.get("x-snowluma-token") or "")
        self.logouts += 1
        return web.json_response({"success": True})


class FakeBili:
    """B 站的 passport (扫码登录) 和 api (spi、nav) 合在一个本机服务上. script: poll 依次回的状态码 (最后一个一直回)."""

    def __init__(self, script=(86101, 86090, 0), mid: str = "20261107", uname: str = "绫音的小号"):
        self.script = list(script)
        self.polls = 0
        self.mid, self.uname = mid, uname
        self.sess = "SESSDATA-ABC%2C1790000000%2Cxyz*c1"
        self.jct = "JCT-1234567890abcdef"
        self.valid = True
        self.calls: list[str] = []
        self.set_cookie_headers = True
        self.nav_cookie: str | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/x/passport-login/web/qrcode/generate", self._gen)
        app.router.add_get("/x/passport-login/web/qrcode/poll", self._poll)
        app.router.add_get("/x/frontend/finger/spi", self._spi)
        app.router.add_get("/x/web-interface/nav", self._nav)
        return app

    async def _gen(self, request):
        self.calls.append("generate")
        self.polls = 0
        return web.json_response({"code": 0, "message": "0", "ttl": 1, "data": {
            "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?navhide=1&callback=close&qrcode_key=QK123&from=",
            "qrcode_key": "QK123"}})

    async def _poll(self, request):
        self.calls.append("poll")
        if request.query.get("qrcode_key") != "QK123":
            return web.json_response({"code": 0, "data": {"code": 86038, "message": "二维码已失效", "url": "", "refresh_token": ""}})
        code = self.script[min(self.polls, len(self.script) - 1)]
        self.polls += 1
        data = {"url": "", "refresh_token": "", "timestamp": 0, "code": code, "message": ""}
        headers = {}
        if code == 0:
            data["url"] = (f"https://passport.biligame.com/x/passport-login/web/crossDomain?DedeUserID={self.mid}"
                           f"&DedeUserID__ckMd5=ckmd5abc&Expires=1805000000&SESSDATA={self.sess}&bili_jct={self.jct}&gourl=https%3A%2F%2Fwww.bilibili.com")
            data["refresh_token"] = "REFRESH-TOKEN-xyz"
        resp = web.json_response({"code": 0, "message": "0", "ttl": 1, "data": data}, headers=headers)
        if code == 0 and self.set_cookie_headers:
            resp.headers.add("Set-Cookie", f"SESSDATA={self.sess}; Path=/; Domain=bilibili.com; Expires=Fri, 01 Jan 2027 00:00:00 GMT; HttpOnly; Secure")
            resp.headers.add("Set-Cookie", f"bili_jct={self.jct}; Path=/; Domain=bilibili.com; Max-Age=15551000")
            resp.headers.add("Set-Cookie", f"DedeUserID={self.mid}; Path=/; Domain=bilibili.com")
            resp.headers.add("Set-Cookie", "DedeUserID__ckMd5=ckmd5abc; Path=/; Domain=bilibili.com")
            resp.headers.add("Set-Cookie", "sid=sid12345; Path=/; Domain=bilibili.com")
        return resp

    async def _spi(self, request):
        self.calls.append("spi")
        return web.json_response({"code": 0, "data": {"b_3": "BUVID3-FAKE-0001infoc", "b_4": "BUVID4-FAKE-0002infoc"}})

    async def _nav(self, request):
        self.calls.append("nav")
        cookie = request.headers.get("Cookie", "")
        self.nav_cookie = cookie
        ok = self.valid and f"SESSDATA={self.sess}" in cookie
        return web.json_response({"code": 0 if ok else -101, "data": {"isLogin": ok, "mid": int(self.mid) if ok else 0, "uname": self.uname if ok else ""}})


async def serve(app: web.Application, port: int = 0) -> tuple[web.AppRunner, int]:
    """把一个替身服务起在 127.0.0.1 的一个端口上 (0 = 随便一个). 返回 (runner, 端口); 用完 await runner.cleanup()."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]
