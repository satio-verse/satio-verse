import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.terminal import OSCState, TerminalManager, dimensions, terminal_environment
from wm.agent.terminal_http import terminal


def hub_fixture(tmp_path):
    session = SimpleNamespace(id='test-session', workspace=str(tmp_path), preset='standard', mode='full', archived=False)
    other = SimpleNamespace(**{**vars(session), 'id': 'other-session'})
    hub = SimpleNamespace(get=lambda sid: {session.id: session, other.id: other}.get(sid),
                          blacklist=SimpleNamespace(check=lambda *args: None))
    return hub, session, other


class FakePTY:
    def __init__(self, cwd, cols, rows):
        self.closed, self.input, self.size = False, [], (cols, rows)
        self.chunks = []
    def read(self):
        return self.chunks.pop(0) if self.chunks else ''
    def alive(self):
        return not self.closed
    def exit_code(self):
        return 0
    def write(self, data):
        self.input.append(data)
    def resize(self, cols, rows):
        self.size = cols, rows
    def close(self):
        self.closed = True


def test_osc_fragmented_progress_cwd_and_untrusted_payload():
    row = dict(command='', state='waiting', sequence=44, prompt_seq=0, commands=[], cwd='', progress=None)
    parser = OSCState(row)
    parser.feed('\x1b]777;worldmodel;{"command":"test"}\x07\x1b]133;C\x07\x1b]9;4;1;')
    parser.feed('65\x1b\\\x1b]7;file:///E:/some%20folder\x07')
    assert row['state'] == 'running' and row['progress'] == 65 and row['cwd'] == 'E:/some folder'
    parser.feed('\x1b]133;D;2\x07\x1b]133;A\x07')
    assert row['state'] == 'failed' and row['at_prompt'] and row['commands'][0]['exit_code'] == 2
    parser.feed('\x1b]777;worldmodel;{"state":"closed","progress":10000}\x07')
    assert row['state'] == 'failed'
    parser.feed('\x1b]' + 'x' * 20000)
    assert not parser.pending


def test_startup_script_reported_as_command_does_not_look_running():
    """PSReadLine 把开终端时注入的那段脚本当成第一条历史交给 AddToHistoryHandler: 不能因此显示"在跑"
    (2026-11-03 PowerShell1 停在提示符, 看板"在做"里挂了 9 小时, 命令一栏是 [Console]::OutputEncoding = …)."""
    row = dict(command='', state='waiting', sequence=0, prompt_seq=1, at_prompt=True, commands=[], cwd='', progress=None)
    parser = OSCState(row)
    startup = json.dumps({'command': '\n#wm-terminal-integration\n[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)'})
    parser.feed('\x1b]777;worldmodel;' + startup + '\x07\x1b]133;C\x07')
    assert row['state'] == 'waiting' and row['command'] == '' and row['at_prompt']
    parser.feed('\x1b]777;worldmodel;{"command":"dir"}\x07\x1b]133;C\x07')
    assert row['state'] == 'running' and row['command'] == 'dir'


def test_psp_04_hierarchy_base64_clear_and_invalid_reports():
    row = dict(command='build', state='running', at_prompt=False)
    parser = OSCState(row)
    parser.feed('\x1b]7501;state=working:id=build/test:progress=40:app=cmake:title=VVMgRWFzdA==:msg=UHVzaGluZyBpbWFnZQ==\x1b\\')
    assert row['progress'] == 40 and row['notice'] == 'Pushing image'
    parser.feed('\x1b]7501;state=blocked:id=build/approval:kind=permission\x07')
    assert row['state'] == 'waiting' and len(row['reports']) == 2
    parser.feed('\x1b]7501;state=error:id=evil:msg=AA==\x07')
    assert row['state'] == 'waiting' and len(row['reports']) == 2
    parser.feed('\x1b]7501;state=clear:id=build/approval\x07')
    assert row['state'] == 'running' and row['progress'] == 40
    parser.feed('\x1b]7501;state=clear:id=build\x07')
    assert not row['reports'] and row['state'] == 'running'


def test_terminal_env_strips_credentials_and_preloads(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'secret-canary')
    monkeypatch.setenv('NODE_OPTIONS', '--require evil.js')
    monkeypatch.setenv('PYTHONSTARTUP', 'evil.py')
    env = terminal_environment()
    assert 'OPENAI_API_KEY' not in env and 'NODE_OPTIONS' not in env and 'PYTHONSTARTUP' not in env
    assert env['TERM'] == 'xterm-256color'


def test_notifications_ignore_control_or_chunked_packets():
    row = {'notice': ''}
    parser = OSCState(row)
    parser.feed('\x1b]9;4;1;40\x07\x1b]9;8;run.exe\x07\x1b]99;p=close;bad\x07')
    assert not row['notice']
    parser.feed('\x1b]99;i=build:p=title;Build complete\x07')
    assert row['notice'] == 'Build complete'
    parser.feed('\x1b]99;d=0;unfinished\x07\x1b]99;e=1;YmFk\x07')
    assert row['notice'] == 'Build complete'


@pytest.mark.parametrize('cols,rows', [(1, 24), (80, 1), (False, 10), (501, 40)])
def test_dimensions_invalid(cols, rows):
    with pytest.raises(ValueError):
        dimensions(cols, rows)


def test_multi_session_stream_cursor_ownership_revocation_and_close(tmp_path, monkeypatch):
    async def main():
        hub, session, other = hub_fixture(tmp_path)
        manager = TerminalManager(hub, FakePTY)
        monkeypatch.setattr('wm.agent.terminal.MAX_OUTPUT', 8)
        a = await manager.create(session)
        b = await manager.create(other)
        with pytest.raises(ValueError):
            await manager.input(other, a['id'], 'wrong')
        manager.processes[a['id']].chunks.append('abcdefghijk')
        await asyncio.sleep(.1)
        output = manager.output(session, a['id'], 0)
        assert output['output'] == 'defghijk' and output['reset'] and output['next_cursor'] == 11
        assert manager.output(session, a['id'], 11)['output'] == ''
        await manager.input(session, a['id'], 'hello')
        await manager.resize(session, a['id'], 123, 31)
        assert manager.processes[a['id']].size == (123, 31)
        session.mode = 'read'
        with pytest.raises(PermissionError):
            await manager.input(session, a['id'], 'blocked')
        await manager.interrupt(session, a['id'])
        assert manager.processes[a['id']].input[-1] == '\x03'
        await manager.terminate(session, a['id'])
        assert manager.records[a['id']]['state'] == 'closed'
        await manager.close()
        assert all(p.closed for p in manager.processes.values())
    asyncio.run(main())


def test_blacklist_and_plan_mode_prevent_shell_start(tmp_path):
    async def main():
        hub, session, _ = hub_fixture(tmp_path)
        manager = TerminalManager(hub, FakePTY)
        hub.blacklist.check = lambda *a: SimpleNamespace(why='blocked')
        with pytest.raises(PermissionError, match='blocked'):
            await manager.create(session)
        assert not manager.processes
        hub.blacklist.check = lambda *a: None
        session.preset = 'plan'
        with pytest.raises(PermissionError):
            await manager.create(session)
    asyncio.run(main())


def test_immediate_close_and_cancelled_creation_leave_no_process(tmp_path):
    async def main():
        import threading
        hub, session, _ = hub_fixture(tmp_path)
        manager = TerminalManager(hub, FakePTY)
        row = await manager.create(session)
        await manager.close()
        assert manager.processes[row['id']].closed
        created, started, release = [], threading.Event(), threading.Event()
        def factory(*args):
            started.set()
            release.wait(5)
            proc = FakePTY(*args)
            created.append(proc)
            return proc
        other = TerminalManager(hub, factory)
        task = asyncio.create_task(other.create(session))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(created) == 1 and created[0].closed and not other.records
    asyncio.run(main())


def test_http_input_validation_and_ownership(tmp_path):
    async def main():
        hub, session, other = hub_fixture(tmp_path)
        hub.terminals = TerminalManager(hub, FakePTY)
        app = web.Application()
        async def handler(request):
            return await terminal(request, hub)
        app.router.add_route('*', '/', handler)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/', json=[])).status == 400
            r = await client.post('/', json={'id': session.id, 'action': 'create'})
            tid = (await r.json())['terminal']['id']
            response = await client.get('/', params={'id': session.id})
            assert len((await response.json())['terminals']) == 1
            assert (await client.post('/', json={'id': other.id, 'action': 'close', 'terminal_id': tid})).status == 400
            assert (await client.get('/', params={'id': session.id, 'terminal_id': tid, 'after': '-1'})).status == 400
            await hub.terminals.close()
    asyncio.run(main())


def test_terminal_route_rejects_foreign_origin_and_host(tmp_path):
    from wm.app import AGENT, CFG, api_agent_terminal
    async def main():
        hub, session, _ = hub_fixture(tmp_path)
        hub.terminals = TerminalManager(hub, FakePTY)
        app = web.Application()
        app[AGENT], app[CFG] = hub, {'server': {}}
        app.router.add_route('*', '/terminal', api_agent_terminal)
        async with TestClient(TestServer(app)) as client:
            payload = {'id': session.id, 'action': 'create'}
            assert (await client.post('/terminal', json=payload, headers={'Origin': 'https://untrusted.example'})).status == 403
            assert (await client.get('/terminal', params={'id': session.id}, headers={'Host': 'untrusted.example'})).status == 403
            assert not hub.terminals.records
            assert (await client.get('/terminal', params={'id': session.id})).status == 200
            await hub.terminals.close()
    asyncio.run(main())


def test_terminal_notifications_coalesce_and_cancel_on_shutdown(tmp_path):
    async def main():
        hub, session, _ = hub_fixture(tmp_path)
        events = []
        async def notify(payload):
            events.append(payload)
        hub.e = SimpleNamespace(channels=SimpleNamespace(notify=notify))
        manager = TerminalManager(hub, FakePTY)
        row = await manager.create(session)
        await manager._notify(row)
        manager._notice_at[row['id']] = time.monotonic() - 2.98
        row['state'] = 'failed'
        await manager._notify(row)
        row['state'] = 'waiting'
        await manager._notify(row)
        assert len(events) == 1 and len(manager._notice_pending) == 1
        await asyncio.sleep(.06)
        assert len(events) == 2 and events[-1]['state'] == 'waiting'
        await manager._notify(row)
        assert manager._notice_pending
        await manager.close()
        assert not manager._notice_pending
    asyncio.run(main())


@pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY integration')
def test_real_conpty_powershell_readhost_cwd_interrupt_resize_and_exit(tmp_path):
    pytest.importorskip('winpty')
    async def main():
        hub, session, _ = hub_fixture(tmp_path)
        manager = TerminalManager(hub)
        row = await manager.create(session)
        tid = row['id']
        cursor = 0
        async def until(predicate, timeout=20):
            nonlocal cursor
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                data = manager.output(session, tid, cursor)
                cursor = data['next_cursor']
                # Emulate the terminal's DSR reply in this headless integration.
                if '\x1b[6n' in data['output']:
                    await manager.input(session, tid, '\x1b[1;1R')
                if predicate(manager.records[tid]):
                    return
                await asyncio.sleep(.05)
            raise AssertionError(str(manager.metadata(manager.records[tid])) + '\n' + repr(manager.records[tid]['output'][-3000:]))
        try:
            await until(lambda r: r['prompt_seq'] >= 1)
            await manager.input(session, tid, "$v=Read-Host 'NAME'; Write-Output ('RESULT-'+$v)\r")
            await until(lambda r: 'NAME:' in r['output'])
            await manager.input(session, tid, '绫音✓\r')
            await until(lambda r: 'RESULT-绫音✓' in r['output'] and r['prompt_seq'] >= 2)
            subdir = tmp_path / 'sub folder'
            subdir.mkdir()
            before = manager.records[tid]['prompt_seq']
            await manager.input(session, tid, "Set-Location 'sub folder'\r")
            await until(lambda r: r['prompt_seq'] > before)
            assert Path(manager.records[tid]['cwd']) == subdir
            before = manager.records[tid]['prompt_seq']
            await manager.input(session, tid, 'NotAnExistingCommandForTerminalTest\r')
            await until(lambda r: r['prompt_seq'] > before)
            assert manager.records[tid]['last_exit_code'] != 0
            await manager.resize(session, tid, 112, 30)
            before = manager.records[tid]['prompt_seq']
            await manager.input(session, tid, 'Start-Sleep -Seconds 40\r')
            await asyncio.sleep(.5)
            await manager.interrupt(session, tid)
            await until(lambda r: r['prompt_seq'] > before, 10)
            await manager.input(session, tid, 'exit 7\r')
            await until(lambda r: r['state'] == 'closed')
            assert manager.records[tid]['exit_code'] == 7
        finally:
            await manager.close()
    from pathlib import Path
    # 跑测试的进程要是被带 CREATE_NEW_PROCESS_GROUP 起的 (正式服务端就是这样起的), 子进程会不理 Ctrl+C, 中断那一步过不了:
    # 照服务端那样先清掉 (见 test_interrupt_works_when_started_like_the_real_server), 测完把 Python 的 Ctrl+C 处理放回去
    import signal
    from wm.agent.terminal import let_children_take_ctrl_c
    old = signal.getsignal(signal.SIGINT)
    let_children_take_ctrl_c()
    try:
        asyncio.run(main())
    finally:
        signal.signal(signal.SIGINT, old)


@pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY + process group')
def test_interrupt_works_when_started_like_the_real_server(tmp_path):
    """看守用 CREATE_NEW_PROCESS_GROUP 起正式服务端, Windows 会让它和它起的子进程都不理 Ctrl+C, 工作台终端的"中断"停不下
    正在跑的命令 (2026-11-01 复现: 同一条测试普通子进程过、带这个标记失败). 照看守那样起一个子进程, 调
    let_children_take_ctrl_c (wm/__main__.py 在看守起的服务端里调), 终端里跑 Start-Sleep 40 再中断, 10 秒内要回到提示符."""
    pytest.importorskip('winpty')
    import subprocess
    import sys
    from pathlib import Path
    script = r'''
import asyncio, sys, time
from types import SimpleNamespace
from wm.agent.terminal import TerminalManager, let_children_take_ctrl_c
let_children_take_ctrl_c()
session = SimpleNamespace(id='s', workspace=sys.argv[1], preset='standard', mode='full', archived=False)
hub = SimpleNamespace(get=lambda sid: session if sid == 's' else None, blacklist=SimpleNamespace(check=lambda *a: None))

async def main():
    m = TerminalManager(hub)
    tid = (await m.create(session))['id']
    cursor = 0
    async def until(pred, timeout):
        nonlocal cursor
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = m.output(session, tid, cursor)
            cursor = data['next_cursor']
            if '\x1b[6n' in data['output']:
                await m.input(session, tid, '\x1b[1;1R')
            if pred(m.records[tid]):
                return True
            await asyncio.sleep(.05)
        return False
    try:
        assert await until(lambda r: r['prompt_seq'] >= 1, 30), 'no prompt'
        before = m.records[tid]['prompt_seq']
        await m.input(session, tid, 'Start-Sleep -Seconds 40\r')
        await asyncio.sleep(.8)
        await m.interrupt(session, tid)
        print('INTERRUPTED' if await until(lambda r: r['prompt_seq'] > before, 10) else 'STILL-RUNNING')
    finally:
        await m.close()

asyncio.run(main())
'''
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, '-c', script, str(tmp_path)], cwd=str(root), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=120, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    assert 'INTERRUPTED' in r.stdout, (r.stdout[-800:], r.stderr[-2000:])


@pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY lifecycle')
def test_real_terminal_close_kills_owned_shell_and_descendant(tmp_path):
    pytest.importorskip('winpty')
    import psutil
    import re
    async def main():
        hub, session, _ = hub_fixture(tmp_path)
        manager = TerminalManager(hub)
        row = await manager.create(session)
        tid = row['id']
        shell_pid = manager.processes[tid].pty.pid
        try:
            deadline = time.monotonic() + 15
            while manager.records[tid]['prompt_seq'] < 1:
                assert time.monotonic() < deadline
                if '\x1b[6n' in manager.records[tid]['output']:
                    await manager.input(session, tid, '\x1b[1;1R')
                await asyncio.sleep(.05)
            await manager.input(session, tid, "$p=Start-Process powershell.exe -ArgumentList '-NoProfile -Command Start-Sleep 60' -WindowStyle Hidden -PassThru; Write-Output ('CHILD_PID='+$p.Id)\r")
            child = None
            while not child:
                assert time.monotonic() < deadline
                match = re.search(r'CHILD_PID=(\d+)', manager.records[tid]['output'])
                if match:
                    child = psutil.Process(int(match[1]))
                await asyncio.sleep(.05)
            assert child.is_running()
            await manager.terminate(session, tid)
            child.wait(5)
            assert not psutil.pid_exists(shell_pid)
            assert manager.processes[tid].pty is None
        finally:
            await manager.close()
    asyncio.run(main())


@pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY')
def test_real_fresh_terminal_waits_at_prompt_instead_of_running(tmp_path):
    """真开一个终端什么都不敲: 停在提示符就是"等待操作", 命令一栏是空的 (以前 0.7 秒后就变成"在跑", 命令是启动脚本);
    敲一条命令以后照常记下这条命令和退出码. 记下最后一次有输出的时间 (统一状态据此写"多久没有新输出")."""
    pytest.importorskip('winpty')
    async def main():
        hub, session, _ = hub_fixture(tmp_path)
        manager = TerminalManager(hub)
        tid = (await manager.create(session))['id']
        row = manager.records[tid]
        cursor = 0
        async def pump(seconds, until=None):
            nonlocal cursor
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                data = manager.output(session, tid, cursor)
                cursor = data['next_cursor']
                if '\x1b[6n' in data['output']:
                    await manager.input(session, tid, '\x1b[1;1R')
                if until and until(row):
                    return True
                await asyncio.sleep(.05)
            return False
        try:
            assert await pump(30, lambda r: r['prompt_seq'] >= 1), manager.metadata(row)
            await pump(3)  # 以前就是在这之后变成"在跑"
            assert row['state'] == 'waiting' and row['command'] == '' and not row['commands'], manager.metadata(row)
            assert row['last_output'] and row['last_output'] >= row['started']
            await manager.input(session, tid, 'Write-Output fresh-ok\r')
            assert await pump(20, lambda r: r['prompt_seq'] >= 2 and r['commands']), manager.metadata(row)
            assert row['commands'][-1]['command'] == 'Write-Output fresh-ok' and row['state'] == 'completed'
        finally:
            await manager.close()
    asyncio.run(main())
