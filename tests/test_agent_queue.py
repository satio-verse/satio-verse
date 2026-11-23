"""Queued user inputs: actual Hub methods, temp storage, controllable events, no LLM/network."""
import asyncio
from copy import deepcopy
import json

import pytest

from wm.agent.core import AgentHub, Session


def setup(tmp_path):
    ws = tmp_path / 'workspace'
    ws.mkdir()
    hub = AgentHub({'agent': {'workspace': str(ws)}}, home=tmp_path / 'agent')
    s = Session('queue-test', str(ws), model='offline-test')
    hub.sessions[s.id] = s
    hub.save(s)
    started, release, workers = [], asyncio.Event(), []

    async def worker(session, text):
        workers.append(asyncio.current_task())
        if not session.stop_event.is_set():
            started.append({'text': text, 'run': deepcopy(session.run), 'materials': deepcopy(session.materials)})
        await release.wait()
    hub._run = worker

    async def finish():
        release.set()
        await asyncio.sleep(0)
        await asyncio.gather(*workers, return_exceptions=True)
        if s.task and not s.task.done():
            await s.task
    return hub, s, started, finish


def disk(hub, s):
    return json.loads((hub.home / 'sessions' / (s.id + '.json')).read_text('utf-8'))


def pending(s, *texts):
    s.queue = [{'id': f'item-{i}', 'text': text, 'materials': [], 'created': i + 1}
               for i, text in enumerate(texts)]
    return list(s.queue)


@pytest.mark.parametrize('flag', ['archived', 'management_busy', 'parent_id'])
def test_enqueue_rejects_unavailable_session_without_acknowledging_or_mutating(tmp_path, flag):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        setattr(s, flag, 'parent' if flag == 'parent_id' else True)
        original = disk(hub, s)
        with pytest.raises(ValueError):
            await hub.enqueue(s.id, 'not accepted')
        assert not s.queue and not started and disk(hub, s) == original
        await finish()
    asyncio.run(run())


@pytest.mark.parametrize('flag', ['archived', 'management_busy'])
def test_send_refusal_retains_existing_queue_and_materials(tmp_path, flag):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        items = pending(s, 'first', 'second')
        material = hub.contexts.add_file(s.id, 'original.txt', b'ORIGINAL_VERSION')
        s.materials = hub.contexts.select(s.id, [material['id']])
        original_materials = deepcopy(s.materials)
        hub.save(s)
        setattr(s, flag, True)
        await hub._next_queued(s)
        assert s.queue == items and disk(hub, s)['queue'] == items
        assert s.materials == original_materials
        assert not started and not s.run
        await finish()
    asyncio.run(run())


def test_stop_during_enqueue_event_keeps_item_and_never_restarts(tmp_path):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        async def stop_on_ack(event):
            if event.get('type') == 'queued' and event['queue']:
                await hub.stop(s.id)
        hub.subscribe(s.id, stop_on_ack)
        item = await hub.enqueue(s.id, 'stop must win')
        await asyncio.sleep(0)
        assert s.stop_event.is_set() and s.queue == [item]
        assert disk(hub, s)['queue'] == [item] and not s.run and not started
        await finish()
    asyncio.run(run())


def test_other_send_during_ack_preserves_queued_input(tmp_path):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        once = False
        async def direct(event):
            nonlocal once
            if not once and event.get('type') == 'queued' and event['queue']:
                once = True
                await hub.send(s.id, 'direct input')
        hub.subscribe(s.id, direct)
        item = await hub.enqueue(s.id, 'later input')
        await asyncio.sleep(0)
        assert [x['text'] for x in started] == ['direct input']
        assert s.queue == [item] and disk(hub, s)['queue'] == [item]
        await finish()
    asyncio.run(run())


def test_archive_after_ack_keeps_item_for_explicit_later_dispatch(tmp_path):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        async def archive(event):
            if event.get('type') == 'queued' and event['queue']:
                s.archived = True
        hub.subscribe(s.id, archive)
        item = await hub.enqueue(s.id, 'retain through refusal')
        assert s.queue == [item] and disk(hub, s)['queue'] == [item] and not started
        assert any(e['type'] == 'note' and '已保留' in e['text'] for e in s.events)
        await finish()
    asyncio.run(run())


def test_two_drains_consume_exactly_one_head_and_commit_before_event(tmp_path):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        first, second = pending(s, 'first', 'second')
        hub.save(s)
        entered, release = asyncio.Event(), asyncio.Event()
        event_snapshot = []
        async def hold(event):
            if event.get('type') == 'queued':
                event_snapshot.append((deepcopy(event['queue']), disk(hub, s)))
                entered.set()
                await release.wait()
        hub.subscribe(s.id, hold)
        one = asyncio.create_task(hub._next_queued(s))
        await entered.wait()
        await hub._next_queued(s)
        release.set()
        await one
        await asyncio.sleep(0)
        assert len(started) == 1 and started[0]['text'] == 'first'
        assert s.queue == [second]
        event_queue, saved = event_snapshot[0]
        assert event_queue == saved['queue'] == [second]
        assert saved['run']['queue_item_id'] == first['id']
        assert saved['run']['queued_input'] == first
        # A crash after durable acceptance cannot silently replay the consumed item.
        restored = Session.from_json(saved)
        assert restored.run['state'] == 'interrupted' and restored.queue == [second]
        await finish()
    asyncio.run(run())


def test_stale_enqueue_handoff_does_not_consume_next_item(tmp_path):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        once = False
        async def hold_first(event):
            nonlocal once
            if not once and event.get('type') == 'queued' and event['queue']:
                once = True
                entered.set()
                await release.wait()
        hub.subscribe(s.id, hold_first)
        one = asyncio.create_task(hub.enqueue(s.id, 'same text'))
        await entered.wait()
        second = await hub.enqueue(s.id, 'same text')
        release.set()
        first = await one
        await asyncio.sleep(0)
        assert first['id'] != second['id']  # Contents are deliberately not an idempotency key.
        assert s.queue == [second] and len(started) == 1
        assert started[0]['run']['queue_item_id'] == first['id']
        await finish()
    asyncio.run(run())


def test_material_snapshot_and_full_input_survive_acceptance(tmp_path):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        s.status = 'running'
        material = hub.contexts.add_file(s.id, 'spec.txt', b'VERSION_ONE')
        text = 'synthetic requirement ' * 250
        item = await hub.enqueue(s.id, text, [material['id']])
        snapshot = deepcopy(item['materials'])
        hub.contexts.update(s.id, material['id'], remove=True)
        s.status = 'idle'
        await hub._next_queued(s)
        await asyncio.sleep(0)
        saved = disk(hub, s)
        assert saved['materials'] == snapshot == started[0]['materials']
        assert saved['run']['queued_input']['text'] == text.strip()
        assert saved['run']['queued_input']['materials'] == snapshot
        assert 'VERSION_ONE' in hub.contexts.read(s.id, material['id'], snapshot=snapshot)
        await finish()
    asyncio.run(run())


def test_enqueue_persistence_failure_is_not_acknowledged(tmp_path, monkeypatch):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        original = disk(hub, s)
        s.stop_event.set()
        stop = s.stop_event
        def fail(*_):
            raise OSError('synthetic disk failure')
        monkeypatch.setattr(hub, '_write', fail)
        with pytest.raises(OSError):
            await hub.enqueue(s.id, 'not durably accepted')
        assert not s.queue and not s.events and not started
        assert s.stop_event is stop and stop.is_set() and disk(hub, s) == original
        await finish()
    asyncio.run(run())


@pytest.mark.parametrize('failure_location', ['sessions/', 'runs/'])
def test_dispatch_persistence_failure_restores_order_plan_materials_and_stop(tmp_path, monkeypatch, failure_location):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        items = pending(s, 'first', 'second')
        s.run = {'id': 'old-run', 'state': 'completed', 'plan': {'old': True}}
        s.plan = {'steps': ['keep this exact plan']}
        material = hub.contexts.add_file(s.id, 'original.txt', b'ORIGINAL_VERSION')
        s.materials = hub.contexts.select(s.id, [material['id']])
        s.allow_turn = {'existing permission'}
        stop, plan, materials, allowed = s.stop_event, s.plan, s.materials, s.allow_turn
        run_identity, old_run = s.run, deepcopy(s.run)
        hub.save(s)
        old_disk = disk(hub, s)
        write = hub._write
        def fail(path, value):
            if path.startswith(failure_location):
                raise OSError('synthetic durable commit failure')
            return write(path, value)
        monkeypatch.setattr(hub, '_write', fail)
        with pytest.raises(OSError):
            await hub._next_queued(s)
        assert s.queue == items and disk(hub, s) == old_disk
        assert s.status == 'idle' and s.run is run_identity and s.run == old_run and s.task is None
        assert s.plan is plan and s.materials is materials and s.stop_event is stop and s.allow_turn is allowed
        assert not started
        await finish()
    asyncio.run(run())


@pytest.mark.parametrize('command', ['/plan', '/compact'])
def test_queued_slash_command_consumed_and_next_item_runs(tmp_path, command):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        first, second = pending(s, command, 'real task')
        hub.save(s)
        await hub._next_queued(s)
        await asyncio.sleep(0)
        assert not s.queue and [x['text'] for x in started] == ['real task']
        assert s.run['queue_item_id'] == second['id']
        assert len([e for e in s.events if e['type'] == 'queued']) == 2
        if command == '/plan':
            assert s.preset == 'plan'
        await finish()
    asyncio.run(run())


def test_plan_commit_failure_keeps_queue_and_previous_mode(tmp_path, monkeypatch):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        items = pending(s, '/plan', 'later')
        hub.save(s)
        def fail(*_):
            raise OSError('synthetic failure')
        monkeypatch.setattr(hub, '_write', fail)
        with pytest.raises(OSError):
            await hub._next_queued(s)
        assert s.queue == items and s.preset == 'standard' and not started
        await finish()
    asyncio.run(run())


def test_explicit_new_enqueue_can_restart_previously_stopped_idle_queue(tmp_path):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        old = pending(s, 'previously stopped')[0]
        await hub.stop(s.id)
        new = await hub.enqueue(s.id, 'explicitly continue queue')
        await asyncio.sleep(0)
        assert not s.stop_event.is_set() and s.queue == [new]
        assert started[0]['run']['queue_item_id'] == old['id']
        await finish()
    asyncio.run(run())


@pytest.mark.parametrize('command', ['/plan', '/compact'])
def test_stop_during_queued_command_keeps_next_input_pending(tmp_path, command):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        first, second = pending(s, command, 'must remain queued')
        hub.save(s)
        async def stop_at_note(event):
            if event.get('type') == 'note':
                await hub.stop(s.id)
        hub.subscribe(s.id, stop_at_note)
        await hub._next_queued(s)
        assert s.queue == [second] and disk(hub, s)['queue'] == [second]
        assert s.stop_event.is_set() and not started and not s.run
        await finish()
    asyncio.run(run())


def test_real_compaction_write_failure_retains_original_history_and_queue(tmp_path, monkeypatch):
    async def run():
        hub, s, started, finish = setup(tmp_path)
        original = 'SYNTHETIC_TOOL_RESULT_' * 70
        s.messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'history-call', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'history-call', 'content': original},
        ] + [{'role': 'user', 'content': f'tail {i}'} for i in range(12)]
        items = pending(s, '/compact', 'later input')
        hub.save(s)
        original_disk = disk(hub, s)
        mid = s.messages[1]['_id']
        def fail(*_):
            raise OSError('synthetic disk failure after in-memory folding')
        monkeypatch.setattr(hub, '_write', fail)
        with pytest.raises(OSError):
            await hub._next_queued(s)
        # Queue acceptance is atomic on disk; unrelated safe in-memory folding is not undone.
        assert s.messages[1]['content'] != original
        assert hub.history_read(s.id, mid)['text'] == original
        assert hub.history_read(s.id, mid)['folded'] is True
        assert s.queue == items and disk(hub, s) == original_disk
        assert not s.run and s.task is None and not started
        await finish()
    asyncio.run(run())
