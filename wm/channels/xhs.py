"""小红书只读客户端: 首页推荐里挑一篇笔记, 读标题、作者、正文、标签和首屏评论. 给"她自己的生活"里的"看笔记"用 (manager._xhs_watch).

- 只发 GET, 只连 base (默认 https://www.xiaohongshu.com) 这一个域名; 不走系统代理 (trust_env=False); 不跟随跳转;
  不存回应里的 Cookie (DummyCookieJar); 每个请求有超时, 回应最多读 4 MB;
- 正文和首屏评论都在页面里的 `window.__INITIAL_STATE__` (SSR 直出), 不是 XHR, 也不需要签名; 只有这一屏, 不翻页;
- 要登录时: 你自己登录小红书网页版, 把 Cookie 存在本机文件里 (read_cookie), 只发给 xiaohongshu.com 的域名;
  不发评论、不点赞: 网页版这些写接口要 X-s / X-t 签名、xsec_token 和风控验证, 这里不逆向、不绕验证, 直接抛 XhsPostError;
- 读到的都是外部内容: 由管理器包上边界交给"隔离读者"概括, 不直接进她的会话.

资料 (2026-10-31 查的公开资料):
- 首页/发现页的推荐在 `window.__INITIAL_STATE__.feed.feeds`; 笔记页在 `window.__INITIAL_STATE__.note.noteDetailMap`;
- 评论首屏也在同一份 noteDetailMap 的 comments 里; 继续翻页的 `/api/sns/web/v2/comment/page` 要签名, 不碰;
- 公开的 `__INITIAL_STATE__` 解析方式参考 xhs-read-mcp (MIT) 和 opencli 的 xiaohongshu 站点说明.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

import aiohttp

BASE = "https://www.xiaohongshu.com"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
MAX_BYTES = 4 * 1024 * 1024
COOKIE_MAX = 8192
# 网页版登录和过风控要用的几个; 别的 (埋点、广告) 都不带
COOKIE_KEYS = ("web_session", "a1", "webId", "gid", "xsecappid", "customerClientId")
NOTE_ID = re.compile(r"^[0-9A-Za-z_-]{4,64}$")


class XhsError(RuntimeError):
    """小红书页面没拿到: 网络、超时、跳到登录/验证页、笔记删了、页面结构变了."""


class XhsPostError(XhsError):
    """写操作没做. code: signature_required (网页版要 X-s 签名 / xsec_token / 人机验证, 这里不硬破)."""

    def __init__(self, code, message: str):
        super().__init__(f"code {code}: {message}")
        self.code = code


def _cookie_value_ok(v: str) -> bool:
    return bool(v) and v.isascii() and v.isprintable() and not any(c in ' ;"\\' for c in v)


def parse_cookie(text: str) -> str:
    """Cookie 文件的内容 -> Cookie 头 (只留 COOKIE_KEYS; 认 "a=1; b=2"、一行一个、只有值三种写法).
    没有认得的 key 就是空串. # 开头的行不算."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    pairs: dict[str, str] = {}
    for ln in lines:
        for part in ln.split(";"):
            k, eq, v = part.partition("=")
            if eq and k.strip() in COOKIE_KEYS and _cookie_value_ok(v.strip()):
                pairs[k.strip()] = v.strip()
    if not pairs and len(lines) == 1 and "=" not in lines[0] and _cookie_value_ok(lines[0]):
        pairs["web_session"] = lines[0]
    return "; ".join(f"{k}={pairs[k]}" for k in COOKIE_KEYS if k in pairs)


def read_cookie(path: Path) -> str:
    """读你存的 Cookie 文件 (最多 8 KB). 没有文件、读不了就是空串."""
    try:
        with open(path, "rb") as f:
            raw = f.read(COOKIE_MAX)
    except OSError:
        return ""
    return parse_cookie(raw.decode("utf-8-sig", errors="replace"))


def _unwrap(value):
    """Vue / Pinia 的 ref 会包一层 {value: ...} 或 {_value: ...}; 一直拆到真的数据."""
    seen = 0
    while isinstance(value, dict) and seen < 4:
        nxt = value.get("value")
        if nxt is None:
            nxt = value.get("_value")
        if nxt is None:
            nxt = value.get("_rawValue")
        if nxt is None or nxt is value:
            break
        value, seen = nxt, seen + 1
    return value


def initial_state(html: str) -> dict:
    """页面里的 window.__INITIAL_STATE__ -> dict. 没有 (跳到了登录/验证页、页面改版) 抛 XhsError."""
    key = "window.__INITIAL_STATE__"
    i = html.find(key)
    if i < 0:
        raise XhsError("页面里没有笔记资料 (可能要登录、碰到验证页, 或者页面改版了)")
    j = html.find("</script>", i)
    raw = html[i:j if j > i else len(html)].partition("=")[2].strip().rstrip(";").strip()
    if not raw:
        raise XhsError("页面里的笔记资料是空的")
    try:
        state = json.loads(raw.replace("undefined", "null"))
    except ValueError:
        raise XhsError("页面里的笔记资料解析不了") from None
    if not isinstance(state, dict):
        raise XhsError("页面里的笔记资料不是对象")
    return state


def _as_items(value) -> list[dict]:
    value = _unwrap(value)
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        if isinstance(value.get("items"), list):
            return [x for x in value["items"] if isinstance(x, dict)]
        return [x for x in value.values() if isinstance(x, dict)]
    return []


def feed_items(state: dict) -> list[dict]:
    """首页推荐里的笔记 [{id, xsec_token, title, author, type}]. 认 feed.feeds 为主, 别的版本兜底."""
    for root in ("feed", "homefeed", "explore"):
        node = state.get(root)
        if not isinstance(node, dict):
            continue
        for key in ("feeds", "items", "notes"):
            items = _as_items(node.get(key))
            if items:
                return [n for n in (_feed_note(it) for it in items) if n["id"]]
    return [n for n in (_feed_note(it) for it in _as_items(state.get("feeds"))) if n["id"]]


def _feed_note(item: dict) -> dict:
    """一条推荐 -> 统一的笔记形状. 接口字段是 internal 的, 几种写法都认."""
    card = item.get("noteCard") or item.get("note_card") or item.get("note") or item
    if not isinstance(card, dict):
        card = item
    user = card.get("user") or card.get("userInfo") or {}
    nid = card.get("noteId") or card.get("note_id") or card.get("id") or item.get("id") or item.get("noteId")
    token = item.get("xsecToken") or item.get("xsec_token") or card.get("xsecToken") or card.get("xsec_token")
    title = card.get("displayTitle") or card.get("display_title") or card.get("title") or ""
    return {"id": str(nid or ""), "xsec_token": str(token or ""), "title": str(title),
            "author": str(user.get("nickname") or user.get("nickName") or user.get("name") or ""),
            "type": str(card.get("type") or item.get("type") or "")}


def _detail(state: dict, note_id: str) -> tuple[dict, dict]:
    """__INITIAL_STATE__ -> (note, comments). 认 note.noteDetailMap 为主, 旧的 noteData.data 兜底."""
    node = state.get("note")
    detail = {}
    if isinstance(node, dict):
        dmap = node.get("noteDetailMap")
        if isinstance(dmap, dict) and dmap:
            detail = dmap.get(note_id) or (next(iter(dmap.values())) if len(dmap) == 1 else {})
    if isinstance(detail, dict) and (detail.get("note") or detail.get("comments")):
        return detail.get("note") or {}, _unwrap(detail.get("comments") or {})
    data = ((state.get("noteData") or {}).get("data") or {}) if isinstance(state.get("noteData"), dict) else {}
    note = data.get("noteData") if isinstance(data, dict) else {}
    comments = data.get("commentData") if isinstance(data, dict) else {}
    return (note if isinstance(note, dict) else {}), _unwrap(comments if isinstance(comments, dict) else {})


def _sub_comments(value) -> list[dict]:
    raw = value.get("comments") if isinstance(value, dict) else value
    subs = [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    out = []
    for sc in subs[:2]:
        user = sc.get("user") or sc.get("userInfo") or {}
        text = str(sc.get("content") or "")
        if text:
            out.append({"name": str(user.get("nickname") or user.get("nickName") or ""), "text": text})
    return out


def _comments(value, n: int) -> list[dict]:
    raw = value.get("comments") if isinstance(value, dict) else value
    out = []
    for c in (raw if isinstance(raw, list) else [])[:n]:
        if not isinstance(c, dict):
            continue
        user = c.get("user") or c.get("userInfo") or {}
        text = str(c.get("content") or "")
        if not text:
            continue
        try:
            like = int(c.get("likeCount") or c.get("like_count") or 0)
        except (TypeError, ValueError):
            like = 0
        out.append({"id": str(c.get("id") or ""), "name": str(user.get("nickname") or user.get("nickName") or ""),
                    "text": text, "like": like, "subs": _sub_comments(c.get("subComments") or c.get("sub_comments"))})
    return out


def _tags(note: dict) -> list[str]:
    out = []
    for t in note.get("tagList") or note.get("tags") or []:
        if isinstance(t, dict):
            name = str(t.get("name") or t.get("tagName") or "").strip()
        else:
            name = str(t).strip()
        if name:
            out.append(name)
    if not out:
        out = re.findall(r"#([^#\s]{1,20})", str(note.get("desc") or ""))
    return list(dict.fromkeys(out))[:10]


def _count(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class XhsClient:
    def __init__(self, base: str = BASE, timeout_s: float = 10.0):
        self.base = base.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    def cookie_ok(self) -> bool:
        """Cookie 只发给真的小红书 (https 的 *.xiaohongshu.com), 或者测试用的本机替身."""
        u = urllib.parse.urlsplit(self.base)
        host = u.hostname or ""
        return (u.scheme == "https" and (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))) or \
            host in ("127.0.0.1", "localhost", "::1")

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, trust_env=False,
                                                   cookie_jar=aiohttp.DummyCookieJar(),
                                                   headers={"User-Agent": UA, "Accept": "text/html,application/json"})
        return self._session

    @staticmethod
    async def _body(resp, max_bytes: int) -> bytes:
        chunks, size = [], 0  # 读到底, 超过 max_bytes 就停 (页面再大也不往内存里塞)
        async for chunk in resp.content.iter_chunked(65536):
            size += len(chunk)
            if size > max_bytes:
                raise XhsError("回应太大")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _raw(self, url: str, params: dict, cookie: str = "", max_bytes: int = MAX_BYTES) -> bytes:
        """只 GET, 不跟随跳转; 302 到登录/验证页也算没拿到. Cookie 只在 cookie_ok 时带."""
        headers = {"Referer": self.base + "/", "Accept-Language": "zh-CN,zh;q=0.9"}
        if cookie and self.cookie_ok():
            headers["Cookie"] = cookie
        try:
            async with self._http().get(url, params=params, headers=headers, allow_redirects=False) as resp:
                if resp.status != 200:
                    raise XhsError(f"HTTP {resp.status}")
                return await self._body(resp, max_bytes)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise XhsError(f"{type(exc).__name__}: {exc}") from None

    async def _state(self, path: str, params: dict, cookie: str) -> dict:
        raw = await self._raw(self.base + path, params, cookie)
        return initial_state(raw.decode("utf-8", errors="replace"))

    async def homefeed(self, cookie: str, n: int = 20) -> list[dict]:
        """首页/发现页的推荐笔记 (第一屏): [{id, xsec_token, title, author, type}]. 要登录/验证页时抛 XhsError."""
        last = None
        for path, params in (("/explore", {"channel_id": "homefeed_recommend"}), ("/", {})):
            try:
                items = feed_items(await self._state(path, params, cookie))
            except XhsError as exc:
                last = exc
                continue
            if items:
                return items[:max(1, n)]
        raise last or XhsError("首页里没有笔记")

    async def note(self, note_id: str, xsec_token: str = "", cookie: str = "", comments: int = 15) -> dict:
        """一篇笔记: {id, title, desc, author, tags, likes, collects, comment_count, comments, url}.
        评论区只有页面里直出的首屏 (最多 comments 条), 不翻页; xsec_token 从首页推荐里带上, 没有也能试."""
        if not NOTE_ID.fullmatch(str(note_id or "")):
            raise XhsError("笔记号不对")
        params = {"xsec_source": "pc_feed"}
        if xsec_token:
            params["xsec_token"] = xsec_token
        state = await self._state(f"/explore/{note_id}", params, cookie)
        note, cmt = _detail(state, note_id)
        if not (note.get("title") or note.get("desc")):
            raise XhsError("笔记没打开 (可能要登录、要 xsec_token, 或者被删了)")
        info = note.get("interactInfo") or note.get("interact_info") or {}
        user = note.get("user") or {}
        return {"id": str(note.get("noteId") or note.get("note_id") or note_id),
                "title": str(note.get("title") or ""), "desc": str(note.get("desc") or ""),
                "author": str(user.get("nickname") or user.get("nickName") or ""), "tags": _tags(note),
                "likes": _count(info.get("likedCount") or info.get("liked_count")),
                "collects": _count(info.get("collectedCount") or info.get("collected_count")),
                "comment_count": _count(info.get("commentCount") or info.get("comment_count")),
                "comments": _comments(cmt, max(1, comments)),
                "url": f"https://www.xiaohongshu.com/explore/{note_id}"}

    async def post_comment(self, note_id: str, message: str, cookie: str, xsec_token: str = "", root: str = "") -> str:
        """不硬破: 网页版评论要 X-s / X-t 签名、xsec_token 和风控验证, 这里不发请求, 直接说做不到."""
        raise XhsPostError("signature_required",
                           "小红书网页版发评论要 X-s/X-t 签名、xsec_token 和风控验证, 没有公开的写接口")

    async def like_note(self, note_id: str, cookie: str, unlike: bool = False) -> None:
        """不硬破: 网页版点赞同样要签名和登录态, 这里不发请求, 直接说做不到."""
        raise XhsPostError("signature_required",
                           "小红书网页版点赞要 X-s/X-t 签名和登录态, 没有公开的写接口")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
