"""下载队列本身: 任务记录、状态机、调度、存盘. 在下载服务的进程里跑 (daemon.py); 测试里也直接在进程内用.

一个任务的一生:
  登记 -> awaiting (等你点头, 一个字节都不传)
       -> 你点了: queued (排队) -> running (在下) -> done (下完, 哈希核对过) / failed (没成)
       -> 你没同意: denied
  running 时遇到卡片上没写的事 (要转去没列的来源、实际比批的大): 回到 awaiting, 再问你一次 (ask.why = redirect / size)
  发起的会话 (和你) 可以 pause -> paused -> resume, 或者 cancel -> cancelled; failed 的可以 resume 接着试
终态: done / failed / cancelled / denied. 结束了要回到发起的会话报一声的, report 记成 pending, 宿主交出去之前先 ack.

存盘: 一个任务一个文件 <家>/jobs/D12.json (一条坏了不耽误别的: 坏的改名留着, 跳过); 编号在 seq.json, 重启不倒退;
状态一变就写, 字节进度只在检查点写 (transfer.py). 服务重启以后: 在下的回到排队, 从断点接着下; 等点头的照旧等.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

from . import (STATES, TERMINAL, DownloadError, Limits, check_writable, human_size, legacy_report, legacy_text, read_json,
               unwritable_text, write_json)
from . import rules as R
from . import text as TX
from . import transfer as TR

log = logging.getLogger("wm.downloads")

USER = {"kind": "user"}        # 你 (页面、桌宠卡片、QQ 上点的); 会话是 {"kind": "session", "id": 会话编号}
SPEED_WINDOW_S = 5.0
FATAL = {"hash", "too-large", "dest", "disk-full"}  # 换一个地址也没用的: 直接算没成
CLEAN_SCOPES = ("records", "cache", "allWithoutFiles", "all")


def _clip(value, n: int) -> str:
    text = " ".join(str(value or "").split())
    return "".join(c for c in text if ord(c) >= 32)[:n]


class DownloadQueue:
    def __init__(self, home: Path, root: Path, limits: Limits | None = None, *, allow: frozenset = frozenset(),
                 proxy: str | None = None, clock=time.time, free=None):
        self.home, self.root = Path(home), Path(root)
        self.limits = limits or Limits()
        self.allow, self.proxy, self.clock = frozenset(allow), proxy, clock
        self.free = free or TR._free
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.live: dict[str, collections.deque] = {}   # 在下的: 最近几秒收到的 (时刻, 字节), 算速度用
        self.served: dict[str, float] = {}             # 每个会话上一次轮到是什么时候 (几个会话都在排时轮着来)
        self.seq = 1
        self.rev = 1
        self.boot = secrets.token_hex(4)
        self.storage = {"errors": 0, "last": "", "at": 0.0}
        self.dir_error = ""     # 下载文件夹写不进去的原因 (空 = 能写); start() 和每次入队时试写
        self.legacy: dict = {}  # 旧位置 (downloads/queue) 里还有的东西 (只报告, 不动)
        self.http = None
        self.closing = False
        self._waiters: set[asyncio.Future] = set()
        self.last_activity = clock()
        self.load()

    # ---- 存盘 ----
    def load(self) -> None:
        (self.home / "jobs").mkdir(parents=True, exist_ok=True)
        top = 0
        for path in sorted((self.home / "jobs").glob("D*.json")):
            job = read_json(path)
            ok = (job.get("id") == path.stem and job.get("state") in STATES and isinstance(job.get("urls"), list) and job["urls"]
                  and all(isinstance(u, str) for u in job["urls"]) and isinstance(job.get("dest"), str)
                  and isinstance(job.get("hosts"), list) and isinstance(job.get("name"), str))
            if not ok:  # 一条坏了不耽误别的: 改名留着 (你能看), 跳过
                with contextlib.suppress(OSError):
                    path.rename(path.with_name(f"{path.name}.bad-{int(self.clock())}"))
                log.warning("任务记录 %s 读不懂, 改名留着, 跳过", path.name)
                continue
            if job["state"] == "running":  # 上次停的时候正在下: 回到排队, 从断点接着下
                job["state"] = "queued"
            job.pop("speed", None)
            job["rev"] = 1
            self.jobs[job["id"]] = job
            top = max(top, int(job.get("seq") or 0))
        self.seq = max(int(read_json(self.home / "seq.json").get("next") or 1), top + 1)
        self._strand_outside()
        self._summary()

    def _strand_outside(self) -> None:
        """下载位置改过 (2026-11-16 从 downloads/queue 改到 download-queue, 或者配置里改了 downloads.dir): 没下完的任务如果存放位置
        不在现在的下载文件夹里, 传输时 recheck_dest 只会回一句含糊的"落到下载目录外面了". 这里直接标没成, 把话说清楚; 让她重新排
        (会下到新位置). 只改状态, 不碰任何文件 (旧位置的半截文件留着, 不删不搬). 下完的任务不管: 它们的文件还在原地."""
        for job in sorted(self.jobs.values(), key=lambda j: j["seq"]):
            if job["state"] in TERMINAL:
                continue
            try:
                R.recheck_dest(self.root, Path(job["dest"]))
                continue
            except DownloadError:
                pass
            job["error"] = (f"下载位置现在是 {self.root}，这个任务的存放位置 {job['dest']} 不在里面（下载位置改过）；旧位置的半截文件没有动，"
                            f"也没有搬。要下请重新排一个（会下到新位置）。")
            job["error_kind"] = "dest"
            log.warning("任务 %s 的存放位置不在现在的下载文件夹里, 标成没成 (文件没动): %s", job["id"], job["dest"])
            self._finish(job, "failed")

    def _save(self, job: dict) -> None:
        try:
            write_json(self.home / "jobs" / f"{job['id']}.json", {k: v for k, v in job.items() if k not in ("rev", "speed", "eta_s")})
        except OSError as exc:  # 存不上要让人知道: 断点没落盘, 下次续传会从旧位置来
            self.storage.update(errors=self.storage["errors"] + 1, last=f"{job['id']}: {exc}"[:200], at=self.clock())
            log.error("任务 %s 的记录没存上: %s", job["id"], exc)

    def _summary(self) -> None:
        states = [j["state"] for j in self.jobs.values()]
        with contextlib.suppress(OSError):
            write_json(self.home / "summary.json", {
                "active": sum(s in ("queued", "running") for s in states), "awaiting": states.count("awaiting"),
                "unreported": sum(j.get("report") == "pending" for j in self.jobs.values()), "t": self.clock()})

    def _audit(self, op: str, job: dict, actor: dict | None = None, **more) -> None:
        row = {"t": round(self.clock(), 3), "op": op, "id": job["id"], "by": (actor or {}).get("id") or (actor or {}).get("kind", ""),
               **more}
        path = self.home / "audit.jsonl"
        with contextlib.suppress(OSError):
            if path.exists() and path.stat().st_size > 1_000_000:
                os.replace(path, path.with_name("audit.1.jsonl"))
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _touch(self, job: dict) -> None:
        """状态变了: 存盘、记版本、写小结、叫醒在等的."""
        job["updated"] = self.clock()
        self.rev += 1
        job["rev"] = self.rev
        self._save(job)
        self._summary()
        for fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()

    async def _changed(self, timeout: float) -> None:
        """等到有任务变了, 或者到点. (不用 wait_for: Python 3.10 的 wait_for 在"刚好变了"的那一下会把取消吞掉.)"""
        fut = asyncio.get_running_loop().create_future()
        self._waiters.add(fut)
        try:
            await asyncio.wait([fut], timeout=max(0.0, timeout))
        finally:
            self._waiters.discard(fut)
            if not fut.done():
                fut.cancel()

    # ---- 起、停 ----
    async def start(self) -> None:
        # 写不进去不拦着服务起来 (不然看守拉不起来, 只报"起不来", 看不出是文件夹的事): 记下原因, 入队时明确拒绝
        self.dir_error = check_writable(self.root)
        if self.dir_error:
            log.error("下载文件夹 %s 写不进去: %s (不会换到别的地方; 入队会被拒绝)", self.root, self.dir_error)
        self.legacy = legacy_report(self.root)
        if self.legacy:
            log.warning(legacy_text(self.legacy))
        if self.http is None:
            self.http = TR.session(self.limits, self.allow, self.proxy)
        self._pump()

    async def close(self) -> None:
        """服务要停: 在下的先停手 (收到的写实), 记录里照旧是"在下", 下次起来接着下."""
        self.closing = True  # 停手的时候别再开新的
        tasks = list(self.tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.http is not None:
            await self.http.close()
            self.http = None

    def busy(self) -> bool:
        """有在排、在下的 (闲了 idle_exit 秒服务就退). 结束了还没回报的不算: 记录在文件里, 宿主回来再叫醒服务取 (见 wanted)."""
        return any(j["state"] in ("queued", "running") for j in self.jobs.values())

    # ---- 谁能动 ----
    @staticmethod
    def _mine(job: dict, actor: dict) -> bool:
        return actor.get("kind") == "user" or (actor.get("kind") == "session" and bool(actor.get("id"))
                                               and actor["id"] in (job.get("owner"), job.get("origin")))

    def _get(self, jid) -> dict:
        job = self.jobs.get(str(jid or "").strip().upper().replace("#", ""))
        if job is None:
            raise DownloadError("not-found", f"没有编号是 {jid} 的下载 (用 download_status 看有哪些)")
        return job

    def _own(self, jid, actor: dict) -> dict:
        job = self._get(jid)
        if not self._mine(job, actor):
            raise DownloadError("not-owner", f"{job['id']} 是别的会话排的下载: 看得到, 但只有发起它的会话 (和主人) 能暂停、继续、取消")
        return job

    # ---- 登记 ----
    def enqueue(self, args: dict, *, session: str, owner: str = "", owner_title: str = "", run: str = "") -> dict:
        """登记一个下载 (只是登记: 不联网、不查域名). 返回 {job, deduplicated}; 不收的抛 DownloadError."""
        self.last_activity = self.clock()
        if not isinstance(args, dict):
            raise DownloadError("bad-args", "参数要是对象")
        raw = args.get("urls")
        raw = [raw] if isinstance(raw, str) else raw
        if not isinstance(raw, list) or not raw or len(raw) > 4 or not all(isinstance(u, str) for u in raw):
            raise DownloadError("bad-args", "urls 要是 1 到 4 个下载地址")
        urls = list(dict.fromkeys(R.check_url(u, self.allow) for u in raw))
        dest = R.check_dest(self.root, args.get("dest"), urls[0])
        sha = R.check_sha256(args.get("sha256"))
        size = args.get("size")
        if size in (None, "", 0):
            size = None
        elif type(size) is not int or size < 0:
            raise DownloadError("bad-args", "size 要是字节数 (整数); 不知道就不填")
        elif size > self.limits.max_file_bytes:
            raise DownloadError("too-large", f"{human_size(size)} 超过单个文件的上限 {human_size(self.limits.max_file_bytes)} "
                                             "(要下这么大的, 请主人在配置里把 downloads.max_file_gb 调大)")
        extra = args.get("redirect_hosts") or []
        if not isinstance(extra, list) or len(extra) > 6:
            raise DownloadError("bad-args", "redirect_hosts 最多写 6 个域名")
        hosts = list(dict.fromkeys([R.origin(u) for u in urls] + [R.check_host(h, self.allow) for h in extra]))
        name = _clip(args.get("name"), 120) or dest.name
        owner = owner or session
        key = list(R.dedup_key(dest, urls[0]))
        for j in self.jobs.values():  # 同一个文件不下两次: 按落地路径加来源
            if j.get("key", [None])[0] != key[0]:
                continue
            same = j.get("key") == key
            if j["state"] not in TERMINAL:
                if same:
                    return {"job": self.view(j, session), "deduplicated": True}
                raise DownloadError("dest-in-use", f"{j['id']} 正要往这个位置下另一个来源的文件: 换个名字, 或者等它结束")
            if j["state"] == "done" and same and self._intact(j):
                return {"job": self.view(j, session), "deduplicated": True}
        if dest.exists():
            raise DownloadError("dest-exists", "这个位置已经有文件了, 不覆盖: 换个名字存, 或者请主人先处理掉原来那个")
        open_jobs = [j for j in self.jobs.values() if j["state"] not in TERMINAL]
        if len(open_jobs) >= self.limits.max_open:
            raise DownloadError("too-many", f"队列里没结束的已经有 {len(open_jobs)} 个了, 等一些结束再排")
        waiting = sum(j["state"] == "awaiting" and j.get("owner") == owner for j in open_jobs)
        if waiting >= self.limits.max_waiting:
            raise DownloadError("too-many", f"这个会话等主人点头的下载已经有 {waiting} 个了: 等主人答复了再排新的")
        self.dir_error = check_writable(self.root)  # 真试写: 修好权限不用重启, 没修好每次都明说
        if self.dir_error:
            raise DownloadError("dir-unwritable", unwritable_text(self.root, self.dir_error))
        try:
            left = self.free(self.root)
        except OSError as exc:
            raise DownloadError("dest-invalid", f"下载目录用不了: {str(exc)[:160]}") from None
        queued = sum(self._remaining(j) for j in open_jobs)
        mine = size or self.limits.unknown_ok_bytes
        if left < self.limits.reserve_bytes + queued + mine:  # 一个个单看都够, 加起来写满盘
            raise DownloadError("disk-full", f"磁盘空间不够: 要留 {human_size(self.limits.reserve_bytes)}, 队列里还要 {human_size(queued)}, "
                                             f"这个要 {human_size(mine)}{'' if size else ' (大小未知, 按这个数估)'}, 现在只剩 {human_size(left)}")
        now = self.clock()
        jid = f"D{self.seq}"
        job = {"v": 1, "id": jid, "seq": self.seq, "name": name, "urls": urls, "hosts": hosts, "dest": str(dest),
               "sha256": sha, "size_declared": size, "license": _clip(args.get("license"), 80), "note": _clip(args.get("note"), 200),
               "route": "direct" if args.get("direct") is True else "auto",
               "owner": owner, "origin": session, "owner_title": _clip(owner_title, 60), "run": str(run or "")[:64],
               "state": "awaiting", "ask": {"seq": 1, "why": "new", "card": f"dl-{jid}-1", "shown": False, "asked": now},
               "approvals": [], "approved_bytes": 0, "pause_by": None, "created": now, "updated": now, "ended": None,
               "total": -1, "done": 0, "checkpoint": 0, "url_index": 0, "part": {}, "error": "", "error_kind": "",
               "final": None, "report": "none", "key": key}
        self.seq += 1
        with contextlib.suppress(OSError):
            write_json(self.home / "seq.json", {"next": self.seq})
        self.jobs[jid] = job
        self._audit("enqueue", job, {"kind": "session", "id": session}, urls=urls, dest=str(dest))
        self._touch(job)
        return {"job": self.view(job, session), "deduplicated": False}

    def _remaining(self, job: dict) -> int:
        total = job["total"] if job.get("total", -1) >= 0 else (job.get("size_declared") or self.limits.unknown_ok_bytes)
        return max(0, int(total) - int(job.get("done") or 0))

    @staticmethod
    def _intact(job: dict) -> bool:
        """下完的那个文件还在不在 (是文件、不是空的、大小没变)."""
        try:
            return Path(job["dest"]).is_file() and Path(job["dest"]).stat().st_size == int((job.get("final") or {}).get("bytes") or -1) > 0
        except OSError:
            return False

    # ---- 你的答复 ----
    def find_card(self, card: str) -> dict | None:
        return next((j for j in self.jobs.values() if j["state"] == "awaiting" and (j.get("ask") or {}).get("card") == card), None)

    def answer(self, ref: str, ok: bool, via: str = "") -> dict:
        """你点头 / 不同意 (只有宿主替你转过来的才走到这里; 她的工具里没有这一条). ref: 卡片编号或任务编号."""
        self.last_activity = self.clock()
        job = self.find_card(str(ref or "")) or self._get(ref)
        if job["state"] != "awaiting":
            raise DownloadError("bad-state", f"{job['id']} 现在不在等点头 ({TX.STATE_TEXT.get(job['state'], job['state'])})")
        ask, now = job["ask"] or {}, self.clock()
        job["approvals"].append({"seq": ask.get("seq"), "why": ask.get("why"), "ok": bool(ok), "at": now, "via": _clip(via, 20),
                                 **{k: ask[k] for k in ("host", "total") if k in ask}})
        self._audit("approve" if ok else "deny", job, USER, why=ask.get("why"), via=via)
        if not ok:
            job["ask"] = None
            self._drop_part(job)
            self._finish(job, "denied")
            return self.view(job)
        if ask.get("why") == "redirect" and ask.get("host") and ask["host"] not in job["hosts"]:
            job["hosts"].append(ask["host"])
        elif ask.get("why") == "size":
            job["approved_bytes"] = self.limits.max_file_bytes if ask.get("open") else int(ask.get("total") or 0)
        elif ask.get("why") == "new":
            job["approved_bytes"] = int(job.get("size_declared") or 0)  # 0 = 卡片上写的"未知" (按 unknown_ok_bytes 算)
            job["approved_at"] = now
        job["ask"], job["state"], job["error"], job["error_kind"] = None, "queued", "", ""
        self._touch(job)
        self._pump()
        return self.view(job)

    def shown(self, jid, seq) -> None:
        """宿主已经把这一问的卡片放进会话了 (重启以后不再放一遍)."""
        job = self._get(jid)
        if job.get("ask") and job["ask"].get("seq") == seq and not job["ask"].get("shown"):
            job["ask"]["shown"] = True
            self._save(job)

    def _ask(self, job: dict, why: str, **data) -> None:
        seq = len(job["approvals"]) + 1
        job["ask"] = {"seq": seq, "why": why, "card": f"dl-{job['id']}-{seq}", "shown": False, "asked": self.clock(), **data}
        job["state"] = "awaiting"
        self._audit("ask", job, None, why=why, **{k: v for k, v in data.items() if k in ("host", "from", "total")})
        self._touch(job)

    # ---- 暂停、继续、取消 ----
    async def _halt(self, job: dict) -> None:
        task = self.tasks.get(job["id"])
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def pause(self, jid, actor: dict) -> dict:
        self.last_activity = self.clock()
        job = self._own(jid, actor)
        if job["state"] not in ("queued", "running"):
            raise DownloadError("bad-state", f"{job['id']} 现在{TX.STATE_TEXT.get(job['state'], job['state'])}, 没有在下, 不用暂停")
        await self._halt(job)
        if job["state"] in TERMINAL or job["state"] == "awaiting":  # 停手的时候它刚好有了结果
            return self.view(job, actor.get("id", ""))
        job["state"], job["pause_by"] = "paused", "user" if actor.get("kind") == "user" else "owner"
        self._audit("pause", job, actor)
        self._touch(job)
        self._pump()
        return self.view(job, actor.get("id", ""))

    def resume(self, jid, actor: dict) -> dict:
        self.last_activity = self.clock()
        job = self._own(jid, actor)
        if job["state"] not in ("paused", "failed"):
            raise DownloadError("bad-state", f"{job['id']} 现在{TX.STATE_TEXT.get(job['state'], job['state'])}, 不是暂停或没成的, 不用继续")
        if job["state"] == "paused" and job.get("pause_by") == "user" and actor.get("kind") != "user":
            raise DownloadError("not-owner", f"{job['id']} 是主人暂停的, 只有主人能让它继续")
        if job["state"] == "failed" and job.get("error_kind") in ("hash", "private", "dest"):
            raise DownloadError("bad-state", f"{job['id']} 是因为「{job.get('error')}」没成的, 接着下也一样: 要下就重新排一个")
        if job["state"] == "failed":
            job.update(url_index=0, error="", error_kind="", ended=None, report="none")
        job["state"], job["pause_by"] = "queued", None
        self._audit("resume", job, actor)
        self._touch(job)
        self._pump()
        return self.view(job, actor.get("id", ""))

    async def cancel(self, jid, actor: dict) -> dict:
        self.last_activity = self.clock()
        job = self._own(jid, actor)
        if job["state"] in TERMINAL:
            raise DownloadError("bad-state", f"{job['id']} 已经{TX.STATE_TEXT.get(job['state'], job['state'])}, 不用取消")
        await self._halt(job)
        if job["state"] in TERMINAL:
            return self.view(job, actor.get("id", ""))
        job["ask"] = None
        self._drop_part(job)
        self._audit("cancel", job, actor)
        # 她自己取消的不用再回报给她; 你取消的要告诉发起的会话
        self._finish(job, "cancelled", report=actor.get("kind") == "user")
        return self.view(job, actor.get("id", ""))

    def _drop_part(self, job: dict) -> int:
        part = TR.part_of(job["dest"])
        try:
            size = part.stat().st_size
            part.unlink()
        except OSError:
            return 0
        job.update(part={}, checkpoint=0, done=0)
        return size

    def _finish(self, job: dict, state: str, report: bool = True) -> None:
        job["state"], job["ended"] = state, self.clock()
        job["report"] = "pending" if report else "none"
        job.pop("phase", None)
        self._touch(job)
        self._trim()

    def _trim(self) -> None:
        done = sorted((j for j in self.jobs.values() if j["state"] in TERMINAL and j.get("report") != "pending"),
                      key=lambda j: j.get("ended") or 0)
        for j in done[:max(0, len(done) - self.limits.keep_records)]:
            self._forget(j)

    def _forget(self, job: dict) -> None:
        self.jobs.pop(job["id"], None)
        with contextlib.suppress(OSError):
            (self.home / "jobs" / f"{job['id']}.json").unlink()

    # ---- 调度 ----
    def _pump(self) -> None:
        """有空位就开下一个. 几个会话都在排时轮着来 (手上在下的少的先), 不让一个会话占满."""
        if self.http is None or self.closing:
            return
        while True:
            running = [j for j in self.jobs.values() if j["id"] in self.tasks]
            if len(running) >= self.limits.max_active:
                return
            queued = [j for j in self.jobs.values() if j["state"] == "queued" and j["id"] not in self.tasks]
            if not queued:
                return
            load = collections.Counter(j.get("owner") for j in running)
            job = min(queued, key=lambda j: (load[j.get("owner")], self.served.get(j.get("owner"), 0.0), j["seq"]))
            job["state"], job["started"] = "running", job.get("started") or self.clock()
            self.served[job.get("owner")] = time.monotonic()
            self.live[job["id"]] = collections.deque()
            task = self.tasks[job["id"]] = asyncio.get_running_loop().create_task(self._run(job))
            # 收尾放在这里, 不放在协程的 finally 里: 任务还没开始跑就被取消 (刚排上就暂停) 的话, 协程一行都不会执行
            task.add_done_callback(lambda t, jid=job["id"]: self._ended(jid, t))
            self._touch(job)

    def _ended(self, jid: str, task: asyncio.Task) -> None:
        if self.tasks.get(jid) is task:
            del self.tasks[jid]
            self.live.pop(jid, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("下载 %s 的任务意外结束: %r", jid, task.exception())
        if self.http is not None:
            self._pump()

    def _progress(self, jid: str, n: int) -> None:
        q = self.live.get(jid)
        if q is not None:
            now = time.monotonic()
            q.append((now, n))
            while q and now - q[0][0] > SPEED_WINDOW_S:
                q.popleft()
        self.last_activity = self.clock()

    async def _run(self, job: dict) -> None:
        jid, fails, delays = job["id"], 0, self.limits.retry_delays
        try:
            while True:
                before = int(job.get("done") or 0)
                try:
                    job.pop("phase", None)
                    await TR.fetch(job, self.root, http=self.http, limits=self.limits, allow=self.allow, proxy=self.proxy,
                                   progress=lambda n: self._progress(jid, n), checkpoint=lambda: self._save(job), free=self.free)
                    job["phase"] = "verifying"
                    fut = asyncio.ensure_future(asyncio.to_thread(TR.finalize, job, self.root, self.limits))
                    try:
                        final = await asyncio.shield(fut)
                    except asyncio.CancelledError:  # 已经在收尾了 (核对、改名): 等它做完, 结果照算
                        final = await fut
                except TR.Ask as ask:
                    self._ask(job, ask.why, **ask.data)
                    return
                except TR.TransferError as exc:
                    job["error"], job["error_kind"] = str(exc), exc.kind
                    fails = 1 if int(job.get("done") or 0) > before else fails + 1  # 这一次有进展就重新数
                    self._audit("error", job, None, kind=exc.kind, text=str(exc)[:200], url=job["urls"][job["url_index"]])
                    if exc.kind not in FATAL:
                        if exc.retry and fails <= len(delays):
                            self._save(job)
                            await asyncio.sleep(delays[fails - 1])
                            continue
                        if job["url_index"] + 1 < len(job["urls"]):  # 这个地址不行: 换卡片上的下一个 (从头下)
                            job["url_index"] += 1
                            fails = 0
                            continue
                    self._finish(job, "failed")
                    return
                job["final"], job["error"], job["error_kind"] = final, "", ""
                job["done"] = job["total"] = final["bytes"]
                self._audit("done", job, None, bytes=final["bytes"], sha256=final["sha256"], verified=final["verified"])
                self._finish(job, "done")
                return
        except asyncio.CancelledError:  # 暂停、取消、服务要停: 状态由叫停的那边改, 这里只把进度存下
            self._save(job)
            raise
        except Exception as exc:  # noqa: BLE001 (没想到的错: 算这个任务没成, 不带垮整个队列)
            log.exception("下载 %s 出了没想到的错", jid)
            job["error"], job["error_kind"] = f"程序出错: {type(exc).__name__}: {str(exc)[:160]}", "internal"
            self._finish(job, "failed")

    # ---- 看 ----
    def view(self, job: dict, viewer: str = "") -> dict:
        q = self.live.get(job["id"])
        job.pop("speed", None)
        job.pop("eta_s", None)
        if job["state"] == "running" and q:
            span = max(1.0, min(SPEED_WINDOW_S, time.monotonic() - q[0][0]))
            job["speed"] = int(sum(n for _, n in q) / span)
            if job["speed"] > 0 and job.get("total", -1) > 0:
                job["eta_s"] = int((job["total"] - job.get("done", 0)) / job["speed"])
        out = TX.view(job, viewer)
        if job.get("phase") == "verifying" and job["state"] == "running":
            out["text"] = f"{job['id']}「{job['name'][:40]}」收全了, 在核对"
        return out

    def status(self, jid=None, viewer: str = "") -> dict:
        """不给编号: 没结束的全部 + 最近结束的 20 个 (所有会话的都看得到). 发起的会话看到自己的结果了, 就不用再回报."""
        self.last_activity = self.clock()
        if jid:
            rows = [self._get(jid)]
        else:
            rows = sorted(self.jobs.values(), key=lambda j: -j["seq"])
            rows = [j for j in rows if j["state"] not in TERMINAL] + [j for j in rows if j["state"] in TERMINAL][:20]
        for j in rows:
            if viewer and j.get("report") == "pending" and viewer in (j.get("owner"), j.get("origin")):
                j["report"] = "acked"
                self._touch(j)
        out = {"jobs": [self.view(j, viewer) for j in rows], "dir": str(self.root)}
        if self.dir_error:  # 有错才重试 (修好就消失); 没错不每次都去试写
            self.dir_error = check_writable(self.root)
        if self.dir_error:
            out["dir_error"] = self.dir_error
        if self.legacy:
            out["legacy"] = dict(self.legacy)
        if self.storage["errors"]:
            out["storage"] = dict(self.storage)
        return out

    async def wait(self, jid, timeout: float = 30.0, viewer: str = "") -> dict:
        """等一个下载有结果 (结束了, 或者下到一半又要你点头), 最多 timeout 秒."""
        job = self._get(jid)
        end = time.monotonic() + max(0.0, min(float(timeout or 0), 300.0))
        moved = job["state"] in ("queued", "running")
        while job["state"] not in TERMINAL and not (moved and job["state"] == "awaiting"):
            left = end - time.monotonic()
            if left <= 0:
                break
            await self._changed(min(left, 5.0))
            moved = moved or job["state"] in ("queued", "running")
        return self.status(job["id"], viewer)["jobs"][0]

    # ---- 宿主盯着的 ----
    async def changes(self, since: int = 0, boot: str = "", timeout: float = 20.0) -> dict:
        """宿主长等着的: 版本号 since 之后变过的任务 (第一次来、或者服务重启过: 所有没结束的和没回报的).
        每个带上卡片和回报要用的字 (宿主不用自己拼)."""
        fresh = boot != self.boot or not since
        if not fresh and since >= self.rev and timeout > 0:
            await self._changed(min(float(timeout), 25.0))
        if fresh:
            rows = [j for j in self.jobs.values() if j["state"] not in TERMINAL or j.get("report") == "pending"]
        else:
            rows = [j for j in self.jobs.values() if j.get("rev", 0) > since]
        out = []
        for j in sorted(rows, key=lambda j: j["seq"]):
            row = {**self.view(j), "origin": j.get("origin", ""), "run": j.get("run", ""), "report": j.get("report", "none")}
            if j["state"] == "awaiting":
                row["ask"] = dict(j["ask"])
                row["card_text"] = TX.card(j, self.limits, self.root, self.proxy)
                row["card_short"] = TX.card_short(j, self.limits)
            if j["state"] in TERMINAL:
                row["report_info"] = TX.report(j)
            out.append(row)
        return {"rev": self.rev, "boot": self.boot, "jobs": out}

    def ack(self, jid) -> bool:
        """宿主要把结果交回会话了: 先记成交过 (宁可漏报一次, 不重复交). 返回这一次是不是真的从"没交"改成"交过" ——
        不是 (她刚好自己 download_wait / download_status 看到了结果) 宿主就不用再交了."""
        job = self._get(jid)
        if job.get("report") != "pending":
            return False
        job["report"] = "acked"
        self._touch(job)
        self._trim()
        return True

    # ---- 清理 ----
    def cleanup(self, scope: str, actor: dict) -> dict:
        """清已经结束的 (只清这个会话排的; 你清的是全部). records 只清记录; cache 只清没下完的半截文件; allWithoutFiles 两样都清
        (下好的文件留着); all 连下好的文件一起删."""
        self.last_activity = self.clock()
        scope = scope or "allWithoutFiles"
        if scope not in CLEAN_SCOPES:
            raise DownloadError("bad-args", "scope 只能是 " + " / ".join(CLEAN_SCOPES))
        out = {"scope": scope, "removed_records": 0, "removed_cache_files": 0, "removed_files": 0, "freed_bytes": 0}
        for job in [j for j in self.jobs.values() if j["state"] in TERMINAL and self._mine(j, actor)]:
            if job.get("report") == "pending":  # 结果还没交回发起的会话: 先留着
                continue
            if scope in ("cache", "allWithoutFiles", "all") and job["state"] != "done":
                freed = self._drop_part(job)
                if freed:
                    out["removed_cache_files"] += 1
                    out["freed_bytes"] += freed
                    self._save(job)
            if scope == "all" and job["state"] == "done" and self._intact(job):
                try:
                    R.recheck_dest(self.root, Path(job["dest"]))
                    size = Path(job["dest"]).stat().st_size
                    Path(job["dest"]).unlink()
                    out["removed_files"] += 1
                    out["freed_bytes"] += size
                except (OSError, DownloadError):
                    pass
            if scope != "cache":
                self._forget(job)
                out["removed_records"] += 1
        self._summary()
        return out
