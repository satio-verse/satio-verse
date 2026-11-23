import asyncio
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from wm.agent.core import AgentHub, Session
from wm.platform_http import platform, services


def test_real_hub_contribution_flow_and_busy_checks(tmp_path):
    async def run():
        workspace=tmp_path/'workspace';workspace.mkdir()
        hub=AgentHub({'agent':{'workspace':str(workspace),'extensions':{'settings_path':False}}},home=tmp_path/'agent')
        session=Session('main',str(workspace),'Current')
        session.events=[{'type':'user','message_id':'u1','text':'question'},
                        {'type':'assistant','message_id':'a1','text':'answer','feedback':{'value':'good'}},
                        {'type':'user','message_id':'u2','text':'do not include this'}]
        hub.sessions[session.id]=session;hub.save(session)
        app=web.Application()
        async def handler(request):return await platform(request,hub)
        app.router.add_route('*','/api/agent/platform',handler)
        async with TestClient(TestServer(app)) as client:
            async def post(body,status=200):
                response=await client.post('/api/agent/platform',json=body)
                assert response.status==status
                return await response.json()
            state=await (await client.get('/api/agent/platform')).json()
            assert not state['data']['settings']['training']
            preview=(await post({'action':'data_preview','session':'main','kind':'training','event_id':'a1'}))['result']
            assert len(preview['payload']['messages'])==2
            assert 'do not include this' not in str(preview)
            await post({'action':'data_queue','session':'main','kind':'training','event_id':'a1','sha256':preview['sha256']},400)
            await post({'action':'data_settings','training':True})
            queued=await post({'action':'data_queue','session':'main','kind':'training','event_id':'a1','sha256':preview['sha256']})
            item_id=queued['result']['id']
            await post({'action':'data_send','id':item_id},400)
            withdrawn=await post({'action':'data_withdraw','id':item_id})
            assert 'payload' not in withdrawn['result']
            await post({'action':'data_settings','training':'yes'},400)
            await post({'action':'not-real'},400)
            updater=services(hub)[2]
            assert not updater.busy()
            hub.workspace_jobs=SimpleNamespace(records={'x':{'state':'running'}})
            assert updater.busy()
            hub.workspace_jobs.records.clear()
            hub.teams._records['queued']={'status':'queued'}
            assert updater.busy()
            hub.teams._records.clear()
    asyncio.run(run())
