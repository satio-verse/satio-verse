"""聊天网络的客户端这一半 (原型, 2026-11-11): 一台"电脑"上的东西. 方案: 知识库《对话终端方案（2026-11-11）》.

LinkClient  一个端点 (human 或 pet) 到中继的一条连接: 断线自己重连, 带游标补收, 没等到确认的消息重连后用同一个编号重发.
Guest       到本机服务端访客入口 /guest 的连接: 一句进, 一句出.
Node        一个主人和他的桌宠: 两个端点 + 本机的好友表 + 桥 (pet 端点收到好友的话交给 /guest, 她的回话发回去).

隔离放在哪:
  - 好友表在本机, 主人点了同意才写进去. 中继说"你们是好友"不算数: 桥只认本机这张表.
  - 桥只把好友的话交给访客会话. 访客会话本来就没有工具、读不到主人的记忆, 回话出来之前服务端查过隐私, 被拦下的桥不转发.
  - 桌宠对桌宠连续说到 streak_cap 句就停; 每个好友每分钟、每天能让她回几句有上限.
  所以不靠中继守规矩, 也不靠对方的桌宠守规矩.
只依赖标准库和 aiohttp, 不导入 wm, 不读 ~/.worldmodel.

和正式做法的差别 (原型的限制): /guest 只有文字、只能被动回话, 所以桌宠先开口用的是"打招呼" (knock) 加一句程序写的旁白,
不是她自己起的头; 没确认的发件只在内存里, 这边的进程重启会丢; 一个端点的消息排着队一条一条处理.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import uuid
from collections import Counter, deque
from pathlib import Path

import aiohttp

MAX_FRAME = 16 * 1024


class LinkError(RuntimeError):
    """连不上、被拒绝、等不到回执."""


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    """先写到旁边再换上, 写到一半断电不会留下半个文件."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class LinkClient:
    BACKOFF = (0.1, 0.3, 1.0, 2.0)

    def __init__(self, http: aiohttp.ClientSession, url: str, token: str, ep: str, state_file: Path, on_event, *,
                 backoff=None, ack_timeout: float = 10.0):
        """on_event(帧): 收到的 msg / notice, 按 seq 一条一条调, 调完才算处理过 (游标往前走、告诉中继可以删)."""
        self.http, self.url, self.token, self.ep = http, url, token, ep
        self.state_file, self.on_event = Path(state_file), on_event
        self.backoff = tuple(backoff or self.BACKOFF)
        self.ack_timeout = ack_timeout
        self.cursor = int(_read_json(self.state_file, {}).get("cursor", 0))
        self.hello: dict | None = None
        self.fatal: str | None = None     # 被拒绝 (令牌不对之类): 不再重连
        self.connects = 0                 # 连上过几次 (测试看有没有重连)
        self.duplicates = 0               # 补收时又来了一遍、被跳过的条数
        self.hold_cursor = False          # 测试用: 处理完不记游标、不告诉中继, 像刚处理完进程就没了
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ready = asyncio.Event()     # 连着, 而且补收完了
        self._pending: dict[str, dict] = {}            # 没等到 ack 的 send, 重连后原样重发
        self._acks: dict[str, asyncio.Future] = {}
        self._replies: dict[str, asyncio.Future] = {}  # 带 rid 的请求等的 ok / err
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None

    # -- 起停 ------------------------------------------------------------------
    async def start(self, timeout: float = 5.0) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run())
        self._worker = asyncio.create_task(self._work())
        await self.wait_ready(timeout)

    async def wait_ready(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self._ready.is_set():
            if self.fatal:
                raise LinkError(f"中继拒绝了连接: {self.fatal}")
            if time.monotonic() > deadline:
                raise LinkError("等了一会儿还没连上中继")
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self._stopping = True
        self._ready.clear()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        for task in (self._task, self._worker):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = self._worker = None

    @property
    def online(self) -> bool:
        return self._ready.is_set()

    # -- 连接 ------------------------------------------------------------------
    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                async with self.http.ws_connect(f"{self.url}?ep={self.ep}", max_msg_size=MAX_FRAME, heartbeat=20,
                                                headers={"Authorization": f"Bearer {self.token}"}) as ws:
                    self._ws, attempt = ws, 0
                    self.connects += 1
                    await self._serve(ws)
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in (400, 401, 403):
                    self.fatal = f"HTTP {exc.status}"
                    return
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                pass
            finally:
                self._ws = None
                self._ready.clear()
            if self._stopping:
                return
            await asyncio.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            attempt += 1

    async def _serve(self, ws) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                f = json.loads(msg.data)
            except ValueError:
                continue
            t = f.get("t") if isinstance(f, dict) else None
            if t == "hello":
                self.hello = f
                await ws.send_json({"t": "sync", "since": self.cursor})
            elif t == "synced":
                self._ready.set()
                for frame in list(self._pending.values()):  # 上次没等到确认的: 同一个编号再发一次
                    await ws.send_json(frame)
            elif t in ("msg", "notice"):
                self._inbox.put_nowait(f)
            elif t == "ack":
                self._pending.pop(f.get("id"), None)
                fut = self._acks.pop(f.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(f)
            elif t in ("ok", "err") and f.get("rid") in self._replies:
                fut = self._replies.pop(f["rid"])
                if not fut.done():
                    fut.set_result(f)

    async def _work(self) -> None:
        """一条一条处理收到的东西. 处理完才把游标记下来、告诉中继; 处理到一半断了, 重连以后中继会再给一遍."""
        while True:
            f = await self._inbox.get()
            seq = f.get("seq")
            if not isinstance(seq, int) or seq <= self.cursor:
                self.duplicates += 1
                continue
            try:
                await self.on_event(f)
            except asyncio.CancelledError:
                raise
            except Exception:  # 一条处理坏了不能卡住后面的; 这一条算处理过
                import logging
                logging.getLogger("link").exception("处理收到的一帧出错")
            if self.hold_cursor:
                continue
            self.cursor = seq
            _write_json(self.state_file, {"cursor": self.cursor})
            ws = self._ws
            if ws is not None and not ws.closed:
                with contextlib.suppress(ConnectionError, RuntimeError, aiohttp.ClientError):
                    await ws.send_json({"t": "cursor", "upto": seq})

    # -- 发 --------------------------------------------------------------------
    async def send(self, to: str, body: str = "", kind: str = "text", id: str | None = None,
                   timeout: float | None = None) -> dict:
        """发一条, 等中继的回执 {ok, mid} 或 {ok: false, why}. 没连着就先放着, 连上以后发; id 一样的只算一次."""
        cid = id or uuid.uuid4().hex
        frame = {"t": "send", "id": cid, "to": to, "kind": kind, "body": body}
        fut = self._acks.get(cid)
        if fut is None or fut.done():
            fut = self._acks[cid] = asyncio.get_running_loop().create_future()
        self._pending[cid] = frame
        ws = self._ws
        if ws is not None and not ws.closed and self._ready.is_set():
            with contextlib.suppress(ConnectionError, RuntimeError, aiohttp.ClientError):
                await ws.send_json(frame)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout or self.ack_timeout)
        except asyncio.TimeoutError:
            raise LinkError(f"发给 {to} 的消息等不到中继的回执 (留着, 连上以后会再发)") from None

    async def request(self, frame: dict, timeout: float = 5.0) -> dict:
        """带 rid 的请求 (好友的几种帧), 等 ok / err."""
        await self.wait_ready(timeout)
        rid = uuid.uuid4().hex
        fut = self._replies[rid] = asyncio.get_running_loop().create_future()
        try:
            await self._ws.send_json({**frame, "rid": rid})
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError, aiohttp.ClientError, AttributeError):
            raise LinkError(f"{frame.get('t')} 没等到中继的回话") from None
        finally:
            self._replies.pop(rid, None)


class Guest:
    """本机服务端的访客入口 /guest (wm/app.py 的 guest_handler): 发 {type: chat, id, user, text}, 收 {type: reply, id, ok, reply, …}.
    这个入口后面是访客会话: 没有工具, 读不到主人的记忆, 回话出来之前查过隐私 (查到了 reply 是空的, 多一个 blocked)."""

    def __init__(self, http: aiohttp.ClientSession, url: str, timeout: float = 30.0):
        self.http, self.url, self.timeout = http, url, timeout
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._lock = asyncio.Lock()
        self._n = 0

    async def _open(self):
        if self._ws is None or self._ws.closed:
            self._ws = await self.http.ws_connect(self.url, max_msg_size=256 * 1024, heartbeat=30)
            hello = await asyncio.wait_for(self._ws.receive_json(), 5)
            if hello.get("guest") is not True:  # 连错了入口 (比如连到了带工具的 /ws) 就不用
                await self.close()
                raise LinkError("对面不是访客入口")
        return self._ws

    async def chat(self, user: str, text: str) -> dict:
        async with self._lock:
            for _ in range(2):  # 连接断了重连再问一次
                try:
                    ws = await self._open()
                    self._n += 1
                    await ws.send_json({"type": "chat", "id": self._n, "user": user, "text": text})
                    while True:
                        msg = await asyncio.wait_for(ws.receive(), self.timeout)
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            raise ConnectionError("访客连接断了")
                        data = json.loads(msg.data)
                        if data.get("id") == self._n and data.get("type") in ("reply", "error"):
                            return data if data["type"] == "reply" else {"ok": False, "error": data.get("message")}
                except LinkError:
                    raise
                except (ConnectionError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
                    await self.close()
            return {"ok": False, "status": 0, "error": "本机服务端的访客入口连不上"}

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None


class Node:
    def __init__(self, home, relay_url: str, token: str, guest_url: str | None = None, *, streak_cap: int = 6,
                 per_min: int = 10, per_day: int = 200, guest_timeout: float = 30.0, backoff=None, clock=time.time):
        """home: 这台"电脑"上存东西的文件夹 (游标、好友表). guest_url: 本机服务端的 /guest; 不给就没有桥 (只有主人自己聊).
        streak_cap: 桌宠对桌宠连续几句以后停. per_min / per_day: 每个好友每分钟、每天最多让她回几句 (0 不限)."""
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.relay_url, self.token = relay_url, token
        self.streak_cap, self.per_min, self.per_day = streak_cap, per_min, per_day
        self.clock = clock
        self.friends: dict = _read_json(self.home / "friends.json", {})       # 账号 -> {name, pet_name, since}
        self.pending_out: dict = _read_json(self.home / "pending.json", {})   # 我发出去、对方还没点同意的申请
        self.requests: list[dict] = []     # 别人发来的好友申请: 只给主人看, 不进模型
        self.owner_notes: list[str] = []   # 给主人的提示 (拦下的话、没回成的)
        self.log: list[dict] = []          # 两个端点收发的每一条 (测试对账用, 以后是聊天页的数据)
        self.stats: Counter = Counter()
        self.streak: dict[str, int] = {}   # 和某个好友的桌宠连续说了几句 (两边的都算)
        self._calls: dict[str, deque] = {}
        self._http = aiohttp.ClientSession()
        self.human = LinkClient(self._http, relay_url, token, "human", self.home / "human.json", self._on_human,
                                backoff=backoff)
        self.pet = LinkClient(self._http, relay_url, token, "pet", self.home / "pet.json", self._on_pet,
                              backoff=backoff)
        self.guest = Guest(self._http, guest_url, guest_timeout) if guest_url else None

    @property
    def acct(self) -> str:
        return (self.human.hello or {}).get("acct", "")

    @property
    def code(self) -> str:
        return (self.human.hello or {}).get("code", "")

    async def start(self, timeout: float = 5.0) -> "Node":
        await self.human.start(timeout)
        await self.pet.start(timeout)
        return self

    async def stop(self) -> None:
        await self.human.stop()
        await self.pet.stop()
        if self.guest is not None:
            await self.guest.close()
        await self._http.close()

    def _record(self, direction: str, ep: str, peer: str, mid, kind: str, body: str) -> None:
        self.log.append({"dir": direction, "ep": ep, "peer": peer, "mid": mid, "kind": kind, "body": body})

    # -- 主人这边 (以后是"聊天"页点的按钮) ----------------------------------------
    async def request_friend(self, code: str, note: str = "") -> dict:
        r = await self.human.request({"t": "friend_request", "code": code, "note": note})
        if r.get("t") == "ok":  # 记下"我申请过这个人": 以后中继说加上了, 对得上才认
            self.pending_out[r["to"]] = {"name": r.get("name", ""), "pet_name": r.get("pet_name", "")}
            _write_json(self.home / "pending.json", self.pending_out)
        return r

    async def accept(self, acct: str) -> dict:
        """主人点了同意. 先写本机的好友表, 再告诉中继; 中继没认就撤回."""
        req = next((r for r in self.requests if r.get("from") == acct), None)
        if req is None:
            raise LinkError("没有收到过这个人的申请")
        self._befriend(acct, req.get("from_name", ""), req.get("pet_name", ""))
        r = await self.human.request({"t": "friend_accept", "from": acct})
        if r.get("t") != "ok":
            self.unfriend_local(acct)
        else:
            self.requests = [x for x in self.requests if x.get("from") != acct]
        return r

    def _befriend(self, acct: str, name: str, pet_name: str) -> None:
        self.friends[acct] = {"name": name, "pet_name": pet_name, "since": round(self.clock())}
        _write_json(self.home / "friends.json", self.friends)

    def unfriend_local(self, acct: str) -> None:
        """只在本机删掉: 之后这个人的话桥不再交给她, 不管中继那边怎么说."""
        self.friends.pop(acct, None)
        _write_json(self.home / "friends.json", self.friends)

    async def say(self, acct: str, text: str, to: str = "pet", **kw) -> dict:
        """主人自己打字, 发给朋友本人 (to="human") 或朋友的桌宠 (to="pet"). 不经过自己的桌宠."""
        ack = await self.human.send(f"{acct}/{to}", text, **kw)
        if ack.get("ok") and not ack.get("dup"):
            self._record("out", "human", f"{acct}/{to}", ack["mid"], "text", text)
        return ack

    async def knock(self, acct: str) -> dict:
        """主人让她去找朋友的桌宠串门: 她这边发一个招呼过去, 连续句数从头算."""
        self.streak[acct] = 0
        ack = await self.pet.send(f"{acct}/pet", kind="knock")
        if ack.get("ok") and not ack.get("dup"):
            self._record("out", "pet", f"{acct}/pet", ack["mid"], "knock", "")
        return ack

    async def _on_human(self, ev: dict) -> None:
        if ev["t"] == "msg":  # 给主人看的; 不经过她
            self._record("in", "human", ev["from"], ev["mid"], ev["kind"], ev["body"])
        elif ev.get("kind") == "friend_request":
            self.requests.append(ev)
        elif ev.get("kind") == "friend_added":
            peer = ev.get("peer")
            if peer in self.pending_out:  # 我申请过, 对方同意了
                self.pending_out.pop(peer)
                _write_json(self.home / "pending.json", self.pending_out)
                self._befriend(peer, ev.get("peer_name", ""), ev.get("pet_name", ""))
            elif peer not in self.friends:  # 我没申请过也没同意过: 中继说了不算
                self.stats["ignored_friend_added"] += 1

    # -- 桥: 好友的话交给她, 她的回话发回去 ------------------------------------------
    def _allow(self, acct: str) -> bool:
        now, q = self.clock(), self._calls.setdefault(acct, deque())
        while q and now - q[0] >= 86400:
            q.popleft()
        if self.per_day and len(q) >= self.per_day:
            return False
        if self.per_min and sum(1 for t in q if now - t < 60) >= self.per_min:
            return False
        q.append(now)
        return True

    @staticmethod
    def _who(ev: dict, acct: str, is_bot: bool) -> str:
        """交给访客会话的称呼 (也是她按谁分会话的依据). 名字里的"主人"换掉: 她在别处用这两个字认真正的主人."""
        name = re.sub("主人", "某人", str(ev.get("from_name") or ""))[:20] or "某人"
        return f"{name}（{'桌宠' if is_bot else '本人'}·{acct[-6:]}）"

    async def _on_pet(self, ev: dict) -> None:
        if ev["t"] != "msg":  # 好友的事不该进 pet 端点; 进来了也不理
            self.stats["ignored_notice"] += 1
            return
        src = ev["from"]
        acct, ep = src.split("/")
        self._record("in", "pet", src, ev["mid"], ev["kind"], ev["body"])
        if acct not in self.friends:
            self.stats["dropped_not_friend"] += 1
            return
        if self.guest is None:
            return
        is_bot = ep == "pet"
        if ev["kind"] == "knock":
            if not is_bot:
                return
            self.streak[acct] = 0
            text = f"（{self.friends[acct].get('pet_name') or '对方的桌宠'}来串门，跟你打了个招呼）"
        else:
            text = ev["body"]
            if is_bot:
                self.streak[acct] = self.streak.get(acct, 0) + 1
                if self.streak[acct] >= self.streak_cap:  # 两只桌宠连着说够了: 停, 等有人说话
                    self.stats["streak_stopped"] += 1
                    return
        if not self._allow(acct):
            self.stats["limited_local"] += 1
            return
        self.stats["guest_calls"] += 1
        r = await self.guest.chat(self._who(ev, acct, is_bot), text)
        if r.get("ok") and r.get("reply"):
            ack = await self.pet.send(src, r["reply"], id=f"re-{ev['mid']}")  # 编号跟着来信走: 这封信再来一遍也只回一次
            if ack.get("ok"):
                if not ack.get("dup"):
                    self._record("out", "pet", src, ack["mid"], "text", r["reply"])
                    self.stats["replied"] += 1
                    if is_bot:
                        self.streak[acct] = self.streak.get(acct, 0) + 1
            else:
                self.stats["reply_refused"] += 1
        elif r.get("blocked"):
            self.stats["blocked"] += 1
            self.owner_notes.append(f"她想回{self.friends[acct].get('name') or '好友'}的一句没发出去：{r['blocked']}")
        elif r.get("ok"):
            self.stats["silent"] += 1
        else:
            self.stats["guest_failed"] += 1
            self.owner_notes.append(f"这次没回成：{r.get('error') or '访客入口没回话'}")
