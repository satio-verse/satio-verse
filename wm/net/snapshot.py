"""一次网络体检采到的原始数据 (2026-11-11 网络插件第 0 步: 只读体检).

采集 (collect.py) 只把系统当时的样子原样记下来; 分析 (checkup.py) 只看这份记录、不做任何输入输出. 所以体检可以对着假数据测,
也可以把真机的一份快照存下来以后再分析 (python -m wm.net --save / --from-file).

记录里有什么: 进程 (pid、父 pid、名字)、服务 (名字、状态、pid)、TCP 监听、网卡 (含隐藏的)、网卡地址、网卡的接口跃点和连接状态、
路由 (生效的和持久存储里的分开标)、系统代理、用户级环境变量里的代理. 没有什么: 命令行、程序路径、账号、密码. 代理地址只留主机和端口,
带用户名密码的地址在采集那一步就去掉了账号密码 (split_host_port / strip_userinfo), 任何地方都不存、不打印. 进程、服务、监听采到就筛
(trim.py), 只留分析用得上的那十来个, 用户开着什么程序、装了什么服务不进快照.

快照是原始数据, 只在内存里用; 给她、给面板、给模型的只能是 Report (checkup.py 的分析结果), 不能是快照. (命令行的 --save 是给开发者
自己调试存的.)

某一项没采到时, 它的列表是空的, 原因记在 errors 里 ({项名: 原因}); 分析时用 errors 区分"没有"和"没采到".
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any

SCHEMA = 1  # 快照格式的版本: 以后只加字段、不改旧字段的意思


@dataclass
class Process:
    pid: int = 0
    ppid: int = 0
    name: str = ""  # 像 sing-box.exe; 只有名字, 没有命令行和路径


@dataclass
class Service:
    name: str = ""
    state: str = ""  # Running / Stopped ...
    pid: int = 0  # 没在跑是 0


@dataclass
class Listener:
    address: str = ""  # 0.0.0.0 / :: / 127.0.0.1 / ...
    port: int = 0
    pid: int = 0


@dataclass
class Adapter:
    name: str = ""
    description: str = ""
    status: str = ""  # Up / Disconnected / Not Present / Disabled ...
    index: int = 0  # 接口序号 (ifIndex), 地址、路由用它指回网卡


@dataclass
class Address:
    adapter: str = ""
    if_index: int = 0
    ip: str = ""
    prefix: int = 0
    origin: str = "system"  # manual 手动 / dhcp / system 系统自带 (链路本地、回环、路由器通告等)


@dataclass
class IPInterface:
    adapter: str = ""
    if_index: int = 0
    family: str = ""  # IPv4 / IPv6
    metric: int = 0  # 接口跃点
    auto_metric: bool = True  # 跃点是不是系统自动算的 (False = 手动设的)
    state: str = ""  # Connected / Disconnected


@dataclass
class Route:
    prefix: str = ""  # 0.0.0.0/0, 192.168.1.0/24, ::/0 ...
    next_hop: str = ""
    metric: int = 0  # 路由跃点
    if_metric: int = 0  # 接口跃点
    adapter: str = ""
    if_index: int = 0
    persistent: bool = False  # True: 持久存储里的 (重启后还在); False: 当前生效的 (断开的网卡上的路由也会在这里)


@dataclass
class SystemProxy:
    enabled: bool = False
    server: str = ""  # ProxyServer 原样 (只去掉可能夹带的账号密码): host:port 或 http=...;https=...;socks=...
    pac: bool = False  # 有没有设 PAC 脚本 (只记有没有, 地址里可能带口令, 不存)


@dataclass
class EnvProxyItem:
    present: bool = False
    host: str = ""
    port: int | None = None


@dataclass
class EnvProxy:
    """用户级环境变量 (HKCU 下的 Environment) 里的代理; no_proxy 只记有没有."""
    http_proxy: EnvProxyItem = field(default_factory=EnvProxyItem)
    https_proxy: EnvProxyItem = field(default_factory=EnvProxyItem)
    all_proxy: EnvProxyItem = field(default_factory=EnvProxyItem)
    no_proxy: EnvProxyItem = field(default_factory=EnvProxyItem)


ENV_NAMES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
LIST_SECTIONS = {  # 快照里的列表项: 名字 -> 每一行的类型
    "processes": Process, "services": Service, "listeners": Listener, "adapters": Adapter, "addresses": Address,
    "ip_interfaces": IPInterface, "routes": Route,
}


# ---- 地址里只留主机和端口 -------------------------------------------------------------------------------

_ENTRY = re.compile(r"^(?P<pre>(?:[A-Za-z][A-Za-z0-9]*=)?(?:[A-Za-z][A-Za-z0-9+.\-]*://)?)(?P<info>.*)@(?P<rest>[^@]*)$")


def strip_userinfo(text: str) -> str:
    """把 'http=user:pw@host:port;socks=host:port' 这类写法里的 user:pw@ 去掉, 别的原样 (按分号一段一段处理).
    账号密码里可能有 @ : / 之类的字符, 所以以每一段里最后一个 @ 为界."""
    out = []
    for part in text.split(";"):
        m = _ENTRY.match(part.strip())
        out.append(m.group("pre") + m.group("rest") if m else part)
    return ";".join(out)


def split_host_port(address: str) -> tuple[str, int | None]:
    """'http://user:pw@127.0.0.1:7890/' -> ('127.0.0.1', 7890). 账号密码、协议、路径、查询都丢掉, 只留主机和端口;
    解析不了是 ('', None). 以最后一个 @ 为界, 所以密码里带 / : ? # 也不会漏进主机或端口."""
    text = address.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.rpartition("@")[2]
    for sep in "/?#":
        text = text.split(sep, 1)[0]
    text = text.strip()
    if not text:
        return "", None
    if text.startswith("["):  # [::1]:7890
        end = text.find("]")
        if end < 0:
            return "", None
        rest = text[end + 1:]
        return text[1:end], _port(rest[1:]) if rest.startswith(":") else None
    if text.count(":") > 1:  # 没带方括号的 IPv6, 没法再带端口
        return text, None
    host, sep, tail = text.partition(":")
    if not sep or not tail:
        return host, None
    if not tail.isdigit():
        return "", None
    return host, _port(tail)


def _port(text: str) -> int | None:
    return int(text) if text.isdigit() and 0 < int(text) < 65536 else None


def drop_zone(address: str) -> str:
    """fe80::1%12 -> fe80::1 (IPv6 地址后面的网卡序号不要)."""
    return address.partition("%")[0]


# ---- 宽松地读回来 ---------------------------------------------------------------------------------------

def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _opt_int(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str:
    return "" if v is None else str(v)


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


_CONVERT = {"int": _int, "str": _str, "bool": _bool, "int | None": _opt_int}  # 键是 dataclass 字段注解的文字 (from __future__ annotations)


def make(cls: type, row: Any) -> Any:
    """按 dataclass 的字段从一行字典里读: 缺的字段用默认值, 多的字段不要, 类型不对的转成对的类型."""
    kwargs = {}
    if isinstance(row, dict):
        for f in fields(cls):
            if f.name in row:
                conv = _CONVERT.get(f.type)
                kwargs[f.name] = conv(row[f.name]) if conv else row[f.name]
    return cls(**kwargs)


def as_rows(value: Any) -> list[dict]:
    """PowerShell 5.1 的 ConvertTo-Json 会把只有一个元素的数组写成单个对象: 单个对象按一行的列表处理."""
    if isinstance(value, dict):
        return [value]
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


@dataclass
class Snapshot:
    collected_at: str = ""  # UTC, ISO 8601
    processes: list[Process] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    listeners: list[Listener] = field(default_factory=list)
    adapters: list[Adapter] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)
    ip_interfaces: list[IPInterface] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    system_proxy: SystemProxy = field(default_factory=SystemProxy)
    env_proxy: EnvProxy = field(default_factory=EnvProxy)
    errors: dict[str, str] = field(default_factory=dict)  # 没采到的项: {项名: 原因}

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, **asdict(self)}

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> Snapshot:
        """从字典读回一份快照 (宽松: 缺的项是空的; 地址里夹带的账号密码、IPv6 的网卡序号读的时候也去掉)."""
        if not isinstance(data, dict):
            raise ValueError("快照要是一个 JSON 对象")
        snap = cls(collected_at=_str(data.get("collected_at")))
        for name, kind in LIST_SECTIONS.items():
            setattr(snap, name, [make(kind, r) for r in as_rows(data.get(name))])
        for item in snap.listeners:
            item.address = drop_zone(item.address)
        for item in snap.addresses:
            item.ip = drop_zone(item.ip)
        proxy = make(SystemProxy, data.get("system_proxy"))
        proxy.server = strip_userinfo(proxy.server)
        snap.system_proxy = proxy
        env = data.get("env_proxy") if isinstance(data.get("env_proxy"), dict) else {}
        for name in ENV_NAMES:
            item = make(EnvProxyItem, env.get(name))
            if item.host:
                host, port = split_host_port(item.host)
                item.host, item.port = host, item.port if item.port is not None else port
            setattr(snap.env_proxy, name, item)
        errors = data.get("errors")
        snap.errors = {str(k): str(v) for k, v in errors.items()} if isinstance(errors, dict) else {}
        return snap

    @classmethod
    def from_json(cls, text: str | bytes) -> Snapshot:
        return cls.from_dict(json.loads(text))
