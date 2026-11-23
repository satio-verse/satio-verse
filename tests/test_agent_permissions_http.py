"""Shared permission APIs: real app origin checks, atomic revisions, AI add-only provenance."""
import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm.app import AGENT, create_app


def test_shared_authority_revision_and_ai_source(tmp_path):
    async def main():
        async with TestClient(TestServer(create_app(make_cfg(tmp_path)))) as client:
            hub = client.server.app[AGENT]
            initial = await (await client.get('/api/agent/permissions')).json()
            assert initial['global_unrestricted'] is False
            assert Path(initial['storage_path']) == hub.blacklist.path
            compact = await (await client.get('/api/agent/permissions?execution=1')).json()
            assert compact['audit'] == [] and compact['rules'] == initial['rules'] and compact['revision'] == initial['revision']
            revision = initial['revision']
            changed = await client.post('/api/agent/permissions', json={'action': 'global', 'enabled': True, 'expected_revision': revision})
            assert changed.status == 200
            current = await changed.json()
            assert current['global_unrestricted'] and current['revision'] == revision + 1
            stale = await client.post('/api/agent/permissions', json={'action': 'global', 'enabled': False, 'expected_revision': revision})
            assert stale.status == 409 and hub.blacklist.snapshot()['global_unrestricted'] is True
            rule = {'kind': 'instruction', 'value': '回答用中文', 'reason': '主人要求'}
            learned = await client.post('/api/agent/permissions/learn', json={'rule': rule, 'evidence': '以后回答用中文'})
            assert learned.status == 200
            assert (await learned.json())['rule']['source'] == 'assistant'
            forged = await client.post('/api/agent/permissions/learn', json={'rule': rule, 'action': 'global', 'enabled': False})
            assert forged.status == 400
            assert hub.blacklist.snapshot()['global_unrestricted'] is True
            disabled = await client.post('/api/agent/permissions/learn', json={'rule': {**rule, 'enabled': False}})
            assert disabled.status == 400
            sid = hub.new_session().id
            stale_rule = await client.post('/api/agent/studio', json={'id': sid, 'action': 'rule_add', 'rule': rule, 'expected_revision': revision})
            assert stale_rule.status == 409
            assert (await (await client.get('/api/agent/studio', params={'id': sid})).json())['authority']['global_unrestricted']
            for endpoint in ('/api/agent/permissions', '/api/agent/permissions/learn', '/api/agent/studio', '/api/agent/context'):
                denied = await client.post(endpoint, json={}, headers={'Origin': 'https://evil.invalid'})
                assert denied.status == 403
            for name in ('workbench-studio.js', 'workbench-studio.css'):
                assert (await client.get('/pet/' + name)).status == 200
    asyncio.run(main())


def test_command_checks_implicit_working_directory(tmp_path):
    async def main():
        async with TestClient(TestServer(create_app(make_cfg(tmp_path)))) as client:
            hub = client.server.app[AGENT]
            s = hub.new_session(mode='unrestricted')
            hub.blacklist.add({'kind': 'path', 'value': s.workspace, 'operations': 'write', 'reason': '目录禁止执行修改'})
            out, error = await hub._tool(s, {'id': 'test', 'function': {'name': 'run_command', 'arguments': json.dumps({'command': 'Write-Output harmless'})}}, hub._offered(s))
            assert error and '黑名单' in out
            assert s.events[-1]['type'] == 'tool_result' and s.events[-1]['ok'] is False
    asyncio.run(main())
