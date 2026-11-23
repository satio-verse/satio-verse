"""显式的 SOCKS5 握手探测 (2026-11-11 网络插件第 0 步; 体检默认不调用, 命令行要显式写 --probe 127.0.0.1:端口).

问一个本机端口: 它是不是 SOCKS5、支不支持 UDP ASSOCIATE (UDP 游戏要走的就是这条). 做的事只有: TCP 连上 -> 发无认证的方法协商 ->
发 UDP ASSOCIATE 请求 (地址和端口填全 0) -> 读回应 -> 关闭. 不发任何业务数据, 也不往回应里给的 UDP 端口发包, 不认证 (对方要用户名密码
就到此为止), 不碰配置.

只接受 127.0.0.0/8 里的地址: 别的 (包括 localhost、::1、局域网地址、域名) 直接拒绝, 不发起任何连接. 要不要对本机真的代理端口做这个
探测, 是用户点头才做的事 (见方案页); 测试只对测试里自己起的假服务端做.

返回 {"socks5": bool, "udp": bool, "detail": 文字}; detail 以一个固定的词开头, 好分清几种情况:
  拒绝 (地址不是 127.0.0.0/8 或端口不对) / 连不上 (没人听、被拒绝) / 超时 / 不是 SOCKS5 (回的不是 SOCKS5, 比如 HTTP 代理) /
  是 SOCKS5, 但... (要认证、不支持 UDP、回应不对) / 支持 UDP ASSOCIATE.
"""
from __future__ import annotations

import asyncio
import ipaddress

LOOPBACK = ipaddress.ip_network("127.0.0.0/8")

_GREETING = b"\x05\x01\x00"  # VER=5, NMETHODS=1, METHODS=[0 无认证]
_UDP_ASSOCIATE = b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00"  # VER, CMD=3 UDP ASSOCIATE, RSV, ATYP=1 (IPv4), 0.0.0.0, 端口 0
_REPLY_CODES = {1: "一般性失败", 2: "规则不允许", 3: "网络不可达", 4: "主机不可达", 5: "连接被拒绝", 6: "TTL 过期", 7: "不支持这个命令",
                8: "不支持这种地址类型"}


def _result(socks5: bool, udp: bool, detail: str) -> dict:
    return {"socks5": socks5, "udp": udp, "detail": detail}


def _refusal(host: object, port: object) -> str:
    """地址或端口不合规矩时的原因 (空 = 可以探测)."""
    try:
        ip = ipaddress.ip_address(host.strip()) if isinstance(host, str) else None
    except ValueError:
        ip = None
    if not isinstance(ip, ipaddress.IPv4Address) or ip not in LOOPBACK:
        return f"拒绝：只探测 127.0.0.0/8 里的地址（收到的是 {host!r}），没有发起任何连接"
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return f"拒绝：端口要是 1-65535 的整数（收到的是 {port!r}），没有发起任何连接"
    return ""


async def _read(reader: asyncio.StreamReader, n: int) -> bytes:
    """读 n 个字节; 对方提前关了连接就返回读到的那一点 (可能是空)."""
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as exc:
        return exc.partial


def _not_socks5(head: bytes) -> str:
    if not head:
        return "不是 SOCKS5：对方没回话就把连接关了"
    if head[:1] == b"H":
        return "不是 SOCKS5：对方回的是 HTTP（像是 HTTP 代理或网页服务）"
    return f"不是 SOCKS5：对方回的第一个字节是 0x{head[0]:02x}"


async def _udp_reply_port(reader: asyncio.StreamReader, atyp: int) -> int | None:
    """把 UDP ASSOCIATE 回应里的地址和端口读掉 (只用来在说明里写个端口, 不会往里发任何东西); 读不全是 None."""
    if atyp == 1:
        tail = await _read(reader, 6)
    elif atyp == 4:
        tail = await _read(reader, 18)
    elif atyp == 3:
        size = await _read(reader, 1)
        tail = await _read(reader, size[0] + 2) if size else b""
    else:
        return None
    return int.from_bytes(tail[-2:], "big") if len(tail) >= 2 else None


async def _probe(host: str, port: int, st: dict) -> dict:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except ConnectionRefusedError:
        return _result(False, False, "连不上：对方拒绝了连接（这个端口上没有程序在听）")
    except OSError as exc:
        return _result(False, False, f"连不上：{type(exc).__name__}")
    try:
        st["stage"] = "方法协商"
        writer.write(_GREETING)
        await writer.drain()
        head = await _read(reader, 2)
        if len(head) < 2 or head[0] != 5:
            return _result(False, False, _not_socks5(head))
        st["socks5"] = True
        if head[1] != 0:
            return _result(True, False, f"是 SOCKS5，但不接受无认证（对方选了 0x{head[1]:02x}，要认证），没有继续探测")
        st["stage"] = "UDP ASSOCIATE"
        writer.write(_UDP_ASSOCIATE)
        await writer.drain()
        reply = await _read(reader, 4)
        if len(reply) < 2 or reply[0] != 5:
            return _result(True, False, "是 SOCKS5，但对 UDP ASSOCIATE 的回应不对（格式不是 SOCKS5 的）")
        if reply[1] != 0:
            why = _REPLY_CODES.get(reply[1], "没见过的回应码")
            return _result(True, False, f"是 SOCKS5，但不支持 UDP：UDP ASSOCIATE 被拒绝（回应码 0x{reply[1]:02x}，{why}）")
        relay = await _udp_reply_port(reader, reply[3] if len(reply) > 3 else 0)
        where = f"，给了 UDP 中继端口 {relay}（没有往里发过包）" if relay else ""
        return _result(True, True, f"支持 UDP ASSOCIATE{where}")
    except OSError as exc:  # 对方中途把连接断了 (reset / broken pipe)
        who = "是 SOCKS5，但" if st["socks5"] else "不是 SOCKS5：对方没回话，"
        return _result(st["socks5"], False, f"{who}中途把连接断了（{type(exc).__name__}）")
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 0.5)
        except (OSError, asyncio.TimeoutError):
            pass


async def probe_socks5(host: str, port: int, timeout_s: float = 3) -> dict:
    """探测 host:port 是不是 SOCKS5、支不支持 UDP ASSOCIATE. host 只能是 127.0.0.0/8 里的 IPv4 地址, 否则直接拒绝、不发起连接.
    timeout_s 是整个探测的总时限."""
    refused = _refusal(host, port)
    if refused:
        return _result(False, False, refused)
    st = {"stage": "连接", "socks5": False}
    try:
        return await asyncio.wait_for(_probe(str(ipaddress.ip_address(host.strip())), port, st), timeout_s)
    except asyncio.TimeoutError:
        note = "（Windows 上连没人听的本机端口，要 2 秒多才会收到拒绝；时限太短会先报成超时）" if st["stage"] == "连接" else ""
        return _result(st["socks5"], False, f"超时：{st['stage']} 这一步 {timeout_s:g} 秒内没有结果{note}")
