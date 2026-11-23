import asyncio
import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wm.agent.core import AgentHub
from wm.agent.capabilities import inventory, tool_catalog
from wm.agent.prompt_context import workspace_instruction, workspace_rules, RULE_BYTES
from wm.agent.runtime_http import runtime


def hub_at(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    hub = AgentHub({'agent': {'workspace': str(root), 'extensions': {'settings_path': False}}}, home=tmp_path / 'agent')
    return hub, hub.new_session(), root


def direct_tool(hub):
    root = hub.home / 'extensions'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'registry.json').write_text(json.dumps({'packages': {'clock': {'enabled': True, 'active_revision': 'r1'}}}))
    (root / 'direct-tools.json').write_text(json.dumps({'servers': {'creator:clock': {
        'revision': 'r1', 'label': 'Clock test', 'tools': [{'name': 'clock_now', 'read_only': True,
        'description': 'Current time', 'inputSchema': {'type': 'object'}}]}}}))


def test_catalog_includes_actual_direct_extension_and_preset(tmp_path):
    hub, s, _ = hub_at(tmp_path)
    direct_tool(hub)
    rows = {row['name']: row for row in tool_catalog(hub, s)}
    assert rows['clock_now']['source'] == 'extension'
    assert rows['clock_now']['enabled']
    hub.presets.save({'id': 'read-only', 'name': 'Read', 'tools': ['read_file']})
    result = inventory(hub, s, 'read-only')
    assert result['preview'] and s.preset == 'standard'
    assert [row['name'] for row in result['tools'] if row['enabled']] == ['read_file']
    assert next(group for group in result['groups'] if group['id'] == 'time-context')['status'] == 'inactive'


def test_inventory_does_not_claim_missing_compatibility(tmp_path):
    hub, s, _ = hub_at(tmp_path)
    groups = {row['id']: row for row in inventory(hub, s)['groups']}
    assert groups['workflow-ptc']['status'] == 'missing'
    assert groups['cordis']['status'] == 'missing'
    assert '不是' in groups['tool-pwsh']['detail']
    assert groups['tool-pwsh']['tools'][0]['name'] == 'run_command'
    with pytest.raises(ValueError):
        inventory(hub, s, 'does-not-exist')


def test_inventory_ignores_stale_direct_snapshot(tmp_path):
    hub, s, _ = hub_at(tmp_path)
    direct_tool(hub)
    path = hub.home / 'extensions' / 'registry.json'
    path.write_text(json.dumps({'packages': {'clock': {'enabled': True, 'active_revision': 'r2'}}}))
    assert 'clock_now' not in {row['name'] for row in tool_catalog(hub, s)}


def test_rules_bounded_root_only_and_visible_in_prompt(tmp_path):
    hub, s, root = hub_at(tmp_path)
    (tmp_path / 'AGENTS.md').write_text('PARENT-SHOULD-NOT-LOAD')
    (root / 'AGENTS.md').write_text('PROJECT-RULE ' + 'a' * RULE_BYTES)
    (root / 'CLAUDE.md').write_text('CLAUDE-RULE')
    rows = workspace_rules(hub, s)
    assert len(rows) == 2 and rows[0]['truncated']
    assert len(rows[0]['text'].encode()) == RULE_BYTES
    prompt = hub._system(s)
    assert 'PROJECT-RULE' in prompt and 'CLAUDE-RULE' in prompt
    assert 'PARENT-SHOULD-NOT-LOAD' not in prompt and '不得更改用户目标' in prompt


def test_rules_respect_user_blacklist_even_unrestricted(tmp_path):
    hub, s, root = hub_at(tmp_path)
    s.mode = 'unrestricted'
    path = root / 'AGENTS.md'
    path.write_text('SECRET-RULE')
    hub.blacklist.add({'kind': 'path', 'value': str(path), 'reason': 'test', 'operations': 'read'})
    assert workspace_rules(hub, s)[0]['state'] == 'blocked'
    assert 'SECRET-RULE' not in workspace_instruction(hub, s)


def test_rules_do_not_follow_workspace_symlink_escape(tmp_path):
    hub, s, root = hub_at(tmp_path)
    outside = tmp_path / 'outside.md'
    outside.write_text('OUTSIDE-RULE')
    try:
        (root / 'AGENTS.md').symlink_to(outside)
    except OSError:
        pytest.skip('Windows user cannot create symlinks')
    assert not workspace_rules(hub, s)


def test_runtime_capability_endpoint(tmp_path):
    async def main():
        hub, s, _ = hub_at(tmp_path)
        direct_tool(hub)
        app = web.Application()
        async def route(request):
            return await runtime(request, hub)
        app.router.add_route('*', '/runtime', route)
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/runtime', params={'view': 'capabilities', 'id': s.id})
            data = await response.json()
            assert response.status == 200 and data['ok']
            assert {'session', 'global', 'planned'} == {row['scope'] for row in data['groups']}
            response = await client.get('/runtime', params={'view': 'tools', 'id': s.id})
            assert 'clock_now' in {row['name'] for row in (await response.json())['tools']}
            assert (await client.get('/runtime?view=capabilities&id=../escape')).status == 400
            assert (await client.get('/runtime?view=capabilities&preset=standard')).status == 400
    asyncio.run(main())
