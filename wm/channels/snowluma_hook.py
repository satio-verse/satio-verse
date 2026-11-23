"""替用户点 SnowLuma 管理页里的"挂接" (2026-11-07 用户: "每次我都需要去重启 snowluma 的 bat, 太不方便了").

背景: SnowLuma 的 hookAutoLoad 是关的, SnowLuma 一重启, 她的 QQ 就要在它的管理页手动挂接一次. 上游的 SNOWLUMA_HOOK_AUTOLOAD=1 会把电脑上
发现的所有 QQ 都挂上 (这台电脑上还有另一个号), 所以不用它, 改成程序替人点管理页里的按钮, 而且只点她那个号的:

  POST /api/login {password}                -> {success, token, mustChangePassword} / 401 密码错 / 200 {needsTotp} / 429 登录太多次
  GET  /api/processes (x-snowluma-token)    -> {list: [{pid, injected, connected, loggedIn, uin, status, error, account: {uin, nickname}|null}]}
  POST /api/processes/<pid>/load            -> {success, process}     (管理页上的"挂接"按钮就是它)
  POST /api/logout

(接口是照 SnowLuma 2.2.0 发行包里自带的管理页前端和服务端代码读出来的, 没有额外的文档; 版本变了接口可能跟着变, 对不上就报
protocol, 不乱试.) 规矩:
- 只对 account.uin == 她的号 的那个进程点挂接; 认不出账号的 (登录界面里的 QQ) 不碰, 同一个号有两个进程 (认不准) 也不碰;
- 管理页 15 分钟内输错 5 次密码会把本机 (包括主人的浏览器) 锁 15 分钟: 密码被拒 (401) 一次就停, 由调用方记下来, 等主人换了密码再试;
- 开了两步验证 (needsTotp)、要先同意协议 (consentRequired)、要先改密码 (mustChangePassword): 这些得主人本人在管理页做, 这里不替做;
- 只连本机 127.0.0.1, 不走代理, 不跟随跳转; 每个请求有超时, 回应最多读 1 MB; 用完退出登录;
- 密码只在请求体里出现一次, 错误信息、状态里都不带它 (异常只记类型).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

LOOPBACKS = ("127.0.0.1", "::1")
MAX_BODY = 1 << 20
UA = "WorldModel-Pet/1.0 (local; qq-attach)"
DEFAULT_WEBUI_PORT = 5099


class HookError(RuntimeError):
    """挂接没办成. kind: network (连不上、超时) / rejected (密码被拒) / locked (登录次数太多被锁) / totp (开了两步验证) /
    consent (管理页要先同意协议) / must_change (要先改密码) / ambiguous (同一个号认出两个进程) / server (它回了错) /
    protocol (回的东西对不上, 版本可能变了). text 是给人看的一句, 不带密码."""

    def __init__(self, kind: str, text: str, wait_s: float = 0.0):
        super().__init__(text)
        self.kind, self.text, self.wait_s = kind, text, wait_s


@dataclass
class AttachResult:
    state: str                # attached / refreshed / already / login_window / no_account / no_qq
    detail: str
    pid: int | None = None
    waiting: list[int] = field(default_factory=list)  # 认不出账号的 QQ 主进程 (多半停在登录界面)
    seen: int = 0


# -- 本机进程 ---------------------------------------------------------------------
def _norm(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def process_names() -> dict[int, str]:
    """{pid: 映像名}. Windows 上一次系统调用拿全 (wm.resources._nt_processes, 约 12 ms): psutil.process_iter 在这台电脑上要 3–8 秒,
    因为打不开的进程 (别的用户的、受保护的, 约 200 个) 每个都要拍一次全系统快照, 快照期间一直握着 GIL, 主线程跟着卡
    (2026-11-01 查过, 见知识库 资源监测). 别的系统、或那个调用失败时退回 psutil."""
    if os.name == "nt":
        try:
            from ..resources import _nt_processes
            return {pid: name for pid, (name, _cpu) in _nt_processes().items()}
        except (OSError, ValueError, ImportError):
            pass
    import psutil
    return {p.info["pid"]: str(p.info.get("name") or "") for p in psutil.process_iter(["pid", "name"])}


def node_scripts() -> list[tuple[int, Path | None]]:
    """每个 node 进程跑的脚本 (绝对路径); 读不到命令行 (打不开、已经退出一半) 的给 None. 只细查名字叫 node 的那几个进程."""
    import psutil
    out: list[tuple[int, Path | None]] = []
    for pid, name in process_names().items():
        if name.lower() not in {"node.exe", "node"}:
            continue
        try:
            proc = psutil.Process(pid)
            args = proc.cmdline()
            if not args:
                out.append((pid, None))
            elif len(args) >= 2:  # 只有 node 没有脚本的 (交互模式) 不算
                script = Path(args[1])
                out.append((pid, script if script.is_absolute() else Path(proc.cwd()) / script))
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError):
            out.append((pid, None))
    return out


def snowluma_pids(directory: Path) -> list[int]:
    """跑着这个目录里的 index.mjs 的 node 进程 (bat 起的有一层外壳, 两层都算)."""
    entry = _norm(Path(directory) / "index.mjs")
    return [pid for pid, script in node_scripts() if script is not None and _norm(script) == entry]


def qq_main_pids() -> list[int]:
    """电脑上 QQ 的主进程 (名字是 QQ.exe、命令行里没有 --type= 的; 渲染 / GPU / 工具进程不算)."""
    import psutil
    out: list[int] = []
    for pid, name in process_names().items():
        if name.lower() not in {"qq.exe", "qq"}:
            continue
        try:
            if not any(a.startswith("--type=") for a in psutil.Process(pid).cmdline()):
                out.append(pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, OSError):
            continue
    return out


def listening_ports(pids: list[int]) -> list[int]:
    """这些进程在本机监听的 TCP 端口."""
    import psutil
    ports: set[int] = set()
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            conns = (getattr(proc, "net_connections", None) or proc.connections)(kind="tcp")
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        for c in conns:
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.ip in LOOPBACKS + ("0.0.0.0", "::"):
                ports.add(c.laddr.port)
    return sorted(ports)


def configured_port(directory: Path) -> int:
    """config/runtime.json 里的 webuiPort (读不到用 5099)."""
    try:
        port = int(json.loads((Path(directory) / "config" / "runtime.json").read_text(encoding="utf-8")).get("webuiPort"))
        return port if 0 < port < 65536 else DEFAULT_WEBUI_PORT
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_WEBUI_PORT


# -- 管理页 -----------------------------------------------------------------------
async def _body(resp: aiohttp.ClientResponse) -> dict:
    if (resp.content_length or 0) > MAX_BODY:
        raise HookError("protocol", "SnowLuma 管理页回的东西太大, 不像是它")
    raw, total = [], 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if total > MAX_BODY:
            raise HookError("protocol", "SnowLuma 管理页回的东西太大, 不像是它")
        raw.append(chunk)
    try:
        data = json.loads(b"".join(raw) or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _session(timeout_s: float) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s), trust_env=False,
                                 headers={"User-Agent": UA}, connector=aiohttp.TCPConnector(force_close=True))


async def probe_webui(port: int, timeout_s: float = 2.0) -> bool:
    """这个端口是不是 SnowLuma 的管理页 (GET /api/ui/public 不用登录, 回 {appearance: …})."""
    try:
        async with _session(timeout_s) as http:
            async with http.get(f"http://127.0.0.1:{int(port)}/api/ui/public", allow_redirects=False) as r:
                return r.status == 200 and "appearance" in await _body(r)
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError, HookError):
        return False


async def find_webui(directory: Path) -> int | None:
    """SnowLuma 的管理页在哪个端口 (默认 5099, 被占用时它会换 5100 …): 先看它的进程监听了什么, 再试配置里的端口和后面两个."""
    pids = await asyncio.to_thread(snowluma_pids, directory)
    ports = await asyncio.to_thread(listening_ports, pids) if pids else []
    base = configured_port(directory)
    for port in dict.fromkeys([*ports, base, base + 1, base + 2]):
        if await probe_webui(port):
            return port
    return None


async def _login(http: aiohttp.ClientSession, base: str, password: str) -> str:
    try:
        async with http.post(f"{base}/api/login", json={"password": password}, allow_redirects=False) as r:
            data = await _body(r)
            status = r.status
    except asyncio.TimeoutError:
        raise HookError("network", "SnowLuma 管理页登录超时") from None
    except (aiohttp.ClientError, OSError) as exc:
        raise HookError("network", f"连不上 SnowLuma 管理页 ({type(exc).__name__})") from None
    if status == 429:
        import re
        m = re.search(r"(\d+)\s*秒", str(data.get("message") or ""))
        wait = float(m.group(1)) if m else 900.0
        raise HookError("locked", f"SnowLuma 管理页说登录次数太多, 要等 {int(wait)} 秒", wait_s=wait)
    if status == 401:
        raise HookError("rejected", "SnowLuma 管理页说密码不对")
    if status != 200:
        raise HookError("server", f"SnowLuma 管理页登录回了 HTTP {status}")
    if data.get("needsTotp"):
        raise HookError("totp", "SnowLuma 管理页开了两步验证, 程序没法替你输验证码")
    token = data.get("token")
    if data.get("success") is not True or not isinstance(token, str) or not token:
        raise HookError("protocol", "SnowLuma 管理页登录的回复对不上 (版本变了?)")
    return token


async def _call(http: aiohttp.ClientSession, method: str, url: str, token: str, timeout_s: float | None = None) -> tuple[int, dict]:
    kw = {"timeout": aiohttp.ClientTimeout(total=timeout_s)} if timeout_s else {}
    try:
        async with http.request(method, url, headers={"x-snowluma-token": token}, allow_redirects=False, **kw) as r:
            return r.status, await _body(r)
    except asyncio.TimeoutError:
        raise HookError("network", "SnowLuma 管理页没回话 (超时)") from None
    except (aiohttp.ClientError, OSError) as exc:
        raise HookError("network", f"和 SnowLuma 管理页的连接断了 ({type(exc).__name__})") from None


def _gate(status: int, data: dict) -> None:
    """管理页的"先做别的"关卡."""
    if status == 403 and data.get("consentRequired"):
        raise HookError("consent", "SnowLuma 管理页要先同意用户协议和隐私政策, 这得你本人在管理页上点")
    if status == 403 and data.get("mustChangePassword"):
        raise HookError("must_change", "SnowLuma 管理页要先改密码, 这得你本人在管理页上做")
    if status == 401:
        raise HookError("server", "SnowLuma 管理页不认刚拿到的令牌")


def _uin_of(p: dict) -> str:
    acct = p.get("account") if isinstance(p.get("account"), dict) else {}
    return str(acct.get("uin") or p.get("uin") or "").strip()


async def attach_account(port: int, password: str, uin: int, timeout_s: float = 6.0) -> AttachResult:
    """登录管理页 -> 找 account.uin == uin 的 QQ 进程 -> 没挂上就点"挂接" -> 退出登录. 失败抛 HookError."""
    base = f"http://127.0.0.1:{int(port)}"
    want = str(int(uin))
    async with _session(timeout_s) as http:
        token = await _login(http, base, str(password))
        try:
            status, data = await _call(http, "GET", f"{base}/api/processes", token)
            _gate(status, data)
            if status != 200 or not isinstance(data.get("list"), list):
                raise HookError("protocol", "SnowLuma 管理页的进程列表对不上 (版本变了?)")
            procs = [p for p in data["list"] if isinstance(p, dict) and isinstance(p.get("pid"), int)]
            mine = [p for p in procs if _uin_of(p) == want]
            if len(mine) > 1:
                raise HookError("ambiguous", f"认出 {len(mine)} 个 QQ 进程都是她的号, 不敢乱挂, 请在管理页手动处理")
            if not mine:
                waiting = [p["pid"] for p in procs if not _uin_of(p)]
                if waiting:
                    return AttachResult("login_window", f"QQ 里有 {len(waiting)} 个没登录 (或认不出账号), 没有她的号 {want}",
                                        waiting=waiting, seen=len(procs))
                if procs:
                    return AttachResult("no_account", f"电脑上的 QQ 登录的都不是她的号 {want}, 没动", seen=len(procs))
                return AttachResult("no_qq", "SnowLuma 没看到 QQ 进程", seen=len(procs))
            p = mine[0]
            if p.get("injected") and p.get("connected"):
                return AttachResult("already", "她的 QQ 已经挂着了", pid=p["pid"], seen=len(procs))
            action = "refresh" if p.get("injected") else "load"  # 挂过但连接断了: 刷新 (管理页上的"刷新"按钮); 没挂过: 挂接
            status, data = await _call(http, "POST", f"{base}/api/processes/{p['pid']}/{action}", token, timeout_s=25)
            _gate(status, data)
            proc = data.get("process") if isinstance(data.get("process"), dict) else {}
            if status != 200 or data.get("success") is not True:
                why = str(proc.get("error") or data.get("message") or "")[:80]
                raise HookError("server", f"SnowLuma 说{'刷新' if action == 'refresh' else '挂接'}没成功" + (f": {why}" if why else ""))
            return AttachResult("refreshed" if action == "refresh" else "attached",
                                "刷新了她的 QQ 的连接" if action == "refresh" else "替她挂接了 QQ", pid=p["pid"], seen=len(procs))
        finally:
            with contextlib.suppress(Exception):  # 退出登录没成也不影响结果 (令牌 24 小时后自己过期); 取消照样往外传
                await _call(http, "POST", f"{base}/api/logout", token, timeout_s=3)
