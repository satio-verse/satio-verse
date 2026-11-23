"""网络体检的假数据 (tests/test_net_*.py 共用; wm/net/ 第 0 步).

- Box: 攒一份假的 Snapshot. 进程、网卡都按名字指, pid 和接口序号自己编; 不碰这台电脑的真实状态.
- sample_*(): 验收用的几份样本 ("这台电脑 11-11 的样子"、别的 TUN 开着、干净的机器 ...).
- ps_output(): 假的 PowerShell 输出 (采集脚本每项输出一行 WMNET1|项名|ok|JSON), 测试里替换掉起进程那一步.
- FakeSocks5: 起在 127.0.0.1 随机端口的假 SOCKS5 服务端, 测 wm/net/socks_probe.py 用. 测试从不对真的 1082-1085 或任何真端口探测.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket

from wm.net.config_gen import DEVICE, EXCLUDE_V4  # 我们自己的网卡名和 TUN 的排除网段: 从 config_gen 来, 测试里不再抄一份字面量
from wm.net.snapshot import (Adapter, Address, EnvProxyItem, IPInterface, Listener, Process, Route, Service, Snapshot, SystemProxy)

# 样本里夹带的、和代理无关的进程名和服务名: 筛选 (wm/net/trim.py) 之后快照的 JSON 里一个也不该搜到
NOISE_WORDS = ("game.exe", "chrome.exe", "node.exe", "WeChat.exe", "vendorsvc.exe", "explorer.exe", "svchost.exe", "vmware-tray",
               "Spooler", "Dnscache", "VendorUpdateService", "WSearch")


def complement_prefixes(excluded: tuple[str, ...] = EXCLUDE_V4) -> list[str]:
    """整个 IPv4 地址空间减去 excluded 拆出来的一串路由 (TUN 配了排除网段时, 核心加的就是这样的路由: 0.0.0.0/5、8.0.0.0/7 ...).
    用 ipaddress 算, 不手抄."""
    nets = [ipaddress.ip_network("0.0.0.0/0")]
    for cut in map(ipaddress.ip_network, excluded):
        rest = []
        for net in nets:
            if not net.overlaps(cut):
                rest.append(net)
            elif cut.subnet_of(net):
                rest.extend(net.address_exclude(cut))  # net 整个在 cut 里的 (没有这种) 就整个丢掉
        nets = rest
    return [str(net) for net in ipaddress.collapse_addresses(nets)]

STAMP = "2026-11-11T12:00:00+00:00"


class Box:
    """攒一份假快照: box.proc("sing-box.exe", ppid=...) 返回 pid; box.adapter("WLAN", "Intel...") 之后用名字加地址、路由."""

    def __init__(self) -> None:
        self.snap = Snapshot(collected_at=STAMP)
        self._pid = 1000
        self._index = 10
        self._by_name: dict[str, int] = {}

    # 进程、服务、监听
    def proc(self, name: str, ppid: int = 0) -> int:
        self._pid += 1
        self.snap.processes.append(Process(pid=self._pid, ppid=ppid, name=name))
        return self._pid

    def service(self, name: str, pid: int = 0, state: str = "Running") -> None:
        self.snap.services.append(Service(name=name, state=state, pid=pid))

    def listen(self, pid: int, *ports: int, address: str = "127.0.0.1") -> None:
        for port in ports:
            self.snap.listeners.append(Listener(address=address, port=port, pid=pid))

    # 网卡
    def adapter(self, name: str, description: str, status: str = "Up") -> int:
        self._index += 1
        self._by_name[name] = self._index
        self.snap.adapters.append(Adapter(name=name, description=description, status=status, index=self._index))
        return self._index

    def address(self, adapter: str, ip: str, prefix: int, origin: str = "system") -> None:
        self.snap.addresses.append(Address(adapter=adapter, if_index=self._by_name[adapter], ip=ip, prefix=prefix, origin=origin))

    def interface(self, adapter: str, family: str = "IPv4", metric: int = 25, auto: bool = True, state: str = "Connected") -> None:
        self.snap.ip_interfaces.append(IPInterface(adapter=adapter, if_index=self._by_name[adapter], family=family, metric=metric,
                                                   auto_metric=auto, state=state))

    def route(self, adapter: str, prefix: str, next_hop: str = "0.0.0.0", metric: int = 256, if_metric: int = 25,
              persistent: bool = False) -> None:
        self.snap.routes.append(Route(prefix=prefix, next_hop=next_hop, metric=metric, if_metric=if_metric, adapter=adapter,
                                      if_index=self._by_name[adapter], persistent=persistent))

    def ghost(self, name: str) -> None:
        """一个网卡列表里没有的接口 (系统自带的 VPN 连接、回环就是这样): 之后只有接口表、地址、路由里有它."""
        self._index += 1
        self._by_name[name] = self._index

    def own(self, status: str = "Up", name: str = DEVICE) -> None:
        """我们自己的虚拟网卡 (名字是 config_gen.DEVICE)."""
        self.adapter(name, "Wintun Userspace Tunnel", status)
        self.interface(name, "IPv4", 0, auto=False, state="Connected" if status == "Up" else "Disconnected")

    def noise(self) -> None:
        """和代理无关的进程、服务和监听: 用户开着的游戏和浏览器、带监听的 node、装着的服务. 分析本来就不看它们, 筛选之后快照里
        一个也不该留下."""
        explorer = self.proc("explorer.exe")
        self.proc("game.exe", ppid=explorer)
        chrome = self.proc("chrome.exe", ppid=explorer)
        self.proc("chrome.exe", ppid=chrome)
        node = self.proc("node.exe", ppid=explorer)
        self.listen(node, 3000, address="0.0.0.0")
        self.listen(node, 5173)
        self.proc("WeChat.exe", ppid=explorer)
        shared = self.proc("svchost.exe")
        self.service("Spooler", shared)
        self.service("Dnscache", shared)
        vendor = self.proc("vendorsvc.exe")
        self.service("VendorUpdateService", vendor)
        self.listen(vendor, 49152)
        self.service("WSearch", 0, "Stopped")

    def finish(self) -> Snapshot:
        """样本的收尾: 夹上无关的进程、服务、监听, 交出快照."""
        self.noise()
        return self.snap

    # 常用的一整块
    def physical(self, name: str = "WLAN", description: str = "Intel(R) Wi-Fi 6E AX211 160MHz", ip: str = "192.168.1.50",
                 ipv6_default: bool = False) -> None:
        """一块通着的物理网卡: DHCP 地址、自动跃点、IPv4 默认路由 (ipv6_default=True 再加一条 IPv6 默认路由)."""
        self.adapter(name, description)
        self.address(name, ip, 24, "dhcp")
        self.interface(name, "IPv4", 30)
        self.route(name, "0.0.0.0/0", "192.168.1.1", 0, 30)
        self.route(name, f"{ip}/32", metric=256, if_metric=30)
        if ipv6_default:
            self.interface(name, "IPv6", 30)
            self.route(name, "::/0", "fe80::1", 0, 30)

    def system_proxy(self, server: str = "", enabled: bool = True, pac: bool = False) -> None:
        self.snap.system_proxy = SystemProxy(enabled=enabled, server=server, pac=pac)

    def env(self, **items: tuple[str, int | None] | bool) -> None:
        """box.env(http_proxy=("127.0.0.1", 7890), no_proxy=True): 元组是 (主机, 端口), True 是只记有没有."""
        for name, value in items.items():
            item = EnvProxyItem(present=True)
            if isinstance(value, tuple):
                item.host, item.port = value
            setattr(self.snap.env_proxy, name, item)


# ---- 样本 -------------------------------------------------------------------------------------------------

def sample_1111() -> Snapshot:
    """这台电脑 2026-11-11 的样子 (组长当时只读看到的): 系统代理指向本机 1082; sing-box 由 nssm 托管、监听 0.0.0.0 的 1082-1085;
    Clash Verge Rev 只有后台服务; 火绒; vgate0 在用但不接管默认路由; cfw-tap 断开了却留着手动地址、手动跃点和持久默认路由."""
    b = Box()
    b.physical("WLAN")
    services_exe = b.proc("services.exe")
    b.proc("svchost.exe", ppid=services_exe)
    nssm = b.proc("nssm.exe", ppid=services_exe)
    sing_box = b.proc("sing-box.exe", ppid=nssm)
    verge_service = b.proc("clash-verge-service.exe", ppid=services_exe)
    b.proc("HipsTray.exe")
    b.proc("vmware-tray.exe")
    b.service("clash_verge_service", verge_service)
    b.service("SingBoxProxy", nssm)  # nssm 托管的服务 (服务名当时没看, 这里自己起一个)
    b.listen(sing_box, 1082, 1083, 1084, 1085, address="0.0.0.0")
    b.listen(4, 135, 445, address="0.0.0.0")  # System 的监听: 不是代理客户端, 不该被当成问题
    # vgate0: 在用, 上面只有自己的 /32、组播、广播路由
    b.adapter("vgate0", "Rust Wintun Tunnel Tunnel")
    b.address("vgate0", "172.30.243.250", 32, "manual")
    b.interface("vgate0", "IPv4", 5)
    for prefix in ("172.30.243.250/32", "224.0.0.0/4", "255.255.255.255/32"):
        b.route("vgate0", prefix, if_metric=5)
    # cfw-tap: 断开了, 留着手动地址、手动设的接口跃点 1、持久的默认路由 (生效的列表里也有这一条)
    b.adapter("cfw-tap", "TAP-Windows Adapter V9", "Disconnected")
    b.address("cfw-tap", "169.254.87.180", 16, "system")
    b.address("cfw-tap", "10.0.0.1", 24, "manual")
    b.interface("cfw-tap", "IPv4", 1, auto=False, state="Disconnected")
    b.interface("cfw-tap", "IPv6", 1, auto=False, state="Disconnected")
    b.route("cfw-tap", "0.0.0.0/0", "10.0.0.0", 1, 1)
    b.route("cfw-tap", "0.0.0.0/0", "10.0.0.0", 1, 1, persistent=True)
    b.adapter("Teredo Tunneling Pseudo-Interface", "", "Not Present")
    b.system_proxy("127.0.0.1:1082")
    b.env(http_proxy=("127.0.0.1", 7890))  # 指向哪当时没看, 编一个本机地址; 没有 no_proxy
    return b.finish()


def sample_other_tun() -> Snapshot:
    """别的 TUN 开着: Clash Verge Rev 的 Meta 网卡 (Up) 用 0.0.0.0/1 + 128.0.0.0/1 接管了默认路由, verge-mihomo 在跑."""
    b = Box()
    b.physical("WLAN")
    verge = b.proc("clash-verge.exe")
    core = b.proc("verge-mihomo.exe", ppid=verge)
    b.listen(core, 7897)
    b.adapter("Meta", "Meta Tunnel")
    b.address("Meta", "198.18.0.1", 30, "manual")
    b.interface("Meta", "IPv4", 0, auto=False)
    b.route("Meta", "0.0.0.0/1", "198.18.0.2", 0, 0)
    b.route("Meta", "128.0.0.0/1", "198.18.0.2", 0, 0)
    b.system_proxy("127.0.0.1:7897")
    b.env(no_proxy=True)
    return b.finish()


def sample_clean() -> Snapshot:
    """干净的机器: 没有代理客户端, 系统代理关着."""
    b = Box()
    b.physical("WLAN")
    b.proc("explorer.exe")
    b.system_proxy(enabled=False)
    return b.finish()


def sample_verge_ok() -> Snapshot:
    """Clash Verge Rev 界面和核心都在跑, 端口 7897 只听 127.0.0.1, 系统代理指向它, 没有别的问题."""
    b = Box()
    b.physical("WLAN")
    verge = b.proc("clash-verge.exe")
    core = b.proc("verge-mihomo.exe", ppid=verge)
    b.listen(core, 7897)
    b.system_proxy("127.0.0.1:7897")
    return b.finish()


def sample_v2rayn() -> Snapshot:
    """v2rayN 加 xray, xray 听 10808 (只听本机)."""
    b = Box()
    b.physical("WLAN")
    gui = b.proc("v2rayN.exe")
    xray = b.proc("xray.exe", ppid=gui)
    b.listen(xray, 10808)
    b.system_proxy("127.0.0.1:10808")
    return b.finish()


def sample_leftover_up() -> Snapshot:
    """cfw-tap 又连上了: 状态 Up, 默认路由在 (生效的和持久的都有)."""
    b = Box()
    b.physical("WLAN")
    b.adapter("cfw-tap", "TAP-Windows Adapter V9", "Up")
    b.address("cfw-tap", "10.0.0.1", 24, "manual")
    b.interface("cfw-tap", "IPv4", 1, auto=False)
    b.route("cfw-tap", "0.0.0.0/0", "10.0.0.0", 1, 1)
    b.route("cfw-tap", "0.0.0.0/0", "10.0.0.0", 1, 1, persistent=True)
    b.system_proxy(enabled=False)
    return b.finish()


def sample_noise_adapters() -> Snapshot:
    """一堆长得像、但不该报的网卡: 中文名的物理网卡 (IPv4 和 IPv6 默认路由都在它上面)、WSL 的 vEthernet、VMware 的 VMnet8、蓝牙、
    Teredo (Up, 连 ::/0 都占着)、WAN Miniport、Wi-Fi Direct、内核调试、Hyper-V、VirtualBox Host-Only."""
    b = Box()
    b.physical("以太网", "Realtek PCIe GbE Family Controller", "192.168.0.8", ipv6_default=True)
    b.adapter("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter")
    b.address("vEthernet (WSL)", "172.28.80.1", 20, "manual")
    b.route("vEthernet (WSL)", "172.28.80.0/20", if_metric=15)
    b.adapter("VMware Network Adapter VMnet8", "VMware Virtual Ethernet Adapter for VMnet8")
    b.address("VMware Network Adapter VMnet8", "192.168.188.1", 24, "manual")
    b.interface("VMware Network Adapter VMnet8", "IPv4", 35, auto=False)
    b.route("VMware Network Adapter VMnet8", "192.168.188.0/24", if_metric=35)
    b.adapter("VirtualBox Host-Only Network", "VirtualBox Host-Only Ethernet Adapter", "Disconnected")
    b.address("VirtualBox Host-Only Network", "192.168.56.1", 24, "manual")
    b.adapter("蓝牙网络连接", "Bluetooth Device (Personal Area Network)", "Disconnected")
    b.address("蓝牙网络连接", "169.254.9.9", 16, "manual")
    b.adapter("Teredo Tunneling Pseudo-Interface", "", "Up")
    b.route("Teredo Tunneling Pseudo-Interface", "::/0", "::", 0, 50)
    b.adapter("本地连接* 8", "WAN Miniport (IP)")
    b.adapter("本地连接* 1", "Microsoft Wi-Fi Direct Virtual Adapter", "Disconnected")
    b.adapter("以太网(内核调试器)", "Microsoft Kernel Debug Network Adapter", "Not Present")
    b.system_proxy(enabled=False)
    return b.finish()


def sample_dead_proxy() -> Snapshot:
    """系统代理开着, 指向 127.0.0.1:7890, 但本机没有人在听 (也没有任何代理客户端)."""
    b = Box()
    b.physical("WLAN")
    b.proc("explorer.exe")
    b.system_proxy("127.0.0.1:7890")
    return b.finish()


def sample_tailscale() -> Snapshot:
    """一块不接管默认路由的 Tailscale 类网卡 (Up, 只有 100.64.0.0/10 的路由); 另有一个正常的代理客户端和系统代理."""
    b = Box()
    b.physical("WLAN")
    b.adapter("Tailscale", "Tailscale Tunnel")
    b.address("Tailscale", "100.101.102.103", 32, "manual")
    b.route("Tailscale", "100.64.0.0/10", if_metric=5)
    b.route("Tailscale", "100.100.100.100/32", if_metric=5)
    verge = b.proc("clash-verge.exe")
    core = b.proc("verge-mihomo.exe", ppid=verge)
    b.listen(core, 7897)
    b.system_proxy("127.0.0.1:7897")
    return b.finish()


# ---- 假的 PowerShell 输出 ---------------------------------------------------------------------------------

def ps_output(*, bom: bool = False, crlf: bool = True, noise: bool = False, **sections: object) -> bytes:
    """按采集脚本的格式拼一份输出. sections: 项名 = 要输出的 Python 值 (列表、单个对象都行; 传 ("err", 文字) 是这一项报错;
    传 ("raw", 文字) 是原样的一行内容, 用来造坏掉的 JSON)."""
    lines = ["WARNING: 这是一行警告, 不是 WMNET1 的输出"] if noise else []
    for name, value in sections.items():
        if isinstance(value, tuple) and value and value[0] == "err":
            lines.append(f"WMNET1|{name}|err|{json.dumps(value[1], ensure_ascii=False)}")
        elif isinstance(value, tuple) and value and value[0] == "raw":
            lines.append(f"WMNET1|{name}|ok|{value[1]}")
        else:
            lines.append(f"WMNET1|{name}|ok|{json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
    raw = ("\r\n" if crlf else "\n").join(lines) + ("\r\n" if crlf else "\n")
    return (b"\xef\xbb\xbf" if bom else b"") + raw.encode("utf-8")


def ps_rows() -> dict[str, object]:
    """一份完整的、像真机输出的各项 (中文网卡名、IPv6 地址带网卡序号、Wintun 隧道、持久路由等)."""
    return {
        "adapters": [
            {"name": "WLAN", "description": "Intel(R) Wi-Fi 6E AX211 160MHz", "status": "Up", "index": 12},
            {"name": "以太网", "description": "Realtek Gaming 2.5GbE Family Controller", "status": "Disconnected", "index": 11},
            {"name": "本地连接* 9", "description": "WAN Miniport (IPv6)", "status": "Up", "index": 22},
            {"name": "cfw-tap", "description": "TAP-Windows Adapter V9", "status": "Disconnected", "index": 4},
            {"name": "Teredo Tunneling Pseudo-Interface", "description": "", "status": "Not Present", "index": 14},
        ],
        "ip_interfaces": [
            {"adapter": "WLAN", "if_index": 12, "family": "IPv4", "metric": 30, "auto_metric": True, "state": "Connected"},
            {"adapter": "cfw-tap", "if_index": 4, "family": "IPv4", "metric": 1, "auto_metric": False, "state": "Disconnected"},
        ],
        "addresses": [
            {"adapter": "WLAN", "if_index": 12, "ip": "192.168.1.50", "prefix": 24, "origin": "Dhcp"},
            {"adapter": "cfw-tap", "if_index": 4, "ip": "10.0.0.1", "prefix": 24, "origin": "Manual"},
            {"adapter": "cfw-tap", "if_index": 4, "ip": "fe80::80f1:7357:83db:5073%4", "prefix": 64, "origin": "WellKnown"},
            {"adapter": "WLAN", "if_index": 12, "ip": "2001:db8::2", "prefix": 64, "origin": "RouterAdvertisement"},
        ],
        "routes": [
            {"prefix": "0.0.0.0/0", "next_hop": "192.168.1.1", "metric": 0, "if_metric": 30, "adapter": "WLAN", "if_index": 12},
            {"prefix": "0.0.0.0/0", "next_hop": "10.0.0.0", "metric": 1, "if_metric": 1, "adapter": "cfw-tap", "if_index": 4},
        ],
        "routes_persistent": [
            {"prefix": "0.0.0.0/0", "next_hop": "10.0.0.0", "metric": 1, "if_metric": 1, "adapter": "cfw-tap", "if_index": 4},
        ],
        "listeners": [
            {"address": "0.0.0.0", "port": 1082, "pid": 9660},
            {"address": "fe80::1%12", "port": 5357, "pid": 4},
            {"address": "0.0.0.0", "port": 3000, "pid": 5002},
        ],
        "processes": [
            {"pid": 7112, "ppid": 1632, "name": "nssm.exe"},
            {"pid": 9660, "ppid": 7112, "name": "sing-box.exe"},
            {"pid": 5000, "ppid": 1, "name": "chrome.exe"},
            {"pid": 5001, "ppid": 1, "name": "game.exe"},
            {"pid": 5002, "ppid": 1, "name": "node.exe"},
            {"pid": 5100, "ppid": 1632, "name": "svchost.exe"},
        ],
        "services": [
            {"name": "SingBoxProxy", "state": "Running", "pid": 7112},
            {"name": "ADPSvc", "state": "Stopped", "pid": 0},
            {"name": "Spooler", "state": "Running", "pid": 5100},
        ],
    }


# ---- 假的 SOCKS5 服务端 -----------------------------------------------------------------------------------

HTTP_REPLY = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def free_port() -> int:
    """现在没人听的一个本机端口 (绑一下马上放掉)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeSocks5:
    """起在 127.0.0.1 随机端口的假服务端. 用法: async with FakeSocks5("full") as fake: ... fake.port.
    mode: full 完整支持 (方法协商选 0, UDP ASSOCIATE 回成功和 udp_port, 地址类型 atyp: 1 IPv4 / 4 IPv6 / 3 域名) /
          udp_refused 回应码 0x07 / short_refuse 只回 05 07 就关 / bad_udp UDP 的回应不是 SOCKS5 的格式 / slow_udp 方法协商答了、
          UDP ASSOCIATE 不答 / auth_required 方法协商回 0xFF / http 回一个 HTTP 400 / silent 收了不回话 / closes 一连上就关.
    connections: 连进来几次; received: 每个连接收到的全部字节 (连接结束才记, 用 wait_idle 等一下), 用来核对没发业务数据."""

    def __init__(self, mode: str = "full", udp_port: int = 45678, atyp: int = 1) -> None:
        self.mode, self.udp_port, self.atyp = mode, udp_port, atyp
        self.port = 0
        self.connections = 0
        self.received: list[bytes] = []
        self._active = 0
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> FakeSocks5:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def wait_idle(self, seconds: float = 3) -> None:
        for _ in range(int(seconds * 100)):
            if self._active == 0:
                return
            await asyncio.sleep(0.01)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._active += 1
        got = bytearray()
        try:
            if self.mode == "closes":
                return
            got += await reader.read(100)  # 方法协商
            if self.mode == "http":
                writer.write(HTTP_REPLY)
            elif self.mode == "auth_required":
                writer.write(b"\x05\xff")
            elif self.mode in ("full", "udp_refused", "short_refuse", "bad_udp", "slow_udp"):
                writer.write(b"\x05\x00")
                await writer.drain()
                got += await reader.read(100)  # UDP ASSOCIATE 请求
                port = self.udp_port.to_bytes(2, "big")
                if self.mode == "full" and self.atyp == 4:
                    writer.write(b"\x05\x00\x00\x04" + b"\x00" * 15 + b"\x01" + port)
                elif self.mode == "full" and self.atyp == 3:
                    writer.write(b"\x05\x00\x00\x03\x09localhost" + port)
                elif self.mode == "full":
                    writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01" + port)
                elif self.mode == "udp_refused":
                    writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                elif self.mode == "short_refuse":
                    writer.write(b"\x05\x07")
                    await writer.drain()
                    return
                elif self.mode == "bad_udp":
                    writer.write(b"\x04\x5a\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            got += await reader.read()  # 直到对方关掉连接; 多出来的字节 (业务数据) 会记在 received 里
        except (ConnectionError, OSError):
            pass
        finally:
            self.received.append(bytes(got))
            self._active -= 1
            writer.close()
