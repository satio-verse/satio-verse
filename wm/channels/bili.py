"""B 站只读客户端: 热门列表、视频详情、标签、几条热评, 和 (你登录了的话) 字幕. 给"她自己的生活"里的"看视频"用 (manager._watch).

- 只发 GET, 只连 base (默认 https://api.bilibili.com) 和字幕文件的 CDN (https 的 *.hdslb.com / *.bilibili.com);
  不走系统代理 (trust_env=False, B 站直连就通); 不跟随跳转; 不存回应里的 Cookie (DummyCookieJar);
- 每个请求有超时, 回应最多读 2 MB; 接口回的 code 不是 0 就抛 BiliError (风控 -412、视频没了 -404 这些);
- 字幕要登录: 你自己登录 B 站, 把 Cookie 存在本机文件里 (read_cookie). 只有拿字幕列表的那一个请求 (/x/player/wbi/v2,
  要 WBI 签名) 带上, 而且只带 SESSDATA、buvid3、buvid4 (bili_jct 是发评论、点赞这类写操作用的, 不带); 别的请求都不带;
  base 不是 api.bilibili.com (测试的本机替身除外) 就不带;
- 没字幕时取封面和几帧截图 (/x/player/videoshot 的拼图, 不登录), 拼成一张给看得了图的模型;
- 读到的都是外部内容: 由管理器包上边界交给"隔离读者"概括, 不直接进她的会话.
- 发评论 (2026-10-25, 她自己的 B 站号转正了): 用她那个号的 Cookie (单独一个文件, 带 bili_jct 当 csrf), 只 POST
  /x/v2/reply/add 这一个写接口; 要人机验证 (12015)、风控、没登录这些都抛 BiliPostError, 管理器当天就停.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import urllib.parse
from pathlib import Path

import aiohttp

from .media import contact_sheet

BASE = "https://api.bilibili.com"
VC_BASE = "https://api.vc.bilibili.com"  # 私信在 vc 域 (session_svr / svr_sync / web_im)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_BYTES = 2 * 1024 * 1024
COOKIE_KEYS = ("SESSDATA", "buvid3", "buvid4")  # 读字幕够用的; bili_jct (写操作的令牌) 不带
COOKIE_MAX = 8192
WBI_TTL_S = 6 * 3600  # WBI 的两把 key 每天换, 6 小时取一次
# WBI 签名的重排表 (bilibili-API-collect docs/misc/sign/wbi.md)
MIXIN = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
         41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
         20, 34, 44, 52)


class BiliError(RuntimeError):
    """B 站接口没拿到: 网络、超时、风控、视频不存在."""


class BiliPostError(BiliError):
    """评论没发出去. code: B 站回的错误码 (12015 要人机验证, -101 没登录, 12002 评论区关了, -412 / -509 风控)."""

    def __init__(self, code, message: str):
        super().__init__(f"code {code}: {message}")
        self.code = code


POST_KEYS = ("SESSDATA", "bili_jct", "buvid3", "buvid4", "DedeUserID")  # 发评论要的 (bili_jct 是 csrf)
STOP_CODES = {12015, -101, -111, -352, -412, -509, 12051}  # 碰到这些当天不再发 (验证码、没登录、csrf 不对、风控、太频繁)


def parse_post_cookie(text: str) -> tuple[str, str]:
    """她 B 站号的 Cookie 文件 -> (Cookie 头, csrf). 没有 SESSDATA 或 bili_jct 就是 ("", "")."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    pairs: dict[str, str] = {}
    for ln in lines:
        for part in ln.split(";"):
            k, eq, v = part.partition("=")
            if eq and k.strip() in POST_KEYS and _cookie_value_ok(v.strip()):
                pairs[k.strip()] = v.strip()
    if "SESSDATA" not in pairs or "bili_jct" not in pairs:
        return "", ""
    return "; ".join(f"{k}={pairs[k]}" for k in POST_KEYS if k in pairs), pairs["bili_jct"]


def read_post_cookie(path: Path) -> tuple[str, str]:
    try:
        with open(path, "rb") as f:
            raw = f.read(COOKIE_MAX)
    except OSError:
        return "", ""
    text = raw.decode("utf-8-sig", errors="replace")
    if is_vault_marker(text):  # Cookie 在账号保管箱里: 发评论 / 点赞 / 回私信是"write"用途, 主人没点头就拿不到
        return parse_post_cookie(_from_vault("write"))
    return parse_post_cookie(text)


class BiliLoginError(BiliError):
    """带了 Cookie, 字幕接口还说要登录: Cookie 过期了 (或者存错了)."""


# 账号保管箱 (vault.py): bili_login.py 扫码登录以后, bili_cookie.txt 里只剩一个记号, 真正的 Cookie 加密存在保管箱里,
# 读它要过用途的许可 (read: 读字幕、看消息中心和私信; write: 发评论、点赞、回私信) 并留一条使用记录. 管理器建好保管箱后 use_vault 一下.
VAULT_MARK = "vault:bilibili"
_vault = None


def use_vault(vault) -> None:
    global _vault
    _vault = vault


def is_vault_marker(text: str) -> bool:
    return any(ln.strip().lstrip("#").strip() == VAULT_MARK for ln in str(text or "").splitlines())


def _from_vault(purpose: str) -> str:
    """保管箱里 B 站 Cookie 的全文; 没存、用途没点头 (会弹申请卡片)、解不开: 空串."""
    if _vault is None:
        return ""
    lease = _vault.lease("bilibili", purpose, by="bili")
    if not lease.ok:
        return ""
    text = lease.get("cookie")
    lease.done(bool(text), "读了登录凭证" if text else "里面没有登录凭证", quiet_s=300)
    return text


def _cookie_value_ok(v: str) -> bool:
    return bool(v) and v.isascii() and v.isprintable() and not any(c in ' ;"\\' for c in v)


def parse_cookie(text: str) -> str:
    """Cookie 文件的内容 -> 要带的 Cookie 头 (只留 COOKIE_KEYS, 没有 SESSDATA 就是空串).
    认三种写法: 浏览器里整段复制的 "a=1; b=2"、一行一个 name=value、只有 SESSDATA 的值. # 开头的行不算."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    pairs: dict[str, str] = {}
    for ln in lines:
        for part in ln.split(";"):
            k, eq, v = part.partition("=")
            if eq and k.strip() in COOKIE_KEYS and _cookie_value_ok(v.strip()):
                pairs[k.strip()] = v.strip()
    if not pairs and len(lines) == 1 and "=" not in lines[0] and _cookie_value_ok(lines[0]):
        pairs["SESSDATA"] = lines[0]
    if "SESSDATA" not in pairs:
        return ""
    return "; ".join(f"{k}={pairs[k]}" for k in COOKIE_KEYS if k in pairs)


def read_cookie(path: Path) -> str:
    """读你存的 Cookie 文件 (最多 8 KB). 没有文件、读不了就是空串. 文件里只有保管箱的记号时去保管箱取 (读字幕、看消息是"read"用途)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(COOKIE_MAX)
    except OSError:
        return ""
    text = raw.decode("utf-8-sig", errors="replace")
    if is_vault_marker(text):
        return parse_cookie(_from_vault("read"))
    return parse_cookie(text)


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN if i < len(raw))[:32]


def wbi_sign(params: dict, key: str, wts: int) -> dict:
    """WBI 签名: 加 wts, 按键排序, 值里去掉 !'()*, urlencode 后接上 mixin key 取 md5 当 w_rid."""
    p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted({**params, "wts": wts}.items())}
    p["w_rid"] = hashlib.md5((urllib.parse.urlencode(p) + key).encode("utf-8")).hexdigest()
    return p


def _sub_rank(s: dict) -> int:
    lan = str(s.get("lan") or "")
    return 0 if lan in ("zh-CN", "zh-Hans", "zh") else 1 if lan.startswith("zh") else 2 if lan == "ai-zh" else 3


class BiliClient:
    def __init__(self, base: str = BASE, timeout_s: float = 10.0):
        self.base = base.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._wbi: tuple[str, float] | None = None  # (mixin key, 取到的时间)

    def cookie_ok(self) -> bool:
        """Cookie 只发给真的 B 站 (https://api.bilibili.com), 或者测试用的本机替身."""
        u = urllib.parse.urlsplit(self.base)
        return (u.scheme == "https" and u.hostname == "api.bilibili.com") or u.hostname in ("127.0.0.1", "localhost", "::1")

    def _cdn_ok(self, url: str) -> bool:
        u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.base)
        host = u.hostname or ""
        if u.scheme == "https" and (host.endswith(".hdslb.com") or host.endswith(".bilibili.com")):
            return True
        return (u.scheme, host, u.port) == (b.scheme, b.hostname, b.port)

    async def _fetch(self, url: str, params: dict, headers: dict):
        raw = await self._raw(url, params, headers)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise BiliError("回应不是 JSON") from None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                                                  headers={"User-Agent": UA, "Accept": "application/json"})
        return self._session

    @staticmethod
    async def _body(resp, max_bytes: int) -> bytes:
        chunks, size = [], 0  # 读到底 (content.read(n) 只给当前到手的一块), 超过 max_bytes 就停
        async for chunk in resp.content.iter_chunked(65536):
            size += len(chunk)
            if size > max_bytes:
                raise BiliError("回应太大")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _raw(self, url: str, params: dict, headers: dict, max_bytes: int = MAX_BYTES) -> bytes:
        try:
            async with self._http().get(url, params=params, headers=headers, allow_redirects=False) as resp:
                if resp.status != 200:
                    raise BiliError(f"HTTP {resp.status}")
                return await self._body(resp, max_bytes)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise BiliError(f"{type(exc).__name__}: {exc}") from None

    async def _get(self, path: str, referer: str = "https://www.bilibili.com/", ok: tuple = (0,), cookie: str = "",
                   **params) -> dict | list:
        headers = {"Referer": referer, **({"Cookie": cookie} if cookie else {})}
        body = await self._fetch(self.base + path, params, headers)
        if not isinstance(body, dict) or body.get("code") not in ok:
            raise BiliError(f"code {body.get('code') if isinstance(body, dict) else '?'}: "
                            f"{body.get('message') if isinstance(body, dict) else ''}")
        return body.get("data") or {}

    async def _post(self, path: str, form: dict, cookie: str, csrf: str, referer: str) -> dict:
        """她的号的写操作 (发评论、点赞、点踩): 只 POST 给 B 站 (或本机替身), 不跟随跳转; 出错抛 BiliPostError."""
        if not cookie or not csrf or not self.cookie_ok():
            raise BiliPostError("no_cookie", "没有她的 Cookie, 或者 bili_base 不是 B 站")
        headers = {"Cookie": cookie, "Referer": referer, "Origin": "https://www.bilibili.com"}
        try:
            async with self._http().post(self.base + path, data={**form, "csrf": csrf}, headers=headers,
                                         allow_redirects=False) as resp:
                if resp.status != 200:
                    raise BiliPostError(resp.status, "HTTP 错误")
                body = json.loads((await self._body(resp, MAX_BYTES)).decode("utf-8"))
        except BiliPostError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, BiliError) as exc:
            raise BiliPostError("net", f"{type(exc).__name__}: {exc}") from None
        if not isinstance(body, dict) or body.get("code") != 0:
            code = body.get("code") if isinstance(body, dict) else "?"
            raise BiliPostError(code, str(body.get("message") if isinstance(body, dict) else ""))
        return body.get("data") or {}

    async def post_comment(self, aid, bvid: str, message: str, cookie: str, csrf: str, root: str = "",
                           parent: str = "") -> str:
        """用她的号在视频 (aid) 下面发一条评论, 返回评论号 rpid. root: 回复评论区里的某一条 (楼的 rpid; 对方会收到提醒),
        parent: 楼里具体回的是哪一条 (不给就是 root)."""
        form = {"type": "1", "oid": str(aid), "message": message, "plat": "1",
                **({"root": str(root), "parent": str(parent or root)} if root else {})}
        data = await self._post("/x/v2/reply/add", form, cookie, csrf,
                                f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/")
        return str(data.get("rpid") or "")

    async def like_comment(self, aid, rpid: str, cookie: str, csrf: str, hate: bool = False) -> None:
        """给视频 (aid) 下面的一条评论点赞 (hate: 点踩)."""
        await self._post("/x/v2/reply/hate" if hate else "/x/v2/reply/action",
                         {"type": "1", "oid": str(aid), "rpid": str(rpid), "action": "1"}, cookie, csrf,
                         "https://www.bilibili.com/")

    async def like_video(self, aid, bvid: str, cookie: str, csrf: str) -> None:
        await self._post("/x/web-interface/archive/like", {"aid": str(aid), "like": "1"}, cookie, csrf,
                         f"https://www.bilibili.com/video/{bvid}")

    async def inbox(self, cookie: str) -> list[dict]:
        """她的消息中心里"回复我的"和"@我的" (各最新一页): [{id, kind: reply / at, mid, name, text, oid, type, root, rpid,
        title, mine (她原来那条), time}]. 只留视频下面的评论 (type 1)."""
        out = []
        for kind, path in (("reply", "/x/msgfeed/reply"), ("at", "/x/msgfeed/at")):
            data = await self._get(path, "https://message.bilibili.com/", cookie=cookie,
                                   platform="web", build=0, mobi_app="web")
            for it in data.get("items") or []:
                if not isinstance(it, dict):
                    continue
                user, item = it.get("user") or {}, it.get("item") or {}
                if int(item.get("business_id") or 0) != 1 or not item.get("source_id") or not item.get("subject_id"):
                    continue
                out.append({"id": f"{kind}:{it.get('id') or item.get('source_id')}", "kind": kind,
                            "mid": str(user.get("mid") or ""), "name": str(user.get("nickname") or ""),
                            "text": str(item.get("source_content") or ""), "oid": str(item["subject_id"]), "type": 1,
                            "root": str(item.get("root_id") or ""), "rpid": str(item["source_id"]),
                            "title": str(item.get("title") or ""),
                            "mine": str(item.get("target_reply_content") or item.get("root_reply_content") or ""),
                            "time": int(it.get("reply_time") or it.get("at_time") or 0)})
        return out

    async def me(self, cookie: str) -> str:
        """她自己 B 站号的 uid (发私信要当 sender_uid): 登录了就返回 mid, 没登录空串."""
        data = await self._get("/x/web-interface/nav", referer="https://message.bilibili.com/", cookie=cookie)
        return str((data or {}).get("mid") or "")

    # ---- 私信 (2026-10-30 用户: "B站的私信、at、以及推荐的视频, 你要主动看、主动回复") ----
    async def _get_vc(self, path: str, params: dict, cookie: str) -> dict:
        """vc 域上的只读接口 (私信): 只认 code 0, 别的抛 BiliError."""
        headers = {"Referer": "https://message.bilibili.com/", "Cookie": cookie}
        body = await self._fetch(VC_BASE + path, params, headers)
        if not isinstance(body, dict) or body.get("code") != 0:
            raise BiliError("code %s: %s" % (body.get("code") if isinstance(body, dict) else "?",
                                             body.get("message") if isinstance(body, dict) else ""))
        return body.get("data") or {}

    async def private_sessions(self, cookie: str, count: int = 20) -> list[dict]:
        """私信会话列表 (只留和真人的, 不要系统号): [{talker, unread, ts}]."""
        data = await self._get_vc("/session_svr/v1/session_svr/get_sessions",
                                  {"session_type": 1, "group_id": 0, "count": count, "build": 0, "mobi_app": "web"},
                                  cookie)
        out = []
        for it in data.get("session_list") or []:
            if not isinstance(it, dict) or int(it.get("session_type") or 1) != 1:
                continue
            ack = int(it.get("ack_seqno") or 0)
            out.append({"talker": str(it.get("talker_id") or ""), "unread": int(it.get("unread_count") or 0),
                        "ts": float(it.get("session_ts") or 0) / 1e6, "ack": ack})
        return out

    async def private_messages(self, cookie: str, mid: str, talker: str, size: int = 20) -> list[dict]:
        """和某个人的私信 (旧到新): [{key, mid (谁发的), time, kind, text, title}].
        kind 1 = 文字, 2 = 图, 7 = 分享的视频; 别的一律当看不懂."""
        data = await self._get_vc("/svr_sync/v1/svr_sync/fetch_session_msgs",
                                  {"sender_uid": str(mid), "talker_id": str(talker), "session_type": 1, "size": size},
                                  cookie)
        out = []
        for m in data.get("messages") or []:
            if not isinstance(m, dict):
                continue
            try:
                content = json.loads(m.get("content") or "{}")
            except ValueError:
                content = {}
            kind = int(m.get("msg_type") or 0)
            title = str(content.get("title") or "")
            text = (str(content.get("content") or "") if kind == 1 else
                    "分享了一个视频《%s》" % title if kind == 7 and title else
                    "发了一张图片" if kind == 2 else "")
            out.append({"key": str(m.get("msg_key") or m.get("msg_seqno") or ""), "mid": str(m.get("sender_uid") or ""),
                        "time": int(m.get("timestamp") or 0), "kind": kind, "text": text, "title": title})
        out.sort(key=lambda x: x["time"])
        return out

    async def send_private(self, cookie: str, csrf: str, mid: str, talker: str, text: str) -> str:
        """用她的号给 talker 发一条私信. code 12015 要人机验证、-101 没登录、风控都抛 BiliPostError."""
        if not cookie or not csrf or not self.cookie_ok():
            raise BiliPostError("no_cookie", "没有她的 Cookie, 或者 bili_base 不是 B 站")
        form = {"msg[sender_uid]": str(mid), "msg[receiver_id]": str(talker), "msg[receiver_type]": "1",
                "msg[msg_type]": "1", "msg[msg_status]": "0", "msg[timestamp]": str(int(time.time())),
                "msg[dev_id]": secrets.token_hex(8).upper(),
                "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
                "csrf": csrf, "csrf_token": csrf}
        headers = {"Cookie": cookie, "Referer": "https://message.bilibili.com/",
                   "Origin": "https://message.bilibili.com"}
        try:
            async with self._http().post(VC_BASE + "/web_im/v1/web_im/send_msg", data=form, headers=headers,
                                         allow_redirects=False) as resp:
                if resp.status != 200:
                    raise BiliPostError(resp.status, "HTTP 错误")
                body = json.loads((await self._body(resp, MAX_BYTES)).decode("utf-8"))
        except BiliPostError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, BiliError) as exc:
            raise BiliPostError("net", "%s: %s" % (type(exc).__name__, exc)) from None
        if not isinstance(body, dict) or body.get("code") != 0:
            raise BiliPostError(body.get("code") if isinstance(body, dict) else "?",
                                str(body.get("message") if isinstance(body, dict) else ""))
        return str((body.get("data") or {}).get("msg_key") or "")

    async def popular(self, n: int = 30) -> list[dict]:
        """热门视频: [{bvid, aid, title, tname, owner, duration}]."""
        data = await self._get("/x/web-interface/popular", ps=max(1, min(n, 50)), pn=1)
        out = []
        for v in data.get("list") or []:
            if isinstance(v, dict) and str(v.get("bvid", "")).startswith("BV"):
                out.append({"bvid": v["bvid"], "aid": v.get("aid"), "title": str(v.get("title") or ""),
                            "tname": str(v.get("tname") or ""), "owner": str((v.get("owner") or {}).get("name") or ""),
                            "duration": int(v.get("duration") or 0)})
        return out

    async def view(self, bvid: str) -> dict:
        """视频详情: {bvid, aid, cid (第一 P), pic (封面), title, desc, tname, owner, duration}."""
        v = await self._get("/x/web-interface/view", bvid=bvid)
        return {"bvid": str(v.get("bvid") or bvid), "aid": v.get("aid"), "cid": int(v.get("cid") or 0),
                "pic": str(v.get("pic") or ""),
                "title": str(v.get("title") or ""), "desc": str(v.get("desc") or ""), "tname": str(v.get("tname") or ""),
                "owner": str((v.get("owner") or {}).get("name") or ""), "duration": int(v.get("duration") or 0)}

    async def tags(self, bvid: str) -> list[str]:
        data = await self._get("/x/tag/archive/tags", bvid=bvid)
        return [str(t.get("tag_name") or "") for t in data if isinstance(t, dict)][:10] if isinstance(data, list) else []

    async def hot_comments(self, aid, n: int = 5) -> list[str]:
        """按热度排的几条评论正文 (sort=1)."""
        return [c["msg"] for c in await self.comments(aid, n)]

    async def comments(self, aid, n: int = 20, subs: int = 2) -> list[dict]:
        """评论区 (按热度, sort=1): [{rpid, mid, name, msg, like, rcount, subs: [{name, msg}]}], 每条带前几条楼中楼.
        (2026-10-25 用户: "允许她读评论、看评论, 毕竟评论可是很好的学习、建议、了解世界的一种方式")"""
        data = await self._get("/x/v2/reply", type=1, oid=aid, sort=1, ps=max(1, min(n, 20)), pn=1)
        out = []
        for r in data.get("replies") or []:
            if not isinstance(r, dict) or not ((r.get("content") or {}).get("message")):
                continue
            m = r.get("member") or {}
            out.append({"rpid": str(r.get("rpid") or ""), "mid": str(m.get("mid") or r.get("mid") or ""),
                        "name": str(m.get("uname") or ""), "msg": str(r["content"]["message"]),
                        "like": int(r.get("like") or 0), "rcount": int(r.get("rcount") or 0),
                        "subs": [{"name": str((x.get("member") or {}).get("uname") or ""),
                                  "msg": str((x.get("content") or {}).get("message") or "")}
                                 for x in (r.get("replies") or [])[:subs] if isinstance(x, dict)]})
        return out[:n]

    async def emotes(self, cookie: str) -> list[str]:
        """评论里能用的表情 (她的号的表情面板: 小黄脸、小电视、热词……): ["[doge]", "[笑哭]", …]. 写进评论的这串字 B 站会显示成表情.
        没登录也有默认的几套; 拿不到返回空."""
        if not self.cookie_ok():
            return []
        data = await self._get("/x/emote/user/panel/web", cookie=cookie, business="reply")
        out = []
        for pk in data.get("packages") or []:
            for e in (pk or {}).get("emote") or []:
                t = str((e or {}).get("text") or "")
                if t.startswith("[") and t.endswith("]") and len(t) <= 24:
                    out.append(t)
        return list(dict.fromkeys(out))

    async def _wbi_key(self) -> str:
        """WBI 签名用的 mixin key: 从 /x/web-interface/nav 的 wbi_img 拿 (不带 Cookie; 没登录时 code 是 -101, 照样有)."""
        now = time.time()
        if self._wbi and now - self._wbi[1] < WBI_TTL_S:
            return self._wbi[0]
        img = (await self._get("/x/web-interface/nav", ok=(0, -101))).get("wbi_img") or {}
        stem = [Path(urllib.parse.urlsplit(str(img.get(k) or "")).path).stem for k in ("img_url", "sub_url")]
        if not all(stem):
            raise BiliError("拿不到 WBI 签名的 key")
        self._wbi = (mixin_key(*stem), now)
        return self._wbi[0]

    async def subtitles(self, aid, cid: int, bvid: str, cookie: str, max_chars: int = 1500) -> tuple[str, str]:
        """字幕正文 (连成一段, 最多 max_chars 字) 和语言; 视频没有字幕时是 ("", "").
        cookie 只带在拿字幕列表的那个请求上; 字幕文件在 CDN 上, 不带."""
        if not cookie or not self.cookie_ok():
            raise BiliError("没有 Cookie, 或者 bili_base 不是 B 站 (不带 Cookie)")
        params = wbi_sign({"aid": aid, "cid": cid}, await self._wbi_key(), int(time.time()))
        data = await self._get("/x/player/wbi/v2", referer=f"https://www.bilibili.com/video/{bvid}", cookie=cookie, **params)
        if data.get("need_login_subtitle"):
            raise BiliLoginError("字幕接口说要登录: Cookie 过期了, 或者存的不是 SESSDATA")
        subs = [s for s in ((data.get("subtitle") or {}).get("subtitles") or []) if isinstance(s, dict) and s.get("subtitle_url")]
        if not subs:
            return "", ""
        pick = min(subs, key=_sub_rank)
        url = str(pick["subtitle_url"])
        url = "https:" + url if url.startswith("//") else url
        if not self._cdn_ok(url):
            raise BiliError("字幕地址不在 B 站的域名下, 不读")
        body = await self._fetch(url, {}, {"Referer": "https://www.bilibili.com/"})
        lines = [str(x.get("content") or "").strip() for x in (body.get("body") or []) if isinstance(x, dict)] \
            if isinstance(body, dict) else []
        return " ".join(ln for ln in lines if ln)[:max_chars], str(pick.get("lan") or "")

    async def image(self, url: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        """B 站 CDN 上的一张图 (封面、截图拼图). 地址不在 B 站的域名下就不读."""
        url = "https:" + url if url.startswith("//") else url
        if url.startswith("http://") and urllib.parse.urlsplit(url).hostname.endswith(".hdslb.com"):
            url = "https://" + url[len("http://"):]
        if not self._cdn_ok(url):
            raise BiliError("图片地址不在 B 站的域名下, 不读")
        return await self._raw(url, {}, {"Referer": "https://www.bilibili.com/"}, max_bytes)

    async def snapshot(self, bvid: str, cid: int, n: int = 6) -> bytes | None:
        """在 5%–85% 的时间里均匀挑 n 帧截图 (片头片尾常是黑的), 拼成一张 JPEG (3 列). 这个视频没有截图时返回 None."""
        d = await self._get("/x/player/videoshot", bvid=bvid, cid=cid, index=1)
        sheets = [str(u) for u in d.get("image") or [] if u]
        x, y = int(d.get("img_x_len") or 0), int(d.get("img_y_len") or 0)
        w, h = int(d.get("img_x_size") or 0), int(d.get("img_y_size") or 0)
        if not sheets or not (x and y and w and h):
            return None
        per = x * y
        total = min(len(d.get("index") or []) or per * len(sheets), per * len(sheets))
        picks = sorted({min(total - 1, int(total * (0.05 + 0.8 * i / max(1, n - 1)))) for i in range(n)})
        sprites = {s: await self.image(sheets[s]) for s in sorted({p // per for p in picks})}
        return await asyncio.to_thread(contact_sheet, sprites, [(p // per, p % per) for p in picks], x, w, h)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
