"""对话 (thread) 和消息的记录. 一个文件 threads.json (原子替换 + 文件锁), 回话正文放 replies/<消息号>.json, 审计日志 audit.jsonl.

- 对话归一个"链" (根会话 + 对方): 别的会话看不到、续不了.
- 消息的状态: running (已经交给对方进程) / replied / failed (对方明确报错) / timeout / uncertain (不确定: 重启时在飞的、
  输出坏了的; 字节可能已经发出去了, 绝不自动重发).
- 审计日志只记谁发给谁、第几次、字数、指纹、状态、花费; 不记原文. 留一周.
- 回话正文是对方 agent 写的, 只当资料: 存的时候去掉控制字符、限长.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import REPLY_STORE_MAX, AgentMsgError, FileLock, StateError, read_json, write_json

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
AUDIT_KEEP_S = 7 * 86400
STATES = {"running": "在问", "replied": "已回话", "failed": "对方报错", "timeout": "超时", "uncertain": "不确定"}


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def clean_text(text: str, limit: int) -> str:
    return _CTRL.sub("", str(text or "")).replace("\r\n", "\n")[:limit]


class Store:
    def __init__(self, home: Path | str):
        self.home = Path(home)
        self.path = self.home / "threads.json"
        self.audit_path = self.home / "audit.jsonl"
        self._lock_path = self.home / "store.lock"

    def _locked(self) -> FileLock:
        return FileLock(self._lock_path)

    def _load(self) -> dict:
        data = read_json(self.path)
        if data is None:
            return {"n_thread": 0, "n_msg": 0, "threads": {}, "messages": {}}
        if not (isinstance(data, dict) and isinstance(data.get("threads"), dict) and isinstance(data.get("messages"), dict)
                and isinstance(data.get("n_thread"), int) and isinstance(data.get("n_msg"), int)):
            raise StateError("对话记录的内容不对")
        return data

    # ---- 对话和消息 ----
    def new_thread(self, chain: str, root: str, target: str) -> dict:
        with self._locked():
            db = self._load()
            db["n_thread"] += 1
            thread = {"id": f"T{db['n_thread']}", "chain": chain, "root": root, "target": target, "cli_session": "",
                      "created": time.time(), "msgs": []}
            db["threads"][thread["id"]] = thread
            write_json(self.path, db)
            return dict(thread)

    def add_message(self, thread_id: str, text: str, boot: str, run: str, session: str) -> dict:
        with self._locked():
            db = self._load()
            thread = db["threads"].get(thread_id)
            if thread is None:
                raise AgentMsgError("bad_args", f"没有这个对话 {thread_id}")
            db["n_msg"] += 1
            msg = {"id": f"M{db['n_msg']}", "thread": thread_id, "state": "running", "t0": time.time(), "t1": 0.0,
                   "chars": len(text), "fp": fingerprint(text), "cny": None, "tokens": {}, "reply_chars": 0,
                   "reported": False, "boot": boot, "run": run, "session": session, "n": len(thread["msgs"]) + 1}
            thread["msgs"].append(msg["id"])
            db["messages"][msg["id"]] = msg
            write_json(self.path, db)
            return dict(msg)

    def finish(self, mid: str, state: str, *, reply: str = "", error: str = "", cny=None, tokens=None, cli_session: str = "") -> dict:
        with self._locked():
            db = self._load()
            msg = db["messages"][mid]
            msg.update(state=state, t1=time.time(), cny=cny, tokens=tokens or {}, reply_chars=len(reply))
            if cli_session and re.fullmatch(r"[0-9A-Za-z-]{8,64}", cli_session):
                db["threads"][msg["thread"]]["cli_session"] = cli_session
            write_json(self.path, db)
            body = {"text": clean_text(reply, REPLY_STORE_MAX), "error": clean_text(error, 2000)}
            write_json(self.home / "replies" / f"{mid}.json", body)
            return dict(msg)

    def mark_reported(self, mid: str) -> bool:
        """回报交过了 (第一次返回 True; 同一条不交两次)."""
        with self._locked():
            db = self._load()
            msg = db["messages"].get(mid)
            if msg is None or msg["reported"]:
                return False
            msg["reported"] = True
            write_json(self.path, db)
            return True

    def recover(self, boot: str) -> list[str]:
        """服务端刚起来: 上一个进程留下的 running 记成 uncertain (对方进程跟着死了, 不重发, 也没有回话可交)."""
        if not self.path.exists():
            return []
        with self._locked():
            try:
                db = self._load()
            except StateError:
                return []
            hit = [m["id"] for m in db["messages"].values() if m["state"] == "running" and m["boot"] != boot]
            for mid in hit:
                db["messages"][mid].update(state="uncertain", t1=time.time())
            if hit:
                write_json(self.path, db)
            return hit

    # ---- 读 ----
    def thread(self, tid: str) -> dict | None:
        return self._load()["threads"].get(tid)

    def message(self, mid: str) -> dict | None:
        return self._load()["messages"].get(mid)

    def threads_of(self, chain_prefix: str) -> list[dict]:
        """某个根会话名下的所有对话 (chain 是 "根会话|对方"), 带每条消息的概况."""
        db = self._load()
        out = []
        for t in db["threads"].values():
            if t["root"] != chain_prefix:
                continue
            out.append({"id": t["id"], "target": t["target"], "msgs": [
                {k: db["messages"][m][k] for k in ("id", "state", "chars", "reply_chars", "n")} for m in t["msgs"] if m in db["messages"]]})
        return out

    def reply(self, mid: str) -> dict:
        data = read_json(self.home / "replies" / f"{mid}.json")
        return data if isinstance(data, dict) else {"text": "", "error": ""}

    # ---- 审计 (不记原文) ----
    def audit(self, **row) -> None:
        row = {"t": round(time.time(), 1), **row}
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if self.audit_path.stat().st_size > 256_000:
                self._prune()
        except OSError:
            pass

    def _prune(self) -> None:
        cutoff = time.time() - AUDIT_KEEP_S
        keep = []
        for line in self.audit_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                if json.loads(line).get("t", 0) >= cutoff:
                    keep.append(line)
            except ValueError:
                continue
        self.audit_path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
