"""Local session management, workspace labels and independent conversation forks."""
import asyncio
import json
from pathlib import Path
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.core import AgentHub, Session
from wm.app import AGENT, CFG, api_agent


@pytest.fixture
def hub(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    return AgentHub({'agent': {'workspace': str(workspace), 'extensions': {'settings_path': False}}}, home=tmp_path / 'agent')


def session(hub, sid='main', title='原会话'):
    s = Session(sid, str(hub.default_workspace()), title)
    hub.sessions[sid] = s
    hub.save(s)
    return s


def test_workspace_requires_existing_folder_and_persists_custom_name(hub, tmp_path):
    folder = tmp_path / 'existing'
    folder.mkdir()
    hub.add_workspace(str(folder), '桌宠计划')
    hub.add_workspace(str(folder / '.'), '更新名称')
    state = hub.state()
    rows = [w for w in state['workspaces'] if w['path'] == str(folder)]
    assert len(rows) == 1 and rows[0]['name'] == '更新名称'
    reloaded = AgentHub(hub.cfg, home=hub.home)
    assert next(w for w in reloaded.state()['workspaces'] if w['path'] == str(folder))['name'] == '更新名称'
    with pytest.raises(ValueError, match='已存在'):
        hub.add_workspace(str(tmp_path / 'missing'), '不会创建')
    assert not (tmp_path / 'missing').exists()
    with pytest.raises(ValueError):
        hub.add_workspace('relative', 'bad')
    with pytest.raises(ValueError):
        hub.add_workspace(str(folder), 'x' * 81)


def test_metadata_persists_pin_order_unread_and_full_title(hub):
    older, newer = session(hub), session(hub, 'newer')
    title = '重命名后的会话' * 6
    hub.rename_session(older.id, title)
    hub.flag_session(older.id, 'pinned', True)
    hub.flag_session(older.id, 'unread', True)
    hub.save(newer)
    loaded = AgentHub(hub.cfg, home=hub.home)
    row = loaded.state()['workspaces'][0]['sessions'][0]
    assert row['id'] == older.id and row['title'] == title
    assert row['pinned'] and row['unread']
    assert loaded.get(older.id).view()['unread'] is True
    with pytest.raises(ValueError):
        hub.flag_session(older.id, 'pinned', 'true')
    with pytest.raises(ValueError):
        hub.rename_session(older.id, '')
    with pytest.raises(ValueError):
        hub.rename_session(older.id, 'bad\nname')


def test_legacy_session_defaults_do_not_archive_or_pin(hub):
    s = session(hub)
    data = s.to_json()
    for key in ('pinned', 'unread', 'archived', 'archived_at', 'deleted_at', 'forked_from'):
        del data[key]
    legacy = Session.from_json(data)
    assert not legacy.pinned and not legacy.unread and not legacy.archived
    assert legacy.forked_from == ''


def test_archive_is_reversible_metadata_preserving_context_and_history(hub):
    s = session(hub)
    s.messages = [{'role': 'user', 'content': '原始需求'}]
    s.run = {'id': 'previous-run', 'state': 'completed'}
    item = hub.contexts.add_file(s.id, '材料.md', '原始材料'.encode())
    s.materials = hub.contexts.select(s.id, [item['id']])
    hub.save(s)
    hub.archive(s.id)
    assert (hub.home / 'sessions' / 'main.json').exists()
    assert not (hub.home / 'trash' / 'main.json').exists()
    assert hub.state()['workspaces'][0]['sessions'] == []
    assert hub.state()['archived_sessions'][0]['id'] == s.id
    assert asyncio.run(hub.send(s.id, '不能在归档中运行')).startswith('请先恢复')
    restored = hub.restore_session(s.id)
    assert not restored.archived and restored.messages[0]['content'] == '原始需求'
    assert restored.run['id'] == 'previous-run'
    assert '原始材料' in hub.contexts.read(s.id, item['id'])
    assert hub.state()['archived_sessions'] == []


def test_delete_moves_to_trash_with_explicit_confirmation_and_restore(hub):
    s = session(hub)
    workspace_file = Path(s.workspace) / 'keep.txt'
    workspace_file.write_text('never delete workspace content')
    with pytest.raises(ValueError, match='确认'):
        hub.delete_session(s.id)
    assert hub.get(s.id) is s
    assert hub.delete_session(s.id, confirm=True)
    assert hub.get(s.id) is None
    row = hub.state()['archived_sessions'][0]
    assert row['trashed'] is True and row['deleted_at'] > 0
    assert workspace_file.read_text() == 'never delete workspace content'
    assert not hub.restore_session(s.id).archived
    assert not (hub.home / 'trash' / f'{s.id}.json').exists()


def test_restore_old_archive_trash_format_and_rejects_id_mismatch(hub):
    s = session(hub)
    trash = hub.home / 'trash'
    trash.mkdir()
    (hub.home / 'sessions' / 'main.json').replace(trash / 'main.json')
    hub.sessions.pop('main')
    assert hub.state()['archived_sessions'][0]['trashed'] is True
    assert hub.restore_session('main').title == '原会话'
    (trash / 'wrong.json').write_text(json.dumps(s.to_json()), 'utf-8')
    with pytest.raises(ValueError, match='无效'):
        hub.restore_session('wrong')


def test_failed_delete_move_rolls_back_visible_and_persisted_metadata(hub, monkeypatch):
    s = session(hub)
    original = Path.replace
    def fail_trash(path, target):
        if Path(target) == hub.home / 'trash' / 'main.json':
            raise OSError('simulated locked destination')
        return original(path, target)
    monkeypatch.setattr(Path, 'replace', fail_trash)
    with pytest.raises(OSError, match='locked'):
        hub.delete_session(s.id, confirm=True)
    assert not s.archived and s.deleted_at == 0
    saved = json.loads((hub.home / 'sessions' / 'main.json').read_text('utf-8'))
    assert not saved['archived'] and saved['deleted_at'] == 0
    assert not (hub.home / 'trash' / 'main.json').exists()


def test_failed_restore_keeps_the_original_recoverable_trash_record(hub, monkeypatch):
    s = session(hub)
    hub.delete_session(s.id, confirm=True)
    before = (hub.home / 'trash' / 'main.json').read_bytes()
    def fail_write(*args):
        raise OSError('simulated disk write failure')
    monkeypatch.setattr(hub, '_write', fail_write)
    with pytest.raises(OSError, match='disk'):
        hub.restore_session(s.id)
    assert (hub.home / 'trash' / 'main.json').read_bytes() == before
    assert not (hub.home / 'sessions' / 'main.json').exists()
    assert s.id not in hub.sessions


@pytest.mark.parametrize('operation', ['archive', 'delete', 'move', 'fork'])
def test_running_session_and_children_block_lifecycle_changes(hub, tmp_path, operation):
    s = session(hub)
    folder = tmp_path / 'target'
    folder.mkdir()
    hub.add_workspace(str(folder))
    def invoke():
        if operation == 'archive':
            return hub.archive(s.id)
        if operation == 'delete':
            return hub.delete_session(s.id, confirm=True)
        if operation == 'move':
            return hub.move_session(s.id, str(folder))
        return asyncio.run(hub.fork_session(s.id))
    s.status = 'running'
    with pytest.raises(ValueError, match='停止'):
        invoke()
    s.status = 'idle'
    hub.teams._records['child'] = {'id': 'child', 'parent_id': s.id, 'status': 'running', 'created': 1}
    with pytest.raises(ValueError, match='停止'):
        invoke()
    assert s.workspace == str(hub.default_workspace()) and not s.archived


def test_move_changes_future_workspace_without_moving_user_files(hub, tmp_path):
    s = session(hub)
    old_path = Path(s.workspace) / 'output.txt'
    old_path.write_text('source output')
    s.run = {'id': 'old-run', 'state': 'completed'}
    artifact = asyncio.run(hub.register_delivery(s, str(old_path)))
    target = tmp_path / 'target'
    target.mkdir()
    hub.add_workspace(str(target), '另一个项目')
    hub.move_session(s.id, str(target))
    assert old_path.read_text() == 'source output' and not (target / 'output.txt').exists()
    assert hub.deliveries.read(s.id, artifact['id'], run_id='old-run')['preview']['text'] == 'source output'
    assert str(target) in hub.get(s.id).context_system


def test_fork_copies_history_and_immutable_materials_with_independent_state(hub):
    s = session(hub)
    s.messages = [{'role': 'user', 'content': '保留上下文'}, {'role': 'assistant', 'content': '收到'}]
    s.events = [{'type': 'user', 'text': '保留上下文'}, {'type': 'assistant', 'text': '收到'},
                {'type': 'done', 'run': {'id': 'old-run'}, 'stats': {'cost': 12}}, {'type': 'ask', 'id': 'old-ask'}]
    s.run = {'id': 'old-run', 'state': 'completed'}
    s.stats['cost'] = 12
    s.queue = [{'text': '不自动执行'}]
    s.plan = {'steps': [{'title': '原计划', 'status': 'done'}]}
    item = hub.contexts.add_file(s.id, '材料.md', '分支材料'.encode())
    link = hub.contexts.add_reference(s.id, 'https://example.org/source')
    hub.contexts.update(s.id, link['id'], pinned=True)
    s.materials = hub.contexts.select(s.id, [item['id']])
    hub.contexts.update(s.id, item['id'], remove=True)
    hub.save(s)
    fork = asyncio.run(hub.fork_session(s.id))
    assert fork.id != s.id and fork.forked_from == s.id
    assert fork.messages == s.messages and fork.messages is not s.messages
    assert fork.run == {} and fork.queue == [] and fork.plan == {} and fork.stats['cost'] == 0
    assert not fork.asks and fork.status == 'idle'
    assert not any(e.get('type') in {'done', 'ask'} for e in fork.events)
    assert len(hub.contexts.list(fork.id)) == 2 and len(fork.materials) == 2
    copied = next(x for x in fork.materials if x['kind'] == 'file')
    assert copied['id'] != item['id'] and copied['owner'] == fork.id
    assert hub.contexts._file_data(fork.id, copied)[0] != hub.contexts._file_data(s.id, item)[0]
    hub.contexts._file_data(s.id, item)[0].write_bytes(b'modified original')
    assert '分支材料' in hub.contexts.read(fork.id, copied['id'])
    fork.messages[0]['content'] = '独立修改'
    assert s.messages[0]['content'] == '保留上下文'


def test_failed_fork_is_not_published_and_does_not_change_original(hub):
    s = session(hub)
    item = hub.contexts.add_file(s.id, 'missing.md', b'content')
    hub.contexts._file_data(s.id, item)[0].unlink()
    with pytest.raises(ValueError, match='附件'):
        asyncio.run(hub.fork_session(s.id))
    assert set(hub.sessions) == {s.id} and not s.management_busy
    assert len(list((hub.home / 'sessions').glob('*.json'))) == 1
    assert [x.name for x in hub.contexts.home.iterdir()] == [s.id]


def test_send_is_rejected_while_fork_snapshot_is_being_copied(hub, monkeypatch):
    s = session(hub)
    entered, release = threading.Event(), threading.Event()
    original = hub._fork_copy
    def hold(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(hub, '_fork_copy', hold)
    async def run():
        task = asyncio.create_task(hub.fork_session(s.id))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert '正在复制' in await hub.send(s.id, 'new task')
            with pytest.raises(ValueError, match='正在复制'):
                hub.move_session(s.id, s.workspace)
            with pytest.raises(ValueError, match='正在复制'):
                hub.settings(s.id, mode='unrestricted')
        finally:
            release.set()
            await task
    asyncio.run(run())


def test_http_navigation_contract_and_origin_checks(hub, tmp_path):
    async def run():
        s = session(hub)
        application = web.Application()
        application[AGENT], application[CFG] = hub, {}
        application.router.add_route('*', '/api/agent', api_agent)
        async with TestClient(TestServer(application)) as client:
            async def post(action, **extra):
                return await client.post('/api/agent', json={'action': action, 'id': s.id, **extra})
            assert (await client.post('/api/agent', json={'action': 'pin', 'id': s.id, 'pinned': True}, headers={'Origin': 'https://evil.example'})).status == 403
            folder = tmp_path / 'target'
            folder.mkdir()
            response = await post('workspace', path=str(folder), name='命名工作区')
            assert response.status == 200
            assert (await response.json())['workspaces'][-1]['name'] == '命名工作区'
            assert (await post('rename', title='新的会话名')).status == 200
            assert (await post('pin', pinned=True)).status == 200
            assert (await post('unread', unread=True)).status == 200
            assert (await post('unread', unread='true')).status == 400
            assert (await post('move_workspace', workspace=str(folder))).status == 200
            fork = await (await post('fork')).json()
            assert fork['forked_from'] == s.id and fork['id'] != s.id
            assert (await post('archive')).status == 200
            state = await (await client.get('/api/agent')).json()
            assert [x['id'] for x in state['archived_sessions']] == [s.id]
            assert (await post('restore')).status == 200
            assert (await post('delete')).status == 400
            assert (await post('delete', confirm=True)).status == 200
            assert (await post('restore')).status == 200
            assert (await post('rename', id='../main', title='bad')).status == 400
    asyncio.run(run())
