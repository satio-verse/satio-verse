"""User terminal HTTP API; caller applies the app Host/Origin checks."""
from aiohttp import web

from .terminal import TerminalManager


async def terminal(request, hub):
    try:
        body = await request.json() if request.method == 'POST' else dict(request.query)
        if not isinstance(body, dict):
            raise ValueError('需要 JSON 对象')
        session = hub.get(str(body.get('id') or ''))
        if session is None:
            raise ValueError('请先选择会话')
        if not hasattr(hub, 'terminals'):
            hub.terminals = TerminalManager(hub)
        manager, tid = hub.terminals, body.get('terminal_id')
        if request.method == 'GET':
            data = manager.output(session, tid, body.get('after', 0)) if tid else manager.list(session)
            return web.json_response(data, headers={'Cache-Control': 'no-store'})
        action = body.get('action')
        if action == 'create':
            result = await manager.create(session, shell=body.get('shell', 'powershell'), cols=body.get('cols', 100), rows=body.get('rows', 28), title=body.get('title', ''))
        elif action == 'input':
            result = await manager.input(session, tid, body.get('data'))
        elif action == 'resize':
            result = await manager.resize(session, tid, body.get('cols'), body.get('rows'))
        elif action == 'interrupt':
            result = await manager.interrupt(session, tid)
        elif action == 'close':
            result = await manager.terminate(session, tid)
        elif action == 'rename':
            result = manager.rename(session, tid, body.get('title'))
        else:
            raise ValueError('未知终端操作')
        return web.json_response({'ok': True, 'terminal': result}, headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError, OSError, RuntimeError, KeyError) as exc:
        return web.json_response({'ok': False, 'error': str(exc)[:300]}, status=400)
