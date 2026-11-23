"""她自己写的 MCP 用的骨架 (只用标准库, 拷进每个 MCP 的文件夹里一起跑). 规范见 wm/selfmcp.py 的 CONVENTION.

    from selfmcp_kit import Server, http_get_json

    srv = Server("weather", "0.1.0")

    @srv.tool("today", "查一个城市今天的天气", {"city": {"type": "string", "description": "城市名"}}, required=["city"])
    def today(city: str) -> str:
        data = http_get_json("https://wttr.in/" + city, {"format": "j1"})
        return ...

    if __name__ == "__main__":
        srv.run()

- 走 stdio, 一行一个 JSON-RPC (MCP 旧版握手: initialize / tools/list / tools/call; 新版的 server/discover 回"没有这个方法", 客户端会退回旧版);
- 工具都是只读的 (annotations.readOnlyHint = true);
- 联网只能用 http_get / http_get_json: 只许 GET, 只许 manifest.json 里 network 列出的域名 (https), 每次最多读 1 MB, 10 秒超时, 不带 Cookie;
- 存东西只能用 save_json / load_json: 放在这个 MCP 自己的 data/ 文件夹里.
"""
from __future__ import annotations

import json as _json
import sys as _sys
import urllib.parse as _parse
import urllib.request as _request
from pathlib import Path as _Path

_HERE = _Path(__file__).resolve().parent
_MAX_BYTES = 1024 * 1024
_UA = "Mozilla/5.0 (WorldModel self-mcp)"


def _manifest() -> dict:
    try:
        return _json.loads((_HERE / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class _NoRedirect(_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 跳转可能跳到别的域名: 不跟


_OPENER = _request.build_opener(_NoRedirect, _request.ProxyHandler({}))


def http_get(url: str, params: dict | None = None, timeout: float = 10.0) -> bytes:
    """GET 一个 https 地址 (只许 manifest.json 的 network 里的域名), 最多读 1 MB. 不行就抛 ValueError."""
    if params:
        url += ("&" if "?" in url else "?") + _parse.urlencode(params)
    u = _parse.urlsplit(url)
    host = (u.hostname or "").lower()
    allowed = [str(h).lower() for h in _manifest().get("network") or []]
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in allowed):
        raise ValueError(f"不许访问 {host or url} (manifest.json 的 network 里没有, 或者不是 https)")
    req = _request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"}, method="GET")
    with _OPENER.open(req, timeout=timeout) as resp:
        data = resp.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError("回应超过 1 MB")
    return data


def http_get_json(url: str, params: dict | None = None, timeout: float = 10.0):
    return _json.loads(http_get(url, params, timeout).decode("utf-8", errors="replace"))


def _data_file(name: str) -> _Path:
    if not name or any(c in name for c in "/\\:") or name.startswith("."):
        raise ValueError("文件名只能是一个简单的名字")
    d = _HERE / "data"
    d.mkdir(exist_ok=True)
    return d / (name if name.endswith(".json") else name + ".json")


def save_json(name: str, obj) -> None:
    """存到这个 MCP 自己的 data/<name>.json (最多 1 MB)."""
    text = _json.dumps(obj, ensure_ascii=False)
    if len(text.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("太大了")
    _data_file(name).write_text(text, encoding="utf-8")


def load_json(name: str, default=None):
    try:
        return _json.loads(_data_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Server:
    def __init__(self, name: str, version: str = "0.1.0"):
        self.name, self.version = name, version
        self._tools: dict[str, tuple[dict, object]] = {}

    def tool(self, name: str, description: str, properties: dict | None = None, required: list[str] | None = None):
        """登记一个只读工具. 函数返回字符串 (或者能转成 JSON 的东西)."""
        def wrap(fn):
            schema = {"type": "object", "properties": properties or {}}
            if required:
                schema["required"] = list(required)
            self._tools[name] = ({"name": name, "description": description, "inputSchema": schema,
                                  "annotations": {"readOnlyHint": True}}, fn)
            return fn
        return wrap

    def handle(self, msg: dict) -> dict | None:
        """一条 JSON-RPC -> 回应 (通知返回 None). 测试直接调这个."""
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                      "capabilities": {"tools": {}}, "serverInfo": {"name": self.name, "version": self.version}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [spec for spec, _ in self._tools.values()]}
        elif method == "tools/call":
            spec_fn = self._tools.get(params.get("name"))
            if spec_fn is None:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "没有这个工具"}}
            try:
                out = spec_fn[1](**(params.get("arguments") or {}))
                text = out if isinstance(out, str) else _json.dumps(out, ensure_ascii=False)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as exc:  # 工具出错: 告诉模型, 不让服务器挂掉
                result = {"content": [{"type": "text", "text": f"出错了: {type(exc).__name__}: {exc}"[:500]}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"没有这个方法: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def run(self) -> None:
        stdin = _sys.stdin.buffer
        out = _sys.stdout.buffer
        for line in stdin:
            try:
                msg = _json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            resp = self.handle(msg) if isinstance(msg, dict) else None
            if resp is not None:
                out.write((_json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                out.flush()
