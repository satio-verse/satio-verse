import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.core import AgentHub
from wm.agent.resident import Resident
from wm.app import AGENT, CFG, api_agent_resident


def setup(tmp_path):
    work = tmp_path / 'project'
    work.mkdir()
    hub = AgentHub({'agent': {'workspace': str(work)}, 'billing': {'env_key': ''}}, home=tmp_path / 'agent')
    source = hub.new_session(mode='read')
    return hub, source


async def drain(hub):
    for worker in list(hub.resident._workers.values()):
        await worker
    await asyncio.sleep(0)


def test_goal_persists_separate_session_materials_and_event_cursor(tmp_path):
    hub, source = setup(tmp_path)
    material = hub.contexts.add_file(source.id, 'source.txt', b'bounded context')
    source.materials = [material]
    source.time_budget_s, source.max_steps = 300, 75
    item = hub.resident.create(source.id, 'finish the report', budget_cny=2)
    target = hub.get(item['session_id'])
    assert target.id != source.id and target.workspace == source.workspace and target.mode == 'read'
    assert (target.time_budget_s, target.max_steps) == (300, 75)
    assert target.materials[0]['owner'] == source.id
    target.materials[0]['label'] = 'changed'
    assert source.materials[0].get('label') != 'changed'
    reopened = Resident(hub)
    assert reopened.get(item['id'])['objective'] == 'finish the report'
    first = reopened.state()
    assert len(first['events']) == 1 and reopened.state(first['next_cursor'])['events'] == []


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True, 'oops'])
def test_invalid_budgets_do_not_create_sessions(tmp_path, value):
    hub, source = setup(tmp_path)
    with pytest.raises(ValueError):
        hub.resident.create(source.id, 'target', budget_cny=value)
    assert len(hub.sessions) == 1


def test_future_not_run_and_pause_cas(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        hub.resident.clock = lambda: 1000
        item = hub.resident.create(source.id, 'target', next_at=2000)
        await hub.resident.tick()
        assert not hub.resident._workers
        paused = await hub.resident.control(item['id'], 'pause', item['revision'])
        with pytest.raises(ValueError, match='状态已更新'):
            await hub.resident.control(item['id'], 'resume', item['revision'])
        await hub.resident.control(item['id'], 'resume', paused['revision'])
        assert hub.resident.get(item['id'])['next_at'] == 1000
    asyncio.run(main())


def test_real_hub_round_complete_without_ui_or_model(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        calls = []
        async def turn(s, message):
            calls.append(message)
            s.stats['cost'] += .02
            hub.resident.report(s, 'completed', 'Report exists and was checked')
        hub._turn = turn
        item = hub.resident.create(source.id, 'write report')
        await hub.resident.tick()
        await drain(hub)
        final = hub.resident.get(item['id'])
        assert final['state'] == 'completed' and final['rounds'] == 1
        assert final['spent'] == .02 and hub.get(item['session_id']).unread
        assert not hub.subs and len(calls) == 1
        await hub.resident.tick()
        assert not hub.resident._workers
    asyncio.run(main())


def test_sleep_then_continue_does_not_send_until_due(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        now = [1000]
        hub.resident.clock = lambda: now[0]
        async def turn(s, _):
            hub.resident.report(s, 'progress', 'first stage ready', delay_s=60)
        hub._turn = turn
        item = hub.resident.create(source.id, 'follow up')
        await hub.resident.tick(); await drain(hub)
        assert hub.resident.get(item['id'])['state'] == 'scheduled'
        await hub.resident.tick()
        assert not hub.resident._workers
        now[0] = 1061
        await hub.resident.tick(); await drain(hub)
        assert hub.resident.get(item['id'])['rounds'] == 2
    asyncio.run(main())


def test_no_report_never_marks_goal_completed(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        async def turn(s, _):
            return
        hub._turn = turn
        item = hub.resident.create(source.id, 'ambiguous progress')
        await hub.resident.tick(); await drain(hub)
        assert hub.resident.get(item['id'])['state'] == 'waiting'
    asyncio.run(main())


def test_budget_stops_next_round(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        now = [1000]
        hub.resident.clock = lambda: now[0]
        async def turn(s, _):
            s.stats['cost'] += .1
            hub.resident.report(s, 'progress', 'one stage done')
        hub._turn = turn
        item = hub.resident.create(source.id, 'budgeted', budget_cny=.05)
        await hub.resident.tick(); await drain(hub)
        now[0] += 2
        await hub.resident.tick(); await drain(hub)
        assert hub.resident.get(item['id'])['state'] == 'blocked'
        assert hub.resident.get(item['id'])['rounds'] == 1
    asyncio.run(main())


def test_user_stop_and_unrelated_session_cannot_resume_goal(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        entered = asyncio.Event()
        async def turn(s, _):
            entered.set()
            await s.stop_event.wait()
            with pytest.raises(ValueError):
                hub.resident.report(s, 'progress', 'cannot undo user pause')
        hub._turn = turn
        item = hub.resident.create(source.id, 'long work')
        await hub.resident.tick(); await entered.wait()
        current = hub.resident.get(item['id'])
        await hub.resident.control(item['id'], 'pause', current['revision'])
        await drain(hub)
        assert hub.resident.get(item['id'])['state'] == 'paused'
        with pytest.raises(ValueError, match='未绑定'):
            hub.resident.report(source, 'progress', 'wrong session')
    asyncio.run(main())


def test_restart_never_replays_claimed_round(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        item = hub.resident.create(source.id, 'unknown side effects')
        hub.resident._change(item['id'], lambda v: v.update(state='running'))
        reopened = Resident(hub)
        await reopened.start()
        assert reopened.get(item['id'])['state'] == 'interrupted'
        await reopened.tick()
        assert not reopened._workers
        await reopened.close()
    asyncio.run(main())


def test_resident_http_origin_and_missing_revision(tmp_path):
    async def main():
        hub, source = setup(tmp_path)
        app = web.Application()
        app[AGENT], app[CFG] = hub, {}
        app.router.add_route('*', '/api/agent/resident', api_agent_resident)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/api/agent/resident', headers={'Origin':'https://evil.example'})).status == 403
            response = await client.post('/api/agent/resident', json={'action':'create','session_id':source.id,'objective':'task'})
            assert response.status == 200
            item = (await response.json())['item']
            response = await client.post('/api/agent/resident', json={'action':'pause','id':item['id']})
            assert response.status == 400
            response = await client.get('/api/agent/resident')
            assert (await response.json())['items'][0]['state'] == 'scheduled'
    asyncio.run(main())
