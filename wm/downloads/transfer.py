"""传一个文件 (一次尝试). 你点了头以后才会走到这里.

- 每一跳都查: 这一跳的来源在不在卡片上 (不在 -> 停下再问你, Ask), 解析出来是不是公网 (不是 -> 不下);
  https 转到 http 的不跟. 连接时还要再查一遍真正连上的 IP (GuardedResolver), 免得域名在两次解析之间换了地方.
- 断点续传: 半截的存在 <目标>.part. 有就带 Range 接着要; 带 If-Range (上次的 ETag / Last-Modified), 文件在那边变了的话
  服务器会从头给, 这里跟着从头写. 每 checkpoint_s 秒刷一次盘、记下稳妥的字节数; 接着下时先截到那里 (断电时最后一截可能没写实).
- 大小: 服务器报的比卡片上批的大 -> 停下再问你 (Ask); 超过单个文件的上限、盘上不够 -> 不下.
- 下完: 算 SHA-256, 有给的必须对上 (对不上删掉); 改成正式的名字 (已经有同名的不覆盖); 标上"来自网络". 不运行、不解压.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver

from . import DownloadError, Limits, human_size
from . import rules as R

CHUNK = 256 * 1024
UA = "WorldModel-Downloads/0.1"
REDIRECTS = 5
SLACK = 1 << 20  # 她查到的大小常是四舍五入过的: 实际比卡片上多 5% + 1 MB 以内不算"更大"


class Ask(Exception):
    """传到一半要再问你一次: why = redirect (要去卡片上没列的来源) / size (实际比批的大)."""

    def __init__(self, why: str, **data):
        super().__init__(why)
        self.why, self.data = why, data


class TransferError(Exception):
    """这一次没传成. retry: 过一会儿再试有没有用 (断线、超时、服务器忙是; 404、哈希不对、内网地址不是)."""

    def __init__(self, kind: str, message: str, retry: bool = False):
        super().__init__(message)
        self.kind, self.retry = kind, retry


class GuardedResolver(AbstractResolver):
    """连接的时候再查一遍解析出来的 IP: 本机、内网的不连 (放行名单里的"主机:端口"除外 —— 测试的假下载站、你设的代理)."""

    def __init__(self, allow: frozenset = frozenset()):
        self._inner = ThreadedResolver()
        self.allow = allow

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        infos = await self._inner.resolve(host, port, family)
        if f"{host.lower()}:{port}" in self.allow:
            return infos
        for info in infos:
            if R.bad_ip(ipaddress.ip_address(info["host"])):
                raise OSError(f"{host} 解析到了本机或内网的地址, 不连")
        return infos

    async def close(self) -> None:
        await self._inner.close()


def session(limits: Limits, allow: frozenset = frozenset(), proxy: str | None = None) -> aiohttp.ClientSession:
    """下载用的连接: 不读环境变量里的代理 (用不用代理每个任务自己定), 不自动解压, 不自动跟跳转."""
    extra = set(allow)
    if proxy:
        u = urlsplit(proxy)
        extra.add(f"{(u.hostname or '').lower()}:{u.port or 80}")
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=limits.read_timeout_s)
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=GuardedResolver(frozenset(extra)), limit=16),
                                 timeout=timeout, trust_env=False, auto_decompress=False, headers={"User-Agent": UA})


async def public(url: str, allow: frozenset) -> str | None:
    """这一跳能不能连: 不能时返回原因. 直接用 web_fetch 的那套 (查域名解析出来的每一个 IP)."""
    if R.hostport(url) in allow:
        return None
    from ..agent.tools import _public
    why = await _public(url)
    return why.replace("不读 (只读公网的网页)", "不下 (只下公网的)").replace("只能读", "只下") if why else None


def approved(job: dict, limits: Limits) -> int:
    """卡片上批到多大 (字节): 她写了大小就是那个数 (加一点余地: 查到的数常是四舍五入过的); 写"未知"的是 unknown_ok_bytes;
    再问过的是你后来批的那个数."""
    got = int(job.get("approved_bytes") or 0)
    return int(got * 1.05) + SLACK if got > 0 else limits.unknown_ok_bytes


def part_of(dest: Path | str) -> Path:
    dest = Path(dest)
    return dest.with_name(dest.name + R.PART)


def _strong(headers) -> tuple[str, str]:
    etag = headers.get("ETag", "").strip()
    return ("" if etag.startswith("W/") else etag), headers.get("Last-Modified", "").strip()


def _content_range(value: str) -> tuple[int, int]:
    """"bytes 100-199/1000" -> (100, 1000); "bytes */1000" -> (-1, 1000); 看不懂 (-1, -1)."""
    m = re.fullmatch(r"\s*bytes\s+(?:(\d+)-\d+|\*)/(\d+|\*)\s*", value or "")
    if not m:
        return -1, -1
    return (int(m.group(1)) if m.group(1) else -1), (int(m.group(2)) if m.group(2) != "*" else -1)


def _reset(job: dict, part: Path) -> None:
    part.unlink(missing_ok=True)
    job["part"], job["done"], job["checkpoint"] = {}, 0, 0


async def fetch(job: dict, root: Path, *, http: aiohttp.ClientSession, limits: Limits, allow: frozenset = frozenset(),
                proxy: str | None = None, progress=None, checkpoint=None, free=None) -> None:
    """把 job 现在这个地址 (urls[url_index]) 传到 .part, 直到收全. job 里的 total / done / checkpoint / part 跟着改;
    progress(这一块几个字节) 每块叫一次, checkpoint() 每次刷盘以后叫 (存任务记录). 被取消 (暂停、取消、服务要停) 时照样先刷盘."""
    first = url = job["urls"][int(job.get("url_index") or 0)]
    dest = Path(job["dest"])
    part = part_of(dest)
    try:
        R.recheck_dest(root, dest)
    except DownloadError as exc:
        raise TransferError("dest", str(exc)) from None
    part.parent.mkdir(parents=True, exist_ok=True)
    meta = job.get("part") or {}
    have = part.stat().st_size if part.exists() else 0
    if meta.get("url") != first:  # 半截的是从别的地址下的 (换了备用来源): 不接, 从头来
        have = 0
    have = min(have, int(job.get("checkpoint") or 0))
    if have and not (meta.get("etag") or meta.get("last_modified")) and not job.get("sha256"):
        # 上次那边没给能认"还是不是同一个文件"的凭据 (ETag / Last-Modified), 她也没给哈希能事后核对: 不冒险往后拼 ——
        # 那边的文件要是在断线这段时间换了, 拼出来的是坏文件, 谁也发现不了. 从头来 (有哈希的照样续传, 拼坏了下完核对得出来)
        have = 0
    if part.exists():
        with open(part, "r+b") as f:
            f.truncate(have)
    job["done"] = have
    headers = {"Accept-Encoding": "identity"}
    if have:
        headers["Range"] = f"bytes={have}-"
        if meta.get("etag") or meta.get("last_modified"):
            headers["If-Range"] = meta.get("etag") or meta["last_modified"]
    hops, prev = 0, first
    while True:
        here = R.origin(url)
        if here not in job["hosts"]:
            raise Ask("redirect", host=here, **{"from": R.origin(prev)})
        if (why := await public(url, allow)) is not None:
            raise TransferError("private", why)
        use_proxy = None if job.get("route") == "direct" or R.hostport(url) in allow else proxy
        try:
            resp = await http.get(url, headers=headers, allow_redirects=False, proxy=use_proxy)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise TransferError("network", f"连不上 {here}: {str(exc)[:160] or type(exc).__name__}", retry=True) from None
        if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            nxt = urljoin(url, resp.headers["Location"])
            resp.release()
            hops += 1
            if hops > REDIRECTS:
                raise TransferError("redirects", "跳转太多次了")
            if urlsplit(url).scheme == "https" and urlsplit(nxt).scheme != "https":
                raise TransferError("redirects", f"{here} 要从 https 转到不加密的 http, 不跟")
            try:
                nxt = R.check_url(nxt, allow)
            except DownloadError as exc:
                raise TransferError("private" if exc.code == "private-address" else "redirects",
                                    f"{here} 转去的地址不收: {exc}") from None
            prev, url = url, nxt
            continue
        break
    try:
        status = resp.status
        if status == 416:  # 要的那一段没有: 已经收全了, 或者那边的文件变了
            _, total = _content_range(resp.headers.get("Content-Range", ""))
            if have and total == have:
                job["total"] = total
                return
            _reset(job, part)
            raise TransferError("range", "续传的位置对不上 (那边的文件可能变了), 从头再下", retry=True)
        if status in (408, 425, 429) or status >= 500:
            raise TransferError("http", f"{here} 回 HTTP {status}", retry=True)
        if status not in (200, 206):
            raise TransferError("http", f"{here} 回 HTTP {status}")
        if (resp.headers.get("Content-Encoding") or "identity").lower() != "identity":
            raise TransferError("http", f"{here} 只肯压缩着传 ({resp.headers['Content-Encoding']}), 存下来的会不是原文件, 不收")
        if status == 206:
            start, total = _content_range(resp.headers.get("Content-Range", ""))
            if start != have:
                _reset(job, part)
                raise TransferError("range", "服务器给的不是要的那一段, 从头再下", retry=True)
        else:
            have = 0  # 不支持续传, 或者文件在那边变了 (If-Range 没对上): 从头收
            total = int(resp.headers["Content-Length"]) if resp.headers.get("Content-Length", "").isdigit() else -1
        if total >= 0:
            if total > limits.max_file_bytes:
                raise TransferError("too-large", f"实际大小 {human_size(total)}, 超过单个文件的上限 {human_size(limits.max_file_bytes)}")
            if total > approved(job, limits):
                raise Ask("size", total=total)
            left = (free or _free)(part.parent)
            if left < total - have + limits.reserve_bytes:
                raise TransferError("disk-full", f"磁盘空间不够: 还要下 {human_size(total - have)}, 下完要留 {human_size(limits.reserve_bytes)}, "
                                                 f"现在只剩 {human_size(left)}")
        etag, modified = _strong(resp.headers)
        job["total"], job["done"] = total, have
        job["part"] = {"url": first, "etag": etag, "last_modified": modified}
        if checkpoint is not None:
            job["checkpoint"] = have
            checkpoint()
        last = time.monotonic()
        with open(part, "ab" if have else "wb") as f:
            try:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    try:
                        f.write(chunk)
                    except OSError as exc:
                        raise TransferError("disk-full", f"写盘没成 ({str(exc)[:120]}), 先停下") from None
                    have += len(chunk)
                    job["done"] = have
                    if progress is not None:
                        progress(len(chunk))
                    if total >= 0 and have > total:
                        raise TransferError("http", f"{here} 给的比它说的多 ({have} / {total} 字节), 不收")
                    if total < 0 and have > limits.max_file_bytes:
                        raise TransferError("too-large", f"已经下了 {human_size(have)} 还没完, 超过单个文件的上限")
                    if total < 0 and have > approved(job, limits):
                        raise Ask("size", total=have, open=True)
                    if time.monotonic() - last >= limits.checkpoint_s:
                        try:  # 刷盘放进线程: 慢盘上也不卡住下载服务的事件循环 (这个协程自己等着, 不会有人同时写)
                            await asyncio.to_thread(_sync, f)
                        except OSError as exc:
                            raise TransferError("disk-full", f"刷盘没成 ({str(exc)[:120]}), 先停下") from None
                        job["checkpoint"] = have
                        last = time.monotonic()
                        if checkpoint is not None:
                            checkpoint()
                        if total < 0 and (free or _free)(part.parent) < limits.reserve_bytes:
                            raise TransferError("disk-full", f"磁盘只剩不到 {human_size(limits.reserve_bytes)} 了, 先停下")
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
                raise TransferError("network", f"传到一半断了: {str(exc)[:160] or type(exc).__name__}", retry=True) from None
            finally:  # 断了、被暂停、被取消、服务要停: 都先把收到的写实, 下次从这里接
                with contextlib.suppress(OSError):
                    f.flush()
                    os.fsync(f.fileno())
                    job["checkpoint"] = have
        if total >= 0 and have != total:
            raise TransferError("network", f"没收全 ({have} / {total} 字节)", retry=True)
        job["total"] = have
    finally:
        resp.release()


def _sync(f) -> None:
    """收到的写实到磁盘 (检查点: 断电、被杀以后只信写实了的那一截)."""
    f.flush()
    os.fsync(f.fileno())


def _free(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def finalize(job: dict, root: Path, limits: Limits) -> dict:
    """收全了: 整个文件算一遍 SHA-256 (有给的必须对上, 对不上删掉), 改成正式的名字, 标上"来自网络". 在线程里跑."""
    dest = Path(job["dest"])
    part = part_of(dest)
    h, size = hashlib.sha256(), 0
    with open(part, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    digest = h.hexdigest()
    if job.get("sha256") and digest != job["sha256"]:
        part.unlink(missing_ok=True)
        raise TransferError("hash", f"SHA-256 对不上 (要的是 {job['sha256'][:16]}…, 下到的是 {digest[:16]}…), 文件已经删掉")
    try:
        R.recheck_dest(root, dest)
    except DownloadError as exc:
        raise TransferError("dest", str(exc)) from None
    if dest.exists():
        raise TransferError("dest", f"目标位置在下载期间出现了同名文件, 没有覆盖; 下好的留在 {part.name}")
    os.rename(part, dest)
    if limits.mark_of_web:
        mark_of_web(dest, job["urls"][0])
    return {"bytes": size, "sha256": digest, "verified": bool(job.get("sha256"))}


def mark_of_web(dest: Path, url: str) -> bool:
    """Windows 的"来自网络"标记 (Zone.Identifier 备用数据流, 浏览器下的文件都有): 以后有人双击运行它, 系统会先提醒.
    不是 NTFS 的盘写不上, 那就算了."""
    if os.name != "nt":
        return False
    try:
        with open(f"{dest}:Zone.Identifier", "w", encoding="ascii", errors="replace", newline="") as f:
            f.write(f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={url}\r\n")
        return True
    except OSError:
        return False
