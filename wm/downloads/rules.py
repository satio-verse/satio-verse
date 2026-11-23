"""下载队列的规矩里不用联网就能判的那部分: 地址长什么样才收、存到哪才收.
入队时 (你点头之前) 只做这里的检查 —— 不查域名、不连任何地方; 域名解析出来是不是公网, 等你点了头、真要连的时候每一跳再查
(transfer.py)."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from . import DownloadError

URL_MAX = 2000
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
PART = ".part"
# 能直接跑起来的东西: 卡片上提醒一句 (下完照样不运行)
EXECUTABLE = {".exe", ".msi", ".msix", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs", ".js", ".jse", ".wsf", ".hta", ".jar",
              ".dll", ".sys", ".lnk", ".reg", ".cpl", ".pif"}


def bad_ip(ip) -> bool:
    """本机、内网、保留地址 (和 web_fetch 的 _public 同一套判法)."""
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def literal_ip(host: str):
    """主机名本身就是 IP (包括 127.1、0x7f000001、2130706433 这些写法) 时返回它; 是域名返回 None. 不查域名."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-fA-FxX.]+", host or ""):  # 只有数字、点、十六进制字母: 可能是 inet_aton 认的老写法
        try:
            return ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            pass
    return None


def origin(url: str) -> str:
    """一个地址的"来源": 主机名 (不是默认端口时带端口). 卡片上列的、允许连的都按这个比."""
    u = urlsplit(url)
    host = (u.hostname or "").lower().rstrip(".")
    try:
        port = u.port
    except ValueError:
        port = None
    default = 443 if u.scheme == "https" else 80
    return host if port in (None, default) else f"{host}:{port}"


def hostport(url: str) -> str:
    """主机名:端口 (总带端口); 测试放行名单按这个比."""
    u = urlsplit(url)
    return f"{(u.hostname or '').lower()}:{u.port or (443 if u.scheme == 'https' else 80)}"


def check_host(raw: str, allow_local: frozenset = frozenset()) -> str:
    """她声明的"会转到的域名": 只收主机名 (可以带端口), 不收地址、路径."""
    text = str(raw or "").strip().lower().rstrip(".")
    if "://" in text:
        text = origin(text)
    host, _, port = text.partition(":")
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        raise DownloadError("url-invalid", f"「{raw}」不像域名")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise DownloadError("url-invalid", f"「{raw}」不像域名") from None
    if not host or len(host) > 253 or not _HOST.fullmatch(host):
        raise DownloadError("url-invalid", f"「{raw}」不像域名")
    if f"{host}:{port}" not in allow_local:
        _refuse_local(host)
    return f"{host}:{port}" if port else host


def _refuse_local(host: str) -> None:
    if host == "localhost" or host.endswith(".localhost"):
        raise DownloadError("private-address", "本机、内网的地址不下 (只下公网的)")
    ip = literal_ip(host)
    if ip is not None and bad_ip(ip):
        raise DownloadError("private-address", "本机、内网的地址不下 (只下公网的)")


def check_url(raw: str, allow_local: frozenset = frozenset()) -> str:
    """地址收不收 (不联网): 只收 http / https, 要有主机名, 不许带账号密码, 本机和内网的 IP 不收. 返回去掉 #片段 的地址.
    allow_local: 测试放行的"主机:端口" (本机起的假下载站), 只在测试里有."""
    url = str(raw or "").strip()
    if not url or len(url) > URL_MAX or any(c.isspace() or ord(c) < 32 for c in url):
        raise DownloadError("url-invalid", "下载地址是空的、太长或者里面有空白")
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise DownloadError("url-invalid", f"下载地址看不懂: {url[:120]}") from None
    if u.scheme not in ("http", "https") or not u.hostname:
        raise DownloadError("url-invalid", "只下 http / https 的地址")
    if u.username is not None or u.password is not None:
        raise DownloadError("url-invalid", "下载地址里不能带账号密码")
    if port == 0:
        raise DownloadError("url-invalid", f"下载地址看不懂: {url[:120]}")
    if hostport(url) not in allow_local:
        _refuse_local(u.hostname.lower().rstrip("."))
    return urlunsplit((u.scheme, u.netloc, u.path or "/", u.query, ""))


def check_sha256(raw) -> str | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip().lower().removeprefix("sha256:")
    if not _HEX64.fullmatch(text):
        raise DownloadError("bad-args", "sha256 要是 64 位十六进制 (官方给的那一串); 没有就不填")
    return text


def name_from_url(url: str) -> str:
    """没说存成什么: 用地址最后一段 (去掉不能当文件名的字)."""
    last = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    last = _BAD_NAME.sub("_", last).strip(" .")
    return last[:120] if last and last.lower().split(".")[0] not in _RESERVED else "download.bin"


def inside(path: Path, root: Path) -> bool:
    a, b = os.path.normcase(str(path)), os.path.normcase(str(root))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def real(path: Path) -> Path:
    """已有的那几层目录要是链接 (junction / symlink) 就跟过去: 看最后真正落在哪."""
    return Path(os.path.realpath(path))


def check_dest(root: Path, raw: str | None, url: str) -> Path:
    """存到哪收不收: 只能是下载目录里的相对路径. 绝对路径、盘符、..、冒号 (盘符和备用数据流)、Windows 的设备名、链接指到
    外面的, 一律不收. 返回完整路径."""
    text = str(raw or "").strip() or name_from_url(url)
    if os.path.isabs(text) or text.startswith(("\\", "/", "~")) or re.match(r"^[A-Za-z]:", text):
        raise DownloadError("dest-invalid", "存放位置只能写下载目录里的相对路径 (比如 模型名/文件名), 不能写完整路径")
    parts = [p for p in re.split(r"[\\/]", text)]
    if not parts or len(parts) > 8 or len(text) > 240:
        raise DownloadError("dest-invalid", "存放位置太深或太长")
    for p in parts:
        stem = p.split(".")[0].lower()
        if (not p or p in (".", "..") or _BAD_NAME.search(p) or p != p.strip() or p.endswith(".") or len(p) > 150
                or stem in _RESERVED):
            raise DownloadError("dest-invalid", f"存放位置里的「{p[:40]}」不能当文件名 (不能有 .. 、冒号、通配符, 不能是设备名)")
    if parts[-1].lower().endswith(PART):
        raise DownloadError("dest-invalid", f"文件名不能以 {PART} 结尾 (那是没下完的半截文件用的)")
    root = Path(root)
    dest = root.joinpath(*parts)
    if not inside(real(dest), real(root)):
        raise DownloadError("dest-invalid", "存放位置落到下载目录外面去了 (中间有链接), 不收")
    return dest


def recheck_dest(root: Path, dest: Path) -> None:
    """真要写之前再看一眼: 入队以后目录里要是多了指到外面的链接, 不往外写."""
    if not inside(real(Path(dest)), real(Path(root))):
        raise DownloadError("dest-invalid", "存放位置现在落到下载目录外面了 (中间多了链接), 没有写")


def dedup_key(dest: Path, url: str) -> tuple[str, str]:
    """同一个文件: 落地路径 (不分大小写、折掉 ..) 加来源 (第一个地址)."""
    u = urlsplit(url)
    return os.path.normcase(os.path.abspath(str(dest))), urlunsplit((u.scheme, u.netloc.lower(), u.path, u.query, ""))
