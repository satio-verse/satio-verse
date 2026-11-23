"""Trusted local UI transport. Caller must enforce app Host/Origin checks."""
import asyncio
import json
from aiohttp import web, ClientError
from .config import ROOT
from .platform_services import ContributionStore, CommunityStore, ReleaseUpdater, identifier


def services(hub):
    if not hasattr(hub, '_platform_services'):
        def busy():
            sessions = any(s.status != 'idle' or (s.task and not s.task.done()) for s in hub.sessions.values())
            jobs = any(row.get('state') == 'running' for row in getattr(getattr(hub, 'workspace_jobs', None), 'records', {}).values())
            children = any(row.get('status') in {'queued', 'running', 'waiting', 'cancelling'}
                           for row in getattr(getattr(hub, 'teams', None), '_records', {}).values())
            return sessions or jobs or children
        hub._platform_services = (ContributionStore(hub.home), CommunityStore(hub.home, hub.extensions),
                                  ReleaseUpdater(hub.home, ROOT, busy), asyncio.Lock())
    return hub._platform_services


async def platform(request, hub):
    data, community, updates, lock = services(hub)
    def state():
        return {'data': data.state(), 'community': community.state(), 'updates': updates.state()}
    if request.method == 'GET':
        return web.json_response(state(), headers={'Cache-Control': 'no-store'})
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('需要 JSON 对象')
        action = body.get('action')
        async with lock:
            result = None
            if action == 'data_settings':
                result = data.configure(body)
            elif action in ('data_preview', 'data_queue'):
                session = hub.get(identifier(body.get('session', '')))
                if session is None:
                    raise ValueError('没有这个会话')
                preview = data.preview(session, body.get('kind'), body.get('event_id'))
                result = preview if action == 'data_preview' else data.queue(preview, body.get('sha256'))
            elif action == 'data_withdraw':
                result = data.withdraw(body.get('id'))
            elif action == 'data_send':
                result = await data.send(body.get('id'))
                result = {k: v for k, v in result.items() if k not in ('payload', 'receipt', 'upload_secret')}
            elif action == 'data_delete':
                result = await data.delete_remote(body.get('id'))
            elif action == 'community_export':
                result = community.export(body.get('id'), body.get('author'), body.get('version'), body.get('revision'))
            elif action == 'community_source':
                result = community.add_source(body.get('source'))
            elif action == 'community_refresh':
                result = await community.refresh(body.get('source_id'))
            elif action == 'community_inspect':
                result = await community.inspect(body.get('source_id'), body.get('id'), body.get('version'))
            elif action == 'community_install':
                result = await community.install(body.get('source_id'), body.get('id'), body.get('version'))
            elif action == 'community_publish':
                result = await community.publish(body.get('id'), body.get('version'), body.get('endpoint'), body.get('token'))
            elif action == 'update_trust':
                updates.trust(body.get('source', ''), body.get('sha256', ''), body.get('public_key', ''))
                result = updates.state()
            elif action in ('update_check', 'update_stage'):
                result = await updates.stage_source()
            elif action == 'update_apply':
                result = updates.apply()
            elif action == 'update_rollback':
                result = updates.rollback()
            else:
                raise ValueError('未知的平台操作')
            return web.json_response({'ok': True, 'result': result, **state()}, headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError, KeyError, OSError, ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
        return web.json_response({'error': str(exc)[:300]}, status=400, headers={'Cache-Control': 'no-store'})
