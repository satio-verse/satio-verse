"""Shared user control plane for desktop-native and workbench constraints."""
from aiohttp import web


def snapshot(hub):
    return {**hub.blacklist.snapshot(), 'scope': 'native-tools', 'enforcement': 'tool_arguments',
            'storage_path': str(hub.blacklist.path.resolve())}


class RevisionConflict(ValueError):
    pass


def check_revision(store, body):
    if 'expected_revision' not in body:
        return
    expected = body['expected_revision']
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise ValueError('expected_revision 必须是非负整数')
    if expected != store.snapshot()['revision']:
        raise RevisionConflict('权限设置已在别处更新，请刷新并检查后重新操作')


async def permissions(request, hub, *, learn=False):
    """Host and Origin checks are applied by the app before entering this handler."""
    try:
        if request.method == 'GET' and not learn:
            data = snapshot(hub)
            if request.query.get('execution') == '1':
                data['audit'] = []
            return web.json_response(data, headers={'Cache-Control': 'no-store'})
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('请求内容必须是对象')
        if learn:
            if set(body) - {'rule', 'evidence'}:
                raise ValueError('AI 入口仅接受规则和来源证据')
            rule = hub.blacklist.add(body.get('rule'), by='assistant', evidence=body.get('evidence', ''))
            return web.json_response({'rule': rule, 'snapshot': snapshot(hub)}, headers={'Cache-Control': 'no-store'})
        with hub.blacklist._lock:
            check_revision(hub.blacklist, body)
            action = body.get('action')
            if action == 'global':
                hub.blacklist.set_global_unrestricted(body.get('enabled'), by='user')
            elif action == 'rule_add':
                hub.blacklist.add(body.get('rule'), by='user')
            elif action == 'rule_update':
                hub.blacklist.update(body.get('rule_id'), body.get('changes'), by='user')
            elif action == 'rule_remove':
                hub.blacklist.remove(body.get('rule_id'), by='user')
            else:
                raise ValueError('不支持的权限设置操作')
            return web.json_response(snapshot(hub), headers={'Cache-Control': 'no-store'})
    except RevisionConflict as exc:
        return web.json_response({'error': str(exc), 'revision': hub.blacklist.snapshot()['revision']}, status=409)
    except (ValueError, TypeError, PermissionError) as exc:
        return web.json_response({'error': str(exc)[:500]}, status=400)
    except OSError:
        return web.json_response({'error': '权限设置未能保存，请检查本地保存目录'}, status=400)
