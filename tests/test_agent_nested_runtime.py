import asyncio
import json
import time

import pytest

from wm.agent.core import AgentHub, Session
from wm.agent.delegation import TeamManager
from wm.agent.runtime_http import runtime
from test_agent_delegation import Hub


def hub_at(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    return AgentHub({'agent': {'workspace': str(workspace)}}, home=tmp_path / 'agent')


def test_legacy_ids_feedback_and_provider_payload(tmp_path):
    hub = hub_at(tmp_path)
    s = hub.new_session()
    s.messages = [{'role': 'user', 'content': 'same'}, {'role': 'assistant', 'content': 'same'}, {'role': 'user', 'content': 'same'}, {'role': 'assistant', 'content': 'same'}]
    s.events = [{'type': m['role'], 'text': m['content'], 't': n + 100} for n, m in enumerate(s.messages)]
    hub.save(s)
    ids = [e['message_id'] for e in s.view()['events']]
    assert len(set(ids)) == 4
    assert [e['message_id'] for e in Session.from_json(s.to_json()).view()['events']] == ids
    assert all(not any(key.startswith('_') for key in m) for m in s.request_messages())
    hub.message_feedback(s.id, ids[1], 'good')
    assert s.view()['events'][1]['feedback']['value'] == 'good'
    hub.message_feedback(s.id, ids[1], 'bad', 'need sources')
    assert s.feedback[ids[1]]['value'] == 'issue'
    hub.message_feedback(s.id, ids[1], '')
    assert 'feedback' not in s.view()['events'][1]
    with pytest.raises(ValueError):
        hub.message_feedback(s.id, ids[0], 'good')


def test_branch_at_message_does_not_copy_later_text_or_files(tmp_path):
    async def main():
        hub = hub_at(tmp_path)
        s = hub.new_session()
        s.messages = [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'LATER-PRIVATE'}, {'role': 'assistant', 'content': 'LATER-ANSWER'}]
        s.events = [{'type': m['role'], 'text': m['content'], 't': n} for n, m in enumerate(s.messages)]
        file = hub.contexts.add_file(s.id, 'later.txt', b'LATER-FILE')
        s.materials = [{**file, 'owner': s.id}]
        hub.save(s)
        fork = await hub.fork_session(s.id, message_id=s.events[1]['message_id'])
        assert [m['content'] for m in fork.messages] == ['first', 'answer']
        assert not fork.materials and not hub.contexts.list(fork.id)
        assert 'LATER' not in json.dumps(fork.to_json())
        assert len(s.messages) == 4
    asyncio.run(main())


def test_compaction_keeps_original_and_tool_pairs(tmp_path):
    hub = hub_at(tmp_path)
    s = hub.new_session()
    original = 'ORIGINAL-EVIDENCE ' * 2000
    s.messages = [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]}, {'role': 'tool', 'tool_call_id': 'c', 'content': original}] + [{'role': 'user', 'content': str(i)} for i in range(13)]
    assert hub._fold(s, force=True) == 1
    mid = s.messages[1]['_id']
    assert 'history_read' in s.messages[1]['content']
    assert s.messages[0]['tool_calls'][0]['id'] == s.messages[1]['tool_call_id']
    assert hub.history_read(s.id, mid, offset=25000)['text'] == original[25000:37000]
    assert hub.history_search(s.id, 'ORIGINAL-EVIDENCE')['items'][0]['message_id'] == mid
    restored = Session.from_json(s.to_json())
    assert restored.folded_originals[mid] == original
    assert hub._fold(s, force=True) == 0


def test_custom_preset_really_filters_tools_and_prompt(tmp_path):
    hub = hub_at(tmp_path)
    s = hub.new_session()
    hub.presets.save({'id': 'my-review', 'name': '审查', 'instructions': 'REPORT-WITH-EVIDENCE', 'tools': ['read_file', 'history_search']})
    hub.settings(s.id, preset='my-review')
    assert hub._offered(s) == {'read_file', 'history_search'}
    assert 'REPORT-WITH-EVIDENCE' in hub._system(s)
    assert [t['function']['name'] for t in hub.studio.tools(s)] == ['history_search']
    assert hub.state()['presets']['my-review'] == '审查'
    with pytest.raises(ValueError):
        hub.presets.save({'id': 'standard', 'name': 'overwrite'})
    hub.presets.delete('my-review')
    assert hub.presets.get('my-review')['id'] == 'standard'


def test_nested_wait_releases_single_slot_and_aggregates_tree_cost(tmp_path):
    class NestedHub(Hub):
        async def send(self, sid, text, preserve_materials=False):
            if 'orchestrate' not in text:
                return await super().send(sid, text, preserve_materials)
            s = self.sessions[sid]
            s.status = 'running'
            s.run = {'started': time.time(), 'cost_start': 0}
            async def run():
                rows = await team.delegate(s, [{'goal': 'leaf'}])
                result = await team.wait(s.id, timeout_s=3)
                assert result['complete']
                s.events.append({'type': 'assistant', 'text': 'merged leaf'})
                s.stats['cost'] = .1
                s.status = 'idle'
            s.task = asyncio.create_task(run())
            return None
    async def main():
        nonlocal team
        hub = NestedHub(tmp_path / 'agent')
        parent = Session('parent', str(tmp_path))
        hub.sessions['parent'] = parent
        team = TeamManager(hub, max_parallel=1, max_depth=3)
        await team.delegate(parent, [{'goal': 'orchestrate'}])
        result = await team.wait(parent.id, timeout_s=5)
        assert result['complete'] and result['children'][0]['status'] == 'completed'
        assert len(team.tree()[0]['children']) == 1
        assert team.spent(parent.id) == .3
        assert not team._slot_owners
        team.configure({'max_parallel': 2, 'max_depth': 4})
        assert TeamManager(hub).configuration()['max_depth'] == 4
    team = None
    asyncio.run(main())


def test_explicit_shared_workspace_and_unlimited_steps(tmp_path):
    async def main():
        hub = Hub(tmp_path / 'agent')
        parent = Session('parent', str(tmp_path), mode='unrestricted')
        parent.max_steps = 0
        hub.sessions[parent.id] = parent
        team = TeamManager(hub)
        rows = await team.delegate(parent, [{'goal': 'shared edits', 'workspace_mode': 'shared'}])
        child = hub.sessions[rows[0]['id']]
        assert child.mode == 'unrestricted' and child.max_steps == 0
        assert rows[0]['workspace_mode'] == 'shared' and not rows[0]['worktree']
        await team.wait(parent.id, timeout_s=2)
        assert '上下文独立不代表文件隔离' in hub.sent[0][1]
    asyncio.run(main())


def test_plan_cannot_delegate_write_and_stop_cascades(tmp_path):
    async def main():
        hub = Hub(tmp_path / 'agent')
        parent = Session('parent', str(tmp_path), mode='unrestricted', preset='plan')
        hub.sessions[parent.id] = parent
        team = TeamManager(hub, max_parallel=2, max_depth=3)
        with pytest.raises(ValueError, match='高于'):
            await team.delegate(parent, [{'goal': 'edit', 'workspace_mode': 'shared', 'mode': 'unrestricted'}])
        rows = await team.delegate(parent, [{'goal': 'STALL parent'}])
        child = hub.sessions[rows[0]['id']]
        grandchildren = await team.delegate(child, [{'goal': 'STALL leaf'}])
        await asyncio.sleep(.05)
        await team.cancel(parent.id)
        assert all(row['status'] == 'cancelled' for row in team.list())
        assert not team._slot_owners and hub.running == 0
    asyncio.run(main())


def test_runtime_http_endpoints_and_validation(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    async def main():
        hub = hub_at(tmp_path)
        s = hub.new_session()
        s.messages = [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}]
        s.events = [{'type': m['role'], 'text': m['content'], 't': n} for n, m in enumerate(s.messages)]
        hub.save(s)
        app = web.Application()
        async def route(request):
            return await runtime(request, hub)
        app.router.add_route('*', '/runtime', route)
        async with TestClient(TestServer(app)) as client:
            for view in ('overview', 'presets', 'tree', 'tools', 'context', 'history'):
                assert (await client.get(f'/runtime?view={view}&id={s.id}')).status == 200
            tools = await (await client.get('/runtime?view=tools')).json()
            assert any(x['name'] == 'history_read' for x in tools['tools'])
            feedback = await client.post('/runtime', json={'action': 'feedback', 'id': s.id, 'message_id': s.events[1]['message_id'], 'value': 'bad'})
            assert feedback.status == 200
            assert (await feedback.json())['feedback']['value'] == 'issue'
            fork = await client.post('/runtime', json={'action': 'fork', 'id': s.id, 'message_id': s.events[0]['message_id']})
            assert fork.status == 200
            assert len((await fork.json())['session']['events']) == 2  # selected user + branch note
            assert (await client.post('/runtime', json={'action': 'team_config', 'config': {'max_parallel': 0}})).status == 400
            assert (await client.get('/runtime?view=history&id=../escape')).status == 400
            assert (await client.post('/runtime', json=[])).status == 400
        await hub.teams.close()
    asyncio.run(main())


def test_command_projection_preserves_middle_for_history(tmp_path):
    import copy
    from wm.agent import tools as T
    async def main():
        output = await T.run_command("Write-Output ('A' * 12000 + 'MIDDLE-EVIDENCE' + 'Z' * 30000)", tmp_path)
        assert 'MIDDLE-EVIDENCE' not in str(output)
        assert 'MIDDLE-EVIDENCE' in output.original
        assert output.startswith('退出码 0')
        assert copy.deepcopy(output).original == output.original
    asyncio.run(main())


def test_extension_projection_keeps_complete_structured_result(tmp_path, monkeypatch):
    async def main():
        hub = hub_at(tmp_path)
        s = hub.new_session(mode='unrestricted')
        original = {'large': 'HEADER ' * 9000, 'proof': 'END-EVIDENCE'}
        async def call(_s, _name, _args):
            return original
        monkeypatch.setattr(hub.studio, 'call', call)
        out, error = await hub.studio.execute(s, 'c', 'context_read', {'id': 'example'})
        assert not error and len(str(out)) <= 30000
        assert json.loads(out.original) == original
    asyncio.run(main())
