"""操作面板和宿主 MCP 客户端的自检 (paneltest.ps1 调用; 设置放在临时目录, 不碰用户真正的设置).

1. 等面板页面自己的自检 (panel.js 的 selftest) 写出结果;
2. 查设置文件里没有密钥明文; 用 MCP 的 pet_state 查当前人设已经交给桌宠页面;
3. 用 wmpet.exe --mcp-probe 测宿主的 MCP 客户端: 新版 (桌宠自己的 MCP 服务端, stdio 和 HTTP)、
   旧版替身 (mcp_legacy_stub.py, stdio 和 HTTP+SSE)、没带 token、命令不存在;
4. 正常关掉测试桌宠.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from api_test import Mcp, check, close_pet, RESULTS  # noqa: E402  (同一套打印和关窗口)


def wait_result(path: str, seconds: float) -> dict | None:
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                if '"panel":"selftest"' in line:
                    return json.loads(line)
        time.sleep(0.5)
    return None


def probe(exe: str, settings_dir: str, work: str, name: str, cfg: dict, tool: str | None = None, args: dict | None = None) -> dict:
    cfg_path = os.path.join(work, f"probe-{name}.json")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    cmd = [exe, "--settings-dir", settings_dir, "--mcp-probe", cfg_path]
    if tool:
        args_path = os.path.join(work, f"probe-{name}-args.json")
        json.dump(args or {}, open(args_path, "w", encoding="utf-8"), ensure_ascii=False)
        cmd += ["--mcp-call", tool, args_path]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=90).stdout.decode("utf-8", "replace").strip()
        return json.loads(out.splitlines()[-1]) if out else {"ok": False, "error": "没有输出"}
    finally:
        os.remove(cfg_path)  # 里面可能有 token
        if tool:
            os.remove(args_path)


def call_text(r: dict) -> str:
    return " ".join(c.get("text", "") for c in ((r.get("call") or {}).get("content") or []) if isinstance(c, dict))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--settings-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--api-port", type=int, default=8795)
    ap.add_argument("--stub-port", type=int, default=8796)
    a = ap.parse_args()
    work = os.path.dirname(a.out)
    try:
        r = wait_result(a.out, 150)
        if not check("面板页面的自检写出了结果", r is not None):
            return 1
        for c in r.get("checks", []):
            check(f"面板: {c['name']}", c["ok"], **{k: v for k, v in c.items() if k not in ("name", "ok")})
        if r.get("error"):
            check("面板自检出错", False, error=r["error"])

        text = open(os.path.join(a.settings_dir, "pet-settings.json"), encoding="utf-8").read()
        check("设置文件里没有密钥明文 (DPAPI 加密)", "sk-selftest-not-a-real-key" not in text and "selftest-secret-value" not in text
              and '"key":"' in text and '"secret":"' in text)

        token = open(os.path.join(os.environ["APPDATA"], "WorldModel", "pet-api-token.txt"), encoding="utf-8").read().strip()
        base = f"http://127.0.0.1:{a.api_port}"
        _, _, st = Mcp(base, token).tool("pet_state")
        persona = ((st.get("structuredContent") or {}).get("page") or {}).get("persona") or {}
        check("当前人设已经交给桌宠页面 (pet_state 里不带 key)", persona.get("name") == "自检人设" and persona.get("provider") == "deepseek"
              and persona.get("model") == "deepseek-selftest" and "sk-" not in json.dumps(st, ensure_ascii=False), persona=persona)

        # 宿主的 MCP 客户端
        self_stdio = {"transport": "stdio", "command": a.exe, "args": ["--mcp-stdio", "--api-port", str(a.api_port)]}
        p = probe(a.exe, a.settings_dir, work, "modern-stdio", self_stdio, "pet_state", {})
        check("MCP 客户端: 新版 stdio (桌宠自己的服务端) + 调 pet_state", p.get("ok") and p.get("modern") and p.get("version") == "2026-07-28"
              and (p.get("call") or {}).get("structuredContent", {}).get("visible") is True, version=p.get("version"), tools=len(p.get("tools") or []),
              ms=p.get("ms"), error=p.get("error") or p.get("call_error"))
        http_cfg = {"transport": "http", "url": f"{base}/mcp", "headers": {"Authorization": f"Bearer {token}"}}
        p = probe(a.exe, a.settings_dir, work, "modern-http", http_cfg, "pet_act", {"skill": "nod"})
        check("MCP 客户端: 新版 HTTP + 调 pet_act", p.get("ok") and p.get("modern") and not (p.get("call") or {}).get("isError", True),
              version=p.get("version"), reply=call_text(p), error=p.get("error") or p.get("call_error"))
        p = probe(a.exe, a.settings_dir, work, "no-token", {"transport": "http", "url": f"{base}/mcp"})
        check("MCP 客户端: 没带 token → 连不上并说明原因", not p.get("ok") and "401" in (p.get("error") or ""), error=p.get("error"))

        stub = os.path.join(HERE, "mcp_legacy_stub.py")
        legacy_stdio = {"transport": "stdio", "command": sys.executable, "args": ["-I", stub, "stdio"]}
        p = probe(a.exe, a.settings_dir, work, "legacy-stdio", legacy_stdio, "add", {"a": 2, "b": 3})
        check("MCP 客户端: 旧版 stdio (退回 initialize, 分页, 回应服务器的 ping)", p.get("ok") and not p.get("modern")
              and p.get("version") == "2025-06-18" and [t["name"] for t in p.get("tools", [])] == ["echo", "add", "slow"] and call_text(p) == "5"
              and (p.get("call") or {}).get("_pong_ok") is True, version=p.get("version"), reply=call_text(p), error=p.get("error") or p.get("call_error"))
        srv = subprocess.Popen([sys.executable, "-I", stub, "http", str(a.stub_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    httpx.get(f"http://127.0.0.1:{a.stub_port}/mcp", timeout=1)
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            p = probe(a.exe, a.settings_dir, work, "legacy-http", {"transport": "http", "url": f"http://127.0.0.1:{a.stub_port}/mcp"},
                      "echo", {"text": "你好"})
            check("MCP 客户端: 旧版 HTTP (SSE 回应, 会话号, 版本头)", p.get("ok") and not p.get("modern") and p.get("version") == "2025-06-18"
                  and call_text(p) == "你好", version=p.get("version"), reply=call_text(p), error=p.get("error") or p.get("call_error"))
        finally:
            srv.kill()
        p = probe(a.exe, a.settings_dir, work, "bad-command", {"transport": "stdio", "command": "no-such-mcp-server-xyz"})
        check("MCP 客户端: 命令不存在 → 报错", not p.get("ok") and "起不来" in (p.get("error") or ""), error=p.get("error"))
    finally:
        close_pet(a.pid)
    failed = [x["name"] for x in RESULTS if not x["ok"]]
    print(json.dumps({"total": len(RESULTS), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
