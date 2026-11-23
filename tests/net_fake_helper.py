"""假助手 (网络插件干跑用; 只在测试里): 在进程里顶替"提权的代办", 消息和错误码照《网络动作接口 v0.2》第 4.1 节的十步, 一个方法 handle(一行请求) ->
一行应答. 真代办 (宿主组以后做) 要过同一套契约测试 (tests/test_net_contract.py).

假的地方 (其余都是真的: 协议、配置生成、作业对象、起的子进程、交给看护方的三个句柄、看护方进程):
- 不提权: elevated 只是一个开关, 假装提没提权;
- protected_dir 假装是"只有管理员能写的目录", 其实是个临时目录;
- "下载来的核心文件" core_source 是假核心脚本 (tests/net_fake_core.py) 的一份拷贝, 名单 pinned 里记着它的 SHA-256;
- 确认窗是 confirm 回调 (返回 CONFIRM / CANCEL / TIMEOUT);
- 起核心: [python, 拷进去的假核心, "-d", 工作目录, "-f", 配置文件], 环境变量自己造 (只有 SystemRoot、windir、只含系统目录的 PATH、指到
  protected_dir 的 TEMP 和 TMP, 不继承测试进程的), 放进作业对象, 标准输出接一根管道;
- 起核心是"先起进程, 再放进作业对象" (Popen 之后 AssignProcessToJobObject): 中间有一个极短的空当, 核心要是在这个空当里自己又起了子进程, 那个
  子进程就不在作业里. 假核心不起子进程, 所以没事; **真代办应该先挂起 (CREATE_SUSPENDED)、放进作业对象、再放行**.

代办只认 net.start 和单独起的 net.restore (契约 4.4 末尾〔补 2〕的 7 步: 提权检查 -> 拿锁 -> 令牌四查 -> 参数 -> [确认窗, 开关, 默认不弹] -> 三步 -> 清 runs/ -> 审计 -> 应答; 演练只回
would, 什么都不动); net.stop、net.status、heartbeat 是看护方会话通道上的动作, 发给代办回 E_BAD_REQUEST.
单独起的 net.restore 的三步: kill_core (真起的时候在 runs/<编号>/session.json 里记了核心的进程号和创建时间, 恢复时按进程号打开、创建时间对得上才结束: 进程号会被重用)、
adapter_gone (等 runs/*/work/ 下的"网卡"标记没了, 最多 restore_adapter_timeout_s 秒)、flush_dns (假的: 往 data_dir/agent-flush.log 追加一行, 绝不调真的).
三步都没有 failed 才清 runs/ 下的子目录; 有 failed 就什么都不清 (记录、标记都留着), 重复恢复的结果才稳定: 结束不了的下次还认得出来、还能再试, 网卡赖着不走的每次都是 failed.
顺序 (契约 4.1): 解请求 -> 提权检查 -> 拿锁 -> 令牌四查 -> 后面各步. 只有真请求拿锁 (进程内的锁模拟跨进程的锁), 一直拿到应答为止; 拿不到回 E_BUSY, 这时
令牌还没查, 不作废; 演练不拿锁, 不会回 E_BUSY; 没提权的真请求在拿锁之前就回 E_NOT_ELEVATED. 演练应答里的 summary 照契约第 6 节末尾的例子写
(processes 去重以后的、upstream 是 host 加 port、core 是 id 加 version、heartbeat 是 interval_s 加 grace_s, 加 max_minutes、config_sha256).

提权的代办每条请求都往 protected_dir/audit.jsonl 写一行, 除非它确定是演练 (契约第 8 节〔补 3〕提权一侧: 时间、动作、令牌号、谁申请的、参数指纹和摘要、结果或错误码;
不记核心输出, 也没有 dry_run 字段: 它永远是假的). 没过第 1 节检查的请求 (解不出来、版本不对、动作不认识) 也记, 动作写 rejected, 令牌号、谁申请的、参数指纹和摘要空着.
各项只在通过了自己那一步的检查以后才写, 没过的空着 (null): 令牌号和谁申请的在令牌的形状通过以后才写 (protocol.token_shape_ok; 形状过了、后面查出过期、用过、对不上的照样写;
形状不对包括没带的、以及拿不到锁回 E_BUSY 的那条 -- 那时令牌还没查 -- 空着); 参数摘要在参数白名单通过以后才写; 参数指纹是代办自己算的 (解出请求以后先算、再去拿锁),
请求里有 params_json 就写. **演练 (确定是演练的) 不写受保护的目录, 审计也不写; 没提权的代办什么都不写**, 包括它拒掉的非演练请求 (E_NOT_ELEVATED) 和 rejected 的
(真的代办没提权时本来也写不了那个目录; 这些记录由宿主在普通权限这边记, 不归假助手管). 用过的令牌号记在 protected_dir/used-tokens.json, 留到过期.

起看护方的命令行一定带 --audit (写到 data/guardian-audit.jsonl) 和两个 --fake- 开头的参数; 真起的时候 --audit 不传 (看护方自己写到 ~/.worldmodel/serve/net/),
这里传是为了测试里绝对不往真的家目录写. 再加一层: 看护方进程的家目录 (USERPROFILE、HOME) 指到 data/fake-home, 万一哪天漏了 --audit, 默认位置也落在那里.

每次真起都在 protected_dir/runs/<编号>/ 下: core/ (拷进去的核心)、work/ (核心的工作目录: 配置、假核心写的几个文件)、tmp/ (核心的 TEMP).
出任何问题都整个删掉 ("不留半截"). 真请求过了第 6 步 (这时已经确定没有会话在跑) 以后, 把 runs/ 下旧的子目录都删掉; 演练什么都不删.
"网卡"是假核心在 work/ 里用"关句柄就删"的方式建的标记文件: 进程一没 (停了、被强杀、自己退了) 系统就把它删掉, 所以"网卡在不在"就是这个文件在不在,
不读它的内容、不用进程号 (进程号会被重用).

另有: make_token (假宿主发令牌)、FakeClock (可以拨的时钟)、start_line / restore_line / Built / request_line (造请求)、channel (对看护方的会话通道发一条请求)、
Rig (一套测试用的目录和假助手)、ProcessLedger (记着起过的进程, 收尾时不管成败都结束掉)、snapshot (给目录拍张照, 看有没有文件被动过)、
wait_for (带期限的轮询)、covers_agent / covers_guardian (给"每个错误码都有测试"那条用的标记)、
skip_unrelated_autouse_fixtures (盖掉 conftest.py 里和网络无关的 autouse 夹具, 省时间).
"""
from __future__ import annotations

import ast
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import pytest

from wm.net import config_gen, guardian, protocol, win32
from wm.net.protocol import (E_ALREADY_RUNNING, E_BAD_REQUEST, E_BUSY, E_CONFIG_MISMATCH, E_CONFIRM_TIMEOUT, E_CORE_HASH, E_CORE_MISSING,
                             E_CORE_UNKNOWN, E_INTERNAL, E_NOT_ELEVATED, E_START_FAILED, E_TOKEN_EXPIRED, E_TOKEN_MISMATCH, E_TOKEN_MISSING,
                             E_TOKEN_USED, E_UNKNOWN_ACTION, E_USER_DECLINED, HEARTBEAT, NET_RESTORE, NET_START, NET_STATUS, NET_STOP, Rejection,
                             Request, check_token, fail, ok, params_fingerprint)
from wm.net.guardian import DONE, FAILED, SKIPPED, STEP_ADAPTER, STEP_DNS, STEP_KILL, WOULD_RESTORE      # 恢复回报里的三步和 result: 和看护方的 net.restore 一个写法
from wm.net.recipe import Recipe, unique_processes

ROOT = Path(__file__).resolve().parents[1]
FAKE_CORE_SCRIPT = Path(__file__).resolve().parent / "net_fake_core.py"
ADAPTER_MARKER = "fake-adapter-WM-Ayane"          # 假核心建的"网卡"标记文件 (真的网卡叫 config_gen.DEVICE = WM-Ayane)
CORE_ID = "mihomo"
CORE_VERSION = "v1.19.34"
CONFIRM, CANCEL, TIMEOUT = "confirm", "cancel", "timeout"      # 确认窗的三种结果
WOULD_START = ["copy_core", "write_config", "start_core", "wait_adapter", "hand_over"]    # 演练时 net.start 会做的事 (契约第 6 节的例子)
SESSION_RECORD = "session.json"                 # runs/<编号>/session.json: 假助手认核心的办法 (核心的进程号和创建时间), 恢复时靠它
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLEAN_ENV_NAMES = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}      # 假核心看到的环境变量名只许是这几个 (Windows 上名字是大写的)

TOKEN_MESSAGES = {
    E_TOKEN_MISSING: "没带同意令牌",
    E_TOKEN_EXPIRED: "令牌过期了（有效期 120 秒，算到代办读到请求为止）",
    E_TOKEN_USED: "这张令牌已经用过了（一次性的）",
    E_TOKEN_MISMATCH: "令牌对不上：格式不对、批的不是这个动作、或者参数的指纹和令牌里的不一样",
}


def sha256_file(path: Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


_REAL_SELF_ELEVATED = guardian.self_elevated      # 导入时存下真的自查 (有的测试会把 guardian.self_elevated 换成假的, 这里认真的)
_ELEVATED_HERE: list = []                         # 缓存: 这个测试进程自己是不是提权的 (查一次)


def require_an_unelevated_test_process() -> None:
    """起真的看护方进程之前核对: 这个测试进程要是自己就是提权的 (比如在"以管理员身份运行"的终端里跑测试, 或者电脑关着提权框、用内置管理员账号登录),
    它起的看护方也是提权的, 看护方的自查会拒绝工作 (退出码 4; 契约第 10 节〔补 2〕, 这一版不支持). 那时直接说清楚原因, 不要让测试报一堆看不懂的 E_INTERNAL."""
    if not _ELEVATED_HERE:
        _ELEVATED_HERE.append(_REAL_SELF_ELEVATED())
    if _ELEVATED_HERE[0] is True:
        pytest.fail("这个测试进程是提权的（管理员权限）：它起的看护方也是提权的，看护方的自查会拒绝工作（退出码 4）。请在普通权限的终端里跑这些测试。", pytrace=False)


# tests/conftest.py 里的 autouse 夹具: 每条测试都要导入服务端、渠道、工作台那一批模块, 再建四五个临时目录, 为的是别的测试起服务端时不碰你真的数据.
# 网络插件这几个测试文件不碰那些东西; 在这台常被占满的机器上, 光这些夹具就占掉每条测试 70 毫秒上下 (250 条纯函数测试 20 秒, 盖掉以后 3 秒).
UNRELATED_AUTOUSE_FIXTURES = ("_no_real_deepseek_key", "_no_supervisor_env", "_no_loopback_capture", "_weixin_home", "_vault_home", "_agent_home",
                              "_services_home", "_no_download_service")
_IMPORT_FUNCTIONS = ("import_module", "__import__")


def foreign_wm_imports(source: str) -> list[str]:
    """源码里导入了项目里 wm.net 以外的模块的地方 (写成"第 N 行: 模块名"; 空 = 没有). 用 ast 读, 不是搜文字: import wm、import wm.services、
    from wm.privilege import x、from wm import services (x 多半是子模块, 记成 wm.services)、importlib.import_module("wm.xxx") 和 __import__("wm.xxx")
    里写死的字符串、函数里面的懒导入, 都算; wm.net 和它下面的 (wm.net.protocol、from wm import net) 不算."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [f"wm.{alias.name}" for alias in node.names] if node.module == "wm" else [node.module]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            func = node.func
            if (func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")) in _IMPORT_FUNCTIONS:
                names = [node.args[0].value]
        for name in names:
            if (name == "wm" or name.startswith("wm.")) and name != "wm.net" and not name.startswith("wm.net."):
                found.append(f"第 {node.lineno} 行: {name}")
    return found


def skip_unrelated_autouse_fixtures(namespace: dict) -> None:
    """在测试模块里用: `skip_unrelated_autouse_fixtures(globals())`. 往这个模块的命名空间里放同名的空夹具, 盖掉 conftest.py 里和网络无关的那 8 个 autouse
    夹具 (只对这个模块里的测试生效, 不改 conftest.py). 以后 conftest.py 里新加的 autouse 夹具照常对这个模块生效.

    **条件**: 调用它的这个测试文件 (namespace["__file__"]) 和这个辅助文件自己, 除了 wm.net 下面的模块, 不许导入项目里别的东西 (那 8 个夹具保护的是别的测试起服务端、
    碰微信/保管箱/工作台会话/服务状态时不碰真数据; 一个文件要是碰了那些东西, 就不能盖). 这一条在这里查, 不靠自觉: 读源码, 有就直接抛 RuntimeError, 所以
    测试文件一被收集就失败. 以后门面、借条那一块的测试要碰宿主和服务端, 不许用这个办法."""
    problems: list[str] = []
    seen: set[str] = set()
    for label, source_file in (("测试文件", namespace.get("__file__")), ("net_fake_helper.py", __file__)):
        if source_file is None:
            problems.append(f"{label}: 没有 __file__, 查不了它导入了什么")
            continue
        if str(source_file) in seen:
            continue
        seen.add(str(source_file))
        found = foreign_wm_imports(Path(source_file).read_text(encoding="utf-8"))
        problems += [f"{label} {Path(source_file).name} {item}" for item in found]
    if problems:
        raise RuntimeError("这个测试文件不能盖掉 conftest.py 的 autouse 夹具: 它导入了 wm.net 以外的项目模块 (" + "；".join(problems) + ")。"
                           "去掉这些导入，或者别调 skip_unrelated_autouse_fixtures。")
    for name in UNRELATED_AUTOUSE_FIXTURES:
        def nothing() -> None:
            return None
        namespace[name] = pytest.fixture(autouse=True, name=name)(nothing)


def wait_for(check: Callable, timeout_s: float, what: str, pause: float = 0.05):
    """带期限的轮询 (不拿固定的等待当同步): check() 返回真值就返回它, 到期了就失败 (期限要给足余量, 这台电脑常被别的任务占满)."""
    deadline = time.monotonic() + timeout_s
    while True:
        got = check()
        if got:
            return got
        if time.monotonic() >= deadline:
            raise AssertionError(f"{timeout_s:g} 秒内没等到: {what}")
        time.sleep(pause)


def covers_agent(*codes: str) -> Callable:
    """标记: 这条测试覆盖代办 (假助手、以后的真代办) 回的这几个错误码. 只是标记 (不改函数); 契约测试最后一条用 ast 读这些标记,
    核对契约第 5 节里代办会回的每个错误码都至少有一条测试."""
    def mark(func: Callable) -> Callable:
        return func
    return mark


def covers_guardian(*codes: str) -> Callable:
    """标记: 这条测试覆盖看护方回的这几个错误码 (用法同 covers_agent)."""
    def mark(func: Callable) -> Callable:
        return func
    return mark


class FakeClock:
    """可以拨的时钟 (假宿主和假助手共用, 令牌的有效期靠它测): 调用它得到现在的 UTC 秒."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_token(action: str, params_json: str, *, now: float, requested_by: str = "owner", ttl: int = 120, plugin: str = "net",
               issuer: str = "fake-host-1", **overrides: object) -> dict:
    """假宿主发的令牌 (契约第 2 节): 随机的 128 位十六进制号, 批 action 这一个动作, 绑 params_json 原文的 SHA-256, 有效期 ttl 秒.
    overrides 直接盖在字段上 (测试用来造坏令牌: 过期的、号重复的、指纹不对的...)."""
    token = {"id": secrets.token_hex(16), "action": action, "params_sha256": params_fingerprint(params_json), "plugin": plugin,
             "requested_by": requested_by, "issued_at": int(now), "expires_at": int(now) + ttl, "issuer": issuer}
    token.update(overrides)
    return token


@dataclass
class Built:
    """造好的一条 net.start 请求: line 是要发的那一行, params_json 和 token 方便测试对着改."""
    line: bytes
    params_json: str
    token: dict | None
    request: dict


def request_line(action: str, *, rid: str = "r1", params_json: str | None = None, token: object = None, dry_run: bool = False,
                 **extra: object) -> bytes:
    """一行请求 (照契约第 1 节). params_json 是字符串; token 是 None 就不带; extra 是额外的顶层键 (测试用来造不认识的键)."""
    obj: dict = {"v": protocol.VERSION, "id": rid, "action": action, "dry_run": dry_run}
    if token is not None:
        obj["token"] = token
    if params_json is not None:
        obj["params_json"] = params_json
    obj.update(extra)
    return protocol.encode(obj)


def start_line(recipe: Recipe, *, now: float, dry_run: bool = False, token: bool = True, requested_by: str = "owner",
               core_id: str = CORE_ID, core_version: str = CORE_VERSION, interval_s: int = 5, grace_s: int = 30, rid: str = "r1",
               token_overrides: dict | None = None, params_json: str | None = None) -> Built:
    """造一条合规的 net.start: 参数 (protocol.start_params)、令牌 (make_token, 绑着这段参数)、请求行. params_json 给了就用它 (造坏参数用)."""
    text = params_json if params_json is not None else protocol.start_params(recipe, core_id=core_id, core_version=core_version,
                                                                              interval_s=interval_s, grace_s=grace_s)
    tok = make_token(NET_START, text, now=now, requested_by=requested_by) if token else None
    if tok is not None:
        tok.update(token_overrides or {})                       # 盖在字段上 (连 action 也能盖: 造"令牌批的不是这个动作")
    line = request_line(NET_START, rid=rid, params_json=text, token=tok, dry_run=dry_run)
    return Built(line, text, tok, json.loads(line))


def restore_line(*, now: float, dry_run: bool = False, token: bool = True, requested_by: str = "owner", params_json: str | None = "{}", rid: str = "r1",
                 token_overrides: dict | None = None) -> Built:
    """造一条单独起的 net.restore: params_json 是 {} (给别的文本造"有键"的坏参数, 给 None 就不带), 令牌批 net.restore、绑着 {} (token=False 不带令牌).
    params_json 给了 None 又带令牌: 令牌绑的是 {}, 请求里没有原文, 令牌那一步就对不上了 (E_TOKEN_MISMATCH)."""
    bound = "{}" if params_json is None else params_json
    tok = make_token(NET_RESTORE, bound, now=now, requested_by=requested_by) if token else None
    if tok is not None:
        tok.update(token_overrides or {})
    line = request_line(NET_RESTORE, rid=rid, params_json=params_json, token=tok, dry_run=dry_run)
    return Built(line, bound, tok, json.loads(line))


def channel(pipe: str, action: str, params: dict | None = None, *, rid: str = "c1", timeout_s: float = 20.0, **extra: object) -> dict:
    """对看护方的会话通道发一条请求 (连上、发一行、收一行、断开), 返回应答 (已解析成字典). params 是个字典, 这里写成 params_json."""
    text = None if params is None else json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    return json.loads(win32.pipe_request(pipe, request_line(action, rid=rid, params_json=text, **extra), timeout_s))


def snapshot(base: Path) -> dict[str, tuple[int, str]]:
    """目录里每个文件 (相对路径) 的 (大小, SHA-256), 以及每个子目录 (大小 -1): 前后各拍一张, 相等就是没有任何东西被动过."""
    out: dict[str, tuple[int, str]] = {}
    for path in sorted(Path(base).rglob("*")):
        rel = path.relative_to(base).as_posix()
        if path.is_dir():
            out[rel] = (-1, "")
            continue
        try:
            out[rel] = (path.stat().st_size, sha256_file(path))
        except OSError:          # 被别的进程独占着 (比如活着的假核心的"网卡"标记: 关句柄就删的文件别的进程读不了)
            out[rel] = (-2, "unreadable")
    return out


class ProcessLedger:
    """记着测试起过的进程 (Popen 对象, 靠句柄分辨, 不会因为进程号被重用而误杀别人); 收尾时不管成败都结束掉."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []

    def add(self, proc: subprocess.Popen) -> subprocess.Popen:
        self.procs.append(proc)
        return proc

    def alive(self) -> list[int]:
        return [proc.pid for proc in self.procs if proc.poll() is None]

    def kill_all(self, timeout_s: float = 15.0) -> None:
        for proc in reversed(self.procs):                  # 后起的先杀: 先杀看护方, 核心跟着没, 看护方就不会在被收拾的当口又去做收尾
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        deadline = time.monotonic() + timeout_s
        for proc in self.procs:
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


@dataclass
class Session:
    """假助手起出来的一段会话 (测试用它找看护方、核心、管道)."""
    id: str
    pipe: str
    guardian: subprocess.Popen
    core: subprocess.Popen
    run_dir: Path
    work_dir: Path
    started_at: int
    deadline_at: int
    core_pid: int = 0
    guardian_pid: int = 0


@dataclass
class _Case:
    """一个请求的审计素材."""
    req: Request | None = None
    token_id: str | None = None
    requested_by: str | None = None
    params_sha256: str | None = None
    summary: dict | None = None
    run_dir: Path | None = None
    extra: dict = field(default_factory=dict)


class FakeHelper:
    """假的提权代办. 可以注入 (全是关键字参数):

    protected_dir   假装"只有管理员能写的目录"的临时目录; data_dir 是普通权限这边的目录 (放看护方的审计、"清 DNS 缓存"的假记录)
    core_source     "下载来的核心文件" (假核心脚本的一份拷贝); pinned 是 {(核心编号, 版本): SHA-256} 的名单
    elevated        假装提没提权 (没提权只接受演练); now 是时钟 (调用它得到 UTC 秒, 令牌用); confirm 是确认窗 (收一个字典, 返回 CONFIRM/CANCEL/TIMEOUT)
    start_timeout_s 等"网卡"出现最多多久 (默认 15 秒, 和契约一样); hand_over_timeout_s 等看护方的管道连得上最多多久
    behavior        假核心的行为 (写进工作目录的 fake-behavior.json, 见 net_fake_core.py); deadline_s 假助手专用: 不按 max_minutes 算时限, 按这个秒数
    ledger          进程账本 (起的核心和看护方都记进去); guardian_module 看护方的模块名 (测试"看护方起不来"时换掉)
    handover_fault  假助手专用的测试钩子: 故意把交给看护方的句柄交错 (测"看护方启动时按种类核对, 不对就以退出码 2 退出, 代办回 E_INTERNAL, 核心没有留下"):
                    "job_is_a_process" (--job 给了核心进程的句柄)、"process_not_in_job" (--process 给了另一个不在作业里的进程)、
                    "output_is_not_a_pipe" (--output 给了一个普通文件的句柄); "core_dead" 不是交错句柄, 是核心在看护方起来之前就已经被杀了
                    (句柄都是对的: 退了的进程还算在作业里, 看护方照常起来、发现核心退了、收尾);
                    "keep_job_handle" 造"残留": 交接以后故意不关自己手里的作业对象句柄 (契约警告过的那种毛病), 这时强杀看护方, 核心不会跟着没
                    (close() 里才关), 这就是"看护方不在、但有残留"
    confirm_restore 单独起的 net.restore 要不要弹确认窗 (默认不弹: 契约 4.4 现在这么定, 宿主组长还在等用户表态; 开着时在三步之前调 confirm, 内容是动作、
                    谁申请的、要做的三件事); restore_adapter_timeout_s 恢复里等"网卡"标记没了最多多久 (默认 10 秒, 和契约一样, 测试里用短的);
                    restore_kill_wait_s 结束核心以后等它退出最多多久 (默认 5 秒)
    """

    def __init__(self, *, protected_dir: Path, data_dir: Path, core_source: Path | None = None, pinned: dict | None = None,
                 elevated: bool = True, now: Callable[[], float] = time.time, confirm: Callable[[dict], str] | None = None,
                 start_timeout_s: float = 15.0, hand_over_timeout_s: float = 20.0, behavior: dict | None = None,
                 deadline_s: float | None = None, ledger: ProcessLedger | None = None, guardian_module: str = "wm.net.guardian",
                 handover_fault: str | None = None, confirm_restore: bool = False, restore_adapter_timeout_s: float = 10.0,
                 restore_kill_wait_s: float = 5.0) -> None:
        self.protected_dir = Path(protected_dir)
        self.data_dir = Path(data_dir)
        self.core_source = None if core_source is None else Path(core_source)
        self.pinned = dict(pinned or {})
        self.elevated = elevated
        self.now = now
        self.confirm = confirm or (lambda summary: CONFIRM)
        self.start_timeout_s = start_timeout_s
        self.hand_over_timeout_s = hand_over_timeout_s
        self.behavior = behavior
        self.deadline_s = deadline_s
        self.ledger = ledger or ProcessLedger()
        self.guardian_module = guardian_module
        self.handover_fault = handover_fault
        self.confirm_restore = confirm_restore
        self.restore_adapter_timeout_s = restore_adapter_timeout_s
        self.restore_kill_wait_s = restore_kill_wait_s
        self.runs_dir = self.protected_dir / "runs"
        self.audit_path = self.protected_dir / "audit.jsonl"
        self.used_path = self.protected_dir / "used-tokens.json"
        self.flush_log = self.data_dir / "flush.log"                 # 看护方的"清 DNS 缓存"往这里追加一行 (假的)
        self.agent_flush_log = self.data_dir / "agent-flush.log"     # 代办恢复里的"清 DNS 缓存"往这里追加一行 (假的, 和看护方那份分开; 绝不调真的)
        self.guardian_audit = self.data_dir / "guardian-audit.jsonl"
        self.guardian_stderr = self.data_dir / "guardian-stderr.log"
        self.fake_home = self.data_dir / "fake-home"       # 看护方进程的"家目录" (USERPROFILE): 万一漏了 --audit, 默认的审计位置也落在这里, 不会写进真的家目录; 正常情况下它不该出现
        self.session: Session | None = None
        self.pipes: list[str] = []                # 起出来的每一段会话的管道名 (收尾时核对没有留下)
        self.last_run_dir: Path | None = None
        self.confirm_calls: list[dict] = []
        self.started_processes = 0                # 起过几个进程 (核心和看护方): 演练和各种拒绝以后这个数必须还是 0
        self._leaked_jobs: list[int] = []         # handover_fault="keep_job_handle" 故意留着的作业对象句柄 (close() 里关)
        self._lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)       # (受保护目录不在这里建: 没提权的代办什么都不写, 要写的时候才建)

    # ---- 对外: 收一行请求, 回一行应答 ----

    def handle(self, line: bytes | str) -> bytes:
        """收一行请求, 回一行应答. 顺序 (契约 4.1): 解请求 -> 不是代办的动作 -> 第 1 步提权检查 -> 第 1 步拿锁 (只有真请求拿) -> 令牌四查 -> 后面各步.
        提权检查在拿锁之前: 没提权的真请求不管有没有别的请求在处理, 回的都是 E_NOT_ELEVATED. 拿不到锁回 E_BUSY, 这时令牌还没查, 当然不作废;
        锁一直拿到应答为止. 演练不拿锁, 不会回 E_BUSY."""
        decoded = protocol.decode_request(line)
        if isinstance(decoded, Rejection):
            self._record_rejected(decoded)                        # 没过第 1 节检查的请求 (解不出来、版本不对、动作不认识): 不知道它是不是演练, 提权的代办也记一行 (rejected)
            return protocol.encode(decoded.reply())
        case = _Case(req=decoded)
        if isinstance(decoded.params_json, str):
            case.params_sha256 = params_fingerprint(decoded.params_json)       # 参数指纹是代办自己算的, 请求里有 params_json 就记 (拿不到锁回 E_BUSY 的那条也有: 先算、再拿锁)
        reply = self._not_for_the_agent(decoded)
        if reply is None and not self.elevated and not decoded.dry_run:
            reply = fail(decoded.id, E_NOT_ELEVATED, "代办没有提权，只接受演练请求")       # 4.1 第 1 步: 没提权 -> 只接受演练
        locked = False
        if reply is None and not decoded.dry_run:
            locked = self._lock.acquire(blocking=False)             # 4.1 第 1 步的锁 (进程内的锁模拟跨进程的锁)
            if not locked:
                reply = fail(decoded.id, E_BUSY, "另一个代办正在处理真请求，稍后再发（这张令牌没有作废）")
        if reply is None:
            try:
                reply = self._dispatch(decoded, case)
            except Exception as exc:  # noqa: BLE001 - 契约的 E_INTERNAL: 别的
                reply = fail(decoded.id, E_INTERNAL, f"代办出了意外（{type(exc).__name__}）")
            finally:
                if locked:
                    self._lock.release()
        self._record(case, decoded, reply)
        return protocol.encode(reply)

    @staticmethod
    def _not_for_the_agent(req: Request) -> dict | None:
        """代办只认 net.start 和单独起的 net.restore (契约第 1 节). net.stop、net.status、heartbeat 是看护方会话通道上的动作, 发给代办回 E_BAD_REQUEST
        (和会话通道上收到 net.start 对称); 别的动作 (decode_request 已经拒过了, 这里是兜底) 回 E_UNKNOWN_ACTION."""
        if req.action in (NET_STOP, NET_STATUS, HEARTBEAT):
            return fail(req.id, E_BAD_REQUEST, f"代办不处理 {req.action}：它是看护方会话通道上的动作")
        if req.action not in (NET_START, NET_RESTORE):
            return fail(req.id, E_UNKNOWN_ACTION, f"代办只认 net.start 和 net.restore（收到 {req.action}）")
        return None

    def close(self) -> None:
        """收尾: 结束这个助手起的进程 (测试结束时调, 不管成败); 再关掉 handover_fault="keep_job_handle" 故意留着的作业对象句柄."""
        self.ledger.kill_all()
        for job in self._leaked_jobs:
            win32.close_handle(job)
        self._leaked_jobs.clear()

    def _token_step(self, req: Request, case: _Case, action: str) -> dict | None:
        """令牌四查 (契约第 2 节; 演练可以不带, 带了照查, 不作废): 过不了回失败的应答, 过了回 None; 真请求过了就作废 (不管后面哪一步不过都不退回).
        审计里令牌号和谁申请的: 令牌的形状通过以后就记 (后面查出过期、用过、对不上的照样记); 形状不对 (包括没带) 的空着."""
        if req.dry_run and req.token is None:
            return None
        if protocol.token_shape_ok(req.token):
            case.token_id = req.token["id"].lower()               # type: ignore[index]    (令牌号记和比之前一律转成小写)
            case.requested_by = req.token["requested_by"]         # type: ignore[index]
        code = check_token(req.token, action=action, params_json=req.params_json, now=self.now(), used_ids=self._used_ids())
        if code:
            return fail(req.id, code, TOKEN_MESSAGES[code])
        if not req.dry_run:
            self._burn(req.token)                                 # type: ignore[arg-type]
        return None

    # ---- 4.1 的十步 ----

    def _dispatch(self, req: Request, case: _Case) -> dict:
        if req.action == NET_RESTORE:
            return self._restore(req, case)
        # 1. (自己提权了没有、拿锁: 在 handle 里做, 到这里已经过了; 真请求拿着锁, 演练没拿)
        # 2. 令牌四查 (演练可以不带, 带了照查, 不作废)
        denied = self._token_step(req, case, NET_START)
        if denied is not None:
            return denied
        # 3. 参数白名单
        params, fields = protocol.parse_start_params(req.params_json)
        if params is None:
            return protocol.params_failure(req.id, protocol.start_param_problems(req.params_json) or [(f, "") for f in fields])
        case.summary = {"processes": list(params.recipe.processes), "upstream_port": params.recipe.upstream.port,
                        "max_minutes": params.recipe.max_minutes, "core": f"{params.core_id} {params.core_version}"}
        # 4. 配置由代办自己按模板 1 生成, 指纹和请求里的比
        config = config_gen.build(params.recipe)
        if config_gen.fingerprint(config) != params.config_sha256:
            return fail(req.id, E_CONFIG_MISMATCH, "代办按模板 1 生成的配置和请求里的指纹不一样（两边的模板不一致）")
        config_bytes = config_gen.render(config).encode("utf-8")
        # 5. 核心: 名单里有没有 / 文件在不在 / 哈希
        expected = self.pinned.get((params.core_id, params.core_version))
        if expected is None:
            return fail(req.id, E_CORE_UNKNOWN, f"核心 {params.core_id} {params.core_version} 不在代办的名单里")
        if self.core_source is None or not self.core_source.is_file():
            return fail(req.id, E_CORE_MISSING, "核心文件不在")
        run_dir: Path | None = None
        try:
            if req.dry_run:
                if sha256_file(self.core_source) != expected:           # 就地、只读地算: 不拷文件
                    return fail(req.id, E_CORE_HASH, "核心文件的哈希和代办的名单不一致")
            else:
                run_dir = self._new_run_dir()
                case.run_dir = run_dir
                copied = run_dir / "core" / self.core_source.name
                copied.parent.mkdir()
                shutil.copyfile(self.core_source, copied)               # 先拷进受保护的目录, 再对拷进去的那份算
                if sha256_file(copied) != expected:
                    return self._decline(run_dir, fail(req.id, E_CORE_HASH, "拷进受保护目录以后核心文件的哈希和代办的名单不一致，已删掉"))
            # 6. 已经有一段会话, 或我们的网卡已经在
            if self._running():
                return self._decline(run_dir, fail(req.id, E_ALREADY_RUNNING, "已经有一段会话在跑（或 WM-Ayane 网卡已经在了）"))
            if req.dry_run:
                # 演练应答里的 summary 照请求里参数的形状写 (契约第 6 节末尾的例子; 进程名是去重以后的); 审计里那份摘要 (case.summary) 另说
                return ok(req.id, {"dry_run": True, "would": list(WOULD_START), "summary": {
                    "processes": list(unique_processes(params.recipe.processes)),
                    "upstream": {"host": params.recipe.upstream.host, "port": params.recipe.upstream.port},
                    "max_minutes": params.recipe.max_minutes,
                    "core": {"id": params.core_id, "version": params.core_version},
                    "heartbeat": {"interval_s": params.interval_s, "grace_s": params.grace_s},
                    "config_sha256": params.config_sha256}})
            self._clear_stale_runs(keep=run_dir)           # 第 6 步过了 (确定没有会话在跑): runs/ 下旧的子目录都不要了 (演练什么都不删)
            # 7. 确认窗 (默认取消; 点取消 -> E_USER_DECLINED, 60 秒没点 -> E_CONFIRM_TIMEOUT)
            window = {"name": params.recipe.name, "processes": list(unique_processes(params.recipe.processes)),
                      "upstream": f"{params.recipe.upstream.host}:{params.recipe.upstream.port}", "max_minutes": params.recipe.max_minutes,
                      "core": f"{params.core_id} {params.core_version}", "requested_by": case.requested_by,
                      "driver": "第一次用时，核心会给系统装一个网络驱动（Wintun）；关掉以后驱动留在系统里。"}
            self.confirm_calls.append(window)
            verdict = self.confirm(window)
            if verdict == TIMEOUT:
                return self._decline(run_dir, fail(req.id, E_CONFIRM_TIMEOUT, "确认窗 60 秒没人点，按取消算"))
            if verdict != CONFIRM:
                return self._decline(run_dir, fail(req.id, E_USER_DECLINED, "在确认窗里点了取消"))
            return self._launch(req, case, params, config_bytes, run_dir)
        except BaseException:        # 5-10 步里出了意外 (比如拷文件失败、确认窗的回调抛了异常): 这次拷进去的东西不留
            if run_dir is not None:
                shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @staticmethod
    def _decline(run_dir: Path | None, reply: dict) -> dict:
        """第 6、7 步不过: 把这次拷进去的核心整个删掉 (不留半截)."""
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
        return reply

    # ---- 8-10: 起核心、等网卡、交接 ----

    def _core_env(self, tmp: Path) -> dict[str, str]:
        """核心的环境变量由代办自己造 (契约 4.1 第 8 步): 只有 SystemRoot、windir、只含系统目录的 PATH、指到受保护目录的 TEMP 和 TMP.
        系统目录用系统调用问 (GetSystemWindowsDirectoryW), 不读调用方的环境."""
        root = win32.windows_directory()
        return {"SystemRoot": root, "windir": root, "PATH": f"{root}\\System32;{root};{root}\\System32\\Wbem", "TEMP": str(tmp), "TMP": str(tmp)}

    def _guardian_env(self) -> dict[str, str]:
        """看护方进程的环境变量: 照测试进程的, 只把家目录 (USERPROFILE、HOME) 指到 data/fake-home. 起看护方的命令行一定带 --audit, 它不会去算默认的审计
        位置 (~/.worldmodel/serve/net/guardian-audit.jsonl); 万一哪天漏了, 默认位置也落在 fake-home 里, 绝不会写进真的家目录 (测试里绝对不许往真的家目录写)."""
        env = dict(os.environ)
        env["USERPROFILE"] = env["HOME"] = str(self.fake_home)
        return env

    def _launch(self, req: Request, case: _Case, params, config_bytes: bytes, run_dir: Path) -> dict:
        work, tmp = run_dir / "work", run_dir / "tmp"
        work.mkdir()
        tmp.mkdir()
        config_path = work / "config.yaml"
        config_path.write_bytes(config_bytes)                         # 写进文件的就是算指纹的那串字节
        if self.behavior is not None:
            (work / "fake-behavior.json").write_text(json.dumps(self.behavior), encoding="utf-8")
        core_copy = run_dir / "core" / self.core_source.name
        job = win32.create_job()
        proc: subprocess.Popen | None = None
        try:
            proc = self.ledger.add(subprocess.Popen(
                [sys.executable, str(core_copy), "-d", str(work), "-f", str(config_path)], env=self._core_env(tmp), cwd=str(work),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW))
            self.started_processes += 1
            win32.assign_process(job, int(proc._handle))             # type: ignore[attr-defined]
            self._write_session_record(run_dir, proc)                # 单独起的 net.restore 靠它认核心 (假核心是 python.exe 跑的脚本, 按程序文件认不了)
            # 9. 等网卡出现 (最多 start_timeout_s): 没出现或核心先退了 -> 收拾干净, E_START_FAILED
            marker = work / ADAPTER_MARKER
            deadline = time.monotonic() + self.start_timeout_s
            why = ""
            while not why:
                if proc.poll() is not None:
                    why = f"核心先退出了（退出码 {proc.returncode}）"
                elif marker.exists():
                    break
                elif time.monotonic() >= deadline:
                    why = f"网卡 {config_gen.DEVICE} 在 {self.start_timeout_s:g} 秒内没出现"
                else:
                    time.sleep(0.02)
            if why:
                return self._start_failed(req, proc, job, run_dir, why)
            # 10. 把看护交出去
            return self._hand_over(req, case, params, proc, job, run_dir, work, marker)
        except BaseException:
            win32.terminate_job(job)
            if proc is not None:
                self._reap(proc)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        finally:
            if self.handover_fault == "keep_job_handle":             # 造"残留": 交接以后故意不关自己手里的作业对象句柄 (close() 里才关)
                self._leaked_jobs.append(job)
            else:
                win32.close_handle(job)

    @staticmethod
    def _write_session_record(run_dir: Path, proc: subprocess.Popen) -> None:
        """runs/<编号>/session.json: 核心的进程号和创建时间. 恢复时按进程号打开, 创建时间对得上才算是我们起的那个核心 (进程号会被重用)."""
        record = {"core_pid": proc.pid, "core_created": win32.process_creation_time(int(proc._handle))}      # type: ignore[attr-defined]
        (run_dir / SESSION_RECORD).write_text(json.dumps(record), encoding="utf-8")

    def _reap(self, proc: subprocess.Popen) -> str:
        """结束核心 (作业对象已经结束过了就只是等), 把它还没读走的输出读完, 返回打过码的最后几行."""
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        tail: list[str] = []
        try:
            raw = proc.stdout.read() if proc.stdout is not None else b""
            tail = [protocol.mask_output_line(text) for text in raw.decode("utf-8", errors="replace").splitlines() if text.strip()][-5:]
        except (OSError, ValueError):
            pass
        for stream in (proc.stdout,):
            if stream is not None:
                stream.close()
        return " | ".join(tail)

    def _start_failed(self, req: Request, proc: subprocess.Popen, job: int, run_dir: Path, why: str) -> dict:
        win32.terminate_job(job)
        tail = self._reap(proc)
        shutil.rmtree(run_dir, ignore_errors=True)                    # 这次写进去的配置和拷进去的核心都清掉
        return fail(req.id, E_START_FAILED, f"核心没起来：{why}。最后几行输出（已打码）：{tail or '（没有输出）'}")

    def _hand_over(self, req: Request, case: _Case, params, proc: subprocess.Popen, job: int, run_dir: Path, work: Path, marker: Path) -> dict:
        """交接 (契约第 10 节"假的交接办法"): 起看护方进程, 让它继承作业对象 (只带结束和查询)、核心进程、核心输出读端这三个句柄, 确认它的管道连得上以后,
        自己关掉手里的这三个, 再回应答."""
        import msvcrt      # 只有 Windows 上才有; 放在这里导入, 别的系统上这个模块也能被导入 (测试靠 skipif 跳过)
        session_id = secrets.token_hex(16)
        started_at = int(time.time())
        deadline_at = started_at + (int(self.deadline_s) if self.deadline_s is not None else params.recipe.max_minutes * 60)
        if self.handover_fault == "core_dead":
            proc.kill()
            proc.wait(10)
        job_dup = win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS, inheritable=True)
        proc_dup = win32.duplicate_handle(int(proc._handle), access=win32.PROCESS_LIMITED_ACCESS, inheritable=True)   # type: ignore[attr-defined]
        out_dup = win32.duplicate_handle(msvcrt.get_osfhandle(proc.stdout.fileno()), inheritable=True)
        if self.handover_fault == "job_is_a_process":
            win32.close_handle(job_dup)
            job_dup = win32.duplicate_handle(int(proc._handle), access=win32.PROCESS_LIMITED_ACCESS, inheritable=True)   # type: ignore[attr-defined]
        elif self.handover_fault == "process_not_in_job":
            decoy = self.ledger.add(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW))
            win32.close_handle(proc_dup)
            proc_dup = win32.duplicate_handle(int(decoy._handle), access=win32.PROCESS_LIMITED_ACCESS, inheritable=True)   # type: ignore[attr-defined]
        elif self.handover_fault == "output_is_not_a_pipe":
            with open(self.data_dir / "not-a-pipe.txt", "wb") as plain:
                win32.close_handle(out_dup)
                out_dup = win32.duplicate_handle(msvcrt.get_osfhandle(plain.fileno()), inheritable=True)
        argv = [sys.executable, "-m", self.guardian_module, "--session", session_id, "--job", str(job_dup), "--process", str(proc_dup),
                "--output", str(out_dup), "--deadline", str(deadline_at), "--started", str(started_at), "--interval", str(params.interval_s),
                "--grace", str(params.grace_s), "--processes", json.dumps(list(unique_processes(params.recipe.processes)), ensure_ascii=False),
                "--audit", str(self.guardian_audit), "--fake-adapter-marker", str(marker), "--fake-flush-log", str(self.flush_log)]
        guardian: subprocess.Popen | None = None
        try:
            with open(self.guardian_stderr, "ab") as errlog:
                guardian = self.ledger.add(win32.spawn_inheriting(
                    argv, [job_dup, proc_dup, out_dup], cwd=str(ROOT), env=self._guardian_env(), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=errlog, creationflags=NO_WINDOW))
            self.started_processes += 1
        finally:
            for handle in (job_dup, proc_dup, out_dup):             # 子进程已经继承了 (或根本没起来): 这几份复制件不再需要
                win32.close_handle(handle)
        pipe = protocol.pipe_name(session_id)
        self.pipes.append(pipe)
        connected = False
        deadline = time.monotonic() + self.hand_over_timeout_s
        while not connected and guardian.poll() is None and time.monotonic() < deadline:
            try:
                answer = json.loads(win32.pipe_request(pipe, protocol.encode({"v": protocol.VERSION, "id": "hand-over", "action": "heartbeat"}), 1.0))
                connected = answer.get("ok") is True
            except (TimeoutError, OSError, ValueError):
                time.sleep(0.05)
        if not connected:
            win32.terminate_job(job)
            guardian.kill()
            self._reap(proc)
            shutil.rmtree(run_dir, ignore_errors=True)
            return fail(req.id, E_INTERNAL, "看护方没起来或它的管道连不上，已经把核心收拾掉了")
        # 看护方接住了: 关掉自己手里的这三个句柄 (作业对象在外面的 finally 里关)
        proc.stdout.close()
        self.session = Session(session_id, pipe, guardian, proc, run_dir, work, started_at, deadline_at, proc.pid, guardian.pid)
        self.last_run_dir = run_dir
        return ok(req.id, {"session": session_id, "state": "running", "started_at": started_at, "deadline_at": deadline_at,
                           "core": {"id": params.core_id, "version": params.core_version, "pid": proc.pid}, "adapter": config_gen.DEVICE,
                           "config_sha256": params.config_sha256})

    # ---- 单独起的 net.restore (契约 4.4 末尾〔补 2〕的 7 步) ----

    def _restore(self, req: Request, case: _Case) -> dict:
        """第 1、2 步 (提权检查、拿锁) 在 handle 里做, 这里从第 3 步起: 令牌四查 (action 是 net.restore, params_json 必须带、内容是 {}; 过了就作废) -> 参数 (空对象,
        有任何键 E_PARAMS) -> [确认窗: 开关, 默认不弹] -> 三步 (kill_core、adapter_gone、flush_dns, 逐步回报) -> 清 runs/ (三步都没有 failed 才清; 有 failed 就什么都不清,
        下次恢复还认得出核心、还看得到标记) -> 审计 (handle 里记) -> 应答.
        演练只做只读的核对 (令牌带了照查不作废、参数), 回 would, 什么都不动. 应答的形状和会话内的 net.restore 一样: steps、clean、reboot_advised.
        审计摘要 (三步做完以后才有, 是代办自己算出来的值): clean 和每一步的 result."""
        denied = self._token_step(req, case, NET_RESTORE)       # 真请求没带 params_json 时, 令牌这一步就对不上了 (E_TOKEN_MISMATCH)
        if denied is not None:
            return denied
        if not isinstance(req.params_json, str):                  # 演练可以不带令牌, 但 params_json 还是要有 (内容是 {})
            return protocol.params_failure(req.id, [("params_json", "必须带，内容是 {}（这个动作没有参数）")])
        found = protocol.parse_empty_params(req.params_json)
        if found:
            return protocol.params_failure(req.id, found)
        if req.dry_run:
            return ok(req.id, {"dry_run": True, "would": list(WOULD_RESTORE)})
        if self.confirm_restore:                                  # 开关 (默认关): 契约现在定的是不弹; 开着时在三步之前问一次
            window = {"action": NET_RESTORE, "requested_by": case.requested_by, "does": list(WOULD_RESTORE),
                      "text": "结束我们的核心、等 WM-Ayane 网卡消失、清 DNS 缓存；不动别的软件的网卡和设置。"}
            self.confirm_calls.append(window)
            verdict = self.confirm(window)
            if verdict == TIMEOUT:
                return fail(req.id, E_CONFIRM_TIMEOUT, "确认窗 60 秒没人点，按取消算")
            if verdict != CONFIRM:
                return fail(req.id, E_USER_DECLINED, "在确认窗里点了取消")
        kill = self._restore_kill_core()
        steps = [kill, self._restore_adapter_gone(kill["result"]), self._restore_flush_dns()]
        clean = all(step["result"] != FAILED for step in steps)
        case.summary = {"clean": clean, **{step["step"]: step["result"] for step in steps}}
        if clean:                                                 # 三步都没有 failed 才清 runs/ (尽力而为); 有 failed 就什么都不清, 记录、标记都留着:
            self._clear_stale_runs(keep=None)                     # 结束不了的核心下次还认得出来、还能再试; 网卡赖着不走的, 每次都是 failed、每次都建议重启, 直到它真的没了.
        return ok(req.id, {"steps": steps, "clean": clean, "reboot_advised": not clean})   # (不能把下次认核心的记录和"网卡在不在"的凭据自己清掉: 真的网卡删目录是删不掉的)

    def _restore_kill_core(self) -> dict:
        """结束受保护目录里还在跑的核心. 真代办按"进程的程序文件在不在受保护目录里"来认; 假核心是 python.exe 跑的脚本, 认不了, 假助手用自己的办法: 真起的时候在
        runs/<编号>/session.json 里记了核心的进程号和创建时间, 这里逐个读, 按进程号打开, 创建时间对得上才算是它 (进程号会被重用), 活着就结束、等它没了.
        一个活着的都没有 -> skipped; 结束不了 -> failed; session.json 读不了或内容不对 -> 跳过那一个, detail 里说一句, 不让整个恢复失败."""
        killed = failed = 0
        notes: list[str] = []
        for record in sorted(self.runs_dir.glob(f"*/{SESSION_RECORD}")):
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
                pid, created = data["core_pid"], data["core_created"]
                if type(pid) is not int or type(created) is not int or pid <= 0:
                    raise ValueError("字段不对")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                notes.append(f"{record.parent.name}/{SESSION_RECORD} 读不了或内容不对（{type(exc).__name__}），跳过")
                continue
            outcome, note = self._kill_recorded_core(pid, created)
            killed += outcome == "killed"
            failed += outcome == "failed"
            if note:
                notes.append(note)
        return {"step": STEP_KILL, "result": FAILED if failed else (DONE if killed else SKIPPED), "detail": "；".join(notes)}

    def _kill_recorded_core(self, pid: int, created: int) -> tuple[str, str]:
        """session.json 里记的那一个核心: ("killed" 结束了 | "gone" 已经没了 | "reused" 进程号被别的进程重用了, 没动 | "failed" 结束不了, 要写进 detail 的说明).
        创建时间对不上、或者根本打不开 (我们起的核心, 同一个用户一定打得开) 的, 都不是我们的核心, 绝不动它."""
        try:
            handle = win32.open_process(pid, access=win32.PROCESS_KILL_ACCESS)
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror == 87:                                      # ERROR_INVALID_PARAMETER: 没有这个进程号的进程了
                return "gone", ""
            return "reused", f"进程号 {pid} 打不开（winerror {winerror}），不是我们的核心，没动"
        try:
            if win32.process_creation_time(handle) != created:
                return "reused", f"进程号 {pid} 的创建时间对不上（进程号被别的进程重用了），没动"
            if win32.process_exit_code(handle) is not None:
                return "gone", ""                                   # 就是它, 但已经退了
            if not win32.terminate_process(handle) and win32.process_exit_code(handle) is None:
                return "failed", f"结束不了进程 {pid}"
            if not win32.wait_process(handle, self.restore_kill_wait_s):
                return "failed", f"结束了进程 {pid}，但 {self.restore_kill_wait_s:g} 秒内它没退出"
            return "killed", ""
        finally:
            win32.close_handle(handle)

    def _restore_adapter_gone(self, kill_result: str) -> dict:
        """等 runs/*/work/ 下的"网卡"标记都没了, 最多 restore_adapter_timeout_s 秒. 本来就没有、而且上一步没什么可结束的 (skipped) -> skipped (和看护方的 net.restore
        一个规矩); 到时间还在 -> failed."""
        def markers() -> list[Path]:
            return list(self.runs_dir.glob(f"*/work/{ADAPTER_MARKER}"))

        if not markers() and kill_result == SKIPPED:
            return {"step": STEP_ADAPTER, "result": SKIPPED, "detail": ""}
        deadline = time.monotonic() + self.restore_adapter_timeout_s
        while markers():
            if time.monotonic() >= deadline:
                return {"step": STEP_ADAPTER, "result": FAILED, "detail": f"{self.restore_adapter_timeout_s:g} 秒后网卡 {config_gen.DEVICE} 还在"}
            time.sleep(0.02)
        return {"step": STEP_ADAPTER, "result": DONE, "detail": ""}

    def _restore_flush_dns(self) -> dict:
        """清 DNS 缓存: 假的. 往 data_dir 里的记录文件 (agent-flush.log, 和看护方那份 flush.log 分开) 追加一行, 绝不调真的 (win32.flush_dns / DnsFlushResolverCache).
        什么都没开过的时候也做 (和契约 4.4 一样: 三种状态下都清)."""
        try:
            with open(self.agent_flush_log, "a", encoding="utf-8", newline="\n") as handle:
                handle.write("flush\n")
        except OSError as exc:
            return {"step": STEP_DNS, "result": FAILED, "detail": f"记录文件写不进去（{type(exc).__name__}）"}
        return {"step": STEP_DNS, "result": DONE, "detail": ""}

    # ---- 受保护目录里的东西 ----

    def _new_run_dir(self) -> Path:
        run_dir = self.runs_dir / secrets.token_hex(4)
        run_dir.mkdir(parents=True)
        return run_dir

    def _running(self) -> bool:
        """已经有一段会话 (自己起的看护方还活着), 或 WM-Ayane 网卡已经在 (runs/*/work/ 下有标记文件: 假核心用"关句柄就删"的方式建的, 核心一没它就没了,
        所以不用读它、不用查进程号)."""
        if self.session is not None and self.session.guardian.poll() is None:
            return True
        return any(True for _ in self.runs_dir.glob(f"*/work/{ADAPTER_MARKER}"))

    def _clear_stale_runs(self, keep: Path | None) -> None:
        """runs/ 下除了 keep 以外的子目录都删掉 (上几次会话留下的; 调用的时候已经确定没有会话在跑)."""
        if not self.runs_dir.is_dir():
            return
        for old in self.runs_dir.iterdir():
            if old.is_dir() and old != keep:
                shutil.rmtree(old, ignore_errors=True)

    def _used_ids(self) -> set[str]:
        """用过的令牌号 (没过期的; 过期的不算了: 令牌本身也过期, 过期那一查先拦住)."""
        try:
            stored = json.loads(self.used_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        now = self.now()
        return {key for key, expires in stored.items() if expires >= now}

    def _burn(self, token: dict) -> None:
        try:
            stored = json.loads(self.used_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        now = self.now()
        stored = {key: expires for key, expires in stored.items() if expires >= now}
        stored[token["id"].lower()] = token["expires_at"]
        self.used_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.used_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(stored), encoding="utf-8")
        tmp.replace(self.used_path)

    def _record(self, case: _Case, req: Request, reply: dict) -> None:
        """提权的代办每条请求都记一行, 除非它确定是演练 (契约第 8 节〔补 3〕): 解得出来的真请求 (成功的、被拒的都记) 在这里记; 演练不记; 没提权的什么都不写."""
        if self.elevated and not req.dry_run:
            self._audit(case, req.action, reply)

    def _record_rejected(self, rejection: Rejection) -> None:
        """没过第 1 节检查的请求 (解不出来、版本不对、动作不认识): 提权的代办也记一行, 动作写 rejected, 令牌号、谁申请的、参数指纹和摘要空着 (没核对过的值不往账上写),
        结果写错误码. 没提权的什么都不写."""
        if self.elevated:
            self._append_audit({"at": int(self.now()), "action": "rejected", "token_id": None, "requested_by": None, "params_sha256": None,
                                "summary": None, "result": rejection.code})

    def _audit(self, case: _Case, action: str, reply: dict) -> None:
        """每条解得出来的真请求写一行 (契约第 8 节提权一侧): 时间、动作、令牌号、谁申请的、参数指纹和摘要、结果或错误码 (没有 dry_run 字段: 它永远是假的).
        各项只在通过了自己那一步的检查以后才写, 没过的空着 (null): 令牌号和谁申请的 = 令牌的形状通过以后 (形状过了、后面查出过期、用过、对不上的照样写; 形状不对包括
        没带的, 以及拿不到锁回 E_BUSY 的那条, 空着); 参数摘要 = 参数白名单通过以后; 参数指纹 = 代办自己算的, 请求里有 params_json 就写. 不记核心输出."""
        self._append_audit({"at": int(self.now()), "action": action, "token_id": case.token_id, "requested_by": case.requested_by,
                            "params_sha256": case.params_sha256, "summary": case.summary,
                            "result": "ok" if reply.get("ok") else reply["error"]["code"]})

    def _append_audit(self, record: dict) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_lock, open(self.audit_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def audit_lines(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(text) for text in self.audit_path.read_text(encoding="utf-8").splitlines() if text.strip()]


class Rig:
    """一套测试用的东西: base 下面有 protected/ (假的受保护目录)、data/ (普通权限这边)、download/ (假核心脚本的拷贝), 加上假助手和进程账本.
    helper_kwargs 原样交给 FakeHelper. close() 把起过的进程都结束掉; 测试收尾时调, 然后 assert not rig.ledger.alive()."""

    def __init__(self, base: Path, **helper_kwargs: object) -> None:
        require_an_unelevated_test_process()
        self.base = Path(base)
        self.protected = self.base / "protected"
        self.data = self.base / "data"
        self.download = self.base / "download"
        for folder in (self.protected, self.data, self.download):
            folder.mkdir(parents=True, exist_ok=True)
        self.core_source = self.download / "mihomo-fake.py"
        shutil.copyfile(FAKE_CORE_SCRIPT, self.core_source)
        self.core_sha = sha256_file(self.core_source)
        self.clock = FakeClock()
        self.ledger = ProcessLedger()
        kwargs: dict = {"protected_dir": self.protected, "data_dir": self.data, "core_source": self.core_source,
                        "pinned": {(CORE_ID, CORE_VERSION): self.core_sha}, "now": self.clock, "ledger": self.ledger}
        kwargs.update(helper_kwargs)
        self.helper = FakeHelper(**kwargs)

    def send(self, built: Built | bytes) -> dict:
        """发一条请求给假助手, 返回解析好的应答."""
        return json.loads(self.helper.handle(built.line if isinstance(built, Built) else built))

    def leftover_pipes(self) -> list[str]:
        """这套东西起过的会话管道里, 现在还在的 (收尾以后应当一个也没有; 只看自己起的, 别的会话的 wm-net- 管道不管)."""
        return [pipe for pipe in self.helper.pipes if win32.pipe_exists(pipe)]

    def close(self) -> None:
        self.helper.close()
