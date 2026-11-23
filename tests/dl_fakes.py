"""本机的假下载站 (下载队列的测试用, 不连外网): 支持 Range 续传、ETag、限速、跳转、中途掉线、报错,
每个请求和实际发出去的字节数都记下来 (测"没点头时传输量为 0""从断点接着下"就看这份记录).

测试里直接用:  site = FakeSite(); port = await site.start(); site.add("a.bin", blob(1 << 20)); ...; await site.stop()
单独起一个进程: python tests/dl_fakes.py --port 18999 --file a.bin:1048576:7 --rate 200000 --log 记录.jsonl
  (起好了在标准输出打一行 ready <端口>; 记录是一行一个 JSON: {"ev": "request" | "sent", ...})
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
import time
from pathlib import Path

from aiohttp import web

CHUNK = 16 * 1024


def blob(size: int, seed: int = 1) -> bytes:
    """按种子造的固定内容 (两个进程里用同一个种子造出来的一样, 好核对哈希)."""
    return random.Random(seed).randbytes(size)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeSite:
    def __init__(self, rate: int = 0, log: Path | None = None):
        self.rate = rate                      # 每条连接每秒发多少字节 (0 = 不限)
        self.files: dict[str, dict] = {}
        self.requests: list[dict] = []        # 每个请求: {t, method, path, range, if_range, status}
        self.sent = 0                         # 一共发出去多少字节的文件内容
        self.sent_by: dict[str, int] = {}
        self.log = Path(log) if log else None
        self.runner = None
        self.port = 0
        self.gate = asyncio.Event()           # 清掉以后所有响应停在半路 (测"下到一半"用); 默认放行
        self.gate.set()

    def add(self, name: str, data: bytes, **opts) -> str:
        """opts: redirect (转去的地址) · status (固定回这个状态码) · fail_times (前几次回 503) · no_range (不认 Range) ·
        drop_after (每个响应发这么多字节就掉线) · chunked (不报 Content-Length) · etag (自己定; "" = 不给) · weak (弱 ETag)"""
        self.files[name] = {"data": data, "etag": opts.pop("etag", f'"{sha256(data)[:16]}"'), "hits": 0, **opts}
        return self.url(name)

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def _log(self, row: dict) -> None:
        if self.log is not None:
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        row = {"ev": "request", "t": time.time(), "method": request.method, "path": name,
               "range": request.headers.get("Range", ""), "if_range": request.headers.get("If-Range", ""), "status": 0}
        self.requests.append(row)
        f = self.files.get(name)

        def done(status: int, resp):
            row["status"] = status
            self._log(row)
            return resp
        if f is None:
            return done(404, web.Response(status=404, text="no such file"))
        f["hits"] += 1
        if f.get("redirect"):
            return done(302, web.Response(status=302, headers={"Location": f["redirect"]}))
        if f.get("status"):
            return done(f["status"], web.Response(status=f["status"], text="nope"))
        if f["hits"] <= int(f.get("fail_times") or 0):
            return done(503, web.Response(status=503, text="busy"))
        data, etag = f["data"], ("W/" if f.get("weak") else "") + f["etag"] if f["etag"] else ""
        start, status = 0, 200
        rng = request.headers.get("Range", "")
        if rng.startswith("bytes=") and not f.get("no_range"):
            cond = request.headers.get("If-Range", "")
            if not cond or cond == etag:  # 文件变了 (If-Range 对不上): 从头给
                start = int(rng[6:].split("-")[0] or 0)
                if start >= len(data):
                    return done(416, web.Response(status=416, headers={"Content-Range": f"bytes */{len(data)}"}))
                status = 206
        headers = {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"}
        if etag:
            headers["ETag"] = etag
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        resp = web.StreamResponse(status=status, headers=headers)
        if not f.get("chunked"):
            resp.content_length = len(data) - start
        row["status"] = status
        self._log(row)
        await resp.prepare(request)
        sent = 0
        try:
            for at in range(start, len(data), CHUNK):
                await self.gate.wait()
                piece = data[at:at + CHUNK]
                drop = f.get("drop_after")
                if drop is not None and sent + len(piece) > drop:
                    await resp.write(piece[:max(0, drop - sent)])
                    sent += max(0, drop - sent)
                    request.transport.close()  # 掉线
                    return resp
                await resp.write(piece)
                sent += len(piece)
                if self.rate:
                    await asyncio.sleep(len(piece) / self.rate)
        except (ConnectionError, OSError):  # 对面不要了 (暂停、取消、被杀): 不算错
            pass
        finally:
            self.sent += sent
            self.sent_by[name] = self.sent_by.get(name, 0) + sent
            self._log({"ev": "sent", "t": time.time(), "path": name, "start": start, "bytes": sent})
        return resp

    async def start(self, port: int = 0) -> int:
        app = web.Application()
        app.router.add_get("/{name:.+}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        self.gate.set()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    def gets(self, name: str | None = None) -> list[dict]:
        return [r for r in self.requests if name is None or r["path"] == name]


def read_log(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


async def _main(a) -> None:
    site = FakeSite(rate=a.rate, log=a.log)
    port = await site.start(a.port)
    for item in a.file:
        name, size, *seed = item.split(":")
        site.add(name, blob(int(size), int(seed[0]) if seed else 1))
    for item in a.redirect:
        name, _, target = item.partition("=")
        site.add(name, b"", redirect=target)
    print(f"ready {port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="本机的假下载站")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--file", action="append", default=[], metavar="名字:字节数[:种子]")
    ap.add_argument("--redirect", action="append", default=[], metavar="名字=转去的地址")
    ap.add_argument("--rate", type=int, default=0, help="每条连接每秒发多少字节")
    ap.add_argument("--log", type=Path)
    try:
        asyncio.run(_main(ap.parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
