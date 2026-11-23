"""待办插件 (平台化第七切片, 任务 ③: 第三个插件 + 单一真相源).

能力:
- `todo_add(text, due, source)`: 加一条待办。文字必填 (最多 200 字); `due` 是可选的截止提示 (最多 32 字,
  只原样记、不解析成时间); `source` 可选, 用来记这条待办是从哪句原话来的 (最多 200 字, 只当文字存).
- `todo_list(include_done)`: 列出待办。默认只看没做完的, `include_done=true` 连做完的一起列; 只读, 不动文件.
- `todo_done(id)`: 把一条待办标成做完 —— **只写 `done_at`, 不删行**; 重复调幂等 (第二次不再写文件).
- `todo_undo(id)`: 撤销"做完"这个标记, 回到没做完; 重复调幂等 (本来就没做完时什么都不写).
- 每次 act 往 `receipts.jsonl` 追加一行回执; 回执里带核对结果, 回执 id 只是记录, 撤销按待办 id 不按回执.
- **核对**: 每次 act 后都回读 `todos.jsonl` 核对 (这一条真的在 / 真的标上了 / 真的回到了没做完),
  结果写进返回的 `verified`. 核对不上时 `ok=false` + `E_RECEIPT_MISMATCH`, 但仍带回已写下的内容.

数据位置: `~/.worldmodel/plugins/todolist/todos.jsonl` (一行一条) 和同目录的 `receipts.jsonl` (一行一个动作);
目录可用 `WM_TODO_HOME` 覆盖 (演练/测试用). 这是插件自己的文件: **不写她的记忆库, 不写 vault**.

单一真相源的边界 (详见 `docs/roadmap/single-source-of-truth-todo.md`):
- 她说过的"安排"由记忆 (`记忆/关于用户.md` 里带日期的事实) 负责, 本插件不复制、也不改记忆;
- "这条待办做没做、撤没撤销"只有本插件的 `done_at` 负责, 记忆里不记完成状态;
- `source` 只是把两边对得上的线索, 不是同步通道。

开关: `WM_PLUGIN_HOST=on` 才可用, 默认关。关着时四个动作都返回 `{"enabled": False, ...}`,
不读也不写任何文件。

说明 (不夸大):
- 没有提醒/到期推送, 没有优先级、标签、重复任务; 截止提示只是一段文字。
- `todos.jsonl` 是追加写; 只有标完成/撤销时才重写一次文件 (先写临时文件再原子替换), 坏行原样保留、跳过不炸。
- 不做跨进程同步: 记忆那边没有能反向写进来的接口, 插件也不去改她的记忆。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

SWITCH_ENV = "WM_PLUGIN_HOST"
HOME_ENV = "WM_TODO_HOME"
DEFAULT_HOME = Path.home() / ".worldmodel" / "plugins" / "todolist"
TODOS_NAME = "todos.jsonl"
RECEIPTS_NAME = "receipts.jsonl"
RECEIPT_MISMATCH = "E_RECEIPT_MISMATCH"
MAX_TEXT = 200
MAX_DUE = 32
MAX_SOURCE = 200
MAX_ID = 64

_OFF_REASON = "插件宿主没有打开：设置环境变量 WM_PLUGIN_HOST=on 才启用（默认关）"
# 时间戳 + 短哈希: 好认、不至于两台机器上撞同一个 id
_ID = re.compile(r"^td-\d{8}-\d{6}-[0-9a-f]{6}$")


# ---- 开关与路径 ----

def switch_on() -> bool:
    return os.environ.get(SWITCH_ENV, "off").strip().lower() in ("on", "1", "true", "yes")


def _off() -> dict:
    return {"enabled": False, "ok": False, "reason": _OFF_REASON}


def _fail(reason: str) -> dict:
    return {"enabled": True, "ok": False, "reason": reason}


def state_home(home=None) -> Path:
    """待办和回执放哪 (参数 > WM_TODO_HOME > ~/.worldmodel/plugins/todolist)."""
    if home is not None:
        return Path(home).expanduser().resolve()
    raw = (os.environ.get(HOME_ENV) or "").strip()
    return (Path(raw).expanduser() if raw else DEFAULT_HOME).resolve()


def _now() -> datetime:
    return datetime.now()


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return prefix + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ---- 文件 ----

def _append_jsonl(path: Path, record: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def _read_raw(home_dir: Path) -> tuple[list[str], int]:
    """todos.jsonl 的原始行 (空行丢掉) 和坏行数 (解析不了的行原样留着, 只报数)."""
    path = home_dir / TODOS_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], 0
    lines, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            bad += 1
            lines.append(line)
            continue
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            bad += 1
        lines.append(line)
    return lines, bad


def _write_raw(home_dir: Path, lines: list[str]) -> Path:
    """整份重写 (先写临时文件再原子替换; 坏行原样留在 lines 里)."""
    home_dir.mkdir(parents=True, exist_ok=True)
    path = home_dir / TODOS_NAME
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line.rstrip("\r\n") + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def _entries(home_dir: Path) -> tuple[list[dict], list[str], int]:
    """(好条目, 对应的原始行, 坏行数)."""
    lines, bad = _read_raw(home_dir)
    entries = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            entries.append(item)
    return entries, lines, bad


def _find(home_dir: Path, todo_id: str) -> tuple[dict | None, int]:
    entries, _lines, bad = _entries(home_dir)
    for entry in entries:
        if entry.get("id") == todo_id:
            return entry, bad
    return None, bad


def _mutate(home_dir: Path, todo_id: str, change) -> bool:
    """把 id 对上的一条改掉, 其余行原样保留; 返回有没有改到."""
    _entries_now, lines, _bad = _entries(home_dir)
    out, changed = [], False
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if not isinstance(item, dict) or item.get("id") != todo_id:
            out.append(line)
            continue
        new = change(dict(item))
        out.append(json.dumps(new, ensure_ascii=False, sort_keys=True))
        changed = True
    if changed:
        _write_raw(home_dir, out)
    return changed


def _write_receipt(home_dir: Path, kind: str, entry: dict, verified: dict) -> tuple[str, Path]:
    receipt = {"id": _new_id("rc-"), "kind": kind, "at": entry.get("created_at"), "ts": time.time(),
               "todo_id": entry.get("id"), "entry": entry, "verified": verified}
    path = _append_jsonl(home_dir / RECEIPTS_NAME, receipt)
    return receipt["id"], path


# ---- 核对 ----

def _verify_present(home_dir: Path, todo_id: str) -> dict:
    entry, _bad = _find(home_dir, todo_id)
    return {"checked": 1, "ok": int(entry is not None)}


def _verify_done(home_dir: Path, todo_id: str, want_done: bool) -> dict:
    entry, _bad = _find(home_dir, todo_id)
    got = bool(entry and entry.get("done_at"))
    return {"checked": 1, "ok": int(entry is not None and got == want_done)}


# ---- 入参 ----

def _text(value, limit: int, name: str) -> tuple[str | None, str | None]:
    if value is None:
        return "", None
    if not isinstance(value, str):
        return None, "%s 要是字符串" % name
    text = value.strip()
    if len(text) > limit:
        return None, "%s 最多 %d 字" % (name, limit)
    return text, None


def _todo_id(value) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "待办 id 必填"
    todo_id = value.strip()
    if len(todo_id) > MAX_ID:
        return None, "待办 id 太长（最多 %d 字）" % MAX_ID
    if not _ID.match(todo_id):
        return None, "待办 id 不对：%s" % todo_id
    return todo_id, None


def _mismatch(message: str, home_dir: Path, verified: dict) -> dict:
    return {"enabled": True, "ok": False, "code": RECEIPT_MISMATCH, "message": message,
            "ledger_path": str(home_dir / TODOS_NAME), "verified": verified}


# ---- 动作 ----

def add(text, due=None, source=None, *, home=None) -> dict:
    """加一条待办。"""
    if not switch_on():
        return _off()
    body, why = _text(text, MAX_TEXT, "待办内容")
    if why:
        return _fail(why)
    if not body:
        return _fail("待办内容不能是空的")
    due_text, why = _text(due, MAX_DUE, "截止提示")
    if why:
        return _fail(why)
    source_text, why = _text(source, MAX_SOURCE, "来源")
    if why:
        return _fail(why)
    home_dir = state_home(home)
    now = _now()
    entry = {"id": _new_id("td-"), "text": body, "due": due_text or None, "source": source_text or None,
             "created_at": _stamp(now), "done_at": None}
    ledger_path = home_dir / TODOS_NAME
    try:
        _append_jsonl(ledger_path, entry)
    except OSError as exc:
        return _mismatch("写待办文件失败：%s" % exc, home_dir, {"checked": 1, "ok": 0})
    verified = _verify_present(home_dir, entry["id"])
    receipt = receipt_path = None
    try:
        receipt, receipt_path = _write_receipt(home_dir, "add", entry, verified)
    except OSError:
        pass
    out = {"enabled": True, "ok": True, "id": entry["id"], "entry": entry, "text": body,
           "due": due_text or None, "source": source_text or None, "done": False,
           "ledger_path": str(ledger_path), "receipt": receipt,
           "receipt_path": str(receipt_path) if receipt_path else None,
           "verified": verified, "reason": "加了一条待办：%s" % body}
    if verified["ok"] != verified["checked"]:
        out.update(_mismatch("加完回读待办文件没对上", home_dir, verified))
    return out


def list_todos(include_done=False, *, home=None) -> dict:
    """列出待办; 默认只看没做完的。"""
    if not switch_on():
        return _off()
    if not isinstance(include_done, bool):
        return _fail("include_done 要是 true / false")
    home_dir = state_home(home)
    entries, _lines, bad = _entries(home_dir)
    done_count = sum(1 for entry in entries if entry.get("done_at"))
    items = [entry for entry in entries if include_done or not entry.get("done_at")]
    open_count = len(entries) - done_count
    if not entries:
        reason = "还没有待办"
    else:
        reason = "待办 %d 条（没做完 %d 条，做完 %d 条）" % (len(entries), open_count, done_count)
    return {"enabled": True, "ok": True, "items": items, "total": len(entries),
            "open": open_count, "done": done_count, "include_done": include_done,
            "skipped_lines": bad, "ledger_path": str(home_dir / TODOS_NAME), "reason": reason}


def done(todo_id, *, home=None) -> dict:
    """把一条待办标成做完 (只写 done_at, 不删行); 幂等。"""
    if not switch_on():
        return _off()
    tid, why = _todo_id(todo_id)
    if why:
        return _fail(why)
    home_dir = state_home(home)
    entry, _bad = _find(home_dir, tid)
    if entry is None:
        return _fail("没有这条待办：%s" % tid)
    already = bool(entry.get("done_at"))
    if not already:
        stamp = _stamp(_now())

        def mark(item: dict) -> dict:
            item["done_at"] = stamp
            return item

        try:
            _mutate(home_dir, tid, mark)
        except OSError as exc:
            return _mismatch("写待办文件失败：%s" % exc, home_dir, {"checked": 1, "ok": 0})
        entry, _bad = _find(home_dir, tid)
    verified = _verify_done(home_dir, tid, True)
    receipt = receipt_path = None
    if not already:
        try:
            receipt, receipt_path = _write_receipt(home_dir, "done", entry, verified)
        except OSError:
            pass
    out = {"enabled": True, "ok": True, "id": tid, "entry": entry, "already_done": already,
           "done_at": entry.get("done_at"), "ledger_path": str(home_dir / TODOS_NAME),
           "receipt": receipt, "receipt_path": str(receipt_path) if receipt_path else None,
           "verified": verified,
           "reason": "这条之前就做完了：%s" % entry.get("text") if already else "做完了：%s" % entry.get("text")}
    if verified["ok"] != verified["checked"]:
        out.update(_mismatch("标完回读待办文件没对上", home_dir, verified))
    return out


def undo(todo_id, *, home=None) -> dict:
    """撤销"做完"标记, 回到没做完; 幂等。"""
    if not switch_on():
        return _off()
    tid, why = _todo_id(todo_id)
    if why:
        return _fail(why)
    home_dir = state_home(home)
    entry, _bad = _find(home_dir, tid)
    if entry is None:
        return _fail("没有这条待办：%s" % tid)
    already = not entry.get("done_at")
    if not already:

        def clear(item: dict) -> dict:
            item["done_at"] = None
            return item

        try:
            _mutate(home_dir, tid, clear)
        except OSError as exc:
            return _mismatch("写待办文件失败：%s" % exc, home_dir, {"checked": 1, "ok": 0})
        entry, _bad = _find(home_dir, tid)
    verified = _verify_done(home_dir, tid, False)
    receipt = receipt_path = None
    if not already:
        try:
            receipt, receipt_path = _write_receipt(home_dir, "undone", entry, verified)
        except OSError:
            pass
    out = {"enabled": True, "ok": True, "id": tid, "entry": entry, "already_open": already,
           "done_at": None, "ledger_path": str(home_dir / TODOS_NAME), "receipt": receipt,
           "receipt_path": str(receipt_path) if receipt_path else None, "verified": verified,
           "reason": "这条本来就没做完：%s" % entry.get("text") if already else "改回没做完：%s" % entry.get("text")}
    if verified["ok"] != verified["checked"]:
        out.update(_mismatch("撤销完回读待办文件没对上", home_dir, verified))
    return out


# ---- 入口层 (plugin_worker 按 ACTIONS 里的名字调这几个) ----

def entry_add(text, due=None, source=None):
    return add(text, due, source)


def entry_list(include_done=None):
    return list_todos(bool(include_done) if include_done is not None else False)


def entry_done(id):  # noqa: A002 - 参数名和清单里的 params 对齐 (worker 按 params 名调)
    return done(id)


def entry_undo(id):  # noqa: A002
    return undo(id)


ACTIONS = {"todo_add": entry_add, "todo_list": entry_list, "todo_done": entry_done, "todo_undo": entry_undo}


# ---- 命令行 (演练/排查用, 和动作走同一套函数) ----

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="待办插件")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("text")
    p_add.add_argument("--due", default=None)
    p_add.add_argument("--source", default=None)
    p_list = sub.add_parser("list")
    p_list.add_argument("--include-done", action="store_true")
    p_done = sub.add_parser("done")
    p_done.add_argument("id")
    p_undo = sub.add_parser("undo")
    p_undo.add_argument("id")
    for p in (p_add, p_list, p_done, p_undo):
        p.add_argument("--home", default=None)
        p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "add":
        out = add(args.text, args.due, args.source, home=args.home)
    elif args.cmd == "list":
        out = list_todos(args.include_done, home=args.home)
    elif args.cmd == "done":
        out = done(args.id, home=args.home)
    else:
        out = undo(args.id, home=args.home)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out.get("reason") or out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
