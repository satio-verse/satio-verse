"""No real SnowLuma, QQ login, model call, or social traffic in these tests."""
import asyncio
import json
import functools
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wm.channels import snowluma as S
from wm.channels.manager import parse_conf
from wm.channels.onebot import OneBotClient


def async_test(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


@pytest.fixture
def install(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / ("node.exe" if os.name == "nt" else "node")).write_bytes(b"fake")
    (tmp_path / "index.mjs").write_text("// fixture")
    (tmp_path / "config" / "consent.json").write_text(json.dumps({"version": "fixture", "acceptedAt": "2026-10-31"}))
    return tmp_path


def conf(path, **kw):
    return {"enabled": True, "snowluma_dir": str(path), "endpoint": "ws://127.0.0.1:3001/", **kw}


@pytest.fixture
def offline(monkeypatch):
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(S, "_listening", probe)
    monkeypatch.setattr(S, "_existing_process", lambda _: None)
    process = SimpleNamespace(pid=12345, poll=lambda: None)
    calls = []
    monkeypatch.setattr(S, "_spawn", lambda directory: calls.append(directory) or process)
    return calls, process


@async_test
async def test_disabled_or_manual_never_probes_or_spawns(install, offline):
    launcher = S.SnowLumaLauncher()
    assert (await launcher.ensure(conf(install, enabled=False)))["state"] == "off"
    assert (await launcher.ensure(conf(install, snowluma_autostart=False)))["state"] == "manual"
    S._listening.assert_not_awaited()
    assert not offline[0]


@async_test
async def test_existing_endpoint_is_reused_even_without_directory(monkeypatch):
    monkeypatch.setattr(S, "_listening", AsyncMock(return_value=True))
    monkeypatch.setattr(S, "_spawn", lambda _: pytest.fail("must reuse listener"))
    assert (await S.SnowLumaLauncher().ensure(conf("")))["state"] == "ready"


@async_test
async def test_concurrent_reconnects_start_once_and_keep_process(install, offline):
    launcher = S.SnowLumaLauncher()
    results = await asyncio.gather(*(launcher.ensure(conf(install)) for _ in range(6)))
    assert len(offline[0]) == 1
    assert all(r["state"] == "starting" for r in results)
    launcher.inactive()
    assert launcher.status()["state"] == "off"
    assert offline[1].poll() is None
    await launcher.ensure(conf(install))
    assert len(offline[0]) == 1


@async_test
async def test_existing_process_waits_for_account_without_duplicate(install, offline, monkeypatch):
    monkeypatch.setattr(S, "_existing_process", lambda _: 54321)
    state = await S.SnowLumaLauncher().ensure(conf(install))
    assert state["state"] == "waiting_account"
    assert state["pid"] == 54321
    assert not offline[0]


@async_test
async def test_start_failure_backoff_and_no_exception_secrets(install, offline, monkeypatch):
    def broken(_):
        raise OSError("secret-token-should-not-leak")
    monkeypatch.setattr(S, "_spawn", broken)
    launcher = S.SnowLumaLauncher()
    state = await launcher.ensure(conf(install))
    assert state["state"] == "error"
    assert "secret-token" not in json.dumps(state)
    monkeypatch.setattr(S, "_spawn", lambda _: pytest.fail("must back off"))
    assert await launcher.ensure(conf(install)) == state


@async_test
async def test_exited_child_is_not_restart_loop(install, offline):
    launcher = S.SnowLumaLauncher()
    await launcher.ensure(conf(install))
    offline[1].poll = lambda: 1
    assert (await launcher.ensure(conf(install)))["state"] == "error"
    assert len(offline[0]) == 1
    launcher._retry_at = 0
    assert (await launcher.ensure(conf(install)))["state"] == "starting"
    assert len(offline[0]) == 2


@async_test
async def test_missing_install_and_consent_need_setup(tmp_path, install, offline):
    launcher = S.SnowLumaLauncher()
    assert (await launcher.ensure(conf("")))["state"] == "not_configured"
    assert (await launcher.ensure(conf(install / "missing")))["state"] == "missing"
    (install / "config" / "consent.json").unlink()
    assert (await launcher.ensure(conf(install)))["state"] == "setup_required"
    assert not offline[0]


@async_test
async def test_real_tcp_probe_is_loopback_only():
    async def handler(reader, writer):
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        assert await S._listening(f"ws://127.0.0.1:{port}/")
        with pytest.raises(ValueError):
            await S._listening("ws://example.com:3001/")
    finally:
        server.close()
        await server.wait_closed()


def test_environment_never_passes_model_keys_or_preloads(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("NODE_OPTIONS", "--require bad.js")
    monkeypatch.setenv("SNOWLUMA_ACCEPT_EULA", "1")
    monkeypatch.setenv("SNOWLUMA_ACCEPT_PRIVACY", "1")
    monkeypatch.setenv("SNOWLUMA_HOOK_AUTOLOAD", "1")
    monkeypatch.setenv("PATH", "system-path")
    env = S._environment()
    assert env["PATH"] == "system-path"
    assert not any(k in env for k in ["DEEPSEEK_API_KEY", "NODE_OPTIONS", "SNOWLUMA_ACCEPT_EULA", "SNOWLUMA_ACCEPT_PRIVACY", "SNOWLUMA_HOOK_AUTOLOAD"])


def test_spawn_uses_bundled_node_no_shell_hidden_and_no_output_capture(install, monkeypatch):
    seen = {}
    monkeypatch.setattr(S.subprocess, "Popen", lambda args, **kw: seen.update(args=args, **kw))
    S._spawn(install)
    assert seen["args"] == [str(install / ("node.exe" if os.name == "nt" else "node")), str(install / "index.mjs")]
    assert seen["cwd"] == install
    assert not seen.get("shell")
    assert seen["stdout"] == seen["stderr"] == seen["stdin"] == S.subprocess.DEVNULL
    if os.name == "nt":
        assert seen["creationflags"] & S.subprocess.CREATE_NO_WINDOW


def test_manual_global_node_launcher_is_reused(install, monkeypatch):
    import psutil
    from wm.channels import snowluma_hook as H
    state = {"cwd": str(install)}
    process = SimpleNamespace(cmdline=lambda: ["node", "./index.mjs"], cwd=lambda: state["cwd"])
    monkeypatch.setattr(H, "process_names", lambda: {12: "node.exe", 99: "notepad.exe"})
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    assert S._existing_process(install) == 12
    state["cwd"] = str(install / "other")
    assert S._existing_process(install) is None


def test_an_unreadable_node_process_stops_a_second_launch(install, monkeypatch):
    import psutil
    from wm.channels import snowluma_hook as H

    def locked(pid):
        raise psutil.AccessDenied(pid)
    monkeypatch.setattr(H, "process_names", lambda: {7: "node.exe"})
    monkeypatch.setattr(psutil, "Process", locked)
    with pytest.raises(RuntimeError):  # 看不清是不是 SnowLuma: 宁可不起, 也不起第二份
        S._existing_process(install)


def test_launch_lock_rejects_another_manager(install):
    with S._launch_lock(install):
        with pytest.raises(OSError):
            with S._launch_lock(install):
                pytest.fail("duplicate lock entered")


def test_autostart_default_and_opt_out():
    assert parse_conf({})[0]["snowluma_autostart"] is True
    assert parse_conf({"snowluma_autostart": False})[0]["snowluma_autostart"] is False


@async_test
async def test_onebot_runs_startup_hook_before_getting_token():
    order = []
    async def preconnect():
        order.append("ensure")
    def token():
        order.append("token")
        client._stopping = True
        raise ValueError("fixture stop")
    client = OneBotClient("ws://127.0.0.1:3001", token, AsyncMock(), AsyncMock(), before_connect=preconnect)
    await client._run()
    assert order == ["ensure", "token"]


def test_native_and_ui_forward_opt_out():
    root = Path(__file__).resolve().parents[1]
    native = (root / "desktop/host/src/settings.cpp").read_text(encoding="utf-8")
    assert 'out.set("snowluma_autostart", q["snowluma_autostart"].boolean(true))' in native
    ui = (root / "desktop/web/panel-qq.js").read_text(encoding="utf-8")
    assert "q.snowluma_autostart = t.checked" in ui
    assert "ctx.esc(s.snowluma.detail)" in ui
