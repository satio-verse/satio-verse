"""聊天网络的本机中继 (原型, 2026-11-11). 方案: 知识库《对话终端方案（2026-11-11）》第五节第 1 步.

做什么: 账号 (一个主人和他的桌宠, 各一个端点 human / pet)、好友 (主人点了同意才算)、WebSocket 收发、SQLite 信箱
(对方不在线先存着; 重连带游标补收; 同一个编号重发只算一次).
不做: 群、附件、语音、签名、审核、实名、多设备. 这些在方案的后面几步.
只依赖标准库和 aiohttp, 不导入 wm, 不读 ~/.worldmodel; 只听 127.0.0.1.

帧是 JSON 文本, 一帧不超过 16 KB. 上行帧可以带 rid, 回的 ok / err 会带回去.
  上行  send {id, to: "账号/端点", kind: text | knock, body}  -> ack {id, ok, mid} 或 ack {id, ok: false, why}
        sync {since}             -> 补发 seq > since 的, 再回 synced {upto}; 这之后新消息才实时推
        cursor {upto}            -> seq <= upto 的都处理完了, 信箱里删掉
        friend_request {code, note} / friend_accept {from} / friend_reject {from}   只有 human 端点能发
        ping
  下行  hello {acct, ep, name, pet_name, code, last_seq}
        msg {seq, mid, from, from_name, is_bot, kind, body, ts}
        notice {seq, mid, kind: friend_request | friend_added, ...}   好友的事只进 human 端点的信箱, pet 端点收不到
        ack / ok / err / synced / pong

编号: mid 是全中继统一递增的消息号 (两边对账用); seq 是每个端点自己的收件序号 (补收用). from_name 和 is_bot 是中继按账号和
端点标的, 发的人自己写不了.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import secrets
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

HOST = "127.0.0.1"          # 原型只听本机, 没有别的选项
EPS = ("human", "pet")
KINDS = ("text", "knock")   # knock: 桌宠去找桌宠串门时打的招呼, 没有正文
MAX_FRAME = 16 * 1024
MAX_BODY = 1000
MAX_NOTE = 60
SENT_TTL_S = 7 * 86400      # 重发去重的记录留多久
REPLACED = 4000             # 同一个端点又连上来一条: 旧的那条用这个码关掉

_CTL = dict.fromkeys(range(0x20), " ")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _line(text, limit: int) -> str:
    """好友申请的附言: 去控制字符, 压成一行."""
    return " ".join(str(text or "").translate(_CTL).split())[:limit]


class Store:
    """SQLite 里的账号、好友、申请、信箱. 每次改动提交了才返回: 进程被杀, 已经回过 ack 的消息还在."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        for sql in (
            "CREATE TABLE IF NOT EXISTS accounts(acct TEXT PRIMARY KEY, name TEXT NOT NULL, pet_name TEXT NOT NULL,"
            " token_sha TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE, created REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS friends(a TEXT NOT NULL, b TEXT NOT NULL, since REAL NOT NULL, PRIMARY KEY(a, b))",
            "CREATE TABLE IF NOT EXISTS requests(src TEXT NOT NULL, dst TEXT NOT NULL, note TEXT NOT NULL,"
            " created REAL NOT NULL, PRIMARY KEY(src, dst))",
            "CREATE TABLE IF NOT EXISTS mailbox(rcpt TEXT NOT NULL, seq INTEGER NOT NULL, mid INTEGER NOT NULL,"
            " payload TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(rcpt, seq))",
            "CREATE TABLE IF NOT EXISTS sent(sender TEXT NOT NULL, client_id TEXT NOT NULL, mid INTEGER NOT NULL,"
            " created REAL NOT NULL, PRIMARY KEY(sender, client_id))",
            "CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL)",
        ):
            self.db.execute(sql)
        self.db.execute("DELETE FROM sent WHERE created < ?", (time.time() - SENT_TTL_S,))

    def close(self) -> None:
        self.db.close()

    @contextlib.contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # -- 账号 ------------------------------------------------------------------
    def register(self, name: str, pet_name: str) -> dict:
        """建一个账号. 令牌只在这里给一次, 库里只存它的哈希."""
        name, pet_name = _line(name, 20), _line(pet_name, 20)
        if not name or not pet_name:
            raise ValueError("主人和桌宠都要有名字")
        acct, token = "u" + secrets.token_hex(5), secrets.token_urlsafe(24)
        code = secrets.token_hex(4).upper()
        with self.tx() as db:
            db.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)", (acct, name, pet_name, _sha(token), code, time.time()))
        return {"acct": acct, "token": token, "code": code, "name": name, "pet_name": pet_name}

    def by_token(self, token: str):
        return self.db.execute("SELECT * FROM accounts WHERE token_sha=?", (_sha(token),)).fetchone() if token else None

    def by_code(self, code: str):
        return self.db.execute("SELECT * FROM accounts WHERE code=?", (code,)).fetchone()

    def account(self, acct: str):
        return self.db.execute("SELECT * FROM accounts WHERE acct=?", (acct,)).fetchone()

    # -- 好友 ------------------------------------------------------------------
    def are_friends(self, a: str, b: str) -> bool:
        return self.db.execute("SELECT 1 FROM friends WHERE a=? AND b=?", (a, b)).fetchone() is not None

    def friends(self, acct: str) -> list[str]:
        return [r["b"] for r in self.db.execute("SELECT b FROM friends WHERE a=? ORDER BY b", (acct,))]

    def has_request(self, src: str, dst: str) -> bool:
        return self.db.execute("SELECT 1 FROM requests WHERE src=? AND dst=?", (src, dst)).fetchone() is not None

    def add_request(self, src: str, dst: str, note: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO requests VALUES(?,?,?,?)", (src, dst, note, time.time()))

    def drop_requests(self, a: str, b: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM requests WHERE (src=? AND dst=?) OR (src=? AND dst=?)", (a, b, b, a))

    def make_friends(self, a: str, b: str) -> None:
        now = time.time()
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO friends VALUES(?,?,?)", (a, b, now))
            db.execute("INSERT OR IGNORE INTO friends VALUES(?,?,?)", (b, a, now))
            db.execute("DELETE FROM requests WHERE (src=? AND dst=?) OR (src=? AND dst=?)", (a, b, b, a))

    # -- 信箱 ------------------------------------------------------------------
    @staticmethod
    def _next(db, name: str) -> int:
        db.execute("INSERT INTO counters VALUES(?, 1) ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))
        return db.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()["value"]

    def sent(self, sender: str, client_id: str):
        return self.db.execute("SELECT mid FROM sent WHERE sender=? AND client_id=?", (sender, client_id)).fetchone()

    def put(self, rcpt: str, payload: dict, sender: str | None = None, client_id: str | None = None) -> dict:
        """放进 rcpt 的信箱, 配上 mid 和 seq. sender + client_id: 记下来, 同一个编号再发时认得出."""
        with self.tx() as db:
            mid, seq = self._next(db, "mid"), self._next(db, "seq:" + rcpt)
            payload = {**payload, "seq": seq, "mid": mid}
            db.execute("INSERT INTO mailbox VALUES(?,?,?,?,?)",
                       (rcpt, seq, mid, json.dumps(payload, ensure_ascii=False), time.time()))
            if sender is not None:
                db.execute("INSERT INTO sent VALUES(?,?,?,?)", (sender, client_id, mid, time.time()))
        return payload

    def since(self, rcpt: str, seq: int) -> list[dict]:
        rows = self.db.execute("SELECT payload FROM mailbox WHERE rcpt=? AND seq>? ORDER BY seq", (rcpt, seq))
        return [json.loads(r["payload"]) for r in rows]

    def done(self, rcpt: str, upto: int) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM mailbox WHERE rcpt=? AND seq<=?", (rcpt, upto))

    def last_seq(self, rcpt: str) -> int:
        row = self.db.execute("SELECT value FROM counters WHERE name=?", ("seq:" + rcpt,)).fetchone()
        return row["value"] if row else 0

    def waiting(self, rcpt: str | None = None) -> int:
        """信箱里还没被确认收到的条数."""
        if rcpt is None:
            return self.db.execute("SELECT COUNT(*) AS n FROM mailbox").fetchone()["n"]
        return self.db.execute("SELECT COUNT(*) AS n FROM mailbox WHERE rcpt=?", (rcpt,)).fetchone()["n"]


class _Conn:
    """一个端点的一条连接. 往下发的帧都排进一个队列由一个任务写, 先后顺序就是入队的顺序."""

    def __init__(self, relay: "Relay", ws: web.WebSocketResponse, acct, ep: str):
        self.relay, self.ws = relay, ws
        self.acct, self.ep = acct["acct"], ep
        self.name = acct["name"] if ep == "human" else acct["pet_name"]
        self.key = f"{self.acct}/{ep}"
        self.live = False  # sync 补完以后才实时推, 不然补发和实时会乱序
        self.q: asyncio.Queue = asyncio.Queue()
        self.writer = asyncio.create_task(self._write())

    def push(self, frame: dict) -> None:
        self.q.put_nowait(frame)

    async def _write(self) -> None:
        while (frame := await self.q.get()) is not None:
            text = json.dumps(frame, ensure_ascii=False)
            self.relay.log_frame("out", self.key, text)
            try:
                await self.ws.send_str(text)
            except (ConnectionError, RuntimeError):
                return

    async def close(self, code: int = 1000, why: str = "") -> None:
        self.q.put_nowait(None)
        with contextlib.suppress(Exception):
            await self.ws.close(code=code, message=why.encode("utf-8"))


class Relay:
    def __init__(self, db_path, *, per_min: int = 20, requests_per_min: int = 5, frame_log=None, clock=time.time):
        """per_min: 同一个端点给同一个对象一分钟最多发几条 (0 不限). frame_log: 测试用, 记下每一帧的原文
        (list, 或者能写一行的文件对象); 平时不开, 帧里是别人的聊天."""
        self.store = Store(db_path)
        self.per_min, self.requests_per_min = int(per_min), int(requests_per_min)
        self.frame_log = frame_log
        self.clock = clock
        self.conns: dict[str, _Conn] = {}
        self._rate: dict[tuple, deque] = {}
        self.runner: web.AppRunner | None = None
        self.port: int | None = None

    # -- 起停 ------------------------------------------------------------------
    async def start(self, port: int = 0) -> int:
        app = web.Application()
        app.router.add_get("/link", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, HOST, port).start()
        self.port = self.runner.addresses[0][1]
        return self.port

    async def stop(self) -> None:
        for conn in list(self.conns.values()):
            await conn.close(1001, "relay stopping")
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        self.store.close()

    def log_frame(self, direction: str, key: str, text: str) -> None:
        if self.frame_log is None:
            return
        if isinstance(self.frame_log, list):
            self.frame_log.append((direction, key, text))
        else:
            self.frame_log.write(json.dumps([direction, key, text], ensure_ascii=False) + "\n")
            self.frame_log.flush()

    # -- 连接 ------------------------------------------------------------------
    async def handle(self, request: web.Request):
        if request.headers.get("Origin") is not None:  # 浏览器的 WebSocket 一定带 Origin: 网页连不进来
            return web.Response(status=403, text="不接受网页来的连接")
        auth = request.headers.get("Authorization", "")
        acct = self.store.by_token(auth[len("Bearer "):].strip()) if auth.startswith("Bearer ") else None
        if acct is None:
            return web.Response(status=401, text="令牌不对")
        ep = request.query.get("ep")
        if ep not in EPS:
            return web.Response(status=400, text="ep 只能是 human 或 pet")
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=20)
        await ws.prepare(request)
        conn = _Conn(self, ws, acct, ep)
        old, self.conns[conn.key] = self.conns.get(conn.key), conn
        if old is not None:
            await old.close(REPLACED, "replaced")
        conn.push({"t": "hello", "acct": conn.acct, "ep": ep, "name": acct["name"], "pet_name": acct["pet_name"],
                   "code": acct["code"], "last_seq": self.store.last_seq(conn.key)})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                self.log_frame("in", conn.key, msg.data)
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    frame = None
                if not isinstance(frame, dict):
                    conn.push({"t": "err", "why": "bad_json"})
                    continue
                self.dispatch(conn, frame)  # 里面没有 await: 一帧处理完才轮到下一帧
        finally:
            if self.conns.get(conn.key) is conn:
                del self.conns[conn.key]
            conn.q.put_nowait(None)
        return ws

    def dispatch(self, conn: _Conn, f: dict) -> None:
        t = f.get("t")
        if t == "send":
            self._send(conn, f)
        elif t == "sync":
            since = f.get("since") if isinstance(f.get("since"), int) and f["since"] >= 0 else 0
            rows = self.store.since(conn.key, since)
            for row in rows:
                conn.push(row)
            conn.push({"t": "synced", "upto": rows[-1]["seq"] if rows else since})
            conn.live = True
        elif t == "cursor":
            if isinstance(f.get("upto"), int) and f["upto"] > 0:
                self.store.done(conn.key, f["upto"])
        elif t in ("friend_request", "friend_accept", "friend_reject"):
            self._friend(conn, t, f)
        elif t == "ping":
            conn.push({"t": "pong"})
        else:
            conn.push({"t": "err", "rid": f.get("rid"), "why": "unknown_frame"})

    def _rate_ok(self, key: tuple, limit: int) -> bool:
        now, q = self.clock(), self._rate.setdefault(key, deque())
        while q and now - q[0] >= 60:
            q.popleft()
        if limit and len(q) >= limit:
            return False
        q.append(now)
        return True

    def _deliver(self, rcpt: str, payload: dict) -> None:
        conn = self.conns.get(rcpt)
        if conn is not None and conn.live:
            conn.push(payload)

    # -- 发消息 ----------------------------------------------------------------
    def _send(self, conn: _Conn, f: dict) -> None:
        cid = f.get("id")
        if not isinstance(cid, str) or not 1 <= len(cid) <= 64:
            return conn.push({"t": "err", "why": "bad_id"})

        def nak(why: str) -> None:
            conn.push({"t": "ack", "id": cid, "ok": False, "why": why})

        prev = self.store.sent(conn.key, cid)
        if prev is not None:  # 这个编号收过了 (对方没等到 ack 又发了一次): 回同一个结果, 不再投递
            return conn.push({"t": "ack", "id": cid, "ok": True, "mid": prev["mid"], "dup": True})
        to, kind, body = f.get("to"), f.get("kind", "text"), f.get("body", "")
        if not isinstance(to, str) or to.count("/") != 1:
            return nak("bad_to")
        peer, ep = to.split("/")
        if ep not in EPS or self.store.account(peer) is None:
            return nak("unknown_peer")
        if peer == conn.acct:
            return nak("self")
        if kind not in KINDS:
            return nak("bad_kind")
        if kind == "knock":
            if conn.ep != "pet" or ep != "pet":
                return nak("bad_kind")
            body = ""
        elif not isinstance(body, str) or not body.strip():
            return nak("empty")
        elif len(body) > MAX_BODY:
            return nak("too_long")
        if not self.store.are_friends(conn.acct, peer):
            return nak("not_friends")
        if not self._rate_ok((conn.key, to), self.per_min):
            return nak("rate")
        payload = self.store.put(to, {"t": "msg", "from": conn.key, "from_name": conn.name, "is_bot": conn.ep == "pet",
                                      "kind": kind, "body": body, "ts": round(self.clock(), 3)},
                                 sender=conn.key, client_id=cid)
        conn.push({"t": "ack", "id": cid, "ok": True, "mid": payload["mid"]})
        self._deliver(to, payload)

    # -- 好友 ------------------------------------------------------------------
    def _friend(self, conn: _Conn, t: str, f: dict) -> None:
        rid = f.get("rid")

        def err(why: str) -> None:
            conn.push({"t": "err", "rid": rid, "re": t, "why": why})

        if conn.ep != "human":  # 她自己不加好友: 这几种帧 pet 端点发了不算
            return err("pet_cannot")
        me = self.store.account(conn.acct)
        if t == "friend_request":
            dst = self.store.by_code(str(f.get("code") or "").strip().upper())
            if dst is None:
                return err("unknown_code")
            if dst["acct"] == conn.acct:
                return err("self")
            if self.store.are_friends(conn.acct, dst["acct"]):
                return err("already_friends")
            if not self._rate_ok(("friend_request", conn.acct), self.requests_per_min):
                return err("rate")
            again = self.store.has_request(conn.acct, dst["acct"])
            note = _line(f.get("note"), MAX_NOTE)
            self.store.add_request(conn.acct, dst["acct"], note)
            if not again:  # 同一个人再申请一次不再提示一遍
                rcpt = f"{dst['acct']}/human"
                self._deliver(rcpt, self.store.put(rcpt, {
                    "t": "notice", "kind": "friend_request", "from": conn.acct, "from_name": me["name"],
                    "pet_name": me["pet_name"], "note": note, "ts": round(self.clock(), 3)}))
            return conn.push({"t": "ok", "rid": rid, "re": t, "to": dst["acct"], "name": dst["name"],
                              "pet_name": dst["pet_name"]})
        src = f.get("from")
        if not isinstance(src, str) or not self.store.has_request(src, conn.acct):
            return err("no_request")
        if t == "friend_reject":
            self.store.drop_requests(src, conn.acct)
            return conn.push({"t": "ok", "rid": rid, "re": t, "peer": src})
        self.store.make_friends(conn.acct, src)
        for a, b in ((conn.acct, src), (src, conn.acct)):
            other, rcpt = self.store.account(b), f"{a}/human"
            self._deliver(rcpt, self.store.put(rcpt, {
                "t": "notice", "kind": "friend_added", "peer": b, "peer_name": other["name"],
                "pet_name": other["pet_name"], "ts": round(self.clock(), 3)}))
        conn.push({"t": "ok", "rid": rid, "re": t, "peer": src})


async def _serve(args) -> None:
    log = open(args.frame_log, "a", encoding="utf-8") if args.frame_log else None
    relay = Relay(args.db, per_min=args.per_min, frame_log=log)
    port = await relay.start(args.port)
    print(json.dumps({"ready": True, "host": HOST, "port": port}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await relay.stop()
        if log:
            log.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="聊天网络的本机中继 (原型, 只听 127.0.0.1)")
    p.add_argument("--db", type=Path, required=True, help="SQLite 文件 (账号、好友、信箱)")
    p.add_argument("--port", type=int, default=0, help="端口; 0 是随便挑一个空的")
    p.add_argument("--per-min", type=int, default=20, help="同一个端点给同一个对象一分钟最多发几条")
    p.add_argument("--frame-log", type=Path, help="测试用: 把每一帧的原文记进这个文件. 平时别开")
    p.add_argument("--register", metavar="主人名字", help="建一个账号, 打印账号、令牌和好友码, 然后退出")
    p.add_argument("--pet", default="桌宠", help="和 --register 一起用: 桌宠的名字")
    args = p.parse_args(argv)
    if args.register:
        store = Store(args.db)
        print(json.dumps(store.register(args.register, args.pet)), flush=True)
        store.close()
        return 0
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
