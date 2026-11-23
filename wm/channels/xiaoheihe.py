"""小黑盒只读客户端: 从公开的站点地图里挑一篇帖子, 读公开页里的标题、作者和正文. 给"她自己的生活"里的"看帖子"用 (manager._xhh_watch).

- 只发 GET, 只连 base (默认 https://www.xiaoheihe.cn) 这一个站; 不走系统代理 (trust_env=False); 不跟随跳转;
  不存回应里的 Cookie (DummyCookieJar); 每个请求有超时, 回应最多读 4 MB;
- 站点地图: robots.txt 里给了 https://www.xiaoheihe.cn/sitemap.xml, 按月分 (posts-YYYY-MM.xml),
  每个 URL 是 /app/bbs/link/<key>. 这些是平台自己放出来给搜索引擎的公开页, robots.txt 也写着 Allow: /app/bbs/link/;
- 帖子页: 有的帖子在 HTML 里直出 <article id="post-static"> (标题、作者、时间、正文、图片), 不登录、不签名就能读;
  没直出正文的只拿到一个空壳页面 (SPA), 当没打开, 换下一篇;
- 公开页只有正文, 没有评论. 评论列表、发评论、回复、@、点赞、消息中心都要网页版的 hkey 签名 (还要登录态),
  按规矩不硬破: 这些方法一个请求都不发, 直接抛 XhhPostError(signature_required);
- 要登录的 Cookie: 照 B 站的做法从 ~/.worldmodel/ 下的文件读 (read_cookie), 只认 pkey / heybox_id 这些;
  但现在能用的只有公开页, 不需要登录, 所以不发 Cookie (以后有了不签名的登录接口再用);
- 读到的都是外部内容: 由管理器包上边界交给"隔离读者"概括, 不直接进她的会话.

资料 (2026-11-10 查的公开资料):
- robots.txt / sitemap.xml: https://www.xiaoheihe.cn/robots.txt, https://www.xiaoheihe.cn/sitemap.xml;
- 帖子页的静态正文标记 website-post-seo:v1 / <article id="post-static">;
- 接口契约 (含 hkey、_time、nonce): xiaoheihe-cli 的 docs/api-contract.md、xhhBackCrack 的 README、
  xiaoheihe-mcp 的 client.py. 所有 /bbs/... 接口都先要 _time、nonce, 再要 hkey; 写接口还要 Workshop 签名.
  这里只读公开页, 不实现这些签名.
"""
from __future__ import annotations

import html as _html
import re
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

import aiohttp

BASE = "https://www.xiaoheihe.cn"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
MAX_BYTES = 4 * 1024 * 1024
COOKIE_MAX = 8192
# 网页版登录态里认得的几个 (别的埋点、广告都不带); 现在只用来读, 发请求时不带
COOKIE_KEYS = ("pkey", "heybox_id", "x_xhh_tokenid", "user_pkey")
POST_KEY = re.compile(r"^[0-9A-Za-z_-]{4,64}$")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_ARTICLE = re.compile(r"<article\b(?=[^>]*\bid=[\"']post-static[\"'])[^>]*>.*?</article>", re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class XhhError(RuntimeError):
    """小黑盒公开页没拿到: 网络、超时、页面没有直出正文、帖子删了、页面结构变了."""


class XhhPostError(XhhError):
    """写操作 (评论、回复、点赞) 和评论列表没做. code: signature_required (网页接口要 hkey 签名 / 登录, 这里不硬破)."""

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
        pairs["pkey"] = lines[0]
    return "; ".join(f"{k}={pairs[k]}" for k in COOKIE_KEYS if k in pairs)


def read_cookie(path: Path) -> str:
    """读你存的小黑盒 Cookie 文件 (最多 8 KB). 没有文件、读不了就是空串."""
    try:
        with open(path, "rb") as f:
            raw = f.read(COOKIE_MAX)
    except OSError:
        return ""
    return parse_cookie(raw.decode("utf-8-sig", errors="replace"))


class _PostParser(HTMLParser):
    """把 <article id="post-static"> 拆成标题、作者、时间、正文行和图片数. 别的标签 (脚本、样式) 都不看."""

    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "div"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_article = False
        self.in_time = False
        self.post_id = ""
        self.time = ""
        self.images = 0
        self.blocks: list[tuple[int, str, str]] = []  # (顺序, 标签, 文字)
        self._order = 0
        self._time_order: int | None = None
        self._tag: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "article":
            if not self.in_article and attrs.get("id") == "post-static":
                self.in_article, self.post_id = True, str(attrs.get("data-post-id") or "")
            return
        if not self.in_article:
            return
        tag = tag.lower()
        if tag == "time":
            self.in_time, self.time = True, str(attrs.get("datetime") or "")
            self._time_order = self._order
            self._order += 1  # 给时间留一个位置: 正文在它后面
            return
        if tag == "img":
            self.images += 1
            return
        if tag == "br":
            self._flush()
            return
        if tag in self.BLOCK:
            self._flush()
            self._tag = tag

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "article":
            self._flush()
            self.in_article = False
        elif self.in_article and tag == "time":
            self.in_time = False
        elif self.in_article and tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.in_article and not self.in_time:
            self._buf.append(data)

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text and self._tag:
            self.blocks.append((self._order, self._tag, text))
            self._order += 1
        self._tag = None


def parse_post(html: str, url: str = "", key: str = "") -> dict:
    """帖子公开页 -> {key, id, title, author, time, text, images, url, comments}.
    页面里没有 <article id="post-static"> (SPA 空壳、要登录、验证页、改版) 抛 XhhError.
    评论不在公开页里, 一律空 (要签名, 见 comments)."""
    found = _ARTICLE.search(html or "")
    if not found:
        raise XhhError("公开页里没有正文 (可能要登录、碰到验证页, 或者这篇没开公开页)")
    p = _PostParser()
    p.feed(found.group(0))
    p.close()
    title = next((t for _, tag, t in p.blocks if tag == "h1"), "")
    if not title:
        m = _TITLE.search(html or "")
        title = " ".join(_html.unescape(m.group(1) if m else "").split()).removesuffix(" - 小黑盒").strip()
    if not title:
        raise XhhError("公开页里没有标题")
    rest = [(o, tag, t) for o, tag, t in p.blocks if tag != "h1"]
    author, body = "", rest
    if p._time_order is not None:
        before = [(o, tag, t) for o, tag, t in rest if o < p._time_order]
        author = before[-1][2] if before else ""
        body = [(o, tag, t) for o, tag, t in rest if o > p._time_order]
    elif rest:
        author, body = rest[0][2], rest[1:]
    text = "\n".join(t for _, _, t in body)[:4000]
    return {"key": key or urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1],
            "id": p.post_id, "title": title, "author": author, "time": p.time, "text": text,
            "images": p.images, "url": url, "comments": []}


def post_urls(xml: str) -> list[dict]:
    """一份按月站点地图 -> [{key, url}]. 只认 /app/bbs/link/<key> 这种帖子地址."""
    out, seen = [], set()
    for loc in _LOC.findall(xml or ""):
        u = urllib.parse.urlsplit(_html.unescape(loc))
        parts = [x for x in u.path.split("/") if x]
        if len(parts) < 4 or parts[-3:-1] != ["bbs", "link"]:
            continue
        key = parts[-1]
        if POST_KEY.fullmatch(key) and key not in seen:
            seen.add(key)
            out.append({"key": key, "url": _html.unescape(loc)})
    return out


def month_urls(xml: str) -> list[str]:
    """sitemap.xml -> 各月的站点地图地址 (posts-YYYY-MM.xml), 从旧到新."""
    return [u for u in (_html.unescape(x) for x in _LOC.findall(xml or ""))
            if re.search(r"/posts-\d{4}-\d{2}\.xml$", u)]


class XhhClient:
    def __init__(self, base: str = BASE, timeout_s: float = 10.0):
        self.base = base.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._months: tuple[list[str], float] | None = None  # (各月站点地图, 取到的时间)
        self._month_cache: dict[str, tuple[list[dict], float]] = {}  # 某个月的帖子地址 (同样 6 小时)

    def cookie_ok(self) -> bool:
        """Cookie 只发给真的小黑盒 (https 的 xiaoheihe.cn / *.xiaoheihe.cn), 或者测试用的本机替身."""
        u = urllib.parse.urlsplit(self.base)
        host = u.hostname or ""
        return (u.scheme == "https" and (host == "xiaoheihe.cn" or host.endswith(".xiaoheihe.cn"))) or \
            host in ("127.0.0.1", "localhost", "::1")

    def _page_ok(self, url: str) -> bool:
        """只读 base 这个站 (正式的就是小黑盒自己的 https 域名) 的公开页, 不跟着站点地图去别处."""
        u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.base)
        if (u.scheme, u.hostname, u.port) == (b.scheme, b.hostname, b.port):
            return True
        return b.hostname in ("www.xiaoheihe.cn", "xiaoheihe.cn") and u.scheme == "https" and \
            u.hostname in ("www.xiaoheihe.cn", "xiaoheihe.cn")

    def _post_page_ok(self, url: str) -> bool:
        u = urllib.parse.urlsplit(url)
        parts = [x for x in u.path.split("/") if x]
        return self._page_ok(url) and len(parts) >= 4 and parts[-3:-1] == ["bbs", "link"] and \
            bool(POST_KEY.fullmatch(parts[-1]))

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, trust_env=False,
                                                   cookie_jar=aiohttp.DummyCookieJar(),
                                                   headers={"User-Agent": UA, "Accept": "text/html,application/xml"})
        return self._session

    @staticmethod
    async def _body(resp, max_bytes: int) -> bytes:
        chunks, size = [], 0  # 读到底, 超过 max_bytes 就停
        async for chunk in resp.content.iter_chunked(65536):
            size += len(chunk)
            if size > max_bytes:
                raise XhhError("回应太大")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _raw(self, url: str, params: dict | None = None) -> bytes:
        """只 GET 公开页, 不跟随跳转; 302 到登录/验证页也算没拿到. 不带 Cookie (公开页不需要登录)."""
        if not self._page_ok(url):
            raise XhhError("地址不在小黑盒的公开页下, 不读")
        try:
            async with self._http().get(url, params=params or {}, allow_redirects=False) as resp:
                if resp.status != 200:
                    raise XhhError(f"HTTP {resp.status}")
                return await self._body(resp, MAX_BYTES)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise XhhError(f"{type(exc).__name__}: {exc}") from None

    async def _html(self, url: str, params: dict | None = None) -> str:
        return (await self._raw(url, params)).decode("utf-8", errors="replace")

    async def sitemap_months(self) -> list[str]:
        """站点地图索引里的各月地址. 取一次缓存 6 小时 (平台一天才更新一回)."""
        now = time.time()
        if self._months and now - self._months[1] < 6 * 3600:
            return self._months[0]
        months = month_urls(await self._html(self.base + "/sitemap.xml"))
        if not months:
            raise XhhError("站点地图里没有帖子列表")
        self._months = (months, now)
        return months

    async def _month_posts(self, url: str) -> list[dict]:
        """一份按月站点地图里的帖子地址, 同样缓存 6 小时 (一份几百 KB, 不每次看都重下)."""
        now = time.time()
        cached = self._month_cache.get(url)
        if cached and now - cached[1] < 6 * 3600:
            return cached[0]
        rows = post_urls(await self._html(url))
        self._month_cache[url] = (rows, now)
        return rows

    async def recent_posts(self, limit: int = 60, months: int = 2) -> list[dict]:
        """最近的公开帖子 [{key, url}]: 从最新的月份往前找, 一份站点地图就够 limit 条时不再往前.
        当月文件还没生成 (404) 就跳过, 最多试 months + 3 个月."""
        got: list[dict] = []
        seen: set[str] = set()
        for url in list(reversed(await self.sitemap_months()))[:max(1, months) + 3]:
            try:
                rows = await self._month_posts(url)
            except XhhError:
                continue
            for row in rows[-max(1, limit):]:
                if row["key"] not in seen:
                    seen.add(row["key"])
                    got.append(row)
            if len(got) >= limit:
                break
        if not got:
            raise XhhError("站点地图里没有能看的帖子")
        return got[:max(1, limit)]

    async def post(self, key_or_url: str) -> dict:
        """一篇帖子: 只看公开页里直出的正文. key 是 /app/bbs/link/<key> 里的那串, 也可以给整条地址."""
        raw = str(key_or_url or "").strip()
        if "://" in raw:
            url = raw
            key = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        else:
            key = raw
            url = self.base + "/app/bbs/link/" + raw
        if not POST_KEY.fullmatch(key) or not self._post_page_ok(url):
            raise XhhError("帖子号不对, 或者不是小黑盒的公开帖子地址")
        return parse_post(await self._html(url), url, key)

    async def comments(self, key: str, n: int = 15) -> list[dict]:
        """不硬破: 评论列表在 /bbs/app/link/tree 这些接口里, 要 hkey、_time、nonce 签名, 这里不发请求."""
        raise XhhPostError("signature_required", "小黑盒的评论列表要 hkey 签名, 没有公开的读取接口")

    async def post_comment(self, key: str, message: str, cookie: str = "", root: str = "") -> str:
        """不硬破: 发评论要 hkey / Workshop 签名和登录态, 这里不发请求."""
        raise XhhPostError("signature_required", "小黑盒网页版发评论要 hkey / Workshop 签名和登录态, 没有公开的写接口")

    async def reply_comment(self, key: str, message: str, root: str, cookie: str = "") -> str:
        """不硬破: 回复评论和发评论是同一个签名接口 (/bbs/app/comment/create), 这里不发请求."""
        raise XhhPostError("signature_required", "小黑盒网页版回复评论要 hkey / Workshop 签名和登录态, 没有公开的写接口")

    async def like(self, key: str, comment_id: str = "", cookie: str = "", unlike: bool = False) -> None:
        """不硬破: 点赞帖子、评论同样要签名和登录态, 这里不发请求."""
        raise XhhPostError("signature_required", "小黑盒网页版点赞要 hkey 签名和登录态, 没有公开的写接口")

    async def inbox(self, cookie: str = "") -> list[dict]:
        """不硬破: "回复我的 / @我的" 在登录后的消息接口里, 要签名和登录态, 这里不发请求."""
        raise XhhPostError("signature_required", "小黑盒的消息中心要登录和 hkey 签名, 没有公开的读取接口")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
