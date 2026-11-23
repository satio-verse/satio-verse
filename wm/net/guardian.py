"""网络插件·看护方 (2026-11-11 "网络"插件干跑的一块; 契约: 网络动作接口 v0.2 第 0、1、4.2-4.5、8 节): 会话期间一直活着的普通权限小进程.
`python -m wm.net.guardian ...` 能单独跑成一个进程.

会话期间要有一个看护方活着: 代办 (提权) 起完核心, 把作业对象的句柄 (只带结束和查询两项权利)、核心进程的句柄、核心输出的读端交给它,
自己退出. 看护方没了, 它手里那个作业对象的最后一个句柄就关了, 核心跟着没 (契约第 0 节). 它负责: 心跳、时限、停、查状态、会话内的恢复、
数连接 (只在内存里数, 不落盘).

两层:

1. GuardianState: 纯的状态机, 不碰系统. 输入是事件 (request 收到一个请求、tick 时钟走到某一刻、core_exited 核心退了、core_line 收到一行
   核心输出、shutdown 要求关机, 还有 killed / adapter_seen / dns_flushed 这三个"做完一个动作"的回报), 输出是要做的动作 (Reply 回应答、
   KillCore 结束核心、CheckAdapter 查网卡在不在、FlushDns 清 DNS 缓存、WriteAudit 写审计、Exit 退出). 时钟是传进来的数 (UTC 秒), 所以到时限、
   心跳断这些不用等真时间就能测. 停的做法照契约 4.2: 结束作业 -> 等"网卡"消失 (最多 10 秒) -> 清 DNS 缓存 -> 应答 -> 退出.
2. Guardian (外壳) 和 main: 建命名管道 \\\\.\\pipe\\wm-net-<会话号> (只许当前用户和 SYSTEM, 只建一个实例, 不接受远程; 一行一个 JSON, 一问一答,
   一次服务一个连接, 断了可以再连), 一个线程读核心输出 (只在内存里数, 不落盘、不打印、不记日志), 主线程把事件喂给状态机、把动作变成系统调用.
   "网卡在不在"和"清 DNS 缓存"是可以换的函数 (World), 默认实现在 win32.py; 命令行的 --fake-adapter-marker 和 --fake-flush-log 一起给就换成假的
   (网卡在不在 = 标记文件在不在, 假核心用"关句柄就删"的方式建它, 进程一没文件就没了; 清缓存 = 往文件里追加一行), 这时一点都不碰系统.
   两个必须同时给.

启动时先按种类核对交接来的三个句柄 (check_handles): --job 是作业对象 (并且两项设置对: "句柄全关掉时结束里面所有进程"开着、"允许里面的进程脱离作业"的两项关着 --
看护方"自己没了核心跟着没"靠的就是这几项)、--process 是进程并且在这个作业里、--output 是管道; 有一项不对, 作业句柄要是真的就先结束作业, 往 stderr 写一行
原因, 退出码 2. (句柄只是个数字, 数字碰巧在本进程里有效不等于继承到的就是约好的那个东西.)
句柄核对完再自查一次: 看护方不能是提权的 (契约第 10 节〔补 2〕: 它是项目里 Python 写的代码, 用户账号改得动, 提权跑就等于任意代码以管理员身份跑). 用
wm.privilege.is_elevated(): 是提权的就把作业结束掉、往 stderr 写一行原因、退出码 4 (不工作); 查不出来 (None) 照常跑, 往 stderr 记一笔.
main() 的退出码: 2 命令行或句柄不对、3 建不了会话通道、4 自己是提权的; 正常收尾是状态机说的退出码 (0).
审计 (--audit) 变成只给测试用的参数: 真起的时候不传, 看护方自己写到 ~/.worldmodel/serve/net/guardian-audit.jsonl (default_audit_path; 目录不在就建, 写不成不影响
收尾). 这个默认位置只在 main() 里算, 进程内的 Guardian(audit=None) 仍然是不记. 测试里绝对不许往真的家目录写: 起看护方进程一律带 --audit.

停的六种情况 (契约 4.5 的五种加一种): net.stop (原因是请求里的 reason)、超过宽限没收到心跳 (heartbeat_lost)、到了时限 (max_time)、核心自己退了
(core_exited, 状态记成 failed)、关机或注销 (system_shutdown; 控制台被关、Ctrl+C 也按这个算)、看护方自己的故障 (guardian_error, 状态记成 failed:
读核心输出的线程出了异常, 不收尾的话没人读管道, 核心写满 64 KB 就堵住, 真机上等于全机断网). 会话内的 net.restore 把会话收掉时原因记成 restore.
停了以后看护方退出, 不常驻; 退出前往 --audit 文件追加一行 (会话号、起止时间、原因、状态、网卡有没有消失、有没有清缓存、core_exit_code、每个进程的连接计数):
core_exit_code 是核心自己退的时候的退出码, 别的原因都是 null. "没有会话时 net.stop 回什么""停了以后 net.status 回什么"由网络插件的门面答, 它从这行审计里读.

会话通道上不说话的连接会被断开 (PipeServer 的 idle_s, 默认 5 秒): 客户端每次请求连一次, 读到应答就断.

核心输出里"哪个进程有几条连接走了上游" (net.status 的 matches) 怎么认, 要到真机才能核对 (契约第 12 节), 所以解析是可以换的: LINE_PARSERS 里登记,
命令行 --line-format 选. 现在只有假核心的格式 wmfake (`WMFAKE match process=<名字> via=upstream`).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import protocol, win32
from .config_gen import DEVICE
from .protocol import (E_BAD_REQUEST, E_BUSY, E_INTERNAL, HEARTBEAT, NET_RESTORE, NET_START, NET_STATUS, NET_STOP, REASON_CORE_EXITED,
                       REASON_GUARDIAN_ERROR, REASON_HEARTBEAT_LOST, REASON_MAX_TIME, REASON_RESTORE, REASON_SYSTEM_SHUTDOWN, Rejection, Request,
                       fail, ok)
from .recipe import MAX_PROCESSES, ascii_lower, process_problems

# ---- 状态、步骤、常量 ---------------------------------------------------------------------------------------------------
STATE_IDLE, STATE_STARTING, STATE_RUNNING, STATE_STOPPING, STATE_STOPPED, STATE_FAILED = (
    "idle", "starting", "running", "stopping", "stopped", "failed")
STATES = (STATE_IDLE, STATE_STARTING, STATE_RUNNING, STATE_STOPPING, STATE_STOPPED, STATE_FAILED)   # net.status 的 state (契约 4.3)

STEP_KILL, STEP_ADAPTER, STEP_DNS = "kill_core", "adapter_gone", "flush_dns"      # net.restore 回报里的三步 (契约 4.4)
DONE, SKIPPED, FAILED = "done", "skipped", "failed"                               # 每步的 result
WOULD_STOP = ["kill_core", "wait_adapter_gone", "flush_dns", "exit"]              # 演练时 net.stop 会做的事
WOULD_RESTORE = ["kill_core", "adapter_gone", "flush_dns"]                        # 演练时 net.restore 会做的事

ADAPTER_WAIT_S = 10.0       # 停的时候等网卡消失, 最多这么久 (契约 4.2)
ADAPTER_POLL_S = 0.25       # 等的时候隔多久问一次
TICK_S = 0.1                # 外壳每隔多久过一遍时钟
REQUEST_TIMEOUT_S = 60.0    # 管道线程最多等主线程这么久才回应答 (主线程 12 秒内一定做完停的流程, 这只是兜底)
REPLY_FLUSH_S = 5.0         # 退出前最多等最后一条应答被对面读走这么久
IDLE_DISCONNECT_S = 5.0     # 会话通道上一条连接这么久没有一条完整的请求, 就断开它 (从连上算, 或从上一条应答写完算; 等主线程出应答的时间不算)
EXIT_BAD_HANDLES, EXIT_NO_CHANNEL, EXIT_ELEVATED = 2, 3, 4    # main() 的退出码: 交接来的句柄不对 (命令行不合规矩 argparse 也是 2)、建不了会话通道、自己是提权的
AUDIT_PARTS = (".worldmodel", "serve", "net", "guardian-audit.jsonl")     # 看护方自己写审计的位置 (相对家目录; 契约第 8 节)
OUTPUT_LINE_LIMIT = 65536   # 核心输出里一行最长看这么多字节 (更长的半行丢掉)

TAG_STATUS, TAG_STOP = "status", "stop"       # CheckAdapter 是为谁查的


# ---- 核心输出的解析 (可以换) -----------------------------------------------------------------------------------------------

_WMFAKE = re.compile(r"WMFAKE match process=(?P<name>.+?) via=upstream\s*")


def parse_wmfake_line(line: str) -> str | None:
    """假核心的输出格式: `WMFAKE match process=<名字> via=upstream` -> 进程名; 别的行 (包括 via=direct) 返回 None.
    真核心的格式要到真机才核对 (契约第 12 节), 到时候在 LINE_PARSERS 里加一个同样签名的函数."""
    found = _WMFAKE.fullmatch(line.strip())
    return found.group("name") if found else None


LINE_PARSERS: dict[str, Callable[[str], str | None]] = {"wmfake": parse_wmfake_line}
DEFAULT_LINE_FORMAT = "wmfake"


# ---- 动作 (状态机的输出) -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Reply:
    """回一条应答给 conn (收请求时外壳给的那个凭据, 状态机原样带回)."""
    conn: object
    message: dict


@dataclass(frozen=True)
class KillCore:
    """结束作业对象 (里面的核心). 做完用 killed(now, 成功了吗) 回报."""


@dataclass(frozen=True)
class CheckAdapter:
    """查一次 WM-Ayane 网卡在不在. 做完用 adapter_seen(now, tag, 在不在) 回报 (tag 原样带回)."""
    tag: str


@dataclass(frozen=True)
class FlushDns:
    """清系统 DNS 缓存. 做完用 dns_flushed(now, 成功了吗) 回报."""


@dataclass(frozen=True)
class WriteAudit:
    """往审计文件追加一行 (record 是一个字典; 外壳没给 --audit 就什么也不做)."""
    record: dict


@dataclass(frozen=True)
class Exit:
    """退出进程."""
    code: int = 0


Action = Reply | KillCore | CheckAdapter | FlushDns | WriteAudit | Exit


@dataclass
class _Op:
    """正在进行的一轮"收尾": net.stop、心跳断、到时限、核心退了、关机、会话内的 net.restore 都是它."""
    kind: str                    # "stop" | "restore"
    reason: str
    final: str                   # 会话收掉以后的状态: stopped | failed
    waiters: list                # [(请求号, conn)]: 收尾做完回应答给谁
    ends_session: bool           # 这一轮会不会把一段还在跑的会话收掉
    kill: str = SKIPPED
    phase: str = "kill"          # kill -> adapter -> flush
    adapter_deadline: float = 0.0
    last_check: float = 0.0
    checking: bool = False       # 有一个 CheckAdapter 发出去还没回报
    first_present: bool | None = None
    adapter_gone: bool | None = None
    dns_ok: bool | None = None


def _int(value: float | None) -> int | None:
    return None if value is None else int(value)


# ---- 状态机 ---------------------------------------------------------------------------------------------------------------

class GuardianState:
    """看护方的状态机 (纯的, 不碰系统, 不读时钟: 每个事件都带着 now, UTC 秒).

    session 是 None 就是"没有会话" (idle): net.stop 回 idle、net.status 回 idle、net.restore 照样做 (确认网卡没了、清 DNS 缓存);
    给了 session 就是一段已经在跑的会话 (看护方是在网卡出现以后才起的, 所以一开始就是 running).
    processes 是配方里的进程名: net.status 的 matches 只统计它们 (不分 ASCII 大小写, 按配方里的写法给).
    parse_line 把核心输出的一行变成"走了上游的进程名" (没有就 None), 见 LINE_PARSERS.

    每个事件方法返回要做的动作清单 (list[Action]); 外壳照做, 做完把结果用 killed / adapter_seen / dns_flushed 送回来."""

    def __init__(self, *, now: float, session: str | None = None, started_at: float | None = None, deadline_at: float | None = None,
                 interval_s: float = protocol.DEFAULT_INTERVAL_S, grace_s: float = protocol.DEFAULT_GRACE_S,
                 processes: Sequence[str] = (), parse_line: Callable[[str], str | None] = parse_wmfake_line) -> None:
        self.session = session
        self.started_at = (now if started_at is None else started_at) if session else None
        self.deadline_at = deadline_at if session else None
        self.interval_s = interval_s     # 只记着: 判断只看 grace_s
        self.grace_s = grace_s
        self.state = STATE_RUNNING if session else STATE_IDLE
        self.last_stop: dict | None = None
        self.last_error: dict | None = None
        self.core_alive = bool(session)
        self.core_exit_code: int | None = None
        self.matches: dict[str, int] = {}
        self._names: dict[str, str] = {}              # 小写 -> 配方里的写法
        for name in processes:
            if ascii_lower(name) not in self._names:
                self._names[ascii_lower(name)] = name
                self.matches[name] = 0
        self._parse_line = parse_line
        self._last_heartbeat = now
        self._fault_detail = ""                       # fault() 给的异常类型名 (写进 last_error 的说明)
        self._op: _Op | None = None
        self._status_waiters: deque = deque()         # [(请求号, conn)]: 等 CheckAdapter(TAG_STATUS) 的结果

    # ---- 事件: 请求 ----

    def request(self, now: float, req: Request, conn: object) -> list[Action]:
        """收到一个解好的请求. 会话通道上认 net.stop、net.status、net.restore、heartbeat; net.start 回 E_BAD_REQUEST."""
        if req.action == NET_START:
            return [Reply(conn, fail(req.id, E_BAD_REQUEST, "会话通道上不认 net.start（它只在代办启动时那一条请求里认）"))]
        if req.action == NET_STOP:
            return self._stop_request(now, req, conn)
        problems = protocol.parse_empty_params(req.params_json)
        if problems:
            return [Reply(conn, protocol.params_failure(req.id, problems))]
        if req.action == HEARTBEAT:
            if self.state == STATE_RUNNING and not req.dry_run:
                self._last_heartbeat = now
            return [Reply(conn, ok(req.id, {"state": self.state}))]
        if req.action == NET_STATUS:
            self._status_waiters.append((req.id, conn))
            return [CheckAdapter(TAG_STATUS)]
        if req.action == NET_RESTORE:
            return self._restore_request(now, req, conn)
        return [Reply(conn, fail(req.id, protocol.E_UNKNOWN_ACTION, f"不认识的动作 {req.action!r}"))]   # decode_request 已经挡过, 兜底

    def _stop_request(self, now: float, req: Request, conn: object) -> list[Action]:
        reason, problems = protocol.parse_stop_params(req.params_json, self.session)
        if problems:
            return [Reply(conn, protocol.params_failure(req.id, problems))]
        if self.state in (STATE_IDLE, STATE_STOPPED, STATE_FAILED):
            return [Reply(conn, ok(req.id, {"state": STATE_IDLE}))]     # 没有会话在跑: 不算错, 可以重复发
        if self.state == STATE_STOPPING:
            return [Reply(conn, fail(req.id, E_BUSY, "正在停，稍后再发"))]
        if req.dry_run:
            return [Reply(conn, ok(req.id, {"dry_run": True, "would": list(WOULD_STOP), "state": self.state}))]
        return self._begin_stop(now, str(reason), STATE_STOPPED, [(req.id, conn)], "stop")

    def _restore_request(self, now: float, req: Request, conn: object) -> list[Action]:
        if self.state == STATE_STOPPING:
            return [Reply(conn, fail(req.id, E_BUSY, "正在停，稍后再发"))]
        if req.dry_run:
            return [Reply(conn, ok(req.id, {"dry_run": True, "would": list(WOULD_RESTORE), "state": self.state}))]
        if self.state in (STATE_RUNNING, STATE_STARTING):
            return self._begin_stop(now, REASON_RESTORE, STATE_STOPPED, [(req.id, conn)], "restore")
        op = _Op("restore", REASON_RESTORE, self.state, [(req.id, conn)], False, phase="adapter",
                 adapter_deadline=now + ADAPTER_WAIT_S, last_check=now, checking=True)   # 没有会话: 不用杀, 确认网卡没了、清 DNS 缓存
        self._op = op
        return [CheckAdapter(TAG_STOP)]

    # ---- 事件: 时间、核心、输出、关机 ----

    def tick(self, now: float) -> list[Action]:
        """时钟走到 now: 收尾的时候催着查网卡 (或到了 10 秒就不等了); 平时看心跳和时限."""
        op = self._op
        if op is not None:
            if op.phase == "adapter" and not op.checking:
                if now >= op.adapter_deadline:
                    return self._adapter_verdict(op, False)
                if now - op.last_check >= ADAPTER_POLL_S:
                    op.checking = True
                    op.last_check = now
                    return [CheckAdapter(TAG_STOP)]
            return []
        if self.state != STATE_RUNNING:
            return []
        if now - self._last_heartbeat > self.grace_s:
            return self._begin_stop(now, REASON_HEARTBEAT_LOST, STATE_STOPPED, [], "stop")
        if self.deadline_at is not None and now >= self.deadline_at:
            return self._begin_stop(now, REASON_MAX_TIME, STATE_STOPPED, [], "stop")
        return []

    def core_exited(self, now: float, exit_code: int | None = None) -> list[Action]:
        """核心进程退了. 在跑的会话: 确认网卡没了、清 DNS 缓存, 状态记成 failed (原因 core_exited). 其余状态只记一下核心没了."""
        self.core_alive = False
        if exit_code is not None:
            self.core_exit_code = exit_code
        if self.state == STATE_RUNNING:
            return self._begin_stop(now, REASON_CORE_EXITED, STATE_FAILED, [], "stop")
        return []

    def core_line(self, line: str) -> None:
        """核心输出的一行. 只在内存里数: 走了上游的进程在配方里就给它加一, 不在就扔掉; 这一行本身不留."""
        try:
            name = self._parse_line(line)
        except Exception:  # noqa: BLE001 - 解析函数是可以换的: 换进来的出了毛病也只是这一行不算, 不能带倒看护方
            return
        spelled = self._names.get(ascii_lower(name)) if type(name) is str else None
        if spelled is not None:
            self.matches[spelled] += 1

    def shutdown(self, now: float, reason: str = REASON_SYSTEM_SHUTDOWN) -> list[Action]:
        """关机、注销 (或控制台被关): 在跑的会话照停; 没有会话直接退出."""
        if self.state == STATE_RUNNING:
            return self._begin_stop(now, reason, STATE_STOPPED, [], "stop")
        if self.state == STATE_IDLE and self._op is None:
            return [Exit(0)]
        return []

    def fault(self, now: float, detail: str = "") -> list[Action]:
        """看护方自己出了故障 (现在只有一种: 读核心输出的线程出了异常; 读到头 = 核心关了输出, 不算): 在跑的会话按故障收尾——结束作业、等网卡消失、
        清 DNS 缓存、写审计、退出, 状态记成 failed, 原因和 last_error 的 code 都是 guardian_error. 不收尾的话没人读管道, 核心写满 64 KB 就会堵住,
        真机上所有流量都过核心, 等于全机断网, 而心跳还在走, 看护方自己发现不了. detail 只放异常的类型名 (异常的内容里可能带着核心输出的片段)."""
        if detail:
            self._fault_detail = detail
        if self.state == STATE_RUNNING:
            return self._begin_stop(now, REASON_GUARDIAN_ERROR, STATE_FAILED, [], "stop")
        return []

    # ---- 事件: 动作做完的回报 ----

    def killed(self, now: float, ok_: bool) -> list[Action]:
        """KillCore 做完了 (ok_ 是 TerminateJobObject 成不成功)."""
        op = self._op
        if op is None or op.phase != "kill":
            return []
        if ok_:
            self.core_alive = False        # 作业对象已经结束了: net.status 在收尾的这几秒里就如实说核心没了 (外壳随后也会发现它退出)
        else:
            op.kill = FAILED
        op.phase = "adapter"
        op.adapter_deadline = now + ADAPTER_WAIT_S
        op.last_check = now
        op.checking = True
        return [CheckAdapter(TAG_STOP)]

    def adapter_seen(self, now: float, tag: str, present: bool) -> list[Action]:
        """CheckAdapter 做完了 (网卡在不在)."""
        if tag == TAG_STATUS:
            if not self._status_waiters:
                return []
            request_id, conn = self._status_waiters.popleft()
            return [Reply(conn, ok(request_id, self._status_result(present)))]
        op = self._op
        if op is None or op.phase != "adapter":
            return []
        op.checking = False
        if op.first_present is None:
            op.first_present = present
        if not present:
            return self._adapter_verdict(op, True)
        if now >= op.adapter_deadline:
            return self._adapter_verdict(op, False)
        return []

    def dns_flushed(self, now: float, ok_: bool) -> list[Action]:
        """FlushDns 做完了 (ok_ 是清得成不成功). 收尾到此结束: 回应答、写审计、(会话收掉了就) 退出."""
        op = self._op
        if op is None or op.phase != "flush":
            return []
        op.dns_ok = ok_
        return self._finish(now, op)

    # ---- 收尾 ----

    def _begin_stop(self, now: float, reason: str, final: str, waiters: list, kind: str) -> list[Action]:
        op = _Op(kind, reason, final, waiters, True)
        op.kill = DONE if self.core_alive else SKIPPED     # 核心已经自己退了 (core_exited) 就只是顺手清一下作业里可能剩下的进程
        self._op = op
        self.state = STATE_STOPPING
        return [KillCore()]

    def _adapter_verdict(self, op: _Op, gone: bool) -> list[Action]:
        op.adapter_gone = gone
        op.phase = "flush"
        return [FlushDns()]

    def _steps(self, op: _Op) -> list[dict]:
        if op.adapter_gone:
            adapter = SKIPPED if (op.kill == SKIPPED and op.first_present is False) else DONE
        else:
            adapter = FAILED
        steps = [
            {"step": STEP_KILL, "result": op.kill, "detail": "结束作业对象失败" if op.kill == FAILED else ""},
            {"step": STEP_ADAPTER, "result": adapter,
             "detail": f"{int(ADAPTER_WAIT_S)} 秒后网卡 {DEVICE} 还在" if adapter == FAILED else ""},
            {"step": STEP_DNS, "result": DONE if op.dns_ok else FAILED, "detail": "" if op.dns_ok else "清 DNS 缓存没成功（可能没有权限）"},
        ]
        return steps

    def _finish(self, now: float, op: _Op) -> list[Action]:
        self._op = None
        actions: list[Action] = []
        if op.ends_session:
            self.state = op.final
            self.core_alive = False
            self.last_stop = {"reason": op.reason, "stopped_at": int(now)}
            if op.final == STATE_FAILED:
                if op.reason == REASON_GUARDIAN_ERROR:
                    message = "看护方自己出了故障（读不了核心的输出" + (f"：{self._fault_detail}" if self._fault_detail else "") + "），已把核心停掉"
                    self.last_error = {"code": op.reason, "message": message, "exit_code": None, "at": int(now)}
                else:
                    self.last_error = {"code": op.reason, "message": "核心自己退出了", "exit_code": self.core_exit_code, "at": int(now)}
        for request_id, conn in op.waiters:
            if op.kind == "restore":
                steps = self._steps(op)
                clean = all(step["result"] != FAILED for step in steps)
                result = {"steps": steps, "clean": clean, "reboot_advised": not clean}
            else:
                result = {"session": self.session, "state": STATE_STOPPED, "reason": op.reason, "adapter_gone": bool(op.adapter_gone),
                          "dns_flushed": bool(op.dns_ok), "stopped_at": int(now)}
            actions.append(Reply(conn, ok(request_id, result)))
        if op.ends_session:
            # core_exit_code: 核心自己退的时候是退出码, 别的时候是 null (停之前核心没退过; 杀了以后它的退出码是我们杀出来的, 不记)
            actions.append(WriteAudit({"session": self.session, "started_at": _int(self.started_at), "stopped_at": int(now),
                                       "reason": op.reason, "state": self.state, "adapter_gone": bool(op.adapter_gone),
                                       "dns_flushed": bool(op.dns_ok), "core_exit_code": self.core_exit_code if op.reason == REASON_CORE_EXITED else None,
                                       "matches": dict(self.matches)}))
            actions.append(Exit(0))
        return actions

    def _status_result(self, adapter_present: bool) -> dict:
        return {"state": self.state, "session": self.session, "started_at": _int(self.started_at), "deadline_at": _int(self.deadline_at),
                "core_alive": self.core_alive, "adapter_present": bool(adapter_present), "matches": dict(self.matches),
                "last_stop": self.last_stop, "last_error": self.last_error}


# ---- 外壳: 世界 (可以换的几个系统调用) -------------------------------------------------------------------------------------------

@dataclass
class World:
    """外壳靠它碰系统. 默认 (real_world) 用 win32.py 里的真调用; 测试和命令行的 --fake-* 换成假的."""
    adapter_present: Callable[[], bool]
    flush_dns: Callable[[], bool]
    terminate_job: Callable[[], bool]
    core_exit_code: Callable[[], int | None]


def real_world(job: int, process: int) -> World:
    """真的: 网卡在不在 = win32.adapter_exists(WM-Ayane) (只读); 清缓存 = win32.flush_dns; 结束作业、查核心退没退 = 手里那两个句柄."""
    return World(adapter_present=lambda: win32.adapter_exists(DEVICE), flush_dns=win32.flush_dns,
                 terminate_job=lambda: win32.terminate_job(job), core_exit_code=lambda: win32.process_exit_code(process))


def fake_world(job: int, process: int, marker: Path, flush_log: Path, clock: Callable[[], float] = time.time) -> World:
    """假的 (命令行 --fake-adapter-marker 和 --fake-flush-log): 网卡在不在 = 标记文件在不在 (假核心用"关句柄就删"的方式建这个文件, 进程一没系统就把它
    删了, 和真的网卡是核心进程持有的一个意思; 别的进程读不了它的内容, 所以只看在不在, 不读); 清缓存 = 往 flush_log 追加一行时间.
    结束作业、查核心退没退照旧用句柄. 一点都不碰系统的网络."""
    def adapter_present() -> bool:
        return marker.exists()

    def flush() -> bool:
        with open(flush_log, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"flush {clock():.3f}\n")
        return True

    return World(adapter_present=adapter_present, flush_dns=flush, terminate_job=lambda: win32.terminate_job(job),
                 core_exit_code=lambda: win32.process_exit_code(process))


class Clock:
    """UTC 秒的时钟, 从启动时读到的真实时间起, 之后只跟着单调时钟走: 有人把系统时间往前往后拨, 心跳和时限也不会跟着乱."""

    def __init__(self) -> None:
        self._wall0 = time.time()
        self._mono0 = time.monotonic()

    def now(self) -> float:
        return self._wall0 + (time.monotonic() - self._mono0)


# ---- 外壳: 会话通道 ---------------------------------------------------------------------------------------------------------------

class Waiter:
    """会话通道上一个等着应答的请求: 管道线程等它 (event), 主线程把应答填进来 (deliver). final 的是最后一条应答 (之后进程要退出):
    管道线程写完还要等对面读走 (FlushFileBuffers), 然后设 written, 主线程等到它才退出."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.written = threading.Event()
        self.message: dict | None = None
        self.final = False

    def deliver(self, message: dict, final: bool = False) -> None:
        self.message = message
        self.final = final
        self.event.set()


class PipeServer:
    """\\\\.\\pipe\\wm-net-<会话号>: 一次服务一个连接, 一问一答, 断了可以再连. 读请求用 PeekNamedPipe 轮询 (不会卡死在一个不说话的连接上
    以外的地方; 连接上没有数据时每 1-25 毫秒看一眼). 每条请求: 解 (protocol.decode_request) -> 不合格的当场回错误; 合格的交给主线程
    (events 队列), 等它填应答, 写回去.

    不说话的连接要断开: 一条连接上 idle_s 秒 (默认 5 秒) 没有收到一条完整的请求 (从连上算, 或从上一条应答写完算; 发了半行就不动也算; 等主线程出应答
    的那段时间不算), 就断开它, 接着等下一个. 不然一个只连不说话的客户端会一直占着这个只有一个实例的通道. 客户端的规矩 (写进了契约):
    每次请求连一次, 读到应答就断."""

    def __init__(self, name: str, events: queue.Queue, handle: int | None = None, idle_s: float = IDLE_DISCONNECT_S) -> None:
        self.name = name
        self.events = events
        self.idle_s = idle_s
        self.handle = win32.pipe_server_create(name) if handle is None else handle
        self._closing = False
        self._waiting = False                    # 服务线程现在是不是堵在 ConnectNamedPipe 上 (等客户端)
        self._thread = threading.Thread(target=self._serve, name="wm-net-pipe", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        """收摊 (进程退出前, 或测试收尾): 线程在一条连接上轮询的话, 几十毫秒内自己就会看到 _closing 退出; 要是它堵在 ConnectNamedPipe 上
        (没有客户端), 就连一下马上断来叫醒它. 等它走完, 再关管道句柄. 最多等 3 秒."""
        self._closing = True
        if self._thread.ident is not None:                # 线程起过才有得等 (没 start 过的直接关句柄)
            if self._thread.is_alive() and self._waiting:
                self._wake()
            self._thread.join(0.3)
            if self._thread.is_alive():          # 还没走 (刚才判断"堵在连接上"的那一下赶巧错过了): 再叫醒一次
                self._wake()
                self._thread.join(2.0)
        win32.close_handle(self.handle)

    def _wake(self) -> None:
        try:
            win32.PipeClient(self.name, 0.5).close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._closing:
            self._waiting = True
            try:
                win32.pipe_server_connect(self.handle)
            except OSError:
                win32.pipe_server_disconnect(self.handle)
                time.sleep(0.05)
                continue
            finally:
                self._waiting = False
            try:
                self._serve_connection()
            except OSError:        # 对面断了 (PipeClosed 也是 OSError)
                pass
            except Exception:  # noqa: BLE001 - 一个连接里出了意外不该带倒整个通道; 丢掉这个连接, 接着等下一个
                pass
            finally:
                win32.pipe_server_disconnect(self.handle)

    def _serve_connection(self) -> None:
        buffer = protocol.LineBuffer()
        pause = 0.001
        quiet_since = time.monotonic()               # 从连上算起, 之后是从上一条应答写完算起
        while not self._closing:
            if time.monotonic() - quiet_since > self.idle_s:
                return                               # 太久没有一条完整的请求 (只连不说话、只发半行不动、一直灌不成行的数据都算): 断开 (调用方的 finally
                                                     # 会 DisconnectNamedPipe), 接着等下一个
            avail = win32.pipe_peek(self.handle)
            if not avail:
                time.sleep(pause)
                pause = min(pause * 2, 0.025)
                continue
            pause = 0.001
            for raw in buffer.feed(win32.pipe_read(self.handle, min(avail, win32.PIPE_BUFFER))):
                self._answer(raw)
                quiet_since = time.monotonic()       # 等主线程出应答的那段时间不算: 应答写完才重新计

    def _answer(self, raw: bytes) -> None:
        decoded = protocol.decode_request(raw)
        waiter: Waiter | None = None
        if isinstance(decoded, Rejection):
            message = decoded.reply()
        else:
            waiter = Waiter()
            self.events.put(("request", waiter, decoded))
            if waiter.event.wait(REQUEST_TIMEOUT_S) and waiter.message is not None:
                message = waiter.message
            else:
                message = fail(decoded.id, E_INTERNAL, "看护方没有及时处理这条请求")
        try:
            win32.pipe_write(self.handle, protocol.encode(message))
            if waiter is not None and waiter.final:
                win32.pipe_flush(self.handle)
        finally:
            if waiter is not None:
                waiter.written.set()


# ---- 外壳: 主循环 -----------------------------------------------------------------------------------------------------------------

def _pump_output(fd: int, events: queue.Queue) -> None:
    """读核心输出 (线程): 按行切开, 一批一批交给主线程数. 只在内存里过一下, 不落盘、不打印、不记日志.

    读到头 (核心关了输出, 管道的另一头没了) 照旧不管: 核心退了由主线程按进程句柄发现. **出了异常不能悄悄没了**: 之后没人读管道, 核心写满 64 KB 就堵住,
    真机上所有流量都过核心, 等于全机断网, 而心跳还在走, 看护方自己发现不了. 所以给主线程发一个 ("fault", 异常的类型名) 事件, 主线程按故障收尾
    (原因 guardian_error, 状态 failed). 事件里只放类型名, 不放异常的内容: 内容里可能带着核心输出的片段."""
    pending = b""
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            pending += chunk
            *lines, pending = pending.split(b"\n")
            if len(pending) > OUTPUT_LINE_LIMIT:
                pending = b""
            if lines:
                events.put(("lines", [raw.decode("utf-8", errors="replace") for raw in lines]))
    except Exception as exc:  # noqa: BLE001
        events.put(("fault", type(exc).__name__))


def shutdown_reason(event: int) -> str | None:
    """控制台事件号 -> 停的原因. 关机、注销、控制台被关、Ctrl+C、Ctrl+Break 都按 system_shutdown 收尾 (契约只列了关机和注销; 后三个是这里补的,
    省得被人在控制台里打断时核心和网卡留在系统里). 别的事件不管 (None)."""
    if event in (win32.CTRL_CLOSE_EVENT, win32.CTRL_LOGOFF_EVENT, win32.CTRL_SHUTDOWN_EVENT, win32.CTRL_C_EVENT, win32.CTRL_BREAK_EVENT):
        return REASON_SYSTEM_SHUTDOWN
    return None


class Guardian:
    """外壳: 把状态机接到真的 (或假的) 世界上. run() 一直跑到状态机说退出, 返回退出码."""

    def __init__(self, state: GuardianState, world: World, *, pipe: PipeServer | None, events: queue.Queue | None = None,
                 output_fd: int | None = None, audit: Path | None = None, clock: Callable[[], float] | None = None) -> None:
        self.state = state
        self.world = world
        self.events: queue.Queue = events if events is not None else (pipe.events if pipe is not None else queue.Queue())
        self.pipe = pipe
        self.output_fd = output_fd
        self.audit = audit
        self.clock = clock or Clock().now
        self.exit_code: int | None = None
        self.finished = threading.Event()
        self._reported_exit = False
        self._final_waiters: list[Waiter] = []

    def post_shutdown(self, reason: str) -> None:
        """从别的线程 (控制台事件的回调) 要求关机."""
        self.events.put(("shutdown", reason))

    def run(self) -> int:
        if self.pipe is not None:
            self.pipe.start()
        if self.output_fd is not None:
            threading.Thread(target=_pump_output, args=(self.output_fd, self.events), name="wm-net-output", daemon=True).start()
        try:
            while self.exit_code is None:
                try:
                    batch = [self.events.get(timeout=TICK_S)]
                except queue.Empty:
                    batch = []
                while True:
                    try:
                        batch.append(self.events.get_nowait())
                    except queue.Empty:
                        break
                for event in batch:
                    self._handle(event)
                self._poll_core()
                self._run(self.state.tick(self.clock()))
            for waiter in self._final_waiters:     # 最后一条应答要等对面读走了再退出
                waiter.written.wait(REPLY_FLUSH_S)
            return self.exit_code
        finally:
            self.finished.set()

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "request":
            _, waiter, request = event
            try:
                self._run(self.state.request(self.clock(), request, waiter))
            except Exception:  # noqa: BLE001 - 状态机或系统调用出了意外: 回 E_INTERNAL, 不让这条请求悬着
                waiter.deliver(fail(request.id, E_INTERNAL, "看护方处理这条请求时出了意外"))
        elif kind == "lines":
            for line in event[1]:
                self.state.core_line(line)
        elif kind == "shutdown":
            self._run(self.state.shutdown(self.clock(), event[1]))
        elif kind == "fault":
            self._run(self.state.fault(self.clock(), event[1]))

    def _poll_core(self) -> None:
        if self._reported_exit:
            return
        try:
            code = self.world.core_exit_code()
        except OSError:
            return
        if code is not None:
            self._reported_exit = True
            self._run(self.state.core_exited(self.clock(), code))

    def _run(self, actions: list[Action]) -> None:
        pending = deque(actions)
        while pending:
            action = pending.popleft()
            if isinstance(action, Reply):
                waiter = action.conn
                if isinstance(waiter, Waiter):
                    ending = any(isinstance(later, Exit) for later in pending)    # 后面跟着 Exit: 这是最后一条应答
                    waiter.deliver(action.message, final=ending)
                    if ending:
                        self._final_waiters.append(waiter)
            elif isinstance(action, KillCore):
                pending.extend(self.state.killed(self.clock(), self._attempt(self.world.terminate_job, False)))
            elif isinstance(action, CheckAdapter):
                # 查不出来按"还在"算: 没确认消失就不能说消失了
                pending.extend(self.state.adapter_seen(self.clock(), action.tag, self._attempt(self.world.adapter_present, True)))
            elif isinstance(action, FlushDns):
                pending.extend(self.state.dns_flushed(self.clock(), self._attempt(self.world.flush_dns, False)))
            elif isinstance(action, WriteAudit):
                self._write_audit(action.record)
            elif isinstance(action, Exit):
                self.exit_code = action.code

    @staticmethod
    def _attempt(call: Callable[[], bool], fallback: bool) -> bool:
        try:
            return bool(call())
        except Exception:  # noqa: BLE001 - 系统调用失败按最坏的算 (fallback), 收尾照样往下走
            return fallback

    def _write_audit(self, record: dict) -> None:
        """普通权限这边的审计 (契约第 8 节): 会话号、起止时间、停的原因、每个进程的连接计数. 一行 JSON 追加; 不给 --audit 就不记."""
        if self.audit is None:
            return
        try:
            self.audit.parent.mkdir(parents=True, exist_ok=True)       # 目录不在就建 (默认位置 ~/.worldmodel/serve/net/ 头一回用时不在); 写不成不影响收尾
            with open(self.audit, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError:
            pass


# ---- 命令行 -------------------------------------------------------------------------------------------------------------------

def _handle_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("句柄必须是整数") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("句柄必须是正整数")
    return value


def _session_arg(text: str) -> str:
    if not protocol.valid_session(text):
        raise argparse.ArgumentTypeError("会话号必须是 32 个小写十六进制字符")
    return text


def _range_arg(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("必须是整数") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"必须在 {low} 到 {high} 之间")
        return value
    return parse


def _processes_arg(text: str) -> list[str]:
    try:
        names = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是 JSON 数组") from None
    if type(names) is not list or not 1 <= len(names) <= MAX_PROCESSES:
        raise argparse.ArgumentTypeError(f"必须是 1 到 {MAX_PROCESSES} 个进程名的 JSON 数组")
    for index, name in enumerate(names):
        found = process_problems(name, path=f"processes[{index}]")
        if found:
            raise argparse.ArgumentTypeError(found[0])
    return names


def check_handles(job: int, process: int, output: int) -> tuple[str, bool]:
    """交接来的三个句柄按种类和关系核对 (句柄只是个数字: 数字碰巧在本进程里有效, 不等于继承到的就是约好的那个东西; 交接要是出了岔子, 看护方
    就会拿着不相干的东西跑起来, 到要停的时候结束不了核心). 依次:

    - --job 确实是作业对象 (并且带查询权): win32.is_job_object;
    - --job 的两项设置对 (契约 4.1 第 8 步、第 10 节〔补 2〕: 看护方"自己没了核心跟着没"靠的就是它们): 带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (句柄全关掉时
      结束里面所有进程); 不带 JOB_OBJECT_LIMIT_BREAKAWAY_OK 和 JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK (里面的进程不许脱离作业): win32.job_limit_flags;
    - --process 确实是进程 (GetExitCodeProcess 调得通), 并且就在这个作业里 (IsProcessInJob 为真);
    - --output 确实是管道 (GetFileType 等于 3).

    返回 (不对的原因, 空 = 全对; 作业句柄是不是真的作业对象). 核心在看护方起来之前就已经退了: 退了的进程还算在作业里, 照常往下走, 由主循环发现
    它退出, 按 core_exited 收尾."""
    if not win32.is_job_object(job):
        return f"--job {job} 不是作业对象（或没有查询权）", False
    flags = win32.job_limit_flags(job)
    if flags is None:
        return f"--job {job} 这个作业的设置读不出来（没有查询权？）", True
    if not flags & win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
        return f"--job {job} 这个作业没有设“句柄全关掉时结束里面所有进程”（JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE）：看护方没了核心不会跟着没", True
    if flags & (win32.JOB_OBJECT_LIMIT_BREAKAWAY_OK | win32.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK):
        return (f"--job {job} 这个作业允许里面的进程脱离作业（JOB_OBJECT_LIMIT_BREAKAWAY_OK 或 JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK）："
                "脱离出去的进程不会跟着没"), True
    if not win32.is_process_handle(process):
        return f"--process {process} 不是进程句柄（或没有查询权）", True
    if not win32.process_in_job(process, job):
        return f"--process {process} 这个进程不在 --job {job} 这个作业里", True
    if not win32.is_pipe_handle(output):
        return f"--output {output} 不是管道", True
    return "", True


def self_elevated() -> bool | None:
    """看护方自己是不是提权的: wm.privilege.is_elevated() (True 提权、False 不是、None 查不出来; 它自己不抛异常, 这里再兜一层, 返回值也只认布尔).
    导入放在函数里: 导入这个模块时不拖进 wm.privilege (wm/net 只许用标准库、wm.privilege 和自己, 隔离测试守着)."""
    try:
        from wm import privilege
        got = privilege.is_elevated()
    except Exception:  # noqa: BLE001 - 自查不许让看护方起不来: 查不出来就按 None
        return None
    return got if isinstance(got, bool) else None


def check_not_elevated() -> tuple[bool, str]:
    """看护方不能是提权的 (契约第 10 节〔补 2〕). 返回 (要不要拒绝工作, 要往 stderr 写的一行; 空 = 不用写):
    提权的 -> (True, 原因); 查不出来 (None) -> (False, 记一笔: 照常跑); 不是 -> (False, "")."""
    elevated = self_elevated()
    if elevated is True:
        return True, ("wm.net.guardian: 看护方是提权的（管理员权限），不工作：它是项目里 Python 写的代码，用户账号改得动，"
                      "提权跑就等于任意代码以管理员身份跑")
    if elevated is None:
        return False, "wm.net.guardian: 查不出自己是不是提权的，照常运行"
    return False, ""


def default_audit_path(home: Path | None = None) -> Path:
    """看护方自己写审计的位置: <家目录>/.worldmodel/serve/net/guardian-audit.jsonl (契约第 8 节: serve 整个目录本来就在保护名单里, 她的文件工具读不到也改不了,
    要看只能经门面的 net.status). 只在 main() 里算 (没给 --audit 的时候); 进程内的 Guardian(audit=None) 仍然是不记. 不给 home 就是当前用户的家目录
    (Path.home(); 找不到会抛 RuntimeError). 测试里绝对不许往真的家目录写: 起看护方进程一律带 --audit, 这个函数用一个假的家目录测."""
    return (Path.home() if home is None else Path(home)).joinpath(*AUDIT_PARTS)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m wm.net.guardian", allow_abbrev=False,
                                 description="网络插件的看护方: 拿到代办交来的作业对象、核心进程、核心输出三个句柄, 管心跳、时限、停、查状态、恢复.")
    ap.add_argument("--session", required=True, type=_session_arg, help="会话号 (32 个小写十六进制字符), 管道名是 \\\\.\\pipe\\wm-net-<会话号>")
    ap.add_argument("--job", required=True, type=_handle_arg, help="作业对象的句柄 (继承来的, 只要结束和查询两项权利)")
    ap.add_argument("--process", required=True, type=_handle_arg, help="核心进程的句柄 (继承来的, 能等它、能查退出码)")
    ap.add_argument("--output", required=True, type=_handle_arg, help="核心输出的读端 (继承来的管道句柄)")
    ap.add_argument("--deadline", required=True, type=float, help="时限, UTC 秒 (到了就停, 原因 max_time)")
    ap.add_argument("--interval", required=True, type=_range_arg(*protocol.INTERVAL_RANGE), help="心跳间隔 (秒), 只记着")
    ap.add_argument("--grace", required=True, type=_range_arg(*protocol.GRACE_RANGE), help="心跳宽限 (秒): 超过没收到心跳就停")
    ap.add_argument("--processes", required=True, type=_processes_arg, help='配方里的进程名, JSON 数组; matches 只统计它们')
    ap.add_argument("--started", type=float, help="会话开始的时间, UTC 秒 (不给就是看护方启动的时间)")
    ap.add_argument("--audit", type=Path, help="只给测试用: 审计文件, 会话结束时追加一行. 真起的时候不传, 看护方自己写到 ~/.worldmodel/serve/net/guardian-audit.jsonl")
    ap.add_argument("--line-format", choices=sorted(LINE_PARSERS), default=DEFAULT_LINE_FORMAT, help="核心输出的格式 (怎么认走了上游的连接)")
    ap.add_argument("--fake-adapter-marker", type=Path, help="只给测试用: 网卡在不在 = 这个文件在不在 (假核心用关句柄就删的方式建它; 要和 --fake-flush-log 一起给)")
    ap.add_argument("--fake-flush-log", type=Path, help="只给测试用: 清 DNS 缓存 = 往这个文件追加一行 (要和 --fake-adapter-marker 一起给)")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if (args.fake_adapter_marker is None) != (args.fake_flush_log is None):
        ap.error("--fake-adapter-marker 和 --fake-flush-log 要一起给（只给一个会让另一个落到真的系统调用上）")
    problem, job_is_real = check_handles(args.job, args.process, args.output)
    if problem:
        if job_is_real:
            win32.terminate_job(args.job)      # 作业句柄是真的: 先把里面的核心结束 (交接出了岔子, 没有人能再管它了), 再退出
        print(f"wm.net.guardian: {problem}", file=sys.stderr)
        return EXIT_BAD_HANDLES
    refuse, note = check_not_elevated()      # 句柄核对完再自查: 看护方不能是提权的 (契约第 10 节〔补 2〕)
    if note:
        print(note, file=sys.stderr)
    if refuse:
        win32.terminate_job(args.job)        # 提权的看护方不工作: 把作业结束掉 (没有人能再管里面的核心了), 再退出
        return EXIT_ELEVATED
    audit = args.audit
    if audit is None:                        # 真起的时候不传 --audit: 自己算位置 (--audit 只给测试用; 进程内的 Guardian(audit=None) 仍然是不记)
        try:
            audit = default_audit_path()
        except (RuntimeError, OSError) as exc:
            print(f"wm.net.guardian: 找不到家目录，这次不记审计（{type(exc).__name__}）", file=sys.stderr)
    clock = Clock()
    now = clock.now()
    if args.fake_adapter_marker is not None:
        world = fake_world(args.job, args.process, args.fake_adapter_marker, args.fake_flush_log, clock.now)
    else:
        world = real_world(args.job, args.process)
    try:
        pipe = PipeServer(protocol.pipe_name(args.session), queue.Queue())
    except OSError:
        win32.terminate_job(args.job)      # 没有通道就没人能发心跳、也没人能叫停: 别留着核心, 直接收拾
        return EXIT_NO_CHANNEL
    import msvcrt
    output_fd = msvcrt.open_osfhandle(args.output, os.O_RDONLY)
    state = GuardianState(now=now, session=args.session, started_at=args.started, deadline_at=args.deadline, interval_s=args.interval,
                          grace_s=args.grace, processes=args.processes, parse_line=LINE_PARSERS[args.line_format])
    guardian = Guardian(state, world, pipe=pipe, output_fd=output_fd, audit=audit, clock=clock.now)

    def on_console_event(event: int) -> bool:
        reason = shutdown_reason(event)
        if reason is None:
            return False
        guardian.post_shutdown(reason)
        if event in (win32.CTRL_CLOSE_EVENT, win32.CTRL_LOGOFF_EVENT, win32.CTRL_SHUTDOWN_EVENT):
            guardian.finished.wait(4.0)      # 系统在这几个事件之后一两秒就会杀进程: 能收拾多少收拾多少
        return True

    try:
        win32.set_console_ctrl_handler(on_console_event)
    except OSError:      # 没有控制台的进程登记不上: 没关系, 只是收不到关机通知
        pass
    code = guardian.run()
    pipe.close()
    return code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):      # 重定向到文件时默认是系统的 ANSI 代码页 (中文 Windows 上是 GBK): 往 stderr 写的原因要读得回来, 一律 UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
