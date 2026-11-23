"""只读采集: 把这台电脑的网络现状记成一份 Snapshot (2026-11-11 网络插件第 0 步).

collect() 是阻塞函数 (大约 5-10 秒), 调用方自己放进线程里. 全程只读, 只起一个 PowerShell 进程, 跑一段写死在本模块里的脚本
(SCRIPT), 脚本里只许用 ALLOWED_CMDLETS 里的命令 (tests/test_net_collect.py 逐条核对); 系统代理和用户级环境变量直接读注册表.

脚本怎么传: 整段脚本作为 powershell.exe 的 -Command 参数 (不把脚本编成 Base64 再传: 项目自己的守门把那种写法当可疑; 不用 -File:
要写临时文件、还受执行策略管; 不用标准输入: 管道进去的脚本按控制台代码页解码、多行块也容易出怪事). 脚本里只有 ASCII 和单引号,
所以不碰引号转义和代码页.

怎么分项容错: 脚本里每一项采集各自包在 try/catch 里, 采完立刻单独输出一行 "WMNET1|项名|ok|JSON" (或 "WMNET1|项名|err|原因");
所有查询都带 -ErrorAction Stop, 所以一项要么完整、要么整项记成出错 (不会悄悄少几条). Python 这边一行一行读: 某一项出错、JSON
坏了、或者超时被杀时根本没输出, 都只影响那一项, 记进 Snapshot.errors, 别的照常返回. 超时被杀时已经输出的几项照样能用
(所以最快、最要紧的几项排在前面).

采到以后立刻筛 (trim.py): 脚本一次采到全部进程名、服务、监听, 分析只用得上认得的客户端和安全软件的那十来个, 其余的不进快照.

几个已知的坑:
- 中文网卡名: 脚本开头把 [Console]::OutputEncoding 设成 UTF-8, 这边按 UTF-8 解 (带不带 BOM 都行).
- Windows PowerShell 5.1 的 ConvertTo-Json 管道进去的单元素数组会变成单个对象, -Depth 默认只有 2: 脚本用 -InputObject 传整个数组
  (单元素也是数组), 带 -Depth 4; Python 这边再兜一层 (单个对象按只有一行的列表读).
- 持久路由存储里没有东西时 Get-NetRoute 会报错 (消息是本地化的, 不能按文字判断): 那一项带 -ErrorAction SilentlyContinue, 当成空.
  生效的路由那一项照常 Stop, 所以模块真坏了那边会报出来.
- 断开的网卡上的路由也会出现在"生效"的列表里 (cfw-tap 就是): 所以网卡状态、地址、接口跃点、持久路由都要采准, 分析时按它们判断.
- 起进程带 CREATE_NO_WINDOW, 不弹黑窗; 列进程用 Win32_Process 只取 pid、父 pid、名字 (-Property 限定, 命令行和路径根本不取).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .snapshot import (ENV_NAMES, LIST_SECTIONS, EnvProxy, EnvProxyItem, Route, Snapshot, SystemProxy, as_rows, drop_zone, make,
                       split_host_port, strip_userinfo)
from .trim import trim

TAG = "WMNET1"

# 采集脚本. 只读: 只有 Get-* 查询; 每项单独输出一行, 排的是 "快而要紧的在前".
SCRIPT = r"""$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
function Emit([string]$Name, [scriptblock]$Block) {
  try {
    $rows = @(& $Block)
    [Console]::Out.WriteLine('WMNET1|' + $Name + '|ok|' + (ConvertTo-Json -InputObject $rows -Depth 4 -Compress))
  } catch {
    [Console]::Out.WriteLine('WMNET1|' + $Name + '|err|' + (ConvertTo-Json -InputObject ([string]$_.Exception.Message) -Compress))
  }
}
Emit 'adapters' { Get-NetAdapter -IncludeHidden | ForEach-Object { [pscustomobject]@{ name = [string]$_.Name; description = [string]$_.InterfaceDescription; status = [string]$_.Status; index = [int]$_.ifIndex } } }
Emit 'ip_interfaces' { Get-NetIPInterface | ForEach-Object { [pscustomobject]@{ adapter = [string]$_.InterfaceAlias; if_index = [int]$_.InterfaceIndex; family = [string]$_.AddressFamily; metric = [int]$_.InterfaceMetric; auto_metric = ([string]$_.AutomaticMetric -eq 'Enabled'); state = [string]$_.ConnectionState } } }
Emit 'addresses' { Get-NetIPAddress | ForEach-Object { [pscustomobject]@{ adapter = [string]$_.InterfaceAlias; if_index = [int]$_.InterfaceIndex; ip = [string]$_.IPAddress; prefix = [int]$_.PrefixLength; origin = [string]$_.PrefixOrigin } } }
Emit 'routes' { Get-NetRoute | ForEach-Object { [pscustomobject]@{ prefix = [string]$_.DestinationPrefix; next_hop = [string]$_.NextHop; metric = [int]$_.RouteMetric; if_metric = [int]$_.InterfaceMetric; adapter = [string]$_.InterfaceAlias; if_index = [int]$_.InterfaceIndex } } }
Emit 'routes_persistent' { Get-NetRoute -PolicyStore PersistentStore -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ prefix = [string]$_.DestinationPrefix; next_hop = [string]$_.NextHop; metric = [int]$_.RouteMetric; if_metric = [int]$_.InterfaceMetric; adapter = [string]$_.InterfaceAlias; if_index = [int]$_.InterfaceIndex } } }
Emit 'listeners' { Get-NetTCPConnection -State Listen | ForEach-Object { [pscustomobject]@{ address = [string]$_.LocalAddress; port = [int]$_.LocalPort; pid = [int]$_.OwningProcess } } }
Emit 'processes' { Get-CimInstance -ClassName Win32_Process -Property ProcessId,ParentProcessId,Name | ForEach-Object { [pscustomobject]@{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; name = [string]$_.Name } } }
Emit 'services' { Get-CimInstance -ClassName Win32_Service -Property Name,State,ProcessId | ForEach-Object { [pscustomobject]@{ name = [string]$_.Name; state = [string]$_.State; pid = [int]$_.ProcessId } } }
"""

# 脚本里允许出现的 "动词-名词" 命令 (tests/test_net_collect.py 拿它核对 SCRIPT): 查询 + 整理输出.
ALLOWED_CMDLETS = frozenset({
    "Get-CimInstance", "Get-NetTCPConnection", "Get-NetAdapter", "Get-NetIPAddress", "Get-NetIPInterface", "Get-NetRoute",
    "Select-Object", "Where-Object", "ForEach-Object", "Sort-Object", "ConvertTo-Json",
})
ALLOWED_CIM_CLASSES = frozenset({"Win32_Process", "Win32_Service"})  # Get-CimInstance 只许查这两个

# 脚本输出的项 (持久路由和生效路由是两项, 合并进 Snapshot.routes 时用 persistent 分开标)
PS_SECTIONS = ("adapters", "ip_interfaces", "addresses", "routes", "routes_persistent", "listeners", "processes", "services")

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
ENV_KEY = "Environment"


@dataclass
class RunResult:
    """起 PowerShell 那一步的结果 (测试里把 _run_powershell 换成假的, 返回这个)."""
    stdout: bytes = b""
    error: str = ""  # 没有跑完时的原因 (超时、起不来、异常退出); 跑完了是空
    timed_out: bool = False


def _clip(text: str, n: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "…"


def _why(exc: BaseException) -> str:
    return _clip(f"{type(exc).__name__}: {exc}", 120)


def _decode(raw: bytes) -> str:
    return (raw or b"").decode("utf-8-sig", errors="replace")


def _powershell_exe() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else "powershell.exe"


def _run_powershell(script: str, timeout_s: float) -> RunResult:
    """起一个 PowerShell 跑脚本 (整个采集只有这一处起进程). 超时杀掉, 已经输出的部分照样带回来."""
    argv = [_powershell_exe(), "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", script]
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout_s,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        return RunResult(exc.stdout or b"", f"超时（{timeout_s:g} 秒）没采完", True)
    except OSError as exc:
        return RunResult(b"", f"起不了 PowerShell（{_why(exc)}）")
    error = "" if done.returncode == 0 else f"PowerShell 异常退出（退出码 {done.returncode}）：{_clip(_decode(done.stderr))}"
    return RunResult(done.stdout, error)


def parse_output(text: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """读脚本的输出: 返回 ({项名: 行的列表}, {项名: 出错原因}). 不是 WMNET1 开头的行 (警告之类) 不管;
    一项出现两次以后一次为准; 只有一行的数组被写成单个对象也当列表读."""
    sections: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for line in text.split("\n"):  # 只按换行切: splitlines 还会在一些少见的控制字符上断开
        parts = line.strip().split("|", 3)
        if len(parts) != 4 or parts[0] != TAG:
            continue
        _, name, kind, body = parts
        sections.pop(name, None)
        errors.pop(name, None)
        if kind == "ok":
            try:
                value = json.loads(body)
            except ValueError:
                errors[name] = "PowerShell 的输出不是 JSON"
                continue
            if isinstance(value, (list, dict)):
                sections[name] = as_rows(value)
            else:
                errors[name] = "PowerShell 的输出格式不对"
        elif kind == "err":
            try:
                message = json.loads(body)
            except ValueError:
                message = body
            errors[name] = f"PowerShell 报错：{_clip(str(message))}"
        else:
            errors[name] = "PowerShell 的输出格式不对"
    return sections, errors


def _origin(raw: str) -> str:
    """Get-NetIPAddress 的 PrefixOrigin -> manual 手动 / dhcp / system 系统自带 (WellKnown、RouterAdvertisement 等)."""
    value = raw.strip().lower()
    return value if value in ("manual", "dhcp") else "system"


def _apply(snap: Snapshot, sections: dict[str, list[dict]]) -> None:
    for name in ("processes", "services", "listeners", "adapters", "addresses", "ip_interfaces"):
        if name in sections:
            setattr(snap, name, [make(LIST_SECTIONS[name], row) for row in sections[name]])
    for item in snap.listeners:
        item.address = drop_zone(item.address)
    for item in snap.addresses:
        item.ip = drop_zone(item.ip)
        item.origin = _origin(item.origin)
    routes: list[Route] = []
    for name, persistent in (("routes", False), ("routes_persistent", True)):
        for row in sections.get(name, ()):
            route = make(Route, row)
            route.persistent = persistent
            routes.append(route)
    snap.routes = routes


# ---- 注册表 (只读): 系统代理、用户级环境变量 --------------------------------------------------------------------

def _query_hkcu(subkey: str, names: tuple[str, ...]) -> dict:
    """只读: 从 HKEY_CURRENT_USER 下的 subkey 读指定的几个值 (值名不分大小写, 没有的不在结果里). 只按名字取这几个,
    不枚举整个键 (用户级环境变量里可能放着别的口令, 我们碰都不碰)."""
    import winreg
    out = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
        for name in names:
            try:
                out[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                continue
    return out


def system_proxy_from(values: dict) -> SystemProxy:
    """values: Internet Settings 里的 ProxyEnable / ProxyServer / AutoConfigURL (有几个给几个)."""
    try:
        enabled = int(values.get("ProxyEnable") or 0) == 1
    except (TypeError, ValueError):
        enabled = False
    server = strip_userinfo(str(values.get("ProxyServer") or "").strip())
    return SystemProxy(enabled=enabled, server=server, pac=bool(str(values.get("AutoConfigURL") or "").strip()))


def env_proxy_from(values: dict) -> EnvProxy:
    """values: {环境变量名: 值}, 名字不分大小写. 地址里的账号密码在这里就丢掉了: 快照里只有有没有、主机、端口."""
    lowered = {str(k).lower(): v for k, v in values.items()}
    env = EnvProxy()
    for name in ENV_NAMES:
        value = str(lowered.get(name) or "").strip()
        if not value:
            continue
        item = EnvProxyItem(present=True)
        if name != "no_proxy":
            item.host, item.port = split_host_port(value)
        setattr(env, name, item)
    return env


def read_system_proxy() -> SystemProxy:
    return system_proxy_from(_query_hkcu(INTERNET_SETTINGS, ("ProxyEnable", "ProxyServer", "AutoConfigURL")))


def read_env_proxy() -> EnvProxy:
    return env_proxy_from(_query_hkcu(ENV_KEY, ENV_NAMES))


def collect(timeout_s: float = 30) -> Snapshot:
    """采一份快照. 阻塞 (最多约 timeout_s 秒), 调用方自己放进线程里. 采不到的项记进 Snapshot.errors, 别的照常返回."""
    started = time.monotonic()
    snap = Snapshot(collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for name, read in (("system_proxy", read_system_proxy), ("env_proxy", read_env_proxy)):
        try:
            setattr(snap, name, read())
        except Exception as exc:  # noqa: BLE001 - 原因记进 errors, 别的项照常采
            snap.errors[name] = f"读不了注册表（{_why(exc)}）"
    run = _run_powershell(SCRIPT, max(1.0, timeout_s - (time.monotonic() - started)))
    sections, errors = parse_output(_decode(run.stdout))
    _apply(snap, sections)
    for name in PS_SECTIONS:
        if name not in sections and name not in errors:
            errors[name] = run.error or "PowerShell 没有给出这一项"
    snap.errors.update(errors)
    return trim(snap)  # 采到就筛: 分析用不上的进程、服务、监听不进快照 (见 trim.py)
