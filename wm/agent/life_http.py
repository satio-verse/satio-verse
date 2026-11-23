"""Local control-plane handler; caller applies Host/Origin checks."""
from aiohttp import web
from .life import LifeConflict


def dispatch(life, body, actor='user'):
    action = body.get('action', 'state')
    if action in {'state', 'catalog', 'verify', 'history'}:
        return life.state()
    if action == 'preview':
        return life.preview(body.get('changes'))
    if action == 'apply':
        return life.apply(body.get('changes'), body.get('reason'), body.get('expected_revision'), actor)
    if action == 'observe':
        return life.observe(body.get('change_id'), body.get('text'), actor)
    if action == 'rollback':
        return life.rollback(body.get('change_id'), body.get('expected_revision'), body.get('reason'),
                             preview=body.get('preview', True) is not False, actor=actor)
    raise ValueError('不支持的生活设置操作')


async def life_handler(request, manager):
    try:
        if not getattr(manager, 'life_settings', None):
            raise ValueError('生活设置服务尚未连接')
        body = {'action': request.query.get('action', 'state')} if request.method == 'GET' else await request.json()
        if not isinstance(body, dict):
            raise ValueError('请求内容必须为对象')
        return web.json_response(dispatch(manager.life_settings, body), headers={'Cache-Control': 'no-store'})
    except LifeConflict as exc:
        return web.json_response({'error': str(exc)}, status=409)
    except (ValueError, TypeError, OSError) as exc:
        return web.json_response({'error': str(exc)[:300]}, status=400)
