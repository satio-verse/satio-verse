import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
import pytest
from aiohttp.test_utils import TestClient, TestServer
from wm.agent.extensions import ExtensionRegistry
from wm.platform_services import ContributionStore, CommunityStore, ReleaseUpdater, canonical, digest, redact, remote_json
from tools.community_server import create_app


def sample_session():
    return SimpleNamespace(events=[{'id':'a','type':'user','text':'hello user@example.com'},
        {'message_id':'b','type':'assistant','text':'Here is sk-abcdefghijklmnop','feedback':{'value':'good','note':'user@example.com'}},
        {'id':'tool','type':'tool','text':'private output'}, {'id':'c','type':'user','text':'later'},
        {'id':'d','type':'assistant','text':'later reply'}], stats={'cost':1,'steps':3,'secret':'no'})


def test_consent_separate_preview_and_revoke(tmp_path):
    store=ContributionStore(tmp_path)
    assert not store.settings()['training'] and not store.settings()['diagnostics']
    preview=store.preview(sample_session(),'training','b')
    assert len(preview['payload']['messages']) == 2 and preview['event_id']=='b'
    assert 'user@example.com' not in str(preview) and 'abcdefghijklmnop' not in str(preview)
    with pytest.raises(ValueError,match='开启'):store.queue(preview,preview['sha256'])
    store.configure({'training':True})
    with pytest.raises(ValueError,match='变化'):store.queue(preview,'bad')
    item=store.queue(preview,preview['sha256'])
    assert item['state']=='queued'
    with pytest.raises(ValueError,match='配置'):asyncio.run(store.send(item['id']))
    store.configure({'training':False})
    assert store.item(item['id'])['state']=='withdrawn' and 'payload' not in store.item(item['id'])
    assert not store.settings()['diagnostics']


def test_diagnostic_whitelist_and_redaction(tmp_path):
    store=ContributionStore(tmp_path)
    assert store.preview(sample_session(),'diagnostics')['payload']['stats']=={'steps':3,'cost':1}
    assert redact({'api_key':'secret','text':'C:\\Users\\private\\file.txt'})=={'api_key':'[已移除]','text':'[已脱敏]'}
    with pytest.raises(ValueError):store.configure({'training':1})
    with pytest.raises(ValueError):store.preview(sample_session(),'training','missing')


def registry(tmp_path):
    reg=ExtensionRegistry(tmp_path,settings_path=False)
    reg.create({'id':'sample','name':'Sample','description':'A skill','files':{'SKILL.md':'# Hi'}})
    return reg


def test_creator_export_install_and_tamper(tmp_path):
    source=CommunityStore(tmp_path/'source',registry(tmp_path/'registry'))
    result=source.export('sample','author','1.0.0')
    assert not result['published'] and len(result['entry']['sha256'])==64
    target=CommunityStore(tmp_path/'target',ExtensionRegistry(tmp_path/'other',settings_path=False))
    index=str(Path(result['path']).parent/'index.json')
    sid=target.add_source(index)[0]['id']
    assert len(asyncio.run(target.refresh(sid)))==1
    installed=asyncio.run(target.install(sid,'sample','1.0.0'))
    assert installed['enabled'] is False
    Path(result['path']).write_text('{}')
    with pytest.raises(ValueError,match='哈希'):asyncio.run(target.install(sid,'sample','1.0.0'))


def test_export_version_immutable(tmp_path):
    store=CommunityStore(tmp_path,registry(tmp_path/'r'))
    store.export('sample','one','1.0.0')
    with pytest.raises(ValueError,match='版本'):store.export('sample','other','1.0.0')
    for version in ('../x','bad','1.0'):
        with pytest.raises(ValueError):store.export('sample','author',version)


def bundle(name='desktop/web/test.js', value=b'new'):
    return {'worldmodel_release':1,'version':'1.2.3','files':[{'path':name,'sha256':digest(value),'content':base64.b64encode(value).decode()}]}


def updater(tmp_path, release=None, busy=lambda:False):
    release=release or bundle()
    path=tmp_path/'release.json';path.write_bytes(canonical(release))
    up=ReleaseUpdater(tmp_path/'home',tmp_path/'install',busy)
    up.trust(str(path),digest(canonical(release)))
    asyncio.run(up.stage_source())
    return up


def test_update_hot_apply_and_rollback(tmp_path):
    target=tmp_path/'install/desktop/web/test.js';target.parent.mkdir(parents=True);target.write_bytes(b'old')
    up=updater(tmp_path)
    assert up.apply()['state']=='applied' and target.read_bytes()==b'new'
    with pytest.raises(ValueError,match='没有待应用'):up.apply()
    assert up.rollback()['state']=='rolled_back' and target.read_bytes()==b'old'


def test_update_busy_cold_and_modified_rollback(tmp_path):
    up=updater(tmp_path,busy=lambda:True)
    with pytest.raises(ValueError,match='执行'):up.apply()
    up.busy=lambda:False
    up.apply();(tmp_path/'install/desktop/web/test.js').write_bytes(b'user edit')
    with pytest.raises(ValueError,match='修改'):up.rollback()
    up=updater(tmp_path,bundle('wm/test.py',b'pass'))
    assert up.apply()['state']=='restart_required'
    assert not (tmp_path/'install/wm/test.py').exists()
    up.apply(offline=True)
    with pytest.raises(ValueError,match='离线'):up.rollback()
    up.rollback(offline=True)


@pytest.mark.parametrize('path',['../secrets','desktop/web/../../keys','desktop/web/CON.js','vault/x.py','wm/secret.json'])
def test_release_path_restrictions(tmp_path,path):
    with pytest.raises(ValueError):updater(tmp_path,bundle(path))


def test_update_tamper_rechecked_at_apply(tmp_path):
    up=updater(tmp_path)
    (up.root/'staged.json').write_bytes(canonical(bundle(value=b'tampered')))
    with pytest.raises(ValueError,match='信任'):up.apply()


def test_update_failed_transaction_restores_all_files(tmp_path,monkeypatch):
    import wm.platform_services as module
    release=bundle()
    release['files'].append(bundle('desktop/web/second.js')['files'][0])
    up=updater(tmp_path,release)
    target=tmp_path/'install/desktop/web/test.js';target.parent.mkdir(parents=True);target.write_bytes(b'old')
    replace=module.os.replace
    def fail_second(src,dst):
        if str(dst).endswith('second.js') and '.update-' in str(src):raise OSError('simulated lock')
        return replace(src,dst)
    monkeypatch.setattr(module.os,'replace',fail_second)
    with pytest.raises(OSError):up.apply()
    assert target.read_bytes()==b'old' and not (target.parent/'second.js').exists()


def test_remote_disallows_loopback_and_http():
    with pytest.raises(ValueError):asyncio.run(remote_json('http://example.org'))
    with pytest.raises(ValueError,match='公网'):asyncio.run(remote_json('https://127.0.0.1'))


def test_registry_real_http_auth_and_contribution_deletion(tmp_path):
    async def run():
        app=create_app(tmp_path/'server','https://example.org',{digest(b'alice-token'):'alice',digest(b'bob-token'):'bob'},b'x'*32)
        async with TestClient(TestServer(app)) as client:
            package=CommunityStore(tmp_path/'client',registry(tmp_path/'reg')).export('sample','Name','1.0.0')['package']
            assert (await client.post('/packages',json=package)).status==401
            response=await client.post('/packages',json=package,headers={'Authorization':'Bearer alice-token'})
            assert response.status==200
            rows=(await (await client.get('/index.json')).json())['packages']
            assert rows[0]['author']=='alice'
            assert (await client.post('/packages',json=package,headers={'Authorization':'Bearer bob-token'})).status==403
            package['spec']['files']['SKILL.md']='# changed'
            assert (await client.post('/packages',json=package,headers={'Authorization':'Bearer alice-token'})).status==409
            store=ContributionStore(tmp_path/'contrib')
            store.configure({'training':True,'endpoint':'https://example.org'})
            preview=store.preview(sample_session(),'training','b');item=store.queue(preview,preview['sha256'])
            async def transport(url,method='GET',body=None,token=''):
                path='/' + url.split('https://example.org/')[1]
                response=await client.request(method,path,json=body,headers={'Authorization':'Bearer '+token} if token else {})
                assert response.status==200
                return await response.json()
            await store.send(item['id'],transport)
            assert store.item(item['id'])['state']=='sent'
            assert 'payload' not in store.item(item['id'])
            stolen={'client_id':item['id'],'client_secret':'a'*64,'sha256':item['sha256'],'payload':item['payload']}
            assert (await client.post('/contributions',json=stolen)).status==409
            assert (await client.delete('/contributions/'+item['id'])).status==401
            await store.delete_remote(item['id'],transport)
            assert store.item(item['id'])['state']=='deleted'
            assert not (tmp_path/'server/contributions'/f'{item["id"]}.json').exists()
    asyncio.run(run())
