"""文件整理插件 (平台化第一切片的第一个真实插件, 任务 B).

能力:
- `plan(dir)`: 扫描**显式给出**的目录 (不自动扫全盘, 不接受盘符根目录/整个用户目录), 只扫这一层的文件,
  按类型分组, 生成"每个文件打算移到哪"的计划。**默认只出计划, 不动任何文件**。
- `confirm(dir)`: 真移。移动前每个源路径和目标路径都过 `BlacklistStore` (`~/.worldmodel/agent/blacklist.json`)
  的路径保护, 受保护的跳过并报告; 同名目标加 ` (1)` 这类后缀, 绝不覆盖。
- **回执**: 每次 confirm / undo 都往 `~/.worldmodel/plugins/filesorter/receipts.jsonl` 追加一行 JSON:
  动了哪些文件、从哪到哪、时间、耗时、跳过原因。回执 id 用于撤销。
- `undo(receipt_id)`: 按回执把文件移回原位 (倒序); 已经不存在的跳过并报告; 重复调用幂等
  (第二次移回 0 个, 不报错、不改别的文件)。
- **核对 (第四切片)**: confirm / undo 做完都回文件系统核对一遍 (目标路径在、源路径不在), 结果写进
  回执的 `verified`。核对不上时 `ok=false` + `E_RECEIPT_MISMATCH` 并点名没到位的文件, 但仍带回
  已成功的清单和回执 (撤销还要用)。

开关: `WM_PLUGIN_HOST=on` 才可用, 默认关。关着时 plan / confirm / undo 都返回
`{"enabled": False, ...}`, 不读目录、不写回执。路径: 回执目录可用 `WM_FILESORTER_HOME` 覆盖, 路径保护库可用 `WM_BLACKLIST_HOME` 覆盖 (演练/测试用).

说明 (不夸大):
- 只整理给定目录**第一层**的文件, 不递归进子目录 (子目录是整理的目标, 不是要整理的源)。
- 隐藏文件 (以 `.` 开头) 和符号链接不动, 会在 skipped 里报告。
- 回执在动作**做完以后**写: 如果进程在移动中途被杀, 这一次可能没有回执可撤销 (单文件移动本身是原子重命名)。
- 文件系统跨卷时 `os.rename` 会失败, 失败的文件进 skipped, 不会假装移过。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wm.agent.governance import BlacklistStore  # noqa: E402  (要先补仓库根目录才 import 得到)

SWITCH_ENV = "WM_PLUGIN_HOST"
HOME_ENV = "WM_FILESORTER_HOME"
BLACKLIST_HOME_ENV = "WM_BLACKLIST_HOME"
DEFAULT_HOME = Path.home() / ".worldmodel" / "plugins" / "filesorter"
DEFAULT_BLACKLIST_HOME = Path.home() / ".worldmodel" / "agent"
RECEIPTS_NAME = "receipts.jsonl"
RECEIPT_MISMATCH = "E_RECEIPT_MISMATCH"
OTHER = "other"

# 扩展名 -> 子目录. 不认识的 -> other (测试里用 .xyz / 没有扩展名的文件验).
CATEGORIES = (
    ("images", {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}),
    ("documents", {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv"}),
    ("audio", {".mp3", ".wav", ".flac", ".m4a", ".ogg"}),
    ("video", {".mp4", ".mkv", ".avi", ".mov", ".webm"}),
    ("archives", {".zip", ".7z", ".rar", ".tar", ".gz"}),
    ("code", {".py", ".js", ".ts", ".json", ".html", ".css", ".sh", ".ps1"}),
)
CATEGORY_OF = {ext: name for name, exts in CATEGORIES for ext in exts}
MAX_NAME_TRIES = 1000

_OFF_REASON = "插件宿主没有打开：设置环境变量 WM_PLUGIN_HOST=on 才启用（默认关）"


# ---- 开关与路径 ----

def switch_on() -> bool:
    return os.environ.get(SWITCH_ENV, "off").strip().lower() in ("on", "1", "true", "yes")


def _off() -> dict:
    return {"enabled": False, "ok": False, "reason": _OFF_REASON}


def _fail(reason: str) -> dict:
    return {"enabled": True, "ok": False, "reason": reason}


def state_home(home=None) -> Path:
    """回执放哪 (参数 > WM_FILESORTER_HOME > ~/.worldmodel/plugins/filesorter)."""
    if home is not None:
        return Path(home).expanduser().resolve()
    raw = (os.environ.get(HOME_ENV) or "").strip()
    return (Path(raw).expanduser() if raw else DEFAULT_HOME).resolve()


def _blacklist_home(blacklist_home=None) -> Path:
    """路径保护库在哪: 参数 > WM_BLACKLIST_HOME > ~/.worldmodel/agent. 环境变量是给演练/测试隔离用的."""
    if blacklist_home is not None:
        return Path(blacklist_home).expanduser().resolve()
    raw = (os.environ.get(BLACKLIST_HOME_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(DEFAULT_BLACKLIST_HOME).expanduser().resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _check_dir(directory):
    """要扫的目录必须是显式给出的、具体的目录. 返回 (Path, None) 或 (None, 理由)."""
    if directory is None or (isinstance(directory, str) and not directory.strip()):
        return None, "必须用 --dir 明确指定要整理的目录（不自动扫全盘、不默认扫用户目录）"
    try:
        root = Path(directory).expanduser().resolve(strict=True)
    except (OSError, ValueError):
        return None, "目录不存在或读不了：%s" % (directory,)
    if not root.is_dir():
        return None, "不是目录：%s" % root
    if root == Path(root.anchor):
        return None, "不能扫整个盘（%s），请指定具体的目录" % root
    home = Path.home().resolve()
    if root == home:
        return None, "不能直接扫整个用户目录，请指定具体的子目录：%s" % root
    if _inside(home, root):
        return None, "目录范围太大（包含整个用户目录）：%s" % root
    return root, None


def category_for(path) -> str:
    """这个文件该放进哪个子目录. 不认识的扩展名进 other."""
    return CATEGORY_OF.get(Path(path).suffix.lower(), OTHER)


def _unique_path(candidate: Path, taken=None) -> Path:
    """不覆盖已有文件: 同名就加 ` (1)`、` (2)` … 后缀."""
    candidate = Path(candidate)
    if not candidate.exists() and not (taken and os.path.normcase(str(candidate)) in taken):
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for n in range(1, MAX_NAME_TRIES):
        alt = candidate.with_name("%s (%d)%s" % (stem, n, suffix))
        if not alt.exists() and not (taken and os.path.normcase(str(alt)) in taken):
            return alt
    raise OSError("同名文件太多，排不出不重名的名字：%s" % candidate)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return "fs-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ---- 计划 ----

def _scan(root: Path, receipts_home: Path):
    """扫根目录这一层的文件. 返回 ([(Path, size)], [skipped])."""
    files, skipped = [], []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        try:
            if entry.is_symlink():
                skipped.append({"path": str(entry), "why": "链接（不跟随、不动）"})
                continue
            if entry.is_dir():
                continue  # 子目录是整理的目标, 不是要整理的源
            if not entry.is_file():
                continue
            if entry.name.startswith("."):
                skipped.append({"path": str(entry), "why": "隐藏文件（不动）"})
                continue
            if _inside(entry, receipts_home):
                skipped.append({"path": str(entry), "why": "插件自己的回执目录（不动）"})
                continue
            files.append((entry, entry.stat().st_size))
        except OSError as exc:
            skipped.append({"path": str(entry), "why": "读不了：%s" % exc})
    return files, skipped


def plan(directory, *, home=None) -> dict:
    """只看不动: 生成整理计划 (dry-run)."""
    if not switch_on():
        return _off()
    root, why = _check_dir(directory)
    if root is None:
        return _fail(why)
    receipts_home = state_home(home)
    if _inside(root, receipts_home):
        return _fail("不能整理插件自己的回执目录：%s" % root)
    if _inside(receipts_home, root):
        return _fail("这个目录里有插件自己的回执，换一个目录整理：%s" % root)
    files, skipped = _scan(root, receipts_home)
    moves, taken, groups = [], set(), {}
    for src, size in files:
        category = category_for(src)
        try:
            target = _unique_path(root / category / src.name, taken)
        except OSError as exc:
            skipped.append({"path": str(src), "why": str(exc)})
            continue
        taken.add(os.path.normcase(str(target)))
        relative = target.relative_to(root).as_posix()
        moves.append({"src": str(src), "dst": str(target), "target": relative,
                      "category": category, "size": size})
        groups.setdefault(category, []).append(relative)
    return {"enabled": True, "ok": True, "dry_run": True, "dir": str(root),
            "count": len(moves), "moves": moves, "skipped": skipped,
            "groups": groups, "receipts_home": str(receipts_home),
            "reason": "只出计划，没有动文件；确认要移请用 confirm=True（命令行 --confirm）"}


# ---- 移动 ----

def _append_receipt(receipts_home: Path, receipt: dict) -> Path:
    receipts_home.mkdir(parents=True, exist_ok=True)
    path = receipts_home / RECEIPTS_NAME
    line = json.dumps(receipt, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _verify_moved(moved: list) -> dict:
    """回文件系统核对: 每个声称移走的文件, 目标真的在、源真的不在了.

    只核对真正动过的 (moved): 扫描跳过 / 受保护 / rename 失败的项本来就不在 moved 里,
    所以 checked 只数真移的, 不把没动过的算成核对过。
    """
    missing = []
    for item in moved:
        dst, src = Path(item["dst"]), Path(item["src"])
        if not (dst.exists() and not src.exists()):
            missing.append(str(dst))
    return {"checked": len(moved), "ok": len(moved) - len(missing), "missing": missing}


def _verify_restored(restored: list) -> dict:
    """undo 后的核对: 文件真的回到了原位、旧的新位置真的没了."""
    missing = []
    for item in restored:
        to, old = Path(item["to"]), Path(item["from"])
        if not (to.exists() and not old.exists()):
            missing.append(str(to))
    return {"checked": len(restored), "ok": len(restored) - len(missing), "missing": missing}


def _mismatch_message(verb: str, verified: dict) -> str:
    """核对没通过的一句话: 点名哪几个文件没到位 (最多列 5 个, 其余给个数)."""
    names = [Path(path).name for path in verified["missing"]]
    shown = "、".join(names[:5])
    if len(names) > 5:
        shown += " 等 %d 个" % len(names)
    return ("%s后核对没通过：%d 个文件里 %d 个没到位（%s）"
            % (verb, verified["checked"], verified["checked"] - verified["ok"], shown))


def confirm_move(directory, *, home=None, blacklist_home=None) -> dict:
    """真移: 计划 + BlacklistStore 路径保护 + 回执. 受保护/移不动的进 skipped, 不假装做过."""
    if not switch_on():
        return _off()
    planned = plan(directory, home=home)
    if not planned.get("ok"):
        return planned
    root = Path(planned["dir"])
    receipts_home = Path(planned["receipts_home"])
    try:
        store = BlacklistStore(_blacklist_home(blacklist_home))
    except (OSError, ValueError) as exc:
        return _fail("路径保护（BlacklistStore）打不开，没动任何文件：%s" % exc)

    started = time.perf_counter()
    moved, skipped = [], [{"path": s["path"], "why": "扫描时跳过：" + s["why"]} for s in planned["skipped"]]
    for item in planned["moves"]:
        src, dst = Path(item["src"]), Path(item["dst"])
        denied = store.check("write_file", {"path": str(src)}, root) or store.check(
            "write_file", {"path": str(dst)}, root)
        if denied is not None:
            skipped.append({"path": str(src), "why": "受保护路径，跳过：" + denied.why})
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = _unique_path(dst)
            os.rename(src, final)
        except OSError as exc:
            skipped.append({"path": str(src), "why": "没移成：%s" % exc})
            continue
        moved.append({"src": str(src), "dst": str(final), "target": final.relative_to(root).as_posix(),
                      "category": item["category"], "size": item["size"]})

    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    verified = _verify_moved(moved)
    receipt = {"id": _new_id(), "kind": "move", "at": _now(), "ts": time.time(), "dir": str(root),
               "elapsed_ms": elapsed_ms, "files_moved": len(moved), "moves": moved, "skipped": skipped,
               "verified": verified}
    path = _append_receipt(receipts_home, receipt)
    out = {"enabled": True, "ok": True, "dry_run": False, "dir": str(root), "receipt": receipt["id"],
           "receipt_path": str(path), "elapsed_ms": elapsed_ms, "files_moved": len(moved),
           "moved": moved, "skipped": skipped, "verified": verified,
           "reason": "移了 %d 个文件；回执 %s（可 undo）" % (len(moved), receipt["id"])}
    if verified["ok"] != verified["checked"]:
        message = (_mismatch_message("移动", verified)
                   + "；已成功的清单和回执照常给了（回执 %s，可 undo）" % receipt["id"])
        out.update({"ok": False, "code": RECEIPT_MISMATCH, "message": message, "reason": message})
    return out


confirm = confirm_move  # 契约里这个动作叫 confirm


def run(directory, *, confirm=False, home=None, blacklist_home=None) -> dict:
    """一个入口: confirm=False 只出计划 (默认), confirm=True 才真移."""
    if confirm:
        return confirm_move(directory, home=home, blacklist_home=blacklist_home)
    return plan(directory, home=home)


# ---- 回执与撤销 ----

def load_receipts(*, home=None) -> list:
    """读回执 (新到旧). 坏行跳过, 不让一条坏记录毁掉整个撤销."""
    path = state_home(home) / RECEIPTS_NAME
    if not path.is_file():
        return []
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def undo(receipt_id, *, home=None) -> dict:
    """按回执把文件倒序移回原位. 目标已经不在的跳过并报告; 重复调用幂等."""
    if not switch_on():
        return _off()
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        return _fail("要撤销哪条回执？请给回执编号（confirm 的返回里有）")
    receipts_home = state_home(home)
    receipt = next((item for item in reversed(load_receipts(home=receipts_home))
                    if item.get("id") == receipt_id and item.get("kind") == "move"), None)
    if receipt is None:
        return _fail("没有找到这条回执：%s" % receipt_id)

    started = time.perf_counter()
    restored, skipped = [], []
    for item in reversed(receipt.get("moves") or []):
        if not isinstance(item, dict) or not item.get("dst") or not item.get("src"):
            continue
        dst, src = Path(item["dst"]), Path(item["src"])
        if not dst.exists():
            skipped.append({"path": str(dst), "why": "文件已经不在原来的新位置（可能已经移回或删掉）"})
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            final = _unique_path(src)
            os.rename(dst, final)
        except OSError as exc:
            skipped.append({"path": str(dst), "why": "没移回：%s" % exc})
            continue
        restored.append({"from": str(dst), "to": str(final), "size": item.get("size")})

    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    verified = _verify_restored(restored)
    undo_receipt = {"id": _new_id(), "kind": "undo", "undo_of": receipt_id, "at": _now(), "ts": time.time(),
                    "dir": receipt.get("dir"), "elapsed_ms": elapsed_ms,
                    "files_restored": len(restored), "restored": restored, "skipped": skipped,
                    "verified": verified}
    path = _append_receipt(receipts_home, undo_receipt)
    out = {"enabled": True, "ok": True, "dry_run": False, "receipt": undo_receipt["id"], "undo_of": receipt_id,
           "receipt_path": str(path), "elapsed_ms": elapsed_ms, "files_restored": len(restored),
           "restored": restored, "skipped": skipped, "already_undone": not restored and bool(skipped),
           "verified": verified,
           "reason": "移回 %d 个文件（撤销回执 %s）" % (len(restored), undo_receipt["id"])}
    if verified["ok"] != verified["checked"]:
        message = (_mismatch_message("移回", verified)
                   + "；撤销回执 %s 已写，可以再查一遍" % undo_receipt["id"])
        out.update({"ok": False, "code": RECEIPT_MISMATCH, "message": message, "reason": message})
    return out


# ---- 宿主约定入口 (平台化第二切片): ACTIONS = {"<清单动作名>": callable} ----
# 宿主扫描清单 / 建工具表时不 import 本文件; 只有真的 invoke 才导入并执行顶层代码.
# callable 收 **params (参数名来自 wmworld.json 的 plugin.world.actions), 返回 dict.

def entry_plan(dir):  # noqa: A002 - 清单里这个参数名就叫 dir
    """filesorter_plan: 只读计划 (dry-run), 不动任何文件."""
    return plan(dir)


def entry_move(dir, confirm):  # noqa: A002
    """filesorter_move: confirm=True 才真移; 没确认只回一句, 不动文件."""
    if confirm is not True:
        return _fail("还没确认：要真的移动文件请带 confirm=true；想先看计划用 filesorter_plan")
    return confirm_move(dir)


def entry_undo(receipt_id):
    """filesorter_undo: 按回执把文件移回原位."""
    return undo(receipt_id)


ACTIONS = {
    "filesorter_plan": entry_plan,
    "filesorter_move": entry_move,
    "filesorter_undo": entry_undo,
}


# ---- 命令行 ----

def _print_plan(result: dict) -> None:
    print("计划（dry-run，不动文件）：%s，%d 个文件" % (result["dir"], result["count"]))
    for item in result["moves"]:
        print("  %-40s -> %s" % (Path(item["src"]).name, item["target"]))
    for item in result["skipped"]:
        print("  跳过 %s：%s" % (Path(item["path"]).name, item["why"]))
    print(result["reason"])


def _print_move(result: dict) -> None:
    print("回执 %s（耗时 %dms）：移了 %d 个，跳过 %d 个" % (
        result["receipt"], result["elapsed_ms"], result["files_moved"], len(result["skipped"])))
    for item in result["moved"]:
        print("  %-40s -> %s" % (Path(item["src"]).name, item["target"]))
    for item in result["skipped"]:
        print("  跳过 %s：%s" % (Path(item["path"]).name, item["why"]))
    print("回执写到 %s" % result["receipt_path"])


def _print_undo(result: dict) -> None:
    print("撤销回执 %s（新回执 %s，耗时 %dms）：移回 %d 个，跳过 %d 个" % (
        result["undo_of"], result["receipt"], result["elapsed_ms"],
        result["files_restored"], len(result["skipped"])))
    for item in result["restored"]:
        print("  %-40s -> %s" % (Path(item["from"]).name, Path(item["to"]).name))
    for item in result["skipped"]:
        print("  跳过 %s：%s" % (Path(item["path"]).name, item["why"]))


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="文件整理插件: 默认只出计划, --confirm 才真移")
    parser.add_argument("--dir", help="要整理的目录（必须显式给出）")
    parser.add_argument("--confirm", action="store_true", help="真移（默认只出计划）")
    parser.add_argument("--undo", metavar="回执编号", help="按回执把文件移回原位")
    parser.add_argument("--home", help="回执目录（默认 ~/.worldmodel/plugins/filesorter）")
    parser.add_argument("--blacklist-home", help="BlacklistStore 所在目录（默认 ~/.worldmodel/agent）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if args.undo:
        result = undo(args.undo, home=args.home)
    elif not args.dir:
        parser.error("必须用 --dir 指定要整理的目录（不自动扫全盘）")
    elif args.confirm:
        result = confirm_move(args.dir, home=args.home, blacklist_home=args.blacklist_home)
    else:
        result = plan(args.dir, home=args.home)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get("ok"):
        print("没做成：%s" % result.get("reason"))
    elif args.undo:
        _print_undo(result)
    elif result.get("dry_run"):
        _print_plan(result)
    else:
        _print_move(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
