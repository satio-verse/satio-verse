"""Agent 互发消息的引擎 (她主动找本机装着的 Claude Code、Codex 问一件事, 拿回对方最后那段结论).
工作台这边的工具层在 wm/agent/agentmsg.py; 计划: backups/tool4-agent-msg/计划.md. 这个包不 import wm.agent.

- limits.py  额度本: 总开关、每天条数 / 钱、来回计数, 查上限 + 预占 + 写盘在一个临界区里一步做完 (硬拦, 不靠她自觉)
- store.py   对话 (thread) 和消息的记录、回话落盘、审计日志 (不记原文)
- runner.py  固定的命令模板、环境白名单、起进程、超时、杀整棵、解析输出
- scan.py    发出去之前的检查 (长度、受保护路径、密钥样子的字符串)
状态都放 <工作台的家>/../serve/agent-msg/ (真的那份是 ~/.worldmodel/serve/agent-msg, serve 整个目录在守门的固定名单里,
她的读写工具和带这个路径的命令碰不到).
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

try:  # Windows 用 msvcrt 锁文件, 别处用 fcntl
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None
    import fcntl

TARGETS = {"claude": "Claude Code", "codex": "Codex"}

# 你定的数字 (等你拍板的事, 工具4): 来回 6 次、每天 20 条、每天 2 元. agent_msg 配置节可以改, 有夹紧.
DEFAULTS = {"max_passes": 6, "daily_messages": 20, "daily_cny": 2.0,
            "per_call_cny": 0.5, "usd_cny": 7.2, "codex_flat_cny": 0.3, "codex_price": None,
            "timeout_s": 300.0, "max_inflight": 2}
CLAMPS = {"max_passes": (1, 20), "daily_messages": (1, 200), "daily_cny": (0.0, 50.0), "per_call_cny": (0.01, 10.0),
          "usd_cny": (1.0, 20.0), "codex_flat_cny": (0.01, 10.0), "timeout_s": (10.0, 1800.0), "max_inflight": (1, 8)}

MESSAGE_MAX = 4000
REPLY_STORE_MAX = 200_000


class AgentMsgError(Exception):
    """code 是拒绝 / 失败的编码 (off / target_off / daily_count / daily_cny / chain / inflight / state / bad_args /
    bad_message / not_found / lock), 给人看的原因在 str(exc)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StateError(Exception):
    """状态文件读不出来 / 坏了 (一律按关着、拒发处理)."""


def home_for(agent_home: Path | str) -> Path:
    """状态目录: 工作台的家旁边的 serve/agent-msg (真的那份是 ~/.worldmodel/serve/agent-msg; 测试换了工作台的家, 这里跟着换)."""
    return Path(agent_home).parent / "serve" / "agent-msg"


def settings(conf: dict | None) -> dict:
    """agent_msg 配置节 -> 带夹紧的设置 (不认识的键忽略, 值不对用默认)."""
    conf = conf if isinstance(conf, dict) else {}
    out = dict(DEFAULTS)
    for key, (lo, hi) in CLAMPS.items():
        value = conf.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out[key] = min(hi, max(lo, value))
    for key in ("max_passes", "daily_messages", "max_inflight"):
        out[key] = int(out[key])
    price = conf.get("codex_price")
    if isinstance(price, dict) and all(isinstance(price.get(k), (int, float)) and not isinstance(price.get(k), bool)
                                       for k in ("input", "cached", "output")):
        out["codex_price"] = {k: float(price[k]) for k in ("input", "cached", "output")}
    else:
        out["codex_price"] = None
    out["targets"] = conf.get("targets") if isinstance(conf.get("targets"), dict) else {}
    return out


# ---- 文件 ----
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.RLock())


class FileLock:
    """进程内一把锁 + 跨进程的文件锁 (同一个目录同一时刻只许一个人改额度). 不要嵌套着用同一个路径."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path, self.timeout, self.f = Path(path), timeout, None

    def __enter__(self):
        lock = _thread_lock(self.path)
        lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.f = open(self.path, "a+b")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if msvcrt is not None:
                        self.f.seek(0)
                        msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover
                        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.monotonic() > deadline:
                        raise AgentMsgError("lock", "额度本被别处占着没放开, 这次不发") from None
                    time.sleep(0.005)
        except BaseException:
            if self.f is not None:
                self.f.close()
                self.f = None
            lock.release()
            raise

    def __exit__(self, *exc):
        try:
            if self.f is not None:
                with contextlib.suppress(OSError):
                    if msvcrt is not None:
                        self.f.seek(0)
                        msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover
                        fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
                self.f.close()
        finally:
            self.f = None
            _thread_lock(self.path).release()


def read_json(path: Path, tries: int = 6):
    """读一个 JSON 文件; 文件不在返回 None. 在却读不出来 (写的那边正在换它: Windows 上会报 PermissionError) 稍等再读,
    还不行抛 StateError (调用的人按关着处理)."""
    last = None
    for i in range(tries):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError) as exc:
            last = exc
            time.sleep(0.01 * (i + 1))
    raise StateError(f"{Path(path).name} 读不出来: {last}")


def write_json(path: Path, obj) -> None:
    """先写旁边的临时文件再换过去 (读的人要么读到整份旧的、要么整份新的)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    last = None
    for i in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # 目标正被别的进程开着读
            last = exc
            time.sleep(0.01 * (i + 1))
    with contextlib.suppress(OSError):
        tmp.unlink()
    raise last
