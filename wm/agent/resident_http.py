"""Local-only responsibility API; the app enforces Host/Origin before entry."""
from aiohttp import web


async def resident_handler(request, hub):
    try:
        if request.method == 'GET':
            result = hub.resident.state(request.query.get('after', 0))
        else:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('请求必须为对象')
            if body.get('action') == 'create':
                item = hub.resident.create(body.get('session_id'), body.get('objective'),
                    title=body.get('title', ''), next_at=body.get('next_at', 0), budget_cny=body.get('budget_cny', 0))
            else:
                if 'expected_revision' not in body:
                    raise ValueError('请提供当前任务版本')
                item = await hub.resident.control(body.get('id'), body.get('action'), body['expected_revision'])
            result = {'item': item}
        return web.json_response({'ok': True, **result})
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return web.json_response({'ok': False, 'error': str(exc)}, status=400)
