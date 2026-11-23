"""本机模型的检测接口 /api/llm/local (2026-11-02 本机大模型接进来时加的; 界面归工作台改版会话做, 这里只给数据).
和别的本机接口一样先过 Host / Origin 检查. 只问本机 / 局域网地址的服务 (云端的地址一律不问), 不发生成请求, key 不出模型对象.

GET  ?                           配置里写了的 (llm.local_api) 和现在连着的本机模型 (桌宠、QQ、工作台选了的), 各问一遍
GET  ?base_url=地址&model=名字    问这一个 (面板上填好地址、点"检测"时): 有哪些模型、上下文多长、看不看得了图、是哪家
返回 {items: [{base_url, model, ok, server, models, ctx, ctx_train, vision, tools, slots, notes, configured}], defaults}
"""
from __future__ import annotations

from aiohttp import web

from . import cloud as C
from . import ledger as L


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


async def _ask(llm, force: bool) -> dict:
    info = await llm.probe(force)
    return {"base_url": C.redact(llm.base_url), "model": llm.model, **{k: v for k, v in info.items() if k != "model"},
            "notes": C.probe_notes(info, llm.base_url)}


async def local_status(request: web.Request) -> web.Response:
    q = request.query
    if q.get("base_url"):
        try:
            spec = C.parse_model({"provider": "selfhost", "base_url": q["base_url"], "model": q.get("model") or ""})
        except C.ProfileError as exc:
            return _json({"error": str(exc)}, 400)
        if not C.is_local_url(spec["base_url"]):
            return _json({"error": "只检测本机或局域网里的服务 (127.0.0.1、192.168.x.x 这类地址)"}, 400)
        llm = C.CloudLLM(spec, {})
        try:
            return _json({"items": [await _ask(llm, True)]})
        finally:
            await llm.close()
    items, seen = [], set()
    spec = C.local_spec()
    if spec is not None:
        llm = C.CloudLLM(spec, {})
        try:
            items.append({**await _ask(llm, False), "configured": True})
        finally:
            await llm.close()
        seen.add((spec["base_url"], spec["model"]))
    led = L.current()
    for m in (led.live_models() if led else []):  # 连着的本机模型: 用它自己问 (局域网里带 key 的服务, key 也还在它手里)
        if getattr(m, "local", False) and (m.base_url, m.model) not in seen:
            seen.add((m.base_url, m.model))
            items.append({**await _ask(m, False), "configured": False})
    return _json({"items": items, "defaults": {k: C.LOCAL_API[k] for k in ("base_url", "model", "thinking", "ctx", "read_timeout_s")}})


def register(app: web.Application, refusal) -> None:
    """refusal: app.py 的 ws_refusal (Host / Origin 不对返回原因)."""
    async def handler(request: web.Request) -> web.Response:
        if (why := refusal(request)) is not None:
            raise web.HTTPForbidden(text=why)
        return await local_status(request)

    app.router.add_get("/api/llm/local", handler)
