"""插件宿主第二切片: 显式提权、动作执行器、挂进她的工具列表 (任务 A/B).

只测 `wm/agent/plugin_host.py` 新增部分和 `studio` 的工具面接入; 全程用 tmp_path 的插件目录,
不碰真 `~/.worldmodel/plugins`; 假插件只在插件目录里记调用, 不碰用户文件。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wm.agent import plugin_host as ph
from wm.agent import plugin_manifest as pm
from wm.agent.core import AgentHub
from wm.agent.studio import StudioRuntime

DEFAULT_ACTIONS = [
    {
        "name": "demo_look",
        "kind": "query",
        "why": "只读看看这个房间",
        "params": {
            "room": {"type": "string", "required": True, "max_len": 4000},
            "limit": {"type": "int"},
            "mode": {"type": "enum", "values": ["fast", "full"]},
        },
    },
    {
        "name": "demo_move",
        "kind": "act",
        "outward": False,
        "receipt": "moved",
        "why": "往房间里挪东西",
        "params": {
            "room": {"type": "string", "required": True},
            "force": {"type": "bool"},
        },
    },
]

PLUGIN_SOURCE = '''
from pathlib import Path

CALLS = Path(__file__).with_name("calls.log")


def _log(name):
    with CALLS.open("a", encoding="utf-8") as stream:
        stream.write(name + "\\n")


def look(room, limit=None, mode=None):
    _log("look")
    return {"room": room, "seen": ["a", "b"], "limit": limit, "mode": mode}


def move(room, force=False):
    _log("move")
    if room == "boom":
        raise RuntimeError("插件自己的错误" * 40)
    return {"moved": room, "force": force, "receipt": "rc-" + room}


ACTIONS = {"demo_look": look, "demo_move": move}
'''

IMPORT_MARK_SOURCE = '''
from pathlib import Path

Path(__file__).with_name("imported.flag").write_text("yes", encoding="utf-8")


def look(room):
    return {"room": room}


ACTIONS = {"demo_look": look}
'''


def demo_manifest(actions=None, *, plugin_id="demo-tool", prefix="demo", name="演示工具"):
    """一份合法的最小 space 档清单: 动作挂在 plugin.world.actions 上 (契约里动作只在这里)."""
    return {
        "id": plugin_id,
        "name": name,
        "description": "测试用",
        "plugin": {
            "schema": 2,
            "version": "1.0.0",
            "author": "测试",
            "license": "MIT",
            "host": ">=0.4",
            "tier": "panel",
            "permissions": {},
            "panel": {"entry": "spectate.html"},
            "world": {
                "kind": "space",
                "prefix": prefix,
                "events": [{"type": "moved", "sense": "receipt"}],
                "actions": DEFAULT_ACTIONS if actions is None else actions,
                "perception": {"visible": ["room"], "twin_fixtures": ["fixtures/twin.json"]},
                "spectate": {"panel": "spectate.html"},
            },
        },
    }


def package(tmp_path, manifest_obj=None, source=PLUGIN_SOURCE, *, name="pkg"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / ph.MANIFEST_NAME).write_text(
        json.dumps(manifest_obj or demo_manifest(), ensure_ascii=False), encoding="utf-8")
    (root / "plugin.py").write_text(source, encoding="utf-8")
    (root / "spectate.html").write_text("<!doctype html><title>demo</title>", encoding="utf-8")
    (root / "fixtures").mkdir(exist_ok=True)
    (root / "fixtures" / "twin.json").write_text("{}", encoding="utf-8")
    return root


def install(tmp_path, *, manifest_obj=None, source=PLUGIN_SOURCE, enable=True, name="pkg"):
    result = ph.install(package(tmp_path, manifest_obj, source, name=name))
    assert result["ok"] is True, result
    plugin_id = result["id"]
    if enable:
        assert ph.set_enabled(plugin_id, True)["ok"] is True
    return plugin_id


def audit_rows(root):
    path = Path(root) / "audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


@pytest.fixture
def host(tmp_path, monkeypatch):
    """开关打开, 插件目录指到临时目录. 返回插件目录."""
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    root = tmp_path / "plugins"
    monkeypatch.setenv(ph.HOME_ENV, str(root))
    return root


@pytest.fixture
def off(tmp_path, monkeypatch):
    """开关关着 (默认状态), 返回本应使用的插件目录 (不该被建出来)."""
    monkeypatch.delenv(ph.SWITCH_ENV, raising=False)
    root = tmp_path / "plugins"
    monkeypatch.setenv(ph.HOME_ENV, str(root))
    return root


def error_code(result):
    assert result["ok"] is False, result
    return result["error"]["code"]


# ---- 关着开关 ----

def test_switch_off_invoke_and_the_tool_table_touch_no_disk(off, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("关着开关不该读插件目录")

    monkeypatch.setattr(ph, "home", boom)
    monkeypatch.setattr(ph, "_load_registry", boom)
    out = ph.invoke("demo-tool", "demo_look", {"room": "x"})
    assert set(out) == {"ok", "action", "result", "error", "receipt_id"}
    assert out["ok"] is False and out["error"]["code"] == "E_HOST_OFF"
    assert "WM_PLUGIN_HOST=on" in out["error"]["message"]
    assert out["result"] is None and out["receipt_id"] is None
    assert ph.tool_table() == {}
    assert ph.tool_specs() == []
    assert ph.tool_names() == set()
    assert ph.read_only("demo_look") is False
    assert ph.find_tool("demo_look") == (None, None)
    assert not off.exists()


# ---- 提权 ----

def test_grant_tier_walks_text_panel_code_and_writes_audit(host, tmp_path):
    install(tmp_path)
    assert ph.list()["plugins"][0]["granted_tier"] == "text"
    panel = ph.grant_tier("demo-tool", "panel", why="演练：允许移动文件")
    assert panel["ok"] is True and panel["changed"] is True
    assert panel["granted_tier"] == "panel" and panel["previous_tier"] == "text"
    assert ph.list()["plugins"][0]["granted_tier"] == "panel"
    rows = audit_rows(host)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "grant_tier" and row["plugin_id"] == "demo-tool"
    assert row["from"] == "text" and row["to"] == "panel" and row["why"] == "演练：允许移动文件"
    assert row["by"] and row["at"]
    code = ph.grant_tier("demo-tool", "code", why="演练：允许跑代码")
    assert code["ok"] is True and code["granted_tier"] == "code"
    assert len(audit_rows(host)) == 2
    same = ph.grant_tier("demo-tool", "code", why="再提一次")
    assert same["ok"] is True and same["changed"] is False
    assert len(audit_rows(host)) == 2  # 平级不写审计
    down = ph.grant_tier("demo-tool", "panel", why="想降回来")
    assert down["ok"] is False and "只能往上" in down["reason"]
    assert ph.list()["plugins"][0]["granted_tier"] == "code"


def test_grant_tier_never_grants_process_or_unknown_tiers(host, tmp_path):
    install(tmp_path)
    for tier in ("process", "builtin", "root"):
        result = ph.grant_tier("demo-tool", tier, why="试试")
        assert result["ok"] is False and "process" in result["reason"]
    assert audit_rows(host) == []
    assert ph.list()["plugins"][0]["granted_tier"] == "text"


def test_grant_tier_requires_a_reason_and_a_known_plugin(host, tmp_path):
    assert ph.grant_tier("demo-tool", "panel", why="没装")["ok"] is False
    install(tmp_path)
    for why in ("", "   ", None, 5):
        result = ph.grant_tier("demo-tool", "panel", why=why)
        assert result["ok"] is False and "理由" in result["reason"]
    assert ph.list()["plugins"][0]["granted_tier"] == "text"
    assert audit_rows(host) == []


# ---- 执行器 ----

def test_query_runs_at_text_while_act_needs_the_panel_grant(host, tmp_path):
    install(tmp_path)
    assert "demo_look" in ph.tool_names() and "demo_move" not in ph.tool_names()
    look = ph.invoke("demo-tool", "demo_look", {"room": "书房", "limit": 3, "mode": "fast"})
    assert look["ok"] is True and look["error"] is None and look["receipt_id"] is None
    assert look["result"] == {"room": "书房", "seen": ["a", "b"], "limit": 3, "mode": "fast"}
    refused = ph.invoke("demo-tool", "demo_move", {"room": "书房", "force": True})
    assert refused["ok"] is False and refused["error"]["code"] == "E_NEEDS_GRANT"
    assert "需要你先授权" in refused["error"]["message"]
    assert refused["result"] is None and refused["receipt_id"] is None
    calls_log = host / "demo-tool" / "calls.log"
    assert calls_log.read_text(encoding="utf-8").split() == ["look"]  # 被拒时没碰插件
    assert ph.grant_tier("demo-tool", "panel", why="演练")["ok"] is True
    assert {"demo_look", "demo_move"} <= ph.tool_names()
    moved = ph.invoke("demo-tool", "demo_move", {"room": "书房", "force": True})
    assert moved["ok"] is True and moved["receipt_id"] == "rc-书房"
    assert moved["result"]["force"] is True and moved["result"]["moved"] == "书房"


def test_act_tools_disappear_in_read_only_mode(host, tmp_path):
    install(tmp_path)
    ph.grant_tier("demo-tool", "panel", why="演练")
    assert ph.tool_names(read_only=True) == {"demo_look"}
    assert {"demo_look", "demo_move"} == ph.tool_names()


def test_invoke_validates_only_the_params_the_manifest_declares(host, tmp_path):
    install(tmp_path)
    ph.grant_tier("demo-tool", "panel", why="演练")
    assert error_code(ph.invoke("demo-tool", "demo_look", {})) == "E_PARAM_REQUIRED"
    assert error_code(ph.invoke("demo-tool", "demo_look", None)) == "E_PARAM_REQUIRED"
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "x", "nope": 1})) == "E_PARAM_UNKNOWN"
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": 5})) == "E_PARAM_TYPE"
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "x", "limit": "3"})) == "E_PARAM_TYPE"
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "x", "mode": "slow"})) == "E_PARAM_VALUE"
    assert error_code(ph.invoke("demo-tool", "demo_look", ["room"])) == "E_PARAMS"
    assert error_code(ph.invoke("demo-tool", "demo_move", {"room": "x", "force": "yes"})) == "E_PARAM_TYPE"
    # 参数值一律当数据: 原样交给插件, 不做任何字符串求值
    payload = "__import__('os').system('echo pwned'); $(rm -rf /) `id`"
    out = ph.invoke("demo-tool", "demo_look", {"room": payload})
    assert out["ok"] is True and out["result"]["room"] == payload


def test_param_max_len_is_enforced(host, tmp_path):
    actions = [{
        "name": "demo_look", "kind": "query", "why": "留一句话",
        "params": {"room": {"type": "string", "required": True, "max_len": 4}},
    }]
    source = "def look(room):\n    return {'room': room}\n\n\nACTIONS = {'demo_look': look}\n"
    install(tmp_path, manifest_obj=demo_manifest(actions), source=source)
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "12345"})) == "E_PARAM_LENGTH"
    assert ph.invoke("demo-tool", "demo_look", {"room": "1234"})["ok"] is True


def test_invoke_rejects_uninstalled_disabled_unknown_and_bad_names(host, tmp_path):
    install(tmp_path, enable=False)
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "x"})) == "E_NOT_ENABLED"
    ph.set_enabled("demo-tool", True)
    assert error_code(ph.invoke("other-tool", "demo_look", {"room": "x"})) == "E_NOT_INSTALLED"
    assert error_code(ph.invoke("Bad Tool", "demo_look", {"room": "x"})) == "E_PLUGIN_ID"
    assert error_code(ph.invoke("demo-tool", "nope", {})) == "E_ACTION_NAME"
    assert error_code(ph.invoke("demo-tool", "demo_other", {})) == "E_NO_ACTION"


def test_import_failure_is_structured_and_does_not_leak_a_traceback(host, tmp_path):
    install(tmp_path, source='raise RuntimeError("顶层炸了")\n')
    out = ph.invoke("demo-tool", "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_ENTRY_IMPORT"
    message = out["error"]["message"]
    assert "RuntimeError" in message and "顶层炸了" in message
    assert "Traceback" not in message and len(message) < 400


def test_missing_entry_mapping_and_action_are_structured_errors(host, tmp_path):
    install(tmp_path, source="def look(room):\n    return {'room': room}\n")
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "x"})) == "E_ENTRY_MISSING"
    ph.uninstall("demo-tool")
    install(tmp_path, source="ACTIONS = {}\n", name="pkg2")
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "x"})) == "E_ACTION_MISSING"
    ph.uninstall("demo-tool")
    install(tmp_path, source="ACTIONS = {'demo_look': 'not-callable'}\n", name="pkg3")
    assert error_code(ph.invoke("demo-tool", "demo_look", {"room": "x"})) == "E_ACTION_MISSING"


def test_plugin_exception_is_structured_and_short(host, tmp_path):
    install(tmp_path)
    ph.grant_tier("demo-tool", "panel", why="演练")
    out = ph.invoke("demo-tool", "demo_move", {"room": "boom"})
    assert out["ok"] is False and out["error"]["code"] == "E_CRASH"  # 第三切片②: 插件动作在子进程里跑, 异常是崩溃
    message = out["error"]["message"]
    assert "RuntimeError" in message and "插件自己的错误" in message
    assert "Traceback" not in message and len(message) < 260


def test_bad_return_from_the_plugin_is_a_structured_error(host, tmp_path):
    install(tmp_path, source="def look(room):\n    return [room]\n\n\nACTIONS = {'demo_look': look}\n")
    out = ph.invoke("demo-tool", "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_RESULT_TYPE"
    ph.uninstall("demo-tool")
    source = "ACTIONS = {'demo_look': lambda room: {'room': object()}}\n"
    install(tmp_path, source=source, name="pkg2")
    out = ph.invoke("demo-tool", "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_RESULT_NOT_JSON"


# ---- 工具面 ----

def test_tool_specs_name_description_and_parameters(host, tmp_path):
    install(tmp_path)
    spec = {row["function"]["name"]: row["function"] for row in ph.tool_specs()}
    assert set(spec) == {"demo_look"}  # text 档只有 query 动作
    fn = spec["demo_look"]
    assert fn["description"] == "只读看看这个房间"
    parameters = fn["parameters"]
    assert parameters["type"] == "object" and parameters["additionalProperties"] is False
    assert parameters["required"] == ["room"]
    assert parameters["properties"]["room"] == {"type": "string", "maxLength": 4000}
    assert parameters["properties"]["limit"] == {"type": "integer"}
    assert parameters["properties"]["mode"] == {"type": "string", "enum": ["fast", "full"]}
    ph.grant_tier("demo-tool", "panel", why="演练")
    assert {row["function"]["name"] for row in ph.tool_specs()} == {"demo_look", "demo_move"}
    assert ph.read_only("demo_look") is True
    assert ph.read_only("demo_move") is False
    assert ph.read_only("read_file") is False


def test_building_the_tool_table_never_imports_plugin_code(host, tmp_path):
    install(tmp_path, source=IMPORT_MARK_SOURCE)
    plugin_dir = host / "demo-tool"
    assert not (plugin_dir / "imported.flag").exists()
    ph.tool_table()
    ph.tool_specs()
    ph.tool_names()
    ph.read_only("demo_look")
    ph.find_tool("demo_look")
    ph.tool_names(read_only=True)
    assert not (plugin_dir / "imported.flag").exists()  # 扫清单/建工具表没有执行插件顶层代码
    assert ph.invoke("demo-tool", "demo_look", {"room": "x"})["ok"] is True
    assert (plugin_dir / "imported.flag").exists()  # 只有 invoke 才 import


# ---- 挂进她的工具列表 (studio 同一层) ----

class _Presets:
    def get(self, name):
        return {"tools": ["*"]}


class _Teams:
    mode = "unrestricted"

    def effective_mode(self, session):
        return self.mode


class _Blacklist:
    def check(self, name, args, path):
        return None


class _Hub:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.presets = _Presets()
        self.teams = _Teams()
        self.blacklist = _Blacklist()


class _Session:
    preset = "standard"

    def __init__(self, workspace):
        self.workspace = str(workspace)
        self.stop_event = SimpleNamespace(is_set=lambda: False)


def studio(tmp_path):
    return StudioRuntime(_Hub(tmp_path / "agent-home"))


def test_tool_list_is_identical_until_an_enabled_plugin_appears(host, tmp_path, monkeypatch):
    runtime = studio(tmp_path)
    session = _Session(tmp_path)
    baseline_tools = runtime.tools(session)
    baseline_names = runtime.names(session)
    baseline = [row["function"]["name"] for row in baseline_tools]
    assert not any(name.startswith("demo_") for name in baseline)
    install(tmp_path, enable=False)  # 装了但停着
    assert runtime.tools(session) == baseline_tools
    assert runtime.names(session) == baseline_names
    ph.set_enabled("demo-tool", True)  # 启用了: 只多出 query 动作
    after_tools = runtime.tools(session)
    after = [row["function"]["name"] for row in after_tools]
    assert after == baseline + ["demo_look"]
    old_rows = {row["function"]["name"]: row for row in baseline_tools}
    for row in after_tools:
        if row["function"]["name"] in old_rows:
            assert row == old_rows[row["function"]["name"]]  # 原有工具的描述/参数一个都没变
    assert ph.grant_tier("demo-tool", "panel", why="演练")["ok"] is True
    assert [row["function"]["name"] for row in runtime.tools(session)] == baseline + ["demo_look", "demo_move"]
    ph.set_enabled("demo-tool", False)  # 再停用: 逐项回到基线
    assert runtime.tools(session) == baseline_tools
    assert runtime.names(session) == baseline_names
    assert ph.set_enabled("demo-tool", True)["ok"] is True
    monkeypatch.delenv(ph.SWITCH_ENV)  # 宿主开关关着: 就算插件开着也一条不多
    assert runtime.tools(session) == baseline_tools
    assert runtime.names(session) == baseline_names


def test_studio_dispatches_plugin_actions_and_hides_act_in_read_mode(host, tmp_path):
    install(tmp_path)
    runtime = studio(tmp_path)
    session = _Session(tmp_path)
    assert {"demo_look"} <= runtime.names(session) and "demo_move" not in runtime.names(session)
    assert runtime.plugin_tool("demo_look")["plugin"] == "demo-tool"
    assert runtime.is_plugin_tool("demo_look") is True
    assert runtime.is_plugin_tool("read_file") is False
    assert runtime.decision(session, "demo_look", {"room": "x"}).how == "auto"
    out = asyncio.run(runtime.call(session, "demo_look", {"room": "x"}))
    assert out["ok"] is True and out["result"]["room"] == "x"
    bad = asyncio.run(runtime.call(session, "demo_look", {"room": 5}))
    assert bad["isError"] is True and bad["error"]["code"] == "E_PARAM_TYPE"
    ph.grant_tier("demo-tool", "panel", why="演练")
    assert "demo_move" in runtime.names(session)
    moved = asyncio.run(runtime.call(session, "demo_move", {"room": "x"}))
    assert moved["ok"] is True and moved["receipt_id"] == "rc-x"
    session.preset = "plan"  # 计划模式 = 只读档: act 不出现, query 还在
    assert "demo_look" in runtime.names(session) and "demo_move" not in runtime.names(session)


def test_core_routes_plugin_tool_calls_through_the_studio(host, tmp_path):
    install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    hub = AgentHub({"agent": {"workspace": str(project), "extensions": {"settings_path": False}}},
                   home=tmp_path / "agent")
    session = hub.new_session(mode="unrestricted")
    offered = hub._offered(session)
    assert "demo_look" in offered
    assert "demo_look" in {row["function"]["name"] for row in hub.studio.tools(session)}
    call = {"id": "call-1", "function": {"name": "demo_look",
                                         "arguments": json.dumps({"room": "书房"}, ensure_ascii=False)}}
    text, error = asyncio.run(hub._tool(session, call, offered))
    assert error is False, text
    assert "书房" in text
    assert ph.grant_tier("demo-tool", "panel", why="演练")["ok"] is True
    offered = hub._offered(session)
    assert "demo_move" in offered
    call = {"id": "call-2", "function": {"name": "demo_move", "arguments": json.dumps({"room": "x"})}}
    text, error = asyncio.run(hub._tool(session, call, offered))
    assert error is False, text
    assert "rc-x" in text


def test_a_broken_registry_does_not_break_the_tool_list(host, tmp_path):
    (host).mkdir(parents=True, exist_ok=True)
    (host / "registry.json").write_text("{ not json", encoding="utf-8")
    runtime = studio(tmp_path)
    assert ph.tool_table() == {}
    assert ph.tool_names() == set()
    assert not any(name.startswith("demo_") for name in runtime.names(_Session(tmp_path)))
