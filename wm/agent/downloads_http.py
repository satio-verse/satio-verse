"""下载队列的本机接口 /api/agent/downloads (给你用的: 现在是页面脚本和命令行, 以后是下载面板). 和别的工作台接口一样先过
Host / Origin 检查. 她的工具不走这里; 这里的动作都算你做的, 所有会话排的你都能动.
点头 / 不同意不在这里: 只走卡片 (会话里那张申请卡片、桌宠对话栏、QQ / 微信).

GET   ?             服务在不在、小看守怎样、没结束的和最近结束的任务. 没活的时候不把下载服务叫起来 (history=1 才叫)
POST  {action: pause | resume | cancel, id}
      {action: cleanup, scope}                scope: records / cache / allWithoutFiles (默认) / all
      {action: restart_service}               让下载服务重新起 (之前"起不来"的也再试一次; 在下的从断点接着下)
"""
from __future__ import annotations

from aiohttp import web

from ..downloads import DownloadError


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


async def downloads(request: web.Request, hub) -> web.Response:
    dl = hub.downloads
    try:
        if request.method == "GET":
            return _json(await dl.overview(history=request.query.get("history") == "1"))
        if request.method != "POST":
            return _json({"error": "只支持 GET / POST"}, 405)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("请求要是对象")
        return _json(await dl.user(str(body.get("action") or ""), str(body.get("id") or ""), str(body.get("scope") or "")))
    except DownloadError as exc:
        return _json({"error": str(exc), "code": exc.code}, 503 if exc.code == "service-down" else 400)
    except (ValueError, TypeError, KeyError) as exc:
        return _json({"error": str(exc)[:500] or "请求看不懂"}, 400)


def register(app: web.Application, refusal, hub) -> None:
    """挂 /api/agent/downloads; refusal 是 app.ws_refusal (Host / Origin 不对回 403)."""
    async def handler(request: web.Request) -> web.Response:
        if (why := refusal(request)) is not None:
            raise web.HTTPForbidden(text=why)
        return await downloads(request, hub)

    app.router.add_get("/api/agent/downloads", handler)
    app.router.add_post("/api/agent/downloads", handler)
