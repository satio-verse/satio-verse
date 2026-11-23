"""Small MCP client for the workbench. Discovery also starts a process/connects a server.

Callers authorize both discovery and calls before using this module. A client owns its
subprocess and HTTP session; cancellation closes them. No inherited API keys are sent
to stdio servers unless their configuration explicitly supplies them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
from urllib.parse import urlsplit

import aiohttp

MAX_MESSAGE = 2 * 1024 * 1024
PROTOCOL = "2025-11-25"
SUPPORTED = {PROTOCOL, "2025-06-18", "2025-03-26", "2024-11-05"}
SECRET_ENV = re.compile(r"key|token|secret|password|passwd|cookie|credential|auth", re.I)


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, config: dict, timeout: float = 45):
        self.config = dict(config)
        self.timeout = min(max(float(timeout), 1), 180)
        self.proc = None
        self.http = None
        self.stderr_task = None
        self.session_id = None
        self.protocol = PROTOCOL
        self.counter = 0

    async def __aenter__(self):
        try:
            if self.config.get("transport", "stdio") == "stdio":
                command = str(self.config.get("command") or "").strip()
                args = self.config.get("args") or []
                if not command or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise MCPError("MCP 启动命令或参数无效")
                env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
                env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
                env["PYTHONIOENCODING"] = "utf-8"
                executable = shutil.which(command) or command
                if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
                    if any(re.search(r'[&|<>^%!\r\n"]', item) for item in [executable, *args]):
                        raise MCPError("批处理启动参数含 Shell 特殊符号，请配置明确的 cmd 启动命令")
                    quoted = '"' + ' '.join('"' + item + '"' for item in [executable, *args]) + '"'
                    command, args = os.environ.get("COMSPEC", "cmd.exe"), ["/d", "/s", "/c", quoted]
                else:
                    command = executable
                options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                self.proc = await asyncio.create_subprocess_exec(command, *args, cwd=self.config.get("cwd") or None,
                    env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE + 1, **options)
                self.stderr_task = asyncio.create_task(self._drain_stderr())
            else:
                url = urlsplit(str(self.config.get("url") or ""))
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                    raise MCPError("MCP 地址必须为不含用户名或密码的 HTTP(S) 地址")
                self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False)
            init = await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "worldmodel-workbench", "version": "1.0"}})
            self.protocol = init.get("protocolVersion", PROTOCOL)
            if self.protocol not in SUPPORTED:
                raise MCPError("MCP 服务器协商了不支持的协议版本")
            await self.notify("notifications/initialized", {})
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *args):
        await self.close()

    async def _drain_stderr(self):
        # Never expose server stderr: credentials can occur in third-party diagnostics.
        while await self.proc.stderr.read(8192):
            pass

    async def close(self):
        if self.http is not None:
            await self.http.close()
            self.http = None
        if self.proc is not None:
            proc, self.proc = self.proc, None
            if proc.stdin:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), .4)
                except asyncio.TimeoutError:
                    if os.name == "nt":
                        killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(killer.wait(), 3)
                    else:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                    await proc.wait()
            if self.stderr_task:
                self.stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.stderr_task
                self.stderr_task = None

    async def _write(self, message):
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_MESSAGE:
            raise MCPError("MCP 请求过大")
        self.proc.stdin.write(data + b"\n")
        await self.proc.stdin.drain()

    async def notify(self, method, params):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.proc:
            await self._write(message)
        else:
            await self._http(message, notification=True)

    async def request(self, method, params=None):
        self.counter += 1
        message = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}}
        if self.proc:
            await self._write(message)
            response = await asyncio.wait_for(self._read_response(message["id"]), self.timeout)
        else:
            response = await self._http(message)
        if not isinstance(response, dict) or response.get("id") != message["id"]:
            raise MCPError("MCP 返回了不匹配的响应")
        if "error" in response:
            # Do not echo arbitrary server errors; they may embed connection secrets.
            raise MCPError("MCP 服务器拒绝了请求，请检查工具参数和服务器状态")
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPError("MCP 返回的结果格式无效")
        return result

    async def _read_response(self, wanted):
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise MCPError("MCP 返回的消息过大") from exc
            if not raw:
                raise MCPError("MCP 进程已断开")
            if len(raw) > MAX_MESSAGE:
                raise MCPError("MCP 返回的消息过大")
            try:
                reply = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise MCPError("MCP 标准输出不是有效 JSON 消息") from exc
            if not isinstance(reply, dict):
                raise MCPError("MCP 消息格式无效")
            if "method" in reply:
                if "id" in reply:
                    answer = {"jsonrpc": "2.0", "id": reply["id"]}
                    answer.update({"result": {}} if reply["method"] == "ping" else
                        {"error": {"code": -32601, "message": "Client capability not supported"}})
                    await self._write(answer)
                continue
            if reply.get("id") == wanted:
                return reply

    async def _http(self, message, notification=False):
        headers = {str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}
        headers.update({"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.protocol})
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        async with self.http.post(self.config["url"], json=message, headers=headers, allow_redirects=False) as response:
            if response.status >= 300:
                raise MCPError(f"MCP HTTP 请求失败 ({response.status})")
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                if len(sid) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in sid):
                    raise MCPError("MCP 会话标识无效")
                self.session_id = sid
            if notification:
                return {}
            if response.content_type == "text/event-stream":
                total, data = 0, []
                async for raw in response.content:
                    total += len(raw)
                    if total > MAX_MESSAGE:
                        raise MCPError("MCP 返回的消息过大")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        value = json.loads("\n".join(data))
                        data = []
                        if isinstance(value, dict) and value.get("id") == message["id"] and "method" not in value:
                            return value
                raise MCPError("MCP 数据流结束前没有返回结果")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_MESSAGE:
                    raise MCPError("MCP 返回的消息过大")
                chunks.append(chunk)
            raw = b"".join(chunks)
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise MCPError("MCP 返回的结果不是有效 JSON") from exc

    async def list_tools(self):
        tools, cursor, seen = [], None, set()
        for _ in range(20):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            batch = result.get("tools")
            if not isinstance(batch, list):
                raise MCPError("MCP 工具清单格式无效")
            tools.extend(batch)
            if len(tools) > 500:
                raise MCPError("MCP 工具清单过大")
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise MCPError("MCP 工具清单分页无效")
            seen.add(cursor)
        raise MCPError("MCP 工具清单页数过多")
