"""账号保管箱的本机接口 /api/vault 和页面 /vault (app.py 调 register; 存储和规矩见 vault.py).
和 /mail 一样只认本机页面 (查 Host 和 Origin, 和 /ws 一样); 页面是服务端自己的一页, 桌宠和工作台里放个入口打开它就行.

  GET  /vault                                     页面 (vault_page.html)
  GET  /api/vault[?log=N][&qr=1]                  状态: 账号的公开视图 (没有密码、令牌、Cookie, 账号名打了码)、各平台说明、QQ 挂接的状态、
                                                  B 站扫码的进度、其他已连接的东西 (邮箱、微信)、最近 N 条使用记录; qr=1 再带上
                                                  QQ 登录窗口上读到的二维码 (重新画的, data URL)
  POST (要带请求头 X-WM-Page: vault, 浏览器跨站的网页带不上; 只是挡误碰, 防不了本机上故意的程序)
    {"action": "save", platform, label?, user?, fields: {…}, purposes?: {用途: true/false}}   存 / 换一个账号的密码 (只进不出)
    {"action": "forget", id}                      删掉一个账号
    {"action": "purpose", id, purpose, state}     改一个用途 (allow 允许 / ask 先问我 / deny 不允许)
    {"action": "qq_attach"}                       现在就替她挂接一次 QQ (跳过等待; 被管理页拒过的密码要先换新的)
    {"action": "qq_qr"}                           重新看一眼 QQ 登录窗口上的二维码
    {"action": "bili_begin" | "bili_poll" | "bili_cancel" | "bili_check"}   B 站扫码登录 (出二维码 / 看进度 / 取消 / 检查还有效不)
    {"action": "bili_confirm", replace}           扫出来的是另一个号时: 换 (true) 还是不换 (false)
接口里没有"把密码取出来"这一项. 页面和接口在任何服务端上都开着, 保管箱目录默认是 ~/.worldmodel/vault (自检、试跑起的服务端
要避开真账号就配 vault.home); 替她挂接 QQ 不在这里, 在 ChannelManager 的 snowluma 启动器里, 跟着 channels.qq.enabled 走
(自检起的服务端都是关着 QQ 的, 不会去碰真的 SnowLuma).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

from . import vault as V
from .bili_login import BiliLogin, BiliLoginError

log = logging.getLogger("wm.channels.vault_http")
KEY = web.AppKey("vault", V.Vault)
LOGIN_KEY = web.AppKey("vault_bili_login", BiliLogin)
PAGE = Path(__file__).with_name("vault_page.html")
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
PAGE_HEADER = "X-WM-Page"
BILI_COOKIE = "~/.worldmodel/bili_cookie.txt"


def _platforms() -> list[dict]:
    return [{"id": p.id, "name": p.name, "kind": p.kind, "login": p.login, "howto": p.howto, "single": p.single,
             "secrets": [{"name": n, "label": lbl} for n, lbl in p.secrets],
             "purposes": [{"id": u.id, "name": u.name, "desc": u.desc, "risk": u.risk, "ui_on": u.ui_on} for u in p.purposes]}
            for p in V.PLATFORMS.values()]


def register(app: web.Application, cfg: dict, refusal, engines=None, agent=None) -> V.Vault:
    """挂 /vault 和 /api/vault. refusal(request): 来源不对时返回原因 (app.py 的 ws_refusal); engines.channels 是渠道管理器
    (保管箱、SnowLuma 的挂接都在它手里); agent: 工作台 (往它的黑名单里加保管箱目录)."""
    conf = cfg.get("vault") or {}
    ch = getattr(engines, "channels", None)
    port = (cfg.get("server") or {}).get("port")
    if port:
        with contextlib.suppress(TypeError, ValueError):
            V.PAGE_URL = f"http://127.0.0.1:{int(port)}/vault"
    vault = getattr(ch, "vault", None) or V.Vault(Path(str(conf["home"])).expanduser() if conf.get("home") else V.HOME)
    app[KEY] = vault

    def cookie_file() -> Path:
        path = ((getattr(ch, "conf", None) or {}).get("bili_cookie_file")) or BILI_COOKIE
        return Path(str(path)).expanduser()

    login = BiliLogin(vault, cookie_file, passport=str(conf.get("bili_passport") or "https://passport.bilibili.com"),
                      api=str(conf.get("bili_api") or "https://api.bilibili.com"))
    app[LOGIN_KEY] = login
    tasks: set[asyncio.Task] = set()

    def launcher():
        return getattr(ch, "snowluma", None)

    def qq_view() -> dict:
        sl = launcher()
        conf_ = getattr(ch, "conf", None) or {}
        return {"enabled": bool(conf_.get("enabled")), "self_id": conf_.get("self_id"), "state": getattr(ch, "state", ""),
                "error": getattr(ch, "error", None), "autostart": conf_.get("snowluma_autostart", True) is not False,
                "attach": conf_.get("snowluma_attach", True) is not False,
                "snowluma": sl.status() if sl is not None else None}

    def others(request: web.Request) -> dict:
        out: dict = {}
        with contextlib.suppress(Exception):
            from . import mailbox_http
            box = request.app.get(mailbox_http.KEY)
            if box is not None:
                st = box.status()
                out["mail"] = {"count": len(st.get("accounts") or []), "here": bool(st.get("here")), "url": "/mail"}
        with contextlib.suppress(Exception):
            wx = ch.weixin.status()
            out["weixin"] = {"bound": bool(wx.get("bound")), "state": wx.get("state"), "user": wx.get("user")}
        return out

    def status(request: web.Request, log_n: int = 0) -> dict:
        out = {"ok": True, "now": time.time(),
               "utc_offset_min": int((datetime.now().astimezone().utcoffset() or timedelta()).total_seconds() // 60),
               "accounts": vault.accounts(), "platforms": _platforms(), "page_url": V.PAGE_URL,
               "protected": V.protected(getattr(agent, "blacklist", None), vault.home) if getattr(agent, "blacklist", None) is not None else None,
               "qq": qq_view(), "bili": login.view(), "bili_file": _marker_state(cookie_file()), "others": others(request),
               "max_accounts": V.MAX_ACCOUNTS}
        if log_n:
            out["log"] = list(reversed(vault.log_tail(log_n)))
        return out

    async def page(request: web.Request) -> web.Response:
        if (reason := refusal(request)) is not None:
            raise web.HTTPForbidden(text=reason)
        return web.Response(body=await asyncio.to_thread(PAGE.read_bytes), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store", "Content-Security-Policy": _CSP,
                                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    def kick_attach() -> None:
        """存了 SnowLuma 的密码 / 点了"现在检查": 不等下一轮重连, 马上替她挂接一次."""
        sl, conf_ = launcher(), getattr(ch, "conf", None)
        if sl is None or not conf_ or not conf_.get("enabled"):
            return
        task = asyncio.ensure_future(sl.attach_now(conf_))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def api(request: web.Request) -> web.Response:
        if (reason := refusal(request)) is not None:
            raise web.HTTPForbidden(text=reason)
        if request.method == "GET":
            try:
                n = max(0, min(200, int(request.query.get("log") or 0)))
            except ValueError:
                n = 0
            out = await asyncio.to_thread(status, request, n)
            if request.query.get("qr") and launcher() is not None:
                png = launcher().qr_png()
                if png:
                    import base64
                    out["qq_qr"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            return web.json_response(out, headers={"Cache-Control": "no-store"})
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
        if request.headers.get(PAGE_HEADER) != "vault":
            return web.json_response({"ok": False, "error": "只能从保管箱页面操作"}, status=403)
        if (request.content_length or 0) > 64 << 10:
            raise web.HTTPRequestEntityTooLarge(max_size=64 << 10, actual_size=request.content_length)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "要 JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "要一个 JSON 对象"}, status=400)
        action = body.get("action")
        try:
            if action == "save":  # 密码只在这里经过: 不放进返回、不记日志, 用完就丢
                fields = body.get("fields")
                if not isinstance(fields, dict):
                    raise ValueError("fields 要是对象")
                purposes = body.get("purposes") if isinstance(body.get("purposes"), dict) else None
                platform = str(body.get("platform") or "")
                plat = V.PLATFORMS.get(platform)
                if plat is None or plat.login == "scan":
                    raise ValueError("这个平台不用手填，走扫码登录" if plat is not None else "不认识这个平台")
                acct = await asyncio.to_thread(
                    vault.put, platform, {k: str(v) for k, v in fields.items()}, label=str(body.get("label") or ""),
                    user=str(body.get("user") or ""), purposes=purposes, by="page", aid=None if not plat.single else plat.id)
                note = await _protect(vault, agent)
                if platform == "snowluma":
                    kick_attach()
                return web.json_response({"ok": True, "account": acct, "protect_note": note})
            if action == "forget":
                return web.json_response({"ok": vault.forget(str(body.get("id") or ""))})
            if action == "purpose":
                acct = vault.set_purpose(str(body.get("id") or ""), str(body.get("purpose") or ""), str(body.get("state") or ""))
                if acct["platform"] == "snowluma" and body.get("state") == "allow":
                    kick_attach()
                return web.json_response({"ok": True, "account": acct})
            if action == "qq_attach":
                if launcher() is None or not getattr(ch, "conf", None) or not ch.conf.get("enabled"):
                    raise ValueError("QQ 渠道没开 (外部渠道页里的 QQ 开关), 没东西可挂接")
                await launcher().attach_now(ch.conf)
                return web.json_response({"ok": True, "qq": qq_view()})
            if action == "qq_qr":
                if launcher() is None:
                    raise ValueError("QQ 渠道没开")
                seen = await launcher().refresh_qr()
                return web.json_response({"ok": True, "seen": seen, "qq": qq_view()})
            if action == "bili_begin":
                return web.json_response({"ok": True, "login": await login.begin()})
            if action == "bili_poll":
                return web.json_response({"ok": True, "login": await login.poll()})
            if action == "bili_cancel":
                return web.json_response({"ok": True, "login": login.cancel()})
            if action == "bili_confirm":
                return web.json_response({"ok": True, "login": await login.confirm(body.get("replace") is True)})
            if action == "bili_check":
                return web.json_response(await login.check())
            raise ValueError("action 只认 save / forget / purpose / qq_attach / qq_qr / bili_begin / bili_poll / bili_cancel / bili_confirm / bili_check")
        except (ValueError, V.VaultError, BiliLoginError) as exc:
            return web.json_response({"ok": False, "error": getattr(exc, "text", None) or str(exc),
                                      **({"kind": exc.kind} if hasattr(exc, "kind") else {})}, status=400)
        finally:
            body.pop("fields", None)

    async def startup(_app):
        with contextlib.suppress(Exception):
            await _protect(vault, agent)

    async def cleanup(_app):
        for task in list(tasks):
            task.cancel()
        for task in list(tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    app.router.add_route("*", "/api/vault", api)
    app.router.add_get("/vault", page)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return vault


async def _protect(vault: V.Vault, agent) -> str:
    """保管箱目录和 /api/vault 加进工作台的黑名单 (已经在的、你停用 / 删掉过的不再加). 返回一句说明."""
    store = getattr(agent, "blacklist", None)
    if store is None:
        return ""
    try:
        _ok, note = await asyncio.to_thread(V.protect, store, vault.home)
        return note
    except (OSError, ValueError) as exc:
        log.warning("保管箱没加进工作台的保护名单: %s", type(exc).__name__)
        return "保管箱没加进工作台的保护名单"


def _marker_state(path: Path) -> str:
    """bili_cookie.txt 现在是什么: marker (记号, Cookie 在保管箱里) / plain (旧的明文 Cookie) / none (没有)."""
    from .bili import is_vault_marker
    try:
        text = path.read_bytes()[:8192].decode("utf-8-sig", errors="replace")
    except OSError:
        return "none"
    return "marker" if is_vault_marker(text) else ("plain" if text.strip() else "none")
