"""记忆核心 (记忆按脑区重设计阶段 1, 2026-11-01): 把她分散在各处的记忆连成一张关系网, 存在 SQLite 里 (默认 ~/.worldmodel/memory/core.db).

阶段 1 是影子: 只从现有的 Markdown、工作经验、小窝经历建索引 (wm/memory_migrate.py 只读地收集), 她检索、回话照旧用 Markdown,
核心不影响她. 以后的阶段 (显著性、情绪、强度、归档、程序) 把各自的量记在这里, 方案见知识库"记忆系统按脑区重设计方案".
  节点  记忆条目、实体 (人、物、地点)、那天的对话、她的日记、做法和经验、她以为东西在哪…… 每个都标来源 (memory_migrate.ORIGINS)
  边    提到 (记忆 -> 实体)、来自那天的对话、更正为、同一天、命令 -> 经验、东西 -> 以为在 / 找到过的地方
  变更  每次重建和上次比: 新加、没了、改了 (状态、来源、想起记录……) 各记一行 (changes 表), 看得到她的记忆是怎么变的
同样的东西每次建出来一样 (编号是内容的短哈希), 重建两次第二次什么都不变; 你在 Obsidian 里改了一行, 只换掉那一条.
行号不算"改了" (上面插一行, 下面的行号都会变), 照样更新.
配置 memory.core: off (默认, 不建库) / shadow (要看时建, 只给看, 不影响检索); memory.core_path 换库的位置
(没设时: 测试、自检改了 trace.log_dir 的放 <log_dir>/memory/, 不然 ~/.worldmodel/memory/, 卸载不删).
命令行 (都不改记忆文件):
  python -m wm.memory_core migrate --dry-run     # 迁移演练: 会建出什么、哪里对不上; 不建库
  python -m wm.memory_core build [--db 路径]      # 建或更新核心
  python -m wm.memory_core stats [--db 路径]      # 有多少节点、边, 最近改了什么
  都可以带 --config / --set (和 python -m wm 一样).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from . import memory_migrate as MM

SCHEMA = 1
MODES = ("off", "shadow")
_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, type TEXT, origin TEXT, status TEXT, text TEXT, file TEXT, line INTEGER,
    day TEXT, data TEXT, sig TEXT, first_seen REAL, updated REAL);
CREATE TABLE IF NOT EXISTS edges (src TEXT, dst TEXT, kind TEXT, weight REAL, data TEXT, updated REAL, PRIMARY KEY (src, dst, kind));
CREATE TABLE IF NOT EXISTS changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, op TEXT, target TEXT, what TEXT, before TEXT,
    after TEXT, by TEXT);
CREATE INDEX IF NOT EXISTS edges_dst ON edges (dst);
CREATE INDEX IF NOT EXISTS nodes_type ON nodes (type);
"""
_CONTENT = ("type", "origin", "status", "text", "file", "day", "data")


def mode(cfg: dict) -> str:
    m = str((cfg.get("memory") or {}).get("core") or "off")
    return m if m in MODES else "off"


def core_path(cfg: dict) -> Path:
    """核心的库文件: memory.core_path (以 .db 结尾就是文件本身, 不然当文件夹); 没设时测试、自检改了 trace.log_dir 的放
    <log_dir>/memory/, 不然 ~/.worldmodel/memory/ (wm/cognition/store.py 的 user_dir, 和她的经历一个规矩)."""
    from .cognition.store import user_dir
    from .config import resolve_path
    p = str((cfg.get("memory") or {}).get("core_path") or "")
    return resolve_path(p) if p.endswith(".db") else user_dir(cfg, "memory", p) / "core.db"


def sources(cfg: dict) -> dict:
    """从哪读: 记忆库 (memory 那段配置)、工作经验在哪 (agent.home)、小窝认知的经历在哪."""
    from .cognition.store import data_dir
    from .config import resolve_path
    home = (cfg.get("agent") or {}).get("home")
    return {"mem_cfg": cfg.get("memory") or {},
            "agent_home": resolve_path(home) if home else Path.home() / ".worldmodel" / "agent",
            "cognition_dir": data_dir(cfg)}


def _sig(n: dict) -> str:
    return hashlib.sha1(json.dumps({k: n.get(k) for k in _CONTENT}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _short(n: dict) -> str:
    return f"{MM.TYPES.get(n.get('type'), n.get('type'))}：{str(n.get('text') or '')[:40]}"


class MemoryCore:
    """核心本身: 一个 SQLite 文件. build 用 memory_migrate.collect 的结果和库里的比, 一个事务里改完, 每处变化记一行."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript(_DDL)
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def build(self, result: dict, by: str = "build") -> dict:
        """把收集来的节点、边写进库: 新的加上、没了的删掉、内容变了的更新, 每处变化记进 changes. 返回这次的小结."""
        t0, now = time.perf_counter(), time.time()
        nodes = result["nodes"]
        edges = {(e["src"], e["dst"], e["kind"]): e for e in result["edges"]}
        out = Counter()
        with self.lock, self.db:
            cur = self.db.cursor()
            old = {r[0]: r for r in cur.execute(f"SELECT id, sig, line, {', '.join(_CONTENT)} FROM nodes")}
            log = []
            for i, n in nodes.items():
                sig, data = _sig(n), json.dumps(n.get("data") or {}, ensure_ascii=False, sort_keys=True)
                row = old.get(i)
                if row is None:
                    cur.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (i, n["type"], n["origin"], n["status"], n["text"], n.get("file"), n.get("line"), n.get("day"), data, sig, now, now))
                    log.append(("add", i, f"新加 {_short(n)}", None, json.dumps({k: n.get(k) for k in _CONTENT}, ensure_ascii=False)))
                    out["added"] += 1
                elif row[1] != sig:
                    before = dict(zip(_CONTENT, row[3:]))
                    before["data"] = json.loads(before["data"] or "{}")
                    diff = {k: (before.get(k), n.get(k)) for k in _CONTENT if before.get(k) != n.get(k)}
                    cur.execute("UPDATE nodes SET type=?, origin=?, status=?, text=?, file=?, line=?, day=?, data=?, sig=?, updated=? WHERE id=?",
                                (n["type"], n["origin"], n["status"], n["text"], n.get("file"), n.get("line"), n.get("day"), data, sig, now, i))
                    what = "、".join(f"{k} {a!s:.30} → {b!s:.30}" for k, (a, b) in diff.items() if k != "data") or "细节变了"
                    log.append(("update", i, f"改了 {_short(n)}（{what}）", json.dumps({k: a for k, (a, _) in diff.items()}, ensure_ascii=False),
                                json.dumps({k: b for k, (_, b) in diff.items()}, ensure_ascii=False)))
                    out["changed"] += 1
                elif row[2] != n.get("line"):
                    cur.execute("UPDATE nodes SET line=? WHERE id=?", (n.get("line"), i))  # 行号挪了不算改
            for i in sorted(set(old) - set(nodes)):
                row = old[i]
                gone = dict(zip(_CONTENT, row[3:]))
                cur.execute("DELETE FROM nodes WHERE id=?", (i,))
                log.append(("remove", i, f"没了 {_short(gone)}", json.dumps(gone, ensure_ascii=False), None))
                out["removed"] += 1
            old_e = {(r[0], r[1], r[2]): (r[3], r[4]) for r in cur.execute("SELECT src, dst, kind, weight, data FROM edges")}
            for key, e in edges.items():
                data = json.dumps(e.get("data") or {}, ensure_ascii=False, sort_keys=True)
                if key not in old_e:
                    cur.execute("INSERT INTO edges VALUES (?,?,?,?,?,?)", (*key, e["weight"], data, now))
                    out["edges_added"] += 1
                elif old_e[key] != (e["weight"], data):
                    cur.execute("UPDATE edges SET weight=?, data=?, updated=? WHERE src=? AND dst=? AND kind=?", (e["weight"], data, now, *key))
                    out["edges_changed"] += 1
            for key in set(old_e) - set(edges):
                cur.execute("DELETE FROM edges WHERE src=? AND dst=? AND kind=?", key)
                out["edges_removed"] += 1
            cur.executemany("INSERT INTO changes (at, op, target, what, before, after, by) VALUES (?,?,?,?,?,?,?)",
                            [(now, op, t, what, b, a, by) for op, t, what, b, a in log])
            for k, v in (("built", now), ("nodes", len(nodes)), ("edges", len(edges)), ("problems", json.dumps(result.get("problems") or [], ensure_ascii=False))):
                cur.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))
        return {"nodes": len(nodes), "edges": len(edges), **{k: out[k] for k in ("added", "removed", "changed", "edges_added", "edges_removed", "edges_changed")},
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    def stats(self) -> dict:
        with self.lock:
            q = self.db.execute
            meta = dict(q("SELECT key, value FROM meta").fetchall())
            return {"path": str(self.path), "nodes": q("SELECT COUNT(*) FROM nodes").fetchone()[0],
                    "edges": q("SELECT COUNT(*) FROM edges").fetchone()[0], "changes": q("SELECT COUNT(*) FROM changes").fetchone()[0],
                    "types": dict(q("SELECT type, COUNT(*) FROM nodes GROUP BY type").fetchall()),
                    "origins": dict(q("SELECT origin, COUNT(*) FROM nodes GROUP BY origin").fetchall()),
                    "statuses": dict(q("SELECT status, COUNT(*) FROM nodes GROUP BY status").fetchall()),
                    "edge_kinds": dict(q("SELECT kind, COUNT(*) FROM edges GROUP BY kind").fetchall()),
                    "built": float(meta["built"]) if meta.get("built") else None, "schema": meta.get("schema")}

    def changes(self, n: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT seq, at, op, target, what, by FROM changes ORDER BY seq DESC LIMIT ?", (n,)).fetchall()
        return [{"seq": s, "at": a, "op": o, "target": t, "what": w, "by": b} for s, a, o, t, w, b in rows]

    def graph(self, types: list[str] | None = None, q: str | None = None, limit: int = 400) -> dict:
        """给关系图看的: 节点 (按连了几条排, 多的在前, 最多 limit 个) 和它们之间的边."""
        with self.lock:
            deg = Counter()
            for s, d in self.db.execute("SELECT src, dst FROM edges"):
                deg[s] += 1
                deg[d] += 1
            sql, args = "SELECT id, type, origin, status, text, file, line, day, data FROM nodes", []
            where = []
            if types:
                where.append(f"type IN ({','.join('?' * len(types))})")
                args += types
            if q:
                where.append("text LIKE ?")
                args.append(f"%{q}%")
            rows = self.db.execute(sql + (" WHERE " + " AND ".join(where) if where else ""), args).fetchall()
            rows.sort(key=lambda r: (-deg[r[0]], r[1], r[0]))
            rows = rows[:limit]
            keep = {r[0] for r in rows}
            edges = [{"src": s, "dst": d, "kind": k, "weight": w}
                     for s, d, k, w in self.db.execute("SELECT src, dst, kind, weight FROM edges") if s in keep and d in keep]
        nodes = [{"id": i, "type": t, "type_label": MM.TYPES.get(t, t), "origin": o, "origin_label": MM.ORIGINS.get(o, o), "status": st,
                  "text": tx, "file": f, "line": ln, "day": dy, "data": json.loads(da or "{}"), "degree": deg[i]}
                 for i, t, o, st, tx, f, ln, dy, da in rows]
        return {"nodes": nodes, "edges": edges}


class CoreService:
    """服务端用的影子核心: 第一次要看时才开库、建索引, 之后隔 min_gap 秒以上才重建 (在线程池里调; 不碰检索用的 Memory)."""

    def __init__(self, cfg: dict, min_gap: float = 30.0):
        self.cfg, self.min_gap = cfg, min_gap
        self.enabled = mode(cfg) != "off"
        self.core: MemoryCore | None = None
        self.last = 0.0
        self.summary: dict = {}
        self.problems: list[str] = []
        self.cache: dict = {}  # 文件 -> (修改时间, 切好的块, 各行): 没改的文件不重切
        self.lock = threading.Lock()

    def refresh(self, force: bool = False) -> dict:
        with self.lock:
            if self.core is None:
                self.core = MemoryCore(core_path(self.cfg))
            if force or time.time() - self.last >= self.min_gap:
                src = sources(self.cfg)
                result = MM.collect(src["mem_cfg"], src["agent_home"], src["cognition_dir"], self.cache)
                self.summary, self.problems, self.last = self.core.build(result, by="shadow"), result["problems"], time.time()
            return self.summary

    def view(self, types=None, q=None, limit: int = 400, force: bool = False) -> dict:
        self.refresh(force)
        return {"mode": mode(self.cfg), "built": self.last, "build": self.summary, "problems": self.problems,
                **self.core.graph(types, q, limit), "stats": self.core.stats(), "changes": self.core.changes(30),
                "types": MM.TYPES, "origins": MM.ORIGINS}


def main(argv: list[str] | None = None) -> int:
    from .config import load_config
    ap = argparse.ArgumentParser(prog="python -m wm.memory_core", description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["migrate", "build", "stats"])
    ap.add_argument("--dry-run", action="store_true", help="migrate: 只演练, 不建库 (阶段 1 只有演练)")
    ap.add_argument("--db", help="核心的库文件 (默认按配置: memory.core_path, 或 ~/.worldmodel/memory/core.db)")
    ap.add_argument("--json", help="migrate: 另把收集结果存成 JSON (节点、边、对不上的地方)")
    ap.add_argument("--config", help="额外的 JSON 配置文件")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args(argv)
    cfg = load_config(a.config, a.set)
    if not (cfg.get("memory") or {}).get("vault"):
        print("没配 memory.vault (长期记忆库), 没有可读的")
        return 1
    src = sources(cfg)
    if a.cmd == "migrate":
        if not a.dry_run:
            print("阶段 1 只做演练: 加 --dry-run. 真的迁移在阶段 9 (知识库\"记忆系统按脑区重设计方案\").")
            return 2
        result = MM.collect(src["mem_cfg"], src["agent_home"], src["cognition_dir"])
        print(MM.report(result))
        if a.json:
            Path(a.json).write_text(json.dumps({**result, "nodes": list(result["nodes"].values())}, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    core = MemoryCore(a.db or core_path(cfg))
    try:
        if a.cmd == "build":
            result = MM.collect(src["mem_cfg"], src["agent_home"], src["cognition_dir"])
            print(json.dumps(core.build(result), ensure_ascii=False))
            for p in result["problems"]:
                print("对不上:", p)
        print(json.dumps(core.stats(), ensure_ascii=False, indent=1))
        for c in core.changes(15):
            print(f"  #{c['seq']} {time.strftime('%m-%d %H:%M', time.localtime(c['at']))} {c['what']}")
    finally:
        core.close()
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
