"""Offline extension/Creator contract tests, with real temporary MCP processes.

Every registry points at tmp_path. No installed user plugins, real credentials,
remote servers or model requests are used.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wm.agent.extensions import ExtensionRegistry, PREVIEW_CSP, _decrypt_secret
from wm.agent.mcp_runtime import MCPClient, MCPError


SERVER = '''import json, os, sys, time
for line in sys.stdin:
    q = json.loads(line)
    method = q.get('method')
    if 'id' not in q:
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'Echo text', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}},
                            {'name': 'hidden', 'description': 'Disabled', 'inputSchema': {'type': 'object'}}]}
    elif method == 'tools/call':
        a = q['params']['arguments']
        if a.get('wait'):
            open('pid.txt', 'w').write(str(os.getpid()))
            time.sleep(60)
        result = {'content': [{'type': 'text', 'text': a.get('text', '') + os.environ.get('WM_TEST_CREDENTIAL', '')}], 'isError': False}
        if a.get('structured'):
            result = {'structuredContent': {'count': 3, 'title': '结构化结果'}}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': q['id'], 'result': result}), flush=True)
'''


def registry(tmp_path, **kwargs):
    return ExtensionRegistry(tmp_path / "agent", settings_path=False, skill_roots=[], **kwargs)


def package(content="hello", **overrides):
    spec = {"id": "fixture-helper", "name": "测试助手", "description": "离线测试",
        "files": {"SKILL.md": f"---\nname: fixture-helper\ndescription: test\n---\n{content}",
                  "server.py": SERVER, "ui/index.html": '<h1>Test</h1><button onclick="this.textContent=2">1</button>'},
        "mcp": {"command": sys.executable, "args": ["server.py"]}, "ui": "ui/index.html"}
    spec.update(overrides)
    return spec


def test_creator_draft_enable_revisions_rollback_and_preview(tmp_path):
    r = registry(tmp_path)
    old = r.create(package())
    assert old["validation"]["execution"] == "not_run"
    assert r.servers() == r.skill_index() == []
    assert r.list()["extensions"][0]["enabled"] is False
    with pytest.raises(PermissionError):
        r.set_enabled("fixture-helper", True)
    r.set_enabled("fixture-helper", True, authorized=True)
    assert r.read_skill("creator:fixture-helper")["body"].endswith("hello")
    newest = r.create(package("updated"))
    assert newest["revision"] != old["revision"]
    assert r.read_skill("creator:fixture-helper")["body"].endswith("hello"), "Draft must not replace the active version"
    r.set_enabled("fixture-helper", True, authorized=True)
    assert r.read_skill("creator:fixture-helper")["body"].endswith("updated")
    with pytest.raises(PermissionError):
        r.rollback("fixture-helper", old["revision"])
    r.rollback("fixture-helper", old["revision"], authorized=True)
    assert r.read_skill("creator:fixture-helper")["body"].endswith("hello")
    preview = r.preview("fixture-helper")
    assert preview["sandbox"] == "allow-scripts" and "allow-same-origin" not in preview["sandbox"]
    assert "connect-src 'none'" in preview["html"] and preview["csp"] == PREVIEW_CSP
    assert "onclick=" in preview["html"], "Interactive code is retained inside isolated preview"
    assert len(r.list()["extensions"][0]["revisions"]) == 2
    assert r.create(package())["revision"] == old["revision"], "Identical source has stable revision"
    r.set_enabled("fixture-helper", False, authorized=True)
    assert not r.servers() and not r.skill_index()
    with pytest.raises(ValueError):
        asyncio.run(r.call("creator:fixture-helper", "echo", {}))


@pytest.mark.parametrize("path", ["../bad.py", "/bad.py", "C:/bad.py", "x\\bad.py", "x/../bad.py", "x//bad.py", ".hidden/x", "AUX.txt", "x/nul.py", "x.", "manifest.json"])
def test_package_rejects_path_traversal_and_reserved_names(tmp_path, path):
    r = registry(tmp_path)
    with pytest.raises(ValueError):
        r.create({"id": "test", "name": "test", "files": {"SKILL.md": "hi", path: "x"}})
    assert not (r.root / "registry.json").exists()


def test_package_rejects_syntax_credentials_and_size(tmp_path):
    r = registry(tmp_path)
    with pytest.raises(ValueError, match="语法"):
        r.create(package(files={"server.py": "if (", "SKILL.md": "x"}))
    with pytest.raises(ValueError, match="凭据"):
        r.create(package(mcp={"command": "python", "env": {"API_KEY": "fake-only"}}))
    with pytest.raises(ValueError, match="过大"):
        r.create(package(files={"SKILL.md": "x" * (300 * 1024)}))
    with pytest.raises(ValueError, match="重名"):
        r.create(package(files={"SKILL.md": "one", "skill.md": "two"}))


def test_tampered_code_or_manifest_cannot_run_or_preview(tmp_path):
    r = registry(tmp_path)
    saved = r.create(package())
    r.set_enabled("fixture-helper", True, authorized=True)
    folder = r.root / "fixture-helper" / "revisions" / saved["revision"]
    (folder / "server.py").write_text("print('changed')", encoding="utf-8")
    assert r.servers() == [] and r.skill_index() == []
    with pytest.raises(ValueError):
        r.preview("fixture-helper")
    (folder / "server.py").write_text(SERVER, encoding="utf-8", newline="")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["mcp"]["args"] = ["-c", "print('changed')"]
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert r.servers() == []
    with pytest.raises(ValueError):
        r.set_enabled("fixture-helper", True, authorized=True)


def test_bound_desktop_skills_shadow_bundled_and_config_is_redacted(tmp_path):
    user, bundled = tmp_path / "user-skills", tmp_path / "bundled-skills"
    for root in (user, bundled):
        for name in ("computer-use", "bound", "unbound"):
            folder = root / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: >\n  More detail\n---\n{root.name}", encoding="utf-8")
    settings = tmp_path / "pet-settings.json"
    settings.write_text(json.dumps({"active": "p", "personas": [{"id": "p", "skills": ["bound"], "mcp": ["s"]}],
        "mcp": [{"id": "s", "name": "Server", "kind": "windows-mcp", "enabled": True, "command": "fixture", "secret": "FAKE-ENCRYPTED", "env": {"TOKEN": "FAKE-RAW"}},
                {"id": "unbound", "enabled": True}, {"id": "disabled", "enabled": False}]}), encoding="utf-8")
    r = ExtensionRegistry(tmp_path / "agent", settings_path=settings, skill_roots=[user, bundled])
    assert {s["id"] for s in r.skill_index()} == {"bound", "computer-use"}
    assert r.read_skill("bound")["body"].endswith("user-skills")
    assert r.skill_index()[0]["description"] == "More detail"
    assert [s["id"] for s in r.servers()] == ["s"]
    assert "FAKE" not in json.dumps(r.list()) and "FAKE" not in json.dumps(r.server("s"))
    with pytest.raises(ValueError):
        r.read_skill("unbound")


def test_real_stdio_tools_and_disabled_tool_not_callable(tmp_path, monkeypatch):
    monkeypatch.setenv("WM_TEST_CREDENTIAL", "must-not-inherit")
    folder = tmp_path / "server"
    folder.mkdir()
    (folder / "server.py").write_text(SERVER, encoding="utf-8")
    r = registry(tmp_path, servers=[{"id": "fixture", "command": sys.executable, "args": ["server.py"], "cwd": str(folder), "tools": {"hidden": False}}])
    async def run():
        listing = await r.list_tools("fixture")
        assert [t["name"] for t in listing["tools"]] == ["echo"]
        result = await r.call("fixture", "echo", {"text": "你好"})
        assert result["content"] == [{"type": "text", "text": "你好"}]
        structured = await r.call("fixture", "echo", {"structured": True})
        assert json.loads(structured["content"][0]["text"]) == {"count": 3, "title": "结构化结果"}
        with pytest.raises(ValueError, match="停用"):
            await r.call("fixture", "hidden", {})
    asyncio.run(run())


def test_creator_mcp_can_execute_after_enable(tmp_path):
    r = registry(tmp_path)
    r.create(package())
    r.set_enabled("fixture-helper", True, authorized=True)
    result = asyncio.run(r.call("creator:fixture-helper", "echo", {"text": "works"}))
    assert result["content"][0]["text"] == "works"


def test_explicit_credentials_are_usable_but_redacted_from_tool_output(tmp_path):
    folder = tmp_path / "server"
    folder.mkdir()
    (folder / "server.py").write_text(SERVER, encoding="utf-8")
    r = registry(tmp_path, servers=[{"id": "fixture", "command": sys.executable, "args": ["server.py"], "cwd": str(folder),
        "env": {"WM_TEST_CREDENTIAL": "fixture-secret"}}])
    result = asyncio.run(r.call("fixture", "echo", {"text": "result:"}))
    assert result["content"][0]["text"] == "result:[凭据已隐藏]"
    assert "fixture-secret" not in json.dumps(r.list())


def test_existing_self_mcp_registry_is_connected_only_when_hashes_match(tmp_path):
    import hashlib
    home = tmp_path / "self-mcp"
    folder = home / "fixture"
    folder.mkdir(parents=True)
    files = {"manifest.json": '{"id":"fixture"}', "server.py": SERVER, "selfmcp_kit.py": "# fixture"}
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8", newline="")
    entry = {"id": "self-fixture", "name": "Fixture", "dir": str(folder), "python": sys.executable, "enabled": True,
        "sha256": {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in files}}
    (home / "registry.json").write_text(json.dumps({"servers": [entry]}), encoding="utf-8")
    r = registry(tmp_path, self_mcp_root=home)
    assert [s["id"] for s in r.servers()] == ["self-fixture"]
    result = asyncio.run(r.call("self-fixture", "echo", {"text": "legacy works"}))
    assert result["content"][0]["text"] == "legacy works"
    (folder / "server.py").write_text("print('changed')", encoding="utf-8")
    assert r.servers() == []


def test_explicit_skill_roots_without_desktop_profile_are_available(tmp_path):
    folder = tmp_path / "skills" / "example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: example\n---\nTest", encoding="utf-8")
    r = ExtensionRegistry(tmp_path / "agent", settings_path=False, skill_roots=[folder.parent])
    assert r.read_skill("example")["body"].endswith("Test")


def test_desktop_discovery_switch_keeps_legacy_hub_tests_isolated(tmp_path, monkeypatch):
    from wm.agent import extensions
    monkeypatch.setattr(extensions, "DEFAULT_DESKTOP_DISCOVERY", False)
    r = ExtensionRegistry(tmp_path / "agent")
    assert r.settings_path is None and r.skill_roots == [] and r.self_mcp_root is None
    assert r.summary_index() == {"skills": [], "servers": []}


def test_mcp_cancellation_closes_real_child_process(tmp_path):
    import psutil
    folder = tmp_path / "server"
    folder.mkdir()
    (folder / "server.py").write_text(SERVER, encoding="utf-8")
    r = registry(tmp_path, servers=[{"id": "fixture", "command": sys.executable, "args": ["server.py"], "cwd": str(folder)}])
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(r.call("fixture", "echo", {"wait": True}, stop))
        for _ in range(100):
            if (folder / "pid.txt").exists():
                break
            await asyncio.sleep(.02)
        pid = int((folder / "pid.txt").read_text())
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not psutil.pid_exists(pid)
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_streamable_http_handshake_session_and_tools(tmp_path, stream):
    observed = []
    async def run():
        async def handler(request):
            body = await request.json()
            observed.append((body, dict(request.headers)))
            if body["method"] == "notifications/initialized":
                return web.Response(status=202)
            if body["method"] == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
            elif body["method"] == "tools/list":
                result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": body["params"]["arguments"]["text"]}]}
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            headers = {"Mcp-Session-Id": "fixture-session"}
            if stream:
                return web.Response(text='event: message\ndata: ' + json.dumps(reply) + '\n\n', content_type="text/event-stream", headers=headers)
            return web.json_response(reply, headers=headers)
        app = web.Application()
        app.router.add_post("/mcp", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            r = registry(tmp_path, servers=[{"id": "fixture", "transport": "http", "url": str(server.make_url("/mcp")), "headers": {"X-Fixture": "secret-only-in-request"}}])
            result = await r.call("fixture", "echo", {"text": "HTTP works"})
            assert result["content"][0]["text"] == "HTTP works"
            assert all(headers.get("Mcp-Session-Id") == "fixture-session" for _, headers in observed[1:])
            assert observed[0][1]["X-Fixture"] == "secret-only-in-request"
            assert "secret-only-in-request" not in json.dumps(r.list())
        finally:
            await server.close()
    asyncio.run(run())


def test_http_redirect_does_not_forward_credentials(tmp_path):
    hits = []
    async def run():
        async def handler(request):
            raise web.HTTPTemporaryRedirect("/other")
        async def other(request):
            hits.append(request.headers.get("Authorization"))
            return web.json_response({})
        app = web.Application()
        app.router.add_post("/mcp", handler)
        app.router.add_post("/other", other)
        server = TestServer(app)
        await server.start_server()
        try:
            r = registry(tmp_path, servers=[{"id": "fixture", "transport": "http", "url": str(server.make_url("/mcp")), "headers": {"Authorization": "fixture-secret"}}])
            with pytest.raises(MCPError, match="307"):
                await r.list_tools("fixture")
            assert not hits
        finally:
            await server.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI compatibility")
def test_dpapi_uses_desktop_host_entropy_without_exposing_secret():
    import ctypes
    import base64
    class Blob(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = b'{"env":{"FIXTURE_TOKEN":"fake-test-only"},"headers":{}}'
    salt_bytes = b"WorldModel-pet-settings\x00"
    incoming = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    salt = (ctypes.c_ubyte * len(salt_bytes)).from_buffer_copy(salt_bytes)
    src, entropy, out = Blob(len(raw), incoming), Blob(len(salt_bytes), salt), Blob()
    crypt = ctypes.WinDLL("crypt32")
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    assert crypt.CryptProtectData(ctypes.byref(src), "fixture", ctypes.byref(entropy), None, None, 1, ctypes.byref(out))
    try:
        encrypted = base64.b64encode(ctypes.string_at(out.data, out.length)).decode()
        assert _decrypt_secret(encrypted)["env"]["FIXTURE_TOKEN"] == "fake-test-only"
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(out.data)
