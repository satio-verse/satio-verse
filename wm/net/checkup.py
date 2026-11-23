"""只读体检的分析 (2026-11-11 网络插件第 0 步): 一份 Snapshot 进, 一份 Report 出. 纯函数, 不做任何输入输出.

四档沿用插件宿主的体检约定 (dsh-game-daily 的做法), 字样照抄: 就绪 / 有风险 / 缺失 / 阻断, 由轻到重; 另有不计档的"提示".
总档取最重的一条; 没有任何"有风险"以上的发现就是"就绪". 有阻断就不该往下开 TUN (由宿主那边拦, 这里只给结论).

每条发现有 code、level、title (一句话)、detail、hint (要用户做什么, 没有就空), 外加 data (结构化的细节, 给面板和测试用).
规则 (code):
  client_found 提示         认出的代理客户端 (按 CLIENTS 表认进程名和服务名): 家族、在不在跑、是不是以服务方式运行、监听了哪些端口
  system_proxy 提示         系统代理开没开、指向哪、那个端口是谁在听; PAC
  proxy_port_dead 缺失      系统代理开着, 但那个本机端口没有人在听
  no_upstream 缺失          找不到任何可以当上游的本机代理端口
  listener_all_interfaces 有风险   认得的客户端监听在 0.0.0.0 / :: (同一个进程的多个端口合成一条)
  other_tun_active 阻断     别的虚拟网卡接管了默认路由 (按覆盖面判, 见下)
  virtual_adapter 提示      隧道类网卡在 Up 但没接管默认路由 (不冲突)
  leftover_adapter 有风险   以前遗留的隧道类网卡 (状态不是 Up, 还留着手动地址 / 手动接口跃点 / 持久的默认路由)
  own_tun_active 提示       我们自己的虚拟网卡 (名字是 config_gen.DEVICE, 不分大小写) 通着; data.takeover 说它接管了默认路由没有
  own_tun_stale 有风险      我们自己的虚拟网卡还在, 但不是通的 (它随核心进程出现、随进程消失, 正常不会留下)
  security_software 提示    安全软件 (开 TUN 时可能拦驱动)
  env_proxy_no_bypass 有风险  用户环境变量设了代理却没有 no_proxy
  ipv6_default_route 提示   物理网卡上有 IPv6 默认路由
  server_elevated 有风险    服务端以管理员身份在跑 (elevated=True)
  collect_partial 有风险    snap.errors 不空: 有几项没采到; 其余照常分析, 但"没有"的结论 (no_upstream、proxy_port_dead) 在它
                            依赖的那一项没采到时不下

判断网卡: 系统自带的伪网卡 (Teredo、ISATAP、6to4、WAN Miniport、Wi-Fi Direct、蓝牙、回环、内核调试) 一律忽略; Hyper-V / WSL 的
vEthernet、VMware 的 VMnet、VirtualBox 的 Host-Only 不算隧道类; 描述里有 wintun / tunnel / tap-windows / tap-win32 / sing-tun /
wireguard / openvpn / vpn / anyconnect / pangp / zerotier 之一、或名字是 Meta / Mihomo 的才算隧道类. 物理网卡上的默认路由永远不算别人
接管. 断开的网卡上的路由也会出现在"生效"的列表里 (cfw-tap 就是), 所以只看通着的网卡: 状态是 Up, 而且它的 IP 接口 (有记录的话) 至少
有一个是 Connected; 不通的另说 (见 leftover_adapter).

"接管默认路由"按覆盖面判 (takeover): 这块网卡上生效的 (非持久的) IPv4 路由合起来 (重叠的合并, 不重复算) 覆盖了 IPv4 地址空间的一半或
更多, 就算接管; IPv6 看对全球单播 2000::/3 的覆盖. 0.0.0.0/0、/1 那一对当然算, 只有 0.0.0.0/1 一条也算; TUN 配了排除网段时核心加的是
"整个地址空间减去排除网段"拆出来的一串路由 (没有 /0 也没有 /1 那一对), 一样算. 遗留网卡的"持久的默认路由"也这样判 (持久存储里的路由).
网卡列表里没有、路由表和接口表里有的接口 (系统自带的 VPN 连接就是这样): 接口是 Connected 而且覆盖一半或更多, 也报 other_tun_active.

快照里只有 trim.py 筛过的那些进程、服务、监听 (采到就筛, 见 trim.py); 这里用得上的都在里面.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass, field

from .config_gen import DEVICE
from .snapshot import Adapter, Process, Route, Service, Snapshot, drop_zone, split_host_port

READY, RISK, MISSING, BLOCK = "就绪", "有风险", "缺失", "阻断"
NOTE = "提示"  # 不计档
LEVELS = (READY, RISK, MISSING, BLOCK)  # 由轻到重
_RANK = {level: i for i, level in enumerate(LEVELS)}
_SHOW = {BLOCK: 0, MISSING: 1, RISK: 2, NOTE: 3}  # 摘要里重的排前面

FAMILY_MIHOMO, FAMILY_SINGBOX, FAMILY_V2RAYN = "mihomo", "sing-box", "v2rayN"
FAMILY_LABEL = {FAMILY_MIHOMO: "mihomo 系", FAMILY_SINGBOX: "sing-box 系", FAMILY_V2RAYN: "v2rayN 系"}


# ---- 认客户端的表 -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientDef:
    id: str
    name: str  # 给人看的名字
    family: str
    ui: tuple[str, ...] = ()  # 界面的进程名
    core: tuple[str, ...] = ()  # 核心的进程名
    helper: tuple[str, ...] = ()  # 后台服务的进程名
    services: tuple[str, ...] = ()  # Windows 服务名
    core_prefixes: tuple[str, ...] = ()  # 核心进程名以这个开头也算 (带版本号的核心)


# 比较时不分大小写、带不带 .exe 都认 (见 _norm)
CLIENTS = (
    ClientDef("clash-verge-rev", "Clash Verge Rev", FAMILY_MIHOMO, ui=("clash-verge",), core=("verge-mihomo", "verge-mihomo-alpha"),
              helper=("clash-verge-service",), services=("clash_verge_service",)),
    ClientDef("mihomo", "mihomo", FAMILY_MIHOMO, core=("mihomo",), core_prefixes=("mihomo-windows-",)),
    ClientDef("clash-meta", "Clash Meta", FAMILY_MIHOMO, core=("clash-meta",)),
    ClientDef("flclash", "FlClash", FAMILY_MIHOMO, ui=("FlClash",), core=("FlClashCore",), helper=("FlClashHelperService",),
              services=("FlClashHelperService",)),
    ClientDef("clash-party", "Clash Party", FAMILY_MIHOMO, ui=("Clash Party", "mihomo-party")),
    ClientDef("sparkle", "sparkle", FAMILY_MIHOMO, ui=("sparkle",)),
    ClientDef("clash-nyanpasu", "Clash Nyanpasu", FAMILY_MIHOMO, ui=("clash-nyanpasu",)),
    ClientDef("clash-for-windows", "Clash for Windows", FAMILY_MIHOMO, ui=("Clash for Windows",), core=("clash-win64",)),
    ClientDef("sing-box", "sing-box", FAMILY_SINGBOX, core=("sing-box",)),
    ClientDef("gui-for-singbox", "GUI.for.SingBox", FAMILY_SINGBOX, ui=("GUI.for.SingBox",)),
    ClientDef("hiddify", "Hiddify", FAMILY_SINGBOX, ui=("Hiddify",), core=("HiddifyCli",)),
    ClientDef("nekobox", "NekoBox", FAMILY_SINGBOX, ui=("nekobox",), core=("nekobox_core",)),
    ClientDef("nekoray", "NekoRay", FAMILY_SINGBOX, ui=("nekoray",)),
    ClientDef("karing", "Karing", FAMILY_SINGBOX, ui=("Karing",)),
    ClientDef("throne", "Throne", FAMILY_SINGBOX, ui=("Throne",)),
    ClientDef("v2rayn", "v2rayN", FAMILY_V2RAYN, ui=("v2rayN",), core=("xray", "v2ray")),
)

# 安全软件: (名字, 进程名). 开 TUN 时它们可能拦驱动加载或核心程序.
SECURITY = (
    ("火绒", ("HipsTray", "HipsDaemon", "HipsMain")),
    ("360", ("360Tray", "ZhuDongFangYu")),
    ("金山毒霸", ("kxetray", "kxescore")),
    ("腾讯电脑管家", ("QQPCTray", "QQPCRTP")),
)

# 网卡: 系统自带的伪网卡 / 虚拟机的网卡 / 隧道类 的特征 (都按小写找子串; 伪网卡和虚拟机找名字和描述两处, 隧道类只找描述)
PSEUDO_ADAPTER_WORDS = ("teredo", "isatap", "6to4", "ip-https", "wan miniport", "wi-fi direct", "bluetooth", "蓝牙", "loopback",
                        "kernel debug", "内核调试")
VM_ADAPTER_WORDS = ("vethernet", "hyper-v", "vmnet", "vmware", "virtualbox", "host-only")
TUNNEL_DESC_WORDS = ("wintun", "tunnel", "tap-windows", "tap-win32", "sing-tun", "wireguard", "openvpn", "vpn", "anyconnect", "pangp",
                     "zerotier")
TUNNEL_NAMES = ("meta", "mihomo")
TAKEOVER_SHARE = 0.5  # 路由合起来覆盖地址空间的这一份或更多, 就算接管了默认路由
V6_GLOBAL = ipaddress.ip_network("2000::/3")  # IPv6 的"整个网": 全球单播


def norm_name(name: str) -> str:
    """进程名、服务名比较用的写法: 小写、去掉 .exe."""
    n = name.strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def _pretty(name: str) -> str:
    return name[:-4] if name.lower().endswith(".exe") else name


def _build_tables() -> tuple[dict[str, tuple[ClientDef, str]], dict[str, ClientDef]]:
    by_name: dict[str, tuple[ClientDef, str]] = {}
    for d in CLIENTS:
        for role, names in (("ui", d.ui), ("core", d.core), ("helper", d.helper)):
            for n in names:
                by_name[norm_name(n)] = (d, role)
    return by_name, {norm_name(n): d for d in CLIENTS for n in d.services}


_BY_NAME, _BY_SERVICE = _build_tables()
_BY_SECURITY = {norm_name(n): label for label, names in SECURITY for n in names}


def match_process(name: str) -> tuple[ClientDef, str] | None:
    """进程名认得是哪个客户端的哪一部分 (界面 / 核心 / 后台帮手), 认不出是 None."""
    n = norm_name(name)
    hit = _BY_NAME.get(n)
    if hit:
        return hit
    for d in CLIENTS:
        if any(n.startswith(p) for p in d.core_prefixes):
            return d, "core"
    return None


def match_service(name: str) -> ClientDef | None:
    """Windows 服务名认得是哪个客户端的, 认不出是 None."""
    return _BY_SERVICE.get(norm_name(name))


def security_label(name: str) -> str | None:
    """进程名是哪家安全软件的 (火绒、360 ...), 不是是 None."""
    return _BY_SECURITY.get(norm_name(name))


# ---- 结果 -------------------------------------------------------------------------------------------------

@dataclass
class Finding:
    code: str
    level: str  # 就绪以外: 有风险 / 缺失 / 阻断, 或 提示
    title: str
    detail: str = ""
    hint: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class ClientInfo:
    id: str
    name: str
    family: str
    state: str  # running: 界面或核心在跑; service_only: 只有后台服务在
    state_text: str
    processes: list[str] = field(default_factory=list)  # 在跑的进程名 (不带 .exe)
    services: list[str] = field(default_factory=list)  # 在跑的、认得的 Windows 服务名
    as_service: bool = False  # 是不是以服务方式运行 (它自己或它的父进程是某个服务的进程, 比如父进程是 nssm.exe)
    service_hosts: list[str] = field(default_factory=list)  # 托管它的服务名
    ports: list[dict] = field(default_factory=list)  # [{address, port}]: 它监听的 TCP 端口


@dataclass
class Candidate:
    """可以当上游的本机代理端口."""
    host: str  # 连的时候用的地址 (监听在 0.0.0.0 的写 127.0.0.1)
    port: int
    listen: str  # 实际监听的地址
    owner: str  # 谁在听 (进程名)
    client: str  # 认出的客户端名, 认不出是空
    source: str  # system_proxy: 系统代理指的; client: 认得的客户端的监听端口


@dataclass
class Report:
    level: str = READY
    findings: list[Finding] = field(default_factory=list)
    clients: list[ClientInfo] = field(default_factory=list)
    upstream_candidates: list[Candidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def summary(self) -> str:
        """给用户看的中文: 先说总档, 再一条一行 (重的在前); 有要用户做的事就接在后面. 不出现用户名、路径、命令行."""
        lines = [f"网络体检：{self.level}"]
        if not self.findings:
            lines.append("没有发现需要说明的东西。")
        for f in self.findings:
            lines.append(f"- [{f.level}] {f.title}" + (f" → {f.hint}" if f.hint else ""))
        return "\n".join(lines)


# ---- 系统代理的写法 ----------------------------------------------------------------------------------------

@dataclass
class ProxyEntry:
    proto: str  # all (没分协议) / http / https / ftp / socks
    host: str
    port: int | None


def parse_proxy_server(server: str) -> list[ProxyEntry]:
    """ProxyServer 可能是 'host:port', 也可能是 'http=h:p;https=h:p;socks=h:p' 这种分协议的写法 (分号或空白隔开, 地址前可带 scheme://)."""
    out = []
    for part in re.split(r"[;\s]+", server.strip()):
        if not part:
            continue
        proto = "all"
        m = re.match(r"^([A-Za-z]+)=(.*)$", part)
        if m:
            proto, part = m.group(1).lower(), m.group(2)
        host, port = split_host_port(part)
        if host:
            out.append(ProxyEntry(proto, host, port))
    return out


def _ip(text: str):
    try:
        return ipaddress.ip_address(drop_zone(text.strip()))
    except ValueError:
        return None


def _is_local_host(host: str, local_ips: set[str]) -> bool:
    if host.lower() == "localhost":
        return True
    ip = _ip(host)
    return ip is not None and (ip.is_loopback or ip.is_unspecified or str(ip) in local_ips)


def _serves(listen: str, host: str) -> bool:
    """监听在 listen 的程序, 连 host 这个地址连得上吗 (0.0.0.0 和 :: 本机任何地址都连得上, 不区分 IPv4 / IPv6)."""
    a = _ip(listen)
    if a is None:
        return False
    if a.is_unspecified:
        return True
    if host.lower() == "localhost":
        return a.is_loopback
    h = _ip(host)
    return h is not None and a == h


def _connect_host(listen: str) -> str:
    """别人连这个监听地址用什么地址: 通配地址和回环就用本机回环."""
    a = _ip(listen)
    if a is None:
        return listen
    if a.is_unspecified or a.is_loopback:
        return "::1" if a.version == 6 else "127.0.0.1"
    return str(a)


def _hostport(host: str, port: int | None) -> str:
    h = f"[{host}]" if ":" in host else host
    return f"{h}:{port}" if port is not None else h


def _join(items) -> str:
    return "、".join(str(i) for i in items)


# ---- 分析时一次算好的索引 -----------------------------------------------------------------------------------

def _service_pids(snap: Snapshot, procs: dict[int, Process]) -> dict[int, list[str]]:
    """在跑的服务的进程: {pid: [服务名]}. 共享的 svchost 不算 (计划任务之类的子进程挂在它下面, 不是"以服务方式运行")."""
    out: dict[int, list[str]] = {}
    for s in snap.services:
        if s.state.strip().lower() != "running" or s.pid <= 0:
            continue
        p = procs.get(s.pid)
        if p is not None and norm_name(p.name) == "svchost":
            continue
        out.setdefault(s.pid, []).append(s.name)
    return out


class _Ctx:
    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.procs = {p.pid: p for p in snap.processes}
        self.service_pids = _service_pids(snap, self.procs)
        self.local_ips = {str(ip) for a in snap.addresses if (ip := _ip(a.ip)) is not None}
        self.connected: dict[int, bool] = {}  # 网卡的 IP 接口里有没有一个是 Connected (没有接口记录的网卡不在里面)
        for i in snap.ip_interfaces:
            self.connected[i.if_index] = self.connected.get(i.if_index, False) or i.state.strip().lower() == "connected"
        self.routes_of: dict[int, list[Route]] = {}
        for r in snap.routes:
            self.routes_of.setdefault(r.if_index, []).append(r)
        self.clients: list[ClientInfo] = []
        self.client_pids: dict[str, list[int]] = {}
        self._detect_clients()
        self.pid_client = {pid: c for c in self.clients for pid in self.client_pids[c.id]}

    def is_up(self, a: Adapter) -> bool:
        """网卡通着: 状态是 Up, 而且它的 IP 接口 (有记录的话) 至少有一个是 Connected. 断开的网卡上的路由也会出现在生效的列表里,
        两个信号对不上时宁可当它没通, 不报"别的 TUN 接管了默认路由"."""
        return a.status.strip().lower() == "up" and self.connected.get(a.index, True)

    def takeover_of(self, if_index: int, *, persistent: bool = False) -> Takeover:
        """这个接口上生效的 (persistent=True 是持久存储里的) 路由有没有接管默认路由."""
        return takeover([r for r in self.routes_of.get(if_index, ()) if r.persistent == persistent])

    def hosting_services(self, pid: int) -> list[str]:
        """这个进程自己、或它的父进程, 是哪些服务的进程."""
        names = list(self.service_pids.get(pid, ()))
        p = self.procs.get(pid)
        if p is not None:
            names += [n for n in self.service_pids.get(p.ppid, ()) if n not in names]
        return names

    def process_name(self, pid: int) -> str:
        p = self.procs.get(pid)
        return _pretty(p.name) if p is not None else f"pid {pid}"

    def _detect_clients(self) -> None:
        found: dict[str, list[tuple[Process, str]]] = {}
        for p in self.snap.processes:
            hit = match_process(p.name)
            if hit is not None:
                found.setdefault(hit[0].id, []).append((p, hit[1]))
        running_services: dict[str, list[Service]] = {}
        for s in self.snap.services:
            d = match_service(s.name)
            if d is not None and s.state.strip().lower() == "running":
                running_services.setdefault(d.id, []).append(s)
        for d in CLIENTS:  # 在跑的排前面, 同样的按表里的顺序
            procs, services = found.get(d.id, []), running_services.get(d.id, [])
            if not procs and not services:
                continue
            roles = {role for _, role in procs}
            state = "running" if roles & {"ui", "core"} else "service_only"
            pids = [p.pid for p, _ in procs]
            # 以服务方式运行: 看界面/核心进程是不是服务托管的 (后台帮手自己就是服务的进程, 它在不代表核心是服务拉起的);
            # 只有后台服务在的时候, 在跑的就是那个服务本身
            hosts: list[str] = []
            for pid in ([p.pid for p, role in procs if role != "helper"] if state == "running" else pids):
                hosts += [n for n in self.hosting_services(pid) if n not in hosts]
            if state == "service_only":
                hosts += [s.name for s in services if s.name not in hosts]
            ports = sorted({(item.port, item.address) for item in self.snap.listeners if item.pid in pids})
            names: list[str] = []
            for p, _ in procs:
                if _pretty(p.name) not in names:
                    names.append(_pretty(p.name))
            self.clients.append(ClientInfo(
                id=d.id, name=d.name, family=d.family, state=state, state_text=_state_text(d, roles, state), processes=names,
                services=[s.name for s in services], as_service=bool(hosts) or state == "service_only", service_hosts=hosts,
                ports=[{"address": a, "port": port} for port, a in ports]))
            self.client_pids[d.id] = pids
        self.clients.sort(key=lambda c: c.state != "running")  # 稳定排序: 在跑的在前

    def listeners_serving(self, host: str, port: int) -> list:
        return [item for item in self.snap.listeners if item.port == port and _serves(item.address, host)]


def _state_text(d: ClientDef, roles: set[str], state: str) -> str:
    if state == "service_only":
        return "只有后台服务在，界面和核心没开"
    if d.ui and (d.core or d.core_prefixes):
        if "ui" in roles and "core" in roles:
            return "界面和核心都在跑"
        return "界面在跑，核心没开" if "ui" in roles else "核心在跑，界面没开"
    return "在跑"


# ---- 网卡 -------------------------------------------------------------------------------------------------

def adapter_kind(a: Adapter) -> str:
    """pseudo 系统自带的伪网卡 / virtual 虚拟机的网卡 / tunnel 隧道类 / physical 其余 (物理网卡)."""
    name, desc = a.name.lower(), a.description.lower()
    text = f"{name} {desc}"
    if any(w in text for w in PSEUDO_ADAPTER_WORDS):
        return "pseudo"
    if any(w in text for w in VM_ADAPTER_WORDS):
        return "virtual"
    if name in TUNNEL_NAMES or any(w in desc for w in TUNNEL_DESC_WORDS):
        return "tunnel"
    return "physical"


SHOW_ROUTES = 4  # 说明和 data 里最多列几条路由 (一块 TUN 网卡上可能有上百条)


def _pct(share: float) -> int:
    return int(round(share * 100))


@dataclass
class Takeover:
    """一组路由有没有接管默认路由 (按覆盖面判, 见 takeover())."""
    taken: bool = False
    v4: float = 0.0  # 这些路由合起来覆盖 IPv4 地址空间的几分之几 (0-1)
    v6: float = 0.0  # 覆盖 IPv6 全球单播 2000::/3 的几分之几 (0-1)
    routes: list[Route] = field(default_factory=list)  # 接管了的那个地址族里的路由, 大的在前; 可能上百条, 说明里只列前几条

    def coverage_text(self) -> str:
        parts = []
        if self.v4 >= TAKEOVER_SHARE:
            parts.append(f"约 {_pct(self.v4)}% 的 IPv4 地址")
        if self.v6 >= TAKEOVER_SHARE:
            parts.append(f"约 {_pct(self.v6)}% 的 IPv6 全球地址（2000::/3）")
        return "、".join(parts)

    def routes_text(self) -> str:
        """最大的几条路由 (最多 SHOW_ROUTES 条); 还有的写总数."""
        text = _join(r.prefix for r in self.routes[:SHOW_ROUTES])
        return text + (f" 等共 {len(self.routes)} 条" if len(self.routes) > SHOW_ROUTES else "")

    def data(self) -> dict:
        return {"routes": [r.prefix for r in self.routes[:SHOW_ROUTES]], "route_count": len(self.routes),
                "coverage": {"ipv4": round(self.v4, 3), "ipv6": round(self.v6, 3)}}


def takeover(routes) -> Takeover:
    """这些路由有没有接管默认路由, 按覆盖面判: IPv4 路由合起来 (重叠的合并, 不重复算) 覆盖了 IPv4 地址空间的一半或更多, 或者
    IPv6 路由覆盖了全球单播 2000::/3 的一半或更多, 就算接管. 0.0.0.0/0、0.0.0.0/1 加 128.0.0.0/1 当然算, 只有 0.0.0.0/1 一条也算
    (它已经接走半个网); TUN 配了排除网段时, 核心加的是"整个地址空间减去排除网段"拆出来的一串路由 (没有 /0 也没有 /1 那一对),
    一样算. 路由可能上百条: 排序合并是 O(n log n), 不是两两比."""
    v4: list[tuple[ipaddress.IPv4Network, Route]] = []
    v6: list[tuple[ipaddress.IPv6Network, Route]] = []
    for r in routes:
        try:
            net = ipaddress.ip_network(r.prefix.strip(), strict=False)
        except ValueError:
            continue
        if net.version == 4:
            v4.append((net, r))
        elif net.subnet_of(V6_GLOBAL) or V6_GLOBAL.subnet_of(net):  # 和全球单播不沾边的 (fd00::/8、fe80::/64、ff00::/8) 不算
            v6.append((net, r))
    share4 = sum(n.num_addresses for n in ipaddress.collapse_addresses(n for n, _ in v4)) / 2 ** 32
    inside = [V6_GLOBAL if V6_GLOBAL.subnet_of(n) else n for n, _ in v6]  # 比全球单播还大的 (::/0、::/1) 只算全球单播那一份
    share6 = sum(n.num_addresses for n in ipaddress.collapse_addresses(inside)) / V6_GLOBAL.num_addresses
    taken4, taken6 = share4 >= TAKEOVER_SHARE, share6 >= TAKEOVER_SHARE
    chosen = (v4 if taken4 else []) + (v6 if taken6 else [])
    chosen.sort(key=lambda nr: (nr[0].version, nr[0].prefixlen, int(nr[0].network_address)))
    return Takeover(taken4 or taken6, share4, share6, [r for _, r in chosen])


_HINT_WORDS = (  # 网卡名/描述里的线索 -> 可能是哪几个家族的客户端开的
    (re.compile(r"\b(meta|mihomo|clash)\b"), {FAMILY_MIHOMO}),
    (re.compile(r"sing-?tun|sing-?box|singbox"), {FAMILY_SINGBOX, FAMILY_V2RAYN}),  # v2rayN 开 TUN 用的是 sing-box
)


def _guess_clients(a: Adapter, clients: list[ClientInfo]) -> list[ClientInfo]:
    """这块虚拟网卡看起来是哪个在跑的客户端开的 (对不上就是空)."""
    text = f"{a.name} {a.description}".lower()
    families: set[str] = set()
    for pattern, fams in _HINT_WORDS:
        if pattern.search(text):
            families |= fams
    return [c for c in clients if c.state == "running" and c.family in families]


# ---- 各条规则 ---------------------------------------------------------------------------------------------

def _server_elevated() -> Finding:
    return Finding("server_elevated", RISK, "服务端是以管理员身份在跑",
                   "她、工作台任务和所有扩展都会拿到管理员权限（黑名单和工作区限制只是工具层的规则，不是系统隔离）。",
                   "关掉这个管理员终端，用普通权限重新启动服务端（从桌宠或普通终端起）。")


ERROR_LABELS = {
    "processes": "进程", "services": "服务", "listeners": "监听端口", "adapters": "网卡", "addresses": "网卡地址",
    "ip_interfaces": "网卡的跃点和连接状态", "routes": "路由", "routes_persistent": "持久路由", "system_proxy": "系统代理",
    "env_proxy": "环境变量里的代理",
}


def _collect_partial(snap: Snapshot) -> Finding:
    names = [ERROR_LABELS.get(k, k) for k in snap.errors]
    detail = "；".join(f"{ERROR_LABELS.get(k, k)}：{v}" for k, v in snap.errors.items())
    return Finding("collect_partial", RISK, f"体检有 {len(names)} 项没采到：{_join(names)}",
                   f"{detail}。没采到的这几项相关的结论没有下，别的照常分析。", "再跑一次体检；还是不行，看上面写的原因。",
                   {"errors": dict(snap.errors)})


def _client_found(c: ClientInfo) -> Finding:
    title = f"认出代理客户端 {c.name}（{FAMILY_LABEL[c.family]}）：{c.state_text}"
    if c.as_service and c.state == "running":
        title += "，以服务方式运行"
    bits = []
    if c.processes:
        bits.append(f"进程 {_join(c.processes)} 在跑")
    if c.services:
        bits.append(f"服务 {_join(c.services)} 在跑")
    if c.as_service and c.state == "running" and c.service_hosts:
        bits.append(f"由服务 {_join(c.service_hosts)} 托管")
    bits.append("监听 " + _join(f"{_hostport(p['address'], p['port'])}" for p in c.ports) if c.ports else "没有监听 TCP 端口")
    return Finding("client_found", NOTE, title, "；".join(bits) + "。", data=asdict(c))


def _system_proxy_findings(ctx: _Ctx) -> tuple[list[Finding], list[Candidate]]:
    """系统代理: 一条 system_proxy 提示; 指着本机没人听的端口是 proxy_port_dead. 返回 (发现, 系统代理指向的可用上游)."""
    sp = ctx.snap.system_proxy
    findings: list[Finding] = []
    cands: list[Candidate] = []
    if "system_proxy" in ctx.snap.errors:
        return findings, cands
    can_look = not ({"listeners", "processes"} & set(ctx.snap.errors))  # 谁在听要这两项都采到了
    views: list[dict] = []
    for e in (parse_proxy_server(sp.server) if sp.enabled else []):
        view = next((v for v in views if v["host"] == e.host and v["port"] == e.port), None)
        if view is None:
            view = {"protos": [], "host": e.host, "port": e.port, "local": e.port is not None and _is_local_host(e.host, ctx.local_ips),
                    "listening": False, "owner": "", "client": "", "as_service": False}
            views.append(view)
        view["protos"].append(e.proto)
    for v in views:
        if not v["local"] or not can_look:
            continue
        listeners = ctx.listeners_serving(v["host"], v["port"])
        if not listeners:
            continue
        pid = listeners[0].pid
        client = ctx.pid_client.get(pid)
        v.update(listening=True, owner=ctx.process_name(pid), client=client.name if client else "",
                 as_service=bool(ctx.hosting_services(pid)))
        cands.append(Candidate(host="127.0.0.1" if v["host"].lower() == "localhost" else v["host"], port=v["port"],
                               listen=listeners[0].address, owner=v["owner"], client=v["client"], source="system_proxy"))

    def describe(v: dict) -> str:
        base = _hostport(v["host"], v["port"])
        if v["port"] is None:
            return f"{base}（没写端口）"
        if not v["local"]:
            return f"{base}（不是本机的代理）"
        if not can_look:
            return base
        if not v["listening"]:
            return f"{base}（本机没有程序在听这个端口）"
        return f"{base}（{v['owner']} 在听" + ("，以服务方式运行）" if v["as_service"] else "）")

    if sp.enabled:
        if views:
            title = "系统代理开着，指向 " + "；".join(describe(v) for v in views)
        else:
            title = "系统代理开着，但没有写代理地址"
        if sp.pac:
            title += "；还设了 PAC 脚本"
    else:
        title = "系统代理没开，但设了 PAC 脚本" if sp.pac else "系统代理没开"
    labels = {"all": "所有协议", "http": "HTTP", "https": "HTTPS", "ftp": "FTP", "socks": "SOCKS"}
    detail = ("；".join(f"{_join(labels.get(p, p) for p in v['protos'])} → {describe(v)}" for v in views) + "。") if views else ""
    if sp.pac:
        detail += "PAC 脚本开着：用 PAC 的程序会按脚本里的规则决定走不走代理，我们看不到规则。"
    findings.append(Finding("system_proxy", NOTE, title, detail.strip(), data={"enabled": sp.enabled, "pac": sp.pac, "entries": views}))
    if can_look:
        for v in views:
            if v["local"] and not v["listening"] and v["port"] is not None:
                findings.append(Finding(
                    "proxy_port_dead", MISSING, f"系统代理指向 {_hostport(v['host'], v['port'])}，但本机没有程序在听这个端口",
                    "走系统代理的程序现在会连不上网；多半是代理客户端没开或者崩了。", "先把你平时用的代理客户端打开；用不上它的话，到系统设置里关掉系统代理。",
                    {"host": v["host"], "port": v["port"]}))
    return findings, cands


def _upstream_candidates(ctx: _Ctx, from_system: list[Candidate]) -> list[Candidate]:
    """系统代理指的排第一, 其余是认得的客户端的监听端口 (同一个进程的同一个端口只算一次, IPv4 的地址优先)."""
    out = list(from_system)
    seen = {(c.port, c.owner) for c in out}
    for c in ctx.clients:
        for p in sorted(c.ports, key=lambda p: (p["port"], ":" in p["address"])):
            owner = next((ctx.process_name(pid) for pid in ctx.client_pids[c.id]
                          if any(i.pid == pid and i.port == p["port"] and i.address == p["address"] for i in ctx.snap.listeners)), c.name)
            if (p["port"], owner) in seen:
                continue
            seen.add((p["port"], owner))
            out.append(Candidate(host=_connect_host(p["address"]), port=p["port"], listen=p["address"], owner=owner, client=c.name,
                                 source="client"))
    return out


def _listener_findings(ctx: _Ctx) -> list[Finding]:
    """认得的客户端监听在 0.0.0.0 / ::: 同一个进程的多个端口合成一条."""
    out = []
    for c in ctx.clients:
        for pid in ctx.client_pids[c.id]:
            wide = [i for i in ctx.snap.listeners if i.pid == pid and _is_wildcard(i.address)]
            if not wide:
                continue
            ports = sorted({i.port for i in wide})
            addrs = sorted({i.address for i in wide}, key=lambda a: (":" in a, a))
            who = ctx.process_name(pid)
            out.append(Finding(
                "listener_all_interfaces", RISK, f"{who} 的代理端口监听在所有网卡上（{_join(addrs)}）：端口 {_join(ports)}",
                f"{c.name} 的进程 {who} 监听 {_join(addrs)} 的 {_join(ports)} 端口，同一局域网里的其他设备也连得上这些代理端口。",
                "如果不是有意的，在那个客户端自己的配置里把监听地址改成 127.0.0.1。我们不改它的配置。",
                {"process": who, "client": c.name, "addresses": addrs, "ports": ports}))
    return out


def _is_wildcard(address: str) -> bool:
    ip = _ip(address)
    return ip is not None and ip.is_unspecified


def is_own_adapter(name: str) -> bool:
    """是不是我们自己的虚拟网卡 (名字是 config_gen.DEVICE, 不分大小写)."""
    return name.strip().lower() == DEVICE.lower()


def _adapter_findings(ctx: _Ctx) -> list[Finding]:
    snap = ctx.snap
    if {"adapters", "routes"} & set(snap.errors):
        return []
    out: list[Finding] = []
    for a in snap.adapters:
        if is_own_adapter(a.name):  # 我们自己的网卡不算"别的 TUN", 也不算遗留
            out.append(_own_adapter(ctx, a))
            continue
        if adapter_kind(a) != "tunnel":
            continue
        if ctx.is_up(a):
            t = ctx.takeover_of(a.index)
            addrs = [f"{x.ip}/{x.prefix}" for x in snap.addresses if x.if_index == a.index and not x.ip.lower().startswith("fe80")]
            out.append(_other_tun(ctx, a, t) if t.taken else _virtual_adapter(a, addrs))
        else:
            left = _leftover(ctx, a)
            if left is not None:
                out.append(left)
    return out + _unlisted_interfaces(ctx)


def _own_adapter(ctx: _Ctx, a: Adapter) -> Finding:
    if ctx.is_up(a):
        t = ctx.takeover_of(a.index)
        detail = f"网卡 {a.name}（{a.description}）通着" + (
            f"；它上面生效的路由合起来覆盖了{t.coverage_text()}，接管了默认路由（路由：{t.routes_text()}）。" if t.taken else "，没有接管默认路由。")
        return Finding("own_tun_active", NOTE, f"我们的虚拟网卡 {a.name} 开着", detail, data={"adapter": a.name, "takeover": t.taken, **t.data()})
    detail = (f"网卡 {a.name}（{a.description}，状态 {a.status}）还在系统里，但不是通的。它跟着我们起的核心进程一起出现、一起消失，"
              "进程没了它还留着，多半是核心被异常结束了。")
    return Finding("own_tun_stale", RISK, "我们的虚拟网卡还在，但不是通的", detail, "到网络页点一键恢复；不行就重启电脑。",
                   {"adapter": a.name, "description": a.description, "status": a.status})


def _unlisted_interfaces(ctx: _Ctx) -> list[Finding]:
    """网卡列表里没有、接口表和路由表里有的接口 (系统自带的 VPN 连接就是这样): 通着 (有 IP 接口是 Connected) 而且路由覆盖一半或更多,
    也是别人接管了默认路由. 回环那种覆盖很小的不受影响 (系统自带的伪网卡、虚拟机的网卡、我们自己的也跳过)."""
    snap = ctx.snap
    listed = {a.index for a in snap.adapters}
    names: dict[int, str] = {}
    for i in snap.ip_interfaces:
        names.setdefault(i.if_index, i.adapter)
    for r in snap.routes:
        names.setdefault(r.if_index, r.adapter)
    out = []
    for index, name in sorted(names.items()):
        if index in listed or not ctx.connected.get(index, False) or is_own_adapter(name):
            continue
        if adapter_kind(Adapter(name=name, index=index)) in ("pseudo", "virtual"):
            continue
        t = ctx.takeover_of(index)
        if t.taken:
            out.append(_other_interface(name, t))
    return out


def _other_interface(name: str, t: Takeover) -> Finding:
    detail = (f"接口 {name} 不在网卡列表里，但它是通的，上面生效的路由合起来覆盖了{t.coverage_text()}，等于接管了默认路由（路由：{t.routes_text()}）。"
              "多半是系统自带的 VPN 连接。这时再开我们的 TUN 会和它抢路由。")
    return Finding("other_tun_active", BLOCK, f"别的网络接口 {name} 已经接管了默认路由（多半是系统自带的 VPN 连接）", detail,
                   "先断开这个 VPN 连接（Windows 设置 → 网络和 Internet → VPN），再来开我们的；我们不替你断。",
                   {"adapter": name, "description": "", "in_adapter_list": False, "clients": [], **t.data()})


def _other_tun(ctx: _Ctx, a: Adapter, t: Takeover) -> Finding:
    guessed = _guess_clients(a, ctx.clients)
    names = _join(c.name for c in guessed)
    detail = (f"网卡 {a.name}（{a.description}）上生效的路由合起来覆盖了{t.coverage_text()}，等于接管了默认路由，现在大部分流量是从它走的"
              f"（路由：{t.routes_text()}）。")
    detail += (f"看起来是 {names} 在用它（在跑的进程：{_join(p for c in guessed for p in c.processes)}）。" if guessed
               else "没认出是哪个客户端在用它。")
    detail += "这时再开我们的 TUN 会和它抢路由。"
    hint = (f"先在 {names} 里把它的 TUN（虚拟网卡）模式关掉，再来开我们的；我们不替你关别的客户端。" if guessed
            else "先把开着这块虚拟网卡的程序里的 TUN（虚拟网卡）模式关掉，再来开我们的；我们不替你关别的程序。")
    return Finding("other_tun_active", BLOCK, f"别的虚拟网卡 {a.name} 已经接管了默认路由" + (f"（{names}）" if guessed else ""), detail, hint,
                   {"adapter": a.name, "description": a.description, "in_adapter_list": True, "clients": [c.name for c in guessed], **t.data()})


def _virtual_adapter(a: Adapter, addrs: list[str]) -> Finding:
    detail = f"网卡 {a.name}（{a.description}）是通的" + (f"，地址 {_join(addrs)}" if addrs else "") + "，但没有接管默认路由，和我们的 TUN 不冲突。"
    return Finding("virtual_adapter", NOTE, f"有虚拟网卡 {a.name} 在用，没有接管默认路由（不冲突）", detail,
                   data={"adapter": a.name, "description": a.description, "addresses": addrs})


def _leftover(ctx: _Ctx, a: Adapter) -> Finding | None:
    """以前遗留的隧道类网卡: 状态不是 Up, 但还留着手动地址、手动设的接口跃点、或持久的默认路由 (持久存储里的路由覆盖一半或更多;
    有其一即可)."""
    snap = ctx.snap
    manual = [f"{x.ip}/{x.prefix}" for x in snap.addresses if x.if_index == a.index and x.origin == "manual"]
    metrics: dict[int, list[str]] = {}  # {手动设的接口跃点: [地址族]}
    for i in snap.ip_interfaces:
        if i.if_index == a.index and not i.auto_metric:
            metrics.setdefault(i.metric, []).append(i.family)
    metrics = {m: sorted(set(fams)) for m, fams in metrics.items()}
    t = ctx.takeover_of(a.index, persistent=True)
    if not (manual or metrics or t.taken):
        return None
    left = []
    if manual:
        left.append(f"手动设的地址 {_join(manual)}")
    if metrics:
        left.append("接口跃点手动设成 " + "；".join(f"{m}（{_join(fams)}）" for m, fams in sorted(metrics.items())))
    if t.taken:
        if len(t.routes) <= SHOW_ROUTES:
            left.append("持久的默认路由 " + _join(f"{r.prefix} 经 {r.next_hop}（路由跃点 {r.metric}）" for r in t.routes))
        else:  # 排除网段拆出来的一长串: 不一条条列
            left.append(f"持久的路由 {len(t.routes)} 条，合起来覆盖了{t.coverage_text()}，等于默认路由（最大的几条：{t.routes_text()}）")
    detail = f"网卡 {a.name}（{a.description}，状态 {a.status}）上还留着：{'；'.join(left)}。"
    if t.taken:
        detail += "网卡断开所以现在不生效；一旦它连上，所有流量会被引过去。"
    else:
        detail += "网卡断开所以现在不生效；一旦它连上，这些设置会重新生效。"
    shown = [{"prefix": r.prefix, "next_hop": r.next_hop, "metric": r.metric} for r in t.routes[:SHOW_ROUTES]] if t.taken else []
    return Finding("leftover_adapter", RISK, f"发现以前遗留的虚拟网卡 {a.name}，是否清理？", detail,
                   "要清的话：设备管理器 → 网络适配器 → 找到它 → 卸载设备。体检只看不动。",
                   {"adapter": a.name, "description": a.description, "status": a.status, "manual_addresses": manual,
                    "manual_metrics": {str(m): fams for m, fams in metrics.items()}, "persistent_default_routes": shown,
                    "persistent_route_count": len(t.routes) if t.taken else 0,
                    "persistent_coverage": {"ipv4": round(t.v4, 3), "ipv6": round(t.v6, 3)} if t.taken else {}})


def _security_finding(ctx: _Ctx) -> Finding | None:
    found: dict[str, list[str]] = {}
    for p in ctx.snap.processes:
        label = security_label(p.name)
        if label and _pretty(p.name) not in found.setdefault(label, []):
            found[label].append(_pretty(p.name))
    if not found:
        return None
    detail = "；".join(f"{label}：进程 {_join(names)} 在跑" for label, names in found.items()) + "。"
    return Finding("security_software", NOTE, f"发现安全软件：{_join(found)}", detail, "开 TUN 时它可能拦驱动加载或核心程序，到时候要你点放行。",
                   {"products": found})


def _env_finding(ctx: _Ctx) -> Finding | None:
    if "env_proxy" in ctx.snap.errors:
        return None
    env = ctx.snap.env_proxy
    names = [n for n in ("http_proxy", "https_proxy", "all_proxy") if getattr(env, n).present]
    if not names or env.no_proxy.present:
        return None
    where = {n: _hostport(getattr(env, n).host, getattr(env, n).port) if getattr(env, n).host else "" for n in names}
    detail = "；".join(f"{n}" + (f" 指向 {w}" if w else "") for n, w in where.items())
    return Finding("env_proxy_no_bypass", RISK, f"用户环境变量里设了代理（{_join(names)}），但没有 no_proxy",
                   f"{detail}。没有 no_proxy 时，访问本机地址的程序也可能被代理绕回来。",
                   "在用户环境变量里补一个 no_proxy（至少写 localhost,127.0.0.1,::1）。体检不改它。", {"vars": where})


def _ipv6_finding(ctx: _Ctx) -> Finding | None:
    snap = ctx.snap
    if {"adapters", "routes"} & set(snap.errors):
        return None
    names = []
    for a in snap.adapters:
        if adapter_kind(a) == "physical" and not is_own_adapter(a.name) and ctx.is_up(a) and any(
                r.if_index == a.index and not r.persistent and r.prefix.strip().lower() == "::/0" for r in snap.routes):
            names.append(a.name)
    if not names:
        return None
    return Finding("ipv6_default_route", NOTE, f"物理网卡 {_join(names)} 上有 IPv6 默认路由",
                   "以后开 TUN 时，游戏的流量可能走 IPv6 绕过虚拟网卡。", data={"adapters": names})


# ---- 入口 -------------------------------------------------------------------------------------------------

def analyze(snap: Snapshot, *, elevated: bool | None = None) -> Report:
    """体检分析: 纯函数. elevated: 服务端是不是以管理员身份在跑 (命令行里用 wm.privilege.is_elevated() 取了传进来); None 是不知道."""
    ctx = _Ctx(snap)
    findings: list[Finding] = []
    if elevated is True:
        findings.append(_server_elevated())
    if snap.errors:
        findings.append(_collect_partial(snap))
    findings += _adapter_findings(ctx)
    proxy_findings, from_system = _system_proxy_findings(ctx)
    candidates = _upstream_candidates(ctx, from_system)
    findings += proxy_findings
    need = {"system_proxy", "listeners", "processes"}
    if not candidates and not (need & set(snap.errors)):
        findings.append(Finding("no_upstream", MISSING, "找不到可以当上游的本机代理端口",
                                "系统代理没开（或指向的端口没人听），也没认出正在监听端口的代理客户端。",
                                "先启动你平时用的代理客户端（它要在本机开一个代理端口），再来体检。"))
    findings += _listener_findings(ctx)
    findings += [f for f in (_env_finding(ctx),) if f]
    findings += [_client_found(c) for c in ctx.clients]
    findings += [f for f in (_security_finding(ctx), _ipv6_finding(ctx)) if f]
    findings.sort(key=lambda f: _SHOW[f.level])  # 稳定排序: 同档的保持上面的顺序
    level = LEVELS[max((_RANK[f.level] for f in findings if f.level in _RANK), default=0)]  # 提示不计档
    return Report(level=level, findings=findings, clients=ctx.clients, upstream_candidates=candidates)
