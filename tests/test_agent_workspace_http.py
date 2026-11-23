"""Workspace browser and real owned Windows shells; all files live in tmp_path."""
import asyncio
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.governance import BlacklistStore
from wm.agent.workspace_http import WorkspaceJobs, inspect_files, file_path, workspace, MAX_OUTPUT
from wm.platform_http import services


@pytest.fixture
def hub(tmp_path):
    root=tmp_path/'workspace';root.mkdir()
    sessions={sid:SimpleNamespace(id=sid,workspace=str(root),mode='unrestricted',preset='standard',status='idle',task=None) for sid in ('a','b')}
    return SimpleNamespace(get=sessions.get,sessions=sessions,blacklist=BlacklistStore(tmp_path/'state',defaults=False),home=tmp_path/'agent',extensions=None)


def test_paths_limits_decoding_pagination_and_blacklist(hub,tmp_path):
    session=hub.get('a');root=Path(session.workspace)
    for value in ('../outside',str(tmp_path/'outside'), '\x00'):
        with pytest.raises(ValueError):file_path(session,value)
    (root/'readme.md').write_text('# 中文',encoding='utf-8')
    (root/'utf16.txt').write_text('内容',encoding='utf-16')
    (root/'big.txt').write_bytes(b'x'*(1024*1024+1))
    assert inspect_files(session,'readme.md')['markdown']
    assert inspect_files(session,'utf16.txt')['text']=='内容'
    with pytest.raises(ValueError,match='超过'):inspect_files(session,'big.txt')
    hub.blacklist.add({'kind':'path','value':str(root/'readme.md'),'operations':'read'})
    with pytest.raises(PermissionError):inspect_files(session,'readme.md',blacklist=hub.blacklist)
    assert 'readme.md' not in [row['name'] for row in inspect_files(session,blacklist=hub.blacklist)['entries']]
    for i in range(205):(root/f'file-{i:03}.txt').touch()
    page=inspect_files(session,blacklist=hub.blacklist)
    assert len(page['entries'])==200 and page['next_offset']==200
    assert len(inspect_files(session,offset=200,blacklist=hub.blacklist)['entries'])==7


def test_symlink_escape(hub,tmp_path):
    outside=tmp_path/'outside';outside.mkdir();(outside/'secret').write_text('not visible')
    link=Path(hub.get('a').workspace)/'link'
    try:link.symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('Windows symlink privilege unavailable')
    with pytest.raises(ValueError):inspect_files(hub.get('a'),'link/secret')
    assert not inspect_files(hub.get('a'))['entries']


def test_readonly_blacklist_and_start_recheck(hub):
    async def run():
        jobs=WorkspaceJobs(hub);s=hub.get('a')
        s.mode='read'
        with pytest.raises(ValueError,match='只读'):jobs.start(s,'Write-Output test')
        s.mode='unrestricted';s.preset='plan'
        with pytest.raises(ValueError):jobs.start(s,'Write-Output test')
        s.preset='standard'
        hub.blacklist.add({'kind':'command','value':'Write-Output'})
        with pytest.raises(PermissionError):jobs.start(s,'Write-Output test')
        job=jobs.start(s,'Get-Date')
        s.mode='read'  # mutation before scheduled coroutine creates the process
        await jobs.tasks[job['id']]
        assert jobs.records[job['id']]['state']=='failed' and '只读' in jobs.records[job['id']]['error']
        await jobs.close()
        with pytest.raises(ValueError,match='关闭'):jobs.start(s,'Get-Date')
    asyncio.run(run())


windows_shell=pytest.mark.skipif(os.name!='nt' or not shutil.which('powershell'),reason='Windows PowerShell required')


@windows_shell
def test_real_shell_exit_utf8_bounded_output_and_nonzero(hub):
    async def run():
        jobs=WorkspaceJobs(hub)
        try:
            job=jobs.start(hub.get('a'),"[Console]::Write(('界' * 210005)); exit 0")
            await asyncio.wait_for(jobs.tasks[job['id']],20)
            row=jobs.records[job['id']]
            assert row['state']=='completed' and row['exit_code']==0
            assert row['truncated'] and len(row['output'])==MAX_OUTPUT and set(row['output'])=={'界'}
            job=jobs.start(hub.get('a'),"Write-Output 'failure output'; exit 7")
            await asyncio.wait_for(jobs.tasks[job['id']],20)
            row=jobs.records[job['id']]
            assert row['state']=='failed' and row['exit_code']==7 and 'failure output' in row['output']
        finally:await jobs.close()
    asyncio.run(run())


@windows_shell
def test_real_stop_ownership_and_cleanup(hub):
    async def run():
        jobs=WorkspaceJobs(hub)
        hub.workspace_jobs=jobs
        updates=services(hub)[2]
        try:
            job=jobs.start(hub.get('a'),"Write-Output 'ready'; Start-Sleep -Seconds 60")
            assert updates.busy()
            for _ in range(100):
                if 'ready' in jobs.records[job['id']]['output']:break
                await asyncio.sleep(.05)
            assert 'ready' in jobs.records[job['id']]['output']
            with pytest.raises(ValueError):jobs.stop('b',job['id'])
            assert not jobs.stops[job['id']].is_set()
            jobs.stop('a',job['id'])
            await asyncio.wait_for(jobs.tasks[job['id']],15)
            assert jobs.records[job['id']]['state']=='cancelled'
            assert not updates.busy()
            second=jobs.start(hub.get('b'),'Start-Sleep -Seconds 60')
            await asyncio.wait_for(jobs.close(),15)
            assert jobs.records[second['id']]['state']=='cancelled'
            assert all(task.done() for task in jobs.tasks.values())
        finally:await jobs.close()
    asyncio.run(run())


@windows_shell
def test_creation_cancellation_keeps_process_handle_for_cleanup(hub,monkeypatch):
    import psutil
    async def run():
        real=asyncio.create_subprocess_exec
        launched=asyncio.Event();release=asyncio.Event();handles=[]
        async def delayed(*args,**kwargs):
            process=await real(*args,**kwargs)
            if args[0]=='powershell':
                handles.append(process);launched.set();await release.wait()
            return process
        monkeypatch.setattr(asyncio,'create_subprocess_exec',delayed)
        jobs=WorkspaceJobs(hub)
        job=jobs.start(hub.get('a'),'Start-Sleep -Seconds 60')
        await asyncio.wait_for(launched.wait(),10)
        jobs.tasks[job['id']].cancel();release.set()
        await asyncio.gather(jobs.tasks[job['id']],return_exceptions=True)
        assert jobs.records[job['id']]['state']=='cancelled'
        assert handles[0].returncode is not None and not psutil.pid_exists(handles[0].pid)
        await jobs.close()
    asyncio.run(run())


@windows_shell
def test_shell_exit_terminates_owned_detached_child(hub):
    import psutil
    async def run():
        jobs=WorkspaceJobs(hub)
        command="$info = New-Object Diagnostics.ProcessStartInfo; $info.FileName = 'powershell'; $info.Arguments = '-NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 60\"'; $info.UseShellExecute = $false; $info.CreateNoWindow = $true; $child = [Diagnostics.Process]::Start($info); Write-Output ('CHILD:' + $child.Id); exit 0"
        try:
            job=jobs.start(hub.get('a'),command)
            await asyncio.wait_for(jobs.tasks[job['id']],20)
            row=jobs.records[job['id']]
            assert row['state']=='completed',row
            match=re.search(r'CHILD:(\d+)',row['output']);assert match
            pid=int(match[1])
            for _ in range(50):
                if not psutil.pid_exists(pid):break
                await asyncio.sleep(.05)
            assert not psutil.pid_exists(pid)
        finally:await jobs.close()
    asyncio.run(run())


def test_terminal_record_retention_is_bounded(hub):
    async def run():
        jobs=WorkspaceJobs(hub)
        for i in range(250):
            key=str(i)
            task=asyncio.create_task(asyncio.sleep(0));await task
            jobs.records[key]={'state':'completed','session':'a'};jobs.tasks[key]=task;jobs.stops[key]=asyncio.Event()
        jobs._prune()
        assert len(jobs.records)==20 and len(jobs.tasks)==20 and len(jobs.stops)==20
        await jobs.close()
    asyncio.run(run())


def test_workspace_http_enforces_read_blacklist_and_session_scope(hub):
    async def run():
        root=Path(hub.get('a').workspace);(root/'blocked.txt').write_text('private')
        hub.blacklist.add({'kind':'path','value':str(root/'blocked.txt'),'operations':'read'})
        app=web.Application()
        async def handle(request):return await workspace(request,hub)
        app.router.add_route('*','/workspace',handle)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/workspace',params={'id':'a','path':'../'})).status==400
            assert (await client.get('/workspace',params={'id':'a','path':'blocked.txt'})).status==400
            assert (await client.get('/workspace',params={'id':'missing'})).status==400
            assert (await client.post('/workspace',json={'id':'b','action':'stop','job_id':'missing'})).status==400
            assert (await client.get('/workspace',params={'id':'a'})).status==200
        await hub.workspace_jobs.close()
    asyncio.run(run())
