"""起 claude / codex 的无界面会话. 固定的命令模板 (只有几个有类型的空位), 不经过 shell, 提示文字走标准输入.

- 对方锁成只读, 而且不许带 MCP 工具 (项目根的 .mcp.json 配着 wm-pet, 对方若能调 pet_chat, 它的话会被当成主人说的、进长记忆):
  Claude 只开 Read / Grep / Glob、plan 模式、--strict-mcp-config 且不给 --mcp-config、只读用户级设置 (不载项目里的钩子);
  Codex 两种情形 (exec / exec resume) 都用 --ignore-user-config 和 -c 钉死沙箱只读、从不问 (resume 不接受 -s / -C, 所以靠 -c 和进程的工作目录).
- 找程序: 这台电脑上两个都是 npm 装的 .cmd 包装 (里面是 node 脚本). .cmd 要经过 cmd.exe, 参数里有 & 或 % 就是注入口, 所以不走它:
  取包装同目录的 node.exe (没有就用 PATH 里的 node) 加已知的入口脚本; 找不到入口就是"没装".
- 环境白名单 (不带任何 key); 进 Job 对象 (8790 退出时对方跟着死); 超时杀整棵; 输出限 4 MB.
- 提示一旦交到对方进程的标准输入, 之后任何失败都不自动重发 (调用的人记成 uncertain / timeout).
- run() 要凭条 (limits.Ticket): 没有凭条没有别的路到起进程这一步.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import AgentMsgError
from .limits import Ticket

STDOUT_MAX = 4 * 1024 * 1024
STDERR_MAX = 64 * 1024
SESSION_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
JOB_MEMORY = 3 << 30

ENV_KEEP = {"PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
            "PATHEXT", "COMSPEC", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMDRIVE", "USERNAME", "COMPUTERNAME", "OS",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "CODEX_HOME", "CLAUDE_CONFIG_DIR"}

# 包装所在目录下的入口脚本 (npm 的布局)
SHIMS = {"claude": ("claude", ("node_modules", "@anthropic-ai", "claude-code", "cli.js")),
         "codex": ("codex", ("node_modules", "@openai", "codex", "bin", "codex.js"))}


def build_args(target: str, resume: str = "", max_usd: float = 0.07) -> list[str]:
    """命令模板 (不含程序). resume 是对方的会话号 (正则核过才进参数)."""
    if resume and not SESSION_ID.match(resume):
        raise AgentMsgError("bad_args", "对方的会话号格式不对, 不追问")
    if target == "claude":
        args = ["-p", "--output-format", "json", "--permission-mode", "plan", "--tools", "Read,Grep,Glob",
                "--strict-mcp-config", "--setting-sources", "user", "--max-budget-usd", f"{max_usd:.4f}"]
        return args + (["-r", resume] if resume else [])
    if target == "codex":
        head = ["exec", "resume", resume] if resume else ["exec"]
        return head + ["--json", "--skip-git-repo-check", "--ignore-user-config",
                       "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"', "-"]
    raise AgentMsgError("bad_args", "只能找 claude 或 codex")


def find_program(target: str, targets_conf: dict | None = None, launch: dict | None = None) -> list[str]:
    """程序的开头几项 (程序 + 入口脚本). 找不到抛 AgentMsgError("not_found"). launch 是测试换进来的 (正式运行为空)."""
    if launch and target in launch:
        return [str(x) for x in launch[target]]
    conf = (targets_conf or {}).get(target)
    if isinstance(conf, dict) and isinstance(conf.get("argv0"), list) and conf["argv0"] and all(isinstance(x, str) for x in conf["argv0"]):
        return list(conf["argv0"])
    if target not in SHIMS:
        raise AgentMsgError("bad_args", "只能找 claude 或 codex")
    name, entry = SHIMS[target]
    found = shutil.which(name)
    if not found:
        raise AgentMsgError("not_found", f"这台电脑上找不到 {name} 命令, 没法找它")
    path = Path(found)
    if path.suffix.lower() == ".exe":
        return [str(path)]
    script = path.parent.joinpath(*entry)
    node = path.parent / "node.exe"
    node_path = str(node) if node.is_file() else shutil.which("node")
    if not script.is_file() or not node_path:
        raise AgentMsgError("not_found", f"找到了 {name} 的包装, 但找不到它的入口脚本或 node, 不去经过 cmd 硬跑")
    return [node_path, str(script)]


def make_env(source=None, extra: dict | None = None) -> dict:
    """白名单环境: 只带上面列的 (大小写不分), 不带任何 key. extra 只给测试用."""
    source = os.environ if source is None else source
    env = {k: v for k, v in source.items() if k.upper() in ENV_KEEP}
    env.update(PYTHONIOENCODING="utf-8", NO_COLOR="1")
    if extra:
        env.update(extra)
    return env


@dataclass
class Result:
    state: str = "uncertain"      # replied / failed / timeout / uncertain
    text: str = ""
    session_id: str = ""
    cny: float | None = None      # None: 没拿到, 调用的人按预占全额算
    tokens: dict = field(default_factory=dict)
    error: str = ""
    delivered: bool = False       # 提示是不是已经交到对方进程手里了 (交了就不重发)


# ---- 解析 ----
def _last_json_object(text: str):
    text = text.strip()
    with contextlib.suppress(ValueError):
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    for line in reversed(text.splitlines()):
        with contextlib.suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict):
                return value
    return None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def parse_claude(stdout: str, usd_cny: float) -> Result:
    obj = _last_json_object(stdout)
    if obj is None:
        return Result(state="uncertain", error="对方的输出不是 JSON")
    usd = _num(obj.get("total_cost_usd"))
    res = Result(cny=None if usd is None else round(usd * usd_cny, 6), session_id=str(obj.get("session_id") or ""))
    turns = obj.get("num_turns")
    if isinstance(turns, int):
        res.tokens["turns"] = turns
    text = obj.get("result")
    if obj.get("is_error") is True or str(obj.get("subtype") or "").startswith("error"):
        res.state, res.error = "failed", str(text or obj.get("subtype") or "对方报了错")[:1000]
        res.text = text if isinstance(text, str) else ""
    elif isinstance(text, str):
        res.state, res.text = "replied", text
    else:
        res.state, res.error = "uncertain", "对方的输出里没有 result"
    return res


def parse_codex(stdout: str, price: dict | None, flat_cny: float) -> Result:
    res, last, failed, saw = Result(), "", "", False
    usage = None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        saw = True
        kind = ev.get("type")
        if kind == "thread.started" and isinstance(ev.get("thread_id"), str):
            res.session_id = ev["thread_id"]
        elif kind == "item.completed" and isinstance(ev.get("item"), dict) and ev["item"].get("type") == "agent_message":
            if isinstance(ev["item"].get("text"), str):
                last = ev["item"]["text"]
        elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        elif kind in ("turn.failed", "error"):
            err = ev.get("error")
            failed = str(err.get("message") if isinstance(err, dict) else ev.get("message") or err or "对方报了错")[:1000]
    if usage:
        tin, cached, tout = (_num(usage.get(k)) or 0.0 for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
        res.tokens = {"input": int(tin), "cached": int(cached), "output": int(tout)}
        if price:
            res.cny = round(((tin - cached) * price["input"] + cached * price["cached"] + tout * price["output"]) / 1e6, 6)
    if res.cny is None and last:
        res.cny = flat_cny
    if failed:
        res.state, res.error, res.text = "failed", failed, last
    elif last:
        res.state, res.text = "replied", last
    else:
        res.state, res.error = "uncertain", "对方的输出里没有回话" if saw else "对方的输出不是 JSONL"
    return res


# ---- 起进程 ----
def _job(process) -> None:
    """放进 Job 对象 (KILL_ON_JOB_CLOSE: 8790 退出时对方跟着死). 复用 material_readers 那份 (只读不改); 建不起来就算了, 靠超时杀."""
    if os.name != "nt":
        return None
    try:
        from wm.agent.material_readers import _windows_job
        return _windows_job(process, memory=JOB_MEMORY)
    except Exception:  # noqa: BLE001
        return None


def kill_tree(process) -> None:
    try:
        from wm.services import _kill_tree
        _kill_tree(process.pid, timeout=3.0)
    except Exception:  # noqa: BLE001
        pass
    with contextlib.suppress(Exception):
        process.kill()


def _pump(stream, cap: int, sink: list, over: threading.Event, process) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            total += len(chunk)
            if total > cap:
                over.set()
                kill_tree(process)
                return
            sink.append(chunk)
    except (OSError, ValueError):
        return


def run(ticket: Ticket, *, target: str, prompt: str, workspace: str, argv0: list[str], resume: str = "", timeout_s: float = 300.0,
        max_usd: float = 0.07, usd_cny: float = 7.2, codex_price: dict | None = None, codex_flat_cny: float = 0.3,
        env: dict | None = None) -> Result:
    if not isinstance(ticket, Ticket) or ticket.target != target:
        raise TypeError("没有凭条, 不起进程")
    argv = [*argv0, *build_args(target, resume, max_usd)]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=workspace,
                                   env=make_env() if env is None else env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return Result(state="failed", cny=0.0, error=f"起不来: {str(exc)[:300]}")
    close_job = _job(process)
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    over = threading.Event()
    threads = [threading.Thread(target=_pump, args=(process.stdout, STDOUT_MAX, out_chunks, over, process), daemon=True),
               threading.Thread(target=_pump, args=(process.stderr, STDERR_MAX, err_chunks, threading.Event(), process), daemon=True)]
    for t in threads:
        t.start()
    delivered = False
    try:
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
            delivered = True
        except (OSError, ValueError):
            delivered = False
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kill_tree(process)
            with contextlib.suppress(Exception):
                process.wait(timeout=5)
            return Result(state="timeout", delivered=delivered, error=f"超过 {timeout_s:g} 秒没回话, 已经停掉")
        for t in threads:
            t.join(timeout=5)
        stdout = b"".join(out_chunks).decode("utf-8", errors="replace")
        if over.is_set():
            return Result(state="uncertain", delivered=delivered, error="对方的输出太大 (超过 4 MB), 已经停掉")
        if target == "claude":
            res = parse_claude(stdout, usd_cny)
        else:
            res = parse_codex(stdout, codex_price, codex_flat_cny)
        res.delivered = delivered
        if res.state == "uncertain" and process.returncode:
            res.error = f"对方退出码 {process.returncode}; {res.error}; " + b"".join(err_chunks).decode("utf-8", errors="replace")[-300:]
        return res
    finally:
        if process.poll() is None:
            kill_tree(process)
        if close_job:
            with contextlib.suppress(Exception):
                close_job()
