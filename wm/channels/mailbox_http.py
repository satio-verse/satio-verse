"""邮箱的本机接口 /api/mail、连接邮箱的页面 /mail 和挂接 (app.py 调 register; 读信、分拣见 mailbox.py, 汇报见
mailbox_report.py). 都只认本机页面 (查 Host 和 Origin, 和 /ws 一样).

  GET  /mail                                      连接邮箱的页面 (mailbox_page.html; 2026-11-01 用户: "能不能通过桌宠
                                                  (工作台) 进行登录、链接呢? … 不是人人都是懂电脑的"): 填地址、点按钮去生成
                                                  授权码、粘贴、连接; 看连了哪些、改怎么汇报、看读信记录. 不用开终端
  GET  /api/mail[?log=N][&providers=1]            状态 (不含授权码, 地址打码, 每个邮箱一个编号 id) + 最近 N 条读信记录;
                                                  providers=1 再带上各家邮箱的说明 (页面用)
  POST {"action": "connect", "address", "secret", "host"?, "port"?, "proxy"?}
                                                  加一个邮箱 / 重填授权码: 先只读地试登录, 成了才加密存下. 授权码只在这一步
                                                  经过 (页面上的密码框 -> 本机), 不回传、不记日志
  POST {"action": "test" | "forget", "id"}        用存好的授权码试登录一次 / 删掉这个邮箱的授权码
  POST {"action": "proxy", "id", "proxy"}         改这个邮箱走不走代理 (auto / off / http://127.0.0.1:端口)
  POST {"action": "check", "id"?: …}             马上读一次 (不给就每个邮箱都读; 登录被拒以后不再自动重试, 修好以后用它)
  POST {"action": "settings", "changes": {...}}   改设置 (mailbox.DEFAULTS 里那些); 云端摘要 (summary=cloud) 这里只能关,
                                                  要开得在本机终端里开 (tools/mail_setup.py)

只在看守起的正式服务端上读 (有 WM_ASKS_FILE、开着长期记忆库, 和存申请一样; 配置 mail.run=on / off 可以硬开硬关):
自检、试跑、测试起的服务端不读, 免得重复汇报、拿你的邮箱做测试. 邮箱目录默认 ~/.worldmodel/mail (配置 mail.home 可以换).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import mailbox as M
from .mailbox_report import Mailbox

KEY = web.AppKey("mailbox", Mailbox)
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
PAGE = Path(__file__).with_name("mailbox_page.html")
# 页面只从本机拿东西; 邮件里来的字一律当文本放 (textContent), 不当 HTML. 只许本机自己的页面嵌它 (防点击劫持)
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")


def should_run(cfg: dict) -> tuple[bool, str]:
    """这个服务端读不读邮箱 -> (读不读, 不读的原因)."""
    mode = str((cfg.get("mail") or {}).get("run", "auto")).strip().lower()
    if mode in ("off", "false", "0", "no"):
        return False, "配置里关了 (mail.run=off)"
    if mode in ("on", "true", "1", "yes"):
        return True, ""
    if not os.environ.get("WM_ASKS_FILE") or not (cfg.get("memory") or {}).get("vault"):
        return False, "只有看守起的正式服务端读邮箱 (自检、试跑、测试起的服务端不读)"
    return True, ""


def register(app: web.Application, cfg: dict, refusal, engines=None, agent=None) -> Mailbox:
    """建好 Mailbox, 挂 /api/mail 和起停. refusal(request): 来源不对时返回原因 (app.py 的 ws_refusal);
    engines: 发消息用它的 channels (_tell_owner、notify), 摘要用 channels.cloud / 本机 llm; agent: 工作台 (报黑名单保护着没有)."""
    conf = cfg.get("mail") or {}
    home = Path(str(conf["home"])).expanduser() if conf.get("home") else M.HOME
    run, why = should_run(cfg)
    port = (cfg.get("server") or {}).get("port")
    if port:  # 出错提示里让你打开的页面: 跟着这个服务端的端口 (没配就是 8790)
        with contextlib.suppress(TypeError, ValueError):
            M.PAGE_URL = f"http://127.0.0.1:{int(port)}/mail"
    ch = getattr(engines, "channels", None)

    def model(kind: str):
        if kind == "cloud":
            return getattr(ch, "cloud", None)  # QQ 渠道正在用的云端模型 (面板里配的), 没配是 None
        llm = getattr(engines, "llm", None)
        if kind == "local" and getattr(llm, "name", "") == "llamacpp" and getattr(engines, "llm_ok", False) \
                and (urlsplit(str(getattr(llm, "endpoint", ""))).hostname or "") in _LOOPBACK:
            return llm  # 本机的 llama.cpp: 内容不出这台电脑
        return None

    box = Mailbox(home, conf, tell=getattr(ch, "_tell_owner", None), notify=getattr(ch, "notify", None),
                  pet_online=(lambda: bool(getattr(ch, "listeners", None))) if ch is not None else None,
                  model=model, blacklist=getattr(agent, "blacklist", None), run=run, why_off=why)
    app[KEY] = box

    def resolve(body: dict) -> str:
        """请求里的 id (或地址) -> 存着的邮箱地址."""
        address = M.address_of(box.home, body.get("id") or body.get("address") or "")
        if address is None:
            raise ValueError("没有这个邮箱")
        return address

    async def page(request: web.Request) -> web.Response:
        if (reason := refusal(request)) is not None:
            raise web.HTTPForbidden(text=reason)
        return web.Response(body=await asyncio.to_thread(PAGE.read_bytes), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store", "Content-Security-Policy": _CSP,
                                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    async def api(request: web.Request) -> web.Response:
        if (reason := refusal(request)) is not None:
            raise web.HTTPForbidden(text=reason)
        if request.method == "GET":
            try:
                n = max(0, min(200, int(request.query.get("log") or 0)))
            except ValueError:
                n = 0
            out = {"ok": True, **box.status(), "now": time.time(),
                   "utc_offset_min": int((datetime.now().astimezone().utcoffset() or timedelta()).total_seconds() // 60)}
            if n:
                out["log"] = await asyncio.to_thread(box.log_tail, n)
            if request.query.get("providers"):
                hp = M.system_proxy()
                out.update(providers=M.providers(), system_proxy=f"{hp[0]}:{hp[1]}" if hp else None,
                           max_accounts=M.MAX_ACCOUNTS)
            return web.json_response(out)
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
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
            if action == "connect":  # 授权码只在这里经过: 不放进返回、不记日志, 用完就丢
                try:
                    port = int(body.get("port") or 0)
                except (TypeError, ValueError):
                    raise ValueError("端口要是数字") from None
                result = await box.connect_account(str(body.get("address") or ""), str(body.get("secret") or ""),
                                                   str(body.get("host") or ""), port, str(body.get("proxy") or "auto"),
                                                   bool(body.get("enable", True)))
                return web.json_response(result)
            if action == "test":
                return web.json_response(await box.test_account(resolve(body)))
            if action == "forget":
                return web.json_response(box.forget_account(resolve(body)))
            if action == "proxy":
                account = M.set_proxy(box.home, resolve(body), str(body.get("proxy") or ""))
                hp = M.proxy_for(account)
                return web.json_response({"ok": True, "proxy": account["proxy"], "proxy_now": f"{hp[0]}:{hp[1]}" if hp else None})
            if action == "check":  # id / address: 只读这一个邮箱 (不给就全读)
                only = [resolve(body)] if body.get("id") or body.get("address") else None
                rec = await box.poll(manual=True, only=only)
                return web.json_response({"ok": bool(rec.get("ok")), "record": rec, **({"error": rec["error"]["text"]}
                                                                                       if rec.get("error") else {})})
            if action == "settings":
                changed = box.update_settings(body.get("changes"))
                return web.json_response({"ok": True, "changed": changed, "settings": box.settings()})
            raise ValueError("action 只认 connect / test / forget / proxy / check / settings")
        except (ValueError, M.MailError) as exc:
            return web.json_response({"ok": False, "error": getattr(exc, "text", None) or str(exc)}, status=400)
        finally:
            body.pop("secret", None)

    async def startup(_app):
        await box.start()

    async def cleanup(_app):
        await box.close()

    app.router.add_route("*", "/api/mail", api)
    app.router.add_get("/mail", page)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return box
