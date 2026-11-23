"""下载队列在工作台这边的一半 (服务本身在 wm/downloads/, 是另一个进程): 她的七个工具、问你的卡片、下完回到发起的会话.

- 工具照 DLS 的七个: download_enqueue / status / wait / pause / resume / cancel / cleanup. 入队只是登记, 马上返回编号.
- 问你: 队列里一有"等点头"的, 就在发起的会话里放一张卡片 (和别的申请一个样子: 允许一次 / 拒绝), 桌宠对话栏也弹一张,
  15 秒没人点上 QQ / 微信问 (渠道管理器的 agent_ask, 不过期). 卡片写名称、来源、大小、许可. 你点了, 才由这里告诉下载服务"可以传了"
  —— 她的工具里没有这一条, 哪个权限档 (完全授权也一样) 都绕不过去. 服务端重启以后卡片照样能点 (认卡片编号, 不靠内存里的东西).
- 回报: 盯着下载服务的变化 (有活的时候才盯), 结束了的先记成"交过"再交给 AgentHub.job_done —— 和后台作业同一条路
  (这一轮还在做: 下一步开头交给她; 起它的那一轮正常做完了: 接着做一轮看结果; 不然只记一笔).
- 子任务排的下载算在最上面那个会话名下 (卡片和回报都去那里), 子任务自己和它都能停.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from .. import downloads as DQ
from ..downloads import DownloadError
from ..downloads import rules as R
from ..downloads.client import Client
from . import policy as P

log = logging.getLogger("wm.agent.downloads")
IDLE_S = 60.0      # 没有要盯的下载时多久醒一次 (排了新的会马上叫醒)
POLL_S = 20.0      # 有要盯的: 一次长等多久
REPORT_GRACE_S = 60.0  # 服务端刚起来、还没有能用的模型时, 回报最多先等这么久
WHY = "下载先问：你点了才开始传"
USER = {"kind": "user"}


def _spec(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}}


_ID = {"id": {"type": "string", "description": "下载编号，比如 D3"}}
TOOL_SPECS = [
    _spec("download_enqueue",
          "把一个文件排进下载队列。要把文件（模型、安装包、数据集）下到本机就用它，不要用 curl / Invoke-WebRequest / 脚本自己去下。"
          "这一步只是登记，马上返回编号：主人在卡片上点头以后才开始传，下完会自己回来告诉你，排完不用一直等。"
          "卡片上写名称、来源、大小、许可：大小和许可先查清楚再填，查不到就不填（卡片写“未知”），不要编。"
          "只下公网的 http/https，只存进下载目录，下完不运行、不解压。来源只连你列出来的："
          "会跳转到别的域名的（GitHub Releases 转到 release-assets.githubusercontent.com，Hugging Face 的大文件转到它自己的 CDN），"
          "知道就写进 redirect_hosts，没写的到时候会停下再问主人一次。同一个位置、同一个来源已经在下或下过的，返回原来那个。",
          {"urls": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4,
                    "description": "下载地址；写多个时后面的是备用来源，按顺序试，都会写在卡片上（换镜像也要写在这里让主人看到）"},
           "name": {"type": "string", "description": "给主人看的名称：是什么、哪个版本"},
           "dest": {"type": "string", "description": "存成什么：下载目录里的相对路径（比如 kokoro/model.onnx）；不填用网址最后一段。下载目录在哪看 download_status 返回的 dir"},
           "sha256": {"type": "string", "description": "官方给的 SHA-256；有就填，下完必须对上，对不上会删掉"},
           "size": {"type": "integer", "description": "你查到的字节数；不知道就不填"},
           "license": {"type": "string", "description": "你查到的许可（比如 MIT、Apache-2.0）；不知道就不填"},
           "redirect_hosts": {"type": "array", "items": {"type": "string"}, "maxItems": 6, "description": "下载时会转去的别的域名，知道才填"},
           "note": {"type": "string", "description": "一句话：为什么要下"},
           "direct": {"type": "boolean", "description": "不走代理直连（国内的来源直连更快时用）"}}, ["urls", "name"]),
    _spec("download_status", "看下载队列。不给 id：列出没结束的和最近结束的（所有会话排的都看得到，mine=true 是这个会话排的）；给 id：看那一个的进度。", _ID),
    _spec("download_wait", "等一个下载有结果（下完、没成、取消、被拒绝，或者下到一半又要主人点头），最长 timeout_s 秒（默认 30，最多 300）。"
          "还在等主人点头的不用一直等：这一轮可以先结束，下完会自己回来告诉你。",
          {**_ID, "timeout_s": {"type": "number", "minimum": 0, "maximum": 300}}, ["id"]),
    _spec("download_pause", "暂停这个会话排的一个下载（别的会话排的看得到、动不了）。", _ID, ["id"]),
    _spec("download_resume", "让暂停的下载接着下；没成的（断线、服务器出错）也用它从断的地方接着试。", _ID, ["id"]),
    _spec("download_cancel", "取消这个会话排的一个下载（没下完的半截文件会删掉）。", _ID, ["id"]),
    _spec("download_cleanup", "清理这个会话排的、已经结束的下载。scope：records 只清记录；cache 只清没下完的半截文件；"
          "allWithoutFiles（默认）两样都清，下好的文件留着；all 连下好的文件一起删（要主人点头）。",
          {"scope": {"type": "string", "enum": ["records", "cache", "allWithoutFiles", "all"]}}),
]
NAMES = {x["function"]["name"] for x in TOOL_SPECS}
READ_ONLY = {"download_status", "download_wait"}
LABELS = {"download_enqueue": "排下载", "download_status": "看下载队列", "download_wait": "等下载", "download_pause": "暂停下载",
          "download_resume": "继续下载", "download_cancel": "取消下载", "download_cleanup": "清理下载记录"}

# 照《底层终端（插件宿主）方案》的清单格式写的声明 (现在宿主还不读清单; 以后挂到"服务型插件"下面时, 这就是它的那一份.
# tests/test_agent_downloads.py 核对这里列的工具和上面真正给她的对得上).
MANIFEST = {
    "id": "dl-queue", "name": "下载队列", "description": "常驻的下载队列：排队、断点续传、哈希核对；每次下载先问主人。",
    "plugin": {
        "schema": 2, "version": "0.1.0", "author": "WorldModel", "license": "随 WorldModel 本体", "tier": "process",
        "tools": [{"name": "download_enqueue", "risk": "write", "why": "登记一个下载；主人点头以后把文件写进下载目录"},
                  {"name": "download_status", "risk": "read", "why": "看队列"},
                  {"name": "download_wait", "risk": "read", "why": "等一个下载有结果"},
                  {"name": "download_pause", "risk": "write", "why": "暂停自己排的下载"},
                  {"name": "download_resume", "risk": "write", "why": "继续自己排的下载"},
                  {"name": "download_cancel", "risk": "write", "why": "取消自己排的下载，删掉半截文件"},
                  {"name": "download_cleanup", "risk": "write", "why": "清记录和半截文件；scope=all 时删下好的文件（要点头）"}],
        "permissions": {"network": "ask-each", "local": [], "files": [{"path": "$DOWNLOADS", "mode": "rw"}], "accounts": [],
                        "notify": True, "model": None, "upload": "never", "elevated": []},
        "service": {"cmd": ["python", "-m", "wm.downloads", "serve"], "health": "/health", "start": "on-demand"},
        "panel": None, "schedule": [], "world": None, "components": [],
        "settings": [{"key": "dir", "type": "directory", "title": "下载到哪（默认：桌宠文件夹里的 download-queue）"},
                     {"key": "max_file_gb", "type": "number", "title": "单个文件最多多大 (GB)"},
                     {"key": "reserve_gb", "type": "number", "title": "下完以后盘上至少留多少 (GB)"}]},
}


def shown(name: str, args: dict) -> str:
    """页面上那一行工具记录写什么 (studio.execute 默认只认 id / 目标 / 名字, 排下载这几样都没有): 名称 ← 来源."""
    if name != "download_enqueue":
        return ""
    urls = args.get("urls")
    first = urls if isinstance(urls, str) else (urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], str) else "")
    try:
        host = R.origin(first) if first else ""
    except ValueError:
        host = ""
    title = " ".join(str(args.get("name") or "").split())[:80]
    return f"{title} ← {host}" if title and host else title or host


def decision(hub, s, name: str, args: dict):
    """工作台权限. 入队只是登记, 这里不拦也不问: 真正的闸是队列自己的那张卡片 (哪个权限档都要你点头, 完全授权也一样).
    连下好的文件一起删 (cleanup 的 all) 和别的"删东西"一样先问. 存放位置落在你拉黑的路径里的不收."""
    if name == "download_cleanup" and str(args.get("scope") or "") == "all" and hub.teams.effective_mode(s) != "unrestricted":
        return P.Decision("ask", "会把这个会话下好的文件一起删掉 (删东西每次都问)", "power")
    if name == "download_enqueue":
        urls = args.get("urls")
        first = urls if isinstance(urls, str) else (urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], str) else "")
        with contextlib.suppress(DownloadError, TypeError, ValueError):
            dest = R.check_dest(hub.downloads.client.root, args.get("dest"), first)
            if (denied := hub.blacklist.check("write_file", {"path": str(dest)}, Path(s.workspace))):
                return denied
    return P.Decision("auto")


def _brief(job: dict, full: bool = False) -> dict:
    """给她看的一个任务 (省着点字)."""
    out = {"id": job["id"], "state": job["state"], "text": job["text"], "mine": job.get("mine", False)}
    if not out["mine"] and job.get("owner_title"):
        out["by"] = job["owner_title"]
    if job["state"] == "done":
        out["path"] = job["dest"]
        out["sha256"] = (job.get("final") or {}).get("sha256", "")
    if full:
        out.update(name=job["name"], urls=job["urls"], dest=job["dest"], total=job.get("total", -1), done=job.get("done", 0))
    return out


async def call_tool(hub, s, name: str, args: dict):
    """她调下载工具 (StudioRuntime.call 转过来)."""
    dl: Downloads = hub.downloads
    actor = {"kind": "session", "id": s.id}
    try:
        if name == "download_enqueue":
            return await dl.enqueue(s, args)
        if name == "download_status":
            got = await dl.client.rpc("status", id=args.get("id") or None, viewer=s.id)
            return {"jobs": [_brief(j, full=bool(args.get("id"))) for j in got["jobs"]], "dir": got["dir"],
                    **({"dir_error": got["dir_error"]} if got.get("dir_error") else {}),
                    **({"old_dir_note": DQ.legacy_text(got["legacy"])} if got.get("legacy") else {}),
                    **({"storage": got["storage"]} if got.get("storage") else {})}
        if name == "download_wait":
            return await dl.wait(s, args.get("id"), args.get("timeout_s"))
        if name in ("download_pause", "download_resume", "download_cancel"):
            got = await dl.client.rpc(name.removeprefix("download_"), id=args.get("id"), actor=actor)
            await dl.sync()  # 取消的: 把还挂着的卡片收掉
            return _brief(got["job"])
        if name == "download_cleanup":
            return await dl.client.rpc("cleanup", scope=str(args.get("scope") or ""), actor=actor)
    except DownloadError as exc:
        raise ValueError(str(exc)) from None
    raise ValueError("没有这个下载工具")


class Downloads:
    """服务端一个 (AgentHub.downloads). 平时什么都不做: 有下载在排、在下、没回报时才盯着下载服务."""

    def __init__(self, hub):
        self.hub = hub
        conf = hub.cfg.get("downloads") if isinstance(hub.cfg.get("downloads"), dict) else {}
        self.client = Client(DQ.home_for(hub.home), DQ.dir_for(conf), conf)
        self.cards: dict[str, dict] = {}     # 卡片编号 -> {job, session, did (桌宠对话栏那张)}
        self.handed: set[str] = set()        # 这次启动里已经交回会话的 (同一个结果不交两次)
        self.later: dict[str, dict] = {}     # 结束了、先等一等再交的 (服务端刚起来, 还没有能用的模型)
        self.grace = REPORT_GRACE_S
        self.started = time.monotonic()
        self.rev, self.boot = 0, ""
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self._busy: asyncio.Lock | None = None
        self._first = True
        self._stop = False

    # ---- 起、停 ----
    def start(self) -> None:
        if self.task is None:
            self._stop = False
            self.started = time.monotonic()
            self.task = asyncio.get_running_loop().create_task(self._loop())

    async def close(self) -> None:
        """服务端要停: 只停这边的盯梢; 下载服务是另一个进程, 接着下."""
        self._stop = True
        if self.task is not None:
            while not self.task.done():  # 取消一次不一定到得了 (正好赶上它被叫醒): 到它真的停下为止
                self.task.cancel()
                await asyncio.wait([self.task], timeout=1.0)
            self.task = None
        await self.client.close()

    async def _nap(self, seconds: float) -> None:
        """睡到被叫醒 (wake) 或者到点. 不用 wait_for: Python 3.10 的 wait_for 在"刚好被叫醒"的那一下会把取消吞掉."""
        waiter = asyncio.ensure_future(self.wake.wait())
        try:
            await asyncio.wait([waiter], timeout=seconds)
        finally:
            waiter.cancel()

    async def _loop(self) -> None:
        fails = 0
        while not self._stop:
            try:
                s = DQ.summary(self.client.home)
                if not (s["active"] or s["unreported"] or (self._first and s["awaiting"]) or self.later):
                    self.wake.clear()
                    await self._nap(IDLE_S)
                    continue
                await self.sync(wait_s=2.0 if self.later else POLL_S)
                if self.later and (self.hub.models() or time.monotonic() - self.started >= self.grace):
                    for job in list(self.later.values()):
                        await self._report(job)
                fails = 0
            except DownloadError as exc:  # 下载服务起不来、没应答: 隔得越来越久再试 (最多一分钟一次), 别刷屏
                fails += 1
                if fails in (1, 5) or fails % 30 == 0:
                    log.warning("盯下载队列时 (第 %s 次): %s", fails, exc)
                await asyncio.sleep(min(60.0, 3.0 * 2 ** min(fails, 5)))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("盯下载队列时出错")
                await asyncio.sleep(30.0)

    # ---- 她的工具 ----
    def root_session(self, s):
        """子任务排的下载算在最上面那个会话名下."""
        seen = set()
        while s.parent_id and s.id not in seen:
            seen.add(s.id)
            parent = self.hub.get(s.parent_id)
            if parent is None:
                break
            s = parent
        return s

    async def enqueue(self, s, args: dict) -> dict:
        owner = self.root_session(s)
        got = await self.client.rpc("enqueue", args=args, session=s.id, owner=owner.id, owner_title=owner.title,
                                    run=owner.run.get("id", ""))
        job = got["job"]
        if got.get("deduplicated"):
            note = "这个文件已经下好了，没有重下。" if job["state"] == "done" else \
                "这个位置、这个来源已经有一个在队列里了（没有再排一个）；它" + ("是这个会话排的。" if job["mine"] else "是别的会话排的：你看得到、等得了，但停不了它。")
            return {**_brief(job), "deduplicated": True, "note": note}
        await self.sync()  # 卡片马上弹出去
        self.wake.set()
        return {**_brief(job), "note": "已经登记，在等主人点头（卡片弹出去了）。主人点了才开始传，下完会自己回来告诉你；"
                                       "不用一直 download_wait，这一轮可以先做别的或者结束。"}

    async def wait(self, s, jid, timeout_s) -> dict:
        try:
            seconds = max(0.0, min(float(30 if timeout_s is None else timeout_s), 300.0))
        except (TypeError, ValueError):
            seconds = 30.0
        call = asyncio.ensure_future(self.client.rpc("wait", timeout=seconds + 15, id=jid, wait_s=seconds, viewer=s.id))
        stop = asyncio.ensure_future(s.stop_event.wait())
        try:
            await asyncio.wait([call, stop], return_when=asyncio.FIRST_COMPLETED)
            if not call.done():
                raise ValueError("主人停掉了，没有等完（下载本身不受影响）")
            job = call.result()["job"]
        finally:
            for t in (call, stop):
                if not t.done():
                    t.cancel()
        out = _brief(job, full=True)
        if job["state"] == "awaiting":
            out["note"] = "还在等主人点头。不用一直等：这一轮可以先结束，下完会自己回来告诉你。"
        elif job["state"] in ("queued", "running"):
            out["note"] = "还没下完。要接着等就再调一次；也可以先做别的，下完会自己回来告诉你。"
        return out

    # ---- 卡片 ----
    @staticmethod
    def owns(card: str) -> bool:
        """这个申请编号是不是下载卡片的 (AgentHub.answer 认不得的申请, 是我们的就转过来)."""
        return str(card or "").startswith("dl-D")

    def answer_soon(self, card: str, ok: bool, via: str = "page") -> None:
        asyncio.ensure_future(self.answer(card, ok, via))

    async def answer(self, card: str, ok: bool, via: str = "page") -> bool:
        """你在卡片上点了 (页面、桌宠、QQ / 微信都到这里). 只有这里会告诉下载服务"可以传了"."""
        try:
            await self.client.rpc("answer", ref=card, ok=bool(ok), via=via)
        except DownloadError as exc:
            log.info("下载卡片 %s 的答复没用上: %s", card, exc)
            await self._close_card(card, "stopped", f"这张卡片已经不用答了（{str(exc)[:80]}）")
            return False
        await self._close_card(card, "once" if ok else "deny", "主人点头了，开始下" if ok else "主人没同意，没有下")
        self.wake.set()
        with contextlib.suppress(DownloadError):
            await self.sync()
        return True

    async def _card(self, job: dict) -> None:
        ask, hub = job["ask"], self.hub
        cid = ask["card"]
        self.cards[cid] = {"job": job["id"], "session": job["owner"], "did": None}
        s = hub.get(job["owner"])
        if s is not None and not self._has(s, "ask", cid):  # 会话里放一张 (存进会话记录; 服务端重启以后还在的不重放)
            await hub.emit(s, {"type": "tool", "id": cid, "name": "download_approval", "label": "下载文件",
                               "args": f"{job['id']} {job['name']}"[:300], "state": "asking", "why": WHY})
            await hub.emit(s, {"type": "ask", "id": cid, "label": "下载文件", "text": job["card_text"][:1600], "why": WHY,
                               "risk": "power", "turn_ok": False})
            if s.status == "idle":
                s.unread = True
            hub.save(s)
            with contextlib.suppress(DownloadError):
                await self.client.rpc("shown", id=job["id"], seq=ask["seq"])
        mgr = getattr(hub.e, "channels", None)
        if mgr is not None and hasattr(mgr, "agent_ask"):  # 桌宠对话栏也弹; 15 秒没人点上 QQ / 微信 (和别的申请一样, 不过期)
            with contextlib.suppress(Exception):
                self.cards[cid]["did"] = await mgr.agent_ask(f"download:{cid}", "工作台想下载文件", job["card_short"],
                                                             lambda ok, cid=cid: self.answer_soon(cid, ok, "pet"))

    @staticmethod
    def _has(s, kind: str, cid: str) -> bool:
        return any(e.get("type") == kind and e.get("id") == cid for e in s.events[-600:])

    async def _close_card(self, cid: str, how: str, text: str) -> None:
        card = self.cards.pop(cid, None)
        hub = self.hub
        sid = (card or {}).get("session") or ""
        s = hub.get(sid) if sid else next((x for x in hub.sessions.values() if self._has(x, "ask", cid)), None)
        if s is not None and self._has(s, "ask", cid) and not self._has(s, "ask_done", cid):
            await hub.emit(s, {"type": "ask_done", "id": cid, "how": how})
            await hub.emit(s, {"type": "tool_result", "id": cid, "ok": True, "text": text})
            hub.save(s)
        mgr = getattr(hub.e, "channels", None)
        if card and card.get("did") and mgr is not None:
            with contextlib.suppress(Exception):
                await mgr.agent_ask_done(card["did"], how)

    # ---- 盯变化 ----
    async def sync(self, wait_s: float = 0.0) -> None:
        """拿下载服务这段时间变了的任务: 该弹卡片的弹, 该收的收, 结束了的交回发起的会话."""
        got = await self.client.rpc("changes", timeout=wait_s + 15, since=self.rev, boot=self.boot, wait_s=wait_s)
        if self._busy is None:
            self._busy = asyncio.Lock()
        async with self._busy:
            if got["boot"] != self.boot:
                self.boot, self.rev = got["boot"], 0
            self.rev = max(self.rev, int(got["rev"]))
            self._first = False
            for job in got["jobs"]:
                try:
                    await self._handle(job)
                except Exception:  # noqa: BLE001 (一个任务出错不耽误别的)
                    log.exception("处理下载 %s 的变化时出错", job.get("id"))

    async def _handle(self, job: dict) -> None:
        card = (job.get("ask") or {}).get("card") if job["state"] == "awaiting" else None
        for cid, c in list(self.cards.items()):  # 这个任务以前的卡片: 已经不等了 (别处答了、取消了) 就收掉
            if c["job"] == job["id"] and cid != card:
                await self._close_card(cid, "stopped", "不用再答了（" + DQ.STATE_TEXT.get(job["state"], job["state"]) + "）")
        if card and card not in self.cards:
            await self._card(job)
        if job["state"] in DQ.TERMINAL and job.get("report") == "pending" and job["id"] not in self.handed:
            # 服务端刚起来 (8790 重启时停着的那阵下完的会攒着), 页面、桌宠还没连上来登记模型: 先等一等 (最多 grace 秒),
            # 不然起它的那一轮接着做时会因为"没有能用的模型"报错
            if not self.hub.models() and time.monotonic() - self.started < self.grace:
                self.later[job["id"]] = job
                return
            await self._report(job)

    async def _report(self, job: dict) -> None:
        """一个结束了的下载交回发起的会话 (和后台作业同一条路: AgentHub.job_done)."""
        self.later.pop(job["id"], None)
        if job["id"] not in self.handed:
            self.handed.add(job["id"])
            # 先记成交过 (宁可漏报一次, 不重复接着做); 她这会儿已经自己看到结果了的 (download_wait), 就不再交
            if not (await self.client.rpc("ack", id=job["id"])).get("acked"):
                return
            how = await self.hub.job_done({"session": job["owner"], "run": job.get("run", ""), "job": job["id"]}, job["report_info"])
            log.info("下载 %s 结束 (%s), 交回会话 %s: %s", job["id"], job["state"], job["owner"], how)

    # ---- 你 (页面) ----
    async def overview(self, history: bool = False) -> dict:
        """给页面看的. 服务没开着、也没有活的时候不把它叫起来 (history=True 才叫: 要看结束了的记录)."""
        state = self.client.state()
        summary = state["summary"]
        if not (state["running"] or history or summary["active"] or summary["awaiting"] or summary["unreported"]):
            return {"service": state, "jobs": [], "idle": True}
        got = await self.client.rpc("status")
        return {"service": self.client.state(), "jobs": got["jobs"], "idle": False,
                **{k: got[k] for k in ("dir_error", "legacy", "storage") if got.get(k)}}

    async def user(self, action: str, jid: str = "", scope: str = "") -> dict:
        """你在页面上动一个任务 (你能动所有会话排的). 这里没有"点头": 点头只走卡片 (会话里那张、桌宠对话栏、QQ / 微信),
        不多开一个一条本机请求就能批下载的口子."""
        if action in ("pause", "resume", "cancel"):
            await self.client.rpc(action, id=jid, actor=USER)
        elif action == "cleanup":
            return {"cleaned": await self.client.rpc("cleanup", scope=scope, actor=USER), **(await self.overview())}
        elif action == "restart_service":
            await self.client.restart()
        else:
            raise DownloadError("bad-args", "action 只能是 pause / resume / cancel / cleanup / restart_service (点头在卡片上点)")
        with contextlib.suppress(DownloadError):
            await self.sync()
        self.wake.set()
        return await self.overview()
