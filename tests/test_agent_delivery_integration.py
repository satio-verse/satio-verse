"""Offline Hub delivery/recovery wiring with real temporary file/command effects."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from test_agent_studio_lifecycle import reply, setup_hub
from wm.agent.core import Session


def _protocol_complete(messages):
    for index, message in enumerate(messages):
        calls = message.get('tool_calls') if message.get('role') == 'assistant' else None
        if not calls:
            continue
        found, at = [], index + 1
        while at < len(messages) and messages[at].get('role') == 'tool':
            found.append(messages[at]['tool_call_id'])
            at += 1
        assert sorted(found) == sorted(call['id'] for call in calls), 'Interrupted calls require exactly one explicit unknown-result message'


def test_same_process_stop_repairs_protocol_resume_keeps_plan_and_new_task_resets_it(tmp_path, monkeypatch):
    async def main():
        started = asyncio.Event()
        plan = {'steps': [{'title': '核对已有产物再继续', 'status': 'doing'}], 'acceptance': '有可复核证据'}
        async def handler(session, number, messages):
            if number == 1:
                return reply('', ('task_plan', plan)), 0
            _protocol_complete(messages)
            if number == 2:
                assert session.plan == plan
                assert any('结果未知' in str(m.get('content', '')) for m in messages if m.get('role') == 'tool')
                return reply('检查了中断点，未重复未知操作'), 0
            assert session.plan == {}
            return reply('新任务从新的计划开始'), 0
        hub, session, models = setup_hub(tmp_path, monkeypatch, handler)
        original = hub.studio.call
        invoked = []
        async def interrupted(s, name, args):
            if name == 'task_plan':
                invoked.append(name)
                await original(s, name, args)
                started.set()
                await asyncio.Event().wait()
            return await original(s, name, args)
        monkeypatch.setattr(hub.studio, 'call', interrupted)
        await hub.send(session.id, '执行需要中断的计划')
        await asyncio.wait_for(started.wait(), 2)
        first_run = session.run['id']
        await hub.stop(session.id)
        await asyncio.wait_for(session.task, 6)
        assert session.run['state'] == 'cancelled'
        _protocol_complete(session.messages)
        assert invoked == ['task_plan']
        assert await hub.resume(session.id) is None
        await asyncio.wait_for(session.task, 3)
        assert session.run['resume_from_run'] == first_run and session.plan == plan
        assert hub.run_record(session.id, first_run)['plan'] == plan
        assert invoked == ['task_plan'], 'Resume must not automatically replay the interrupted tool'
        await hub.send(session.id, '一个独立的新任务')
        await asyncio.wait_for(session.task, 3)
        assert session.plan == {} and session.run['plan'] == {}
        assert len(models[session.id].requests) == 3
        await hub.teams.close()
    asyncio.run(main())


def test_recovery_reindexes_backgrounds_without_duplicating_completed_tool_results(tmp_path, monkeypatch):
    async def main():
        async def handler(*_):
            return reply('已核对状态'), 0
        hub, session, _ = setup_hub(tmp_path, monkeypatch, handler)
        session.messages = [
            {'role': 'assistant', 'tool_calls': [{'id': 'done'}, {'id': 'unknown'}]},
            {'role': 'tool', 'tool_call_id': 'done', 'content': 'already recorded'},
            {'role': 'user', 'content': 'old user material'}]
        session.backgrounds = {2: 'background stays with user'}
        assert session.repair_pending_calls() == 1
        assert session.backgrounds == {3: 'background stays with user'}
        assert session.repair_pending_calls() == 0
        _protocol_complete(session.messages)
        assert session.messages[1]['tool_call_id'] == 'done'
        assert session.messages[2]['tool_call_id'] == 'unknown'
        await hub.teams.close()
    asyncio.run(main())


def test_auto_write_and_edit_register_real_file_versions_without_self_acceptance(tmp_path, monkeypatch):
    async def main():
        async def handler(_s, number, _messages):
            if number == 1:
                return reply('', ('write_file', {'path': 'result.md', 'content': 'first evidence'})), 0
            if number == 2:
                return reply('', ('edit_file', {'path': 'result.md', 'old': 'first', 'new': 'verified'})), 0
            return reply('交付 result.md；请用户核对结果'), 0
        hub, session, _ = setup_hub(tmp_path, monkeypatch, handler)
        await hub.send(session.id, '输出并修订成果')
        await asyncio.wait_for(session.task, 4)
        assert (Path(session.workspace) / 'result.md').read_text() == 'verified evidence'
        state = hub.deliveries.state(session.id, session.run['id'], refresh=True)
        assert len(state['artifacts']) == 1
        artifact = state['artifacts'][0]
        assert artifact['registered']['sha256'] == hashlib.sha256(b'verified evidence').hexdigest()
        assert [v['sha256'] for v in artifact['versions']] == [hashlib.sha256(b'first evidence').hexdigest()]
        assert artifact['status'] == 'current'
        events = [e for e in session.events if e['type'] == 'artifact']
        assert len(events) == 2 and {e['artifact']['id'] for e in events} == {artifact['id']}
        assert state['reviews'] == [] and session.run['verification'] == 'unreviewed'
        assert hub.run_record(session.id)['summary'] == '交付 result.md；请用户核对结果'
        await hub.teams.close()
    asyncio.run(main())


@pytest.mark.parametrize('partial', [False, True])
def test_model_stream_without_complete_result_fails_and_does_not_consume_queue(tmp_path, monkeypatch, partial):
    from test_agent_studio_lifecycle import FakeCloud

    async def unfinished_stream(self, messages, tools, **kwargs):
        self.requests.append(deepcopy({'messages': messages, 'tools': tools}))
        if partial:
            yield 'text', '这是一段尚未完成的输出'

    async def main():
        hub, session, models = setup_hub(tmp_path, monkeypatch, None)
        monkeypatch.setattr(FakeCloud, 'chat', unfinished_stream)
        session.queue = [{'id': 'next', 'text': '尚未执行的下一项任务', 'materials': [], 'created': 0}]
        assert await hub.send(session.id, '模型连接提前结束') is None
        await asyncio.wait_for(session.task, 4)
        assert session.status == 'idle' and session.run['state'] == 'failed'
        assert session.run['summary'] == '' and session.run['verification'] == 'unreviewed'
        assert len(session.queue) == 1 and len(models[session.id].requests) == 1
        assert '未收到完整回复' in session.run['reason']
        assert any(e['type'] == 'error' for e in session.events)
        assert hub.run_record(session.id)['state'] == 'failed'
        await hub.teams.close()
    asyncio.run(main())


def _python_command(filename):
    executable = sys.executable.replace("'", "''")
    return f"& '{executable}' '{filename}'; exit $LASTEXITCODE"


@pytest.mark.skipif(not shutil.which('powershell'), reason='The workbench command runner requires PowerShell')
@pytest.mark.parametrize('exit_code', [0, 7])
def test_real_command_exit_is_recorded_as_execution_evidence_not_acceptance(tmp_path, monkeypatch, exit_code):
    async def main():
        command = _python_command('local-check.py')
        async def handler(_s, number, _messages):
            if number == 1:
                return reply('', ('run_command', {'command': command, 'description': '运行临时本地检查'})), 0
            return reply('命令已结束，仍待用户验收'), 0
        hub, session, _ = setup_hub(tmp_path, monkeypatch, handler)
        session.mode = 'unrestricted'
        (Path(session.workspace) / 'local-check.py').write_text(f"import sys\nprint('EXECUTION_FIXTURE')\nsys.exit({exit_code})\n")
        await hub.send(session.id, '运行固定检查')
        await asyncio.wait_for(session.task, 12)
        state = hub.deliveries.state(session.id, session.run['id'])
        assert len(state['evidence']) == 1
        evidence = state['evidence'][0]
        assert evidence['exit_code'] == exit_code and evidence['source'] == 'tool_result'
        assert evidence['status'] == ('succeeded' if exit_code == 0 else 'failed')
        assert 'EXECUTION_FIXTURE' in evidence['output']
        assert evidence['scope'] == 'command_execution_only'
        assert not state['reviews'] and session.run['verification'] == 'unreviewed'
        _protocol_complete(session.messages)
        await hub.teams.close()
    asyncio.run(main())


@pytest.mark.skipif(not shutil.which('powershell'), reason='The workbench command runner requires PowerShell')
@pytest.mark.parametrize('how', ['stop', 'cancel'])
def test_running_command_stop_or_task_cancel_preserves_incomplete_evidence(tmp_path, monkeypatch, how):
    async def main():
        async def handler(_s, number, _messages):
            assert number == 1, 'Stopped runs must not request another model response'
            return reply('', ('run_command', {'command': _python_command('wait-check.py'), 'description': '等待可停止的临时检查'})), 0
        hub, session, _ = setup_hub(tmp_path, monkeypatch, handler)
        session.mode = 'unrestricted'
        workspace = Path(session.workspace)
        (workspace / 'wait-check.py').write_text("from pathlib import Path\nimport time\nPath('ready').write_text('started')\nprint('RUNNING_FIXTURE',flush=True)\ntime.sleep(60)\nPath('should-not-finish').write_text('finished')\n")
        await hub.send(session.id, '运行后停止')
        try:
            async def ready():
                while not (workspace / 'ready').exists():
                    await asyncio.sleep(.03)
            await asyncio.wait_for(ready(), 6)
            if how == 'stop':
                await hub.stop(session.id)
                await asyncio.wait_for(session.task, 12)
            else:
                session.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(session.task, 12)
            state = hub.deliveries.state(session.id, session.run['id'])
            assert len(state['evidence']) == 1
            assert state['evidence'][0]['exit_code'] is None and state['evidence'][0]['status'] == 'incomplete'
            assert session.run['state'] == 'cancelled' and not (workspace / 'should-not-finish').exists()
            assert session.run['verification'] == 'unreviewed'
            _protocol_complete(session.messages)
        finally:
            if not session.task.done():
                await hub.stop(session.id)
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            await hub.teams.close()
    asyncio.run(main())


def test_historical_review_updates_its_run_and_requires_current_preview_hashes(tmp_path, monkeypatch):
    async def main():
        async def handler(_s, number, _messages):
            if number == 1:
                return reply('', ('write_file', {'path': 'deliver.txt', 'content': 'version one'})), 0
            return reply(f'第 {number} 个回复'), 0
        hub, session, _ = setup_hub(tmp_path, monkeypatch, handler)
        await hub.send(session.id, '历史交付')
        await asyncio.wait_for(session.task, 3)
        old_id = session.run['id']
        artifact = hub.deliveries.list(session.id, old_id)['items'][0]
        expected = {artifact['id']: artifact['registered']['sha256']}
        await hub.send(session.id, '新一项工作')
        await asyncio.wait_for(session.task, 3)
        current = deepcopy(session.run)
        with pytest.raises(ValueError):
            hub.review(session.id, old_id, True)
        (Path(session.workspace) / 'deliver.txt').write_text('version two')
        with pytest.raises(ValueError, match='变化'):
            hub.review(session.id, old_id, True, artifact_versions=expected)
        assert hub.run_record(session.id, old_id)['verification'] == 'unreviewed'
        preview = hub.deliveries.read(session.id, artifact['id'], run_id=old_id)
        accepted = hub.review(session.id, old_id, True, '核对当前版本', artifact_versions={artifact['id']: preview['current']['sha256']})
        assert accepted['verification'] == 'accepted' and session.run == current
        assert accepted['delivery_review']['source'] == 'user'
        assert accepted['delivery_review']['artifact_snapshots'][0]['sha256'] == hashlib.sha256(b'version two').hexdigest()
        (Path(session.workspace) / 'deliver.txt').write_text('version three')
        assert hub.deliveries.state(session.id, old_id, refresh=True)['reviews'][-1]['stale'] is True
        await hub.teams.close()
    asyncio.run(main())


def test_conversation_only_review_has_explicit_scope_and_summary_hash(tmp_path, monkeypatch):
    async def main():
        async def handler(_s, n, _messages):
            return reply(f'可核对的文字交付 {n}'), 0
        hub, session, _ = setup_hub(tmp_path, monkeypatch, handler)
        await hub.send(session.id, '给出文字结论')
        await asyncio.wait_for(session.task, 3)
        old_id, summary = session.run['id'], session.run['summary']
        await hub.send(session.id, '下一项任务')
        await asyncio.wait_for(session.task, 3)
        accepted = hub.review(session.id, old_id, True, '已核对文字')
        assert accepted['delivery_review']['scope'] == 'conversation'
        assert accepted['delivery_review']['summary_sha256'] == hashlib.sha256(summary.encode()).hexdigest()
        assert session.run['verification'] == 'unreviewed'
        assert hub.deliveries.state(session.id, old_id)['reviews'] == []
        await hub.teams.close()
    asyncio.run(main())


def test_run_record_rejects_cross_session_path_traversal_and_corrupt_ids(tmp_path, monkeypatch):
    async def main():
        async def handler(*_):
            return reply('文字交付'), 0
        hub, session, _ = setup_hub(tmp_path, monkeypatch, handler)
        await hub.send(session.id, '完成一个任务')
        await asyncio.wait_for(session.task, 3)
        other = Session('other', session.workspace)
        hub.sessions[other.id] = other
        with pytest.raises(ValueError, match='不属于'):
            hub.run_record(other.id, session.run['id'])
        for rid in ('../outside', 'missing', ''):
            if rid:
                with pytest.raises(ValueError):
                    hub.run_record(session.id, rid)
        path = hub.home / 'runs' / 'invalid.json'
        path.write_text(json.dumps({'id': 'wrong-id', 'session': session.id}))
        with pytest.raises(ValueError):
            hub.run_record(session.id, 'invalid')
        await hub.teams.close()
    asyncio.run(main())


def test_material_tools_enforce_assignment_and_keep_immutable_snapshot_after_removal(tmp_path, monkeypatch):
    from test_agent_materials import xlsx_bytes

    async def main():
        hub, session, _ = setup_hub(tmp_path, monkeypatch, None)
        item = hub.contexts.add_file(session.id, 'budget.xlsx', xlsx_bytes())
        other = hub.contexts.add_file(session.id, 'unassigned.txt', b'not supplied to this task')
        session.materials = [deepcopy(item)]
        metadata = await hub.studio.call(session, 'context_inspect', {'id': item['id']})
        assert metadata['format'] == 'xlsx' and metadata['parse_status'] == 'ready'
        assert [x['name'] for x in metadata['sheets']] == ['预算', '来源']
        text = await hub.studio.call(session, 'context_read', {'id': item['id'], 'sheet': '预算'})
        assert '苹果' in text and '=B2*2' in text and '[全文结束]' in text
        assert 'not-fetched' not in text
        with pytest.raises(ValueError, match='未分配'):
            await hub.studio.call(session, 'context_read', {'id': other['id']})
        hub.contexts.update(session.id, item['id'], remove=True)
        assert await hub.studio.call(session, 'context_read', {'id': item['id'], 'sheet': '预算'}) == text
        await hub.teams.close()
    asyncio.run(main())


def test_http_material_inspect_selected_sheet_paging_and_removed_reference(tmp_path):
    from test_agent_materials import xlsx_bytes
    from test_agent_studio_http import app, post

    async def main():
        async with app(tmp_path) as (hub, session, client):
            item = hub.contexts.add_file(session.id, 'budget.xlsx', xlsx_bytes())
            response = await post(client, 'context_inspect', context_id=item['id'])
            assert response.status == 200
            metadata = (await response.json())['item']
            assert metadata['parse_status'] == 'ready' and len(metadata['sheets']) == 2
            response = await post(client, 'context_read', context_id=item['id'], sheet='来源', limit=8)
            assert response.status == 200
            data = await response.json()
            assert data['selection'] == {'page': None, 'sheet': '来源'}
            assert data['offset'] == 0 and data['next_offset'] == 8
            response = await post(client, 'context_read', context_id=item['id'], sheet='来源', offset=8)
            data = await response.json()
            assert response.status == 200 and data['next_offset'] is None
            assert '[全文结束]' in data['text'] and '苹果' not in data['text']
            assert (await post(client, 'context_read', context_id=item['id'], page=1)).status == 400
            hub.contexts.update(session.id, item['id'], remove=True)
            assert (await post(client, 'context_inspect', context_id=item['id'])).status == 400
    asyncio.run(main())


def test_http_historical_delivery_refresh_preview_review_and_blacklist_are_consistent(tmp_path):
    from test_agent_studio_http import app, post

    async def main():
        async with app(tmp_path) as (hub, session, client):
            workspace = Path(session.workspace)
            workspace.mkdir()
            path = workspace / 'result.txt'
            path.write_text('first version', encoding='utf-8')
            session.run = {'id': 'old-run', 'session': session.id, 'state': 'completed', 'verification': 'unreviewed'}
            artifact = await hub.register_delivery(session, str(path))
            hub._write('runs/old-run.json', json.dumps(session.run))
            session.run = {'id': 'current-run', 'session': session.id, 'state': 'completed', 'verification': 'unreviewed'}
            current = deepcopy(session.run)
            query = '/studio?id=main&run_id=old-run&deliveries=1'
            response = await client.get(query)
            data = await response.json()
            assert response.status == 200 and data['delivery_run']['id'] == 'old-run'
            assert data['run'] == current and data['deliveries']['artifacts'][0]['status'] == 'current'
            response = await post(client, 'delivery_read', run_id='old-run', artifact_id=artifact['id'])
            preview = (await response.json())['artifact']
            assert response.status == 200 and preview['preview']['text'] == 'first version'
            versions = {artifact['id']: preview['current']['sha256']}
            assert (await post(client, 'review', run_id='old-run', accepted=True)).status == 400
            assert (await post(client, 'review', run_id='old-run', accepted=True, artifact_versions={})).status == 400
            path.write_text('changed version', encoding='utf-8')
            assert (await post(client, 'review', run_id='old-run', accepted=True, artifact_versions=versions)).status == 400
            response = await client.get(query)
            assert (await response.json())['deliveries']['artifacts'][0]['status'] == 'changed'
            response = await post(client, 'delivery_read', run_id='old-run', artifact_id=artifact['id'])
            preview = (await response.json())['artifact']
            response = await post(client, 'review', run_id='old-run', accepted=True,
                                  artifact_versions={artifact['id']: preview['current']['sha256']})
            assert response.status == 200 and (await response.json())['run']['verification'] == 'accepted'
            assert session.run == current
            assert (await post(client, 'delivery_read', run_id='current-run', artifact_id=artifact['id'])).status == 400
            other = Session('other', session.workspace)
            hub.sessions[other.id] = other
            assert (await client.get('/studio?id=other&run_id=old-run&deliveries=1')).status == 400
            hub.blacklist.add({'kind': 'path', 'value': str(path)})
            assert (await post(client, 'delivery_read', run_id='old-run', artifact_id=artifact['id'])).status == 400
            assert (await client.get(query)).status == 400
    asyncio.run(main())


@pytest.mark.parametrize('command', ['/compact', '/plan'])
def test_session_commands_do_not_replace_interrupted_task_material_snapshot(tmp_path, monkeypatch, command):
    async def main():
        hub, session, _ = setup_hub(tmp_path, monkeypatch, None)
        item = hub.contexts.add_file(session.id, 'source.txt', b'original task source')
        session.materials = [{**item, 'owner': session.id}]
        snapshot = deepcopy(session.materials)
        session.run = {'id': 'interrupted-task', 'state': 'cancelled'}
        assert await hub.send(session.id, command) is None
        assert session.materials == snapshot
        assert session.run == {'id': 'interrupted-task', 'state': 'cancelled'}
        await hub.teams.close()
    asyncio.run(main())


def test_http_context_pagination_uses_terminal_marker_not_quoted_document_content(tmp_path):
    from test_agent_studio_http import app, post

    async def main():
        async with app(tmp_path) as (hub, session, client):
            content = '正文引用：[后续用 offset=999999]。' + ('后面的实际材料。' * 40)
            item = hub.contexts.add_file(session.id, 'markers.txt', content.encode())
            response = await post(client, 'context_read', context_id=item['id'])
            data = await response.json()
            assert response.status == 200 and data['next_offset'] is None
            assert '[后续用 offset=999999]' in data['text'] and data['text'].endswith('[全文结束]')
            response = await post(client, 'context_read', context_id=item['id'], limit=80)
            data = await response.json()
            assert response.status == 200 and data['next_offset'] == 80
            assert data['text'].endswith('[后续用 offset=80]')
    asyncio.run(main())


def test_large_document_inspection_returns_complete_bounded_tool_metadata(tmp_path, monkeypatch):
    import io
    docx = pytest.importorskip('docx')
    document = docx.Document()
    for index in range(700):
        document.add_paragraph(f'第 {index + 1} 个实际材料段落。')
    data = io.BytesIO()
    document.save(data)

    async def main():
        hub, session, _ = setup_hub(tmp_path, monkeypatch, None)
        item = hub.contexts.add_file(session.id, 'many-paragraphs.docx', data.getvalue())
        session.materials = [item]
        out, error = await hub.studio.execute(session, 'inspect-document', 'context_inspect', {'id': item['id']})
        assert error is False and len(out) <= 30000
        metadata = json.loads(out)
        assert metadata['format'] == 'docx' and metadata['parse_status'] == 'ready'
        assert metadata['chars'] > 10000 and metadata['readable'] is True
        await hub.teams.close()
    asyncio.run(main())
