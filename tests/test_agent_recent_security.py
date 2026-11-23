"""Regression cases for attachment previews and direct-extension dispatch.

Everything runs in temporary stores; no real extension processes are launched.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.context import ContextStore
from wm.agent.studio import StudioRuntime
from wm.agent.studio_http import context_image
from wm.agent import policy as P
from test_agent_direct_tools import build, Session


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jY1kAAAAASUVORK5CYII=')


async def preview(tmp_path, name, data, mutate=None):
    contexts = ContextStore(tmp_path)
    item = contexts.add_file('main', name, data)
    if mutate:
        mutate(contexts, item)
    hub = SimpleNamespace(contexts=contexts, get=lambda sid: SimpleNamespace(id='main') if sid == 'main' else None)
    app = web.Application()
    async def handle(request):
        return await context_image(request, hub)
    app.router.add_get('/image', handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/image', params={'id': 'main', 'item': item['id']})
        return response.status, dict(response.headers), await response.read()


@pytest.mark.parametrize('name,data', [
    ('page.html', b'<script>window.__previewExecuted=true</script>'),
    ('picture.svg', b'<svg xmlns="http://www.w3.org/2000/svg" onload="window.__previewExecuted=true"/>'),
    ('fake.png', b'<script>window.__previewExecuted=true</script>'),
])
def test_preview_rejects_active_documents_and_disguised_images(tmp_path, name, data):
    status, _, body = asyncio.run(preview(tmp_path, name, data))
    assert status == 400
    assert b'<script>' not in body


def test_raster_preview_has_opaque_sandbox_and_no_sniff(tmp_path):
    status, headers, body = asyncio.run(preview(tmp_path, 'tiny.png', PNG))
    assert status == 200 and body == PNG
    assert headers['Content-Type'] == 'image/png'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert 'sandbox' in headers['Content-Security-Policy']
    assert 'allow-same-origin' not in headers['Content-Security-Policy']
    assert headers['Cache-Control'] == 'no-store'


def test_preview_rechecks_immutable_hash(tmp_path):
    def mutate(store, item):
        (store._folder('main') / item['file']).write_bytes(PNG + b'changed')
    assert asyncio.run(preview(tmp_path, 'tiny.png', PNG, mutate))[0] == 400


def test_preview_rejects_index_path_escape(tmp_path):
    sentinel = tmp_path / 'outside.png'
    sentinel.write_bytes(PNG)
    def mutate(store, item):
        item['file'] = str(sentinel)
        store._save('main', [item])
    assert asyncio.run(preview(tmp_path, 'tiny.png', PNG, mutate))[0] == 400


@pytest.mark.parametrize('blocked', ['mcp_call', 'mcp_connect'])
def test_direct_dispatch_honors_generic_mcp_rules(tmp_path, blocked):
    hub = build(tmp_path)
    calls = []
    def check(name, args, workspace):
        calls.append((name, args))
        return P.Decision('deny', 'user rule') if name == blocked else None
    hub.blacklist.check = check
    hub.extensions = SimpleNamespace(server=lambda sid: {'command': 'python', 'args': ['server.py'], 'cwd': str(tmp_path)})
    decision = StudioRuntime(hub).decision(Session(), 'demo_read', {})
    assert decision.how == 'deny'
    assert any(name == blocked for name, _ in calls)


def test_direct_dispatch_honors_server_launch_command_rules(tmp_path):
    hub = build(tmp_path)
    hub.blacklist.check = lambda name, args, _: P.Decision('deny', 'blocked launcher') if args.get('command') == 'python server.py' else None
    hub.extensions = SimpleNamespace(server=lambda sid: {'command': 'python', 'args': ['server.py'], 'cwd': str(tmp_path)})
    assert StudioRuntime(hub).decision(Session(), 'demo_read', {}).how == 'deny'


def test_direct_tools_cannot_shadow_builtins(tmp_path):
    tools = [{'name': name, 'inputSchema': {'type': 'object'}, 'read_only': True}
             for name in ['read_file', 'constraint_add', 'life_settings']]
    studio = StudioRuntime(build(tmp_path, tools=tools))
    assert not studio.direct.names()


def test_ambiguous_direct_names_are_not_exposed(tmp_path):
    hub = build(tmp_path)
    path = tmp_path / 'extensions' / 'direct-tools.json'
    data = json.loads(path.read_text('utf-8'))
    data['servers']['creator:demo']['tools'].append(data['servers']['creator:demo']['tools'][0])
    path.write_text(json.dumps(data), encoding='utf-8')
    assert 'demo_read' not in StudioRuntime(hub).direct.names()


def test_snapshot_requires_a_real_active_revision(tmp_path):
    studio = StudioRuntime(build(tmp_path, revision=None, active=None))
    assert not studio.direct.names()
