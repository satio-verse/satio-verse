"""她自己写、自己装的 MCP: 规范检查和登记. 装好的放在 ~/.worldmodel/self-mcp/<id>/, 名单在 registry.json,
桌宠宿主刷新工具时一起连上 (只交给云端模型; 只读的工具自动调, 别的每次问你; QQ 那边没有工具).

用户定的分档 (2026-10-25): 给自己写 MCP 来认识世界, 符合规范、花得少 (或者很急需) 的, 她自己定, 不用你点头;
花得多又不急、或者可能有风险的, 先问你; 改桌宠本身的, 审了再合 (见 wm/workbench.py). "符合规范"由这里的程序查, 不靠她自觉:

CONVENTION (写进 dsh 的任务书):
- 一个 MCP 一个文件夹: manifest.json + server.py (+ test_server.py, 不装) ; 骨架 selfmcp_kit.py 装的时候换成项目里那份;
- manifest.json: id (小写字母数字和 -, 最多 24 个字)、name (最多 12 个字)、purpose (一句话)、version、
  network (要访问的域名, 最多 5 个, 只能是公网域名)、tools ([{name, description}]); 要 key 或登录的写 needs_key: true;
- server.py 只用标准库里的一小撮 (ALLOWED_IMPORTS) 和骨架: 联网只能 http_get / http_get_json (只 GET、只 https、只 network 里的域名),
  存东西只能 save_json / load_json; 不许 open、eval、exec、getattr 这些, 不许碰下划线开头的属性, 不许出现 cookie、token、
  key、记忆库、本机端口这类字眼; 工具都是只读的;
- 真起一次服务器 (环境变量里不带 key), 工具清单要和 manifest 对得上.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home() / ".worldmodel" / "self-mcp"
KIT = Path(__file__).resolve().parent / "selfmcp_kit.py"
KIT_API = {"Server", "http_get", "http_get_json", "save_json", "load_json"}
ALLOWED_IMPORTS = {"json", "re", "math", "datetime", "time", "html", "html.parser", "xml.etree.ElementTree", "collections",
                   "itertools", "functools", "string", "textwrap", "statistics", "random", "typing", "dataclasses", "enum",
                   "unicodedata", "zoneinfo", "calendar", "base64", "hashlib", "urllib.parse", "difflib", "heapq", "bisect",
                   "decimal", "fractions", "copy", "__future__", "selfmcp_kit"}
BAD_NAMES = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "getattr", "setattr", "delattr", "vars",
             "globals", "locals", "dir", "memoryview", "help", "exit", "quit"}
BAD_ATTRS = {"sys", "os", "io", "modules", "system", "popen", "subprocess", "builtins", "spawn", "remove", "unlink", "rmdir",
             "rmtree", "importlib", "ctypes", "socket", "load_module", "import_module", "exec_module", "write_text",
             "write_bytes", "mkdir", "rename", "touch", "chmod", "open", "urlopen", "request", "build_opener", "environ",
             "getenv", "putenv", "kill", "attrgetter", "methodcaller"}
SENSITIVE = re.compile(r"(?i)cookie|sessdata|bili_jct|token|passw|secret|api[_-]?key|apikey|appdata|ayuki_remember|\.ssh|"
                       r"pet-settings|pet-api|deepseek_api|127\.0\.0\.1|localhost|:3001|:8790|:8793")
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
URL = re.compile(r"https?://([^/\s\"'?#:]+)")
MAX_BYTES = 200 * 1024


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_manifest(m) -> list[str]:
    bad = []
    if not isinstance(m, dict):
        return ["manifest.json 不是对象"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", str(m.get("id") or "")):
        bad.append("id 只能是小写字母、数字和 -, 最多 24 个字")
    if not str(m.get("name") or "").strip() or len(str(m["name"])) > 12:
        bad.append("name 要有, 最多 12 个字")
    if not str(m.get("purpose") or "").strip() or len(str(m["purpose"])) > 80:
        bad.append("purpose 要有, 一句话 (最多 80 个字)")
    net = m.get("network") or []
    if not isinstance(net, list) or len(net) > 5 or not all(isinstance(h, str) and DOMAIN.match(h.lower()) for h in net):
        bad.append("network 只能是最多 5 个公网域名")
    tools = m.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(t, dict) and t.get("name") for t in tools):
        bad.append("tools 要列出工具 ([{name, description}])")
    return bad


def check_code(src: str, network: list[str]) -> tuple[list[str], list[str]]:
    """server.py -> (硬问题: 违反规范的写法, 敏感: 碰到 key、本机端口这类的). 都没有才能自己装."""
    hard, soft = [], []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"语法错误: {exc}"], []
    hosts = {h.lower() for h in network}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    hard.append(f"import {a.name} (不在允许的标准库里)")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or mod not in ALLOWED_IMPORTS:
                hard.append(f"from {mod} import … (不在允许的标准库里)")
            elif mod == "selfmcp_kit":
                hard += [f"from selfmcp_kit import {a.name} (骨架只给 {', '.join(sorted(KIT_API))})"
                         for a in node.names if a.name not in KIT_API]
        elif isinstance(node, ast.Name) and node.id in BAD_NAMES:
            hard.append(f"用了 {node.id}")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in BAD_ATTRS):
            hard.append(f"用了 .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if SENSITIVE.search(node.value):
                soft.append(f"出现了敏感字眼: {node.value[:40]!r}")
            for host in URL.findall(node.value):
                h = host.lower()
                if not any(h == x or h.endswith("." + x) for x in hosts):
                    hard.append(f"访问了 manifest 里没写的域名: {h}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            hard.append("用了 open")
    return sorted(set(hard)), sorted(set(soft))


def probe(folder: Path, timeout: float = 15.0) -> tuple[list[dict], str]:
    """真起一次 (环境变量里不带 key、token 这类), 走一遍握手和 tools/list. 返回 (工具清单, 出错原因)."""
    env = {k: v for k, v in os.environ.items() if not re.search(r"(?i)key|token|secret|cookie|passw", k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selfmcp-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    data = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs).encode("utf-8")
    try:
        # -E -s: 不读 PYTHON* 环境变量和用户目录的包 (-I 还会去掉脚本所在的目录, 骨架就找不到了)
        p = subprocess.run([sys.executable, "-E", "-s", "server.py"], input=data, cwd=str(folder), env=env,
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"起不来: {type(exc).__name__}"
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("id") == 2:
            return list((r.get("result") or {}).get("tools") or []), ""
    return [], f"没回工具清单 (退出码 {p.returncode}): {p.stderr.decode('utf-8', 'replace')[-200:]}"


def check(folder: Path) -> dict:
    """一个 MCP 文件夹 -> {ok, tier: auto / ask, problems, sensitive, manifest, tools}.
    auto: 全部符合规范, 她可以自己装; ask: 有问题或者碰到敏感的, 要问你 (problems 里写原因)."""
    problems, sensitive, tools = [], [], []
    try:
        m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        m, problems = {}, ["没有 manifest.json, 或者不是 JSON"]
    problems += check_manifest(m) if m else []
    server = folder / "server.py"
    if not server.is_file():
        problems.append("没有 server.py")
    elif server.stat().st_size > MAX_BYTES:
        problems.append("server.py 太大 (超过 200 KB)")
    else:
        hard, sensitive = check_code(server.read_text(encoding="utf-8", errors="replace"), m.get("network") or [])
        problems += hard
    if m.get("needs_key"):
        sensitive.append("要 key 或登录 (needs_key)")
    if not problems:  # 静态检查都过了才真起一次 (用项目里的骨架)
        shutil.copy2(KIT, folder / "selfmcp_kit.py")
        tools, why = probe(folder)
        if why:
            problems.append(why)
        names = {t.get("name") for t in tools}
        want = {t.get("name") for t in m.get("tools") or []}
        if tools and names != want:
            problems.append(f"工具清单和 manifest 对不上: 起来的是 {sorted(names)}, manifest 写的是 {sorted(want)}")
        if any(not (t.get("annotations") or {}).get("readOnlyHint") for t in tools):
            problems.append("有工具不是只读的")
    return {"ok": not problems, "tier": "auto" if not problems and not sensitive else "ask",
            "problems": problems, "sensitive": sensitive, "manifest": m, "tools": [t.get("name") for t in tools]}


# -- 登记 ---------------------------------------------------------------------
def registry_path(home: Path = HOME) -> Path:
    return home / "registry.json"


def load(home: Path = HOME) -> dict:
    try:
        reg = json.loads(registry_path(home).read_text(encoding="utf-8"))
        return reg if isinstance(reg, dict) and isinstance(reg.get("servers"), list) else {"version": 1, "servers": []}
    except (OSError, ValueError):
        return {"version": 1, "servers": []}


def _save(reg: dict, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    tmp = registry_path(home).with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, registry_path(home))


def install(folder: Path, by: str, home: Path = HOME) -> dict:
    """检查过的 MCP 装上: 拷 manifest.json、server.py 和项目里的骨架到 ~/.worldmodel/self-mcp/<id>/ (data/ 留着),
    名单里记文件的 SHA-256 (宿主起之前核对, 被改过就不起). by: auto (她自己定的) / owner (你点头的). 返回名单里那一条."""
    m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    dst = home / m["id"]
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "server.py"):
        shutil.copy2(folder / name, dst / name)
    shutil.copy2(KIT, dst / "selfmcp_kit.py")
    if (folder / "README.md").is_file():
        shutil.copy2(folder / "README.md", dst / "README.md")
    entry = {"id": "self-" + m["id"], "name": m["name"], "purpose": m["purpose"], "version": str(m.get("version") or ""),
             "dir": str(dst), "entry": "server.py", "python": sys.executable, "network": m.get("network") or [],
             "tools": [t["name"] for t in m["tools"]], "by": by, "enabled": True,
             "installed": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "sha256": {n: _sha(dst / n) for n in ("manifest.json", "server.py", "selfmcp_kit.py")}}
    reg = load(home)
    reg["servers"] = [s for s in reg["servers"] if s.get("id") != entry["id"]] + [entry]
    _save(reg, home)
    return entry


def set_enabled(sid: str, enabled: bool, home: Path = HOME) -> bool:
    reg = load(home)
    hit = False
    for s in reg["servers"]:
        if s.get("id") == sid:
            s["enabled"], hit = enabled, True
    if hit:
        _save(reg, home)
    return hit


def _main(argv: list[str]) -> int:
    if argv[:1] == ["list"]:
        for s in load()["servers"]:
            print(f"{s['id']}  {s['name']}  {'开' if s.get('enabled') else '关'}  {s['by']}  {s['installed']}  {s['purpose']}")
        return 0
    if len(argv) == 2 and argv[0] in ("disable", "enable"):
        return 0 if set_enabled(argv[1], argv[0] == "enable") else 1
    if len(argv) == 2 and argv[0] == "check":
        print(json.dumps(check(Path(argv[1])), ensure_ascii=False, indent=1))
        return 0
    print("python -m wm.selfmcp list | check <文件夹> | disable <id> | enable <id>")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
