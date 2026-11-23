"""插件宿主第 0 步 (1/3): 扩展登记表、扩展工具快照、扩展审计记录、她自己写的 MCP 的名单, 不让任务直接改.

自动执行 (full) 和完全授权 (unrestricted) 两档以前只认黑名单, 默认保护名单里有钩子配置、权限名单, 没有这几份
(2026-11-11 用临时目录调守门代码核对过): 她能用写文件的工具直接改登记表 / 快照, 等于自己决定哪个版本的扩展开着、
哪些扩展工具免问直接调、宿主起哪些程序. 现在这几份是默认保护项 (只拦写, 读照旧; 版本 3 的迁移把它们补进老的黑名单文件).
全在临时目录, 家目录 (她的 MCP 名单按它算) 也换成临时的, 真的 ~/.worldmodel 一个字不碰."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_agent_direct_tools import Session, build
from wm.agent import policy as P
from wm.agent.extensions import ExtensionRegistry
from wm.agent.governance import DEFAULT_RULES_VERSION, MAX_RULES, BlacklistStore, _validate
from wm.agent.studio import StudioRuntime

MODES = ("workspace", "full", "unrestricted")
NAMES = ["登记表", "工具快照", "审计记录", "她的 MCP 名单"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(P, "_home", lambda: home)
    ws = tmp_path / "project"
    ws.mkdir()
    return SimpleNamespace(store=BlacklistStore(tmp_path / "agent"), home=home, ws=ws, tmp=tmp_path)


def protected(w):
    ext = w.store.home / "extensions"
    return {"登记表": ext / "registry.json", "工具快照": ext / "direct-tools.json", "审计记录": ext / "audit.jsonl",
            "她的 MCP 名单": w.home / ".worldmodel" / "self-mcp" / "registry.json"}


def decide(w, tool, path, mode, workspace=None):
    """AgentHub._tool 里的先后: 先黑名单 (所有档), 再按档位的固定守门."""
    workspace = workspace or w.ws
    return w.store.check(tool, {"path": str(path)}, workspace) or P.check_path(tool, Path(path), mode, workspace)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
@pytest.mark.parametrize("which", NAMES)
def test_write_tools_cannot_touch_them_in_any_mode(world, mode, tool, which):
    d = decide(world, tool, protected(world)[which], mode)
    assert d.how == "deny" and "命中黑名单" in d.why


@pytest.mark.parametrize("mode", MODES)
def test_reading_them_stays_allowed(world, mode):
    for path in protected(world).values():
        assert decide(world, "read_file", path, mode).how == "auto", path
    assert decide(world, "list_dir", world.store.home / "extensions", mode).how == "auto"


def test_a_workspace_that_contains_them_does_not_open_them(world):
    """工作区选在工作台目录的上一层: 工作区内修改档的固定守门以为在工作区里随便改, 黑名单照样拦."""
    path = protected(world)["登记表"]
    workspace = world.store.home.parent
    assert P.check_path("write_file", path, "workspace", workspace).how == "auto"
    assert decide(world, "write_file", path, "workspace", workspace).how == "deny"


@pytest.mark.parametrize("template", [
    "Set-Content -Path '{p}' -Value '{{}}'", 'Add-Content "{p}" "x"', "echo x > {p}", "Remove-Item {p}", "del {p}",
    "python -c \"open(r'{p}','w').write('{{}}')\"", "Copy-Item .\\evil.json {fwd}", "Move-Item {p} {p}.old"])
def test_commands_that_name_them_are_denied(world, template):
    for which, path in protected(world).items():
        command = template.format(p=path, fwd=str(path).replace("\\", "/"))
        d = world.store.check("run_command", {"command": command, "workdir": str(world.ws)}, world.ws)
        assert d is not None and d.how == "deny", (which, command)


def test_tilde_in_a_command_means_the_home_folder(tmp_path, monkeypatch):
    """命令里写 ~/.worldmodel/... (PowerShell 认作家目录) 也对得上这几份; ASUS~1、HEAD~1 这样的不是家目录."""
    home = tmp_path / "home"
    monkeypatch.setattr(P, "_home", lambda: home)
    store = BlacklistStore(home / ".worldmodel" / "agent")
    ws = tmp_path / "project"
    ws.mkdir()
    for command in ("Set-Content ~/.worldmodel/agent/extensions/registry.json '{}'",
                    r"Set-Content ~\.worldmodel\agent\extensions\direct-tools.json x",
                    'Add-Content "~/.worldmodel/agent/extensions/audit.jsonl" x',
                    "echo x > ~/.worldmodel/self-mcp/registry.json"):
        assert store.check("run_command", {"command": command}, ws).how == "deny", command
    for command in ("git show HEAD~1", r"type C:\Users\ASUS~1\notes.txt", "echo ~", "Get-Content ~/notes.txt"):
        assert store.check("run_command", {"command": command}, ws) is None, command


def test_neighbours_are_not_swept_up(world):
    """只护这几份: 同名的项目文件、旁边的备份、她的别的 MCP 的数据目录不受影响."""
    ext = world.store.home / "extensions"
    for path in (ext / "registry.json.bak", world.store.home / "context" / "a.txt", world.ws / "registry.json",
                 world.home / ".worldmodel" / "self-mcp" / "weather" / "data" / "cache.json"):
        assert world.store.check("write_file", {"path": str(path)}, world.ws) is None, path
    assert world.store.check("run_command", {"command": f"type {ext / 'registry.json.bak'}"}, world.ws) is None


def test_the_dedicated_flow_still_updates_the_protected_files(world):
    """专用的路径 (extension_create / extension_enable / 回退) 在进程里直接写这几份文件, 不走工具参数检查, 照常能用;
    这几个工具的调用参数也不会被默认保护项误拦 (包里叫 registry.json 的文件只是包内的相对路径)."""
    registry = ExtensionRegistry(world.store.home, settings_path=False, skill_roots=[], self_mcp_root=False)
    made = registry.create({"id": "demo", "name": "演示", "files": {"SKILL.md": "---\nname: demo\ndescription: d\n---\nhi"}})
    registry.set_enabled("demo", True, authorized=True, revision=made["revision"], by="user")
    ext = world.store.home / "extensions"
    package = json.loads((ext / "registry.json").read_text("utf-8"))["packages"]["demo"]
    assert package["enabled"] and package["active_revision"] == made["revision"]
    assert len((ext / "audit.jsonl").read_text("utf-8").splitlines()) == 2
    spec = {"id": "demo2", "name": "演示二", "files": {"registry.json": "{}", "audit.jsonl": "", "SKILL.md": "x"}}
    for tool, args in [("extension_create", {"spec": spec}), ("extension_enable", {"id": "demo", "enabled": True}),
                       ("extension_rollback", {"id": "demo", "revision": made["revision"]})]:
        assert world.store.check(tool, args, world.ws) is None, tool


def test_direct_tools_keep_working_with_the_new_defaults(world):
    """直通工具 (快照里的扩展工具直接放进她的工具表) 在真的默认黑名单下照常: 只读的自动, 写的按档位问或自动."""
    hub = build(world.store.home)
    hub.blacklist = world.store
    studio = StudioRuntime(hub)
    assert {"demo_read", "demo_write"} <= studio.direct.names()
    hub.teams.mode = "full"
    assert studio.decision(Session(), "demo_read", {}).how == "auto"
    assert studio.decision(Session(), "demo_write", {}).how == "ask"
    hub.teams.mode = "unrestricted"
    assert studio.decision(Session(), "demo_write", {}).how == "auto"


def downgrade_to_v2(store):
    """把默认名单倒回版本 2 的样子 (没有版本 3-6 新增的规则, 版本号 2). 返回拿掉了几条."""
    state = json.loads(store.path.read_text("utf-8"))
    new = {(r["kind"], _validate(r)["value"])
           for r in [*store._extension_defaults(), *store._elevation_defaults(), *store._state_defaults(),
                     *store._heartbeat_defaults(), *store._plugins_defaults()]}
    kept = [r for r in state["rules"] if (r["kind"], r["value"]) not in new]
    removed = len(state["rules"]) - len(kept)
    state["rules"], state["default_rules_version"] = kept, 2
    store.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return removed


def test_version_3_migration_adds_the_new_defaults_once(world):
    store = world.store
    total, revision = len(store.list()), store.snapshot()["revision"]
    removed = downgrade_to_v2(store)
    assert (removed == len(store._extension_defaults()) + len(store._elevation_defaults()) + len(store._state_defaults())
            + len(store._heartbeat_defaults()) + len(store._plugins_defaults()) >= 4 + 8 + 3 + 1 + 1)
    assert DEFAULT_RULES_VERSION >= 3
    path = protected(world)["登记表"]
    assert store.check("write_file", {"path": str(path)}, world.ws) is None  # 倒回去以后确实没挡
    migrated = BlacklistStore(store.home)
    assert len(migrated.list()) == total and all(r["origin"] == "default" for r in migrated.list())
    assert json.loads(migrated.path.read_text("utf-8"))["default_rules_version"] == DEFAULT_RULES_VERSION
    audit = migrated.audit()[0]
    assert audit["action"] == "migrate_defaults" and (audit["from_version"], audit["to_version"]) == (2, DEFAULT_RULES_VERSION)
    assert len(audit["added"]) == removed and not audit["skipped"]
    assert migrated.snapshot()["revision"] == revision + 1  # 迁移只存一次 (倒回去是直接改的文件, 不算)
    assert migrated.check("write_file", {"path": str(path)}, world.ws).how == "deny"
    assert migrated.check("run_command", {"command": "Start-Process cmd -Verb RunAs"}, world.ws).how == "deny"
    before = migrated.path.read_bytes()
    BlacklistStore(store.home)
    assert migrated.path.read_bytes() == before  # 只迁移一次


def test_version_3_migration_respects_what_the_user_already_decided(world):
    store = world.store
    removed = downgrade_to_v2(store)
    editor = BlacklistStore(store.home, defaults=False)  # 老文件里用户自己改过的: 不触发迁移
    mine = editor.add({"kind": "command", "value": "RunAs", "reason": "我自己加的"})
    editor.update(mine["id"], {"enabled": False})  # 又被他停用了
    editor.add({"kind": "path", "value": str(store.home / "extensions"), "operations": "write", "reason": "我自己护着整个扩展目录"})
    migrated = BlacklistStore(store.home)
    audit = migrated.audit()[0]
    skipped = {item["path"] for item in audit["skipped"]}
    assert skipped == {"runas", *(str(store.home / "extensions" / name) for name in ("registry.json", "direct-tools.json", "audit.jsonl"))}
    assert len(audit["added"]) == removed - 4  # 其余的照加: 7 条命令、她的 MCP 名单、版本 4 三份状态、版本 5 心跳目录、版本 6 插件目录
    assert migrated.check("run_command", {"command": "runas /user:x cmd"}, world.ws) is None  # 他停用的不替他重新开
    assert migrated.check("run_command", {"command": "runas.exe /user:x cmd"}, world.ws).how == "deny"
    assert migrated.check("write_file", {"path": str(protected(world)["她的 MCP 名单"])}, world.ws).how == "deny"
    assert [r["enabled"] for r in migrated.list() if r["value"].lower() == "runas"] == [False]


def test_default_rules_are_valid_for_the_native_host(world):
    """桌宠宿主 (desktop/host/src/autonomy.cpp 的 rule_valid) 读同一份共享名单: 有一条不合格, 整份都拒绝执行.
    照它的条件把现在的默认规则核一遍 (不重编宿主, 靠这条测试守住格式)."""
    rules = world.store.snapshot()["rules"]
    assert 0 < len(rules) < MAX_RULES
    # 版本 3-6 新加的规则也在被核的里面: 4 份扩展 / MCP 的文件、3 份服务端状态、心跳目录、插件目录 (写保护), 8 条提权命令
    paths = {Path(r["value"]).name: r for r in rules if r["kind"] == "path" and r["operations"] == "write"}
    assert {"registry.json", "direct-tools.json", "audit.jsonl", "asks.json", "workbench-threads.json", "qq-digest.json"} <= set(paths)
    assert {"runas", "runas.exe", "runasuser", "-verb", "sudo", "sudo.exe", "gsudo", "gsudo.exe"} <= {
        r["value"] for r in rules if r["kind"] == "command"}
    for rule in rules:
        limit = 2000 if rule["kind"] == "instruction" else 1024
        assert rule["kind"] in {"tool", "path", "command", "domain", "instruction"}, rule
        assert re.fullmatch(r"[a-f0-9]{32}", rule["id"]) and 0 < len(rule["value"]) <= limit and len(rule["reason"]) <= 1000
        assert isinstance(rule["enabled"], bool) and rule["source"] in {"user", "assistant"}
        assert rule["operations"] in {"all", "read", "write"} and (rule["kind"] == "path" or rule["operations"] == "all")
        assert rule["enforcement"] == ("instruction" if rule["kind"] == "instruction" else "tool_arguments")
        if rule["kind"] == "path":
            assert Path(rule["value"]).is_absolute(), rule
