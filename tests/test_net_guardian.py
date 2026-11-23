"""网络插件·干跑 (2/3): 看护方 (wm/net/guardian.py) 和它用的 Windows 调用 (wm/net/win32.py).

分五块:
1. 状态机 GuardianState: 假时钟, 假的"核心活着吗/网卡在不在/清缓存", 不碰系统. 心跳、时限、停、核心自己退、关机、matches、net.status 的字段、
   net.restore 的三步报告 (还开着 / 已经没了 / 从没开过, 重复发结果一样), 演练、参数错误、E_BUSY.
   (第 2 轮评审补: 审计行带 core_exit_code; 看护方自己的故障 guardian_error.)
2. 外壳的主循环 (进程内): 真的线程和队列, 假的世界; 小宽限的真时间. 读核心输出的线程出错 -> 按故障收尾 (guardian_error); 读到头不算故障.
3. 会话通道 (进程内): 真的命名管道和 PipeServer, 假的世界. 一问一答、粘包拆包、超长、断开再连、一次只服务一个连接、访问控制、
   不说话的连接按 idle_s 断开 (第 2 轮评审补; 真进程里默认 5 秒的那条在第 5 块).
4. win32.py: 真的作业对象 (句柄关掉进程跟着没)、句柄继承 (只列出的句柄被继承: 子进程里问"是不是作业对象"来认)、命名管道、网卡 (只读)、清 DNS 缓存 (底层调用
   换成假的)、按种类认句柄 (作业对象、进程、管道、进程在不在作业里)、读作业对象的两项设置 (关句柄就结束里面所有进程要开着、允许脱离作业的两项要关着);
   guardian.main 启动时按种类核对三个句柄和作业对象的两项设置, 不对就 (作业句柄是真的就先结束作业) 退出码 2; 再自查是不是提权的 (第 3 轮评审补: 是 -> 结束
   作业、退出码 4; 查不出来 -> 照常跑, stderr 记一笔; 测试不用提权, 把自查换成返回真); 没给 --audit 时审计的默认位置 (用假的家目录测: 测试里绝对不许往真的家目录写);
   进程的创建时间 (进程号会被重用, 创建时间不会)、结束一个进程 (要有"能结束"这项权利)、等进程退出、问管道对面服务端的进程号 (第 4 轮: 给单独起的 net.restore 认核心、
   给以后的门面找看护方用).
5. 真进程: 假助手起假核心加看护方进程, 对真的会话通道发请求 (状态、matches、各种错误、断开再连、一次一个连接). 真进程的停、恢复、心跳断、核心退了、
   看护方没了核心跟着没, 在 tests/test_net_contract.py.

**这一步里任何时候都不许真的清系统的 DNS 缓存**: 每条测试开头把 win32._dns_flush_resolver_cache 换成一个一调用就失败的函数 (要测 flush_dns 的测试自己再换成假的);
看护方进程只用 --fake-adapter-marker 加 --fake-flush-log 起 (假助手里写死了; 最后一条测试扫源码核对).
"""
from __future__ import annotations

import ast
import ctypes
import hashlib
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from net_fake_helper import (NO_WINDOW, ProcessLedger, Rig, channel, covers_guardian, request_line, skip_unrelated_autouse_fixtures, start_line,
                             wait_for)

from wm.net import guardian, protocol, win32
from wm.net.guardian import (CheckAdapter, Exit, FlushDns, GuardianState, KillCore, Reply, WriteAudit, parse_wmfake_line)
from wm.net.protocol import Request
from wm.net.recipe import Recipe, Upstream

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="看护方和 win32 调用只在 Windows 上测")
skip_unrelated_autouse_fixtures(globals())      # 盖掉 conftest.py 里和网络无关的 autouse 夹具 (见 net_fake_helper.py)

T0 = 1_800_000_000.0          # 整数的秒, 配 0.5 的步长: 加减都是精确的二进制小数, 不会有浮点误差
SESSION = "cd" * 16
STATUS_KEYS = {"state", "session", "started_at", "deadline_at", "core_alive", "adapter_present", "matches", "last_stop", "last_error"}
RECIPE = Recipe("NIKKE", ("nikke.exe",), Upstream("127.0.0.1", 1082), 240)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _never_flush_the_real_dns_cache(monkeypatch):
    """这一步里任何时候都不许真的清系统的 DNS 缓存: 底层调用换成一调用就让测试失败的函数 (测 flush_dns 的那条再换成假的)."""
    def refuse() -> bool:
        raise AssertionError("测试里不许真的清系统的 DNS 缓存")

    monkeypatch.setattr(win32, "_dns_flush_resolver_cache", refuse)


@pytest.fixture(autouse=True)
def fake_home(monkeypatch, tmp_path) -> Path:
    """测试里绝对不许真的往家目录写 (看护方没给 --audit 时会写 ~/.worldmodel/serve/net/guardian-audit.jsonl): 每条测试里 Path.home() 都换成一个临时的假家目录
    (这个目录本身不建, 谁往里写了才有); 起看护方进程一律带 --audit (假助手里写死, 它还把进程的 USERPROFILE 指到 data/fake-home)."""
    home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def ledger():
    ledger = ProcessLedger()
    yield ledger
    ledger.kill_all()
    assert not ledger.alive()


def sleeper(ledger: ProcessLedger) -> subprocess.Popen:
    return ledger.add(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW))


# ====================================================================================================================
# 0. 假核心自己 (tests/net_fake_core.py): 它是下面所有真进程测试的地基, 先验它
# ====================================================================================================================

FAKE_CORE = Path(__file__).resolve().parent / "net_fake_core.py"


def run_fake_core(tmp_path: Path, ledger: ProcessLedger, *, behavior: dict | None, env: dict | None = None, extra: tuple = ()) -> tuple:
    """起假核心 (命令行就是 -d 工作目录 -f 配置文件), 返回 (进程, 工作目录, 配置文件字节)."""
    work = tmp_path / "work"
    work.mkdir()
    config = work / "config.yaml"
    config.write_bytes('{"mode":"rule","name":"原神"}'.encode("utf-8"))
    if behavior is not None:
        (work / "fake-behavior.json").write_text(json.dumps(behavior), encoding="utf-8")
    root = win32.windows_directory()
    proc = ledger.add(subprocess.Popen([sys.executable, str(FAKE_CORE), "-d", str(work), "-f", str(config), *extra],
                                       env=env or {"SystemRoot": root, "windir": root, "WM_TEST_ONE": "x", "wm_test_two": "y"},
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW))
    return proc, work, config.read_bytes()


def test_fake_core_writes_its_environment_names_and_the_config_hash_then_builds_the_adapter_and_prints_wmfake_lines(tmp_path, ledger):
    behavior = {"adapter_delay_s": 0.1, "exit_after_s": 1.5, "exit_code": 5, "matches": {"nikke.exe": 2, "原神.exe": 1}, "direct": {"nikke.exe": 1},
                "startup_lines": ["WMFAKE hello"]}
    proc, work, config = run_fake_core(tmp_path, ledger, behavior=behavior)
    marker = work / "fake-adapter-WM-Ayane"
    wait_for(marker.exists, 60, "假核心建出了\"网卡\"标记")                  # 活着的时候看得到 (别的进程读不了它的内容, 所以只看在不在)
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 5 and err == b""                           # 到点自己退出, 退出码照 behavior
    assert json.loads((work / "fake-env.json").read_text(encoding="utf-8")) == sorted(["SYSTEMROOT", "WINDIR", "WM_TEST_ONE", "WM_TEST_TWO"])   # 只有名字 (没有值)
    assert "x" not in (work / "fake-env.json").read_text(encoding="utf-8").replace("WM_TEST_ONE", "")
    assert json.loads((work / "fake-config.json").read_text(encoding="utf-8")) == {"sha256": hashlib.sha256(config).hexdigest(), "size": len(config)}
    assert not marker.exists()                                           # 进程一退 (这里是自己退的) 标记就没了: 关句柄就删
    lines = out.decode("utf-8").splitlines()
    assert lines == ["WMFAKE core started", "WMFAKE hello", "WMFAKE adapter up", "WMFAKE match process=nikke.exe via=upstream",
                     "WMFAKE match process=nikke.exe via=upstream", "WMFAKE match process=原神.exe via=upstream",
                     "WMFAKE match process=nikke.exe via=direct", "WMFAKE core exiting"]


def test_the_fake_adapter_marker_goes_away_with_the_process_even_when_it_is_killed(tmp_path, ledger):
    """真的网卡是核心进程持有的: 进程没了 (哪怕被强杀) 网卡就没了. 假的"网卡"标记用"关句柄就删"的方式建, 所以强杀以后标记 2 秒内消失; 不用去查进程号."""
    proc, work, _ = run_fake_core(tmp_path, ledger, behavior=None)
    marker = work / "fake-adapter-WM-Ayane"
    wait_for(marker.exists, 60, "假核心建出了\"网卡\"标记")
    assert proc.poll() is None
    proc.kill()
    wait_for(lambda: not marker.exists(), 2.0, "核心被强杀以后标记 2 秒内消失")
    proc.communicate(timeout=30)


def test_fake_core_defaults_and_the_never_adapter_knob(tmp_path, ledger):
    proc, work, _ = run_fake_core(tmp_path, ledger, behavior=None)       # 没有 fake-behavior.json: 默认 0.2 秒后建网卡, 不自己退出
    wait_for(lambda: (work / "fake-adapter-WM-Ayane").exists(), 60, "默认的假核心建出了网卡标记")
    assert proc.poll() is None
    proc.kill()
    proc.communicate(timeout=30)
    second = tmp_path / "second"
    second.mkdir()
    proc, work, _ = run_fake_core(second, ledger, behavior={"never_adapter": True, "exit_after_s": 1.0})
    proc.communicate(timeout=120)
    assert proc.returncode == 0 and not (work / "fake-adapter-WM-Ayane").exists()


def test_fake_core_refuses_any_other_arguments_and_a_missing_config(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    config = work / "config.yaml"
    config.write_bytes(b"{}")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(FAKE_CORE), *args], capture_output=True, timeout=60, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)

    for extra in (("--extra",), ("-x", "1"), ("extra",)):                 # 命令行只有 -d 和 -f 两项, 别的一律报错退出, 什么也不做
        assert run("-d", str(work), "-f", str(config), *extra).returncode == 2, extra
    assert run("-d", str(work)).returncode == 2                           # 少一项也不行
    assert not (work / "fake-env.json").exists() and not (work / "fake-adapter-WM-Ayane").exists()
    done = run("-d", str(work), "-f", str(work / "no-such-file"))         # 配置文件读不到: 报错退出, 不建网卡
    assert done.returncode == 2 and b"WMFAKE error" in done.stdout and not (work / "fake-adapter-WM-Ayane").exists()


# ====================================================================================================================
# 1. 状态机 (假时钟)
# ====================================================================================================================

class Sim:
    """把状态机接到假世界上, 像外壳那样做动作、回报结果. 世界的状态: 核心活着吗、网卡在不在 (核心被杀以后过 adapter_gone_after 秒才消失;
    None = 一直不消失)、杀得成不成、清缓存成不成. 时间自己拨 (advance), 默认 0.5 秒一格."""

    def __init__(self, *, session: str | None = SESSION, processes=("nikke.exe",), grace_s: float = 30, deadline_in: float = 3600.0,
                 adapter_gone_after: float | None = 0.0, **machine_kwargs) -> None:
        self.t = T0
        self.machine = GuardianState(now=T0, session=session, deadline_at=T0 + deadline_in if session else None, interval_s=5,
                                     grace_s=grace_s, processes=processes, **machine_kwargs)
        self.core_alive = session is not None
        self.adapter = session is not None
        self.adapter_gone_after = adapter_gone_after
        self.orphan_gone_at: float | None = None     # 没有会话时网卡本来就在 (别人留下的), 到这个时刻消失
        self.kill_ok = True
        self.flush_ok = True
        self.killed_at: float | None = None
        self.log: list[str] = []
        self.replies: list[tuple[object, dict]] = []
        self.audits: list[dict] = []
        self.exit_code: int | None = None
        self.flushes = 0

    def adapter_now(self) -> bool:
        if self.orphan_gone_at is not None:
            return self.t < self.orphan_gone_at
        if self.killed_at is None:
            return self.adapter
        if not self.kill_ok or self.adapter_gone_after is None:
            return True
        return self.t < self.killed_at + self.adapter_gone_after

    def run(self, actions: list) -> Sim:
        pending = deque(actions)
        while pending:
            action = pending.popleft()
            if isinstance(action, Reply):
                self.log.append("reply")
                self.replies.append((action.conn, action.message))
            elif isinstance(action, KillCore):
                self.log.append("kill")
                self.killed_at = self.t
                if self.kill_ok:
                    self.core_alive = False
                pending.extend(self.machine.killed(self.t, self.kill_ok))
            elif isinstance(action, CheckAdapter):
                self.log.append("check")
                pending.extend(self.machine.adapter_seen(self.t, action.tag, self.adapter_now()))
            elif isinstance(action, FlushDns):
                self.log.append("flush")
                self.flushes += 1
                pending.extend(self.machine.dns_flushed(self.t, self.flush_ok))
            elif isinstance(action, WriteAudit):
                self.log.append("audit")
                self.audits.append(action.record)
            elif isinstance(action, Exit):
                self.log.append("exit")
                self.exit_code = action.code
            else:
                raise AssertionError(f"不认识的动作 {action!r}")
        return self

    def send(self, action: str, params: dict | None = None, *, rid: str = "q1", dry_run: bool = False, conn: object = None,
             raw: str | None = None) -> dict | None:
        """发一个请求, 把动作做完; 返回给它的应答 (停的流程还没做完就是 None). params 写成 params_json; raw 直接当 params_json 用."""
        conn = conn if conn is not None else object()
        text = raw if raw is not None else (None if params is None else json.dumps(params))
        self.run(self.machine.request(self.t, Request(rid, action, dry_run, None, text), conn))
        return self.reply_for(conn)

    def reply_for(self, conn: object) -> dict | None:
        return next((message for who, message in self.replies if who is conn), None)

    def advance(self, seconds: float, step: float = 0.5) -> Sim:
        end = self.t + seconds
        while self.t < end:
            self.t = min(self.t + step, end)
            self.run(self.machine.tick(self.t))
        return self


def stop_params(reason: str = "user") -> dict:
    return {"session": SESSION, "reason": reason}


def code_of(reply: dict | None) -> str:
    assert reply is not None and reply["ok"] is False, reply
    return reply["error"]["code"]


def test_with_no_session_stop_and_status_say_idle_and_time_does_nothing():
    sim = Sim(session=None, processes=())
    assert sim.machine.state == "idle"
    assert sim.send("heartbeat")["result"] == {"state": "idle"}
    for _ in range(3):                                                   # 没有会话在跑: 不算错, 可以重复发
        assert sim.send("net.stop", stop_params(), rid="s") == {"v": 1, "id": "s", "ok": True, "result": {"state": "idle"}}
    assert sim.send("net.status")["result"] == {
        "state": "idle", "session": None, "started_at": None, "deadline_at": None, "core_alive": False, "adapter_present": False,
        "matches": {}, "last_stop": None, "last_error": None}
    sim.advance(10_000)
    assert sim.machine.state == "idle" and sim.exit_code is None and "kill" not in sim.log


def test_running_status_has_exactly_the_contract_fields():
    sim = Sim(processes=("nikke.exe", "原神.exe"))
    status = sim.send("net.status")["result"]
    assert set(status) == STATUS_KEYS
    assert status == {"state": "running", "session": SESSION, "started_at": int(T0), "deadline_at": int(T0) + 3600, "core_alive": True,
                      "adapter_present": True, "matches": {"nikke.exe": 0, "原神.exe": 0}, "last_stop": None, "last_error": None}
    sim.adapter = False                                                  # 核心还活着, 网卡却没了: 如实报
    assert sim.send("net.status")["result"]["adapter_present"] is False


def test_heartbeats_keep_the_session_and_more_than_the_grace_of_silence_stops_it():
    sim = Sim(grace_s=30)
    for _ in range(5):
        sim.advance(25)
        assert sim.send("heartbeat", rid="hb")["result"] == {"state": "running"}
    assert sim.machine.state == "running" and sim.exit_code is None
    sim.advance(30)                                                      # 恰好 grace_s 秒没有心跳: 还没"超过"
    assert sim.machine.state == "running"
    sim.advance(0.5)
    assert sim.machine.state == "stopped" and sim.exit_code == 0
    assert sim.log[sim.log.index("kill"):] == ["kill", "check", "flush", "audit", "exit"]       # 没有人在等应答
    assert sim.audits == [{"session": SESSION, "started_at": int(T0), "stopped_at": int(sim.t), "reason": "heartbeat_lost",
                           "state": "stopped", "adapter_gone": True, "dns_flushed": True, "core_exit_code": None, "matches": {"nikke.exe": 0}}]
    assert sim.flushes == 1 and sim.machine.last_stop == {"reason": "heartbeat_lost", "stopped_at": int(sim.t)}


def test_reaching_the_deadline_stops_the_session_with_max_time():
    sim = Sim(deadline_in=100, grace_s=300)
    sim.advance(99.5)
    assert sim.machine.state == "running"
    sim.advance(0.5)                                                     # now == deadline_at
    assert sim.machine.state == "stopped" and sim.exit_code == 0
    assert sim.audits[-1]["reason"] == "max_time" and sim.audits[-1]["stopped_at"] == int(T0) + 100 and sim.flushes == 1


def test_the_core_exiting_on_its_own_is_a_failed_session_and_the_reason_is_recorded():
    sim = Sim()
    sim.core_alive = False
    sim.run(sim.machine.core_exited(sim.t, 3))
    assert sim.machine.state == "failed" and sim.exit_code == 0
    assert sim.log == ["kill", "check", "flush", "audit", "exit"]         # 确认网卡没了、清 DNS 缓存 (顺手把作业里可能剩下的也清了)
    assert sim.audits == [{"session": SESSION, "started_at": int(T0), "stopped_at": int(T0), "reason": "core_exited", "state": "failed",
                           "adapter_gone": True, "dns_flushed": True, "core_exit_code": 3, "matches": {"nikke.exe": 0}}]
    status = sim.send("net.status")["result"]
    assert status["state"] == "failed" and status["core_alive"] is False and status["last_stop"] == {"reason": "core_exited", "stopped_at": int(T0)}
    assert status["last_error"]["code"] == "core_exited" and status["last_error"]["exit_code"] == 3


def test_a_core_exit_is_ignored_while_already_stopping_and_after_the_end():
    sim = Sim(adapter_gone_after=2.0)
    sim.send("net.stop", stop_params(), conn=object())
    assert sim.machine.state == "stopping"
    assert sim.machine.core_exited(sim.t, 1) == []
    sim.advance(2.0)
    assert sim.machine.state == "stopped" and sim.machine.core_exited(sim.t, 1) == []


def test_core_exit_code_in_the_audit_is_null_unless_the_core_exited_on_its_own():
    """审计里的 core_exit_code: 核心自己退的时候是退出码, 别的原因都是 null——哪怕收尾期间外壳发现了核心的退出 (那是我们杀出来的退出码, 不算)."""
    for how in ("stop", "restore", "shutdown", "heartbeat_lost", "max_time", "guardian_error"):
        sim = Sim(adapter_gone_after=1.0, grace_s=300 if how == "max_time" else 30, deadline_in=100 if how == "max_time" else 3600)
        if how == "stop":
            sim.send("net.stop", stop_params(), conn=object())
        elif how == "restore":
            sim.send("net.restore", conn=object())
        elif how == "shutdown":
            sim.run(sim.machine.shutdown(sim.t))
        elif how == "guardian_error":
            sim.run(sim.machine.fault(sim.t, "OSError"))
        else:
            sim.advance(31 if how == "heartbeat_lost" else 100)          # 走到触发的那一格
        assert sim.machine.state == "stopping", how                      # 网卡还要 1 秒才消失
        assert sim.machine.core_exited(sim.t, 1) == []                   # 收尾期间外壳发现核心退了 (我们杀的): 不改收尾, 也不该进审计
        sim.advance(1.5)
        expected = {"stop": "user", "restore": "restore", "shutdown": "system_shutdown", "heartbeat_lost": "heartbeat_lost", "max_time": "max_time",
                    "guardian_error": "guardian_error"}[how]
        assert sim.machine.state in ("stopped", "failed") and sim.audits[0]["reason"] == expected, how
        assert sim.audits[0]["core_exit_code"] is None, how


def test_a_fault_stops_the_session_as_guardian_error_and_records_it():
    """看护方自己出了故障 (读核心输出的线程出了异常): 结束作业 -> 等网卡消失 -> 清缓存 -> 写审计 -> 退出, 状态 failed, 原因和 last_error 的 code 都是 guardian_error."""
    sim = Sim()
    sim.run(sim.machine.fault(sim.t, "OSError"))
    assert sim.machine.state == "failed" and sim.exit_code == 0
    assert sim.log == ["kill", "check", "flush", "audit", "exit"]
    assert sim.audits == [{"session": SESSION, "started_at": int(T0), "stopped_at": int(T0), "reason": "guardian_error", "state": "failed",
                           "adapter_gone": True, "dns_flushed": True, "core_exit_code": None, "matches": {"nikke.exe": 0}}]
    status = sim.send("net.status")["result"]
    assert status["state"] == "failed" and status["last_stop"] == {"reason": "guardian_error", "stopped_at": int(T0)}
    assert status["last_error"]["code"] == "guardian_error" and status["last_error"]["exit_code"] is None and "OSError" in status["last_error"]["message"]
    assert protocol.REASON_GUARDIAN_ERROR == "guardian_error"


def test_a_fault_waits_for_the_adapter_like_any_other_stop_and_is_ignored_when_there_is_nothing_to_stop():
    sim = Sim(adapter_gone_after=3.0)
    sim.run(sim.machine.fault(sim.t, "OSError"))
    assert sim.machine.state == "stopping" and "flush" not in sim.log
    assert sim.machine.fault(sim.t, "OSError") == []                      # 已经在收尾: 不再来一遍
    sim.advance(3.0)
    assert sim.machine.state == "failed" and sim.audits[0]["reason"] == "guardian_error" and sim.audits[0]["adapter_gone"] is True
    assert sim.machine.fault(sim.t, "OSError") == []                      # 已经收完
    assert Sim(session=None).machine.fault(T0, "OSError") == []           # 没有会话


def test_shutdown_stops_a_running_session_and_ends_an_idle_guardian():
    sim = Sim()
    sim.run(sim.machine.shutdown(sim.t))
    assert sim.machine.state == "stopped" and sim.exit_code == 0 and sim.audits[-1]["reason"] == "system_shutdown"
    assert sim.log == ["kill", "check", "flush", "audit", "exit"]
    idle = Sim(session=None)
    assert idle.machine.shutdown(idle.t) == [Exit(0)]
    done = Sim()
    done.run(done.machine.shutdown(done.t))
    assert done.machine.shutdown(done.t) == []                           # 已经停了: 不再有动作


@pytest.mark.parametrize("reason", protocol.STOP_REASONS)
def test_net_stop_for_each_allowed_reason(reason):
    sim = Sim()
    reply = sim.send("net.stop", stop_params(reason), rid="stop-1")
    assert reply == {"v": 1, "id": "stop-1", "ok": True, "result": {
        "session": SESSION, "state": "stopped", "reason": reason, "adapter_gone": True, "dns_flushed": True, "stopped_at": int(T0)}}
    assert sim.log == ["kill", "check", "flush", "reply", "audit", "exit"]   # 结束核心 -> 等网卡消失 -> 清缓存 -> 应答 -> 退出
    assert sim.audits[0]["reason"] == reason and sim.exit_code == 0 and sim.machine.state == "stopped"


@covers_guardian("E_BUSY")
def test_stop_waits_for_the_adapter_to_go_and_answers_busy_in_the_meantime():
    sim = Sim(adapter_gone_after=3.0)
    conn = object()
    assert sim.send("net.stop", stop_params(), conn=conn) is None        # 网卡还在: 应答要等
    assert sim.machine.state == "stopping"
    sim.advance(2.0)
    assert sim.machine.state == "stopping" and "flush" not in sim.log    # 网卡没消失之前不清缓存
    status = sim.send("net.status")["result"]
    assert status["state"] == "stopping" and status["core_alive"] is False and status["adapter_present"] is True
    assert sim.send("heartbeat")["result"] == {"state": "stopping"}
    assert code_of(sim.send("net.stop", stop_params())) == "E_BUSY"
    assert code_of(sim.send("net.restore")) == "E_BUSY"
    assert sim.reply_for(conn) is None
    sim.advance(1.0)
    result = sim.reply_for(conn)["result"]
    assert result["adapter_gone"] is True and result["dns_flushed"] is True and sim.machine.state == "stopped"
    assert sim.log.count("check") >= 6 and sim.log.index("flush") > max(i for i, name in enumerate(sim.log) if name == "check")
    assert sim.exit_code == 0 and sim.flushes == 1


def test_stop_gives_up_waiting_for_the_adapter_after_ten_seconds_but_still_flushes():
    sim = Sim(adapter_gone_after=None)
    conn = object()
    sim.send("net.stop", stop_params(), conn=conn)
    sim.advance(9.5)
    assert sim.machine.state == "stopping" and sim.reply_for(conn) is None
    sim.advance(0.5)
    result = sim.reply_for(conn)["result"]
    assert result["adapter_gone"] is False and result["dns_flushed"] is True and result["state"] == "stopped"
    assert sim.audits[0]["adapter_gone"] is False and sim.flushes == 1 and sim.exit_code == 0


def test_a_failed_dns_flush_is_reported_not_hidden():
    sim = Sim()
    sim.flush_ok = False
    result = sim.send("net.stop", stop_params())["result"]
    assert result["dns_flushed"] is False and result["adapter_gone"] is True and sim.audits[0]["dns_flushed"] is False


def test_the_restore_report_for_a_running_session():
    sim = Sim()
    reply = sim.send("net.restore", rid="rs")
    assert reply == {"v": 1, "id": "rs", "ok": True, "result": {"steps": [
        {"step": "kill_core", "result": "done", "detail": ""}, {"step": "adapter_gone", "result": "done", "detail": ""},
        {"step": "flush_dns", "result": "done", "detail": ""}], "clean": True, "reboot_advised": False}}
    assert sim.machine.state == "stopped" and sim.exit_code == 0 and sim.audits[0]["reason"] == "restore"
    assert sim.log == ["kill", "check", "flush", "reply", "audit", "exit"]


GONE = {"steps": [{"step": "kill_core", "result": "skipped", "detail": ""}, {"step": "adapter_gone", "result": "skipped", "detail": ""},
                  {"step": "flush_dns", "result": "done", "detail": ""}], "clean": True, "reboot_advised": False}


def test_restore_repeated_gives_the_same_result_in_all_three_states():
    """还开着 / 已经没了 / 从没开过: 三种状态下 net.restore 都能跑, 重复跑结果一样 (干净, 不建议重启). 第一次收掉还开着的会话, 之后都是"没有东西可收"的样子."""
    running = Sim()
    first = running.send("net.restore")["result"]
    assert first["clean"] is True and first["reboot_advised"] is False and running.audits[0]["reason"] == "restore"
    flushes = running.flushes
    for _ in range(3):
        assert running.send("net.restore")["result"] == GONE
    assert running.flushes == flushes + 3 and len(running.audits) == 1 and running.log.count("exit") == 1

    never = Sim(session=None)
    for _ in range(3):
        assert never.send("net.restore")["result"] == GONE
    assert never.flushes == 3 and never.audits == [] and never.exit_code is None and never.machine.state == "idle"

    failed = Sim()
    failed.core_alive = False
    failed.run(failed.machine.core_exited(failed.t, 1))
    assert failed.machine.state == "failed"
    for _ in range(2):
        assert failed.send("net.restore")["result"] == GONE
    assert failed.machine.state == "failed" and len(failed.audits) == 1


def test_restore_with_no_session_waits_for_a_leftover_adapter_to_go():
    sim = Sim(session=None)
    sim.adapter = True
    sim.orphan_gone_at = T0 + 2.0
    conn = object()
    assert sim.send("net.restore", conn=conn) is None
    sim.advance(2.0)
    steps = sim.reply_for(conn)["result"]["steps"]
    assert [step["result"] for step in steps] == ["skipped", "done", "done"]      # 没有核心可杀; 网卡本来在, 后来没了: done


def test_restore_that_cannot_clear_the_adapter_says_failed_and_advises_a_reboot():
    for session in (None, SESSION):
        sim = Sim(session=session, adapter_gone_after=None)
        sim.adapter = True
        conn = object()
        sim.send("net.restore", conn=conn)
        sim.advance(10.0)
        result = sim.reply_for(conn)["result"]
        adapter = result["steps"][1]
        assert adapter["result"] == "failed" and "WM-Ayane" in adapter["detail"]
        assert result["clean"] is False and result["reboot_advised"] is True
        assert result["steps"][2]["result"] == "done"                      # 清缓存照样做


def test_restore_when_killing_the_core_fails_is_not_clean():
    sim = Sim()
    sim.kill_ok = False
    conn = object()
    sim.send("net.restore", conn=conn)
    sim.advance(10.0)
    steps = sim.reply_for(conn)["result"]["steps"]
    assert steps[0]["result"] == "failed" and steps[0]["detail"] and sim.reply_for(conn)["result"]["reboot_advised"] is True


def test_restore_with_a_failing_flush_is_not_clean():
    sim = Sim(session=None)
    sim.flush_ok = False
    result = sim.send("net.restore")["result"]
    assert result["steps"][2]["result"] == "failed" and result["clean"] is False and result["reboot_advised"] is True


def test_dry_run_stop_and_restore_only_say_what_they_would_do():
    sim = Sim()
    assert sim.send("net.stop", stop_params(), dry_run=True)["result"] == {"dry_run": True, "would": guardian.WOULD_STOP, "state": "running"}
    assert sim.send("net.restore", dry_run=True)["result"] == {"dry_run": True, "would": guardian.WOULD_RESTORE, "state": "running"}
    assert sim.machine.state == "running" and sim.log.count("kill") == 0 and sim.flushes == 0 and sim.exit_code is None


def test_a_dry_run_heartbeat_does_not_count_as_a_heartbeat():
    sim = Sim(grace_s=30)
    sim.advance(25)
    sim.send("heartbeat", dry_run=True)
    sim.advance(5.5)
    assert sim.machine.state == "stopped" and sim.audits[0]["reason"] == "heartbeat_lost"


@covers_guardian("E_PARAMS")
def test_bad_parameters_are_e_params_naming_the_fields():
    sim = Sim()

    def fields(action: str, params: dict) -> list[str]:
        reply = sim.send(action, params)
        assert code_of(reply) == "E_PARAMS"
        return reply["error"]["fields"]

    assert fields("net.stop", {}) == ["session", "reason"]
    assert fields("net.stop", {"session": SESSION, "reason": "heartbeat_lost"}) == ["reason"]     # 内部原因不许从外面报
    assert fields("net.stop", {"session": "ef" * 16, "reason": "user"}) == ["session"]
    assert fields("net.stop", {"session": SESSION, "reason": "user", "x": 1}) == ["x"]
    assert fields("net.status", {"a": 1}) == ["a"]
    assert fields("heartbeat", {"a": 1}) == ["a"]
    assert fields("net.restore", {"a": 1}) == ["a"]
    for text in ("not json", "[]", '{"session":"%s","reason":"user","reason":"user"}' % SESSION, '{"a":1.5}'):
        assert code_of(sim.send("net.stop", raw=text)) == "E_PARAMS"
    assert code_of(sim.send("net.status", raw="[]")) == "E_PARAMS"
    assert sim.machine.state == "running" and "kill" not in sim.log


@covers_guardian("E_BAD_REQUEST", "E_UNKNOWN_ACTION")
def test_net_start_is_a_bad_request_on_the_session_channel_and_unknown_actions_are_refused():
    sim = Sim()
    reply = sim.send("net.start", {"template": 1}, rid="st")
    assert code_of(reply) == "E_BAD_REQUEST" and reply["id"] == "st"
    assert code_of(sim.send("net.explode")) == "E_UNKNOWN_ACTION"           # decode_request 已经挡过; 状态机自己也兜着
    assert sim.machine.state == "running"
    idle = Sim(session=None)
    assert code_of(idle.send("net.start")) == "E_BAD_REQUEST"


def test_status_requests_are_answered_in_the_order_they_came_in():
    sim = Sim()
    first, second = object(), object()
    for rid, conn in (("a", first), ("b", second)):
        sim.run(sim.machine.request(sim.t, Request(rid, "net.status", False, None, None), conn))
    assert [(who is first, message["id"]) for who, message in sim.replies] == [(True, "a"), (False, "b")]


def test_every_answer_carries_the_id_of_its_request():
    sim = Sim()
    for action in ("heartbeat", "net.status", "net.stop"):
        params = stop_params() if action == "net.stop" else None
        assert sim.send(action, params, rid=f"id-{action}")["id"] == f"id-{action}"
        if sim.exit_code is not None:
            break


def test_after_the_end_the_machine_answers_by_state_and_takes_no_more_actions():
    sim = Sim()
    sim.send("net.stop", stop_params())
    assert sim.send("heartbeat")["result"] == {"state": "stopped"}
    assert sim.send("net.stop", stop_params())["result"] == {"state": "idle"}
    status = sim.send("net.status")["result"]
    assert status["state"] == "stopped" and status["last_stop"]["reason"] == "user" and status["core_alive"] is False
    assert sim.machine.tick(sim.t + 1000) == [] and sim.log.count("exit") == 1


# ---- matches ----------------------------------------------------------------------------------------------------------

def line(name: str, via: str = "upstream") -> str:
    return f"WMFAKE match process={name} via={via}"


def test_matches_count_only_listed_processes_without_regard_to_ascii_case():
    sim = Sim(processes=("Nikke.exe", "原神.exe", "NIKKE.exe", "Wuthering Waves.exe"))
    assert list(sim.machine.matches) == ["Nikke.exe", "原神.exe", "Wuthering Waves.exe"]      # 去重 (只有 ASCII 大小写不分), 写法按第一次出现的
    for text in (line("nikke.exe"), line("NIKKE.EXE"), line("Nikke.exe") + "\r", line("原神.exe"), line("原神.EXE"), line("Wuthering Waves.exe"),
                 line("chrome.exe"), line("nikke.exe", "direct"), "WMFAKE core started", "WMFAKE match process= via=upstream", "", "garbage",
                 line("nikke.exe") + " extra", "wmfake match process=nikke.exe via=upstream"):
        sim.machine.core_line(text)
    assert sim.send("net.status")["result"]["matches"] == {"Nikke.exe": 3, "原神.exe": 2, "Wuthering Waves.exe": 1}


def test_the_line_parser_can_be_swapped_and_a_broken_one_cannot_hurt():
    sim = Sim(parse_line=lambda text: text.split("=", 1)[1] if text.startswith("custom=") else None)
    for text in ("custom=nikke.exe", "custom=other.exe", line("nikke.exe")):
        sim.machine.core_line(text)
    assert sim.machine.matches == {"nikke.exe": 1}

    calls: list[str] = []

    def flaky(text: str):
        calls.append(text)
        if text == "boom":
            raise RuntimeError("坏掉的解析函数")
        return 5 if text == "number" else "nikke.exe"

    sim = Sim(parse_line=flaky)
    for text in ("boom", "number", "ok"):
        sim.machine.core_line(text)                                     # 抛异常的、返回的不是字符串的, 这一行不算, 不影响后面
    assert calls == ["boom", "number", "ok"] and sim.machine.matches == {"nikke.exe": 1}


def test_the_machine_keeps_counts_but_never_the_lines():
    sim = Sim()
    for text in (line("nikke.exe"), "dial tcp 203.0.113.7:443 failed www.example.com", "connect 198.51.100.9:53 via nikke.exe"):
        sim.machine.core_line(text)
    dumped = repr(vars(sim.machine))
    assert "203.0.113" not in dumped and "example.com" not in dumped and "198.51" not in dumped and sim.machine.matches == {"nikke.exe": 1}


def test_wmfake_parser_and_the_parser_registry():
    assert parse_wmfake_line("WMFAKE match process=nikke.exe via=upstream") == "nikke.exe"
    assert parse_wmfake_line("WMFAKE match process=Wuthering Waves.exe via=upstream\n") == "Wuthering Waves.exe"
    for bad in ("WMFAKE match process=nikke.exe via=direct", "match process=nikke.exe via=upstream", "WMFAKE adapter up", ""):
        assert parse_wmfake_line(bad) is None
    assert guardian.LINE_PARSERS[guardian.DEFAULT_LINE_FORMAT] is parse_wmfake_line


def test_console_events_map_to_a_shutdown_reason():
    for event in (win32.CTRL_SHUTDOWN_EVENT, win32.CTRL_LOGOFF_EVENT, win32.CTRL_CLOSE_EVENT, win32.CTRL_C_EVENT, win32.CTRL_BREAK_EVENT):
        assert guardian.shutdown_reason(event) == "system_shutdown"
    assert guardian.shutdown_reason(3) is None and guardian.shutdown_reason(99) is None


# ====================================================================================================================
# 2. 外壳的主循环 (进程内): 真的线程和队列, 假的世界
# ====================================================================================================================

class FakeWorld:
    """假的世界: 杀了作业以后网卡消失、核心"退出"; 清缓存只记次数."""

    def __init__(self) -> None:
        self.adapter = True
        self.exit_code: int | None = None
        self.kills = 0
        self.flushes = 0
        self.kill_ok = True
        self.flush_error = False
        self.hold = False                        # True: 网卡赖着不走 (核心被杀了也还在), 直到测试把它放开
        self.slow_adapter_s = 0.0                # 查一次"网卡在不在"要这么久 (测"等主线程出应答的时间不算空闲")
        self.gone_at: float | None = None

    def world(self) -> guardian.World:
        return guardian.World(adapter_present=self._adapter, flush_dns=self._flush, terminate_job=self._kill,
                              core_exit_code=lambda: self.exit_code)

    def _adapter(self) -> bool:
        if self.slow_adapter_s:
            time.sleep(self.slow_adapter_s)
        return self.adapter and (self.hold or self.gone_at is None or time.monotonic() < self.gone_at)

    def _flush(self) -> bool:
        self.flushes += 1
        if self.flush_error:
            raise OSError("清缓存出错")
        return True

    def _kill(self) -> bool:
        self.kills += 1
        if self.kill_ok:
            self.gone_at = time.monotonic()
            self.exit_code = 1
        return self.kill_ok


def start_guardian(state: GuardianState, fake: FakeWorld, *, audit: Path | None = None, pipe=None, output_fd: int | None = None) -> SimpleNamespace:
    g = guardian.Guardian(state, fake.world(), pipe=pipe, audit=audit, output_fd=output_fd)
    out: list[int] = []
    thread = threading.Thread(target=lambda: out.append(g.run()), daemon=True)
    thread.start()
    return SimpleNamespace(g=g, thread=thread, out=out, fake=fake, state=state)


def finish(running: SimpleNamespace, timeout_s: float = 20.0) -> int:
    running.thread.join(timeout_s)
    assert not running.thread.is_alive(), "外壳的主循环没有在期限内退出"
    return running.out[0]


def audit_lines(path: Path) -> list[dict]:
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


def test_the_shell_loop_stops_on_a_lost_heartbeat_with_a_small_grace(tmp_path):
    now = time.time()
    state = GuardianState(now=now, session=SESSION, deadline_at=now + 3600, grace_s=0.3, processes=("nikke.exe",))
    fake = FakeWorld()
    running = start_guardian(state, fake, audit=tmp_path / "audit.jsonl")
    assert finish(running) == 0
    assert fake.kills == 1 and fake.flushes == 1 and state.state == "stopped"
    [record] = audit_lines(tmp_path / "audit.jsonl")
    assert record["reason"] == "heartbeat_lost" and record["session"] == SESSION and record["adapter_gone"] is True and record["dns_flushed"] is True


def test_the_shell_loop_stops_on_a_shutdown_request_from_another_thread(tmp_path):
    now = time.time()
    state = GuardianState(now=now, session=SESSION, deadline_at=now + 3600, grace_s=300, processes=("nikke.exe",))
    running = start_guardian(state, FakeWorld(), audit=tmp_path / "audit.jsonl")
    running.g.events.put(("lines", [line("nikke.exe"), line("nikke.exe"), line("chrome.exe")]))
    running.g.post_shutdown("system_shutdown")
    assert finish(running) == 0
    [record] = audit_lines(tmp_path / "audit.jsonl")
    assert record["reason"] == "system_shutdown" and record["matches"] == {"nikke.exe": 2}


def test_the_shell_loop_notices_the_core_exiting_and_cleans_up(tmp_path):
    now = time.time()
    state = GuardianState(now=now, session=SESSION, deadline_at=now + 3600, grace_s=300, processes=("nikke.exe",))
    fake = FakeWorld()
    running = start_guardian(state, fake, audit=tmp_path / "audit.jsonl")
    fake.exit_code = 7
    assert finish(running) == 0
    [record] = audit_lines(tmp_path / "audit.jsonl")
    assert record["reason"] == "core_exited" and record["state"] == "failed" and fake.flushes == 1 and state.core_exit_code == 7
    assert record["core_exit_code"] == 7


def test_the_shell_loop_stops_at_the_deadline(tmp_path):
    now = time.time()
    state = GuardianState(now=now, session=SESSION, deadline_at=now + 0.4, grace_s=300, processes=("nikke.exe",))
    running = start_guardian(state, FakeWorld(), audit=tmp_path / "audit.jsonl")
    assert finish(running) == 0
    assert audit_lines(tmp_path / "audit.jsonl")[0]["reason"] == "max_time"


def test_a_failing_flush_call_is_reported_and_the_stop_still_completes(tmp_path):
    now = time.time()
    state = GuardianState(now=now, session=SESSION, deadline_at=now + 3600, grace_s=0.2, processes=("nikke.exe",))
    fake = FakeWorld()
    fake.flush_error = True
    running = start_guardian(state, fake, audit=tmp_path / "audit.jsonl")
    assert finish(running) == 0
    assert audit_lines(tmp_path / "audit.jsonl")[0]["dns_flushed"] is False


def test_pump_output_hands_over_whole_lines_and_reports_a_fault_with_only_the_error_type():
    """读到头 (核心关了输出) 照旧不管; 出了异常不能悄悄没了: 发一个 ("fault", 异常的类型名), 事件里不带异常的内容."""
    events: queue.Queue = queue.Queue()
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"one\ntwo\npartial")
    os.close(write_fd)
    try:
        guardian._pump_output(read_fd, events)                            # 读到头就返回
    finally:
        os.close(read_fd)
    assert events.get_nowait() == ("lines", ["one", "two"]) and events.empty()       # 没有换行结尾的半行不交, 读到头也没有 fault
    broken: queue.Queue = queue.Queue()
    guardian._pump_output(987654, broken)                                  # 不是一个有效的文件描述符: 一读就出错
    assert broken.get_nowait() == ("fault", "OSError") and broken.empty()


def test_a_failing_output_reader_ends_the_session_as_guardian_error(tmp_path):
    """给外壳一个一读就出错的输出端: 读核心输出的线程出了异常, 会话被收掉 (结束作业、等网卡消失、清缓存、写审计、退出), 原因记 guardian_error, 状态 failed."""
    now = time.time()
    state = GuardianState(now=now, session=SESSION, deadline_at=now + 3600, grace_s=300, processes=("nikke.exe",))
    fake = FakeWorld()
    running = start_guardian(state, fake, audit=tmp_path / "audit.jsonl", output_fd=987654)
    assert finish(running) == 0
    [record] = audit_lines(tmp_path / "audit.jsonl")
    assert record["reason"] == "guardian_error" and record["state"] == "failed" and record["core_exit_code"] is None
    assert record["adapter_gone"] is True and record["dns_flushed"] is True and fake.kills == 1 and fake.flushes == 1
    assert state.state == "failed" and state.last_error["code"] == "guardian_error"


def test_the_end_of_the_core_output_is_not_a_fault_and_the_session_goes_on(tmp_path):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (line("nikke.exe") + "\n").encode("utf-8") * 3)
    os.close(write_fd)                                                    # 核心关了输出 (读到头)
    now = time.time()
    state = GuardianState(now=now, session=SESSION, deadline_at=now + 3600, grace_s=300, processes=("nikke.exe",))
    running = start_guardian(state, FakeWorld(), audit=tmp_path / "audit.jsonl", output_fd=read_fd)
    try:
        wait_for(lambda: state.matches["nikke.exe"] == 3, 30, "读到头之前的三行被数到")
        running.thread.join(0.6)
        assert running.thread.is_alive() and state.state == "running"      # 读到头照旧不管: 核心退不退由进程句柄说了算
        running.g.post_shutdown("system_shutdown")
        assert finish(running) == 0
    finally:
        os.close(read_fd)
    assert audit_lines(tmp_path / "audit.jsonl")[0]["reason"] == "system_shutdown"


def test_attempt_treats_a_failing_system_call_as_the_worst_case():
    def boom() -> bool:
        raise OSError("系统调用失败")

    assert guardian.Guardian._attempt(boom, True) is True            # 查网卡查不出来: 按"还在"算
    assert guardian.Guardian._attempt(boom, False) is False          # 清缓存 / 杀作业失败: 按失败算
    assert guardian.Guardian._attempt(lambda: 5, False) is True


@covers_guardian("E_INTERNAL")
def test_an_unexpected_error_inside_the_machine_becomes_e_internal():
    class Broken(GuardianState):
        def request(self, now, req, conn):
            raise RuntimeError("坏了")

    g = guardian.Guardian(Broken(now=T0, session=SESSION), FakeWorld().world(), pipe=None)
    waiter = guardian.Waiter()
    g._handle(("request", waiter, Request("q9", "heartbeat", False, None, None)))
    assert waiter.event.is_set() and waiter.message["id"] == "q9" and waiter.message["error"]["code"] == "E_INTERNAL"


def test_core_lines_reach_the_machine_and_the_audit_is_optional(tmp_path, fake_home):
    state = GuardianState(now=T0, session=SESSION, deadline_at=T0 + 10, processes=("nikke.exe",))
    target = tmp_path / "new-dir" / "deeper" / "audit.jsonl"
    g = guardian.Guardian(state, FakeWorld().world(), pipe=None, audit=target)
    g._handle(("lines", [line("nikke.exe"), "junk"]))
    assert state.matches == {"nikke.exe": 1}
    g._write_audit({"x": 1})                                           # 目录不在就建 (默认位置 ~/.worldmodel/serve/net/ 头一回用时就是不在的)
    assert json.loads(target.read_text(encoding="utf-8")) == {"x": 1}
    blocked = tmp_path / "a-file"
    blocked.write_text("x", encoding="utf-8")
    guardian.Guardian(state, FakeWorld().world(), pipe=None, audit=blocked / "audit.jsonl")._write_audit({"x": 1})   # 上一级是个文件, 写不成: 不抛, 不影响收尾
    quiet = guardian.Guardian(state, FakeWorld().world(), pipe=None)
    quiet._write_audit({"x": 1})                                       # 进程内的 Guardian(audit=None): 仍然是不记 (默认位置只在 main() 里算)
    assert not fake_home.exists()


def test_clock_runs_forward_from_the_real_time():
    clock = guardian.Clock()
    first = clock.now()
    assert abs(first - time.time()) < 5
    second = wait_for(lambda: clock.now() if clock.now() > first else None, 5, "时钟往前走")
    assert second > first


# ====================================================================================================================
# 3. 会话通道 (进程内): 真的命名管道和 PipeServer
# ====================================================================================================================

@pytest.fixture
def chan():
    now = time.time()
    session = secrets.token_hex(16)
    state = GuardianState(now=now, session=session, deadline_at=now + 3600, grace_s=300, processes=("nikke.exe",))
    yield from channel_rig(session, state)


@pytest.fixture
def idle_chan():
    session = secrets.token_hex(16)
    yield from channel_rig(session, GuardianState(now=time.time(), session=None))


def channel_rig(session: str, state: GuardianState, idle_s: float = guardian.IDLE_DISCONNECT_S):
    fake = FakeWorld()
    pipe = guardian.PipeServer(protocol.pipe_name(session), queue.Queue(), idle_s=idle_s)
    running = start_guardian(state, fake, pipe=pipe)
    ns = SimpleNamespace(name=pipe.name, session=session, running=running, fake=fake, state=state, pipe=pipe)
    yield ns
    running.g.post_shutdown("system_shutdown")
    running.thread.join(20)
    pipe.close()
    assert not running.thread.is_alive()
    assert not win32.pipe_exists(protocol.pipe_name(session))


def ask(chan_: SimpleNamespace, action: str, params: dict | None = None, **kwargs) -> dict:
    return channel(chan_.name, action, params, **kwargs)


def test_the_channel_answers_one_request_with_one_line(chan):
    got = ask(chan, "heartbeat", rid="hb-1")
    assert got == {"v": 1, "id": "hb-1", "ok": True, "result": {"state": "running"}}


def test_two_requests_in_one_write_get_two_answers_in_order(chan):
    with win32.PipeClient(chan.name) as client:
        client.send(request_line("heartbeat", rid="one") + request_line("net.status", rid="two", params_json="{}"))
        first, second = json.loads(client.read_line(10)), json.loads(client.read_line(10))
    assert (first["id"], second["id"]) == ("one", "two") and first["result"] == {"state": "running"} and set(second["result"]) == STATUS_KEYS


def test_a_request_that_arrives_in_pieces_is_put_together(chan):
    data = request_line("heartbeat", rid="pieces")
    with win32.PipeClient(chan.name) as client:
        for start in range(0, len(data), 7):
            client.send(data[start:start + 7])
            time.sleep(0.01)
        assert json.loads(client.read_line(10))["id"] == "pieces"


def test_an_oversize_request_is_a_bad_request_and_the_connection_keeps_working(chan):
    with win32.PipeClient(chan.name) as client:
        client.send(b'{"v":1,"id":"big","action":"heartbeat","params_json":"' + b"x" * 100_000 + b'"}\n')
        got = json.loads(client.read_line(20))
        assert got["ok"] is False and got["error"]["code"] == "E_BAD_REQUEST" and got["id"] is None
        assert json.loads(client.request(request_line("heartbeat", rid="after"), 10))["ok"] is True


@covers_guardian("E_VERSION", "E_BAD_REQUEST", "E_UNKNOWN_ACTION", "E_PARAMS")
def test_the_channel_returns_every_error_a_guardian_can_give(chan):
    def code(raw: bytes) -> str:
        return json.loads(win32.pipe_request(chan.name, raw, 10))["error"]["code"]

    assert code(b'{"v":2,"id":"a","action":"heartbeat"}\n') == "E_VERSION"
    assert code(b'{"v":1,"id":"a","action":"net.explode"}\n') == "E_UNKNOWN_ACTION"
    assert code(b"this is not json\n") == "E_BAD_REQUEST"
    assert code(b'{"v":1,"id":"a","action":"heartbeat","x":1}\n') == "E_BAD_REQUEST"
    assert code(request_line("net.start", params_json="{}")) == "E_BAD_REQUEST"
    assert code(request_line("net.stop", params_json='{"session":"x","reason":"bogus"}')) == "E_PARAMS"
    assert code(request_line("net.status", params_json='{"a":1}')) == "E_PARAMS"


def test_connect_disconnect_and_connect_again_still_works(chan):
    for index in range(5):
        assert ask(chan, "heartbeat", rid=f"again-{index}")["ok"] is True
    client = win32.PipeClient(chan.name)
    client.close()                                                     # 连上就断: 通道照常
    client = win32.PipeClient(chan.name)
    client.send(request_line("heartbeat", rid="never-read")[:20])
    client.close()                                                     # 只发了半行就断
    client = win32.PipeClient(chan.name)
    client.send(request_line("heartbeat", rid="no-reader"))
    client.close()                                                     # 发了请求不等应答就断
    assert ask(chan, "heartbeat", rid="alive")["ok"] is True


def test_only_one_connection_is_served_at_a_time(chan):
    first = win32.PipeClient(chan.name)
    try:
        assert json.loads(first.request(request_line("heartbeat", rid="mine"), 10))["ok"] is True
        with pytest.raises(TimeoutError):                              # 这条连接还开着: 别人连不上
            win32.pipe_connect(chan.name, 0.4)
    finally:
        first.close()
    assert ask(chan, "heartbeat", rid="next")["ok"] is True            # 断开以后又能连


def test_status_over_the_channel_counts_core_lines_handed_over_by_the_reader(chan):
    assert ask(chan, "net.status", {})["result"]["matches"] == {"nikke.exe": 0}
    chan.running.g.events.put(("lines", [line("nikke.exe")] * 3 + [line("chrome.exe")]))
    wait_for(lambda: ask(chan, "net.status", {})["result"]["matches"]["nikke.exe"] == 3, 10, "三条连接被数到")
    assert ask(chan, "net.status", {})["result"]["matches"] == {"nikke.exe": 3}


@pytest.fixture
def make_chan():
    """造会话通道 (进程内, 真管道、假世界), 可以指定不说话的连接多久断开 (idle_s); 收尾时都收拾干净."""
    gens: list = []

    def make(*, idle_s: float = guardian.IDLE_DISCONNECT_S) -> SimpleNamespace:
        now = time.time()
        session = secrets.token_hex(16)
        state = GuardianState(now=now, session=session, deadline_at=now + 3600, grace_s=300, processes=("nikke.exe",))
        gen = channel_rig(session, state, idle_s)
        gens.append(gen)
        return next(gen)

    yield make
    for gen in gens:
        try:
            next(gen)
        except StopIteration:
            pass


def client_dropped(client: win32.PipeClient) -> bool:
    """对面 (看护方) 把这条连接断开了吗: 断开以后再 peek 就是 PipeClosed."""
    try:
        win32.pipe_peek(client.handle)
    except win32.PipeClosed:
        return True
    return False


def test_the_idle_disconnect_default_is_five_seconds_and_is_a_parameter_of_the_pipe_server():
    assert guardian.IDLE_DISCONNECT_S == 5.0
    server = guardian.PipeServer(protocol.pipe_name(secrets.token_hex(16)), queue.Queue())
    try:
        assert server.idle_s == 5.0
    finally:
        server.close()
    quick = guardian.PipeServer(protocol.pipe_name(secrets.token_hex(16)), queue.Queue(), idle_s=0.25)
    try:
        assert quick.idle_s == 0.25
    finally:
        quick.close()


def test_a_connection_that_never_sends_a_request_is_dropped_and_the_next_client_gets_in(make_chan):
    chan = make_chan(idle_s=0.4)
    silent = win32.PipeClient(chan.name)
    try:
        started = time.monotonic()
        wait_for(lambda: client_dropped(silent), 30, "只连不说话的连接被看护方断开")
        assert time.monotonic() - started >= 0.3                          # 不是一连上就断: idle_s 是 0.4 秒
    finally:
        silent.close()
    assert ask(chan, "heartbeat")["ok"] is True                           # 断开以后别的客户端连得上


def test_half_a_request_that_stops_arriving_is_dropped_too(make_chan):
    chan = make_chan(idle_s=0.4)
    client = win32.PipeClient(chan.name)
    try:
        client.send(b'{"v":1,"id":"x","action":"hear')                    # 发了半行就不动
        wait_for(lambda: client_dropped(client), 30, "发了半行就不动的连接被断开")
    finally:
        client.close()
    assert ask(chan, "heartbeat")["ok"] is True


def test_data_that_never_makes_a_whole_request_does_not_keep_a_connection_alive(make_chan):
    chan = make_chan(idle_s=0.4)
    client = win32.PipeClient(chan.name)
    try:
        deadline = time.monotonic() + 30
        while not client_dropped(client):                                 # 每 0.1 秒发一个字节, 一直不成一行
            assert time.monotonic() < deadline, "一直灌不成行的数据也该被断开"
            try:
                client.send(b"x")
            except win32.PipeClosed:
                break
            time.sleep(0.1)
    finally:
        client.close()
    assert ask(chan, "heartbeat")["ok"] is True


def test_a_client_that_keeps_sending_whole_requests_is_never_dropped(make_chan):
    chan = make_chan(idle_s=2.0)
    with win32.PipeClient(chan.name) as client:
        started = time.monotonic()
        for index in range(6):
            time.sleep(0.4)                                               # 请求之间隔得比 idle_s 短; 每条应答写完重新计时
            assert json.loads(client.request(request_line("heartbeat", rid=f"keep-{index}"), 20))["ok"] is True
        assert time.monotonic() - started > 2.0                           # 整个过程比 idle_s 长, 连接一直没断


def test_the_time_spent_waiting_for_the_main_thread_does_not_count_as_idle(make_chan):
    chan = make_chan(idle_s=1.0)
    chan.fake.slow_adapter_s = 1.6                                        # net.status 要查一次网卡, 这一查 1.6 秒: 比 idle_s 长
    with win32.PipeClient(chan.name) as client:
        got = json.loads(client.request(request_line("net.status", rid="slow", params_json="{}"), 30))
        assert got["ok"] is True and got["id"] == "slow"                  # 应答等了 1.6 秒, 连接没被断
        chan.fake.slow_adapter_s = 0.0
        assert json.loads(client.request(request_line("heartbeat", rid="after"), 20))["ok"] is True      # 应答写完才重新计时: 紧接着的请求照样答


def test_stop_over_the_channel_answers_then_exits(chan):
    got = ask(chan, "net.stop", {"session": chan.session, "reason": "user"}, rid="bye")
    assert got["ok"] is True and got["id"] == "bye" and got["result"]["state"] == "stopped" and got["result"]["adapter_gone"] is True
    assert finish(chan.running) == 0 and chan.fake.kills == 1 and chan.fake.flushes == 1


@covers_guardian("E_BUSY")
def test_while_a_stop_is_in_progress_the_pipe_answers_busy_status_and_heartbeat_by_state(chan):
    """核心自己退了 -> 看护方开始收尾, 网卡赖着不走 (测试不放开它就一直在): 这段时间里经真的管道发 net.stop / net.restore 都是 E_BUSY,
    net.status 和心跳照实说在停. 放开以后收尾完成, 状态记成 failed. (不靠时间长短: 放开之前它最多等 10 秒, 这几条请求远用不了那么久.)"""
    chan.fake.hold = True
    chan.fake.exit_code = 7
    wait_for(lambda: ask(chan, "net.status", {})["result"]["state"] == "stopping", 10, "状态变成 stopping")
    stop = ask(chan, "net.stop", {"session": chan.session, "reason": "user"})
    assert stop["ok"] is False and stop["error"]["code"] == "E_BUSY"
    assert ask(chan, "net.restore", {})["error"]["code"] == "E_BUSY"
    assert ask(chan, "heartbeat")["result"] == {"state": "stopping"}
    status = ask(chan, "net.status", {})["result"]
    assert status["state"] == "stopping" and status["core_alive"] is False and status["adapter_present"] is True
    chan.fake.hold = False
    assert finish(chan.running) == 0
    assert chan.state.state == "failed" and chan.fake.flushes == 1 and chan.state.last_stop["reason"] == "core_exited"


def test_with_no_session_stop_over_the_channel_says_idle(idle_chan):
    for _ in range(2):
        got = ask(idle_chan, "net.stop", {"session": idle_chan.session, "reason": "user"})
        assert got["ok"] is True and got["result"] == {"state": "idle"}
    assert ask(idle_chan, "net.status", {})["result"]["state"] == "idle"
    assert idle_chan.running.thread.is_alive()


def test_the_guardian_pipe_server_creates_its_pipe_with_the_protected_acl():
    """看护方的 PipeServer 建出来的管道: 只有当前用户和 SYSTEM. (读 DACL 要在服务线程还没启动时读: 线程正堵在这个同步句柄的 ConnectNamedPipe 上时,
    同一个句柄上的别的调用也会排队等, 会一直等下去.)"""
    server = guardian.PipeServer(protocol.pipe_name(secrets.token_hex(16)), queue.Queue())
    try:
        sddl = win32.pipe_dacl_sddl(server.handle)
    finally:
        server.close()
    assert sddl.startswith("D:P")                                     # 受保护的 DACL: 不继承别处的权限
    assert win32.current_user_sid() in sddl and sddl.count("(A;") == 2 and ";;;SY)" in sddl
    for other in ("WD", "AU", "BU", "BA", "AN", "NU", "IU", "AC", "S-1-1-0"):    # Everyone、已认证用户、Users、Administrators、匿名、网络...
        assert f";;;{other})" not in sddl


def test_a_second_pipe_server_cannot_take_the_name_while_the_first_lives(chan):
    with pytest.raises(OSError) as error:
        win32.pipe_server_create(chan.name)
    assert error.value.winerror == win32.ERROR_ACCESS_DENIED


# ====================================================================================================================
# 4. win32.py
# ====================================================================================================================

@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="结构大小按 64 位核对")
def test_job_object_structures_have_the_sizes_windows_expects():
    assert ctypes.sizeof(win32._JobExtendedLimit) == 144 and ctypes.sizeof(win32._JobBasicAccounting) == 48
    assert ctypes.sizeof(win32._JobBasicLimit) == 64 and ctypes.sizeof(win32._SecurityAttributes) == 24


def test_a_job_object_ends_its_processes_when_the_last_handle_closes(ledger):
    job = win32.create_job()
    child = sleeper(ledger)
    win32.assign_process(job, int(child._handle))
    assert win32.job_active_processes(job) == 1
    other = win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS)    # 再留一个句柄
    win32.close_handle(job)
    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(0.5)                                                # 还有句柄在: 进程活着
    started = time.monotonic()
    win32.close_handle(other)                                          # 最后一个句柄关掉
    child.wait(5)
    assert time.monotonic() - started < 2.0


def test_a_limited_job_handle_can_terminate_and_query_but_not_add_processes(ledger):
    job = win32.create_job()
    limited = win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS)
    child = sleeper(ledger)
    try:
        with pytest.raises(OSError) as error:
            win32.assign_process(limited, int(child._handle))
        assert error.value.winerror == win32.ERROR_ACCESS_DENIED
        win32.assign_process(job, int(child._handle))
        assert win32.job_active_processes(limited) == 1
        assert win32.terminate_job(limited, 7) is True
        assert child.wait(5) == 7
        assert win32.job_active_processes(limited) == 0
    finally:
        win32.close_handle(limited)
        win32.close_handle(job)


def test_terminating_with_a_handle_that_is_not_there_returns_false():
    job = win32.create_job()
    win32.close_handle(job)
    assert win32.terminate_job(job) is False


def test_a_job_without_kill_on_close_leaves_the_process_alone(ledger):
    job = win32.create_job(kill_on_close=False)
    child = sleeper(ledger)
    win32.assign_process(job, int(child._handle))
    win32.close_handle(job)
    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(0.5)


def test_handle_helpers_validity_inheritance_and_double_close():
    job = win32.create_job(kill_on_close=False)
    dup = win32.duplicate_handle(job)
    inheritable = win32.duplicate_handle(job, inheritable=True)
    try:
        assert win32.handle_valid(job) and win32.is_inheritable(dup) is False and win32.is_inheritable(inheritable) is True
        win32.set_inheritable(dup, True)
        assert win32.is_inheritable(dup) is True
        win32.set_inheritable(dup, False)
        assert win32.is_inheritable(dup) is False
    finally:
        for handle in (job, dup, inheritable):
            win32.close_handle(handle)
    assert not win32.handle_valid(job) and not win32.is_inheritable(job)
    win32.close_handle(job)                                            # 重复关闭、关 0、关无效值都不抛
    win32.close_handle(0)
    assert win32.handle_valid(0) is False and win32.handle_valid(win32.INVALID_HANDLE_VALUE) is False


def test_kind_checks_tell_jobs_processes_and_pipes_apart(ledger, tmp_path):
    """句柄只是个数字: 按种类 (作业对象、进程、管道) 和关系 (这个进程在不在这个作业里) 认, 不是问"这个数有没有效". 用交接时那种只留部分权利的复制件试."""
    import msvcrt
    inside, outside = sleeper(ledger), sleeper(ledger)
    job, other_job = win32.create_job(kill_on_close=False), win32.create_job(kill_on_close=False)
    win32.assign_process(job, int(inside._handle))
    win32.assign_process(other_job, int(outside._handle))
    job_h = win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS)
    other_job_h = win32.duplicate_handle(other_job, access=win32.JOB_LIMITED_ACCESS)
    inside_h = win32.duplicate_handle(int(inside._handle), access=win32.PROCESS_LIMITED_ACCESS)
    outside_h = win32.duplicate_handle(int(outside._handle), access=win32.PROCESS_LIMITED_ACCESS)
    reader = ledger.add(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW))
    pipe_h = msvcrt.get_osfhandle(reader.stdout.fileno())
    try:
        with open(tmp_path / "plain.txt", "w") as plain:
            file_h = msvcrt.get_osfhandle(plain.fileno())
            others = (0, win32.INVALID_HANDLE_VALUE)
            # 作业对象: 只有作业对象句柄 (带查询权) 是
            assert win32.is_job_object(job_h) and win32.is_job_object(other_job_h)
            assert not any(win32.is_job_object(handle) for handle in (inside_h, pipe_h, file_h, *others))
            # 进程: 只有进程句柄 (带查询权) 是
            assert win32.is_process_handle(inside_h) and win32.is_process_handle(outside_h)
            assert not any(win32.is_process_handle(handle) for handle in (job_h, pipe_h, file_h, *others))
            # 关系: 进程在不在这个作业里
            assert win32.process_in_job(inside_h, job_h) and win32.process_in_job(outside_h, other_job_h)
            assert not win32.process_in_job(outside_h, job_h) and not win32.process_in_job(inside_h, other_job_h)
            assert not win32.process_in_job(job_h, inside_h) and not win32.process_in_job(pipe_h, job_h)       # 种类不对: 调不通就是 False
            # 管道 (匿名管道也是; 普通文件、作业、进程都不是)
            assert win32.is_pipe_handle(pipe_h)
            assert not any(win32.is_pipe_handle(handle) for handle in (job_h, inside_h, file_h, *others))
        # 退了的进程还算在作业里: 核心在看护方起来之前就退了, 照常往下走, 由主循环按 core_exited 收尾
        inside.kill()
        inside.wait(10)
        assert win32.process_in_job(inside_h, job_h) and win32.is_process_handle(inside_h) and win32.process_exit_code(inside_h) == inside.returncode
    finally:
        for handle in (job, other_job, job_h, other_job_h, inside_h, outside_h):
            win32.close_handle(handle)


def test_spawn_inheriting_passes_only_the_listed_handles(ledger):
    """列出的句柄子进程拿得到, 没列出的拿不到. 判断办法是在子进程里问"这个数是不是作业对象": 列出的那个是 (它就是复制出来的作业对象句柄), 没列出的那个
    不是. 以前问的是"这个数在子进程里有没有效", 那不对: 句柄只是个数字, 没列出的那个数字碰巧是子进程自己别的有效句柄时 (实验 40 次里有 14 次), 有效并不
    说明继承到了它, 所以这条测试时过时不过."""
    job = win32.create_job(kill_on_close=False)
    listed = win32.duplicate_handle(job, inheritable=True)
    unlisted = win32.duplicate_handle(job, inheritable=True)           # 也是可继承的, 但没有列出来: 子进程不该拿到
    code = ("import ctypes, sys\n"
            "k = ctypes.WinDLL('kernel32')\n"
            "buf = ctypes.create_string_buffer(48)\n"
            "print(*[int(bool(k.QueryInformationJobObject(ctypes.c_void_p(int(a)), 1, buf, 48, None))) for a in sys.argv[1:]])")
    try:
        proc = ledger.add(win32.spawn_inheriting([sys.executable, "-c", code, str(listed), str(unlisted)], [listed], stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW))
        out, _ = proc.communicate(timeout=60)
        assert out.split() == [b"1", b"0"]
        with pytest.raises(ValueError):
            win32.spawn_inheriting([sys.executable, "-c", "pass"], [])
    finally:
        for handle in (listed, unlisted, job):
            win32.close_handle(handle)


def test_process_helpers_alive_exit_code_and_open(ledger):
    child = sleeper(ledger)
    handle = win32.open_process(child.pid)
    try:
        assert win32.process_alive(child.pid) is True and win32.process_exit_code(handle) is None
        child.kill()
        child.wait(10)
        wait_for(lambda: win32.process_exit_code(handle) is not None, 10, "进程退出码查得到")
        assert win32.process_exit_code(handle) == child.returncode and win32.process_alive(child.pid) is False
    finally:
        win32.close_handle(handle)


def test_exit_code_259_is_not_mistaken_for_still_running(ledger):
    child = ledger.add(subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW))
    handle = win32.open_process(child.pid)
    try:
        child.wait(30)
        assert win32.process_exit_code(handle) == 259
    finally:
        win32.close_handle(handle)


def test_windows_directory_is_the_system_one_without_reading_the_environment(monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("windir", raising=False)
    assert os.path.isdir(win32.windows_directory()) and os.path.exists(os.path.join(win32.windows_directory(), "System32"))


def test_adapter_names_are_read_only_and_adapter_exists_agrees_with_them():
    names = win32.adapter_names()
    assert names and all(isinstance(name, str) for name in names)
    sample = [name for name in names if name][:5]
    assert sample and all(win32.adapter_exists(name) for name in sample)
    assert all(win32.adapter_exists(name.upper()) and win32.adapter_exists(name.lower()) for name in sample if name.isascii())
    assert win32.adapter_exists("no-such-adapter-" + secrets.token_hex(6)) is False


def test_flush_dns_calls_the_underlying_system_call_and_never_raises(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(win32, "_dns_flush_resolver_cache", lambda: calls.append(1) or True)
    assert win32.flush_dns() is True and calls == [1]
    monkeypatch.setattr(win32, "_dns_flush_resolver_cache", lambda: False)
    assert win32.flush_dns() is False

    def no_permission() -> bool:
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(win32, "_dns_flush_resolver_cache", no_permission)
    assert win32.flush_dns() is False


def test_the_real_dns_flush_is_blocked_everywhere_in_this_file():
    with pytest.raises(AssertionError, match="不许真的清"):
        win32.flush_dns()
    with pytest.raises(AssertionError):
        win32._dns_flush_resolver_cache()


def test_the_pipe_is_created_with_the_safe_flags(monkeypatch):
    seen: dict = {}

    def record(name, open_mode, pipe_mode, max_instances, out_size, in_size, timeout_ms, attributes):
        seen.update(name=name, open_mode=open_mode, pipe_mode=pipe_mode, max_instances=max_instances, descriptor=attributes.lpSecurityDescriptor,
                    inherit=attributes.bInheritHandle)
        return 12345

    monkeypatch.setattr(win32, "_create_named_pipe", record)
    name = protocol.pipe_name("ab" * 16)
    assert win32.pipe_server_create(name) == 12345
    assert seen["name"] == name and seen["max_instances"] == 1
    assert seen["open_mode"] & win32.FILE_FLAG_FIRST_PIPE_INSTANCE and seen["open_mode"] & 3 == 3        # 双向, 名字被占了就失败
    assert seen["pipe_mode"] & win32.PIPE_REJECT_REMOTE_CLIENTS and seen["pipe_mode"] & 6 == 0            # 不接受远程; 字节流
    assert seen["descriptor"] and not seen["inherit"]


def test_a_real_pipe_has_a_protected_acl_for_the_user_and_system_only():
    name = protocol.pipe_name(secrets.token_hex(16))
    handle = win32.pipe_server_create(name)
    try:
        sddl = win32.pipe_dacl_sddl(handle)
        assert sddl.startswith("D:P") and sddl.count("(A;") == 2 and win32.current_user_sid() in sddl and ";;;SY)" in sddl
        assert win32.pipe_sddl().startswith("D:P(A;;GRGW;;;") and win32.pipe_sddl().endswith("(A;;GA;;;SY)")
        with pytest.raises(OSError) as error:                          # 只建一个实例: 同名的第二个建不出来
            win32.pipe_server_create(name)
        assert error.value.winerror == win32.ERROR_ACCESS_DENIED
        assert win32.pipe_exists(name) and name.rsplit("\\", 1)[-1] in os.listdir("\\\\.\\pipe")      # 两种办法都看得到 (listdir 慢, 只这一处用)
    finally:
        win32.close_handle(handle)
    assert not win32.pipe_exists(name)


class EchoServer:
    """一个只服务一个连接的小服务端 (win32 的管道函数自己测): mode 是 echo (原样回 echo:<行>)、silent (收了不回)、hangup (收了一行就断开)."""

    def __init__(self, mode: str = "echo") -> None:
        self.name = protocol.pipe_name(secrets.token_hex(16))
        self.handle = win32.pipe_server_create(self.name)
        self.mode = mode
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            win32.pipe_server_connect(self.handle)
            buf = b""
            while True:
                avail = win32.pipe_peek(self.handle)
                if not avail:
                    time.sleep(0.005)
                    continue
                buf += win32.pipe_read(self.handle, avail)
                while b"\n" in buf:
                    text, buf = buf.split(b"\n", 1)
                    if self.mode == "echo":
                        win32.pipe_write(self.handle, b"echo:" + text + b"\n")
                    elif self.mode == "hangup":
                        win32.pipe_server_disconnect(self.handle)
                        return
        except OSError:
            return

    def close(self) -> None:
        """先让服务线程走完, 再关句柄: 线程还在用这个句柄号的时候就关掉的话, 句柄号会被下一个新建的句柄重用, 这条线程就会拿着别人的管道去读
        (曾经因此偶尔偷走下一条测试的数据: 这个测试偶发失败的原因)."""
        self.thread.join(2.0)                                          # 客户端断开以后, 线程下一次 peek 就会发现而自己走了
        if self.thread.is_alive():                                     # 还堵在 ConnectNamedPipe 上 (没有客户端来过): 连一下叫醒它
            try:
                win32.PipeClient(self.name, 0.5).close()
            except OSError:
                pass
            self.thread.join(5.0)
        assert not self.thread.is_alive()
        win32.close_handle(self.handle)


def test_pipe_client_and_server_round_trip_lines():
    server = EchoServer()
    try:
        with win32.PipeClient(server.name, 5) as client:
            assert client.request(b"hello\n", 5) == b"echo:hello"
            client.send(b"a\nb\n")
            assert client.read_line(5) == b"echo:a" and client.read_line(5) == b"echo:b"       # 一次读一行, 多的留着
            blob = b"x" * 50_000                                         # 比管道缓冲区的一半大
            assert client.request(blob + b"\n", 10) == b"echo:" + blob
    finally:
        server.close()


def test_pipe_request_connects_sends_reads_and_disconnects():
    server = EchoServer()
    try:
        assert win32.pipe_request(server.name, b"ping\n", 5) == b"echo:ping"
    finally:
        server.close()


def test_pipe_exists_neither_connects_nor_uses_up_the_instance():
    server = EchoServer()
    try:
        assert win32.pipe_exists(server.name) is True and win32.pipe_exists(server.name) is True
        with win32.PipeClient(server.name, 5) as client:
            assert client.request(b"hi\n", 5) == b"echo:hi"
            assert win32.pipe_exists(server.name) is True                 # 正忙也算有
        assert win32.pipe_exists(protocol.pipe_name(secrets.token_hex(16))) is False
    finally:
        server.close()


def test_pipe_connect_gives_up_after_its_timeout_when_there_is_no_such_pipe():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        win32.pipe_connect(protocol.pipe_name(secrets.token_hex(16)), 0.3)
    assert 0.2 <= time.monotonic() - started < 5


def test_reading_an_answer_times_out_and_a_hung_up_server_is_noticed():
    silent = EchoServer("silent")
    try:
        with win32.PipeClient(silent.name, 5) as client:
            client.send(b"hello\n")
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                client.read_line(0.3)
            assert time.monotonic() - started < 5
    finally:
        silent.close()
    hangup = EchoServer("hangup")
    try:
        with win32.PipeClient(hangup.name, 5) as client:
            client.send(b"hello\n")
            with pytest.raises(win32.PipeClosed):
                client.read_line(10)
        with pytest.raises(win32.PipeClosed):                              # 关了的客户端再用
            client.send(b"x")
    finally:
        hangup.close()


# ====================================================================================================================
# 命令行的参数 (进程内调用 main / build_parser: 出错分支在碰任何东西之前就退出)
# ====================================================================================================================

def good_args(**over: str) -> list[str]:
    args = {"--session": SESSION, "--job": "4000000", "--process": "4000004", "--output": "4000008", "--deadline": "1800000000", "--interval": "5",
            "--grace": "30", "--processes": '["nikke.exe"]'}
    args.update(over)
    return [item for pair in args.items() for item in pair]


def test_the_command_line_parses_a_good_set_of_arguments():
    parsed = guardian.build_parser().parse_args(good_args(**{"--audit": "a.jsonl", "--started": "1799999990", "--fake-flush-log": "f.log"}))
    assert parsed.session == SESSION and parsed.job == 4000000 and parsed.processes == ["nikke.exe"] and parsed.deadline == 1800000000.0
    assert (parsed.interval, parsed.grace, parsed.started) == (5, 30, 1799999990.0) and parsed.line_format == "wmfake"
    assert parsed.audit == Path("a.jsonl") and parsed.fake_adapter_marker is None


@pytest.mark.parametrize("over", [
    {"--session": "xyz"}, {"--session": "CD" * 16}, {"--session": "cd" * 15}, {"--job": "0"}, {"--job": "-4"}, {"--job": "abc"},
    {"--interval": "0"}, {"--interval": "31"}, {"--grace": "9"}, {"--grace": "301"}, {"--grace": "x"}, {"--deadline": "soon"},
    {"--processes": "[]"}, {"--processes": "nikke.exe"}, {"--processes": '["svchost.exe"]'}, {"--processes": '["a.exe",5]'},
    {"--processes": json.dumps([f"g{i}.exe" for i in range(17)])}, {"--line-format": "mihomo"}])
def test_bad_command_line_values_are_refused(over, capsys):
    with pytest.raises(SystemExit) as exit_:
        guardian.build_parser().parse_args(good_args(**over))
    assert exit_.value.code == 2 and capsys.readouterr().err


def test_main_refuses_one_fake_flag_without_the_other(capsys):
    for only in ("--fake-adapter-marker", "--fake-flush-log"):
        with pytest.raises(SystemExit) as exit_:
            guardian.main(good_args(**{only: "x"}))
        assert exit_.value.code == 2 and "一起给" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_:
        guardian.main([])
    assert exit_.value.code == 2


@pytest.fixture
def hand(ledger, tmp_path):
    """一套"交接来的三个句柄" (进程内, 交接时那种只留部分权利的复制件): 在作业里的核心 inside、不在这个作业里的另一个进程 outside、一根真的管道、一个普通文件."""
    import msvcrt
    inside, outside = sleeper(ledger), sleeper(ledger)
    job = win32.create_job()                         # 交接来的作业该有的设置 (关句柄就结束里面所有进程、不许脱离作业): 启动核对会查
    win32.assign_process(job, int(inside._handle))
    reader = ledger.add(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW))
    plain = open(tmp_path / "plain.txt", "w")
    ns = SimpleNamespace(
        inside=inside, outside=outside, tmp=tmp_path, job=job, plain=plain,
        job_h=win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS),
        inside_h=win32.duplicate_handle(int(inside._handle), access=win32.PROCESS_LIMITED_ACCESS),
        outside_h=win32.duplicate_handle(int(outside._handle), access=win32.PROCESS_LIMITED_ACCESS),
        pipe_h=msvcrt.get_osfhandle(reader.stdout.fileno()), file_h=msvcrt.get_osfhandle(plain.fileno()))
    yield ns
    plain.close()
    for handle in (ns.job_h, ns.inside_h, ns.outside_h, ns.job):
        win32.close_handle(handle)


def run_main(argv: list[str]) -> int:
    """在线程里跑 guardian.main, 最多等 60 秒: 句柄核对该拦住的都在启动时就返回 2; 要是哪天没拦住, main 会一直跑下去, 那时让测试失败, 不是挂住."""
    out: list[int] = []
    thread = threading.Thread(target=lambda: out.append(guardian.main(argv)), daemon=True)
    thread.start()
    thread.join(60)
    assert not thread.is_alive(), "guardian.main 没有在启动时就退出: 句柄核对没拦住?"
    return out[0]


def hand_args(hand_: SimpleNamespace, *, job: int, process: int, output: int, **over: str) -> list[str]:
    return good_args(**{"--job": str(job), "--process": str(process), "--output": str(output),
                        "--fake-adapter-marker": str(hand_.tmp / "marker"), "--fake-flush-log": str(hand_.tmp / "flush.log"), **over})


def test_check_handles_accepts_the_right_kinds_and_names_the_first_wrong_one(hand):
    assert guardian.check_handles(hand.job_h, hand.inside_h, hand.pipe_h) == ("", True)
    reason, job_is_real = guardian.check_handles(hand.inside_h, hand.inside_h, hand.pipe_h)       # --job 其实是个进程
    assert "不是作业对象" in reason and job_is_real is False
    reason, job_is_real = guardian.check_handles(hand.job_h, hand.pipe_h, hand.pipe_h)            # --process 其实是个管道
    assert "不是进程句柄" in reason and job_is_real is True
    reason, job_is_real = guardian.check_handles(hand.job_h, hand.outside_h, hand.pipe_h)         # --process 不在这个作业里
    assert "不在" in reason and job_is_real is True
    reason, job_is_real = guardian.check_handles(hand.job_h, hand.inside_h, hand.file_h)          # --output 其实是个普通文件
    assert "不是管道" in reason and job_is_real is True
    assert guardian.check_handles(4000000, 4000004, 4000008)[0] and guardian.check_handles(0, 0, 0)[0]   # 根本不是句柄的数字


def test_main_refuses_a_job_handle_that_is_something_else_and_ends_nothing(hand, capsys):
    code = run_main(hand_args(hand, job=hand.inside_h, process=hand.inside_h, output=hand.pipe_h))
    assert code == 2 and "不是作业对象" in capsys.readouterr().err
    assert hand.inside.poll() is None and hand.outside.poll() is None     # 作业句柄不是真的: 没有什么可结束的
    assert run_main(hand_args(hand, job=4000000, process=4000004, output=4000008)) == 2
    assert "不是作业对象" in capsys.readouterr().err


def test_main_refuses_a_process_that_is_not_in_the_job_and_ends_the_job(hand, capsys):
    code = run_main(hand_args(hand, job=hand.job_h, process=hand.outside_h, output=hand.pipe_h))
    assert code == 2 and "不在" in capsys.readouterr().err
    assert hand.inside.wait(30) is not None                               # 作业句柄是真的: 先把作业里的 (那个核心) 结束, 再退出
    assert hand.outside.poll() is None                                    # 不在这个作业里的那个进程不受影响


def test_main_refuses_an_output_that_is_not_a_pipe_and_ends_the_job(hand, capsys):
    code = run_main(hand_args(hand, job=hand.job_h, process=hand.inside_h, output=hand.file_h))
    assert code == 2 and "不是管道" in capsys.readouterr().err
    assert hand.inside.wait(30) is not None and hand.outside.poll() is None


def test_main_refuses_a_process_handle_that_is_not_a_process_and_ends_the_job(hand, capsys):
    code = run_main(hand_args(hand, job=hand.job_h, process=hand.pipe_h, output=hand.pipe_h))
    assert code == 2 and "不是进程句柄" in capsys.readouterr().err
    assert hand.inside.wait(30) is not None


@pytest.mark.parametrize("kwargs, word", [
    ({"kill_on_close": False}, "KILL_ON_JOB_CLOSE"), ({"breakaway_ok": True}, "BREAKAWAY_OK"), ({"silent_breakaway_ok": True}, "SILENT_BREAKAWAY_OK"),
    ({"kill_on_close": False, "breakaway_ok": True, "silent_breakaway_ok": True}, "KILL_ON_JOB_CLOSE")])
def test_check_handles_refuses_a_job_whose_two_settings_are_wrong(hand, kwargs, word):
    """契约 4.1 第 8 步、第 10 节〔补 2〕: 作业对象要有两项设置——句柄全关掉时结束里面所有进程 (开着)、里面的进程不许脱离作业 (两项允许脱离的标志关着);
    看护方"自己没了核心跟着没"靠的就是这两项."""
    job = win32.create_job(**kwargs)
    job_h = win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS)            # 交接时那种只留结束和查询的复制件
    try:
        reason, job_is_real = guardian.check_handles(job_h, hand.inside_h, hand.pipe_h)
        assert word in reason and job_is_real is True                                # 作业是真的 (只是设置不对): 退出前要先把里面的进程结束掉
    finally:
        win32.close_handle(job_h)
        win32.close_handle(job)


@pytest.mark.parametrize("kwargs", [{"kill_on_close": False}, {"breakaway_ok": True}, {"silent_breakaway_ok": True}])
def test_main_refuses_a_job_with_a_wrong_setting_exits_with_2_and_ends_the_job(hand, capsys, kwargs):
    """用 create_job(kill_on_close=False) 建的作业 (以及带允许脱离标志的作业) 交给它: 退出码 2, 作业里的进程被结束."""
    job = win32.create_job(**kwargs)
    win32.assign_process(job, int(hand.outside._handle))                             # 这个作业里放进一个"核心": 退出前要被结束
    job_h = win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS)
    try:
        code = run_main(hand_args(hand, job=job_h, process=hand.outside_h, output=hand.pipe_h))
        assert code == 2 and "作业" in capsys.readouterr().err
        assert hand.outside.wait(30) is not None                                     # 设置不对的作业: 里面的进程被结束了
        assert hand.inside.poll() is None                                            # 别的作业 (设置对的那个) 里的进程不受影响
    finally:
        win32.close_handle(job_h)
        win32.close_handle(job)


def test_the_good_job_made_by_create_job_passes_the_settings_check_and_the_flags_read_back(hand):
    assert guardian.check_handles(hand.job_h, hand.inside_h, hand.pipe_h) == ("", True)
    assert win32.job_limit_flags(hand.job_h) == win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE         # 恰好这一项, 没有允许脱离的两项


def test_the_job_limit_flags_read_back_whatever_create_job_set(ledger):
    kill, breakaway, silent = (win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, win32.JOB_OBJECT_LIMIT_BREAKAWAY_OK, win32.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)
    assert (kill, breakaway, silent) == (0x2000, 0x0800, 0x1000)
    for kwargs, expected in (({}, kill), ({"kill_on_close": False}, 0), ({"breakaway_ok": True}, kill | breakaway), ({"silent_breakaway_ok": True}, kill | silent),
                             ({"kill_on_close": False, "breakaway_ok": True, "silent_breakaway_ok": True}, breakaway | silent)):
        job = win32.create_job(**kwargs)
        limited = win32.duplicate_handle(job, access=win32.JOB_LIMITED_ACCESS)       # 只带结束和查询的复制件也读得到
        try:
            assert win32.job_limit_flags(job) == expected == win32.job_limit_flags(limited), kwargs
        finally:
            win32.close_handle(limited)
            win32.close_handle(job)
    proc = sleeper(ledger)
    process_h = win32.duplicate_handle(int(proc._handle), access=win32.PROCESS_LIMITED_ACCESS)
    try:
        assert win32.job_limit_flags(process_h) is None and win32.job_limit_flags(0) is None and win32.job_limit_flags(4000000) is None   # 不是作业对象: None
    finally:
        win32.close_handle(process_h)


def test_process_creation_time_is_the_same_for_one_process_and_later_for_a_later_one(ledger):
    """进程号会被重用, 创建时间不会重: (进程号, 创建时间) 两样都对得上, 才认得出是同一个进程 (恢复时按这个认核心, 进程号被别的进程重用了就不动它)."""
    first = sleeper(ledger)
    time.sleep(0.05)
    second = sleeper(ledger)
    handles = [win32.open_process(proc.pid, access=win32.PROCESS_LIMITED_ACCESS) for proc in (first, first, second)]
    try:
        t1, t1_again, t2 = (win32.process_creation_time(handle) for handle in handles)
        assert t1 == t1_again and t2 > t1                                         # 同一个进程两个句柄读到的一样; 后起的进程创建时间更晚
        assert abs((t1 - 116444736000000000) / 10_000_000 - time.time()) < 300   # FILETIME (1601 年起的 100 纳秒) 换成 unix 秒: 就是刚才
        assert win32.process_creation_time(int(first._handle)) == t1             # Popen 自己的句柄也读得到
        with pytest.raises(OSError):
            win32.process_creation_time(4_000_000)                               # 这个数不是句柄
    finally:
        for handle in handles:
            win32.close_handle(handle)


def test_terminate_process_needs_the_terminate_right_and_wait_process_sees_the_end(ledger):
    proc = sleeper(ledger)
    weak = win32.open_process(proc.pid, access=win32.PROCESS_LIMITED_ACCESS)
    strong = win32.open_process(proc.pid, access=win32.PROCESS_KILL_ACCESS)
    try:
        assert win32.PROCESS_KILL_ACCESS == win32.PROCESS_TERMINATE | win32.PROCESS_QUERY_LIMITED_INFORMATION | win32.SYNCHRONIZE
        assert win32.wait_process(strong, 0.1) is False and win32.process_exit_code(strong) is None       # 还活着
        assert win32.terminate_process(weak) is False and proc.poll() is None                            # 没有"能结束"这项权利
        assert win32.terminate_process(strong, 7) is True
        assert win32.wait_process(strong, 30) is True and win32.process_exit_code(strong) == 7           # 退出码是给的那个
        assert win32.wait_process(strong, 0) is True                                                      # 退了以后再等, 马上返回
        assert win32.terminate_process(strong, 9) is False                                                # 已经退了
        assert win32.terminate_process(4_000_000) is False                                                # 这个数不是句柄
    finally:
        win32.close_handle(weak)
        win32.close_handle(strong)
    with pytest.raises(OSError) as raised:
        win32.open_process(4_000_000, access=win32.PROCESS_KILL_ACCESS)
    assert raised.value.winerror == 87                                                                    # 没有这个进程号的进程


def test_main_refuses_to_work_when_the_guardian_is_elevated_ends_the_job_and_exits_with_4(hand, capsys, monkeypatch):
    """契约第 10 节〔补 2〕: 看护方不能是提权的 (它是项目里 Python 写的代码, 用户账号改得动, 提权跑就等于任意代码以管理员身份跑). 测试不用提权: 三个句柄用真的, 把自查换成返回真."""
    monkeypatch.setattr(guardian, "self_elevated", lambda: True)
    code = run_main(hand_args(hand, job=hand.job_h, process=hand.inside_h, output=hand.pipe_h))
    assert code == 4 == guardian.EXIT_ELEVATED
    assert "提权" in capsys.readouterr().err
    assert hand.inside.wait(30) is not None and hand.outside.poll() is None          # 作业被结束了 (里面的核心没了), 别的进程不受影响
    assert not (hand.tmp / "flush.log").exists() and not win32.pipe_exists(protocol.pipe_name(SESSION))   # 一件事也没做: 没清缓存, 也没建会话通道


def test_the_elevation_check_comes_after_the_handle_check(hand, capsys, monkeypatch):
    """先核对句柄 (不对 -> 退出码 2), 再自查是不是提权的 (是 -> 退出码 4): 两样都不对时说的是句柄."""
    monkeypatch.setattr(guardian, "self_elevated", lambda: True)
    assert run_main(hand_args(hand, job=hand.inside_h, process=hand.inside_h, output=hand.pipe_h)) == 2
    err = capsys.readouterr().err
    assert "不是作业对象" in err and "提权" not in err


def test_check_not_elevated_says_what_to_do_for_each_answer(monkeypatch):
    for answer in (True, False, None):
        monkeypatch.setattr(guardian, "self_elevated", lambda a=answer: a)
        refuse, note = guardian.check_not_elevated()
        assert refuse is (answer is True)
        assert bool(note) is (answer is not False)                                    # 不是提权的: 什么都不写; 提权的: 写原因; 查不出来: 记一笔
        assert not note or note.startswith("wm.net.guardian:")
        if answer is True:
            assert "提权" in note and "不工作" in note
        if answer is None:
            assert "查不出" in note and "照常运行" in note


def test_the_real_self_check_answers_with_a_bool_or_none_and_swallows_whatever_goes_wrong_in_wm_privilege(monkeypatch):
    assert guardian.self_elevated() in (True, False, None)                           # 真的去问 (只读自己进程的令牌), 不提权也不改任何东西
    wm_package = sys.modules["wm"]

    class Boom:
        @staticmethod
        def is_elevated():
            raise OSError("坏了")

    monkeypatch.setattr(wm_package, "privilege", Boom, raising=False)
    assert guardian.self_elevated() is None                                          # 它抛了异常: 当查不出来
    for answer, expected in ((True, True), (False, False), (None, None), ("yes", None), (1, None), (0, None)):
        monkeypatch.setattr(wm_package, "privilege", SimpleNamespace(is_elevated=lambda a=answer: a), raising=False)
        assert guardian.self_elevated() is expected, answer                          # 只认布尔


def test_main_carries_on_when_the_self_check_cannot_tell_and_writes_the_default_audit_under_the_home_directory(hand, capsys, monkeypatch, fake_home):
    """查不出来 (None) 照常跑, 往 stderr 记一笔; 没给 --audit 时审计写到 <家目录>/.worldmodel/serve/net/guardian-audit.jsonl (这里 Path.home 是假的家目录; 目录原本不在,
    要自己建). 时限设在过去, 一进主循环就按 max_time 收尾."""
    monkeypatch.setattr(guardian, "self_elevated", lambda: None)
    monkeypatch.setattr(win32, "set_console_ctrl_handler", lambda handler: None)      # 不往 pytest 进程里登记控制台事件的回调 (它会吞掉 Ctrl+C)
    session = secrets.token_hex(16)
    output = win32.duplicate_handle(hand.pipe_h)                                     # main 会拿它去开文件描述符 (读核心输出): 用复制件, 不动 hand 的原句柄
    assert not fake_home.exists()
    code = run_main(hand_args(hand, job=hand.job_h, process=hand.inside_h, output=output, **{"--session": session, "--deadline": "1"}))
    assert code == 0
    assert "查不出自己是不是提权的" in capsys.readouterr().err
    audit = guardian.default_audit_path(fake_home)
    [record] = [json.loads(text) for text in audit.read_text(encoding="utf-8").splitlines()]
    assert audit == fake_home / ".worldmodel" / "serve" / "net" / "guardian-audit.jsonl"
    assert record["session"] == session and record["reason"] == "max_time" and record["state"] == "stopped"
    assert hand.inside.wait(30) is not None                                          # 时限到了: 作业被结束
    assert (hand.tmp / "flush.log").read_text(encoding="utf-8").count("flush") == 1
    assert not win32.pipe_exists(protocol.pipe_name(session))                        # 收尾以后会话通道没了


def test_main_without_a_home_directory_goes_on_without_an_audit_and_says_so(hand, capsys, monkeypatch, fake_home):
    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(guardian, "self_elevated", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(no_home))
    monkeypatch.setattr(win32, "set_console_ctrl_handler", lambda handler: None)
    output = win32.duplicate_handle(hand.pipe_h)
    assert run_main(hand_args(hand, job=hand.job_h, process=hand.inside_h, output=output, **{"--session": secrets.token_hex(16), "--deadline": "1"})) == 0
    assert "找不到家目录" in capsys.readouterr().err and not fake_home.exists()      # 这次不记审计, 收尾照常


def test_the_default_audit_location_is_under_the_worldmodel_serve_net_folder_of_the_home_directory(tmp_path, fake_home):
    expected = Path(".worldmodel") / "serve" / "net" / "guardian-audit.jsonl"
    assert guardian.default_audit_path(tmp_path) == tmp_path / expected == guardian.default_audit_path(str(tmp_path))
    assert guardian.default_audit_path() == fake_home / expected                     # 不给家目录: 当前用户的 (这里 Path.home 换成了假的)
    assert not fake_home.exists() and not (tmp_path / ".worldmodel").exists()        # 只算位置, 不碰磁盘


def test_the_module_can_be_run_as_a_program_and_refuses_bad_arguments_fast(tmp_path, fake_home):
    """(起看护方进程一律带 --audit, 进程的家目录也换成假的: 就算参数检查哪天不先退出了, 也不会写进真的家目录.)"""
    env = {**os.environ, "USERPROFILE": str(fake_home), "HOME": str(fake_home)}
    done = subprocess.run([sys.executable, "-m", "wm.net.guardian", "--session", "nope", "--audit", str(tmp_path / "audit.jsonl")], cwd=str(ROOT), env=env,
                          capture_output=True, timeout=120, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    assert done.returncode == 2 and b"--session" in done.stderr


# ====================================================================================================================
# 5. 真进程: 假助手起假核心加看护方进程, 对真的会话通道发请求
# ====================================================================================================================

LIVE_BEHAVIOR = {"matches": {"nikke.exe": 3, "chrome.exe": 2}, "direct": {"nikke.exe": 4}, "repeat": {"process": "nikke.exe", "every_s": 0.2}}


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """一段真的会话 (看护方是真进程, 核心是假核心), 给只读的测试共用. 宽限开到最大, 这个文件跑多久都不会因为没心跳而停."""
    rig = Rig(tmp_path_factory.mktemp("live"), behavior=LIVE_BEHAVIOR)
    reply = rig.send(start_line(RECIPE, now=rig.clock(), interval_s=30, grace_s=300))
    assert reply["ok"], reply
    yield SimpleNamespace(rig=rig, start=reply["result"], session=rig.helper.session, pipe=rig.helper.session.pipe)
    rig.close()
    assert not rig.ledger.alive() and not rig.leftover_pipes()


def test_the_real_guardian_reports_the_status_fields_of_the_contract(live):
    status = channel(live.pipe, "net.status", {})["result"]
    assert set(status) == STATUS_KEYS
    assert status["state"] == "running" and status["session"] == live.start["session"]
    assert status["started_at"] == live.start["started_at"] and status["deadline_at"] == live.start["deadline_at"]
    assert status["core_alive"] is True and status["adapter_present"] is True
    assert status["last_stop"] is None and status["last_error"] is None and isinstance(status["matches"], dict)


def test_the_real_guardian_counts_only_the_listed_process_and_keeps_counting(live):
    wait_for(lambda: channel(live.pipe, "net.status", {})["result"]["matches"].get("nikke.exe", 0) >= 3, 30, "头一批连接被数到")
    matches = channel(live.pipe, "net.status", {})["result"]["matches"]
    assert set(matches) == {"nikke.exe"}                                  # 没列的进程 (chrome.exe) 不出现; 直连的行不数
    before = matches["nikke.exe"]
    wait_for(lambda: channel(live.pipe, "net.status", {})["result"]["matches"]["nikke.exe"] > before, 30, "核心后来又打印的行也被数到")


@covers_guardian("E_VERSION", "E_BAD_REQUEST", "E_UNKNOWN_ACTION", "E_PARAMS")
def test_the_real_channel_gives_the_errors_a_guardian_can_give_and_keeps_serving(live):
    def code(raw: bytes) -> str:
        return json.loads(win32.pipe_request(live.pipe, raw, 20))["error"]["code"]

    assert code(b'{"v":2,"id":"a","action":"heartbeat"}\n') == "E_VERSION"
    assert code(b'{"v":1,"id":"a","action":"net.explode"}\n') == "E_UNKNOWN_ACTION"
    assert code(b'{"v":1,"id":"a","action":"heartbeat","extra":1}\n') == "E_BAD_REQUEST"
    assert code(request_line("net.start", params_json="{}")) == "E_BAD_REQUEST"                      # 会话通道上不认 net.start
    assert code(request_line("net.stop", params_json='{"session":"%s","reason":"bogus"}' % live.start["session"])) == "E_PARAMS"
    assert code(request_line("net.status", params_json='{"a":1}')) == "E_PARAMS"
    with win32.PipeClient(live.pipe) as client:                          # 超长的一行: E_BAD_REQUEST, 同一条连接接着用
        client.send(b'{"v":1,"id":"big","action":"heartbeat","params_json":"' + b"y" * 90_000 + b'"}\n')
        assert json.loads(client.read_line(30))["error"]["code"] == "E_BAD_REQUEST"
        assert json.loads(client.request(request_line("heartbeat", rid="after"), 20))["ok"] is True
    assert channel(live.pipe, "heartbeat")["result"] == {"state": "running"}


def test_the_real_channel_survives_connect_disconnect_connect_and_serves_one_connection_at_a_time(live):
    for index in range(3):
        assert channel(live.pipe, "heartbeat", rid=f"again-{index}")["ok"] is True
    first = win32.PipeClient(live.pipe)
    try:
        assert json.loads(first.request(request_line("heartbeat", rid="mine"), 20))["ok"] is True
        with pytest.raises(TimeoutError):
            win32.pipe_connect(live.pipe, 0.5)
    finally:
        first.close()
    assert channel(live.pipe, "heartbeat")["ok"] is True


def test_the_real_guardian_drops_a_silent_connection_after_about_five_seconds_and_the_next_client_gets_in(live):
    """契约 (补 1): 一条连接上 5 秒没有收到一条完整的请求, 看护方就把它断开. 真进程、默认的 5 秒 (别的测试为了快用零点几秒)."""
    silent = win32.PipeClient(live.pipe)
    try:
        started = time.monotonic()
        wait_for(lambda: client_dropped(silent), 40, "只连不说话的连接被看护方断开")
        assert 4.0 <= time.monotonic() - started < 30
    finally:
        silent.close()
    assert channel(live.pipe, "heartbeat")["ok"] is True                 # 断开以后别的客户端连得上


def test_the_pipe_client_asks_the_server_process_number_and_it_is_the_guardian(live):
    """GetNamedPipeServerProcessId: 连上真看护方的管道, 问到的进程号就是看护方的进程号 (给网络插件的门面用: 看护方不答了就结束它)."""
    with win32.PipeClient(live.pipe) as client:
        assert client.server_pid() == live.session.guardian.pid == live.session.guardian_pid
        assert win32.pipe_server_pid(client.handle) == client.server_pid()
    with pytest.raises(win32.PipeClosed):
        client.server_pid()                                                  # 客户端关了以后再问
    with pytest.raises(OSError):
        win32.pipe_server_pid(4_000_000)                                     # 这个数不是句柄


def test_a_dry_run_stop_over_the_real_channel_does_nothing(live):
    got = channel(live.pipe, "net.stop", {"session": live.start["session"], "reason": "user"}, dry_run=True)
    assert got["ok"] is True and got["result"] == {"dry_run": True, "would": guardian.WOULD_STOP, "state": "running"}
    assert channel(live.pipe, "net.status", {})["result"]["state"] == "running"
    assert live.session.guardian.poll() is None and live.session.core.poll() is None


def helper_guardian_argv() -> tuple[str, str]:
    """假助手源码里起看护方的那条命令行 (从 "-m", self.guardian_module 到收尾的 ]) 和整个源码 (扫源码核对, 不靠自觉)."""
    source = (Path(__file__).resolve().parent / "net_fake_helper.py").read_text(encoding="utf-8")
    assert source.count('"-m", self.guardian_module') == 1
    start = source.index('"-m", self.guardian_module')
    return source[start:source.index("]", source.index('"--fake-flush-log"', start))], source


def test_the_test_helpers_only_ever_start_the_guardian_with_the_fake_system_calls():
    """红线: 这一步里不许真的清系统的 DNS 缓存. 假助手起看护方的命令行里必须同时带着 --fake-adapter-marker 和 --fake-flush-log."""
    argv_text, _ = helper_guardian_argv()
    assert '"--fake-adapter-marker"' in argv_text and '"--fake-flush-log"' in argv_text


def test_the_fake_helper_never_calls_the_real_dns_flush():
    """红线: 假助手恢复里的"清 DNS 缓存"也是假的 (往 data_dir 里的记录文件追加一行). 它的源码里没有对 flush_dns、_dns_flush_resolver_cache、DnsFlushResolverCache 的
    任何引用 (只在注释和说明文字里提, ast 读出来的名字里没有)."""
    tree = ast.parse((Path(__file__).resolve().parent / "net_fake_helper.py").read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"flush_dns", "_dns_flush_resolver_cache", "DnsFlushResolverCache"}


def test_the_test_helpers_always_start_the_guardian_with_audit_and_a_fake_home_directory():
    """起看护方的命令行一定带 --audit (真起的时候不传, 看护方自己写到 ~/.worldmodel/serve/net/; 测试里不带的话它会往真的家目录写, 绝对不许); 看护方进程的家目录
    (USERPROFILE、HOME) 也换成测试目录里的假的, 再加一层保险."""
    argv_text, source = helper_guardian_argv()
    assert '"--audit"' in argv_text and "env=self._guardian_env()" in source
    helper_text = source[source.index("def _guardian_env"):source.index("def _launch")]
    assert 'env["USERPROFILE"] = env["HOME"] = str(self.fake_home)' in helper_text
