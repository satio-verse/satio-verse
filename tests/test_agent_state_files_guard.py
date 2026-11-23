"""插件宿主第 0 步第三小步 (A): 服务端自己写的三份状态, 她的写文件工具和命令不让碰, 服务端自己照常写.

~/.worldmodel 下:
- asks.json: 等你点头的申请 (合并改动、装 MCP、学新本事…), 重启后服务端照它把卡片放回来 (tools/serve.py 把 WM_ASKS_FILE 设成
  看守目录上一级的这个文件);
- workbench-threads.json: QQ / 微信上派到工作台的事 (每件一条线) 和回报的消息编号, 你引用回报说话会按它直通到那个会话;
- qq-digest.json: QQ 上攒着还没跟你说的重要消息.
后两份放在 asks.json 旁边 (wm/channels/manager.py). 她能直接写它们, 就能伪造申请卡片、派活的线、QQ 消息.
现在这三份是默认保护项 (版本 4, 只拦写, 读照旧). 黑名单只管她的工具调用, 服务端自己读写不经过它: 下面三条各走一遍真的写入
(申请落盘和重启后放回、工作台线程、QQ 汇总), 断言写得出来、读得回, 同一条里再断言同一路径她的写文件工具被拒.
迁移: 版本 3 的名单升到 6 只加 5 条 (v4 三份状态 + v5 心跳目录 + v6 插件目录), 版本 2 的一次升到 6 加 17 条,
用户删过、停用过、清空过、已有规则盖住的不加.
全在临时目录, 家目录换成临时的 (默认规则只认默认位置), 真的 ~/.worldmodel 一个字不碰."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_channels import run_qq
from test_qq_digest import CONF, msg
from wm.agent import policy as P
from wm.agent.governance import DEFAULT_RULES_VERSION, BlacklistStore, _validate
from wm.app import ENGINES
from wm.channels import qqdigest as QD
from wm.channels import wbthreads as WT

MODES = ("workspace", "full", "unrestricted")
NAMES = ["asks.json", "workbench-threads.json", "qq-digest.json"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(P, "_home", lambda: home)
    monkeypatch.setenv("USERPROFILE", str(home))  # $env:USERPROFILE 和 %USERPROFILE% 的写法也落在这个家目录
    ws = tmp_path / "project"
    ws.mkdir()
    state = home / ".worldmodel"
    return SimpleNamespace(store=BlacklistStore(tmp_path / "agent"), home=home, state=state, ws=ws, tmp=tmp_path)


def decide(w, tool, path, mode, workspace=None):
    """AgentHub._tool 里的先后: 先黑名单 (所有档), 再按档位的固定守门."""
    workspace = workspace or w.ws
    return w.store.check(tool, {"path": str(path)}, workspace) or P.check_path(tool, Path(path), mode, workspace)


# ---------------------------------------------------------------- 她写: 拒
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
@pytest.mark.parametrize("name", NAMES)
def test_write_tools_cannot_touch_them_in_any_mode(world, mode, tool, name):
    d = decide(world, tool, world.state / name, mode)
    assert d.how == "deny" and "命中黑名单" in d.why


@pytest.mark.parametrize("mode", MODES)
def test_reading_them_stays_allowed(world, mode):
    for name in NAMES:
        assert decide(world, "read_file", world.state / name, mode).how == "auto", name
    assert decide(world, "list_dir", world.state, mode).how == "auto"


def test_a_workspace_that_contains_them_does_not_open_them(world):
    """工作区选在家目录: 工作区内修改档的固定守门以为在工作区里随便改, 黑名单照样拦."""
    path = world.state / "asks.json"
    assert P.check_path("write_file", path, "workspace", world.home).how == "auto"
    assert decide(world, "write_file", path, "workspace", world.home).how == "deny"


@pytest.mark.parametrize("template", [
    "Set-Content '{p}' x", 'Add-Content "{p}" x', "echo x > {p}", "Remove-Item {p}", "python -c \"open(r'{p}','w')\"",
    "Copy-Item .\\evil.json {fwd}", "Move-Item {p} {p}.old", "Set-Content ~/.worldmodel/{n} x",
    "Set-Content ~\\.worldmodel\\{n} x", "Set-Content $env:USERPROFILE\\.worldmodel\\{n} x", "echo x > %USERPROFILE%\\.worldmodel\\{n}"])
def test_commands_that_name_them_are_denied(world, template):
    for name in NAMES:
        path = world.state / name
        command = template.format(p=path, fwd=str(path).replace("\\", "/"), n=name)
        d = world.store.check("run_command", {"command": command, "workdir": str(world.ws)}, world.ws)
        assert d is not None and d.how == "deny", (name, command)


def test_neighbours_and_same_named_files_elsewhere_are_not_swept_up(world):
    for path in (world.state / "asks.json.bak", world.state / "other.json", world.state / "qq-digest.old.json",
                 world.ws / "asks.json", world.ws / "workbench-threads.json", world.home / "asks.json"):
        assert world.store.check("write_file", {"path": str(path)}, world.ws) is None, path
    for command in (f"type {world.state / 'asks.json.bak'}", f"Set-Content {world.ws / 'qq-digest.json'} x",
                    "Get-Content ~/.worldmodel/other.json"):
        assert world.store.check("run_command", {"command": command}, world.ws) is None, command


def test_the_new_rules_read_as_the_user_will_see_them(world):
    rules = {Path(r["value"]).name: r for r in world.store.list() if Path(r["value"]).name in NAMES}
    assert set(rules) == set(NAMES)
    for name, rule in rules.items():
        assert rule["kind"] == "path" and rule["operations"] == "write" and rule["origin"] == "default" and rule["enabled"]
        assert Path(rule["value"]) == world.state / name and rule["reason"].strip()
        assert "不让任务直接改" in rule["reason"]


# ---------------------------------------------------------------- 服务端照常写
def test_the_server_still_writes_and_restores_pending_asks(world, monkeypatch):
    """申请落盘和重启后放回 (wm/channels/manager.py 的 _save_asks / restore_asks), 走真的渠道管理器."""
    asks = world.state / "asks.json"
    monkeypatch.setenv("WM_ASKS_FILE", str(asks))

    async def ask(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        ch = bot.app[ENGINES].channels
        assert ch.asks_file == asks
        did = await ch._ask_card("bili_comment", ("bili_comment", "BV1"), "她想在 B 站留言", "在《猫》下面评论: 好可爱", "发", "不发",
                                 qq="想在《猫》下面留言: 好可爱", what="在 B 站《猫》下面留言", remote=False,
                                 bili={"bvid": "BV1", "text": "好可爱"})
        return did, json.loads(asks.read_text(encoding="utf-8"))

    # mail.run=off: 有 WM_ASKS_FILE 又开着记忆库时, 测试起的服务端会去读真的邮箱目录 (~/.worldmodel/mail), 这里关掉
    did, saved = run_qq(world.tmp / "a", ask, "mail.run=off", vault=True)
    assert [(row["id"], row["kind"], row["bili"]["bvid"]) for row in saved] == [(did, "bili_comment", "BV1")]
    for tool in ("write_file", "edit_file"):  # 同一路径, 她的写文件工具被拒 (三档)
        for mode in MODES:
            assert decide(world, tool, asks, mode).how == "deny"
    assert world.store.check("run_command", {"command": f"Set-Content '{asks}' '[]'"}, world.ws).how == "deny"

    async def restart(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        ch = bot.app[ENGINES].channels
        return {d: v["kind"] for d, v in ch.drafts.items()}

    assert run_qq(world.tmp / "b", restart, "mail.run=off", vault=True) == {did: "bili_comment"}  # 重启以后同一个编号放回来


def test_the_server_still_writes_workbench_threads(world):
    """派到工作台的事 (wm/channels/wbthreads.py 的 Threads.open -> _save, 重启时 attach -> _load)."""
    path = world.state / "workbench-threads.json"
    threads = WT.Threads(path)
    threads.open("s1", "比较三家的价格", "QQ", ("private", 42))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [(row["sid"], row["goal"], row["conv"]) for row in saved["threads"]] == [("s1", "比较三家的价格", ["private", 42])]
    again = WT.Threads(path)
    again.attach(None, None)  # 重启以后
    assert again.items["s1"]["goal"] == "比较三家的价格" and again.items["s1"]["state"] == "running"
    for tool in ("write_file", "edit_file"):
        for mode in MODES:
            assert decide(world, tool, path, mode).how == "deny"
    assert world.store.check("run_command", {"command": f"echo x > {path}"}, world.ws).how == "deny"


def test_the_server_still_writes_the_qq_digest(world):
    """QQ 上攒着的重要消息 (wm/channels/qqdigest.py 的 Digest.see -> _save, 重启时 Digest(...) -> _load)."""
    path = world.state / "qq-digest.json"
    digest = QD.Digest(path)
    now = 1_793_000_000.0
    assert digest.see(msg("明天放假", at_all=True), ("group", 111), "群「测试群」", CONF, 10001, now) == "important"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [item["text"] for item in saved["pending"]] == ["明天放假"]
    assert [item["text"] for item in QD.Digest(path).pending] == ["明天放假"]  # 重启以后接着攒
    for tool in ("write_file", "edit_file"):
        for mode in MODES:
            assert decide(world, tool, path, mode).how == "deny"
    assert world.store.check("run_command", {"command": f"Set-Content '{path}' '{{}}'"}, world.ws).how == "deny"


# ---------------------------------------------------------------- 迁移
def downgrade(store, version):
    """把默认名单倒回指定版本的样子: 去掉这个版本以后新增的默认规则, 版本号改成 version. 返回拿掉了几条."""
    gone = []
    if version < 6:
        gone += store._plugins_defaults()
    if version < 5:
        gone += store._heartbeat_defaults()
    if version < 4:
        gone += store._state_defaults()
    if version < 3:
        gone += store._extension_defaults() + store._elevation_defaults()
    keys = {(rule["kind"], _validate(rule)["value"]) for rule in gone}
    state = json.loads(store.path.read_text("utf-8"))
    kept = [rule for rule in state["rules"] if (rule["kind"], rule["value"]) not in keys]
    removed = len(state["rules"]) - len(kept)
    state["rules"], state["default_rules_version"] = kept, version
    store.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return removed


def state_paths(world):
    # v5 起心跳目录、v6 起第三方插件目录也算默认保护, 跟 asks.json 那三份一起数
    return {str(world.state / name) for name in NAMES} | {
        str(world.state / "heartbeat"), str(world.state / "plugins")}


def test_version_3_list_gets_only_the_three_new_rules_once(world):
    store = world.store
    total, revision = len(store.list()), store.snapshot()["revision"]
    assert DEFAULT_RULES_VERSION >= 6 and downgrade(store, 3) == 3 + 1 + 1  # 3 份 v4 状态 + 1 份 v5 心跳目录 + 1 份 v6 插件目录
    assert store.check("write_file", {"path": str(world.state / "asks.json")}, world.ws) is None  # 倒回去以后确实没挡
    migrated = BlacklistStore(store.home)
    assert len(migrated.list()) == total and json.loads(migrated.path.read_text("utf-8"))["default_rules_version"] == DEFAULT_RULES_VERSION
    audit = migrated.audit()[0]
    assert audit["action"] == "migrate_defaults" and (audit["from_version"], audit["to_version"]) == (3, DEFAULT_RULES_VERSION)
    assert {item["path"] for item in audit["added"]} == state_paths(world) and not audit["skipped"]
    assert migrated.snapshot()["revision"] == revision + 1
    for name in NAMES:
        assert migrated.check("write_file", {"path": str(world.state / name)}, world.ws).how == "deny"
    before = migrated.path.read_bytes()
    BlacklistStore(store.home)
    assert migrated.path.read_bytes() == before  # 只迁移一次


def test_version_2_list_goes_straight_to_the_current_version_with_17_rules(world):
    store = world.store
    total = len(store.list())
    assert downgrade(store, 2) == 12 + 3 + 1 + 1  # v3 12 条 + v4 3 份状态 + v5 心跳目录 + v6 插件目录
    migrated = BlacklistStore(store.home)
    audit = migrated.audit()[0]
    assert (audit["from_version"], audit["to_version"]) == (2, DEFAULT_RULES_VERSION)
    assert len(audit["added"]) == 17 and not audit["skipped"] and len(migrated.list()) == total
    assert state_paths(world) <= {item["path"] for item in audit["added"]}


@pytest.mark.parametrize("choice", ["removed", "disabled", "covered", "cleared"])
def test_the_users_earlier_choices_are_not_overridden(world, choice):
    store = world.store
    downgrade(store, 3)
    editor = BlacklistStore(store.home, defaults=False)  # 老文件里用户自己改过: 不触发迁移
    asks = str(world.state / "asks.json")
    if choice in ("removed", "disabled"):
        mine = editor.add({"kind": "path", "value": asks, "operations": "write", "reason": "我自己加的"})
        editor.remove(mine["id"]) if choice == "removed" else editor.update(mine["id"], {"enabled": False})
    elif choice == "covered":
        editor.add({"kind": "path", "value": str(world.state), "operations": "write", "reason": "我护着整个 .worldmodel"})
    else:
        for rule in editor.list():
            editor.remove(rule["id"])
    migrated = BlacklistStore(store.home)
    audit = migrated.audit()[0]
    added = {item["path"] for item in audit["added"]}
    skipped = {item["path"] for item in audit["skipped"]}
    if choice in ("removed", "disabled"):
        assert added == state_paths(world) - {asks} and skipped == {asks}  # 删过、停用过的那条不加回, 另两条照加
    else:
        assert not added and skipped == state_paths(world)  # 被他的规则盖住 / 他清空过整张表: 一条都不加
    assert (migrated.list() == []) == (choice == "cleared")
    if choice != "cleared":
        # 删过 / 停用过的那条, 写 asks.json 照样放行 (是他的决定); 别的两份已经护上
        assert (migrated.check("write_file", {"path": asks}, world.ws) is None) == (choice in ("removed", "disabled"))
        assert migrated.check("write_file", {"path": str(world.state / "qq-digest.json")}, world.ws).how == "deny"

# ---------------------------------------------------------------- 版本 6: 插件目录
def test_plugin_directory_is_write_protected_in_every_mode(world):
    """~/.worldmodel/plugins/ 整目录写保护: 登记表、装进来的插件代码、回执和审计都别让她的写工具碰."""
    for path in (world.state / "plugins", world.state / "plugins" / "registry.json",
                 world.state / "plugins" / "filesorter" / "plugin.py",
                 world.state / "plugins" / "bookkeeper" / "receipts.jsonl"):
        for tool in ("write_file", "edit_file"):
            for mode in MODES:
                d = decide(world, tool, path, mode)
                assert d.how == "deny" and "命中黑名单" in d.why, (path, tool, mode)


@pytest.mark.parametrize("mode", MODES)
def test_reading_the_plugin_directory_stays_allowed(world, mode):
    path = world.state / "plugins" / "registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    assert decide(world, "read_file", path, mode).how == "auto"
    assert decide(world, "list_dir", path.parent, mode).how == "auto"


def test_version_5_upgrade_adds_only_the_plugin_directory_once(world):
    store = world.store
    total, revision = len(store.list()), store.snapshot()["revision"]
    assert DEFAULT_RULES_VERSION >= 6 and downgrade(store, 5) == 1  # v6 只加了插件目录这一条
    target = world.state / "plugins" / "third_party" / "plugin.py"
    assert store.check("write_file", {"path": str(target)}, world.ws) is None  # 倒回 5 以后确实没挡
    migrated = BlacklistStore(store.home)
    audit = migrated.audit()[0]
    assert audit["action"] == "migrate_defaults" and (audit["from_version"], audit["to_version"]) == (5, DEFAULT_RULES_VERSION)
    assert [item["path"] for item in audit["added"]] == [str(world.state / "plugins")] and not audit["skipped"]
    assert len(migrated.list()) == total and migrated.snapshot()["revision"] == revision + 1
    assert migrated.check("write_file", {"path": str(target)}, world.ws).how == "deny"
    before = migrated.path.read_bytes()
    BlacklistStore(store.home)
    assert migrated.path.read_bytes() == before  # 迁移只做一次, 升过的不再重复加
