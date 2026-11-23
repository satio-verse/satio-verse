"""Workbench extensions: bound desktop skills/MCP plus versioned Creator packages.

Creating a revision validates and stores text; it never executes package code. Enabling
or rolling back needs the caller's explicit authorization. MCP discovery and execution
must additionally pass the caller's session policy. This module is not an OS sandbox.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from ..config import ROOT
from . import install_gate
from .mcp_runtime import MCPClient, MCPError, SECRET_ENV

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MAX_BYTES = 1024 * 1024
DEFAULT_DESKTOP_DISCOVERY = True
POOL_IDLE_S = 120  # MCP 长连接空闲多久关掉 (秒)
POOL_MAX = 8       # 最多留几条 MCP 长连接
PREVIEW_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"


def _spec(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}}


TOOL_SPECS = [
    _spec("extensions_list", "查看已接入的技能、MCP 和 Creator 扩展；不启动扩展进程。"),
    _spec("skill_read", "读取一个已启用技能的 SKILL.md。先从 extensions_list 查看名称，按需要读取全文。", {"id": {"type": "string"}}, ["id"]),
    _spec("mcp_tools", "连接一个已配置且启用的 MCP 并查看工具参数。连接可能启动程序，必须遵守当前任务权限。", {"server_id": {"type": "string"}}, ["server_id"]),
    _spec("mcp_call", "调用已启用 MCP 的一个工具。先查看 mcp_tools 的真实参数，不要猜工具名。", {"server_id": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}}, ["server_id", "tool", "arguments"]),
    _spec("extension_create", "创建或更新 Creator 扩展草稿：可包含 SKILL.md、MCP server.py 和独立 HTML 界面。只保存和静态校验，不执行、不自动启用。已有版本不覆盖。",
        {"spec": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
            "files": {"type": "object", "description": "相对文件路径到 UTF-8 文本的映射"},
            "mcp": {"type": "object", "description": "可选 stdio MCP：command、args；在版本目录运行，不支持在包内存放凭据"},
            "ui": {"type": "string", "description": "可选独立 HTML 文件路径，使用内联 CSS/JS；隔离预览没有主应用权限"}},
            "required": ["id", "name", "files"]}}, ["spec"]),
    _spec("extension_enable", "启用或停用指定 Creator 扩展版本。启用会允许后续调用其 MCP；是否直接执行由用户选择的授权模式决定。", {
        "id": {"type": "string"}, "enabled": {"type": "boolean"}, "revision": {"type": "string", "description": "启用哪个已存在版本；省略使用最新草稿"}}, ["id", "enabled"]),
    _spec("extension_rollback", "将 Creator 扩展切回一个已存在且校验完整的旧版本。不会删除新版文件。", {
        "id": {"type": "string"}, "revision": {"type": "string"}}, ["id", "revision"]),
]


def _read_json(path: Path, default=None):
    if not path.is_file():
        return default
    if path.stat().st_size > MAX_BYTES * 2:
        raise ValueError("扩展配置文件过大")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def _relative(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 180 or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("扩展文件必须使用简短的相对路径和 / 分隔符")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {".", "..", ""} for p in name.split("/")):
        raise ValueError("扩展文件路径不能越出版本目录")
    for part in path.parts:
        if part.startswith(".") or part.endswith((".", " ")) or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part, re.I):
            raise ValueError("扩展文件路径包含保留名称")
    return path.as_posix()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _skill_head(text: str, fallback: str):
    values = {}
    if text.startswith("---"):
        head = text[3:].split("\n---", 1)[0]
        current = None
        for line in head.splitlines():
            if line.startswith((" ", "\t")) and current:
                values[current] += " " + line.strip()
            else:
                match = re.match(r"^(name|description):\s*(.*)$", line)
                current = match[1] if match else None
                if match:
                    value = match[2].strip().strip("\"'")
                    values[current] = "" if value in {">", "|", ">-", "|-"} else value
    return values.get("name", "").strip() or fallback, values.get("description", "").strip()[:700]


def _decrypt_secret(encrypted: str) -> dict:
    """Same Windows DPAPI envelope as desktop/host/src/settings.cpp; never returned to UI."""
    if not encrypted:
        return {}
    if os.name != "nt":
        raise ValueError("此 MCP 的凭据由 Windows 加密，需要在原用户的 Windows 会话运行")
    class Blob(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    try:
        raw = base64.b64decode(encrypted, validate=True)
        incoming = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        entropy_bytes = b"WorldModel-pet-settings\x00"
        entropy = (ctypes.c_ubyte * len(entropy_bytes)).from_buffer_copy(entropy_bytes)
        src, salt, out = Blob(len(raw), incoming), Blob(len(entropy_bytes), entropy), Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
        crypt.CryptUnprotectData.restype = ctypes.c_int
        if not crypt.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(salt), None, None, 1, ctypes.byref(out)):
            raise ValueError("此 MCP 的加密凭据无法解锁")
        try:
            decoded = json.loads(ctypes.string_at(out.data, out.length).decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {}
        finally:
            ctypes.memset(out.data, 0, out.length)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(out.data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("此 MCP 的加密凭据无法解锁") from exc


class ExtensionRegistry:
    def __init__(self, home: Path, settings_path=None, skill_roots=None, servers=None, self_mcp_root=None):
        self.home = Path(home)
        self.root = self.home / "extensions"
        appdata = Path(os.environ.get("APPDATA", str(Path.home()))) / "WorldModel"
        if not DEFAULT_DESKTOP_DISCOVERY and settings_path is None:
            settings_path = False
            if skill_roots is None:
                skill_roots = []
        # False is an explicit isolated/non-desktop mode (used by tests and hosted agents).
        self.settings_path = None if settings_path is False else Path(settings_path or appdata / "pet-settings.json")
        self.skill_roots = [Path(p) for p in skill_roots] if skill_roots is not None else [appdata / "skills", ROOT / "desktop" / "skills"]
        self.explicit_servers = list(servers or [])
        self.self_mcp_root = None if self_mcp_root is False or (self_mcp_root is None and settings_path is False) else Path(self_mcp_root or Path.home() / ".worldmodel" / "self-mcp")
        self.lock = threading.RLock()

    def _state(self):
        state = _read_json(self.root / "registry.json", {"version": 1, "packages": {}})
        if not isinstance(state, dict) or not isinstance(state.get("packages"), dict):
            raise ValueError("Creator 登记表无效，未覆盖原文件")
        return state

    def _settings(self):
        if self.settings_path is None:
            return {}
        try:
            data = _read_json(self.settings_path, {})
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _profile(self, settings):
        return next((p for p in settings.get("personas", []) if isinstance(p, dict) and p.get("id") == settings.get("active")), {})

    def _revision(self, package_id, revision=None):
        if not isinstance(package_id, str) or not ID.fullmatch(package_id):
            raise ValueError("扩展编号无效")
        entry = self._state()["packages"].get(package_id)
        if not entry:
            raise ValueError("没有这个扩展")
        revision = revision or entry.get("active_revision") or entry.get("latest_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{24}", revision) or revision not in entry.get("revisions", []):
            raise ValueError("没有这个扩展版本")
        folder = self.root / package_id / "revisions" / revision
        if not _within(folder, self.root):
            raise ValueError("扩展目录超出登记范围")
        manifest = _read_json(folder / "manifest.json")
        if (not isinstance(manifest, dict) or not all(k in manifest for k in ("id", "name", "description", "mcp", "ui", "sha256", "revision"))
                or not isinstance(manifest["sha256"], dict) or not manifest["sha256"] or len(manifest["sha256"]) > 64):
            raise ValueError("扩展清单缺失")
        # Verify all installed code every time before execution or preview.
        for name, digest in manifest.get("sha256", {}).items():
            path = folder / _relative(name)
            if not _within(path, folder) or not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ValueError("扩展文件缺失或超出范围")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("扩展文件与版本记录不符，请创建新版本")
        canonical = {k: manifest[k] for k in ("id", "name", "description", "mcp", "ui", "sha256")}
        wanted = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        if wanted != revision or manifest["revision"] != revision or manifest["id"] != package_id:
            raise ValueError("扩展清单与版本编号不符")
        return entry, manifest, folder

    def create(self, spec: dict, by="assistant"):
        if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not ID.fullmatch(spec["id"]):
            raise ValueError("id 只能是小写字母、数字和 -，最多 48 个字符")
        name = str(spec.get("name") or "").strip()
        if not name or len(name) > 80:
            raise ValueError("扩展名称需要 1～80 个字符")
        # 装之前先过「世界接口契约」：老扩展也要一份报告，世界插件必须真合约（不合约就抛 ContractError）。
        kind = install_gate.classify(spec)
        wanted = spec.get("files") if isinstance(spec.get("files"), dict) else None
        contract = install_gate.gate(spec, files=wanted)
        files = spec.get("files")
        if not isinstance(files, dict) or not files or len(files) > 64:
            raise ValueError("files 需要 1～64 个文本文件")
        clean, total, seen = {}, 0, set()
        for raw, content in files.items():
            path = _relative(raw)
            if path.lower() == "manifest.json" or path.casefold() in seen:
                raise ValueError("扩展不能覆盖 manifest.json 或包含重名文件")
            if not isinstance(content, str) or "\x00" in content:
                raise ValueError("扩展文件必须是 UTF-8 文本")
            raw_bytes = content.encode("utf-8")
            total += len(raw_bytes)
            if len(raw_bytes) > 256 * 1024 or total > MAX_BYTES:
                raise ValueError("扩展文本过大：单文件最多 256 KB，合计最多 1 MB")
            if path.endswith(".py"):
                try:
                    ast.parse(content, filename=path)
                except SyntaxError as exc:
                    raise ValueError(f"{path} 第 {exc.lineno} 行 Python 语法错误") from exc
            clean[path] = content
            seen.add(path.casefold())
        if kind == "plugin":
            # 世界插件的清单随包一起存（插件宿主装的时候要的就是这份 wmworld.json）。
            clean[install_gate.MANIFEST_NAME] = json.dumps(spec["plugin"], ensure_ascii=False, indent=2)
        mcp = spec.get("mcp") or None
        if mcp:
            if not isinstance(mcp, dict) or not str(mcp.get("command") or "").strip():
                raise ValueError("mcp 需要 command 和 args")
            if any(k in mcp for k in ("env", "headers", "secret", "url")):
                raise ValueError("Creator 包不保存凭据或远程连接，请在 MCP 设置中配置")
            args = mcp.get("args", [])
            if not isinstance(args, list) or not all(isinstance(x, str) and len(x) <= 500 for x in args) or len(args) > 32:
                raise ValueError("MCP args 必须是最多 32 项的字符串列表")
            mcp = {"command": str(mcp["command"]).strip(), "args": args, "transport": "stdio"}
        ui = _relative(spec["ui"]) if spec.get("ui") else None
        if not ui and kind == "plugin":
            manifest_block = spec["plugin"]
            panel = manifest_block.get("panel")
            if not isinstance(panel, dict):
                inner = manifest_block.get("plugin")
                panel = inner.get("panel") if isinstance(inner, dict) else None
            entry_path = panel.get("entry") if isinstance(panel, dict) else None
            if isinstance(entry_path, str) and entry_path in clean and entry_path.lower().endswith((".html", ".htm")):
                ui = _relative(entry_path)   # 世界面板就是它的界面，预览直接能开
        if ui and (ui not in clean or not ui.lower().endswith((".html", ".htm"))):
            raise ValueError("ui 必须指向扩展中的 HTML 文件")
        if kind != "plugin" and "SKILL.md" not in clean and not mcp and not ui:
            raise ValueError("扩展至少包含 SKILL.md、MCP 或 HTML 界面之一；世界插件要有 panel 或动作")
        manifest = {"id": spec["id"], "name": name, "description": str(spec.get("description") or "")[:1000],
            "mcp": mcp, "ui": ui, "sha256": {p: hashlib.sha256(t.encode()).hexdigest() for p, t in clean.items()}}
        revision = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        manifest.update({"revision": revision, "created": time.time(), "by": str(by)[:80],
            "validation": {"static": "passed", "execution": "not_run", "kind": kind,
                           "contract": {"name": contract["contract"]["name"], "schema": contract["contract"]["schema"]},
                           "checks": ["relative_paths", "size_limits", "python_syntax", "content_hashes", "world_contract"]}})
        with self.lock:
            state = self._state()
            folder = self.root / spec["id"] / "revisions" / revision
            if not _within(folder, self.root):
                raise ValueError("扩展目录超出登记范围")
            folder.parent.mkdir(parents=True, exist_ok=True)
            if not folder.exists():
                temp = folder.parent / ("stage-" + uuid.uuid4().hex)
                temp.mkdir()
                try:
                    for relative, content in clean.items():
                        path = temp / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8", newline="")
                    _atomic(temp / "manifest.json", manifest)
                    temp.rename(folder)
                finally:
                    if temp.exists() and _within(temp, folder.parent):
                        shutil.rmtree(temp)
            entry = state["packages"].setdefault(spec["id"], {"id": spec["id"], "enabled": False, "active_revision": None, "revisions": []})
            if revision not in entry["revisions"]:
                entry["revisions"].append(revision)
            entry.update({"name": name, "description": manifest["description"], "latest_revision": revision,
                          "updated": time.time(), "kind": kind})
            _atomic(self.root / "registry.json", state)
            self._audit("create", spec["id"], revision, by)
            return {"id": spec["id"], "revision": revision, "enabled": entry["enabled"], "active_revision": entry["active_revision"],
                "validation": manifest["validation"], "files": list(clean), "has_ui": bool(ui),
                "kind": kind, "contract": {"ok": contract["ok"], "world": contract["summary"].get("world", ""),
                                           "actions": contract["summary"].get("actions", []),
                                           "events": contract["summary"].get("events", [])}}

    def _audit(self, action, package_id, revision, by):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "audit.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"at": time.time(), "action": action, "id": package_id, "revision": revision, "by": str(by)[:80]}, ensure_ascii=False) + "\n")

    def set_enabled(self, package_id, enabled, authorized=False, revision=None, by="user"):
        if authorized is not True:
            raise PermissionError("启用或停用扩展需要当前任务的明确授权")
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        with self.lock:
            state = self._state()
            entry = state["packages"].get(package_id)
            if not entry:
                raise ValueError("没有这个扩展")
            if enabled:
                revision = revision or entry["latest_revision"]
                self._revision(package_id, revision)
                entry["active_revision"] = revision
            entry["enabled"] = enabled
            _atomic(self.root / "registry.json", state)
            self._audit("enable" if enabled else "disable", package_id, entry["active_revision"], by)
            return dict(entry)

    def rollback(self, package_id, revision, authorized=False, by="user"):
        if authorized is not True:
            raise PermissionError("回退扩展需要当前任务的明确授权")
        with self.lock:
            self._revision(package_id, revision)
            state = self._state()
            entry = state["packages"][package_id]
            entry["active_revision"] = revision
            _atomic(self.root / "registry.json", state)
            self._audit("rollback", package_id, revision, by)
            return dict(entry)

    def preview(self, package_id, revision=None):
        _, manifest, folder = self._revision(package_id, revision)
        if not manifest.get("ui"):
            raise ValueError("这个扩展没有界面")
        html = (folder / manifest["ui"]).read_text(encoding="utf-8")
        # srcdoc's first policy applies even when generated HTML contains another head.
        safe = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + PREVIEW_CSP.replace('"', '&quot;') + '">' + html
        return {"id": package_id, "revision": manifest["revision"], "html": safe, "sandbox": "allow-scripts", "csp": PREVIEW_CSP}

    def _skills(self):
        settings = self._settings()
        profile = self._profile(settings)
        wanted = set(profile.get("skills") or [])
        if any(s.get("kind") == "windows-mcp" and s.get("enabled", True) and s.get("id") in profile.get("mcp", []) for s in settings.get("mcp", []) if isinstance(s, dict)):
            wanted.add("computer-use")
        out = {}
        for root in self.skill_roots:
            if not root.is_dir():
                continue
            for folder in sorted(root.iterdir()):
                skill = folder / "SKILL.md"
                if folder.name.startswith(".") or not skill.is_file() or not _within(skill, root) or skill.stat().st_size > 256 * 1024:
                    continue
                try:
                    body = skill.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError):
                    continue
                name, description = _skill_head(body, folder.name)
                if (self.settings_path is not None and name not in wanted) or name in out:
                    continue
                out[name] = {"id": name, "name": name, "description": description, "source": "desktop", "path": str(skill), "body": body}
        for package_id, entry in self._state()["packages"].items():
            if not entry.get("enabled"):
                continue
            try:
                _, manifest, folder = self._revision(package_id, entry["active_revision"])
                if "SKILL.md" not in manifest["sha256"]:
                    continue
                body = (folder / "SKILL.md").read_text(encoding="utf-8")
                name, description = _skill_head(body, entry["name"])
                key = "creator:" + package_id
                out[key] = {"id": key, "name": name, "description": description, "source": "creator", "path": str(folder / "SKILL.md"), "body": body}
            except (OSError, ValueError):
                continue
        return out

    def skill_index(self):
        usage = self.skill_usage()
        return [{**{k: v for k, v in skill.items() if k not in {"body", "path"}}, **usage.get(skill["id"], {})}
                for skill in self._skills().values()]

    # 技能用了几次 (2026-11-07 底层对标 P1-7; Hermes 记技能的使用, 久不用的提醒清理): skill_read 一次算用一次
    def skill_usage(self):
        try:
            data = _read_json(self.root / "skill-usage.json", {})
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def note_skill_use(self, skill_id):
        if not isinstance(skill_id, str) or not skill_id or len(skill_id) > 200:
            return
        with self.lock:
            usage = self.skill_usage()
            row = usage.setdefault(skill_id, {"used": 0})
            row["used"] = int(row.get("used") or 0) + 1
            row["last_used"] = round(time.time(), 3)
            with contextlib.suppress(OSError):
                _atomic(self.root / "skill-usage.json", dict(list(usage.items())[-500:]))

    def read_skill(self, skill_id):
        skill = self._skills().get(skill_id)
        if not skill:
            raise ValueError("技能未启用、未绑定当前人设或文件已改变")
        return skill

    def _servers(self):
        settings = self._settings()
        bound = set(self._profile(settings).get("mcp") or [])
        out = {s["id"]: dict(s, source="desktop") for s in settings.get("mcp", [])
            if isinstance(s, dict) and s.get("id") in bound and s.get("enabled", True)}
        for server in self.explicit_servers:
            if isinstance(server, dict) and server.get("id") and server.get("enabled", True):
                out.setdefault(server["id"], dict(server, source="configured"))
        if self.self_mcp_root is not None and (settings.get("self_mcp") or {}).get("enabled", True):
            try:
                installed = _read_json(self.self_mcp_root / "registry.json", {}).get("servers", [])
                for entry in installed:
                    if not isinstance(entry, dict) or not entry.get("enabled") or not str(entry.get("id", "")).startswith("self-"):
                        continue
                    folder = Path(entry.get("dir") or "")
                    if not _within(folder, self.self_mcp_root) or folder.resolve() == self.self_mcp_root.resolve():
                        continue
                    hashes = entry.get("sha256") or {}
                    valid = all((folder / p).is_file() and _within(folder / p, folder)
                        and (folder / p).stat().st_size <= MAX_BYTES
                        and hashlib.sha256((folder / p).read_bytes()).hexdigest() == hashes.get(p)
                        for p in ("manifest.json", "server.py", "selfmcp_kit.py"))
                    python = str(entry.get("python") or "")
                    if valid and python and Path(python).name.lower().startswith("python"):
                        out.setdefault(entry["id"], {"id": entry["id"], "name": entry.get("name", entry["id"]),
                            "command": python, "args": ["-E", "-s", "server.py"], "cwd": str(folder), "transport": "stdio", "source": "self-mcp"})
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        for package_id, entry in self._state()["packages"].items():
            if not entry.get("enabled"):
                continue
            try:
                _, manifest, folder = self._revision(package_id, entry["active_revision"])
                if manifest.get("mcp"):
                    server = dict(manifest["mcp"])
                    if server["command"] in {"python", "python3", "python.exe"}:
                        server["command"] = sys.executable
                    key = "creator:" + package_id
                    out[key] = {**server, "id": key, "name": manifest["name"], "cwd": str(folder), "source": "creator", "revision": manifest["revision"], "enabled": True}
            except (OSError, ValueError):
                continue
        return out

    def servers(self):
        return [{"id": s["id"], "name": s.get("name", s["id"]), "transport": s.get("transport", "stdio"),
            "source": s.get("source", "configured"), "revision": s.get("revision"), "has_credentials": bool(s.get("secret") or s.get("env") or s.get("headers"))}
            for s in self._servers().values()]

    def server(self, server_id, *, credentials=False):
        """Private policy hook. Never serialize this config to model/UI responses."""
        server = self._servers().get(server_id)
        if not server:
            raise ValueError("MCP 未启用、未绑定当前人设，或扩展版本校验失败")
        if credentials:
            secret = _decrypt_secret(server.get("secret", ""))
            server["env"] = {**(server.get("env") or {}), **(secret.get("env") or {})}
            server["headers"] = {**(server.get("headers") or {}), **(secret.get("headers") or {})}
        else:
            server.pop("env", None)
            server.pop("headers", None)
        server.pop("secret", None)
        return server

    def candidate_server(self, package_id, revision):
        """Read-only private hook for authorized regression runs; never changes activation."""
        entry, manifest, folder = self._revision(package_id, revision)
        if not manifest.get("mcp"):
            raise ValueError("这个扩展版本没有可回归测试的 MCP")
        server = dict(manifest["mcp"])
        if server["command"] in {"python", "python3", "python.exe"}:
            server["command"] = sys.executable
        return {**server, "id": "creator:" + package_id, "package_id": package_id,
            "name": manifest["name"], "cwd": str(folder), "source": "creator", "revision": manifest["revision"],
            "files": dict(manifest["sha256"]), "manifest": manifest,
            "enabled": bool(entry.get("enabled")), "active_revision": entry.get("active_revision")}

    def list(self):
        packages = []
        for package_id, entry in self._state()["packages"].items():
            row = dict(entry)
            try:
                _, manifest, _ = self._revision(package_id, entry["latest_revision"])
                row.update({"has_ui": bool(manifest.get("ui")), "has_mcp": bool(manifest.get("mcp")),
                    "has_skill": "SKILL.md" in manifest["sha256"], "validation": manifest.get("validation", {})})
            except (OSError, ValueError):
                row["error"] = "版本文件校验失败"
            packages.append(row)
        return {"extensions": packages, "skills": self.skill_index(), "servers": self.servers()}

    def tool_specs(self):
        return TOOL_SPECS

    def summary_index(self):
        """Metadata only, deliberately bounded for the model's stable tool directory."""
        return {"skills": [{"id": s["id"], "name": s["name"], "description": s["description"][:200]} for s in self.skill_index()[:40]],
            "servers": [{"id": s["id"], "name": s["name"], "source": s["source"]} for s in self.servers()[:40]]}

    @staticmethod
    def _validate_tools(tools, config):
        result, seen = [], set()
        enabled = config.get("tools") or {}
        if not isinstance(enabled, dict):
            raise MCPError("MCP 工具启停配置无效")
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not TOOL_NAME.fullmatch(tool["name"]):
                raise MCPError("MCP 工具名无效")
            if tool["name"] in seen:
                raise MCPError("MCP 返回了重复工具名")
            seen.add(tool["name"])
            if enabled.get(tool["name"]) is False:
                continue
            schema = tool.get("inputSchema")
            if not isinstance(schema, dict):
                raise MCPError("MCP 工具参数说明无效")
            result.append({"name": tool["name"], "description": str(tool.get("description") or "")[:1500],
                "inputSchema": schema, "annotations": tool.get("annotations") or {}})
        return result

    async def _cancellable(self, coroutine, stop=None):
        if stop is None:
            return await coroutine
        if stop.is_set():
            coroutine.close()
            raise asyncio.CancelledError()
        task, stopped = asyncio.create_task(coroutine), asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise asyncio.CancelledError()
            return await task
        finally:
            stopped.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # ---- MCP 长连接 (2026-11-07 底层对标 P1-9; CC / Codex 都是长连接, Codex 0.167 按对话租用共享连接) ----
    # 以前每调一次就新起一个进程、调完就关: Python 写的扩展每次启动 0.5–1 秒 (她的直通工具每次都要等), 有状态的服务 (浏览器这类)
    # 每次都是新的. 现在按 (会话, 服务, 配置) 留着连接: 空闲 POOL_IDLE_S 秒关掉, 最多 POOL_MAX 条 (多了关最久没用的), 进程没了
    # 自动重连; 一次调用出错 / 超时 / 被停掉, 那条连接直接关掉 (状态说不准了), 下次重新起. 每次调用照旧先过守门和审批 (studio).
    # 不给 owner 的 (growth 评测这类) 照旧一次一连.
    def _pool_state(self):
        if not hasattr(self, "_pool"):
            self._pool = {}
        return self._pool

    @staticmethod
    def _alive(client):
        if client.proc is not None:
            return client.proc.returncode is None
        return client.http is not None and not client.http.closed

    async def _drop(self, key):
        entry = self._pool_state().pop(key, None)
        if entry is None:
            return
        if entry["loop"] is not asyncio.get_running_loop():  # 别的事件循环里建的 (测试): 只能直接结束进程
            with contextlib.suppress(Exception):
                if entry["client"].proc is not None:
                    entry["client"].proc.kill()
            return
        with contextlib.suppress(Exception):
            await entry["client"].close()

    async def _reap(self):
        now = time.monotonic()
        for key, entry in list(self._pool_state().items()):
            if (now - entry["used"] > POOL_IDLE_S and not entry["lock"].locked()) or not self._alive(entry["client"]):
                await self._drop(key)

    async def _pooled(self, owner, server_id, config):
        """拿一条 (owner 会话的) 长连接, 没有就新起一个."""
        await self._reap()
        digest = hashlib.sha256(json.dumps({k: config.get(k) for k in ("command", "args", "cwd", "url", "transport", "revision")},
                                           sort_keys=True, default=str).encode()).hexdigest()[:16]
        key = (str(owner), server_id, digest)
        pool = self._pool_state()
        entry = pool.get(key)
        if entry is not None and (entry["loop"] is not asyncio.get_running_loop() or not self._alive(entry["client"])):
            await self._drop(key)
            entry = None
        if entry is None:
            while len(pool) >= POOL_MAX:
                await self._drop(min(pool, key=lambda k: pool[k]["used"]))
            client = MCPClient(config)
            await client.__aenter__()
            entry = pool[key] = {"client": client, "tools": None, "used": time.monotonic(), "lock": asyncio.Lock(),
                                 "loop": asyncio.get_running_loop()}
        return key, entry

    async def _with_client(self, owner, server_id, config, work):
        """在一条连接上做 work(client, entry): 有 owner 用长连接 (一次只做一件事, 出错就关掉那条), 没有就一次一连."""
        if owner is None:
            async with MCPClient(config) as client:
                return await work(client, {"tools": None})
        key, entry = await self._pooled(owner, server_id, config)
        async with entry["lock"]:
            try:
                return await work(entry["client"], entry)
            except BaseException:
                await self._drop(key)
                raise
            finally:
                entry["used"] = time.monotonic()

    async def close(self):
        """服务端关的时候: 留着的 MCP 连接都关掉."""
        for key in list(self._pool_state()):
            await self._drop(key)

    async def list_tools(self, server_id, stop=None, owner=None):
        async def run():
            config = self.server(server_id, credentials=True)

            async def work(client, entry):
                entry["tools"] = self._validate_tools(await client.list_tools(), config)
                return {"server_id": server_id, "tools": entry["tools"]}
            return await self._with_client(owner, server_id, config, work)
        return await self._cancellable(run(), stop)

    async def call(self, server_id, tool, arguments, stop=None, owner=None):
        if not isinstance(tool, str) or not TOOL_NAME.fullmatch(tool) or not isinstance(arguments, dict):
            raise ValueError("MCP 工具名或参数无效")
        async def run():
            config = self.server(server_id, credentials=True)

            async def work(client, entry):
                if entry["tools"] is None or tool not in {t["name"] for t in entry["tools"]}:  # 长连接上的工具清单记着; 没有这个就再问一次
                    entry["tools"] = self._validate_tools(await client.list_tools(), config)
                if tool not in {t["name"] for t in entry["tools"]}:
                    raise ValueError("这个 MCP 工具不存在或已停用")
                return await client.request("tools/call", {"name": tool, "arguments": arguments})
            result = await self._with_client(owner, server_id, config, work)
            if isinstance(result, dict):  # 整理成交给模型的几段 (凭据打码, 图片留给上层; MCPClient.request 只回字典)
                content = []
                secret_values = [str(v) for k, v in (config.get("env") or {}).items() if SECRET_ENV.search(k) and len(str(v)) >= 4]
                for key, value in (config.get("headers") or {}).items():
                    if SECRET_ENV.search(key) and len(str(value)) >= 4:
                        secret_values.append(str(value))
                        if str(value).lower().startswith("bearer "):
                            secret_values.append(str(value)[7:])
                remaining = 24000
                parts = result.get("content", [])
                if not isinstance(parts, list):
                    raise MCPError("MCP 工具内容格式无效")
                if not parts and result.get("structuredContent") is not None:
                    parts = [{"type": "text", "text": json.dumps(result["structuredContent"], ensure_ascii=False)}]
                images = 0
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    kind = part.get("type")
                    if kind == "text":
                        text = str(part.get("text") or "")
                        for value in sorted(set(secret_values), key=len, reverse=True):
                            text = text.replace(value, "[凭据已隐藏]")
                        content.append({"type": "text", "text": text[:remaining]})
                        remaining -= min(len(text), remaining)
                    elif kind == "image":
                        # 图片原样留给上层: core 下次请求时作为多模态块交给模型 (模型不收图会退回纯文字)
                        mime = str(part.get("mimeType") or "image/png")
                        data = str(part.get("data") or "")
                        if images < 3 and mime.startswith("image/") and 0 < len(data) <= 3000000:
                            images += 1
                            content.append({"type": "image_url", "image_url": {
                                "url": "data:%s;base64,%s" % (mime, data), "detail": "low"}})
                        else:
                            content.append({"type": "text", "text": "[MCP 返回了一张图片，太大或格式不认识，没有交给模型]"})
                    else:
                        content.append({"type": "text", "text": "[MCP 返回了 %s 类型的内容，暂不支持，只保留文字]" % kind})
                    if len(content) >= 8 or remaining <= 0:
                        break
                return {"server_id": server_id, "tool": tool, "isError": bool(result.get("isError")), "content": content}
        return await self._cancellable(run(), stop)
