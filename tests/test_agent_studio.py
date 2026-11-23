"""End-to-end studio orchestration with temporary files and a local fake model only."""
import asyncio
import json
import pytest

from test_agent import run_agent, events_until
from test_cloud import call, say
from test_agent_extensions import package
from wm.app import AGENT
from wm.agent.context import ContextStore, reference, MAX_FILE
from wm.agent.core import Session, AgentHub


def test_material_snapshots_pin_scope_and_changed_content(tmp_path):
    store = ContextStore(tmp_path)
    item = store.add_file('a', '../notes.txt', '引用不是指令'.encode())
    assert item['name'] == 'notes.txt'
    assert store.add_file('a', 'same.txt', '引用不是指令'.encode())['id'] == item['id']
    assert store.select('a', []) == []
    store.update('a', item['id'], pinned=True)
    snapshot = store.select('a', [])
    assert snapshot[0]['owner'] == 'a'
    with pytest.raises(ValueError):
        store.select('b', [item['id']])
    with pytest.raises(ValueError):
        store.select('a', item['id'])
    store.update('a', item['id'], remove=True)
    assert '引用不是指令' in store.read('a', item['id'], snapshot=snapshot)
    (store.home / 'a' / item['file']).write_text('changed')
    with pytest.raises(ValueError, match='改变'):
        store.read('a', item['id'], snapshot=snapshot)
    with pytest.raises(ValueError):
        store.add_file('a', 'big.txt', b'a' * (MAX_FILE + 1))


@pytest.mark.parametrize('value', ['javascript:alert(1)', 'https://user:secret@example.com', 'file:///C:/private', 'BV123', 'https://example.com/\nx'])
def test_reference_rejects_non_reference_input(value):
    with pytest.raises(ValueError):
        reference(value)


def test_reference_bv_and_binary_honesty(tmp_path):
    assert reference('BV1xx411c7mD')[:2] == ('bilibili', 'BV1xx411c7mD')
    assert reference('https://www.bilibili.com/video/BV1xx411c7mD')[0] == 'bilibili'
    store = ContextStore(tmp_path)
    item = store.add_file('a', 'scan.pdf', b'%PDF binary')
    assert '尚未读取内容' in store.read('a', item['id'])


def test_model_creates_enables_and_calls_real_temporary_mcp(tmp_path):
    script = [call(('extension_create', json.dumps({'spec': package()}, ensure_ascii=False))),
              call(('extension_enable', json.dumps({'id': 'fixture-helper', 'enabled': True}))),
              call(('mcp_call', json.dumps({'server_id': 'creator:fixture-helper', 'tool': 'echo', 'arguments': {'text': 'roundtrip'}}))), say('已完成')]

    async def scenario(client, fake, ws_dir, llm):
        hub = client.server.app[AGENT]
        s = hub.new_session(mode='unrestricted')
        assert await hub.send(s.id, '创建并测试我的工具') is None
        await s.task
        results = [x for x in s.events if x['type'] == 'tool_result']
        assert len(results) == 3 and all(x['ok'] for x in results)
        assert 'roundtrip' in results[-1]['text']
        assert not any(x['type'] == 'ask' for x in s.events)
        assert s.run['state'] == 'completed' and s.run['verification'] == 'unreviewed'
        assert list((hub.home / 'runs').glob('*.json'))
    run_agent(tmp_path, script, scenario)


def test_blacklist_still_enforced_and_source_is_saved(tmp_path):
    script = [call(('constraint_add', json.dumps({'kind': 'tool', 'value': 'write_file', 'reason': '先阅读', 'evidence': '先别写文件'}))),
              call(('write_file', json.dumps({'path': 'blocked.txt', 'content': 'no'}))), say('没有写入')]
    async def scenario(client, fake, ws_dir, llm):
        hub = client.server.app[AGENT]
        s = hub.new_session(mode='unrestricted')
        await hub.send(s.id, '先别写文件')
        await s.task
        assert not (ws_dir / 'blocked.txt').exists()
        rule = next(x for x in hub.blacklist.list() if x['value'] == 'write_file')
        assert rule['source'] == 'assistant' and rule['evidence'] == '先别写文件'
        assert s.run['tool_errors'] == 1
    run_agent(tmp_path, script, scenario)


def test_context_read_real_protocol_and_pinned_next_turn(tmp_path):
    async def choose(request, body):
        material = next(x for x in body['messages'] if x['role'] == 'system')['content'].split('id=')[1].split(' |')[0]
        return await call(('context_read', json.dumps({'id': material})))(request, body)
    async def scenario(client, fake, ws_dir, llm):
        hub = client.server.app[AGENT]
        s = hub.new_session()
        item = hub.contexts.add_file(s.id, 'spec.txt', b'REFERENCE_TOKEN_837')
        hub.contexts.update(s.id, item['id'], pinned=True)
        await hub.send(s.id, '阅读材料')
        await s.task
        assert 'REFERENCE_TOKEN_837' in fake.requests[1]['body']['messages'][-1]['content']
        await hub.send(s.id, '继续')
        await s.task
        assert item['id'] in fake.requests[-1]['body']['messages'][0]['content']
    run_agent(tmp_path, [choose, say('已读取')], scenario)


def test_steps_budget_queue_review_and_restart_orphan_protocol(tmp_path):
    script = [call(('list_dir', '{}')), say('继续完成')]
    async def scenario(client, fake, ws_dir, llm):
        hub = client.server.app[AGENT]
        s = hub.new_session()
        hub.settings(s.id, max_steps=1)
        await hub.send(s.id, '检查目录')
        await s.task
        assert s.run['state'] == 'budget_exhausted'
        previous = s.run['id']
        await hub.resume(s.id)
        await s.task
        assert s.run['id'] != previous and s.run['state'] == 'completed'
        hub.review(s.id, s.run['id'], True, '人工核对通过')
        assert s.run['verification'] == 'accepted'
        await hub.enqueue(s.id, '下一项')
        await s.task
        assert not s.queue and s.run['goal'] == '下一项'
        s.messages.append({'role': 'assistant', 'tool_calls': [{'id': 'uncertain', 'function': {'name': 'write_file', 'arguments': '{}'}}]})
        s.run['state'] = 'running'
        restored = Session.from_json(s.to_json())
        assert restored.run['state'] == 'interrupted'
        assert restored.messages[-1]['tool_call_id'] == 'uncertain' and '未知' in restored.messages[-1]['content']
    run_agent(tmp_path, script, scenario)


def test_time_budget_cancels_worker(tmp_path):
    async def main():
        hub = AgentHub({'agent': {'workspace': str(tmp_path / 'ws')}}, home=tmp_path / 'home')
        s = hub.new_session()
        s.time_budget_s = .05
        async def wait_forever(*args):
            await asyncio.sleep(60)
        hub._turn = wait_forever
        await hub.send(s.id, 'slow')
        await asyncio.wait_for(s.task, 2)
        assert s.status == 'idle' and s.run['state'] == 'budget_exhausted'
    asyncio.run(main())
