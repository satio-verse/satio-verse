"""User-operated workspace inspector and owned, cancellable terminal jobs.

The caller must enforce the app's Host/Origin checks. Files stay inside the
selected workspace; a UI command is an explicit user action, not model consent.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from aiohttp import web

from . import office, tools

MAX_TEXT = 1024 * 1024
MAX_OUTPUT = 200_000


def file_path(session, value="") -> Path:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("文件路径无效")
    root = Path(session.workspace).resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("此处只浏览当前工作区，请先切换工作区")
    return path


def inspect_files(session, value="", *, offset=0, blacklist=None) -> dict:
    path = file_path(session, value)
    root = Path(session.workspace).resolve()
    relative = path.relative_to(root).as_posix()
    if blacklist is not None:
        denied = blacklist.check('list_dir' if path.is_dir() else 'read_file', {'path': str(path)}, root)
        if denied:
            raise PermissionError(denied.why)
    if path.is_dir():
        entries = []
        for child in path.iterdir():
            try:
                resolved = child.resolve()
                if not resolved.is_relative_to(root):
                    continue
                if blacklist is not None and blacklist.check('list_dir' if child.is_dir() else 'read_file', {'path': str(resolved)}, root):
                    continue
                info = child.stat()
                entries.append({"name": child.name, "path": child.relative_to(root).as_posix(),
                                "directory": child.is_dir(), "size": info.st_size, "modified": info.st_mtime})
            except OSError:
                continue
        entries.sort(key=lambda row: (not row["directory"], row["name"].casefold()))
        offset = max(0, int(offset))
        return {"kind": "directory", "path": relative, "parent": path.parent.relative_to(root).as_posix() if path != root else None,
                "entries": entries[offset:offset + 200], "total": len(entries), "offset": offset,
                "next_offset": offset + 200 if offset + 200 < len(entries) else None}
    if not path.is_file():
        raise ValueError("找不到这个文件")
    if path.stat().st_size > MAX_TEXT:
        raise ValueError("文件超过 1 MB，请使用材料分页阅读或在编辑器打开")
    with path.open('rb') as source:
        raw = source.read(MAX_TEXT + 1)
    if len(raw) > MAX_TEXT:
        raise ValueError('文件读取期间增长超过 1 MB')
    if b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("这是二进制文件，请添加到任务材料中预览")
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        content = raw.decode(encoding)
    except UnicodeError:
        encoding = "gb18030"
        content = raw.decode(encoding)
    return {"kind": "file", "path": relative, "name": path.name, "text": content,
            "size": len(raw), "encoding": encoding, "sha256": hashlib.sha256(raw).hexdigest(),
            "markdown": path.suffix.lower() in {".md", ".markdown"}}


# 全工作区找文件 (工作台 Ctrl+P, 2026-11-01 照 Codex 改版): 只读、只列名字, 跳过依赖和缓存目录; 扫到上限就停, 不阻塞服务端
FIND_SKIP = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache",
             ".mypy_cache", ".tox", "site-packages", "webview2-data", "browser-data", ".archive"}
FIND_LIMIT = 200
FIND_SCAN = 20000
FIND_SECONDS = 1.5


def find_files(session, query="", *, blacklist=None, limit=FIND_LIMIT) -> dict:
    """名字里含查询的每个词 (不分大小写、路径分隔符都当 /) 的文件; 文件名里命中、路径短的排前面. 空查询给最近改过的."""
    root = Path(session.workspace).resolve()
    text = str(query or "").strip().lower().replace("\\", "/")
    if len(text) > 200:
        raise ValueError("查询太长")
    terms = text.split()
    found, scanned, truncated, started = [], 0, False, time.monotonic()
    for directory, folders, files in os.walk(root):
        here = Path(directory)
        if blacklist is not None and here != root and blacklist.check('list_dir', {'path': str(here)}, root):
            folders[:] = []
            continue
        folders[:] = sorted(name for name in folders if name not in FIND_SKIP)
        for name in files:
            scanned += 1
            if scanned > FIND_SCAN or time.monotonic() - started > FIND_SECONDS:
                truncated = True
                break
            relative = (here / name).relative_to(root).as_posix()
            lower = relative.lower()
            if terms and not all(term in lower for term in terms):
                continue
            base = name.lower()
            if terms:
                score = (0 if base.startswith(terms[-1]) else 1 if all(term in base for term in terms) else 2, len(relative))
            else:
                score = (0.0, 0)
                with contextlib.suppress(OSError):
                    score = (-(here / name).stat().st_mtime, 0)
            found.append((score, relative, name))
        if truncated:
            break
    found.sort()
    items = []
    for _, relative, name in found:
        if len(items) >= max(1, min(int(limit), FIND_LIMIT)):
            break
        if blacklist is not None and blacklist.check('read_file', {'path': str(root / relative)}, root):
            continue
        items.append({"path": relative, "name": name})
    return {"items": items, "scanned": min(scanned, FIND_SCAN), "truncated": truncated}


def _change_text(path: Path) -> str | None:
    """和 core 记改动时读法一样 (UTF-8、换行统一成 \\n): 用来核对文件现在是不是那次改完的样子."""
    if not path.is_file():
        return ''
    if path.stat().st_size > 240000:
        return None
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        text = stream.read(60001)
    return text if len(text) <= 60000 else None


def revert_changes(session, calls, *, blacklist=None) -> dict:
    """撤销这几次工具改动 (工作台对话里"已编辑 N 个文件 · 撤销"): 只在文件现在正是那次改完的样子时写回改前的内容, 被再改过的
    一个都不动 (先全部核对再写); 这一轮新建的文件不删, 挪进 <工作区>/.archive/撤销-<时间>/ 保留原来的目录结构."""
    if not isinstance(calls, list) or not calls or len(calls) > 200:
        raise ValueError("要撤销哪些改动？")
    wanted = {str(call) for call in calls}
    changes = [(str(e.get("id")), e["change"]) for e in session.events
               if e.get("type") == "tool_result" and str(e.get("id")) in wanted and isinstance(e.get("change"), dict)]
    if wanted - {cid for cid, _ in changes}:
        raise ValueError("找不到这些改动的完整记录（文件太大时不保存改前内容），不能撤销")
    root = Path(session.workspace).resolve()
    files = {}
    for cid, change in changes:  # 同一个文件改了几次: 核对最后一次改完的样子, 写回第一次改之前的
        path = Path(str(change.get("path") or "")).resolve()
        entry = files.setdefault(os.path.normcase(str(path)), {"path": path, "before": change.get("before"), "calls": []})
        entry["after"] = change.get("after")
        entry["calls"].append(cid)
    plan, blocked = [], []
    for entry in files.values():
        path = entry["path"]
        shown = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        why = ""
        if not path.is_relative_to(root):
            why = "不在当前工作区里"
        elif blacklist is not None and (denied := blacklist.check('write_file', {'path': str(path)}, root)):
            why = denied.why
        elif not isinstance(entry["before"], str) or not isinstance(entry["after"], str):
            why = "没有保存完整的改前内容"
        elif "�" in entry["before"]:
            why = "改前的内容里有不是 UTF-8 的字节，不能原样还原"
        else:
            current = _change_text(path)
            if current is None:
                why = "文件现在太大，没法核对"
            # 文件按文本读 (换行统一成 \n); 改动记录以前也统一过, 2026-11-07 起原样记 CRLF —— 两边按同一规则统一再比, 新旧记录都认
            elif current != entry["after"].replace("\r\n", "\n").replace("\r", "\n"):
                why = "之后又被改过，为了不盖掉新的内容没有撤销"
        if why:
            blocked.append({"path": shown, "why": why})
        else:
            plan.append({**entry, "shown": shown})
    if blocked:
        return {"ok": False, "reverted": [], "archived": [], "blocked": blocked}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    reverted, archived = [], []
    for item in plan:
        path = item["path"]
        if item["before"] == "":  # 这一轮新建的: 挪进归档, 不删
            if path.exists():
                target = root / ".archive" / f"撤销-{stamp}" / item["shown"]
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
                archived.append({"path": item["shown"], "archive": target.relative_to(root).as_posix(), "calls": item["calls"]})
            continue
        raw = path.read_bytes() if path.exists() else b""
        text = item["before"].replace("\r\n", "\n")
        if b"\r\n" in raw:  # 记下来的改前内容换行统一成了 \n; 文件现在是 CRLF 就照 CRLF 写回 (edit_file 按字节改, 不动换行)
            text = text.replace("\n", "\r\n")
        temporary = path.with_name(path.name + ".wm-revert")
        temporary.write_bytes(text.encode("utf-8"))  # 改前内容带 BOM 时第一个字就是 ﻿, 照样写回
        os.replace(temporary, path)
        reverted.append({"path": item["shown"], "calls": item["calls"]})
    return {"ok": True, "reverted": reverted, "archived": archived, "blocked": []}


class _CommandJob:
    """An owned Windows Job Object closes detached descendants on terminal exit."""
    def __init__(self, pid):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes as w
        size_t = ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', w.DWORD),
                        ('min_working', size_t), ('max_working', size_t), ('active', w.DWORD),
                        ('affinity', size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', size_t), ('job_memory', size_t),
                        ('peak_process', size_t), ('peak_job', size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits = Extended(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not self.kernel.AssignProcessToJobObject(handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = handle
        except Exception:
            self.kernel.CloseHandle(handle)
            raise
        finally:
            if process:
                self.kernel.CloseHandle(process)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WorkspaceJobs:
    def __init__(self, hub):
        self.hub = hub
        self.records = {}
        self.tasks = {}
        self.stops = {}
        self.closed = False

    def list(self, sid):
        return [dict(row) for row in self.records.values() if row["session"] == sid][-20:]

    def start(self, session, command):
        if self.closed:
            raise ValueError('终端正在关闭')
        if not isinstance(command, str) or not command.strip() or len(command) > 32000:
            raise ValueError("请输入有效的 PowerShell 命令")
        if any(row["state"] == "running" and row["session"] == session.id for row in self.records.values()):
            raise ValueError("当前命令仍在运行，可先停止或等待完成")
        # Direct user execution is deliberate, while explicit read-only and blacklist remain meaningful.
        self._authorize(session, command)
        self._prune()
        jid = "terminal-" + secrets.token_hex(8)
        self.records[jid] = {"id": jid, "session": session.id, "command": command, "workspace": session.workspace,
                             "started": time.time(), "state": "running", "output": "", "exit_code": None, "truncated": False}
        self.stops[jid] = asyncio.Event()
        self.tasks[jid] = asyncio.create_task(self._run(jid))
        def finished(task):
            if task.cancelled() and self.records[jid]['state'] == 'running':
                self.records[jid].update(state='cancelled', ended=time.time())
        self.tasks[jid].add_done_callback(finished)
        return dict(self.records[jid])

    def _authorize(self, session, command):
        mode = self.hub.teams.effective_mode(session) if hasattr(self.hub, 'teams') else session.mode
        if mode == "read" or session.preset == "plan":
            raise ValueError("当前为只读 / 计划模式，请先切换执行模式")
        denied = self.hub.blacklist.check("run_command", {"command": command, "workdir": session.workspace}, Path(session.workspace))
        if denied:
            raise PermissionError(denied.why)
    def _prune(self):
        finished = [jid for jid, row in self.records.items() if row['state'] != 'running' and self.tasks[jid].done()]
        counts = {}
        keep = set()
        for jid in reversed(finished):
            sid = self.records[jid]['session']
            counts[sid] = counts.get(sid, 0) + 1
            if len(keep) < 200 and counts[sid] <= 20:
                keep.add(jid)
        for jid in finished:
            if jid not in keep:
                self.records.pop(jid); self.tasks.pop(jid); self.stops.pop(jid)

    async def _run(self, jid):
        row, stop = self.records[jid], self.stops[jid]
        proc = reader = stopper = launch = waiter = owned = None
        try:
            if stop.is_set():
                row["state"] = "cancelled"
                return
            session = self.hub.get(row['session'])
            if session is None or session.workspace != row['workspace']:
                raise ValueError('会话或工作区已变化，请重新运行')
            self._authorize(session, row['command'])
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command", tools.PS_PRELUDE + row["command"],
                cwd=row["workspace"], env=tools._clean_env(), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            proc = await asyncio.shield(launch)
            owned = _CommandJob(proc.pid)
            def append(text):
                row['output'] += text
                if len(row['output']) > MAX_OUTPUT:
                    row['output'] = row['output'][-MAX_OUTPUT:]
                    row['truncated'] = True
            async def consume():
                import codecs
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        append(decoder.decode(b"", final=True))
                        break
                    append(decoder.decode(chunk))
                await proc.wait()
            reader = asyncio.create_task(consume())
            stopper = asyncio.create_task(stop.wait())
            async def exited():
                # Process.wait() can wait for inherited stdout held by a detached
                # descendant. returncode is set as soon as the owned shell exits.
                while proc.returncode is None:
                    await asyncio.sleep(.05)
            waiter = asyncio.create_task(exited())
            done, _ = await asyncio.wait([reader, stopper, waiter], return_when=asyncio.FIRST_COMPLETED)
            if stopper in done:
                owned.close()
                await tools._terminate_command_tree(proc)
                row["state"] = "cancelled"
            else:
                owned.close()
            await asyncio.wait_for(asyncio.shield(reader), 10)
            row["exit_code"] = proc.returncode
            if row["state"] == "running":
                row["state"] = "completed" if proc.returncode == 0 else "failed"
        except asyncio.CancelledError:
            row["state"] = "cancelled"
            if proc is None and launch is not None:
                with contextlib.suppress(Exception):
                    proc = await asyncio.shield(launch)
            raise
        except Exception as exc:
            row["state"] = "cancelled" if stop.is_set() else "failed"
            row["error"] = str(exc)[:300]
        finally:
            if owned is not None:
                owned.close()
            if proc is not None and proc.returncode is None:
                await asyncio.shield(tools._terminate_command_tree(proc))
            for pending in (reader, stopper, waiter):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pending
            row["ended"] = time.time()

    def stop(self, sid, jid):
        if jid not in self.records or self.records[jid]["session"] != sid:
            raise ValueError("没有这个会话的命令")
        self.stops[jid].set()

    async def close(self):
        self.closed = True
        for stop in self.stops.values():
            stop.set()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


# 面板里能直接看 / 听的原文件: PDF (第 7 块) 和图片、视频、音频 (2026-11-05 用户: "为什么工作台右侧功能栏的文件列表
# 点击后看不了图片、视频、文档呢" —— 以前只给 PDF, 别的都报"这是二进制文件"). 只认这些后缀、按后缀定类型 (不按文件头猜);
# HTML、SVG、脚本这类同源打开能跑代码的一律不给. 视频音频靠 FileResponse 的 Range 拖进度
RAW_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".bmp": "image/bmp", ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg",
             ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".flac": "audio/flac"}


def raw_file(session, value, *, blacklist=None):
    """工作区里的 PDF / 图片 / 视频 / 音频原文件 (见 RAW_TYPES), 开在面板的"原文"标签或预览里. 别的不给."""
    path = file_path(session, value)
    root = Path(session.workspace).resolve()
    kind = RAW_TYPES.get(path.suffix.lower())
    if kind is None or not path.is_file():
        raise ValueError("这里只直接打开工作区里的 PDF、图片、视频和音频")
    if blacklist is not None and (denied := blacklist.check('read_file', {'path': str(path)}, root)):
        raise PermissionError(denied.why)
    return web.FileResponse(path, headers={"Content-Type": kind, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                                           "Content-Security-Policy": "sandbox"})


raw_pdf = raw_file  # 旧名字 (第 7 块的导出 PDF)


async def workspace(request, hub):
    try:
        body = await request.json() if request.method == "POST" else dict(request.query)
        if not isinstance(body, dict):
            raise ValueError("需要 JSON 对象")
        session = hub.get(str(body.get("id") or ""))
        if session is None:
            raise ValueError("请先选择会话")
        if not hasattr(hub, "workspace_jobs"):
            hub.workspace_jobs = WorkspaceJobs(hub)
        action = body.get("action", "files")
        if request.method == "GET" and action == "files":
            result = await asyncio.to_thread(inspect_files, session, body.get("path", ""), offset=body.get("offset", 0), blacklist=hub.blacklist)
        elif request.method == "GET" and action == "find":
            result = await asyncio.to_thread(find_files, session, body.get("q", ""), blacklist=hub.blacklist)
        elif request.method == "POST" and action == "revert":
            if getattr(session, "status", "idle") != "idle":
                raise ValueError("她还在做这一轮，等做完再撤销")
            result = await asyncio.to_thread(revert_changes, session, body.get("calls"), blacklist=hub.blacklist)
            if result["ok"] and hasattr(hub, "emit"):  # 对话里记一笔, 她下一轮也知道这些文件回到了改之前
                names = [row["path"] for row in result["reverted"]] + [row["path"] for row in result["archived"]]
                await hub.emit(session, {"type": "note", "text": f"你撤销了 {len(names)} 个文件的改动：{'、'.join(names[:6])}"
                                         + (" 等" if len(names) > 6 else "")
                                         + (f"；新建的 {len(result['archived'])} 个挪进了 .archive" if result["archived"] else ""),
                                         "reverted": [cid for row in result["reverted"] + result["archived"] for cid in row.get("calls", [])]})
                if hasattr(hub, "save"):
                    hub.save(session)
        elif request.method == "GET" and action == "docx_preview":  # 第 7 块: Word 在面板里预览 (mammoth), 每段标上和 docx_read 一样的段号
            result = await asyncio.to_thread(office.preview, session, body.get("path", ""), blacklist=hub.blacklist)
        elif request.method == "POST" and action == "office_export":  # 你在面板上点"导出 PDF": 本机 Word / PowerPoint 另存
            result = await office.export_pdf(session, body.get("path", ""), blacklist=hub.blacklist)
        elif request.method == "GET" and action == "raw":
            return raw_file(session, body.get("path", ""), blacklist=hub.blacklist)
        elif request.method == "GET" and action == "jobs":
            result = {"jobs": hub.workspace_jobs.list(session.id)}
        elif request.method == "POST" and action == "command":
            result = hub.workspace_jobs.start(session, body.get("command"))
        elif request.method == "POST" and action == "stop":
            hub.workspace_jobs.stop(session.id, body.get("job_id"))
            result = {"ok": True}
        else:
            raise ValueError("不支持的工作区操作")
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except (ValueError, TypeError, OSError, PermissionError, UnicodeError) as exc:
        return web.json_response({"error": str(exc)[:300]}, status=400)
