"""微信 (腾讯 ClawBot 的 iLink 协议, 2026-10-26, own-harness.md 的 H1): 扫码把你微信里的"ClawBot"联系人绑到她身上,
你在微信里跟它说话就是跟她说话. 管理器 (manager.py) 把它当主人的私聊, 和 QQ 私聊走同一套.

协议照腾讯官方的 npm 包 @tencent-weixin/openclaw-weixin 2.4.10 (MIT, Copyright Tencent) 写的, 只用了这几个接口:
  POST ilink/bot/get_bot_qrcode?bot_type=3        -> {qrcode, qrcode_img_content}  (后者是要画成二维码的那串内容)
  GET  ilink/bot/get_qrcode_status?qrcode=…       -> {status, bot_token, ilink_bot_id, baseurl, ilink_user_id, redirect_host}
       status: wait / scaned / confirmed / expired / scaned_but_redirect / need_verifycode / verify_code_blocked / binded_redirect
  POST ilink/bot/getupdates  {get_updates_buf}    长轮询 (默认 35 秒) -> {ret, errcode, msgs, get_updates_buf, longpolling_timeout_ms}
  POST ilink/bot/sendmessage {msg: {to_user_id, client_id, message_type: 2 (BOT), message_state: 2 (FINISH),
                                    item_list: [{type: 1, text_item: {text}}], context_token}}
  请求头: AuthorizationType: ilink_bot_token、Authorization: Bearer <token>、X-WECHAT-UIN (随机 uint32 的十进制再 base64)、
  iLink-App-Id: bot、iLink-App-ClientVersion (按协议版本 2.4.10 打包). 请求体带 base_info: {channel_version, bot_agent};
  bot_agent 写的是我们自己 ("WorldModel/1.0 (desktop-pet)"), 不冒充 OpenClaw.
平台的限制: 只能和扫码的那个人一对一 (进不了群), 不能主动推送, 只能接着你的话回 (带上你那条消息的 context_token).
所以这里只认扫码人 (ilink_user_id) 发来的消息, 别人的一律不理.

- token: Windows DPAPI 加密后存在 ~/.worldmodel/weixin/account.json (只有这台电脑的这个用户解得开), 日志、状态里都没有;
- 收消息的游标 (get_updates_buf) 存在同目录 sync.txt, 重启不重复收; 消息号再去一次重;
- errcode -14: token 过期了, 停 1 小时再试 (腾讯的插件也这样), 状态里说"可能要重新扫码";
- 连着失败 3 次歇 30 秒, 否则 2 秒后再试;
- 二维码 5 分钟过期, 自动换新的, 最多 3 张; 整个扫码最多等 8 分钟.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import random
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote, urlsplit

import aiohttp

from . import aes_ecb

log = logging.getLogger("wm.channels.weixin")

HOME = Path.home() / ".worldmodel" / "weixin"  # 绑好的账号和游标放这儿 (测试里换成临时目录, 见 tests/conftest.py)
BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"  # 发图要先传到这儿 (腾讯插件里的默认值)
UPLOAD_IMAGE = 1       # getuploadurl 的 media_type: 图片
IMAGE_MAX = 2 * 1024 * 1024
PROTOCOL_VERSION = "2.4.10"  # 照着写的腾讯插件版本 (协议字段一样); 服务端按这个认客户端版本
APP_ID = "bot"
BOT_AGENT = "WorldModel/1.0 (desktop-pet)"
STALE_TOKEN = -14
PAUSE_S = 3600          # token 过期后歇多久再试
LONG_POLL_S = 35.0
QR_TTL_S = 300          # 一张二维码多久过期
QR_MAX = 3              # 最多换几张
LOGIN_MAX_S = 480       # 整个扫码最多等多久
TEXT_MAX = 1800         # 一条消息最多多少字 (长的拆开发)
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

MSG_USER, MSG_BOT = 1, 2
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
_PLACEHOLDER = {ITEM_IMAGE: "［图片］", ITEM_VOICE: "［语音］", ITEM_FILE: "［文件］", ITEM_VIDEO: "［视频］"}
_GAP = {ITEM_IMAGE: "image_failed", ITEM_VOICE: "voice", ITEM_FILE: "file", ITEM_VIDEO: "video"}


class WeixinError(RuntimeError):
    """iLink 请求失败: 连不上、HTTP 不是 200、回了错误码."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def base_ok(url: str) -> bool:
    """iLink 地址: 腾讯的 https 域名 (weixin.qq.com / qq.com 下面的), 或者本机 (测试的假服务)."""
    p = urlsplit(url or "")
    host = (p.hostname or "").lower()
    return (p.scheme == "https" and (host == "qq.com" or host.endswith(".qq.com"))) or host in _LOOPBACK


def _client_version(v: str) -> str:
    major, minor, patch = (int(x) for x in (v.split(".") + ["0", "0"])[:3])
    return str(((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF))


def _uin() -> str:
    return base64.b64encode(str(random.getrandbits(32)).encode("ascii")).decode("ascii")


def qr_svg(content: str) -> str:
    """二维码内容 -> SVG (面板直接放进页面; 服务端画, 面板不用另带二维码库)."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    return svg[svg.find("<svg"):]  # 去掉 <?xml …?> 头, 直接放进页面


def protect(text: str) -> str:
    """DPAPI 加密 (这台电脑的这个用户才解得开) -> base64."""
    import win32crypt
    return base64.b64encode(win32crypt.CryptProtectData(text.encode("utf-8"), "WorldModel weixin", None, None, None, 0)).decode()


def unprotect(blob: str) -> str:
    import win32crypt
    return win32crypt.CryptUnprotectData(base64.b64decode(blob), None, None, None, 0)[1].decode("utf-8")


def mask(user: str) -> str:
    """给人看的微信用户编号: 头尾各留几个字."""
    s = str(user or "")
    return f"{s[:4]}…{s[-6:]}" if len(s) > 12 else ("***" if s else "")


class WeixinClient:
    """一个绑好的 ClawBot (一个微信号). on_message({text, mid, time, gaps}) 收到扫码人发来的一条."""

    def __init__(self, home: Path, base_url: str = BASE_URL, bot_type: str = "3",
                 on_message: Callable[[dict], Awaitable[None]] | None = None, clock=time.time):
        if not base_ok(base_url):
            raise ValueError("微信的地址只能是腾讯的 https 域名或本机")
        self.home = Path(home)
        self.default_base = base_url.rstrip("/")
        self.cdn_base = CDN_BASE_URL
        self.bot_type = str(bot_type)
        self.on_message = on_message
        self.clock = clock
        self.account = self._read_account()
        self.buf = self._read_text("sync.txt")
        self.state = "off"           # off / connecting / online / expired / error
        self.error = ""
        self.context_token = ""      # 最近一条消息的, 回复时带上
        self.last_in = 0.0           # 你最近一次在微信上找她
        self.received = self.sent = 0
        self.ignored = 0
        self._seen: deque[str] = deque(maxlen=200)
        self._poll_task: asyncio.Task | None = None
        self._login_task: asyncio.Task | None = None
        self.login: dict = {"state": "idle"}
        self._code: str | None = None  # 扫码时微信要验证码: 面板填的
        self._http: aiohttp.ClientSession | None = None
        # 管理器定: 没开长期记忆库的服务端 (各种自检起的测试服务端) 不收微信, 免得抢走你发给她的消息
        self.allowed = True
        self._lockf = None  # 收消息时拿着 poll.lock: 同一个号同时只能一处收 (正式服务端和开发预览别抢)

    # ---- 存账号 ----
    def _read_account(self) -> dict | None:
        try:
            a = json.loads((self.home / "account.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return a if isinstance(a, dict) and a.get("token") and a.get("user_id") else None

    def _read_text(self, name: str) -> str:
        try:
            return (self.home / name).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _write(self, name: str, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.home / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.home / name)

    @property
    def bound(self) -> bool:
        return self.account is not None

    @property
    def online(self) -> bool:
        return self.state == "online"

    def _token(self) -> str:
        return unprotect(self.account["token"]) if self.account else ""

    def status(self) -> dict:
        a = self.account or {}
        login = {k: v for k, v in self.login.items() if k not in ("qrcode",)}
        return {"bound": self.bound, "user": mask(a.get("user_id", "")), "bound_at": a.get("bound_at"),
                "state": self.state, "error": self.error, "received": self.received, "sent": self.sent,
                "ignored": self.ignored, "last_in": self.last_in or None, "login": login}

    # ---- HTTP ----
    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            # 不读环境变量里的代理: 腾讯在国内, 直连就好; 本机的 HTTPS_PROXY 是 https://127.0.0.1:1082, aiohttp 不支持,
            # 每次请求都打一条警告 (长轮询半分钟一次, 日志会被刷满)
            self._http = aiohttp.ClientSession(trust_env=False)
        return self._http

    def _headers(self, token: str | None) -> dict:
        h = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN": _uin(),
             "iLink-App-Id": APP_ID, "iLink-App-ClientVersion": _client_version(PROTOCOL_VERSION)}
        if token:
            h["Authorization"] = f"Bearer {token.strip()}"
        return h

    async def _call(self, method: str, base: str, path: str, body: dict | None, token: str | None,
                    timeout_s: float) -> dict:
        if not base_ok(base):
            raise WeixinError(f"地址不对: {base[:60]}")
        url = f"{base.rstrip('/')}/{path}"
        data = None if body is None else json.dumps({**body, "base_info": {"channel_version": PROTOCOL_VERSION,
                                                                            "bot_agent": BOT_AGENT}}, ensure_ascii=False)
        s = await self._session()
        try:
            async with s.request(method, url, data=data, headers=self._headers(token),
                                 timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
                text = await r.text()
                if r.status != 200:
                    raise WeixinError(f"HTTP {r.status}", r.status)
        except asyncio.TimeoutError:  # 长轮询到点 (aiohttp 的读超时也是 ClientError 的子类, 先接住)
            raise
        except aiohttp.ClientError as exc:
            raise WeixinError(f"连不上微信: {type(exc).__name__}") from None
        try:
            got = json.loads(text) if text.strip() else {}
        except ValueError:
            raise WeixinError("微信回的不是 JSON") from None
        return got if isinstance(got, dict) else {}

    # ---- 扫码 ----
    async def begin_login(self) -> dict:
        """开始扫码 (之前的扫码作废). 二维码拿到之前 state 是 fetching; 面板轮询 status() 看进度."""
        await self.cancel_login()
        self._code = None
        self.login = {"state": "fetching", "started": self.clock()}
        self._login_task = asyncio.get_running_loop().create_task(self._login_flow())
        return self.status()

    async def cancel_login(self) -> None:
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._login_task
        self._login_task = None
        if self.login.get("state") not in ("idle", "confirmed"):
            self.login = {"state": "idle"}

    def give_code(self, code: str) -> bool:
        """微信要验证码时, 面板把你在手机上看到的数字交进来."""
        code = "".join(ch for ch in str(code or "") if ch.isalnum())[:12]
        if self.login.get("state") != "need_code" or not code:
            return False
        self._code = code
        self.login["state"] = "checking_code"
        return True

    async def _login_flow(self) -> None:
        started = self.clock()
        try:
            for n in range(1, QR_MAX + 1):
                r = await self._call("POST", self.default_base, f"ilink/bot/get_bot_qrcode?bot_type={quote(self.bot_type)}",
                                     {"local_token_list": []}, None, 15)
                qr, content = str(r.get("qrcode") or ""), str(r.get("qrcode_img_content") or "")
                if not qr or not content:
                    raise WeixinError("微信没给二维码")
                self.login = {"state": "wait", "svg": qr_svg(content), "qrcode": qr, "n": n,
                              "expires_at": self.clock() + QR_TTL_S, "started": started}
                done = await self._wait_scan(qr, self.clock() + QR_TTL_S, started)
                if done:
                    return
            self.login = {"state": "expired", "error": f"换了 {QR_MAX} 张二维码都没扫, 再点一次"}
        except asyncio.CancelledError:
            raise
        except WeixinError as exc:
            self.login = {"state": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 (扫码出错不能影响别的)
            log.exception("微信扫码出错")
            self.login = {"state": "error", "error": f"出错了: {type(exc).__name__}"}

    async def _wait_scan(self, qr: str, until: float, started: float) -> bool:
        """轮询一张二维码的状态. 返回 True: 结束了 (绑好了或者没法再试); False: 这张过期了, 换一张."""
        base = self.default_base
        while self.clock() < until:
            if self.clock() - started > LOGIN_MAX_S:
                self.login = {"state": "expired", "error": "等太久了, 再点一次"}
                return True
            path = f"ilink/bot/get_qrcode_status?qrcode={quote(qr)}"
            if self._code:
                path += f"&verify_code={quote(self._code)}"
            try:
                st = await self._call("GET", base, path, None, None, LONG_POLL_S + 5)
            except asyncio.TimeoutError:
                st = {"status": "wait"}
            status = str(st.get("status") or "wait")
            if status == "confirmed":
                token, user = str(st.get("bot_token") or ""), str(st.get("ilink_user_id") or "")
                if not token or not user:
                    self.login = {"state": "error", "error": "微信说确认了, 但没给 token"}
                    return True
                got = str(st.get("baseurl") or "").rstrip("/")
                self.account = {"bot_id": str(st.get("ilink_bot_id") or ""), "user_id": user,
                                "base_url": got if got and base_ok(got) else self.default_base,
                                "token": protect(token), "bound_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                self._write("account.json", json.dumps(self.account, ensure_ascii=False, indent=1))
                self.buf = ""
                self._write("sync.txt", "")
                self.login = {"state": "confirmed", "user": mask(user)}
                log.info("微信绑好了 (%s)", mask(user))
                self.start()
                return True
            if status in ("scaned_but_redirect", "binded_redirect") and st.get("redirect_host"):
                host = str(st["redirect_host"]).strip().strip("/")
                if base_ok(f"https://{host}"):
                    base = f"https://{host}"
            if status == "expired" or status == "verify_code_blocked":
                return False
            if status == "need_verifycode":
                if self.login.get("state") != "checking_code":
                    self.login.update(state="need_code")
                    self._code = None
                await asyncio.sleep(1)
                continue
            self.login["state"] = {"scaned": "scanned", "scaned_but_redirect": "scanned",
                                   "binded_redirect": "scanned"}.get(status, "wait")
            await asyncio.sleep(1)
        return False

    async def logout(self) -> None:
        """解绑: 停掉收消息, 删掉本机存的 token 和游标 (微信里的 ClawBot 你自己在微信里删)."""
        await self.cancel_login()
        await self.stop()
        self.account = None
        for name in ("account.json", "sync.txt"):
            with contextlib.suppress(OSError):
                (self.home / name).unlink()
        self.buf = self.context_token = ""
        self.state, self.error = "off", ""

    # ---- 收发 ----
    def start(self) -> None:
        if not self.account or (self._poll_task is not None and not self._poll_task.done()):
            return
        if not self.allowed:
            self.state, self.error = "off", "这个服务端没开长期记忆库 (是测试用的), 不收微信"
            return
        if not self._lock():
            self.state, self.error = "off", "别的服务端正在收这个微信 (同一个号同时只能一处收)"
            return
        self.state, self.error = "connecting", ""
        self._poll_task = asyncio.get_running_loop().create_task(self._poll())

    def _lock(self) -> bool:
        if self._lockf is not None:
            return True
        try:
            import msvcrt
            self.home.mkdir(parents=True, exist_ok=True)
            f = open(self.home / "poll.lock", "a+")
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                f.close()
                return False
        except ImportError:  # 不是 Windows: 不加锁
            return True
        self._lockf = f
        return True

    def _unlock(self) -> None:
        if self._lockf is not None:
            import msvcrt
            with contextlib.suppress(OSError):
                self._lockf.seek(0)
                msvcrt.locking(self._lockf.fileno(), msvcrt.LK_UNLCK, 1)
            self._lockf.close()
            self._lockf = None

    async def stop(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        self._poll_task = None
        self._unlock()
        if self.state != "expired":
            self.state = "off"

    async def close(self) -> None:
        await self.cancel_login()
        await self.stop()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    async def _poll(self) -> None:
        fails, wait_s = 0, LONG_POLL_S
        while self.account:
            try:
                r = await self._call("POST", self.account["base_url"], "ilink/bot/getupdates",
                                     {"get_updates_buf": self.buf}, self._token(), wait_s + 5)
            except asyncio.TimeoutError:
                continue  # 长轮询这一轮没消息
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                fails += 1
                self.state, self.error = "error", str(exc)[:200]
                await asyncio.sleep(30 if fails >= 3 else 2)
                fails = 0 if fails >= 3 else fails
                continue
            code = r.get("errcode") or r.get("ret") or 0
            if code == STALE_TOKEN:
                self.state, self.error = "expired", "微信说 token 过期了 (可能要重新扫码), 1 小时后再试"
                log.warning("微信 token 过期 (-14), 歇 1 小时")
                await asyncio.sleep(PAUSE_S)
                continue
            if code:
                fails += 1
                self.state, self.error = "error", f"微信回了错误 {code}: {str(r.get('errmsg') or '')[:120]}"
                await asyncio.sleep(30 if fails >= 3 else 2)
                fails = 0 if fails >= 3 else fails
                continue
            fails = 0
            self.state, self.error = "online", ""
            if r.get("get_updates_buf"):
                self.buf = str(r["get_updates_buf"])
                with contextlib.suppress(OSError):
                    self._write("sync.txt", self.buf)
            ms = r.get("longpolling_timeout_ms")
            if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
                wait_s = min(120.0, ms / 1000)
            for msg in r.get("msgs") or []:
                if isinstance(msg, dict):
                    await self._inbound(msg)

    async def _inbound(self, msg: dict) -> None:
        """只收扫码人发来的 (message_type 1); 自己发出去的回显、别人的都不理."""
        if msg.get("message_type") != MSG_USER or str(msg.get("from_user_id") or "") != (self.account or {}).get("user_id"):
            self.ignored += 1
            return
        mid = str(msg.get("message_id") or "")
        if mid and mid in self._seen:
            return
        if mid:
            self._seen.append(mid)
        parts, typed, gaps = [], [], []  # typed: 只有你打的字 (语音转的字、图片这些不算), 渠道那边拿它认"我叫…""我喜欢…"
        for it in msg.get("item_list") or []:
            if not isinstance(it, dict):
                continue
            t = it.get("type")
            if t == ITEM_TEXT:
                parts.append(str((it.get("text_item") or {}).get("text") or ""))
                typed.append(parts[-1])
            elif t == ITEM_VOICE and str((it.get("voice_item") or {}).get("text") or "").strip():
                parts.append(f"［语音：{(it['voice_item'])['text']}］")  # 微信自己转好的字
            elif t in _PLACEHOLDER:
                parts.append(_PLACEHOLDER[t])
                gaps.append(_GAP[t])
        text = " ".join(p for p in parts if p).strip()
        if msg.get("context_token"):
            self.context_token = str(msg["context_token"])
        if not text:
            return
        self.received += 1
        self.last_in = self.clock()
        when = msg.get("create_time_ms")
        if self.on_message is not None:
            try:
                await self.on_message({"text": text, "mid": mid, "gaps": gaps, "typed": " ".join(p for p in typed if p).strip(),
                                       "time": when / 1000 if isinstance(when, (int, float)) and when > 0 else self.clock()})
            except Exception:  # noqa: BLE001
                log.exception("微信消息交给管理器时出错")

    async def send_text(self, text: str) -> None:
        """回扫码人一条 (长的按 TEXT_MAX 拆开). 失败抛 WeixinError."""
        if not self.account:
            raise WeixinError("还没绑微信")
        text = str(text or "").strip()
        for i in range(0, len(text), TEXT_MAX):
            body = {"msg": {"from_user_id": "", "to_user_id": self.account["user_id"],
                            "client_id": f"worldmodel-{secrets.token_hex(8)}", "message_type": MSG_BOT,
                            "message_state": 2, "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text[i:i + TEXT_MAX]}}],
                            **({"context_token": self.context_token} if self.context_token else {})}}
            r = await self._call("POST", self.account["base_url"], "ilink/bot/sendmessage", body, self._token(), 15)
            code = r.get("ret") or r.get("errcode") or 0
            if code:
                if code == STALE_TOKEN:
                    self.state, self.error = "expired", "微信说 token 过期了 (可能要重新扫码)"
                raise WeixinError(f"微信没收: {code} {str(r.get('errmsg') or '')[:80]}", code)
            self.sent += 1

    async def send_image(self, data: bytes, caption: str = "") -> None:
        """回扫码人一张图 (现在只有"QQ 要扫码"的二维码用它). 照腾讯插件 @tencent-weixin/openclaw-weixin 的写法:
        向 ilink/bot/getuploadurl 要上传参数 -> 图用随机的 AES-128 密钥 ECB 加密 -> POST 到微信的 CDN
        (…/upload?encrypted_query_param=…&filekey=…) -> 把 CDN 回的 x-encrypted-param 连同密钥 (hex 字符串再 base64)
        放进 image_item 发 sendmessage. 说明文字照插件的做法单独先发一条. 平台的限制照旧: 只能接着扫码人的话回.
        没有对真的微信验证过; 失败抛 WeixinError, 调用方退回只发文字. 图只在内存里, 不存盘."""
        if not self.account:
            raise WeixinError("还没绑微信")
        data = bytes(data or b"")
        if not data or len(data) > IMAGE_MAX:
            raise WeixinError("图是空的或者太大")
        key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
        cipher = aes_ecb.encrypt(key, data)
        base, token, user = self.account["base_url"], self._token(), self.account["user_id"]
        got = await self._call("POST", base, "ilink/bot/getuploadurl",
                               {"filekey": filekey, "media_type": UPLOAD_IMAGE, "to_user_id": user, "rawsize": len(data),
                                "rawfilemd5": hashlib.md5(data).hexdigest(), "filesize": len(cipher), "no_need_thumb": True,
                                "aeskey": key.hex()}, token, 15)
        code = got.get("ret") or got.get("errcode") or 0
        if code:
            raise WeixinError(f"微信不给上传地址: {code} {str(got.get('errmsg') or '')[:80]}", code)
        full, param = str(got.get("upload_full_url") or "").strip(), str(got.get("upload_param") or "")
        url = full or (f"{self.cdn_base.rstrip('/')}/upload?encrypted_query_param={quote(param, safe='')}&filekey={quote(filekey, safe='')}"
                       if param else "")
        if not url:
            raise WeixinError("微信没给上传地址")
        if not base_ok(url):
            raise WeixinError(f"上传地址不对: {url[:60]}")
        s = await self._session()
        try:
            async with s.post(url, data=cipher, headers={"Content-Type": "application/octet-stream"}, allow_redirects=False,
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                down = r.headers.get("x-encrypted-param") or ""
                if r.status != 200 or not down:
                    raise WeixinError(f"微信 CDN 没收: HTTP {r.status}", r.status)
        except asyncio.TimeoutError:
            raise WeixinError("传到微信 CDN 超时") from None
        except aiohttp.ClientError as exc:
            raise WeixinError(f"连不上微信 CDN: {type(exc).__name__}") from None
        if caption:
            await self.send_text(caption)
        body = {"msg": {"from_user_id": "", "to_user_id": user, "client_id": f"worldmodel-{secrets.token_hex(8)}",
                        "message_type": MSG_BOT, "message_state": 2,
                        "item_list": [{"type": ITEM_IMAGE, "image_item": {
                            "media": {"encrypt_query_param": down, "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
                                      "encrypt_type": 1}, "mid_size": len(cipher)}}],
                        **({"context_token": self.context_token} if self.context_token else {})}}
        r = await self._call("POST", base, "ilink/bot/sendmessage", body, token, 15)
        code = r.get("ret") or r.get("errcode") or 0
        if code:
            if code == STALE_TOKEN:
                self.state, self.error = "expired", "微信说 token 过期了 (可能要重新扫码)"
            raise WeixinError(f"微信没收图: {code} {str(r.get('errmsg') or '')[:80]}", code)
        self.sent += 1
