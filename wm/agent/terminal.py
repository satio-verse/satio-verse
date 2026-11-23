"""User-operated persistent ConPTY sessions. This is a terminal, not a sandbox.

No model tool is registered here. Output is VT text; clients must render it with a
terminal emulator, never HTML. Closing a view does not close its owned shell.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import tools
from .workspace_http import _CommandJob

MAX_OUTPUT = 400_000
MAX_INPUT = 32_768
TERMINAL_STATES = {'waiting', 'running', 'completed', 'failed', 'closed'}


def let_children_take_ctrl_c() -> bool:
    """The supervisor starts the real server with CREATE_NEW_PROCESS_GROUP, which makes Windows ignore Ctrl-C in the
    server and in every process it starts, so interrupt() (Ctrl-C written to the ConPTY) could not stop a running
    command (2026-11-01: same test passes as a plain child, fails under that flag). Clear the inherited OS flag so
    shells get Ctrl-C again; the server itself keeps ignoring it at the Python level (set first, no gap). Main thread only."""
    if os.name != 'nt':
        return False
    import ctypes
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    return bool(ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False))


def dimensions(cols, rows):
    if isinstance(cols, bool) or isinstance(rows, bool):
        raise ValueError('终端尺寸无效')
    cols, rows = int(cols), int(rows)
    if not 20 <= cols <= 500 or not 5 <= rows <= 200:
        raise ValueError('终端尺寸应为 20–500 列、5–200 行')
    return cols, rows


def terminal_environment():
    # Interactive shell profiles and Node/Python preloads must not turn startup
    # into arbitrary inherited hooks, and model credentials stay in the server.
    env = tools._clean_env()
    for key in list(env):
        if key.upper() in {'NODE_OPTIONS', 'PYTHONSTARTUP', 'BASH_ENV', 'ENV', 'PSMODULEPATH'}:
            env.pop(key)
    env.update(TERM='xterm-256color', COLORTERM='truecolor')
    return env


# The first line marks this startup script. PSReadLine hands the -EncodedCommand script itself to AddToHistoryHandler
# when the first prompt reads input, so without the check below every fresh terminal reported this script as its
# command and sat in "running" until the user typed something (2026-11-03: PowerShell1 showed "在做" for 9 hours).
INTEGRATION_MARKER = '#wm-terminal-integration'
POWERSHELL_INTEGRATION = r'''
#wm-terminal-integration
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$global:wmHadCommand = $false
function global:wmOsc([string]$text) { [Console]::Write(([char]27).ToString()+']'+$text+([char]7)) }
function global:prompt {
    $ok = $?
    if ($global:wmHadCommand) {
        $code = if ($ok) { if ($global:LASTEXITCODE) { $global:LASTEXITCODE } else { 0 } } else { 1 }
        wmOsc ('133;D;'+$code)
        $global:wmHadCommand = $false
    }
    try { wmOsc ('7;'+([uri]$PWD.ProviderPath).AbsoluteUri) } catch {}
    wmOsc '133;A'
    'PS '+$PWD.Path+'> '
    wmOsc '133;B'
}
try {
    Import-Module PSReadLine -ErrorAction Stop
    Set-PSReadLineOption -HistorySaveStyle SaveNothing
    Set-PSReadLineOption -AddToHistoryHandler {
        param($line)
        if ($line -match '^\s*#wm-terminal-integration') { return $false }
        $global:wmHadCommand = $true
        $global:LASTEXITCODE = 0
        wmOsc ('777;worldmodel;'+(@{command=$line} | ConvertTo-Json -Compress))
        wmOsc '133;C'
        return $true
    }
} catch { wmOsc ('777;worldmodel;'+(@{notice='Shell command markers unavailable'} | ConvertTo-Json -Compress)) }
'''


class OSCState:
    """Bounded streaming metadata parser, independent of the terminal display."""
    def __init__(self, record):
        self.record = record
        self.pending = ''
        self.reports = {}
        self.skip_start = False  # the startup script was reported as a command: ignore its "command started" marker

    def psp(self, payload):
        """PSP 0.4 state reports (OSC 7501). Queries do not grant host actions."""
        if len(payload.encode('utf-8')) > 4096 or payload == '?':
            return
        fields = {}
        for field in payload.split(':'):
            key, sep, value = field.partition('=')
            if not sep or key in fields:
                return
            fields[key] = value
        state, ident = fields.get('state'), fields.get('id', '')
        if state not in {'idle', 'working', 'done', 'blocked', 'error', 'clear'}:
            return
        parts = ident.split('/') if ident else []
        if len(ident) > 128 or len(parts) > 8 or any(not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,32}', p) for p in parts):
            return
        if len(fields.get('app', '')) > 32:
            return
        decoded = {}
        for key, limit in (('title', 192), ('msg', 2048)):
            try:
                value = fields.get(key, '')
                raw = base64.b64decode(value + '=' * (-len(value) % 4), validate=True)
                label = raw.decode('utf-8')
                if len(raw) > limit or any(ord(c) < 32 or ord(c) == 127 for c in label):
                    return
                decoded[key] = label
            except (ValueError, UnicodeError):
                return
        progress = None
        if fields.get('progress') is not None:
            try:
                progress = int(fields['progress'])
            except ValueError:
                return
            if not 0 <= progress <= 100 or state not in {'working', 'blocked'}:
                return
        if state == 'clear':
            self.reports = {k: v for k, v in self.reports.items() if ident and k != ident and not k.startswith(ident + '/')}
        else:
            if ident not in self.reports and len(self.reports) >= 32:
                return
            self.reports[ident] = {'id': ident, 'state': state, 'progress': progress,
                                   'app': fields.get('app', ''), 'kind': fields.get('kind', ''), **decoded}
        row = self.record
        row['reports'] = list(self.reports.values())
        if self.reports:
            report = max(self.reports.values(), key=lambda x: {'error':5, 'blocked':4, 'working':3, 'done':2, 'idle':1}[x['state']])
            row.update(state={'error':'failed', 'blocked':'waiting', 'working':'running', 'done':'completed', 'idle':'waiting'}[report['state']],
                       progress=report['progress'], notice=report['msg'] or report['title'])
        else:
            row.update(state=('failed' if row.get('last_exit_code') else 'completed' if row.get('command') else 'waiting') if row.get('at_prompt') else 'running',
                       progress=None, notice='')

    def feed(self, data):
        text = self.pending + data
        self.pending = ''
        offset = 0
        while True:
            start = text.find('\x1b]', offset)
            if start < 0:
                if text.endswith('\x1b'):
                    self.pending = '\x1b'
                return
            match = re.search(r'\x07|\x1b\\', text[start + 2:])
            if not match:
                self.pending = text[start:] if len(text) - start <= 16384 else ''
                return
            end = start + 2 + match.start()
            self.apply(text[start + 2:end])
            offset = start + 2 + match.end()

    def apply(self, payload):
        row = self.record
        if payload.startswith('133;'):
            pieces = payload.split(';')
            marker = pieces[1]
            if marker == 'A':
                row['at_prompt'] = True
                row['prompt_seq'] += 1
                if not row['command']:
                    row['state'] = 'waiting'
            elif marker == 'C':
                if self.skip_start:
                    self.skip_start = False
                    return
                row.update(state='running', at_prompt=False, progress=None)
            elif marker == 'D':
                try:
                    code = int(pieces[2])
                except (ValueError, IndexError):
                    return
                row.update(state='completed' if code == 0 else 'failed', last_exit_code=code, progress=None)
                self.reports.clear()
                row['reports'] = []
                row['commands'].append({'command': row['command'], 'exit_code': code,
                                        'sequence': row['sequence'], 'at': time.time()})
                row['commands'] = row['commands'][-40:]
        elif payload.startswith('7;file://'):
            parsed = urlsplit(payload[2:])
            cwd = unquote(parsed.path)
            if re.match(r'^/[A-Za-z]:', cwd):
                cwd = cwd[1:]
            if cwd and len(cwd) <= 4096:
                row['cwd'] = cwd
        elif payload.startswith('7501;'):
            self.psp(payload[5:])
        elif payload.startswith('777;worldmodel;'):
            try:
                value = json.loads(payload[len('777;worldmodel;'):])
            except (ValueError, TypeError):
                return
            if not isinstance(value, dict):
                return
            if isinstance(value.get('command'), str) and INTEGRATION_MARKER in value['command'][:200]:
                self.skip_start = True  # a shell without the in-script check: this is the startup script, not a command
                return
            if isinstance(value.get('command'), str):
                row['command'] = value['command'][:4000]
            if value.get('state') in TERMINAL_STATES - {'closed'}:
                row['state'] = value['state']
            if isinstance(value.get('progress'), (int, float)) and 0 <= value['progress'] <= 100:
                row['progress'] = value['progress']
            if isinstance(value.get('notice'), str):
                row['notice'] = value['notice'][:500]
        elif payload.startswith('9;4;'):
            parts = payload.split(';')
            try:
                state = int(parts[2])
                percent = int(parts[3]) if len(parts) > 3 else None
            except ValueError:
                return
            if state == 0:
                row['progress'] = None
            elif state in {1, 2, 3, 4}:
                row['state'] = {1: 'running', 2: 'failed', 3: 'running', 4: 'waiting'}[state]
                row['progress'] = max(0, min(100, percent)) if percent is not None else None
        elif payload.startswith('777;notify;'):
            row['notice'] = payload[len('777;notify;'):].replace(';', ' · ')[:500]
        elif payload.startswith('99;'):
            # Basic Kitty notifications only; control/query/chunked packets are
            # not executable UI actions and must never be displayed as commands.
            meta, sep, message = payload[3:].partition(';')
            if not sep or len(meta) > 1024 or len(message.encode('utf-8')) > 2048:
                return
            fields = dict(field.split('=', 1) for field in meta.split(':') if '=' in field)
            if fields.get('p', 'title') not in {'title', 'body'} or fields.get('d') == '0' or fields.get('e') == '1':
                return
            if any(ord(c) < 32 or ord(c) == 127 for c in message):
                return
            row['notice'] = message[:500]
        elif payload.startswith('9;'):
            message = payload[2:]
            if not re.match(r'^\d+(?:;|$)', message):
                row['notice'] = message[:500]


class ConPTY:
    def __init__(self, cwd, cols, rows):
        from winpty import PTY, Backend
        try:
            self.pty = PTY(cols, rows, backend=Backend.ConPTY)
        except Exception as exc:
            raise ValueError('无法初始化 Windows ConPTY；需要 Windows 10 1809 或更新版本') from exc
        self.owned = None
        shell = shutil.which('powershell.exe')
        if not shell:
            raise ValueError('未找到 Windows PowerShell')
        encoded = base64.b64encode(POWERSHELL_INTEGRATION.encode('utf-16le')).decode()
        env = terminal_environment()
        cmdline = subprocess.list2cmdline(['-NoLogo', '-NoProfile', '-NoExit', '-EncodedCommand', encoded])
        if not self.pty.spawn(shell, cmdline=' ' + cmdline, cwd=str(cwd), env='\0'.join(f'{k}={v}' for k, v in env.items()) + '\0'):
            raise RuntimeError('无法创建 ConPTY Shell')
        try:
            self.owned = _CommandJob(self.pty.pid)
        except BaseException:
            # This is the freshly-created process, held by the PTY itself.
            import psutil
            with contextlib.suppress(Exception):
                psutil.Process(self.pty.pid).kill()
            raise

    def read(self):
        return self.pty.read(65536, blocking=False)

    def write(self, data):
        return self.pty.write(data)

    def resize(self, cols, rows):
        self.pty.set_size(cols, rows)

    def alive(self):
        return self.pty.isalive()

    def exit_code(self):
        return self.pty.get_exitstatus()

    def close(self):
        if self.owned:
            # Job close terminates asynchronously. Hold the actual process
            # handle before closing it, so shutdown waits for this shell (not a
            # later process that happens to reuse its PID).
            import ctypes
            from ctypes import wintypes as w
            kernel = self.owned.kernel
            kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
            kernel.WaitForSingleObject.restype = w.DWORD
            process = kernel.OpenProcess(0x100000, False, self.pty.pid)
            try:
                self.owned.close()
                self.owned = None
                if process and kernel.WaitForSingleObject(process, 5000) == 0x102:
                    raise RuntimeError('终端退出超时，请检查该终端的进程状态')
            finally:
                if process:
                    kernel.CloseHandle(process)
                self.pty = None
        self.pty = None  # Release ConPTY pipe/console handles, not just the shell.


class TerminalManager:
    def __init__(self, hub, factory=ConPTY):
        self.hub, self.factory = hub, factory
        self.records, self.processes, self.tasks = {}, {}, {}
        self._notice_at, self._notice_pending = {}, {}
        self.closed = False

    @staticmethod
    def availability():
        if os.name != 'nt':
            return {'supported': False, 'reason': '当前交互终端使用 Windows ConPTY；其他平台后端尚未接入'}
        if sys.getwindowsversion().build < 17763:
            return {'supported': False, 'reason': '交互终端需要 Windows 10 1809 或更新版本'}
        try:
            import winpty  # noqa: F401
        except ImportError:
            return {'supported': False, 'reason': '缺少 pywinpty 组件，请更新桌宠运行环境'}
        return {'supported': True, 'backend': 'ConPTY', 'shells': ['powershell']}

    def _authorize(self, session, command='powershell.exe'):
        mode = self.hub.teams.effective_mode(session) if hasattr(self.hub, 'teams') else session.mode
        if mode == 'read' or session.preset == 'plan' or session.archived:
            raise PermissionError('请在未归档的执行会话中打开终端')
        denied = self.hub.blacklist.check('run_command', {'command': command, 'workdir': session.workspace}, Path(session.workspace))
        if denied:
            raise PermissionError(denied.why)

    def metadata(self, row):
        return {k: v for k, v in row.items() if k not in {'output', 'parser', 'lock'}}

    def list(self, session):
        return {'ok': True, **self.availability(), 'terminals': [self.metadata(x) for x in self.records.values() if x['session'] == session.id]}

    def owned(self, session, tid):
        row = self.records.get(tid)
        if row is None or row['session'] != session.id:
            raise ValueError('没有这个会话的终端')
        return row

    async def create(self, session, *, shell='powershell', cols=100, rows=28, title=''):
        if self.closed:
            raise ValueError('终端服务正在关闭')
        self._authorize(session)
        if shell != 'powershell':
            raise ValueError('当前支持 Windows PowerShell')
        cols, rows = dimensions(cols, rows)
        if not isinstance(title, str) or len(title) > 80:
            raise ValueError('终端名称最多 80 字')
        tid = 'pty-' + secrets.token_hex(10)
        # Availability is reported before creation, not disguised as a pipe fallback.
        if self.factory is ConPTY and not self.availability()['supported']:
            raise ValueError(self.availability()['reason'])
        launch = asyncio.create_task(asyncio.to_thread(self.factory, session.workspace, cols, rows))
        try:
            proc = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Cancellation of to_thread does not cancel its native process creation.
            with contextlib.suppress(Exception):
                orphan = await asyncio.shield(launch)
                await asyncio.to_thread(orphan.close)
            raise
        if self.closed:
            await asyncio.to_thread(proc.close)
            raise ValueError('终端服务正在关闭')
        row = {'id': tid, 'session': session.id, 'title': title.strip() or 'PowerShell', 'backend': 'ConPTY',
               'state': 'waiting', 'progress': None, 'cwd': session.workspace, 'command': '',
               'last_exit_code': None, 'exit_code': None, 'notice': '', 'started': time.time(), 'ended': None,
               'last_output': None,
               'sequence': 0, 'base_cursor': 0, 'prompt_seq': 0, 'at_prompt': False,
               'cols': cols, 'rows': rows, 'output': '', 'commands': [], 'reports': [], 'lock': asyncio.Lock()}
        row['parser'] = OSCState(row)
        self.records[tid], self.processes[tid] = row, proc
        self.tasks[tid] = asyncio.create_task(self._consume(tid))
        return self.metadata(row)

    def output(self, session, tid, after=0):
        row = self.owned(session, tid)
        after = int(after)
        if after < 0 or after > row['sequence']:
            raise ValueError('输出游标无效')
        reset = after < row['base_cursor']
        return {'ok': True, 'terminal': self.metadata(row), 'output': row['output'][max(after - row['base_cursor'], 0):],
                'next_cursor': row['sequence'], 'base_cursor': row['base_cursor'], 'reset': reset}

    async def _consume(self, tid):
        row, proc = self.records[tid], self.processes[tid]
        previous = (row['state'], row['notice'])
        try:
            while row['state'] != 'closed':
                async with row['lock']:
                    data = await asyncio.to_thread(proc.read)
                    alive = await asyncio.to_thread(proc.alive)
                if data:
                    row['last_output'] = time.time()
                    row['sequence'] += len(data)
                    row['output'] = (row['output'] + data)[-MAX_OUTPUT:]
                    row['base_cursor'] = row['sequence'] - len(row['output'])
                    row['parser'].feed(data)
                    current = (row['state'], row['notice'])
                    if current != previous and (row['state'] in {'completed', 'failed', 'waiting'} or row['notice']):
                        await self._notify(row)
                    previous = current
                if not alive:
                    row.update(state='closed', ended=time.time(), exit_code=proc.exit_code())
                    await self._notify(row)
                    break
                await asyncio.sleep(.04)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            row.update(state='closed', notice=str(exc)[:300], ended=time.time())
        finally:
            await asyncio.to_thread(proc.close)

    async def _notify(self, row):
        if self.closed:
            return
        tid = row['id']
        delay = 3 - (time.monotonic() - self._notice_at.get(tid, -10))
        if delay > 0:
            if tid not in self._notice_pending:
                async def later():
                    try:
                        await asyncio.sleep(delay)
                        self._notice_pending.pop(tid, None)
                        await self._notify(row)  # Coalesce to the latest state.
                    finally:
                        self._notice_pending.pop(tid, None)
                self._notice_pending[tid] = asyncio.create_task(later())
            return
        self._notice_at[tid] = time.monotonic()
        channels = getattr(getattr(self.hub, 'e', None), 'channels', None)
        if channels:
            # This is an event to connected UI listeners, never an external message.
            with contextlib.suppress(Exception):
                await channels.notify({'type': 'channel_notice', 'channel': 'workbench', 'kind': 'terminal',
                    'session_id': row['session'], 'terminal_id': row['id'], 'state': row['state'],
                    'text': f"终端「{row['title']}」：" + (row['notice'] or {'waiting':'等待操作', 'completed':'命令已完成', 'failed':'上一条命令失败', 'closed':'已关闭'}.get(row['state'], '运行中'))})

    async def input(self, session, tid, data):
        row = self.owned(session, tid)
        self._authorize(session, data if isinstance(data, str) else '')
        if not isinstance(data, str) or not data or len(data) > MAX_INPUT:
            raise ValueError('输入为空或过长')
        async with row['lock']:
            if row['state'] == 'closed':
                raise ValueError('终端已关闭')
            await asyncio.to_thread(self.processes[tid].write, data)
            if '\r' in data or '\n' in data:
                row.update(state='running', at_prompt=False, notice='')
        return self.metadata(row)

    async def resize(self, session, tid, cols, rows):
        row = self.owned(session, tid)
        cols, rows = dimensions(cols, rows)
        async with row['lock']:
            if row['state'] == 'closed':
                raise ValueError('终端已关闭')
            await asyncio.to_thread(self.processes[tid].resize, cols, rows)
            row.update(cols=cols, rows=rows)
        return self.metadata(row)

    async def interrupt(self, session, tid):
        # A revocation still permits stopping work; never turn Ctrl-C into a kill.
        row = self.owned(session, tid)
        async with row['lock']:
            if row['state'] == 'closed':
                raise ValueError('终端已关闭')
            await asyncio.to_thread(self.processes[tid].write, '\x03')
        return self.metadata(row)

    async def terminate(self, session, tid):
        row = self.owned(session, tid)
        async with row['lock']:
            row.update(state='closed', ended=time.time())
            await asyncio.to_thread(self.processes[tid].close)
        task = self.tasks[tid]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return self.metadata(row)

    def rename(self, session, tid, title):
        row = self.owned(session, tid)
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise ValueError('终端名称应为 1–80 字')
        row['title'] = title.strip()
        return self.metadata(row)

    async def close(self):
        self.closed = True
        for pending in self._notice_pending.values():
            pending.cancel()
        await asyncio.gather(*list(self._notice_pending.values()), return_exceptions=True)
        self._notice_pending.clear()
        for tid, row in self.records.items():
            row.update(state='closed', ended=row['ended'] or time.time())
            self.tasks[tid].cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        # A reader cancelled before its first scheduling never enters finally.
        for tid, proc in self.processes.items():
            async with self.records[tid]['lock']:
                await asyncio.to_thread(proc.close)

