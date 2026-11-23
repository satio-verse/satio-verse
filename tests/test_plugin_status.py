"""插件宿主第六切片: 把"装了什么、给了什么权限档、有哪几个动作、最近几次调用成没成"做成
只读状态, 挂进能力清单给工作台看.

全程用 tmp_path 的插件目录, 不碰真 ~/.worldmodel/plugins; 只读状态永远不 import 插件代码。
"""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent import plugin_host as ph
from wm.agent.capabilities import inventory
from wm.agent.core import AgentHub
from wm.agent.runtime_http import runtime
from test_plugin_host_actions import IMPORT_MARK_SOURCE, demo_manifest, install

REPO = Path(__file__).resolve().parents[1]


def hub_at(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    hub = AgentHub({"agent": {"workspace": str(root), "extensions": {"settings_path": False}}},
                   home=tmp_path / "agent")
    return hub, hub.new_session(), root


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    root = tmp_path / "plugins"
    monkeypatch.setenv(ph.HOME_ENV, str(root))
    return root


@pytest.fixture
def off(tmp_path, monkeypatch):
    monkeypatch.delenv(ph.SWITCH_ENV, raising=False)
    root = tmp_path / "plugins"
    monkeypatch.setenv(ph.HOME_ENV, str(root))
    return root


# ---- 关着开关: 一个元素都不该多出来 ----

def test_status_touches_no_disk_and_reports_off_when_switch_is_off(off, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("关着开关不该读插件目录")

    monkeypatch.setattr(ph, "home", boom)
    monkeypatch.setattr(ph, "_load_registry", boom)
    out = ph.status()
    assert out["enabled"] is False
    assert "plugins" not in out
    assert "WM_PLUGIN_HOST=on" in out["reason"]
    assert not off.exists()


def test_capabilities_has_no_plugin_block_when_switch_is_off(off, tmp_path):
    hub, session, _ = hub_at(tmp_path)
    data = inventory(hub, session)
    assert "plugins" not in data
    assert {"session", "global", "planned"} == {row["scope"] for row in data["groups"]}


def test_runtime_capability_endpoint_has_no_plugin_block_when_switch_is_off(off, tmp_path):
    async def main():
        hub, session, _ = hub_at(tmp_path)
        app = web.Application()

        async def route(request):
            return await runtime(request, hub)

        app.router.add_route("*", "/runtime", route)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/runtime", params={"view": "capabilities", "id": session.id})
            data = await response.json()
            assert response.status == 200 and data["ok"]
            assert "plugins" not in data

    asyncio.run(main())


# ---- 开关开着: 只读地列出已装插件、权限档、动作、最近调用 ----

def test_status_lists_installed_plugin_tier_and_actions(host, tmp_path):
    plugin_id = install(tmp_path)
    assert ph.grant_tier(plugin_id, "panel", why="演练：允许动手")["ok"] is True
    out = ph.status()
    assert out["enabled"] is True and out["ok"] is True
    assert out["home"] == str(host.resolve())
    row = out["plugins"][0]
    assert row["id"] == plugin_id and row["enabled"] is True
    assert row["granted_tier"] == "panel" and row["declared_tier"] == "panel"
    assert row["source"] == ph.SOURCE
    actions = {item["name"]: item for item in row["actions"]}
    assert set(actions) == {"demo_look", "demo_move"}
    assert actions["demo_move"]["kind"] == "act" and actions["demo_look"]["kind"] == "query"
    assert actions["demo_move"]["why"] == "往房间里挪东西"
    assert row["actions_source"] == ph.ENTRY_NAME
    assert row["recent"] == {"calls": 0, "ok": 0, "failed": 0, "last": []}
    assert "manifest_error" not in row


def test_status_filters_one_plugin_and_rejects_unknown_ids(host, tmp_path):
    install(tmp_path)
    other_actions = [{"name": "other_look", "kind": "query", "why": "只读看看另一处"}]
    install(tmp_path, name="pkg2", manifest_obj=demo_manifest(
        other_actions, plugin_id="other-tool", prefix="other", name="另一个演示工具"))
    one = ph.status("other-tool")
    assert [row["id"] for row in one["plugins"]] == ["other-tool"]
    missing = ph.status("nope")
    assert missing["ok"] is False and "没有装这个插件" in missing["reason"]
    assert ph.status("../escape")["ok"] is False


def test_status_reads_actions_without_importing_the_plugin(host, tmp_path):
    install(tmp_path, source=IMPORT_MARK_SOURCE)
    out = ph.status()
    assert not (host / "demo-tool" / "imported.flag").exists()
    actions = {item["name"]: item for item in out["plugins"][0]["actions"]}
    assert actions["demo_look"]["in_entry"] is True     # ACTIONS 字面量里有
    assert actions["demo_move"]["in_entry"] is False    # 清单声明了, plugin.py 里没有


def test_status_summarizes_recent_calls_from_the_audit(host, tmp_path):
    plugin_id = install(tmp_path)
    assert ph.grant_tier(plugin_id, "panel", why="演练")["ok"] is True
    ok = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert ok["ok"] is True
    failed = ph.invoke(plugin_id, "demo_move", {})      # 缺必填参数: 宿主侧拒掉, 也记一行审计
    assert failed["ok"] is False
    recent = ph.status()["plugins"][0]["recent"]
    assert recent["calls"] == 2 and recent["ok"] == 1 and recent["failed"] == 1
    assert [row["ok"] for row in recent["last"]] == [True, False]
    assert recent["last"][1]["error_code"] == failed["error"]["code"]
    assert recent["last"][0]["tool"] == "demo_look"


def test_status_keeps_only_the_last_few_calls(host, tmp_path):
    plugin_id = install(tmp_path)
    for _ in range(ph.STATUS_RECENT + 2):
        assert ph.invoke(plugin_id, "demo_look", {"room": "x"})["ok"] is True
    recent = ph.status("demo-tool")["plugins"][0]["recent"]
    assert recent["calls"] == ph.STATUS_RECENT + 2
    assert len(recent["last"]) == ph.STATUS_RECENT
    assert all(row["ok"] is True for row in recent["last"])
    assert ph.STATUS_RECENT >= 3


def test_status_reports_a_broken_registry_instead_of_raising(host, tmp_path):
    host.mkdir(parents=True)
    (host / ph.REGISTRY_NAME).write_text("{ 这不是 json", encoding="utf-8")
    out = ph.status()
    assert out["enabled"] is True and out["ok"] is False
    assert "登记表" in out["reason"]


def test_capabilities_carry_plugin_status_only_when_switch_is_on(host, tmp_path):
    hub, session, _ = hub_at(tmp_path)
    empty = inventory(hub, session)["plugins"]
    assert empty["ok"] is True and empty["plugins"] == []
    install(tmp_path)
    data = inventory(hub, session)
    host_block = data["plugins"]
    assert host_block["ok"] is True and host_block["home"] == str(host.resolve())
    assert [row["id"] for row in host_block["plugins"]] == ["demo-tool"]
    assert {"session", "global", "planned"} == {row["scope"] for row in data["groups"]}


def test_runtime_capability_endpoint_exposes_plugin_status(host, tmp_path):
    async def main():
        install(tmp_path)
        hub, session, _ = hub_at(tmp_path)
        app = web.Application()

        async def route(request):
            return await runtime(request, hub)

        app.router.add_route("*", "/runtime", route)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/runtime", params={"view": "capabilities", "id": session.id})
            data = await response.json()
            assert response.status == 200 and data["ok"]
            assert data["plugins"]["ok"] is True
            assert data["plugins"]["plugins"][0]["id"] == "demo-tool"

    asyncio.run(main())


# ---- 前端: 只有服务端下发了 plugins 才渲染, 关着时一个元素都不多 ----

def test_workbench_renders_the_plugin_card_only_when_the_server_sends_it():
    text = (REPO / "desktop" / "web" / "workbench-canvas.js").read_text(encoding="utf-8")
    assert "function pluginHostHtml(data)" in text
    assert "+pluginHostHtml(data)+" in text
    segment = text.split("function pluginHostHtml", 1)[1].split("// Agent 预设", 1)[0]
    assert "data.plugins" in segment
    assert "return ''" in segment
