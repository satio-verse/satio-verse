"""她起的后台作业做完了, 自己回到发起的会话 (2026-11-01 用户: "后台作业做完自己回报"; Super-agent 接入设计第 1 步
"长作业先返回 job_id, 完成时发事件"; 研究总报告第四节第 1 项).

后台作业是她自己的 Creator 扩展 workbench-jobs: job_start 立刻返回 job_id, 进程经 WMI 起, 不在工作台的进程树里.
扩展每次调用起一个进程、调完就收, 作业做完没人看着, 以前要她自己再去 job_list / job_output 查.
这里只读它照 SKILL.md 写下的状态 (jobs.json、exit/<id>.txt、logs/<id>.log): 不写、不停、不重跑.

- 记: 工作台里 job_start (直通工具或 mcp_call) 成功了, 记下 会话 + 那一轮 + job_id, 存 <家>/job-watch.json (重启也在).
- 看: 有在看的就每 POLL_S 秒看一次. 有退出码文件 = 结束; 记录是 stopped = 被停了; 进程不在了又没有退出码
  (连着两次, 免得刚好赶上它在写文件) = 退出码未知; 记录没了 = 被 job_clean 清掉了; 看了 MAX_AGE_S 还没完就不再盯.
- 交: AgentHub.job_done 决定怎么交 (这一轮还在做 -> 下一步开头交给她; 起它的那一轮正常做完、你没按停 -> 接着做一轮
  看结果、告诉你; 那一轮停了 / 早换了别的事 -> 只在对话里记一笔, 下次你说话时她知道, 不复活停掉的事).
  先把它从在看的名单里拿掉再交: 宁可漏报一次, 不重复接着做 (重启不重复动作).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path

import psutil

log = logging.getLogger("wm.agent.jobwatch")

SERVER = "creator:workbench-jobs"
WATCH_FILE = "job-watch.json"
POLL_S = 5.0
IDLE_S = 60.0               # 没有在看的作业时多久醒一次 (记了新的会马上叫醒)
MAX_AGE_S = 24 * 3600.0
_STARTED = re.compile(r"已启动\s+([A-Za-z0-9_-]{1,32})（pid\s*(\d+)）")
_EPOCH_1601 = 11644473600   # Windows FILETIME (1601 年起的 100 纳秒) -> unix 秒


def state_dir(home: Path) -> Path:
    """扩展的状态目录, 和 workbench-jobs 的 state_dir() 一样 (环境变量优先)."""
    env = os.environ.get("WORKBENCH_JOBS_STATE_DIR")
    return Path(env) if env else Path(home) / "state" / "workbench-jobs"


def started(name: str, args: dict, text: str, server_for) -> tuple[str, int] | None:
    """这次工具调用是不是 workbench-jobs 的 job_start 起成了一个作业 -> (job_id, pid)."""
    if name == "job_start":
        ours = server_for(name) == SERVER
    elif name == "mcp_call":
        ours = args.get("server_id") == SERVER and args.get("tool") == "job_start"
    else:
        ours = False
    m = _STARTED.search(str(text or "")) if ours else None
    return (m.group(1), int(m.group(2))) if m else None


def records(root: Path) -> dict[str, dict] | None:
    """jobs.json 里的作业 (id -> 记录). 文件不在 = 没有作业; 读坏了返回 None (这一次先不判, 别把正在写当成被清掉)."""
    try:
        data = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if isinstance(jobs, list):
        jobs = {j.get("id"): j for j in jobs if isinstance(j, dict)}
    return {k: v for k, v in jobs.items() if isinstance(v, dict)} if isinstance(jobs, dict) else {}


def alive(rec: dict) -> bool:
    """进程还在, 而且是起作业的那一个 (按创建时间核对, PID 被复用了不算)."""
    try:
        p = psutil.Process(int(rec.get("pid") or 0))
        if not p.is_running():
            return False
        want = rec.get("process_created")
        return want is None or abs(p.create_time() - (int(want) / 1e7 - _EPOCH_1601)) < 1.0
    except (psutil.Error, ValueError, TypeError, OverflowError):
        return False


def check(root: Path, entry: dict, recs: dict[str, dict]) -> dict | None:
    """一个在看的作业现在怎样: 结束了返回 {how, code, ended}; 还在跑返回 None. entry['misses'] 记连着几次没看到进程."""
    rec, now = recs.get(entry["job"]), time.time()
    if rec is None:
        return {"how": "gone", "code": None, "ended": now}
    exitfile = root / "exit" / f"{entry['job']}.txt"
    try:
        code, ended = exitfile.read_text(encoding="ascii").strip(), exitfile.stat().st_mtime
    except (OSError, UnicodeError):
        code, ended = "", now
    if code:
        return {"how": "exited", "code": int(code) if code.lstrip("-").isdigit() else None, "ended": ended}
    if rec.get("state") == "stopped":
        return {"how": "stopped", "code": None, "ended": now}
    if rec.get("state") in ("starting", "unknown") or alive(rec):
        entry["misses"] = 0
        return {"how": "too_long", "code": None, "ended": now} if now - float(entry.get("started") or now) > MAX_AGE_S else None
    entry["misses"] = int(entry.get("misses") or 0) + 1
    return {"how": "lost", "code": None, "ended": now} if entry["misses"] >= 2 else None


def tail(path, lines: int = 12, limit: int = 1500) -> str:
    """日志最后几行 (只读末尾 8 KB)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            data = f.read()
    except (OSError, TypeError, ValueError):
        return ""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").strip()
    return "\n".join(text.splitlines()[-lines:])[-limit:]


def human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


def describe(entry: dict, rec: dict | None, result: dict) -> dict:
    """给页面和她看的: {id, name, how, code, took, text (一句话), body (交给她的那段: 标题、一句话、最后几行输出)}."""
    how, code = result["how"], result.get("code")
    said = {"exited": f"结束了，退出码 {code if code is not None else '未知'}", "stopped": "被停掉了",
            "lost": "进程不在了，没有退出码（可能被外部结束了）", "gone": "的记录被清掉了（job_clean），结果不明",
            "too_long": f"跑了超过 {human(MAX_AGE_S)} 还没结束，不再盯着（要看用 job_list）"}[how]
    took = human(result["ended"] - float((rec or {}).get("started") or entry.get("started") or result["ended"]))
    name = str((rec or {}).get("name") or entry.get("name") or "")[:60]
    head = f"后台作业 {entry['job']}{f'「{name}」' if name else ''}"
    text = f"{head}{said}" + ("" if how in ("gone", "too_long") else f"（跑了 {took}）")
    out = tail((rec or {}).get("log")) if how in ("exited", "stopped", "lost") else ""
    body = f"[后台作业做完了 · {entry['job']}]\n{text}。" + (f"\n最后几行输出：\n{out}" if out else "")
    return {"id": entry["job"], "name": name, "how": how, "code": code, "took": took, "text": text, "body": body}


class JobWatch:
    """一个服务端一个: 记下谁起了哪个作业, 后台看着, 结束了交给 hub.job_done."""

    def __init__(self, hub):
        self.hub = hub
        self.root = state_dir(hub.home)
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()

    def entries(self) -> list[dict]:
        try:
            data = json.loads((Path(self.hub.home) / WATCH_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data.get("jobs", []) if isinstance(e, dict) and e.get("job") and e.get("session")] \
            if isinstance(data, dict) else []

    def _save(self, items: list[dict]) -> None:
        self.hub._write(WATCH_FILE, json.dumps({"jobs": items}, ensure_ascii=False, indent=1))

    def track(self, s, name: str, args: dict, text, error: bool) -> dict | None:
        """job_start 成功了: 记下哪个会话、哪一轮起的. 不是起作业的调用、或者没起成, 返回 None."""
        got = None if error else started(name, args, text, self.hub.studio.direct.server_for)
        if got is None:
            return None
        inner = args if name == "job_start" else (args.get("arguments") or {})
        command = str(inner.get("command") or "").strip()
        entry = {"session": s.id, "run": s.run.get("id", ""), "job": got[0], "pid": got[1],
                 "name": str(inner.get("name") or (command.splitlines()[0][:60] if command else ""))[:80],
                 "started": time.time(), "misses": 0}
        self._save([e for e in self.entries() if e["job"] != entry["job"]] + [entry])
        self.wake.set()
        return entry

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._loop())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _loop(self) -> None:
        while True:
            try:
                busy = await self.poll()
            except Exception:  # noqa: BLE001
                log.exception("看后台作业时出错")
                busy = True
            self.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), POLL_S if busy else IDLE_S)

    async def poll(self) -> bool:
        """看一遍还没结束的; 结束了的先从名单里拿掉再交出去. 返回还有没有在看的."""
        items = self.entries()
        if not items:
            return False
        recs = await asyncio.to_thread(records, self.root)
        if recs is None:
            return True
        results = [(e, await asyncio.to_thread(check, self.root, e, recs)) for e in items]
        self._save([e for e, r in results if r is None])
        for e, r in results:
            if r is not None:
                try:
                    how = await self.hub.job_done(e, describe(e, recs.get(e["job"]), r))
                    log.info("后台作业 %s 结束 (%s), 交回会话 %s: %s", e["job"], r["how"], e["session"], how)
                except Exception:  # noqa: BLE001
                    log.exception("后台作业 %s 结束了, 交回会话时出错", e["job"])
        return any(r is None for _, r in results)
