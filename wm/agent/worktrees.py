"""Managed Git worktrees for isolated agent edits.

Creation starts from the source repository's committed HEAD. Uncommitted source
changes are reported, never stashed, copied over, committed or discarded. Review
exports include committed, staged, unstaged and untracked changes relative to
that starting commit. This manager never merges or removes a user's worktree.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path


class WorktreeError(ValueError):
    pass


class GitWorktreeManager:
    def __init__(self, home: Path, git: str | None = None):
        self.home = Path(home).resolve()
        self.git = git or shutil.which("git")
        self._lock = threading.RLock()

    def _run(self, args: list[str], cwd: Path, *, okay=(0,), env_extra=None) -> bytes:
        if not self.git:
            raise WorktreeError("没有找到 Git，无法创建隔离工作副本")
        # A parent process's GIT_DIR / GIT_INDEX_FILE must not redirect this job.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
        env.update(env_extra or {})
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            result = subprocess.run(
                [self.git, "-c", "core.quotepath=false", "-c", "core.hooksPath=" + str(self.home / "no-hooks"), *args],
                cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60, check=False, creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeError(f"Git 操作未完成：{type(exc).__name__}") from exc
        if result.returncode not in okay:
            reason = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
            raise WorktreeError(reason or f"Git 返回退出码 {result.returncode}")
        return result.stdout

    def _load(self) -> dict:
        try:
            data = json.loads((self.home / "registry.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "items": {}}
        except (OSError, ValueError) as exc:
            raise WorktreeError("工作副本记录无法读取，请检查 registry.json") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise WorktreeError("工作副本记录格式无效")
        return data

    def _save(self, data: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        target = self.home / "registry.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)

    def _item(self, wid: str) -> dict:
        if not isinstance(wid, str) or not re.fullmatch(r"wt-[0-9a-f]{16}", wid):
            raise WorktreeError("工作副本编号无效")
        item = self._load()["items"].get(wid)
        if not isinstance(item, dict):
            raise WorktreeError("没有这个受管理的工作副本")
        # Do not trust an editable registry path to point outside managed items.
        expected = self.home / "items" / wid
        root = self.home / "items"
        if root.resolve() != root or expected.resolve().parent != root:
            raise WorktreeError("工作副本目录被重定向到受管理范围之外")
        if Path(item.get("path", "")).resolve() != expected.resolve():
            raise WorktreeError("工作副本路径与记录不一致")
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(item.get("base", ""))):
            raise WorktreeError("工作副本起始版本无效")
        if not expected.is_dir() or not (expected / ".git").is_file():
            raise WorktreeError("工作副本已被移动或删除；记录与补丁仍保留")
        return dict(item)

    def list(self) -> list[dict]:
        return sorted((dict(v) for v in self._load()["items"].values() if isinstance(v, dict)),
                      key=lambda v: -v.get("created", 0))

    def create(self, repo: str | Path, label: str = "") -> dict:
        source = Path(repo).expanduser().resolve()
        if not source.is_dir():
            raise WorktreeError("工作区目录不存在")
        with self._lock:
            try:
                top = self._run(["rev-parse", "--show-toplevel"], source).decode("utf-8").strip()
                source = Path(top).resolve()
                head = self._run(["rev-parse", "--verify", "HEAD^{commit}"], source).decode("ascii").strip()
            except WorktreeError as exc:
                raise WorktreeError("当前目录不是有已提交版本的 Git 工作区，不能创建 worktree") from exc
            dirty = bool(self._run(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], source))
            wid = "wt-" + secrets.token_hex(8)
            target = self.home / "items" / wid
            target.parent.mkdir(parents=True, exist_ok=True)
            data = self._load()
            # Absolute manager-owned path and exact commit ID: neither is a ref from model output.
            self._run(["worktree", "add", "--detach", str(target), head], source)
            item = {"id": wid, "label": str(label).strip()[:120], "repo": str(source), "path": str(target),
                    "base": head, "created": time.time(), "source_dirty": dirty, "merged": False,
                    "note": "从已提交的 HEAD 创建；原工作区未提交的改动未带入" if dirty else "从已提交的 HEAD 创建"}
            data["items"][wid] = item
            self._save(data)
            return dict(item)

    def status(self, wid: str) -> dict:
        item = self._item(wid)
        path = Path(item["path"])
        status = self._run(["status", "--short", "--untracked-files=all"], path).decode("utf-8", errors="replace")
        head = self._run(["rev-parse", "HEAD"], path).decode("ascii").strip()
        changes = self._run(["diff", "--stat", "--no-ext-diff", "--no-textconv", item["base"], "--"], path)
        return {**item, "head": head, "dirty": bool(status), "status": status[:20000],
                "stat": changes.decode("utf-8", errors="replace")[:20000]}

    def _patch(self, item: dict) -> bytes:
        path = Path(item["path"])
        # A private index adds untracked files for the diff without touching the
        # user's real staging area. Git's --binary format also preserves binary assets.
        scratch = self.home / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        index = scratch / (secrets.token_hex(12) + ".index")
        env = {"GIT_INDEX_FILE": str(index)}
        try:
            self._run(["read-tree", "HEAD"], path, env_extra=env)
            self._run(["add", "-A", "--", "."], path, env_extra=env)
            patch = self._run(["diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
                               item["base"], "--"], path, env_extra=env)
            if len(patch) > 32 * 1024 * 1024:
                raise WorktreeError("补丁超过 32 MB，请在工作副本中按文件审查")
            return patch
        finally:
            # The only removed files are this generated temporary index and its lock.
            index.unlink(missing_ok=True)
            index.with_suffix(".index.lock").unlink(missing_ok=True)

    def diff(self, wid: str, max_chars: int = 40000) -> dict:
        with self._lock:
            item = self._item(wid)
            data = self._patch(item)
            text = data.decode("utf-8", errors="replace")
            cap = max(1000, min(200000, int(max_chars)))
            return {"id": wid, "base": item["base"], "text": text[:cap], "truncated": len(text) > cap,
                    "bytes": len(data), "merged": False, "includes_untracked": True}

    def export_patch(self, wid: str) -> dict:
        with self._lock:
            item = self._item(wid)
            data = self._patch(item)
            folder = self.home / "patches"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{wid}-{time.time_ns()}.patch"
            target.write_bytes(data)
            return {"id": wid, "path": str(target), "bytes": len(data), "base": item["base"],
                    "repo": item["repo"], "merged": False, "note": "补丁已导出，尚未合入原工作区"}
