"""本机服务的看守 (docs/roadmap/own-harness.md 第 9 节 A "桌宠管服务": 只下载一个桌宠就能用的第一步).
桌宠 (wmpet.exe --services) 启动时在后台拉起它; 它按配置决定开哪些服务 —— 服务端 (经 tools/serve.py 的看守)、本机模型
(llama-server)、语音合成 (GPT-SoVITS), 嵌入和复核模型默认不开 (显存紧, 要开在 services 里写 "on").
端口上已经有人在跑的 (你自己在终端开的) 不重复开, 记成"外面开的"; 自己开的挂了按退避重启, 10 分钟里挂太多次就不再拉, 等你点重启.
状态写在 ~/.worldmodel/services/state.json (桌宠托盘、设置面板读它); 重启/停的请求是同一个目录下的 req-*.json.
写状态文件是"写临时文件再换": 别的进程正开着它读的那一下 Windows 上换不了, 稍等再换, 还不行就记一行到 logs/services/supervisor-<日期>.log、
这一遍先不存、下一遍再存, 看守不因此崩; 读状态文件正碰上别的进程在换它也稍等再读 (REPLACE_*、READ_* 几个常量).
服务起不来 (程序不在、日志打不开) 和杀了还结束不掉也不让看守崩: 前者记成 down、按重启次数和退避走 (到 restart_max 次变 failed),
后者把它从看守这边摘掉接着往下走; 都记一行到同一个日志.
请求文件每个只执行一次: 读不出来的先留着下一轮再读 (连着 REQ_READ_S 秒读不出来就删掉不执行)、执行了但删不掉的记住不再执行、内容不对的删掉;
上一次留下的旧请求绝不执行 (run() 开头看到的、文件时间早于看守起来的时刻的). run() 里一圈 step() 出了没接住的错也不退出: 记进同一个日志
(带出错位置)、这一圈作废、下一圈照常, 连着 LOOP_FAIL_MAX 圈都错才退出.
退出桌宠默认不停服务 (QQ 和微信接着收); 托盘里"退出并停掉服务"才停.

  python -m wm.services run                 前台跑看守 (桌宠用 pythonw 在后台跑这个; 已经有一个在跑就直接退出)
  python -m wm.services up                  看守没在跑就在后台起一个, 马上返回
  python -m wm.services status [--json]
  python -m wm.services restart [名字 ...]   重启 (不给名字 = 全部); 名字: server llm embed escalate local_api tts
  python -m wm.services stop [名字 ...]      停掉 (不给名字 = 全部, 看守也退出)
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import localhttp
from .config import ROOT, load_config, resolve_path

HOME = Path.home() / ".worldmodel" / "services"
LOGS = ROOT / "logs" / "services"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0           # CREATE_NO_WINDOW: 子进程有个看不见的控制台, 孙进程 (llama-server) 也用它
DETACH = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
LABEL = {"server": "服务端", "llm": "本机模型", "embed": "记忆检索 (嵌入)", "escalate": "复核模型", "tts": "语音合成",
         "local_api": "本机大模型"}
ORDER = ["llm", "embed", "escalate", "local_api", "tts", "server"]
WINDOW_S = 600  # 这么长时间里重启超过 restart_max 次就不再拉
TAKEOVER_S = 15.0  # 外面开的服务端口空了这么久才接手 (你在终端里重启它、serve.py 换服务端时会空几秒, 别趁机抢端口)
REPLACE_TRIES = 6      # 写状态文件: 别的进程 (宿主、面板、命令行) 正开着它读的那一下 Windows 上 os.replace 会报 PermissionError, 最多换这么多次
REPLACE_WAIT_S = 0.03  # 第 i 次没换成等 i × 这么久再换 (六次都不成共等约 0.45 秒)
READ_TRIES = 5         # 读状态文件: 正碰上别的进程在换它 (打开被拒、读到空的或半截的), 最多读这么多次; 文件本来就不在的不等
READ_WAIT_S = 0.01     # 第 i 次没读成等 i × 这么久再读 (五次都不成共等约 0.1 秒)
SAVE_SAY_S = 60.0      # 存不进状态文件时, 同一阵连着失败最多这么久往日志里记一行
REQ_READ_S = 10.0      # 请求文件连着这么久读不出来 (被别的程序开着、读到半截): 记一行、删掉、不执行; 在这之前每一圈再读一次
REQ_OLD_SLACK_S = 0.2  # 请求文件的时间比看守起来的时刻早过这么多才算"旧请求" (文件时间的刻度比 time.time() 粗约 15 毫秒, 刚写的不能当旧的)
LOOP_FAIL_MAX = 60     # run() 里连着这么多圈 step() 都出错 (出错的一圈作废), 看守才退出
LOOP_SAY_S = 60.0      # run() 里同一个错 (类型加出错位置) 重复时, 最多这么久往日志里记一行 (带次数)


@dataclass
class Spec:
    name: str
    cmd: list[str]
    port: int
    health: str = "tcp"         # "tcp" 端口能连上就算好; 以 / 开头的是 HTTP 路径, 回 200 才算好
    ready_s: float | None = 120.0  # 起来要多久 (超过还没好算没起来); None: 它自己有看守, 起没起来、卡没卡由它判断, 这里不限时
    after: list[str] = field(default_factory=list)  # 这些先好了 (或等了 60 秒) 再起
    stop_cmd: list[str] | None = None  # 体面地停 (服务端: 让 serve.py 的看守停); 没有就直接结束进程树
    restart_cmd: list[str] | None = None  # 重启 (服务端: serve.py restart, 先试跑再换)
    elsewhere: object = None  # 返回 True = 有别的看守在管它 (服务端: 你在终端跑的 tools/serve.py run), 端口空着也不接手

    @property
    def label(self) -> str:
        return LABEL.get(self.name, self.name)


def _local(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def _port(url: str, default: int) -> int:
    try:
        return urlparse(url).port or default
    except ValueError:
        return default


def specs(cfg: dict | None = None, python: str | None = None) -> list[Spec]:
    """按配置算出要看着的服务. services.<名字>: "auto" (配置里用到了、文件都在就开) / "on" / "off"."""
    cfg = cfg or load_config()
    want = {"llm": "auto", "tts": "auto", "embed": "off", "escalate": "off", **cfg.get("services", {})}
    py = python or _console_python()
    llm, out = cfg.get("llm", {}), []
    server = llm.get("server", {})

    def model_ok(s: dict) -> bool:
        return resolve_path(s.get("exe", "")).is_file() and resolve_path(s.get("model", "")).is_file()

    def pick(name: str, used: bool, files: bool) -> bool:
        mode = str(want.get(name, "off"))
        return files and (mode == "on" or (mode == "auto" and used))

    if pick("llm", llm.get("engine") == "llamacpp" and _local(llm.get("endpoint", "")), model_ok(server)):
        out.append(Spec("llm", [py, str(ROOT / "tools" / "start_llm.py")], _port(llm["endpoint"], int(server.get("port", 8784))),
                        "/health", 180))
    emb = cfg.get("memory", {}).get("embed", {})
    if pick("embed", emb.get("engine") in ("qwen", "auto") and _local(emb.get("endpoint", "")), model_ok({**server, **emb.get("server", {})})):
        out.append(Spec("embed", [py, str(ROOT / "tools" / "start_llm.py"), "--which", "embed"],
                        int(emb.get("server", {}).get("port", 8786)), "/health", 120))
    esc = llm.get("escalate", {})
    if pick("escalate", bool(esc.get("endpoint")) and _local(esc.get("endpoint", "")), model_ok({**server, **esc.get("server", {})})):
        out.append(Spec("escalate", [py, str(ROOT / "tools" / "start_llm.py"), "--which", "escalate"],
                        int(esc.get("server", {}).get("port", 8785)), "/health", 120))
    # 本机大模型 (2026-11-02, llm.local_api.server: 桌宠、QQ、工作台选"本机模型"时用的 OpenAI 兼容服务). 默认不开 (要几 GB 内存,
    # CPU 也占着): services.local_api 写 "on" 才开; 你自己开的 Ollama、LM Studio 不用这个
    loc = {**server, **(llm.get("local_api") or {}).get("server", {})}
    if pick("local_api", bool((llm.get("local_api") or {}).get("server", {}).get("model")), model_ok(loc)):
        out.append(Spec("local_api", [py, str(ROOT / "tools" / "start_llm.py"), "--which", "local"],
                        int(loc.get("port", 8788)), "/health", 600))
    tts = cfg.get("tts", {})
    http = tts.get("http", {})
    if pick("tts", tts.get("engine") == "gpt_sovits" and _local(http.get("base_url", "")),
            resolve_path(http.get("server", {}).get("dir", "runtime/gpt-sovits")).is_dir()):
        out.append(Spec("tts", [py, str(ROOT / "tools" / "start_tts.py")], _port(http["base_url"], 9880), "tcp", 300))
    port = int(cfg.get("server", {}).get("port", 8790))
    serve = [py, str(ROOT / "tools" / "serve.py")]
    # 服务端不限起的时间: tools/serve.py 自己看着它 (看心跳分得清起得慢和起不来); 以前这里 150 秒没好就连 serve.py 一起结束,
    # 机器忙、A2F 载入一分多钟时整棵进程树被一遍遍杀掉重来 (2026-10-27 20:07~20:23)
    out.append(Spec("server", [*serve, "run", "--port", str(port)], port, "/api/status", None,
                    after=[s.name for s in out if s.name == "llm"], stop_cmd=[*serve, "stop"], restart_cmd=[*serve, "restart"],
                    elsewhere=serve_watchdog_alive))
    return out


def serve_watchdog_alive() -> bool:
    """tools/serve.py 的看守还活着 (它会自己把服务端拉起来, 重启、试跑时端口空几秒是正常的)."""
    try:
        st = json.loads((Path.home() / ".worldmodel" / "serve" / "serve.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # 嵌套得极深的 JSON 抛的是 RecursionError, 不是 ValueError
        return False
    return isinstance(st, dict) and _alive(st.get("supervisor"))  # 合法 JSON 但不是对象: 和读不出来一样当它不在


def _console_python() -> str:
    """子服务用带控制台的 python.exe (配 CREATE_NO_WINDOW 看不见; pythonw 起的 llama-server 会弹黑窗)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def healthy(spec: Spec, timeout: float = 1.5) -> bool:
    if spec.health.startswith("/"):
        try:  # 直连 (wm/localhttp.py): 环境变量里的代理会把本机地址也劫走, 查不到就把好好的本机模型一遍遍重启 (2026-11-11)
            with localhttp.open_direct(f"http://127.0.0.1:{spec.port}{spec.health}", timeout=timeout) as r:
                return r.status == 200
        except (OSError, ValueError, http.client.HTTPException):
            return False
    try:
        with socket.create_connection(("127.0.0.1", spec.port), timeout=timeout):
            return True
    except OSError:
        return False


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(int(pid))
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001 - 进程没了、看不到
        return False


def _kill_tree(pid: int, timeout: float = 8.0) -> None:
    try:
        import psutil
        root = psutil.Process(pid)
        procs = [*root.children(recursive=True), root]
    except Exception:  # noqa: BLE001
        return
    for p in procs:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    _, left = psutil.wait_procs(procs, timeout=timeout)
    for p in left:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


def _write_json(path: Path, obj) -> None:
    """先写旁边的临时文件再换过去 (读的人要么读到整份旧的、要么整份新的). Windows 上目标文件正被别的进程开着读的那一下,
    os.replace 会报 PermissionError (2026-11-11 在这台机器上量过: 一边反复换、一边反复读, 换失败 191/320 次): 稍等再换,
    最多 REPLACE_TRIES 次. 还不行 (或者写临时文件就出了错) 就把临时文件删掉再把错抛出去, 由调用的人决定怎么办
    (看守的 step 和 run 收尾走 _save_quietly: 记一行日志, 这一遍先不存)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        for i in range(1, REPLACE_TRIES + 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == REPLACE_TRIES:
                    raise
                time.sleep(REPLACE_WAIT_S * i)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _is_supervisor(pid) -> bool:
    """pid 还是那个看守 (进程号会被别的程序重用: 再看一眼命令行)."""
    if not _alive(pid):
        return False
    if int(pid) == os.getpid():
        return True
    try:
        import psutil
        return "wm.services" in " ".join(psutil.Process(int(pid)).cmdline())
    except Exception:  # noqa: BLE001
        return False


def _read_state_file(path: Path) -> dict:
    """读状态文件. 文件本来就不在: 当没有 ({}), 不等. 文件在却读不出来 (Windows 上正碰上看守在"换"它: 打开被拒 PermissionError,
    或者读到空的、半截的): 稍等再读, 最多 READ_TRIES 次, 还不行才当没有. 看守写它用的是"写临时文件再换", 读到的要么整份旧的、
    要么整份新的, 读不出来只能是撞上了换的那一瞬 —— 不该就此认定"看守不在了" (up() 会再起一个看守, /api/services 的重启和停会回 409)."""
    for i in range(1, READ_TRIES + 1):
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            got = None
        if isinstance(got, dict):
            return got
        if i < READ_TRIES:
            time.sleep(READ_WAIT_S * i)
    return {}


def read_state(home: Path = HOME) -> dict:
    """看守写的状态; 看守不在了就标出来 (服务可能还在, 但没人看着)."""
    st = _read_state_file(home / "state.json")
    st["running"] = _is_supervisor(st.get("pid"))
    return st


def request(op: str, names: list[str] | None = None, home: Path = HOME) -> Path:
    """给看守留一个请求 (restart / stop); 看守一秒内会看到."""
    path = home / f"req-{uuid.uuid4().hex[:10]}.json"
    _write_json(path, {"op": op, "names": names or [], "at": time.time()})
    return path


REQUEST_OPS = ("stop", "restart")  # 看守认得的请求 (request() 留的、python -m wm.services restart|stop 和面板 /api/services 发的)


def _bad_request(req) -> str:
    """请求内容不对就返回原因, 对的返回空: 得是个对象, op 得是 stop / restart, names 不给 (或 null) 或者是名字 (字符串) 的列表."""
    if not isinstance(req, dict):
        return f"内容不是对象 ({type(req).__name__})"
    op = req.get("op")
    if op not in REQUEST_OPS:
        return f"op 不认识 ({op[:40] if isinstance(op, str) else type(op).__name__})"  # 不对 op 取 str(): 嵌套得极深的内容 str() 会抛 RecursionError
    names = req.get("names")
    if names is not None and not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
        return "names 不是名字的列表"
    return ""


def _gone(path: Path) -> bool:
    """文件已经不在了 (看不到、没权限看的不算: 删到一半的文件会这样, 还当它在)."""
    try:
        path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _describe(exc: BaseException) -> str:
    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001 - 异常自己的 __str__ 坏了
        return type(exc).__name__


class Supervisor:
    """看着一组服务. 每个服务的状态: off (没开) / starting / up / external (端口上是别人开的, 不管) / down (挂了, 等重启) /
    failed (挂太多次, 不再拉) / stopped (你让停的)."""

    def __init__(self, specs_: list[Spec], home: Path = HOME, logs: Path = LOGS, tick: float = 1.0, restart_max: int = 5):
        self.specs = {s.name: s for s in specs_}
        self.home, self.logs, self.tick, self.restart_max = home, logs, tick, restart_max
        self.proc: dict[str, subprocess.Popen] = {}
        self.st = {n: {"name": n, "label": s.label, "port": s.port, "state": "off", "pid": None, "since": None,
                       "restarts": [], "error": "", "log": ""} for n, s in self.specs.items()}
        self.t0 = time.time()
        self.stop_all = False
        self._miss, self._miss_since, self._miss_said = 0, 0.0, 0.0  # 状态文件存不进去: 连着几次、从什么时候起、上一次往日志里说话的时刻
        self._dead: set[str] = set()  # 请求文件里不再执行的名字 (执行过的、上一次留下的、内容不对的): 文件删不掉就一直留着, 每轮再试着删, 没了就去掉
        self._req_bad: dict[str, float] = {}  # 读不出来的请求文件 -> 第一次读不出来的时刻 (time.monotonic)
        self._errs: dict[tuple, list] = {}  # run() 里出过的错 (类型, 出错位置) -> [上次往日志里记的时刻, 之后又出了几次]
        home.mkdir(parents=True, exist_ok=True)

    # -- 一个服务 --
    def _set(self, name: str, state: str, error: str = "") -> None:
        s = self.st[name]
        if s["state"] != state:
            s["since"] = time.time()
        s["state"], s["error"] = state, error

    def _start(self, name: str) -> None:
        """起一个服务. 起不来 (日志目录建不了、日志文件打不开、程序不在、被拦着: OSError; 命令写错了: TypeError、ValueError) 不往外抛 ——
        主看守是整棵进程树的根, 不能因为一个服务起不来就崩 (配置写错了也不能靠 run() 的兜底每圈作废、一分钟后退出): 开着的日志关掉
        (Windows 上漏一个句柄, 那个日志文件就删不掉、换不了), 这个服务记成 down、error 写清是哪一步什么错, 往 supervisor-<日期>.log 记一行.
        当成"起来就立刻挂了": 照 step 里现有的规矩算进重启次数、隔着退避再试, restart_max 次以后变 failed 不再拉 (总共试 1 + restart_max 次).
        _set 只在状态变了才更新 since, down 到 down 不变, 这里自己把 since 改成这一次失败的时刻, 退避才从这一次算起."""
        spec = self.specs[name]
        what, log, f = "建日志目录", None, None  # 做到哪一步了、日志文件 (出了岔子要说是哪一步, 开着的要关掉)
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            log = self.logs / f"{name}-{time.strftime('%Y%m%d')}.log"
            what = "开日志文件"
            f = open(log, "ab")  # noqa: SIM115 - 交给子进程, 它退出后由 _reap 关
            what = "写日志开头"
            f.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 看守启动: {' '.join(spec.cmd)}\n".encode("utf-8"))
            f.flush()
            what = "起进程"
            # 环境里有代理变量时补上 no_proxy (wm/localhttp.py): 这底下的看守、服务端和它们起的东西访问本机都不走代理
            env = localhttp.with_local_bypass({**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
            p = subprocess.Popen(spec.cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=env, creationflags=NO_WINDOW)
        except (OSError, TypeError, ValueError) as exc:  # 命令写错了 (里面有 None、空字符) 也是起不来
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
            s = self.st[name]
            prog = spec.cmd[0] if isinstance(spec.cmd, (list, tuple)) and spec.cmd else None
            where = f"{what} ({prog})" if what == "起进程" and prog is not None else what
            self._set(name, "down", f"{where}没成: {type(exc).__name__}: {exc}")
            s["pid"], s["since"] = None, time.time()
            if log is not None:
                s["log"] = str(log)
            self._log(f"起 {spec.label} ({name}) 没成 (连着第 {len(s['restarts']) + 1} 次): {where}: {type(exc).__name__}: {exc}")
            return
        p._wm_log = f  # type: ignore[attr-defined]
        self.proc[name] = p
        self.st[name].update(pid=p.pid, log=str(log))
        self._set(name, "starting")

    def _reap(self, name: str) -> int | None:
        """结束一个服务的进程 (杀进程树、关日志、从进程表里拿掉、清 pid, 返回退出码). 杀了还结束不掉 (等 10 秒它还在) 不往外抛 ——
        主看守不能因此崩: 照样关日志、拿掉、清 pid, 返回 None, 记一行到 supervisor-<日期>.log; 调用它的地方照原样往下走."""
        p = self.proc.pop(name, None)
        if not p:
            return None
        code = p.poll()
        if code is None:
            _kill_tree(p.pid)
            try:
                code = p.wait(timeout=10)
            except subprocess.TimeoutExpired:  # 卡在内核里、被杀毒软件拦着…: 它可能还占着端口或显存, 让人去任务管理器里结束它
                code = None
                self._log(f"{self.specs[name].label} ({name}) 的进程 {p.pid} 杀了等 10 秒还结束不掉: 已经从看守这边摘掉、日志关了; "
                          "它可能还占着端口或显存, 要在任务管理器里把它结束")
        try:
            p._wm_log.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self.st[name]["pid"] = None
        return code

    def _stop(self, name: str) -> None:
        spec, p = self.specs[name], self.proc.get(name)
        if p and spec.stop_cmd and p.poll() is None:
            try:
                subprocess.run(spec.stop_cmd, cwd=str(ROOT), capture_output=True, timeout=30, creationflags=NO_WINDOW)
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
            except OSError as exc:  # 停止命令跑不起来 (程序不在、被拦着): 记一行, 接着往下直接结束进程, 和等不到它自己停一样
                self._log(f"{spec.label} ({name}) 的停止命令 {spec.stop_cmd[:1]} 跑不起来 ({_describe(exc)}), 直接结束进程")
        self._reap(name)

    def _ready_to_start(self, name: str) -> bool:
        spec = self.specs[name]
        for dep in spec.after:
            d = self.st.get(dep)
            if d and d["state"] == "starting" and time.time() - (d["since"] or 0) < 60:
                return False
        return True

    def step(self) -> None:
        """看一遍: 处理请求, 该起的起, 挂了的按次数决定拉不拉."""
        self._requests()
        now = time.time()
        for name in ORDER:
            if name not in self.specs or self.stop_all:
                continue
            s, spec, p = self.st[name], self.specs[name], self.proc.get(name)
            if s["state"] in ("failed", "stopped"):
                continue
            if p is None:
                if (spec.elsewhere and spec.elsewhere()) or healthy(spec):
                    self._set(name, "external")  # 端口上有人在跑 (你自己开的), 不管它; 它没了再接手
                    s["gone"] = None
                    continue
                if s["state"] == "external":  # 外面那份刚没了: 等一会儿, 可能只是在重启
                    s["gone"] = s.get("gone") or now
                    if now - s["gone"] < TAKEOVER_S:
                        continue
                if s["state"] in ("external", "off", "down"):
                    if not self._ready_to_start(name):
                        continue
                    recent = [t for t in s["restarts"] if now - t < WINDOW_S]
                    s["restarts"] = recent
                    if s["state"] == "down" and len(recent) >= self.restart_max:
                        self._set(name, "failed", f"{WINDOW_S // 60} 分钟里挂了 {len(recent)} 次, 不再拉; 看日志后点重启")
                        continue
                    if s["state"] == "down":
                        backoff = min(60, 2 ** len(recent))
                        if now - (s["since"] or 0) < backoff:
                            continue
                        s["restarts"].append(now)
                    self._start(name)
                continue
            code = p.poll()
            if code is not None:
                self._reap(name)
                self._set(name, "down", f"退出了 (代码 {code}), 看 {s['log']}")
                continue
            if s["state"] == "starting":
                if healthy(spec):
                    self._set(name, "up")
                elif spec.ready_s is not None and now - (s["since"] or now) > spec.ready_s:
                    self._reap(name)
                    self._set(name, "down", f"{int(spec.ready_s)} 秒还没好, 看 {s['log']}")
            elif s["state"] == "up" and not healthy(spec, timeout=3):
                s["miss"] = s.get("miss", 0) + 1  # 偶尔一次查不到不算 (忙着推理时会慢)
                if s["miss"] >= 5 and name != "server":  # 服务端卡住由 serve.py 自己管
                    self._reap(name)
                    self._set(name, "down", "连着 5 次查不到, 重启")
            else:
                s["miss"] = 0
        self._save_quietly()

    def _requests(self) -> None:
        """处理请求文件 (req-*.json: restart / stop, python -m wm.services restart|stop 和面板 /api/services 留的). 每个请求只执行一次
        (先记上名字、再删文件、再执行); 执行了但文件删不掉的 (被别的程序开着) 记住名字、以后不再执行、每轮再试着删. 读不出来的先留着
        下一轮再读, 连着 REQ_READ_S 秒读不出来就记一行、删掉、不执行; 内容不对的记一行、删掉、不执行. 上一次留下的旧请求绝不执行:
        run() 开头看到的名字、文件时间早于看守起来的时刻的, 一律不执行 (试着删, 每个文件只记一行)."""
        for name in list(self._dead):  # 不再执行的名单: 每轮再试着删, 删掉了或文件没了就从名单里去掉
            path = self.home / name
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            if _gone(path):
                self._dead.discard(name)
        for name in list(self._req_bad):  # 读不出来的那些要是已经没了 (别人删了): 不用再等
            if _gone(self.home / name):
                del self._req_bad[name]
        now = time.monotonic()
        for path in sorted(self.home.glob("req-*.json")):
            name = path.name
            if name in self._dead:
                continue
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:  # 刚被别人删了
                continue
            except OSError:
                mtime = None
            if mtime is not None and mtime < self.t0 - REQ_OLD_SLACK_S:
                self._req_bad.pop(name, None)
                self._drop(path, "是上一次留下的旧请求 (文件时间早于看守起来的时刻), 不执行")
                continue
            try:
                req = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError, RecursionError) as exc:  # 读不出来 (被别的程序开着、读到半截、嵌套得太深): 先留着, 下一轮再读
                first = self._req_bad.setdefault(name, now)
                if now - first >= REQ_READ_S:
                    del self._req_bad[name]
                    self._drop(path, f"连着 {now - first:.1f} 秒读不出来 ({_describe(exc)}), 不执行")
                continue
            self._req_bad.pop(name, None)
            why = _bad_request(req)
            if why:
                self._drop(path, f"{why}, 不执行")
                continue
            self._dead.add(name)  # 先记上: 这个请求只执行这一次, 不管后面删不删得掉、执行到一半出不出错
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._log(f"请求文件 {name} 删不掉 ({_describe(exc)}): 这一次照常执行, 记住它以后不再执行, 每轮再试着删")
            self._execute(req)

    def _drop(self, path: Path, why: str) -> None:
        """这个请求不执行: 记一行、试着删掉; 删不掉的 (被别的程序开着) 留在"不再执行"的名单里, 每轮再试着删 (不再记)."""
        self._log(f"请求文件 {path.name} {why}")
        self._dead.add(path.name)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _execute(self, req: dict) -> None:
        names = [n for n in (req.get("names") or list(self.specs)) if n in self.specs]
        if req.get("op") == "stop":
            for n in reversed(ORDER):
                if n in names:
                    self._stop(n)
                    self._set(n, "stopped")
            if not req.get("names"):
                self.stop_all = True
        elif req.get("op") == "restart":
            for n in names:
                spec = self.specs[n]
                if spec.restart_cmd and n in self.proc and self.proc[n].poll() is None:
                    # 服务端: 让 serve.py 自己先试跑再换 (它的看守进程不换, 我们这边不用动)
                    threading.Thread(target=subprocess.run, args=(spec.restart_cmd,),
                                     kwargs={"cwd": str(ROOT), "capture_output": True, "creationflags": NO_WINDOW}, daemon=True).start()
                    continue
                self._reap(n)
                self.st[n]["restarts"] = []
                self._set(n, "off")

    def save(self) -> None:
        """写状态文件 (桌宠托盘、设置面板读它). 写不进去照直往外抛 (_write_json 已经稍等再换过几次了): 子类 (下载队列的小看守)
        可以自己重写 save 决定怎么办; step 和 run 收尾走 _save_quietly, 不让它连累看守."""
        _write_json(self.home / "state.json", {
            "pid": os.getpid(), "root": str(ROOT), "started": self.t0, "updated": time.time(), "stopping": self.stop_all,
            "services": [self.st[n] for n in ORDER if n in self.st]})

    def _log(self, text: str) -> None:
        """看守自己的话记进 logs/services/supervisor-<日期>.log (主看守是 pythonw 起的, 没有控制台, print 看不到); 日志也写不进去就算了."""
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            with open(self.logs / f"supervisor-{time.strftime('%Y%m%d')}.log", "ab") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n".encode("utf-8"))
        except OSError:
            pass

    def _save_quietly(self) -> None:
        """step 和 run 收尾存状态用: 存不进去 (Windows 上别的进程正开着状态文件读, 换写一直被拒) 不往外抛 —— 主看守是整棵进程树的根
        (服务端、本机模型、语音合成都挂在它下面), 不能因为这个崩; run 收尾那次也不能抛出来盖掉真正的退出原因. 记一行到
        supervisor-<日期>.log (同一阵连着失败每 SAVE_SAY_S 秒最多一行), 这一遍先不存, 下一遍照常再存; 存上了说一声中间落下几次."""
        try:
            self.save()
        except Exception as exc:  # noqa: BLE001 - 存状态是附带的活, 出什么错都不该连累看守
            now = time.time()
            self._miss += 1
            if self._miss == 1:
                self._miss_since = now
            if self._miss == 1 or now - self._miss_said >= SAVE_SAY_S:
                self._miss_said = now
                self._log(f"状态文件 state.json 存不进去 (连着 {self._miss} 次, 已 {now - self._miss_since:.0f} 秒; "
                          f"{type(exc).__name__}: {exc}): 这一遍先不存, 下一遍再试")
        else:
            if self._miss:
                self._log(f"状态文件又存上了 (中间 {self._miss} 次没存上, 共 {time.time() - self._miss_since:.0f} 秒)")
                self._miss = 0

    def _round_failed(self, exc: Exception, fails: int) -> None:
        """run() 里一圈 step() 出了没接住的错 (这一圈作废, 下一圈照常): 第一次出某个错 (类型加出错位置) 记带完整 traceback 的几行; 同一个错
        重复时最多每 LOOP_SAY_S 秒记一行、带次数; 换了个错马上记. 记日志本身出错也不能连累看守."""
        try:
            tb = traceback.extract_tb(exc.__traceback__)
            where = f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno} {tb[-1].name}" if tb else "?"
            key, now = (type(exc).__name__, where), time.time()
            seen = self._errs.get(key)
            if seen is None:
                self._errs[key] = [now, 0]
                text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
                self._log(f"看守这一圈出错 (这一圈作废, 下一圈照常; 连着第 {fails} 圈): {_describe(exc)}\n"
                          + "\n".join("    " + ln for ln in text.splitlines()))
                return
            seen[1] += 1
            if now - seen[0] >= LOOP_SAY_S:
                self._log(f"同一个错 ({key[0]} 在 {where}) 又出了 {seen[1]} 次 (连着第 {fails} 圈): {_describe(exc)}")
                seen[0], seen[1] = now, 0
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> str:
        self.home.mkdir(parents=True, exist_ok=True)
        for old in list(self.home.glob("req-*.json")):  # 上一次留下的请求绝不执行: 不管删没删掉, 这个名字都记进"不再执行"的名单
            self._dead.add(old.name)
            try:
                old.unlink(missing_ok=True)
            except OSError as exc:  # 被别的程序开着 (杀毒扫描之类): 不能因为这个起不来, 跳过它, 每轮再试着删
                self._log(f"上一次留下的请求文件 {old.name} 删不掉 ({_describe(exc)}): 不执行, 每轮再试着删")
        fails = 0  # 连着出错的圈数
        try:
            while not self.stop_all:
                try:
                    self.step()
                    fails = 0
                except Exception as exc:  # noqa: BLE001 - 一圈里没接住的错: 这一圈作废, 下一圈照常
                    # (主看守是 pythonw 起的, 没有控制台: 抛了没人接, 进程就没了, 什么痕迹都不留. KeyboardInterrupt、SystemExit 不是 Exception, 照原路走)
                    fails += 1
                    self._round_failed(exc, fails)
                    if fails >= LOOP_FAIL_MAX:
                        self._log(f"连着 {fails} 圈出错, 看守退出 (最后一个错: {_describe(exc)})")
                        raise
                time.sleep(self.tick)
            return "stop"
        except KeyboardInterrupt:
            for n in reversed(ORDER):
                if n in self.proc:
                    self._stop(n)
            return "interrupted"
        finally:
            self._save_quietly()


def up(home: Path = HOME) -> bool:
    """看守没在跑就在后台起一个 (pythonw, 不弹窗). 返回 True = 新起了一个."""
    if read_state(home)["running"]:
        return False
    exe = Path(sys.executable)
    w = exe.parent / "pythonw.exe"
    subprocess.Popen([str(w if w.is_file() else exe), "-m", "wm.services", "run"], cwd=str(ROOT),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=DETACH | NO_WINDOW, close_fds=True)
    return True


MARK = {"up": "●", "external": "●", "starting": "◌", "down": "✕", "failed": "✕", "stopped": "○", "off": "○"}
WORD = {"up": "在跑", "external": "在跑 (外面开的)", "starting": "启动中", "down": "挂了, 等重启", "failed": "起不来",
        "stopped": "停了", "off": "还没开"}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m wm.services", description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=["run", "up", "status", "restart", "stop"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", help="只管这个目录装的那一份 (卸载程序用: 别把同一台电脑上另一份的服务也停了)")
    a = ap.parse_args(argv)
    if a.what == "run":
        if read_state()["running"]:
            print("看守已经在跑了")
            return 0
        cfg = load_config()
        sup = Supervisor(specs(cfg), restart_max=int(cfg.get("services", {}).get("restart_max", 5)))
        return 0 if sup.run() in ("stop", "interrupted") else 1
    if a.what == "up":
        print("在后台起了看守" if up() else "看守已经在跑了")
        return 0
    st = read_state()
    if a.what == "status":
        if a.json:
            print(json.dumps(st, ensure_ascii=False))
            return 0
        print(f"看守{'在跑 (进程 ' + str(st.get('pid')) + ')' if st['running'] else '没在跑 (python -m wm.services up 起它)'}")
        if not st.get("services"):  # 看守没起过: 按配置列出该有的, 当场查端口
            st["services"] = [{"label": sp.label, "port": sp.port, "state": "external" if healthy(sp, 0.5) else "off"} for sp in specs()]
        for s in st.get("services", []):
            print(f"  {MARK.get(s['state'], '?')} {s['label']:<10} {s['port']:>5}  {WORD.get(s['state'], s['state'])}"
                  f"{'  ' + s['error'] if s.get('error') else ''}")
        return 0
    # 状态里没写目录的 (旧版看守) 也算另一份: 空路径会被当成当前目录, 卸载程序就在装的目录里跑, 会误认成自己的
    if a.root and st["running"] and (not st.get("root") or Path(st["root"]).resolve() != Path(a.root).resolve()):
        print(f"在跑的看守是另一份 ({st.get('root') or '不知道哪一份'}) 的, 不管它")
        return 0
    if not st["running"]:
        if a.what == "stop":
            print("看守没在跑, 没有要停的")
            return 0
        up()
        print("看守没在跑, 起了一个 (它会把该开的都开起来)")
        return 0
    request(a.what, a.names)
    print(f"已经请看守{'重启' if a.what == 'restart' else '停掉'}: {' '.join(a.names) or '全部'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
