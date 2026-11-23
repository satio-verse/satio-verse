"""看守服务端 (docs/roadmap/self-evolution.md 3.4、8.2、8.3): 起 python -m wm, 崩了拉起来, 卡死了也算崩;
"卡死"看服务端的心跳 (wm/beat.py), 不只看 /api/status: 查不到但事件循环还在转 = 机器忙、只是慢, 不杀 (见 Supervisor.judge);
每分钟记一次运行指标 (内存、句柄、线程), 崩了写崩溃记录 (退出码、最后的输出、和上一次好好跑过的那次比的指标).
她的补丁合进来以后 (工作台记在 ~/.worldmodel/serve/canary.json) 的第一次启动, 60 秒内崩了或者一直没起来:
自动退回那次合并 (和 python -m wm.workbench revert 一样), 用旧代码起来, 记成"踩过的坑" (下一次改桌宠的任务书里会带上).

  python tools/serve.py run [--port 8790] [-- 其余参数照传给 python -m wm]
      在终端标签页里跑它, 取代直接跑 python -m wm; 服务端的输出照常打在这里, 也存在 logs/serve/server-<日期>.log
  python tools/serve.py status          看守和服务端在不在、起了多久、有没有待验证的合并、上一次崩溃
  python tools/serve.py restart         让看守重启服务端: 先挑一个空着的端口用替身引擎试跑一次 (不连 QQ、不读记忆库), 起不来就不重启
  python tools/serve.py restart --no-trial
  python tools/serve.py stop            停掉服务端和看守
  python tools/serve.py trial [--port N]   只试跑 (wm/workbench.py 的 trial; 她的补丁合进来时服务端自己也先试跑一次)

文件: ~/.worldmodel/serve/ 下 serve.json (进程号、端口、这次启动的编号)、canary.json (待验证的合并)、restart / stop (请求)、
beat.json (服务端的心跳)、crash-<时间>.json (崩溃记录); 踩过的坑在 ~/.worldmodel/pitfalls.jsonl; 运行指标在 logs/serve/metrics-<日期>.jsonl;
等你点头的申请在 ~/.worldmodel/asks.json (起服务端时放进 WM_ASKS_FILE: 重启以后接着等, 见 wm/channels/manager.py 的 restore_asks).
"""
from __future__ import annotations

import argparse
import collections
import http.client
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wm import beat as B  # noqa: E402
from wm import localhttp  # noqa: E402
from wm import workbench as W  # noqa: E402

HOME = W.HOME / "serve"
LOGS = ROOT / "logs" / "serve"
status_ok = W.status_ok
status_code = W.status_code  # 直连本机, 不走代理 (wm/localhttp.py); 回了话但不是 200 和"连不上"分得开
trial = W.trial  # 试跑在工作台里 (合并以后服务端自己也要用)


def port_busy(port: int) -> int | None:
    """有程序在听这个端口: 返回它的进程号 (看不到是谁时 -1)."""
    for c in psutil.net_connections("tcp"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
            return c.pid or -1
    return None


def _json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _title(job: dict) -> str:
    """待验证的合并是哪件事: 工作台报告的第一行 "# 工作台: 题目 (任务号)"; 读不到就用任务号."""
    try:
        head = (Path(job["dir"]) / "report.md").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, KeyError, IndexError):
        return str(job.get("job", ""))
    return head.removeprefix("# 工作台: ").rsplit(" (", 1)[0].strip() or str(job.get("job", ""))


class Supervisor:
    def __init__(self, port: int = 8790, extra: list[str] = (), home: Path = HOME, logs: Path = LOGS,
                 project: Path = ROOT, cmd: list[str] | None = None, out=None,
                 status_every: float = 10.0, status_timeout: float = 15.0, hung_s: float = 90.0, hung_max_s: float = 600.0,
                 startup_s: float = 120.0, startup_max_s: float = 600.0, canary_s: float = 60.0,
                 metrics_every: float = 60.0, max_restarts: int = 3, restart_window: float = 600.0):
        """cmd: 起服务端的命令 (测试换成替身); out: 服务端输出和看守的话写到哪 (默认终端).
        status_timeout: 查一次 /api/status 等多久 (以前是 5 秒: 门禁、别的会话的测试占满机器时它要 7~9 秒才回);
        hung_s / hung_max_s: 起来以后连着这么久查不到才看心跳 / 查不到这么久不管心跳都算卡死;
        startup_s / startup_max_s: 起的时候这么久还没起来才看还动不动 / 不管动不动都算起不来 (见 judge)."""
        self.port, self.home, self.logs, self.project = port, home, logs, project
        self.cmd = cmd or [sys.executable, "-m", "wm", "--port", str(port), *extra]
        self.out = out if out is not None else sys.stdout.buffer
        self.status_every, self.status_timeout, self.hung_s, self.hung_max_s = status_every, status_timeout, hung_s, hung_max_s
        self.startup_s, self.startup_max_s, self.canary_s = startup_s, startup_max_s, canary_s
        self.metrics_every, self.max_restarts, self.restart_window = metrics_every, max_restarts, restart_window
        self.proc: subprocess.Popen | None = None
        self.boot, self.started, self.ok_once = "", 0.0, False
        self.beat_up = False  # 这次启动一直查不到 /api/status, 可服务端的心跳说起好了 (judge 的 "blind"): 不杀, 也不据此退回合并
        self.last_ok = 0.0  # 上一次查到 /api/status
        self.beat_file = home / "beat.json"  # 服务端的心跳 (wm/beat.py; 起服务端时放进环境变量 WM_BEAT_FILE)
        self.last_beat: dict | None = None  # 这次启动最近一次读到的心跳
        self._ps: psutil.Process | None = None  # 服务端进程 (同一个对象才算得出 CPU 占用)
        self._cpu: float | None = None  # 上一次查不到时服务端进程累计用了多少 CPU 秒
        self._said = 0.0  # 上一次说"忙、慢、接着等"的时刻 (一分钟说一次)
        self.tail: collections.deque[str] = collections.deque(maxlen=200)  # 服务端最后的输出
        self.metrics: collections.deque[dict] = collections.deque(maxlen=30)  # 这次启动的运行指标
        self.baseline: list[dict] = []  # 上一次好好跑过 (起来了、过了 canary_s) 的那次的指标
        self._lock = threading.Lock()
        self.key = secrets.token_hex(16)  # 服务端的 /api/supervisor 只认这把钥匙 (起服务端时放进环境变量)
        self.next_mode, self.next_note = "normal", ""  # 下一次启动告诉服务端的: 为什么起 (桌宠醒来时据此说一句)
        self.boot_s = 30.0  # 上一次从起进程到 /api/status 通用了多久 (告诉桌宠大概要睡多久)

    # -- 输出 -------------------------------------------------------------------
    def _emit(self, data: bytes) -> None:
        with self._lock:
            try:
                self.out.write(data)
                self.out.flush()
            except (OSError, ValueError):
                pass

    def say(self, text: str) -> None:
        self._emit(f"[看守 {time.strftime('%H:%M:%S')}] {text}\n".encode("utf-8"))

    def _pump(self, proc: subprocess.Popen, log) -> None:
        for line in iter(proc.stdout.readline, b""):
            self.tail.append(line.decode("utf-8", "replace").rstrip())
            self._emit(line)
            log.write(line)
            log.flush()
        log.close()

    # -- 起、停 -----------------------------------------------------------------
    def _start(self) -> None:
        self.boot, self.ok_once, self.beat_up = uuid.uuid4().hex[:8], False, False
        self.tail.clear()
        self.metrics.clear()
        self.logs.mkdir(parents=True, exist_ok=True)
        log = open(self.logs / f"server-{time.strftime('%Y%m%d')}.log", "ab")
        log.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} boot={self.boot} =====\n".encode("utf-8"))
        canary = _json(self.home / "canary.json")
        if canary.get("jobs"):  # 待验证的合并: 由这一次启动来验证 (之前没验证完的也算这一次)
            for j in canary["jobs"]:
                j["boot"] = self.boot
            _write(self.home / "canary.json", canary)
            self.say(f"这次启动在验证刚合进来的: {', '.join(j['job'] for j in canary['jobs'])} "
                     f"({self.canary_s:.0f} 秒内起不来就自动退回)")
        # 这次启动的情况交给服务端 (wm/app.py 的 boot_info, 放进 hello): 桌宠醒来时说 "学会了…" / "刚才那版没跑起来, 退回去了"
        learned = [_title(j) for j in canary.get("jobs", [])]
        self.beat_file.unlink(missing_ok=True)  # 上一次的心跳不算
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "WM_SUPERVISOR_KEY": self.key,
               "WM_BOOT_ID": self.boot, "WM_BOOT_MODE": self.next_mode, "WM_BOOT_NOTE": self.next_note,
               "WM_BOOT_LEARNED": "\n".join(learned), "WM_BEAT_FILE": str(self.beat_file),
               "WM_ASKS_FILE": str(self.home.parent / "asks.json")}  # 正式服务端: 等你点头的申请存这里, 重启以后接着等
        env = localhttp.with_local_bypass(env)  # 环境里有代理变量时补上 no_proxy: 服务端和它起的子进程访问本机不走代理
        self.next_mode, self.next_note = "normal", ""
        self.proc = subprocess.Popen(self.cmd, cwd=str(self.project), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        threading.Thread(target=self._pump, args=(self.proc, log), daemon=True).start()
        self.started = self.last_ok = time.time()
        self.last_beat, self._cpu, self._said = None, None, 0.0
        try:
            self._ps = psutil.Process(self.proc.pid)
            self._ps.cpu_percent(None)  # 第一次调只是开始计
        except psutil.Error:
            self._ps = None
        _write(self.home / "serve.json", {"supervisor": os.getpid(), "server": self.proc.pid, "port": self.port, "boot": self.boot,
                                          "started": time.strftime("%Y-%m-%d %H:%M:%S"), "cmd": self.cmd})
        self.say(f"起服务端 (端口 {self.port}, 启动编号 {self.boot}, 进程 {self.proc.pid})")

    def _notify(self, action: str) -> dict | None:
        """要重启 / 停服务端之前先告诉它 (POST /api/supervisor): 它告诉连着的桌宠 (她去睡一会儿, 你的话变便利贴),
        等正在说的那一轮说完再回. 服务端已经不通了、不认这个接口 (老代码) 就算了, 照样停."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/supervisor", method="POST",
                                     data=json.dumps({"action": action, "reason": action, "eta_s": round(self.boot_s)}).encode(),
                                     headers={"Content-Type": "application/json", "X-Supervisor-Key": self.key})
        try:
            with localhttp.open_direct(req, timeout=15) as r:  # 直连: 别让环境变量里的代理把本机地址劫走
                got = json.loads(r.read().decode("utf-8") or "{}")
        except (OSError, ValueError, http.client.HTTPException):
            return None
        self.say(f"告诉了 {got.get('sessions', 0)} 个桌宠要{'停' if action == 'stop' else '重启'}了 (等了 {got.get('waited_s', 0)} 秒)")
        return got

    def _kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)

    # -- 看着它 -----------------------------------------------------------------
    def _says_up(self, now: float, beat: dict | None) -> bool:
        """服务端自己说起好了 (心跳的 phase 是 wm/beat.py 的 READY), 心跳是刚写的, 事件循环也还在转."""
        return (beat is not None and beat.get("phase") == B.READY and now - float(beat.get("t") or 0) < self.hung_s
                and now - float(beat.get("loop") or 0) < self.hung_s)

    def judge(self, now: float, beat: dict | None, cpu_moved: bool, answered: bool = False) -> tuple[str, str]:
        """这一次 /api/status 没查到 200: 返回 ("wait", 要说的话, 空 = 不用说)、("blind", 要说的话) 或 ("kill", 原因).
        answered: 服务端回了话, 只是不是 200 (它自己的毛病); False = 连不上、没回话 (超时、被断开).
        机器忙的时候 (门禁跑 pytest、别的会话跑测试和模拟) 服务端只是慢, 杀了重起反而更糟: 重起要重新载入模型,
        机器还是那么忙, 起得更慢 (2026-10-27 来回崩了十几次). 所以不看"慢", 看"还动不动" — 服务端的心跳 (wm/beat.py):
        - 起的时候: startup_s 内照等; 之后进程还在动 (心跳还在写, 或者在用 CPU: 在载入模型) 就接着等, 到 startup_max_s 为止;
          例外 (blind): 过了 startup_s, 一句回话都没收到, 可服务端的心跳说起好了、事件循环也在转 — 毛病在看守连本机端口这条路上,
          不在服务端. 杀了重起不会好, 更不能据此把刚合进来的改动退回: 不杀, 接着等, 待验证的合并留着不动.
          (2026-11-11: 环境变量里的代理把本机地址劫走, 看守每 607 秒杀一次好好的服务端, 误退回了两次合并. 探测已经改成直连,
          这一条防的是别的原因让这条路整个不通. 那天代理多数回的是 502, 算"回了话", 光靠这一条救不了, 救的是直连.)
          回了话但不是 200 的不算: 探测是直连的, 回话的只会是那个端口上的服务端自己, 是它的 /api/status 坏了, 照老规矩;
        - 起来以后: 连着 hung_s 查不到才看心跳 — 事件循环还在转 = 只是慢, 等; 循环停了而进程还在跑 = 卡死了, 杀;
          整个进程都没动静 (机器被占满、在换页) 等到 hung_max_s; 没有心跳 (老代码) 照老办法, 连着 hung_s 查不到就杀."""
        alive = beat is not None and now - float(beat.get("t") or 0) < self.hung_s
        where = f", 在{beat['phase']}" if beat and beat.get("phase") else ""
        if not self.ok_once:
            up = now - self.started
            if up < self.startup_s:
                return "wait", ""
            if not answered and self._says_up(now, beat):
                return "blind", (f"已经 {up:.0f} 秒查不到 /api/status (连不上、没回话), 可服务端的心跳说起好了、事件循环还在转: "
                                 "毛病在看守连本机端口这条路上 (代理、防火墙?), 不当成没起来, 不杀, 也不退回刚合进来的改动")
            if up >= self.startup_max_s:
                return "kill", f"{up:.0f} 秒还没起来{where}"
            if alive or cpu_moved:
                return "wait", f"起得慢: 已经 {up:.0f} 秒{where}, 还在动, 接着等 (最多 {self.startup_max_s:.0f} 秒)"
            return "kill", f"{up:.0f} 秒还没起来, 也没在动{where}"
        silent = now - self.last_ok
        if silent < self.hung_s:
            return "wait", ""
        if silent >= self.hung_max_s:
            return "kill", f"卡住了: {silent:.0f} 秒查不到 /api/status"
        if beat is None:
            return "kill", f"卡住了: 连着 {silent:.0f} 秒查不到 /api/status (没有心跳)"
        lag = now - float(beat.get("loop") or 0)
        if lag < self.hung_s:
            return "wait", f"服务端忙: /api/status {silent:.0f} 秒没回, 事件循环 {lag:.0f} 秒前还在转, 不当卡住"
        if alive:
            return "kill", f"卡住了: 事件循环 {lag:.0f} 秒没转 (进程还在跑, /api/status {silent:.0f} 秒没回)"
        return "wait", (f"服务端整个 {now - float(beat.get('t') or 0):.0f} 秒没动静 (机器太忙?), "
                        f"查不到 {self.hung_max_s:.0f} 秒再当卡住")

    def _beat(self) -> dict | None:
        """这次启动的心跳 (读到一半、正在换文件时用上一次读到的)."""
        got = B.read(self.beat_file)
        if got and got.get("boot") == self.boot:
            self.last_beat = got
        return self.last_beat

    def _cpu_moved(self) -> bool:
        """服务端进程从上一次查不到到现在用了 CPU 没有 (起得慢时: 在载入模型, 不是卡住了)."""
        try:
            t = self._ps.cpu_times()
        except (psutil.Error, AttributeError):
            return False
        prev, self._cpu = self._cpu, t.user + t.system
        return prev is not None and self._cpu - prev >= 0.2

    def _watch(self) -> tuple[str, str, int | None]:
        """返回 (stop / restart / crash, 原因, 退出码)."""
        last_status = last_metrics = 0.0
        while True:
            for name in ("stop", "restart"):
                req = self.home / name
                if req.exists():
                    req.unlink(missing_ok=True)
                    if self.ok_once:
                        self._notify(name)
                    self._kill()
                    return name, "", None
            code = self.proc.poll()
            if code is not None:
                return "crash", f"退出了 (退出码 {code})", code
            now = time.time()
            if now - last_status >= self.status_every:
                last_status = now
                code = status_code(self.port, timeout=self.status_timeout)
                if code == 200:
                    now = time.time()
                    if not self.ok_once:
                        self.boot_s = now - self.started
                        self.say(f"服务端起来了 (用了 {now - self.started:.0f} 秒)")
                    elif self._said:
                        self.say(f"服务端缓过来了 (/api/status {now - self.last_ok:.0f} 秒没回)")
                    self.ok_once, self.last_ok, self._said = True, now, 0.0
                else:
                    now = time.time()
                    what, why = self.judge(now, self._beat(), self._cpu_moved(), answered=code is not None)
                    if what == "kill":
                        self._kill()
                        return "crash", why, None
                    if what == "blind":
                        self.beat_up = True
                    if why and now - self._said >= (300 if what == "blind" else 60):  # 这条路不通是一直的事, 5 分钟说一次
                        self.say(f"{why} ({_machine()})")
                        self._said = now
            if self.ok_once and now - self.started >= self.canary_s:
                self._verified()
            if now - last_metrics >= self.metrics_every:
                last_metrics = now
                self._sample()
            time.sleep(min(1.0, self.status_every / 2))

    def _verified(self) -> None:
        canary = _json(self.home / "canary.json")
        mine = [j for j in canary.get("jobs", []) if j.get("boot") == self.boot]
        if mine:
            _write(self.home / "canary.json", {"jobs": [j for j in canary["jobs"] if j.get("boot") != self.boot]})
            self.say(f"刚合进来的 {', '.join(j['job'] for j in mine)} 跑了 {self.canary_s:.0f} 秒没事, 算验证过了")

    def _sample(self) -> None:
        try:
            p = self._ps or psutil.Process(self.proc.pid)  # 同一个进程对象: 每次新建的话 cpu_percent 永远是 0
            with p.oneshot():
                m = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "boot": self.boot, "rss_mb": round(p.memory_info().rss / 2 ** 20, 1),
                     "handles": p.num_handles() if hasattr(p, "num_handles") else p.num_fds(), "threads": p.num_threads(),
                     "cpu": p.cpu_percent(None)}
        except psutil.Error:
            return
        self.metrics.append(m)
        with open(self.logs / f"metrics-{time.strftime('%Y%m%d')}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    def _crashed(self, reason: str, code: int | None) -> Path:
        """崩了: 刚合进来、还没验证的那几次合并退回去 (记成踩过的坑), 写崩溃记录."""
        uptime = time.time() - self.started
        time.sleep(0.3)  # 让输出线程把最后几行读完
        jobs = [j for j in _json(self.home / "canary.json").get("jobs", []) if j.get("boot") == self.boot]
        reverted = []
        # 心跳说起好过 (beat_up) 的这一次不算"一上线就起不来": 只是看守查不到它, 待验证的合并留给下一次启动接着验证
        if jobs and (uptime < self.canary_s or not (self.ok_once or self.beat_up)):
            for j in reversed(jobs):
                try:
                    done, conflicts = W.revert(Path(j["dir"]), Path(j.get("project") or self.project))
                except (OSError, KeyError) as exc:
                    done, conflicts = [], [f"{type(exc).__name__}: {exc}"]
                reverted.append({"job": j["job"], "files": done, "conflicts": conflicts})
                with open(self.home.parent / "pitfalls.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M"), "job": j["job"], "files": j.get("files") or [],
                                        "reason": reason, "tail": list(self.tail)[-30:]}, ensure_ascii=False) + "\n")
                self.say(f"刚合进来的 {j['job']} 一上线服务端就{reason}: 退回了 {', '.join(done) or '没有'}"
                         + (f"; 这几个合进来以后又被改过, 没动: {', '.join(conflicts)}" if conflicts else ""))
        if reverted:
            self.next_mode = "reverted"
            self.next_note = f"刚合进来的「{'」「'.join(_title(j) for j in jobs)}」一上线服务端就{reason}, 已经退回去了"
        else:
            self.next_mode, self.next_note = "crashed", f"服务端刚才{reason}, 看守进程把它拉起来了"
        report = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "boot": self.boot, "reason": reason, "exit_code": code,
                  "uptime_s": round(uptime, 1), "cmd": self.cmd, "tail": list(self.tail)[-50:],
                  "metrics": list(self.metrics)[-5:], "baseline": self.baseline[-5:], "reverted": reverted,
                  "beat": self.last_beat, "machine": _machine()}
        path = self.home / f"crash-{time.strftime('%Y%m%d-%H%M%S')}-{self.boot}.json"
        _write(path, report)
        self.say(f"服务端{reason} (跑了 {uptime:.0f} 秒), 崩溃记录 {path}")
        return path

    def run(self) -> str:
        """一直看着, 返回为什么停了: stop / too_many_crashes / port_busy / interrupted."""
        busy = port_busy(self.port)
        if busy:
            self.say(f"端口 {self.port} 已经有程序在听 (进程 {busy}): 先停掉它, 再用看守起")
            return "port_busy"
        self.home.mkdir(parents=True, exist_ok=True)
        for name in ("stop", "restart"):
            (self.home / name).unlink(missing_ok=True)
        crashes: collections.deque[float] = collections.deque()
        try:
            while True:
                self._start()
                what, reason, code = self._watch()
                if self.ok_once and time.time() - self.started >= self.canary_s:
                    self.baseline = list(self.metrics)
                if what == "stop":
                    self.say("停了")
                    return "stop"
                if what == "restart":
                    self.say("照请求重启服务端")
                    self.next_mode = "restarted"
                    continue
                self._crashed(reason, code)
                now = time.time()
                crashes.append(now)
                while crashes and now - crashes[0] > self.restart_window:
                    crashes.popleft()
                if len(crashes) > self.max_restarts:
                    self.say(f"{self.restart_window / 60:.0f} 分钟里崩了 {len(crashes)} 次, 不再拉起; 修好以后重新 python tools/serve.py run")
                    return "too_many_crashes"
                time.sleep(1)
        except KeyboardInterrupt:
            self._kill()
            self.say("Ctrl+C: 服务端停了")
            return "interrupted"
        finally:
            (self.home / "serve.json").unlink(missing_ok=True)


def _machine() -> str:
    """这台电脑这会儿忙不忙 (说"服务端慢"时、崩溃记录里带上: 是不是别的程序把机器占满了)."""
    try:
        mem = psutil.virtual_memory()
        return f"整机 CPU {psutil.cpu_percent(None):.0f}%, 内存剩 {mem.available / 2 ** 30:.1f} GB"
    except (psutil.Error, OSError):
        return "整机负载读不到"


def _alive(pid) -> bool:
    return isinstance(pid, int) and psutil.pid_exists(pid)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python tools/serve.py", description="看守服务端")
    ap.add_argument("--home", type=Path, default=HOME, help="看守的文件放哪 (默认 ~/.worldmodel/serve; 测试另起一个时换开, 别和 8790 的撞)")
    sub = ap.add_subparsers(dest="what", required=True)
    r = sub.add_parser("run")
    r.add_argument("--port", type=int, default=8790)
    r.add_argument("rest", nargs=argparse.REMAINDER, help="-- 后面的照传给 python -m wm")
    sub.add_parser("status")
    rs = sub.add_parser("restart")
    rs.add_argument("--no-trial", action="store_true")
    sub.add_parser("stop")
    t = sub.add_parser("trial")
    t.add_argument("--port", type=int, default=0, help="默认挑一个空着的端口")
    a = ap.parse_args(argv)
    state = _json(a.home / "serve.json")
    if a.what == "run":
        rest = a.rest[1:] if a.rest[:1] == ["--"] else a.rest
        why = Supervisor(a.port, rest, home=a.home).run()
        return 0 if why in ("stop", "interrupted") else 1
    if a.what == "trial":
        ok, text = trial(port=a.port or None)
        print(text)
        return 0 if ok else 1
    if a.what == "status":
        if not _alive(state.get("supervisor")):
            print("看守没在跑 (python tools/serve.py run 起它)")
        else:
            up = "在跑" if _alive(state.get("server")) else "没在跑"
            print(f"看守在跑 (进程 {state['supervisor']}); 服务端{up} (进程 {state.get('server')}, 端口 {state.get('port')}, "
                  f"启动编号 {state.get('boot')}, {state.get('started')} 起的; /api/status {'通' if status_ok(state.get('port') or 8790) else '不通'})")
            b, now = B.read(a.home / "beat.json"), time.time()
            if b and b.get("boot") == state.get("boot"):
                loop = f"事件循环 {now - b['loop']:.0f} 秒前转过" if b.get("loop") else "事件循环还没起"
                print(f"心跳: {b.get('phase')}, {now - float(b.get('t') or 0):.0f} 秒前; {loop}")
        jobs = _json(a.home / "canary.json").get("jobs", [])
        print("待验证的合并: " + (", ".join(j["job"] for j in jobs) if jobs else "没有"))
        crashes = sorted(a.home.glob("crash-*.json"))
        if crashes:
            last = _json(crashes[-1])
            print(f"上一次崩溃: {last.get('at')} {last.get('reason')} ({crashes[-1]})")
        return 0
    if not _alive(state.get("supervisor")):
        print("看守没在跑 (python tools/serve.py run 起它)")
        return 1
    if a.what == "restart" and not a.no_trial:
        ok, text = trial()
        print(text)
        if ok is False:
            print("试跑没过, 没重启 (确定要重启: --no-trial)")
            return 1
    (a.home / a.what).write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    print(f"已经请看守{'重启服务端' if a.what == 'restart' else '停掉服务端'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
