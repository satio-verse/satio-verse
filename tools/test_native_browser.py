"""Build and exercise BrowserPane with two disposable localhost HTTP servers.

No installed browser/profile or production wmpet is touched. All build and
WebView data belongs to the explicitly supplied temporary evidence directory.
For the actual WorkbenchWindow plus current frontend and native tabs, also run
tools/test_workbench_browser.py --out <separate disposable output directory>.
"""
import argparse
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    records = {'protected_hits': 0, 'protected_paths': [], 'fixture_hits': []}
    class Protected(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            records['protected_hits'] += 1
            records['protected_paths'].append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(b'should not reach this')
    protected = ThreadingHTTPServer(('127.0.0.1', 0), Protected)
    port = protected.server_port
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            records['fixture_hits'].append(self.path)
            if self.path in ('/redirect', '/subredirect'):
                self.send_response(302); self.send_header('Location',f'http://127.0.0.1:{port}/private'); self.end_headers(); return
            if self.path == '/sw.js':
                self.send_response(200);self.send_header('Content-Type','text/javascript');self.end_headers()
                self.wfile.write(f'''self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('message',e=>e.waitUntil((async()=>{{await Promise.allSettled([fetch('http://127.0.0.1:{port}/private-worker'),fetch('/subredirect')]);await fetch('/worker-done');e.source.postMessage('worker-done');}})()));'''.encode());return
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Security-Policy',"frame-ancestors 'none'");self.end_headers()
            if self.path == '/a':
                body = f'''<title>first</title><h1>Native browser fixture</h1>
                  <iframe src="http://127.0.0.1:{port}/private-frame"></iframe>
                  <iframe src="/subredirect"></iframe>
                  <script>(async()=>{{let bridge=false;try{{chrome.webview.postMessage('panel {{"op":"save"}}');bridge=true;}}catch(e){{}}
                  window.open('/popup','new-popup');
                  let blocked=false;try{{let r=await fetch('http://127.0.0.1:{port}/private-fetch');blocked=r.status===403;}}catch(e){{blocked=true;}}
                  try{{await fetch('/subredirect');}}catch(e){{}}
                  try{{await navigator.serviceWorker.register('/sw.js');let ready=await navigator.serviceWorker.ready;await new Promise(resolve=>{{navigator.serviceWorker.addEventListener('message',resolve,{{once:true}});ready.active.postMessage('probe');setTimeout(resolve,1800);}});}}catch(e){{}}
                  document.title=blocked?'isolated:blocked':'SECURITY-FAIL';}})();</script>'''
            else: body='<title>second</title><h1>Second document</h1>'
            self.wfile.write(body.encode())
    fixture=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
    for server in (protected,fixture): threading.Thread(target=server.serve_forever,daemon=True).start()
    compiler=Path('E:/Program Files (x86)/CPEditor/mingw64/bin/g++.exe')
    sdk=ROOT/'desktop/third_party/webview2/pkg/build/native'
    executable=out/'browser-probe.exe'; result=out/'native-result.json'
    command=[str(compiler),'-std=c++17','-municode','-static','-static-libgcc','-static-libstdc++',
      '-DUNICODE','-D_UNICODE','-DWIN32_LEAN_AND_MEAN','-DNOMINMAX','-D_WIN32_WINNT=0x0A00',
      '-finput-charset=UTF-8','-fexec-charset=UTF-8','-I'+str(ROOT/'desktop/host/src'),'-I'+str(sdk/'include'),
      str(ROOT/'desktop/host/tests/browser_pane_probe.cpp'),str(ROOT/'desktop/host/src/browser_pane.cpp'),
      str(ROOT/'desktop/host/src/util.cpp'),str(ROOT/'desktop/host/src/json.cpp'),
      '-lole32','-luuid','-lwinhttp','-luser32','-lshell32','-lshlwapi','-lgdi32','-o',str(executable)]
    try:
        subprocess.run(command,check=True,capture_output=True,timeout=120)
        shutil.copy2(sdk/'x64/WebView2Loader.dll',out/'WebView2Loader.dll')
        run=subprocess.run([str(executable),f'http://127.0.0.1:{fixture.server_port}',str(port),str(out/'browser-profile'),str(result)],
                           capture_output=True,timeout=50,creationflags=subprocess.CREATE_NO_WINDOW)
        data=json.loads(result.read_text(encoding='utf-8')) if result.exists() else {'ok':False,'error':run.stderr.decode(errors='replace')}
        data['network']=records
        data['ok']=data['ok'] and records['protected_hits']==0 and '/popup' not in records['fixture_hits'] and run.returncode==0
        data.setdefault('checks',{})['automatic_popup_blocked']='/popup' not in records['fixture_hits']
        data['checks']['service_worker_exercised']='/worker-done' in records['fixture_hits']
        data['ok']=data['ok'] and data['checks']['service_worker_exercised']
        result.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in data.items() if k!='events'},ensure_ascii=False))
        return 0 if data['ok'] else 1
    finally:
        for server in (protected,fixture):server.shutdown();server.server_close()

if __name__=='__main__':raise SystemExit(main())
