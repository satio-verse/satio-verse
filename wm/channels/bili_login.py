"""B 站扫码登录 (2026-11-07 用户: "项目里 bili_cookie.txt 已经失效, 她现在看视频读不到字幕"): 在保管箱页面点"扫码登录",
用登着她那个 B 站号的手机 App 扫二维码并在手机上确认, 程序拿到登录凭证 (Cookie), 加密存进账号保管箱 (vault.py), 不用手动复制粘贴.

用的是 B 站网页版的扫码登录接口 (社区整理的 bilibili-API-collect 里写的, 不是官方文档, 变了可能失效):
  GET  passport.bilibili.com/x/passport-login/web/qrcode/generate            -> data.url (二维码内容)、data.qrcode_key
  GET  passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=…   -> data.code: 86101 没扫 / 86090 扫了没确认 /
                                                                               86038 过期 / 0 成功 (Set-Cookie 里有 SESSDATA、bili_jct、
                                                                               DedeUserID…, data.refresh_token)
  GET  api.bilibili.com/x/frontend/finger/spi                                -> buvid3 / buvid4 (读字幕要带)
  GET  api.bilibili.com/x/web-interface/nav (带 Cookie)                       -> isLogin、mid、uname (核对登录是真的, 也核对是不是她原来那个号)
规矩:
- 不走系统代理 (B 站直连就通)、不跟随跳转、只连 B 站自己的域名 (测试的本机替身除外)、回应最多读 1 MB;
- Cookie、刷新令牌只在内存里经手, 马上加密存保管箱; 返回给页面的、写进日志的都没有它们;
- 登上来的号和以前存的号 (保管箱里记的 uid, 或旧 bili_cookie.txt 里的 DedeUserID) 不是同一个: 不直接换, 先问主人 (confirm);
- 存好以后 ~/.worldmodel/bili_cookie.txt (旧的明文 Cookie 文件) 换成一个记号文件 (bili.py 认到记号就去保管箱取),
  旧文件挪进保管箱目录的 legacy/ 留着, 不删;
- 遇到验证码、风控 (-412)、要短信确认这类: 照实告诉主人去 B 站上自己处理, 不绕过;
- 没做 Cookie 自动续期 (要 RSA 加密, 这台电脑没装 cryptography): 过期了再扫一次.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from . import qq_window
from .bili import VAULT_MARK as MARK, is_vault_marker as is_marker
from .vault import Vault, VaultError

log = logging.getLogger("wm.channels.bili_login")

PASSPORT = "https://passport.bilibili.com"
API = "https://api.bilibili.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_BODY = 1 << 20
QR_TTL_S = 180           # B 站的二维码 3 分钟过期
PENDING_TTL_S = 600      # 登上来的是另一个号: 等主人答复最多这么久
KEEP = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4", "b_nut")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class BiliLoginError(Exception):
    """扫码登录没办成. text 是给人看的一句, 不带 Cookie."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind, self.text = kind, text


def check_base(url: str, host_suffix: str = ".bilibili.com") -> str:
    u = urlsplit(str(url or ""))
    host = (u.hostname or "").lower()
    if host in _LOOPBACK and u.scheme in ("http", "https"):
        return str(url).rstrip("/")
    if u.scheme == "https" and (host == host_suffix[1:] or host.endswith(host_suffix)) and not (u.username or u.password):
        return str(url).rstrip("/")
    raise ValueError("B 站的地址只能是 https 的 bilibili.com 域名或本机")


def _cookie_ok(v: str) -> bool:
    return bool(v) and v.isascii() and v.isprintable() and not any(c in ' ;"\\' for c in v)


def marker_text() -> str:
    from .vault import PAGE_URL
    return (f"# {MARK}\n# 这个文件只是个记号: B 站的登录凭证已经加密存进账号保管箱 (~/.worldmodel/vault), 不在这里。\n"
            f"# 重新登录请到 {PAGE_URL} 点「扫码登录 B 站」。\n")


def legacy_mid(path: Path) -> str:
    """旧的明文 Cookie 文件里的 DedeUserID (用来核对新登上来的是不是同一个号); 没有返回 ""."""
    try:
        text = Path(path).read_bytes()[:8192].decode("utf-8-sig", errors="replace")
    except OSError:
        return ""
    for ln in text.splitlines():
        if ln.strip().startswith("#"):
            continue
        for part in ln.split(";"):
            k, eq, v = part.partition("=")
            if eq and k.strip() == "DedeUserID" and v.strip().isdigit():
                return v.strip()
    return ""


async def _json(resp: aiohttp.ClientResponse) -> dict:
    if (resp.content_length or 0) > MAX_BODY:
        raise BiliLoginError("protocol", "B 站回的东西太大, 不像是它")
    raw, total = [], 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if total > MAX_BODY:
            raise BiliLoginError("protocol", "B 站回的东西太大, 不像是它")
        raw.append(chunk)
    try:
        data = json.loads(b"".join(raw) or b"{}")
    except ValueError:
        raise BiliLoginError("protocol", f"B 站回的不是 JSON (HTTP {resp.status}), 接口可能变了") from None
    if not isinstance(data, dict):
        raise BiliLoginError("protocol", "B 站回的东西对不上, 接口可能变了")
    return data


class BiliLogin:
    """一次只做一个扫码登录. begin() 出二维码, 页面隔两三秒调 poll(), 成了就存进保管箱."""

    def __init__(self, vault: Vault, cookie_file=None, passport: str = PASSPORT, api: str = API, clock=time.time,
                 timeout_s: float = 10.0):
        self.vault = vault
        self.cookie_file = cookie_file or (lambda: Path.home() / ".worldmodel" / "bili_cookie.txt")
        self.passport, self.api = check_base(passport), check_base(api)
        self.clock = clock
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._key = ""
        self._started = 0.0
        self._pending: dict | None = None  # 登上来的是另一个号: 等主人答复的 (Cookie 在内存里, 不落盘)
        self._lock = asyncio.Lock()
        self.last: dict = {"state": "idle"}

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=self.timeout, trust_env=False,
                                     headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                                     connector=aiohttp.TCPConnector(force_close=True))

    async def _get(self, http: aiohttp.ClientSession, url: str, **kw):
        try:
            return await http.get(url, allow_redirects=False, **kw)
        except asyncio.TimeoutError:
            raise BiliLoginError("network", "连 B 站超时了, 稍后再试") from None
        except (aiohttp.ClientError, OSError) as exc:
            raise BiliLoginError("network", f"连不上 B 站 ({type(exc).__name__})") from None

    def view(self) -> dict:
        """页面上显示的 (没有 Cookie)."""
        out = dict(self.last)
        out.pop("_cookie", None)
        return out

    def cancel(self) -> dict:
        self._key, self._pending = "", None
        self.last = {"state": "idle"}
        return self.view()

    async def begin(self) -> dict:
        async with self._lock:
            self._pending = None
            async with self._session() as http:
                async with await self._get(http, f"{self.passport}/x/passport-login/web/qrcode/generate") as r:
                    data = await _json(r)
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            if data.get("code") != 0 or not body.get("url") or not body.get("qrcode_key"):
                raise BiliLoginError("server", f"B 站没给二维码 (code {data.get('code')})")
            self._key, self._started = str(body["qrcode_key"]), self.clock()
            png = await asyncio.to_thread(qq_window.render_png, str(body["url"]))
            self.last = {"state": "waiting", "text": "用登着她那个 B 站号的手机 App 扫这个二维码，并在手机上点确认。",
                         "qr": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                         "expires_at": self._started + QR_TTL_S}
            return self.view()

    async def poll(self) -> dict:
        async with self._lock:
            if self._pending is not None:
                if self.clock() - self._pending["at"] > PENDING_TTL_S:
                    self._pending = None
                    self.last = {"state": "expired", "text": "等你答复的时间太久了, 重新扫一次吧。"}
                return self.view()
            if not self._key:
                return self.view()
            if self.clock() - self._started > QR_TTL_S + 10:
                self._key = ""
                self.last = {"state": "expired", "text": "二维码过期了, 再点一次「扫码登录」。"}
                return self.view()
            async with self._session() as http:
                async with await self._get(http, f"{self.passport}/x/passport-login/web/qrcode/poll",
                                           params={"qrcode_key": self._key}) as r:
                    data = await _json(r)
                    set_cookies = r.headers.getall("Set-Cookie", [])
                body = data.get("data") if isinstance(data.get("data"), dict) else {}
                code = body.get("code")
                if data.get("code") == -412 or code == -412:
                    raise BiliLoginError("risk", "B 站说请求太频繁 / 触发了风控, 过一会儿再扫")
                if code == 86101:
                    return self.view()  # 还没扫: 二维码还是原来那张
                if code == 86090:
                    self.last = {**self.last, "state": "scanned", "text": "已经扫了, 在手机上点「确认登录」。"}
                    return self.view()
                if code == 86038:
                    self._key = ""
                    self.last = {"state": "expired", "text": "二维码过期了, 再点一次「扫码登录」。"}
                    return self.view()
                if code != 0:
                    raise BiliLoginError("protocol", f"B 站扫码的回复对不上 (code {code}), 接口可能变了")
                cookie, refresh, expires = self._collect(set_cookies, body)
                self._key = ""
                return await self._finish(http, cookie, refresh, expires)

    def _collect(self, set_cookies: list[str], body: dict) -> tuple[dict, str, str]:
        jar: dict[str, str] = {}
        for header in set_cookies:
            sc = SimpleCookie()
            with contextlib.suppress(Exception):
                sc.load(header)
            for k, m in sc.items():
                if k in KEEP and _cookie_ok(m.value):
                    jar[k] = m.value
        query: dict[str, str] = {}  # 链接上的值不解码: 和 Cookie 里的写法一样 (SESSDATA 里的 %2C 要原样)
        for pair in urlsplit(str(body.get("url") or "")).query.split("&"):
            k, eq, v = pair.partition("=")
            if eq and k not in query:
                query[k] = v
        for k in KEEP:  # Set-Cookie 里没有的, 回复里带的链接上有
            if k not in jar and _cookie_ok(query.get(k, "")):
                jar[k] = query[k]
        refresh = str(body.get("refresh_token") or "")
        expires = query.get("Expires", "")
        return jar, (refresh if _cookie_ok(refresh) else ""), (expires if expires.isdigit() else "")

    async def _finish(self, http: aiohttp.ClientSession, jar: dict, refresh: str, expires: str) -> dict:
        if "SESSDATA" not in jar or "bili_jct" not in jar:
            raise BiliLoginError("protocol", "扫码成功了, 但 B 站没给登录凭证 (接口可能变了)")
        try:  # 读字幕要带 buvid3 / buvid4: 匿名拿一份 (拿不到也能登录, 只是字幕接口可能不认)
            async with await self._get(http, f"{self.api}/x/frontend/finger/spi") as r:
                spi = (await _json(r)).get("data") or {}
            for k, name in (("buvid3", "b_3"), ("buvid4", "b_4")):
                if _cookie_ok(str(spi.get(name) or "")):
                    jar.setdefault(k, str(spi[name]))
        except BiliLoginError:
            pass
        header = "; ".join(f"{k}={jar[k]}" for k in KEEP if k in jar)
        async with await self._get(http, f"{self.api}/x/web-interface/nav", headers={"Cookie": header}) as r:
            nav = (await _json(r)).get("data") or {}
        if nav.get("isLogin") is not True:
            raise BiliLoginError("server", "扫码成功了, 但拿这个登录凭证去核对时 B 站说没登录, 再扫一次试试")
        mid, name = str(nav.get("mid") or jar.get("DedeUserID") or ""), str(nav.get("uname") or "")[:40]
        old = ""
        with contextlib.suppress(VaultError):
            cur = self.vault.get("bilibili")
            old = str((cur or {}).get("meta", {}).get("mid") or "")
        old = old or legacy_mid(self.cookie_file())
        item = {"cookie": header, "refresh": refresh, "mid": mid, "uname": name, "expires": expires, "at": self.clock(),
                "old": old}
        if old and mid and old != mid:  # 不是以前那个号: 先问
            self._pending = item
            self.last = {"state": "confirm", "uname": name, "mid": mid, "old_mid": old,
                         "text": f"登上来的是「{name}」（uid {mid}），不是以前存的那个号（uid {old}）。要换成这个号吗？"}
            return self.view()
        return self._save(item)

    def _save(self, item: dict) -> dict:
        fresh = self.vault.get("bilibili") is None
        fields = {"cookie": item["cookie"], **({"refresh_token": item["refresh"]} if item["refresh"] else {})}
        meta = {"mid": item["mid"], "uname": item["uname"], "verified_at": round(self.clock()), "verified": True,
                "saved_via": "scan", **({"expires": item["expires"]} if item["expires"] else {})}
        # 第一次存: 读字幕、看消息这类只读的用途算主人这一下扫码就点了头; 发评论要主人另外在页面上打开
        self.vault.put("bilibili", fields, meta=meta, purposes={"read": True} if fresh else None, by="scan")
        self.vault.set_flags("bilibili", expired_at=None)
        note = self._write_marker()
        self._pending = None
        self.last = {"state": "done", "uname": item["uname"], "mid": item["mid"],
                     "text": f"登录好了：「{item['uname']}」（uid {item['mid']}）。" + note}
        return self.view()

    async def confirm(self, replace: bool) -> dict:
        async with self._lock:
            item = self._pending
            if item is None:
                raise BiliLoginError("config", "没有在等你答复的登录")
            if not replace:
                self._pending = None
                self.last = {"state": "idle", "text": "没换, 还是以前存的那个号。"}
                return self.view()
            return self._save(item)

    def _write_marker(self) -> str:
        """旧的明文 Cookie 文件换成记号文件. 返回一句说明 (写进页面)."""
        try:
            path = Path(self.cookie_file()).expanduser()
        except (OSError, ValueError, TypeError):
            return ""
        try:
            old = path.read_bytes()[:8192].decode("utf-8-sig", errors="replace") if path.exists() else ""
            if old and not is_marker(old):
                keep = self.vault.home / "legacy"
                keep.mkdir(parents=True, exist_ok=True)
                os.replace(path, keep / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.clock()))}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(marker_text(), encoding="utf-8", newline="\n")
            return "旧的明文 Cookie 文件已经换成记号，原文件挪进保管箱的 legacy 目录留着。" if old and not is_marker(old) else ""
        except OSError as exc:
            log.warning("B 站 Cookie 的记号文件没写上: %s", type(exc).__name__)
            return f"（记号文件 {path} 没写上，读字幕前先在设置里把 B 站 Cookie 文件指向别处，或者手动建一个内容为「# {MARK}」的文件）"

    async def check(self) -> dict:
        """拿保管箱里存的登录凭证问 B 站: 还有效吗 (页面上的「检查」按钮; 主人点的, 算点过头). 返回 {ok, valid, uname, text}."""
        lease = self.vault.lease("bilibili", "check", by="page", explicit=True)
        if not lease.ok:
            return {"ok": False, "valid": False, "text": "保管箱里还没有 B 站的登录凭证" if lease.why == "none" else (lease.text or "取不出登录凭证")}
        try:
            parts = [kv.strip() for kv in lease.get("cookie").split(";") if kv.strip()]
            header = "; ".join(p for p in parts if p.split("=", 1)[0] in KEEP)
            async with self._session() as http:
                async with await self._get(http, f"{self.api}/x/web-interface/nav", headers={"Cookie": header}) as r:
                    nav = (await _json(r)).get("data") or {}
        except BiliLoginError as exc:
            lease.done(False, "检查没成: " + exc.kind)
            return {"ok": False, "valid": False, "text": exc.text}
        valid = nav.get("isLogin") is True
        lease.done(True, "还有效" if valid else "已经失效")
        if valid:
            self.vault.update_meta("bilibili", verified_at=round(self.clock()), verified=True, uname=str(nav.get("uname") or "")[:40],
                                   mid=str(nav.get("mid") or ""))
            self.vault.set_flags("bilibili", expired_at=None)
        else:
            self.vault.update_meta("bilibili", verified=False, verified_at=round(self.clock()))
            self.vault.set_flags("bilibili", expired_at=self.clock())
        return {"ok": True, "valid": valid, "uname": str(nav.get("uname") or ""),
                "text": "登录还有效。" if valid else "登录已经失效了, 请重新扫码。"}
