"""Independent resident regressions; use real orchestration with deterministic turns."""
import asyncio

import pytest

from wm.agent.core import AgentHub
from wm.agent.resident import Resident


def setup(tmp_path):
    ws = tmp_path / 'workspace'
    ws.mkdir()
    h = AgentHub({'agent': {'workspace': str(ws), 'extensions': {'settings_path': False}}}, home=tmp_path / 'agent')
    source = h.new_session(str(ws), 'workspace')
    clock = [1000.0]
    resident = Resident(h, clock=lambda: clock[0])
    h.resident = resident
    return h, resident, source, clock


async def round_once(resident):
    await resident.tick()
    tasks = list(resident._workers.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


def test_total_budget_counts_nested_agents_and_blocks_next_round(tmp_path, monkeypatch):
    async def run():
        h, r, source, clock = setup(tmp_path)
        child_cost = [0]
        monkeypatch.setattr(h.teams, 'spent', lambda *a, **kw: child_cost[0])
        calls = []
        async def turn(s, _):
            calls.append(s.id)
            s.stats['cost'] += 0.2
            child_cost[0] = 0.8
            r.report(s, 'progress', 'first stage finished', delay_s=1)
        monkeypatch.setattr(h, '_turn', turn)
        item = r.create(source.id, 'test goal', budget_cny=1)
        await round_once(r)
        assert r.get(item['id'])['spent'] == 1
        clock[0] += 2
        await round_once(r)
        assert r.get(item['id'])['state'] == 'blocked'
        assert len(calls) == 1
        await r.close()
        await h.teams.close()
    asyncio.run(run())


def test_crash_recovery_reconciles_cost_without_replaying(tmp_path, monkeypatch):
    async def run():
        h, r, source, _ = setup(tmp_path)
        item = r.create(source.id, 'crash fixture', budget_cny=5)
        s = h.get(item['session_id'])
        s.stats['cost'] = 3
        h.save(s)
        monkeypatch.setattr(h.teams, 'spent', lambda *a, **kw: 2)
        r._change(item['id'], lambda v: v.update(state='running', run_id='unfinished'))
        recovered = Resident(h)
        await recovered.start()
        value = recovered.get(item['id'])
        assert value['state'] == 'interrupted' and value['spent'] == 5
        assert not recovered._workers
        await recovered.close()
        await h.teams.close()
    asyncio.run(run())


def test_queued_followup_cannot_report_or_replace_original_goal_round(tmp_path, monkeypatch):
    async def run():
        h, r, source, _ = setup(tmp_path)
        calls = []
        async def turn(s, _):
            calls.append(s.run['id'])
            if len(calls) == 1:
                r.report(s, 'progress', 'original round finished', delay_s=1)
                await h.enqueue(s.id, 'user followup')
            else:
                with pytest.raises(ValueError, match='没有正在运行'):
                    r.report(s, 'completed', 'must not finish the other run')
        monkeypatch.setattr(h, '_turn', turn)
        item = r.create(source.id, 'queue fixture')
        await round_once(r)
        s = h.get(item['session_id'])
        if s.task:
            await s.task
        value = r.get(item['id'])
        assert value['run_id'] == calls[0]
        assert value['state'] == 'waiting'
        assert value['rounds'] == 1
        await r.close()
        await h.teams.close()
    asyncio.run(run())


def test_cancelled_task_does_not_leave_resident_running(tmp_path, monkeypatch):
    async def run():
        h, r, source, _ = setup(tmp_path)
        async def turn(s, _):
            raise asyncio.CancelledError()
        monkeypatch.setattr(h, '_turn', turn)
        item = r.create(source.id, 'cancel fixture')
        await round_once(r)
        assert r.get(item['id'])['state'] == 'interrupted'
        await r.close()
        await h.teams.close()
    asyncio.run(run())


def test_two_runners_cannot_recover_each_others_running_tasks(tmp_path):
    async def run():
        h, one, _, _ = setup(tmp_path)
        two = Resident(h)
        await one.start()
        try:
            with pytest.raises(RuntimeError, match='已有持续任务'):
                await two.start()
        finally:
            await one.close()
        await two.start()
        assert two.state()['runner_online']
        await two.close()
        await h.teams.close()
    asyncio.run(run())
