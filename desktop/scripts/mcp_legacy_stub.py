"""旧版 (2025-06-18) MCP 服务器的替身, 测桌宠的 MCP 客户端退回旧版握手用. 只用标准库.

    python mcp_legacy_stub.py stdio            一行一个 JSON-RPC (stdin/stdout)
    python mcp_legacy_stub.py http <端口>      Streamable HTTP: 回应用 SSE, initialize 时给 Mcp-Session-Id

故意做成现在多数服务器的样子 (官方 SDK 旧版的行为):
- 没握手就来的请求 (包括新版的 server/discover) 一律报 "Server not initialized";
- 只认 2025-06-18: 客户端要 2025-11-25 时回 2025-06-18 (测版本协商);
- tools/list 分两页 (测分页); 列工具前先反过来 ping 客户端一次 (测客户端回应服务器的请求);
- stdio 启动时往 stdout 打一行不是 JSON 的日志 (测客户端跳过它);
- slow 工具睡 seconds 秒再回 (测客户端的超时和"停"): stdio 下边睡边读, 收到 notifications/cancelled 就不回了.
  --log <文件>: 每次调工具、每个取消通知记一行 JSON (测试脚本核对客户端真的发了取消).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2025-06-18"
TOOLS = [
    {"name": "echo", "description": "原样返回 text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "add", "description": "两个数相加", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "slow", "description": "睡 seconds 秒再回", "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}},
     "annotations": {"readOnlyHint": True}},
]
LOG = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else None
LOG_LOCK = threading.Lock()


def log(**entry) -> None:
    if not LOG:
        return
    with LOG_LOCK, open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def handle(msg: dict, state: dict) -> dict | None:
    """返回要回的消息 (通知返回 None)."""
    method, mid = msg.get("method"), msg.get("id")
    if "id" not in msg:
        if method == "notifications/initialized":
            state["ready"] = True
        elif method == "notifications/cancelled":
            p = msg.get("params") or {}
            state.setdefault("cancelled", set()).add(str(p.get("requestId")))
            log(event="cancelled", requestId=p.get("requestId"), reason=p.get("reason"))
        return None
    if method == "initialize":
        state["init"] = True
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": VERSION, "capabilities": {"tools": {}},
                                                         "serverInfo": {"name": "legacy-stub", "version": "1.0"}, "instructions": "旧版替身"}}
    if not state.get("init"):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "Server not initialized"}}
    if method == "tools/list":
        cursor = (msg.get("params") or {}).get("cursor")
        if not cursor:
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[:1], "nextCursor": "page2"}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[1:]}}
    if method == "tools/call":
        p = msg.get("params") or {}
        args = p.get("arguments") or {}
        log(event="call", name=p.get("name"), id=mid)
        if p.get("name") == "echo":
            text = str(args.get("text", ""))
        elif p.get("name") == "slow":  # HTTP 下每个请求一个线程, 直接睡
            time.sleep(float(args.get("seconds", 1)))
            text = f"睡了 {args.get('seconds', 1)} 秒"
        elif p.get("name") == "add":
            text = str(args.get("a", 0) + args.get("b", 0))
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Unknown tool"}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}


def run_stdio() -> None:
    # 规范: stdio 上是 UTF-8. Windows 上管道默认按系统代码页 (GBK) 解码, 收到中文 (比如取消通知里的原因) 会解码失败
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    out = sys.stdout
    out.write("legacy stub starting (这一行不是 JSON)\n")
    out.flush()
    state: dict = {}
    pinged = False
    lock = threading.Lock()

    def send(obj: dict) -> None:
        with lock:
            out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            out.flush()

    def slow(msg: dict) -> None:  # 边睡边看有没有被取消, 取消了就不回
        seconds = float(((msg.get("params") or {}).get("arguments") or {}).get("seconds", 1))
        end = time.time() + seconds
        while time.time() < end:
            if str(msg["id"]) in state.get("cancelled", set()):
                log(event="dropped", id=msg["id"])
                return
            time.sleep(0.05)
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": f"睡了 {seconds:g} 秒"}], "isError": False}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("method") == "tools/list" and not pinged:  # 先反过来 ping 一次客户端
            pinged = True
            send({"jsonrpc": "2.0", "id": "srv-ping-1", "method": "ping"})
        if "method" not in msg:  # 客户端对我们 ping 的回应
            state["pong"] = msg.get("result") == {}
            continue
        if msg.get("method") == "tools/call" and (msg.get("params") or {}).get("name") == "slow" and state.get("init"):
            log(event="call", name="slow", id=msg.get("id"))
            threading.Thread(target=slow, args=(msg,), daemon=True).start()
            continue
        reply = handle(msg, state)
        if reply is not None:
            if msg.get("method") == "tools/call" and state.get("pong") is not None:
                reply.setdefault("result", {})["_pong_ok"] = state["pong"]
            send(reply)


class Handler(BaseHTTPRequestHandler):
    sessions: dict = {}

    def log_message(self, *args):  # 不往 stderr 打访问日志
        pass

    def _send(self, status: int, body: dict | None, sse: bool, extra: dict | None = None) -> None:
        data = b"" if body is None else ((f"event: message\ndata: {json.dumps(body, ensure_ascii=False)}\n\n").encode() if sse
                                         else json.dumps(body, ensure_ascii=False).encode())
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        sid = self.headers.get("Mcp-Session-Id")
        if msg.get("method") == "initialize":
            sid = f"sess-{len(self.sessions) + 1}"
            self.sessions[sid] = {}
            self._send(200, handle(msg, self.sessions[sid]), True, {"Mcp-Session-Id": sid})
            return
        if sid not in self.sessions:  # 官方 SDK 旧版的说法
            self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Bad Request: No valid session ID provided"}}, False)
            return
        if self.headers.get("MCP-Protocol-Version") != VERSION:
            self._send(400, {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": "Bad MCP-Protocol-Version"}}, False)
            return
        reply = handle(msg, self.sessions[sid])
        if reply is None:
            self._send(202, None, False)
        else:
            self._send(200, reply, True)

    def do_GET(self):
        self._send(405, None, False)


def main() -> None:
    if sys.argv[1:2] == ["http"]:
        ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
    else:
        run_stdio()


if __name__ == "__main__":
    main()
