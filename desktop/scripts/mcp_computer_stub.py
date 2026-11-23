"""Windows-MCP 的替身 (computertest.ps1 用): 工具名、参数和 Windows-MCP 0.8.8 一样, 但不真点、不真打字. 只用标准库.

    python mcp_computer_stub.py --log <文件>      stdio, 一行一个 JSON-RPC

- 21 个工具都列出来 (包括 PowerShell、FileSystem 这些桌宠要锁定的), 测桌宠没把它们交给模型;
- Screenshot / Snapshot 的文字里列出桌面上看得见的窗口标题 (和真的一样), 再带一张 1x1 的 PNG: 测桌宠换掉聊天窗口的标题、拿掉图片;
  Screenshot 的 region 写 "slow" 时睡 8 秒再回 (测"停"和快捷键), 收到 notifications/cancelled 就不回了;
- 其余工具只回一句 "ok", 每次调用、每个取消通知记一行 JSON 到 --log (测试脚本核对哪些调用真的到了服务器).
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes

LOG = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else None
LOCK = threading.Lock()
CANCELLED: set[str] = set()
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

LOC = {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "string"}, {"type": "null"}], "default": None}
LABEL = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}


def tool(name, props=None, read_only=False):
    return {"name": name, "title": name, "description": f"(替身) {name}",
            "inputSchema": {"type": "object", "properties": props or {}},
            "annotations": {"readOnlyHint": read_only, "title": name}}


TOOLS = [
    tool("ControlStatus", read_only=True), tool("DisplayInventory", read_only=True),
    tool("Wait", {"duration": {"type": "integer"}}, True),
    tool("Screenshot", {"region": {"anyOf": [{"type": "array"}, {"type": "string"}, {"type": "null"}]}}, True),
    tool("Snapshot", {"use_vision": {"type": "boolean"}, "use_dom": {"type": "boolean"}, "region": {"type": "array"}}, True),
    tool("WaitFor", {"condition": {"type": "string"}, "window_name": {"type": "string"}}, True),
    tool("Click", {"loc": LOC, "label": LABEL, "button": {"type": "string"}}),
    tool("Type", {"text": {"type": "string"}, "loc": LOC, "label": LABEL, "press_enter": {"type": "boolean"}}),
    tool("Scroll", {"loc": LOC, "label": LABEL, "direction": {"type": "string"}}),
    tool("Move", {"loc": LOC, "label": LABEL, "from_loc": LOC, "drag": {"type": "boolean"}}),
    tool("Shortcut", {"shortcut": {"type": "string"}}),
    tool("MultiSelect", {"locs": {"type": "array"}, "labels": {"type": "array"}}),
    tool("MultiEdit", {"locs": {"type": "array"}, "labels": {"type": "array"}}),
    tool("App", {"mode": {"type": "string", "enum": ["launch", "launch_executable", "resize", "switch"]}, "name": {"type": "string"},
                 "executable": {"type": "string"}, "args": {"type": "array"}, "cwd": {"type": "string"}}),
    tool("PowerShell", {"command": {"type": "string"}}), tool("FileSystem", {"mode": {"type": "string"}, "path": {"type": "string"}}),
    tool("Registry", {"mode": {"type": "string"}, "path": {"type": "string"}}), tool("Process", {"mode": {"type": "string"}}),
    tool("Clipboard", {"mode": {"type": "string"}}), tool("Scrape", {"url": {"type": "string"}}, True),
    tool("Notification", {"title": {"type": "string"}, "message": {"type": "string"}}),
]


def log(**entry) -> None:
    if LOG:
        with LOCK, open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def send(msg: dict) -> None:
    with LOCK:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def window_titles() -> list[str]:
    user32 = ctypes.windll.user32
    titles = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(each, 0)
    return titles


def call(mid, name: str, args: dict) -> None:
    log(event="call", id=mid, name=name, args=args)
    if name in ("Screenshot", "Snapshot"):
        if args.get("region") == "slow":
            for _ in range(80):  # 8 秒, 边睡边看有没有取消
                time.sleep(0.1)
                if str(mid) in CANCELLED:
                    log(event="dropped", id=mid)
                    return
        wins = "\n".join(f'window "{t}"' for t in window_titles())
        text = f"Cursor Position: (100,100)\nOpened Windows:\n{wins}\n├── (120,130) button \"好的\"  [action: click]"
        content = [{"type": "text", "text": text}, {"type": "image", "data": PNG, "mimeType": "image/png"}]
    else:
        content = [{"type": "text", "text": f"ok: {name} {json.dumps(args, ensure_ascii=False)}"}]
    send({"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": False}})


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if "id" not in msg:
            if method == "notifications/cancelled":
                rid = (msg.get("params") or {}).get("requestId")
                CANCELLED.add(str(rid))
                log(event="cancelled", requestId=rid)
            continue
        if method == "initialize":
            want = (msg.get("params") or {}).get("protocolVersion", "2025-11-25")
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": want, "capabilities": {"tools": {}},
                                                         "serverInfo": {"name": "windows-mcp-stub", "version": "0.8.8"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            threading.Thread(target=call, args=(mid, p.get("name", ""), p.get("arguments") or {}), daemon=True).start()
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()
