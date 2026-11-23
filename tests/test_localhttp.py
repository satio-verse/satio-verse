"""看本机服务好不好的探测一律直连 (wm/localhttp.py), 不受环境变量里的代理影响.

2026-11-11: 用户环境变量里有 http_proxy、没有 no_proxy. urllib 把 127.0.0.1 也送去代理 (代理多数回 502, 有时直接断开),
看守 (tools/serve.py) 一直查不到 /api/status, 每 607 秒把好好的服务端当成"没起来"杀一次, 第一次还把刚合进来的两次改动退回了;
服务监控 (wm/services.py) 查不到本机模型的 /health, 约 3 分钟重启它一次. Claude 会话的环境里带着 NO_PROXY, 所以在会话里
跑测试看不出来: 这里的测试自己把环境改成那台机器的样子 (有代理、没有 no_proxy), 先确认老办法在这种环境下真的不通, 再看新的.
代理有两种替身: 端口没人听的; 对什么请求都回 502 并记下来的 (像那天的) — 新办法下它一个请求都不该收到.
"""
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from wm import localhttp, services
from wm import workbench as W
from wm.embed import LlamaEmbedder

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: bytes = b"{}") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # /api/status 回服务器上记的状态码 (默认 200); 别的路径 (/health) 都回 200
        self._reply(self.server.status_code if self.path == "/api/status" else 200)

    def do_POST(self):  # 嵌入服务: 每条文本回一个两维向量; 看守的通知 (/api/supervisor) 也回这个, 是个字典就行
        n = len(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}").get("input", []))
        self._reply(200, json.dumps({"data": [{"index": i, "embedding": [3.0, 4.0]} for i in range(n)]}).encode())

    def log_message(self, *a):
        pass


class _Proxy(BaseHTTPRequestHandler):
    """像那天的代理: 对什么请求都回 502, 记下收到的."""

    def _any(self):
        self.server.hits.append(self.requestline)
        self.send_response(502, "Bad Gateway")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_CONNECT = do_HEAD = _any

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.status_code = 200
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(params=["没人听", "回 502"])
def hijacked(request, monkeypatch):
    """装了代理工具的机器: 环境变量里有代理, 没有 no_proxy. hits 是代理收到的请求 (没人听的那种永远是空的)."""
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    srv, hits = None, []
    if request.param == "回 502":
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
        srv.hits = hits
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    proxy = f"http://127.0.0.1:{srv.server_port if srv else free_port()}"
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    yield types.SimpleNamespace(url=proxy, hits=hits, answers=srv is not None)
    if srv:
        srv.shutdown()
        srv.server_close()


def test_env_proxy_hijacks_loopback_for_plain_urllib_but_not_for_open_direct(local_server, hijacked):
    url = f"http://127.0.0.1:{local_server.server_port}/health"
    assert urllib.request.getproxies().get("http") == hijacked.url and not urllib.request.proxy_bypass("127.0.0.1")
    with pytest.raises(OSError) as err:  # 老办法: 本机地址也被送去代理 (没人听 = 连不上; 回 502 的 = HTTPError)
        urllib.request.build_opener().open(url, timeout=3)
    if hijacked.answers:
        assert isinstance(err.value, urllib.error.HTTPError) and err.value.code == 502 and len(hijacked.hits) == 1
    hijacked.hits.clear()
    with localhttp.open_direct(url, timeout=3) as r:
        assert r.status == 200
    with localhttp.urlopen(urllib.request.Request(url), timeout=3) as r:  # 给 Request 对象也一样
        assert r.status == 200
    assert hijacked.hits == []


def test_watchdog_and_service_probes_reach_the_server_despite_env_proxy(local_server, hijacked):
    port = local_server.server_port
    assert W.status_code(port, timeout=3) == 200 and W.status_ok(port, timeout=3)
    assert services.healthy(services.Spec("llm", [], port, "/health"), timeout=3)
    assert services.healthy(services.Spec("tts", [], port, "tcp"), timeout=3)
    dead = free_port()  # 没人听的端口: 照实说不通 (不是代理替它回的 502)
    assert W.status_code(dead, timeout=1) is None and not W.status_ok(dead, timeout=1)
    assert not services.healthy(services.Spec("llm", [], dead, "/health"), timeout=1)
    assert hijacked.hits == []


def test_status_code_tells_a_broken_status_page_from_no_answer(local_server):
    """回了话但不是 200 (服务端的 /api/status 自己坏了) 和连不上分得开: 看守只在"连一句回话都没有"时才怀疑是这条路不通.
    探测是直连的, 所以回话的只会是那个端口上的服务端自己, 不会是代理."""
    port = local_server.server_port
    local_server.status_code = 500
    assert W.status_code(port, timeout=3) == 500 and not W.status_ok(port, timeout=3)
    local_server.status_code = 200
    assert W.status_code(port, timeout=3) == 200


def test_llama_server_readiness_wait_ignores_env_proxy(local_server, hijacked):
    class Running:
        def poll(self):
            return None

    assert load_tool("start_llm").wait_ready(local_server.server_port, Running(), timeout=5)
    assert hijacked.hits == []


def test_supervisor_notice_before_restart_goes_direct(local_server, hijacked, tmp_path):
    """看守重启前带着钥匙 POST /api/supervisor: 也直连 (钥匙不该送到代理那里去)."""
    sv = load_tool("serve").Supervisor(local_server.server_port, home=tmp_path / "serve", logs=tmp_path / "logs", out=io.BytesIO())
    assert sv._notify("restart") is not None
    assert hijacked.hits == []


def test_embedder_reaches_a_local_endpoint_despite_env_proxy(local_server, hijacked):
    emb = LlamaEmbedder({"endpoint": f"http://127.0.0.1:{local_server.server_port}"})
    got = emb.encode(["一", "二"])
    assert got.shape == (2, 2) and abs(float(got[0, 0]) - 0.6) < 1e-6  # (3, 4) 归一化
    assert hijacked.hits == []


def test_only_loopback_goes_direct(monkeypatch):
    assert localhttp.is_local("http://127.0.0.1:8790/api/status") and localhttp.is_local("http://localhost:8786")
    assert localhttp.is_local("http://[::1]:8790/")
    assert not localhttp.is_local("http://192.168.1.5:8786") and not localhttp.is_local("https://example.com/x")
    assert not localhttp.is_local("不是地址")
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: seen.append((req, timeout)) or "别处")
    assert localhttp.urlopen("http://192.168.1.5:8786/v1/embeddings", timeout=2) == "别处"  # 别的机器: 照 urllib 的老规矩
    assert seen == [("http://192.168.1.5:8786/v1/embeddings", 2)]


def test_no_plain_urlopen_left_in_wm_and_tools():
    """wm/ 和 tools/ 里不许再出现 urllib.request.urlopen( : 它会吃到环境变量里的代理, 访问本机服务要用 wm/localhttp.py.
    例外只有 localhttp 自己 (给别的机器用的那条路) 和两个下载外网东西的安装脚本. 新写的探测再用老写法, 这条直接红."""
    allowed = {"wm/localhttp.py", "tools/setup_models.py", "tools/setup_python.py"}
    bad = []
    for top in ("wm", "tools"):
        for path in (ROOT / top).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in allowed or "__pycache__" in rel:
                continue
            if "urllib.request.urlopen(" in path.read_text(encoding="utf-8", errors="replace"):
                bad.append(rel)
    assert bad == []


# ---- 另一层: 起子进程时给它的环境补上 no_proxy (wm/localhttp.py 的 with_local_bypass) ----
# 我们自己的探测已经直连 (上面那些). 这一层守的是服务监控、看守、服务端起的子进程: 环境里有代理变量就把本机地址写进
# no_proxy, 底下别的程序 (门禁的 pytest、她跑的命令、MCP 服务、语音合成) 用老写法访问本机也通. 没有代理变量时不加:
# 环境里只有 no_proxy 时 urllib 会整个不看 Windows 的系统代理. (原来在 tests/test_local_bypass.py, 并到这里是因为门禁
# 按文件名找测试: 改 wm/localhttp.py 只带这个文件.)
PROXY_NAMES = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
LOCAL = {"127.0.0.1", "localhost", "::1"}
# 子进程: 用老写法 (会吃到环境变量里的代理) 访问给的地址, 打印状态码
PLAIN = "import sys, urllib.request\nprint(urllib.request.urlopen(sys.argv[1], timeout=5).status)\n"
# 子进程: 把自己环境里和代理有关的变量写进文件 (名字转成小写), 然后待着等人来停
DUMP = ("import json, os, sys, time\n"
        "got = {k.lower(): v for k, v in os.environ.items() if 'proxy' in k.lower()}\n"
        "with open(sys.argv[1] + '.tmp', 'w', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(got))\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "time.sleep(60)\n")


def proxy_vars(env: dict) -> dict:
    """环境里和代理有关的变量, 名字转成小写 (Windows 上 os.environ 的名字都是大写)."""
    return {k.lower(): v for k, v in env.items() if "proxy" in k.lower()}


def hosts(value: str) -> list[str]:
    return [h.strip() for h in value.split(",") if h.strip()]


def wait_json(path: Path, timeout: float = 20.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError(f"子进程没写出 {path.name}")


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def serve(handler) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.hits = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def unproxied(monkeypatch):
    """环境里没有任何代理变量 (只用 Windows 系统代理的机器)."""
    for name in PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def proxied(monkeypatch, unproxied):
    """用户那台机器的样子: 环境变量里有代理 (这里指向一个没人听的端口), 没有 no_proxy. 返回代理地址."""
    proxy = f"http://127.0.0.1:{free_port()}"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    return proxy


def test_bypass_is_added_only_when_a_proxy_is_set():
    plain = {"PATH": "x"}
    assert localhttp.with_local_bypass(plain) == {"PATH": "x"}  # 没有代理变量: 不凭空加
    assert localhttp.with_local_bypass({"NO_PROXY": ".corp"}) == {"NO_PROXY": ".corp"}  # 只有 no_proxy: 也不动
    assert "NO_PROXY" not in localhttp.with_local_bypass({"HTTP_PROXY": ""})  # 空着的不算有代理
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        got = localhttp.with_local_bypass({name: "http://127.0.0.1:1082"})
        assert got["NO_PROXY"].split(",") == ["127.0.0.1", "localhost", "::1"], name
        assert got[name] == "http://127.0.0.1:1082"  # 代理本身不动: 上外网照旧走它


def test_existing_no_proxy_is_kept_and_not_duplicated():
    env = {"HTTPS_PROXY": "http://p:1", "no_proxy": ".corp, LOCALHOST"}
    got = localhttp.with_local_bypass(env)
    assert "NO_PROXY" not in got  # 沿用已有的名字 (Windows 上名字不分大小写, 两个都给会重复)
    assert got["no_proxy"].split(",") == [".corp", "LOCALHOST", "127.0.0.1", "::1"]
    assert env == {"HTTPS_PROXY": "http://p:1", "no_proxy": ".corp, LOCALHOST"}  # 不改传进来的
    assert localhttp.with_local_bypass(got) == got  # 再套一次不变
    full = {"http_proxy": "http://p:1", "NO_PROXY": "localhost, 127.0.0.1,::1,.local"}  # Claude 会话里的样子: 已经齐了
    assert localhttp.with_local_bypass(full)["NO_PROXY"] == "localhost, 127.0.0.1,::1,.local"  # 一个字节都不动
    star = {"HTTP_PROXY": "http://p:1", "NO_PROXY": "*"}
    assert localhttp.with_local_bypass(star)["NO_PROXY"] == "*"  # 什么都不走代理的: 不动
    both = localhttp.with_local_bypass({"http_proxy": "http://p:1", "NO_PROXY": "a", "no_proxy": "b"})  # 别的系统上两个都有
    assert hosts(both["NO_PROXY"])[0] == "a" and hosts(both["no_proxy"])[0] == "b"
    assert LOCAL <= set(hosts(both["NO_PROXY"])) and LOCAL <= set(hosts(both["no_proxy"]))


def test_child_using_plain_urllib_reaches_loopback_once_the_bypass_is_there():
    """子进程里用老写法 (urllib.request.urlopen) 访问本机: 不补 no_proxy 时被代理回 502 (那天的样子);
    补了就通, 代理一个请求都收不到."""
    target, proxy = serve(_Ok), serve(_Proxy)
    try:
        url = f"http://127.0.0.1:{target.server_port}/health"
        addr = f"http://127.0.0.1:{proxy.server_port}"
        hijacked = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "HTTP_PROXY": addr, "HTTPS_PROXY": addr}
        bad = subprocess.run([sys.executable, "-c", PLAIN, url], env=hijacked, capture_output=True, timeout=60)
        assert bad.returncode != 0 and b"502" in bad.stderr and len(proxy.hits) == 1
        proxy.hits.clear()
        good = subprocess.run([sys.executable, "-c", PLAIN, url], env=localhttp.with_local_bypass(hijacked),
                              capture_output=True, timeout=60)
        assert good.stdout.strip() == b"200" and proxy.hits == []
    finally:
        for s in (target, proxy):
            s.shutdown()
            s.server_close()


def test_gate_trial_and_dsh_children_get_the_bypass(proxied, monkeypatch):
    """服务端起的子进程 (wm/workbench.py 的 child_env: 门禁的 pytest、试跑的服务端、dsh)."""
    monkeypatch.setenv("WM_SUPERVISOR_KEY", "k")
    env = W.child_env(PYTHONIOENCODING="utf-8")
    got = proxy_vars(env)
    assert got["http_proxy"] == proxied and LOCAL <= set(hosts(got["no_proxy"]))
    assert env["PYTHONIOENCODING"] == "utf-8" and not set(W.SUPERVISOR_ENV) & set(env)  # 原来的本分还在: 看守的那几个不带
    monkeypatch.delenv("HTTP_PROXY")
    monkeypatch.delenv("HTTPS_PROXY")
    assert "no_proxy" not in proxy_vars(W.child_env())  # 没有代理变量: 不凭空加


def test_service_monitor_hands_the_bypass_to_what_it_starts(tmp_path, proxied):
    """桌宠的服务监控 (wm/services.py) 起的服务 (看守、本机模型、语音合成), 以及它们再往下起的."""
    out, port = tmp_path / "env.json", free_port()
    sup = services.Supervisor([services.Spec("tts", [sys.executable, "-c", DUMP, str(out)], port, "tcp", 20)],
                              home=tmp_path / "home", logs=tmp_path / "logs", tick=0.1)
    try:
        sup.step()  # 端口上没人在听: 起它
        got = wait_json(out)
    finally:
        for name in list(sup.proc):
            sup._reap(name)
    assert got["http_proxy"] == proxied and LOCAL <= set(hosts(got["no_proxy"]))


def test_watchdog_hands_the_bypass_to_the_server(tmp_path, proxied):
    """看守 (tools/serve.py) 起的服务端: 你在终端里直接跑 serve.py 时, 上面没有服务监控替它补."""
    out = tmp_path / "env.json"
    sv = load_tool("serve").Supervisor(free_port(), home=tmp_path / "serve", logs=tmp_path / "logs", out=io.BytesIO(),
                                       cmd=[sys.executable, "-c", DUMP, str(out)])
    sv._start()
    try:
        got = wait_json(out)
    finally:
        sv._kill()
    assert got["http_proxy"] == proxied and LOCAL <= set(hosts(got["no_proxy"]))


def test_watchdog_adds_nothing_when_no_proxy_is_set(tmp_path, unproxied):
    out = tmp_path / "env.json"
    sv = load_tool("serve").Supervisor(free_port(), home=tmp_path / "serve", logs=tmp_path / "logs", out=io.BytesIO(),
                                       cmd=[sys.executable, "-c", DUMP, str(out)])
    sv._start()
    try:
        got = wait_json(out)
    finally:
        sv._kill()
    assert got == {}  # 没有代理变量的机器: 子进程的环境里一个和代理有关的变量都没多出来
