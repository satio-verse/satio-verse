"""资料本的本机接口 /api/agent/notebooks (界面归工作台改版会话做; 这里只给数据). 和别的工作台接口一样先过 Host / Origin 检查.

GET   ?                         资料本列表; 带 session=会话编号 时也说这个会话挂着哪本
GET   ?id=编号                  一本的详情: 资料 (状态、块数、有没有向量)、笔记
GET   ?id=编号&key=S3-17        那一块和前后文 (around=0–3) —— 回答里的出处点了回到原文用
GET   ?id=编号&source=3&view=read  一份资料按页 / 按节排好的正文 (阅读面板用; 读不了的带原因)
GET   ?id=编号&source=3&file=1  原文件: 只有 PDF 和图片在页面里打开, 别的一律当下载 (不在本机页面的源上渲染文档)
POST  {action: create, title} / rename {id, title} / delete {id} (挪进 .trash, 能找回)
      attach {id, session_id} / detach {session_id}
      add {id, name, data(Base64)} 文件 | {id, url} 网页 | {id, title, text} 文字
      quote {id, session_id, keys}  (阅读面板: 用户选了发给她的块, 记成这个会话里查到过的出处)
      remove {id, source} / note {id, text, cites?, as_source?} / search {id, query, k?, source?}
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re

from aiohttp import web

from . import material_readers as readers
from . import reading
from . import tools as T
from .context import identifier

UPLOAD_LIMIT = ((readers.MAX_BYTES + 2) // 3) * 4 + 64 * 1024
_INLINE = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".gif": "image/gif", ".webp": "image/webp"}


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _session_ok(hub, sid) -> str:
    sid = identifier(str(sid or ""))
    if sid not in hub.sessions and not (hub.home / "sessions" / f"{sid}.json").is_file():
        raise ValueError("没有这个会话")
    return sid


async def _get(request, hub) -> web.StreamResponse:
    store, q = hub.notebooks, request.query
    if not q.get("id"):
        out = {"notebooks": await asyncio.to_thread(store.list)}
        if q.get("session"):
            out["attached"] = store.bound(_session_ok(hub, q["session"]))
        return _json(out)
    nid = q["id"]
    if q.get("view") == "read":
        return _json(await asyncio.to_thread(reading.view, store, nid, int(q.get("source") or 0)))
    if q.get("key"):
        return _json(await asyncio.to_thread(store.read, nid, q["key"], int(q.get("around") or 1)))
    if q.get("file"):
        no = int(q.get("source") or 0)
        src = next((x for x in store.detail(nid)["sources"] if x.get("no") == no), None)
        name = str((src or {}).get("file") or "")
        if src is None or src.get("kind") != "file" or not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]{1,12}|[0-9a-f]{64}\.bin", name):
            raise ValueError("这份资料没有原文件")
        path = store._dir(nid) / "files" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("原文件不在了")
        suffix = path.suffix.lower()
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"}
        with path.open("rb") as f:
            head = f.read(4096)
        # 按文件头认 (后缀给空: 不信后缀), 真是 PDF / 图片才在页面里打开
        if suffix in _INLINE and readers.sniff(head, "") == {".jpeg": ".jpg"}.get(suffix, suffix):
            return web.FileResponse(path, headers={**headers, "Content-Type": _INLINE[suffix], "Content-Disposition": "inline"})
        return web.FileResponse(path, headers={**headers, "Content-Type": "application/octet-stream",
                                               "Content-Disposition": "attachment"})
    return _json(await asyncio.to_thread(store.detail, nid))


async def _post(request, hub) -> web.Response:
    store = hub.notebooks
    if request.content_length is not None and request.content_length > UPLOAD_LIMIT:
        raise ValueError(f"太大了：单个文件最多 {readers.MAX_BYTES // (1024 * 1024)} MB")
    body = await request.clone(client_max_size=UPLOAD_LIMIT).json()
    if not isinstance(body, dict):
        raise ValueError("请求要是对象")
    action, nid = body.get("action"), body.get("id")
    if action == "create":
        return _json({"notebook": await asyncio.to_thread(store.create, body.get("title"))})
    if action == "rename":
        return _json({"notebook": await asyncio.to_thread(store.rename, nid, body.get("title"))})
    if action == "delete":
        return _json(await asyncio.to_thread(store.trash, nid))
    if action in ("attach", "detach"):
        sid = _session_ok(hub, body.get("session_id"))
        return _json({"attached": await asyncio.to_thread(store.attach, sid, nid if action == "attach" else "")})
    if action == "quote":
        sid = _session_ok(hub, body.get("session_id"))
        return _json(await asyncio.to_thread(reading.quote, store, sid, nid, body.get("keys")))
    if action == "remove":
        return _json({"removed": await asyncio.to_thread(store.remove_source, nid, int(body.get("source") or 0))})
    if action == "note":
        return _json({"note": await asyncio.to_thread(store.add_note, nid, body.get("text"), body.get("cites") or [],
                                                      "user", bool(body.get("as_source")))})
    if action == "search":
        return _json(await asyncio.to_thread(store.search, nid, body.get("query"), int(body.get("k") or 6),
                                             int(body["source"]) if body.get("source") else None))
    if action != "add":
        raise ValueError("不支持的资料本操作")
    if body.get("data") is not None:
        try:
            data = base64.b64decode(str(body.get("data") or ""), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("data 要是有效的 Base64") from None
        return _json({"added": await asyncio.to_thread(store.add_file, nid, body.get("name"), data, "user")})
    if body.get("url"):
        base = str(body["url"]).strip().partition("#")[0]
        final, title, heads, text = await T.fetch_page(base)  # 只读公网 (每一跳都查)
        return _json({"added": await asyncio.to_thread(store.add_text, nid, title or final, text, "user", "web", final, heads)})
    return _json({"added": await asyncio.to_thread(store.add_text, nid, body.get("title") or "", body.get("text"), "user")})


async def notebooks(request: web.Request, hub) -> web.StreamResponse:
    try:
        if request.method == "GET":
            return await _get(request, hub)
        if request.method == "POST":
            return await _post(request, hub)
        return _json({"error": "只支持 GET / POST"}, 405)
    except web.HTTPRequestEntityTooLarge:
        return _json({"error": f"太大了：单个文件最多 {readers.MAX_BYTES // (1024 * 1024)} MB"}, 400)
    except (ValueError, TypeError, KeyError) as exc:
        return _json({"error": str(exc)[:500]}, 400)
    except OSError:
        return _json({"error": "资料本读写失败，请检查保存目录"}, 400)


def register(app: web.Application, refusal, hub) -> None:
    """挂 /api/agent/notebooks; refusal 是 app.ws_refusal (Host / Origin 不对回 403)."""
    async def handler(request: web.Request) -> web.StreamResponse:
        if (why := refusal(request)) is not None:
            raise web.HTTPForbidden(text=why)
        return await notebooks(request, hub)

    app.router.add_get("/api/agent/notebooks", handler)
    app.router.add_post("/api/agent/notebooks", handler)
