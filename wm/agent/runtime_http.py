"""Workbench runtime controls. Caller must enforce the local Host/Origin gate."""
from __future__ import annotations

from aiohttp import web

from . import activity
from .context import identifier
from .capabilities import inventory, tool_catalog


async def runtime(request, hub):
    try:
        if request.method == 'GET':
            view = request.query.get('view', 'overview')
            sid = request.query.get('id', '')
            if view == 'presets':
                result = {'presets': hub.presets.list()}
            elif view == 'activity':  # 统一状态: 现在在做什么、各自在等什么 (wm/agent/activity.py)
                result = activity.build(hub)
            elif view == 'experiences':  # 工作经验: 每条的状态、置信度、来源, 和推送的统计 (wm/agent/experience.py)
                result = hub.experience.view()
            elif view == 'timeline':  # 她的流水账: 她在 QQ 那边做过的事 (某一天, day=YYYY-MM-DD, 不给是今天)
                result = activity.timeline(hub, request.query.get('day', ''))
            elif view == 'tree':
                result = {'tree': hub.teams.tree(sid or None), 'config': hub.teams.configuration()}
            elif view in {'tools', 'capabilities'}:
                session = hub.get(identifier(sid)) if sid else None
                if sid and session is None:
                    raise ValueError('没有这个会话')
                result = inventory(hub, session, request.query.get('preset')) if view == 'capabilities' else {'tools': tool_catalog(hub, session)}
            elif view == 'history':
                result = hub.history_search(sid, request.query.get('query', ''), offset=int(request.query.get('offset', 0)), limit=int(request.query.get('limit', 30)))
            elif view == 'message':
                result = hub.history_read(sid, request.query.get('message_id', ''), offset=int(request.query.get('offset', 0)), limit=int(request.query.get('limit', 12000)))
            elif view == 'context':
                s = hub._managed_session(sid)
                hub._context_prefix(s)
                result = {**s.context_usage(), 'folded_count': len(s.folded_originals), 'messages': len(s.messages)}
            else:
                result = {'presets': hub.presets.list(), 'tree': hub.teams.tree(), 'config': hub.teams.configuration()}
        else:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('请求内容必须是JSON对象')
            action, sid = body.get('action'), str(body.get('id') or '')
            if action == 'preset_save':
                result = {'preset': hub.presets.save(body.get('preset'))}
            elif action == 'preset_delete':
                hub.presets.delete(body.get('preset_id'))
                result = {'presets': hub.presets.list()}
            elif action == 'team_config':
                result = {'config': hub.teams.configure(body.get('config', body))}
            elif action == 'delegate':
                s = hub._managed_session(sid)
                if s.status == 'idle':
                    s.stop_event.clear()
                result = {'children': await hub.teams.delegate(s, body.get('tasks'))}
            elif action == 'cancel':
                result = {'children': await hub.teams.cancel(sid, body.get('child_id'))}
            elif action == 'experience':  # 你对一条经验: approve (确认) / disable / enable / delete
                result = hub.experience.update(body.get('exp_id'), body.get('do'))
            elif action == 'feedback':
                result = hub.message_feedback(sid, body.get('message_id'), body.get('value'), body.get('note', ''))
            elif action == 'fork':
                result = {'session': (await hub.fork_session(sid, title=body.get('title'), message_id=body.get('message_id'))).view()}
            elif action == 'compact':
                s = hub._managed_session(sid)
                if s.status != 'idle':
                    raise ValueError('请等当前回复结束后压缩上下文')
                result = {'folded': hub._fold(s, force=True)}
                hub.save(s)
                result['context'] = s.context_usage()
            else:
                raise ValueError('未知的工作台运行操作')
        return web.json_response({'ok': True, **result})
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return web.json_response({'ok': False, 'error': str(exc)}, status=400)


