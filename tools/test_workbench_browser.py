"""Exercise the actual native workbench and frontend using disposable profiles.

The app routes are real; model execution is disabled, and all sessions, browser
data and build products live under --out. No installed wmpet process is started.
"""
import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def serve(out):
    from aiohttp import web
    from wm.agent.core import AgentHub
    from wm.app import (AGENT, CFG, ENGINES, api_agent, agent_ws, api_agent_runtime,
                        api_agent_platform, api_agent_workspace, api_agent_context,
                        api_agent_permissions, api_agent_terminal, api_webview,
                        pet_file, agent_page)
    with tempfile.TemporaryDirectory(prefix='backend-', dir=out) as temporary:
        home = Path(temporary)
        project = home/'project'
        project.mkdir()
        (project/'README.md').write_text('# Native browser integration fixture', encoding='utf-8')
        cfg = {'agent': {'workspace': str(project), 'extensions': {'settings_path': False}},
               'billing': {'env_key': ''}}
        hub = AgentHub(cfg, home=home/'agent')
        hub.models = lambda: []
        hub._llm = lambda session: None
        hub.new_session()
        app = web.Application()
        app[AGENT] = hub
        app[CFG] = cfg
        app[ENGINES] = SimpleNamespace(channels=None)
        app.router.add_get('/agent', agent_page)
        app.router.add_get('/agent/ws', agent_ws)
        app.router.add_get('/pet/{name:.*}', pet_file)
        for name, handler in [('agent', api_agent), ('agent/runtime', api_agent_runtime),
                              ('agent/platform', api_agent_platform), ('agent/workspace', api_agent_workspace),
                              ('agent/context', api_agent_context), ('agent/permissions', api_agent_permissions),
                              ('agent/terminal', api_agent_terminal), ('webview', api_webview)]:
            app.router.add_route('*', '/api/'+name, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        print(json.dumps({'origin': 'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])}), flush=True)
        try:
            await asyncio.to_thread(sys.stdin.readline)
        finally:
            await hub.teams.close()
            if hasattr(hub, 'workspace_jobs'):
                await hub.workspace_jobs.close()
            if hasattr(hub, 'terminals'):
                await hub.terminals.close()
            await runner.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--url', help='Optional public page diagnosis; normal regression stays local')
    parser.add_argument('--skip-build', action='store_true')
    parser.add_argument('--delay-handshake', action='store_true', help='Force navigation before native capabilities arrive')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.serve:
        return asyncio.run(serve(out))
    compiler = Path('E:/Program Files (x86)/CPEditor/mingw64/bin/g++.exe')
    sdk = ROOT/'desktop/third_party/webview2/pkg/build/native'
    binary = out/'bin'
    binary.mkdir(exist_ok=True)
    executable = binary/'workbench-probe.exe'
    if not args.skip_build:
        sources = ['tests/workbench_browser_probe.cpp', 'src/workbench.cpp', 'src/browser_pane.cpp',
                   'src/webview_host.cpp', 'src/util.cpp', 'src/json.cpp']
        command = [str(compiler), '-std=c++17', '-municode', '-static', '-static-libgcc', '-static-libstdc++',
                   '-DUNICODE', '-D_UNICODE', '-DWIN32_LEAN_AND_MEAN', '-DNOMINMAX', '-D_WIN32_WINNT=0x0A00',
                   '-finput-charset=UTF-8', '-fexec-charset=UTF-8', '-I'+str(ROOT/'desktop/host/src'),
                   '-I'+str(sdk/'include')]
        command += [str(ROOT/'desktop/host'/s) for s in sources]
        command += ['-lole32', '-luuid', '-lwinhttp', '-luser32', '-lshell32', '-lshlwapi', '-lgdi32',
                    '-ldwmapi', '-ldcomp', '-o', str(executable)]
        built = subprocess.run(command, capture_output=True, timeout=120)
        if built.returncode:
            print(built.stderr.decode(errors='replace'))
            return built.returncode
    shutil.copy2(sdk/'x64/WebView2Loader.dll', binary/'WebView2Loader.dll')
    class Page(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(412 if self.path == '/http-error' else 200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(('<!doctype html><title>Actual workbench browser '+self.path+'</title>'
                              '<body style="background:#edf5ff;padding:40px;font:20px system-ui">'
                              '<h1>Actual WorkbenchWindow native browser</h1>'
                              '<p id="selection">Selected fixture text</p><input id="persist" aria-label="Persistent state">'
                              '<p>The independent native page is rendered even when embedding is forbidden.</p></body>').encode())
    page = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    backend = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--out', str(out), '--serve'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        info = json.loads(backend.stdout.readline())
        result = out/'integration.json'
        command = [str(executable), info['origin'], str(ROOT/'desktop/web'),
                   args.url or f'http://127.0.0.1:{page.server_port}/a', str(result)]
        if args.delay_handshake:
            command.append('delay-handshake')
        run = subprocess.run(command,
                             capture_output=True, timeout=100, creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(result.read_text(encoding='utf-8'))
        print(json.dumps(data, ensure_ascii=False))
        return run.returncode
    finally:
        page.shutdown()
        page.server_close()
        _, errors = backend.communicate('\n', timeout=20)
        if errors:
            (out/'backend.log').write_text(errors, encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
