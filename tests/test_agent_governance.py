"""Permissions and governance use temporary files; no tested command is executed."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from wm.agent import policy as P
from wm.agent.governance import BlacklistStore


@pytest.fixture
def store(tmp_path):
    return BlacklistStore(tmp_path / "agent", defaults=False)


@pytest.mark.parametrize("command", ["pip install requests", "Remove-Item .\\old -Recurse", "git push origin main",
                                      "format D:", "shutdown /s /t 0", "Invoke-Expression $code"])
def test_unrestricted_is_really_auto_when_no_blacklist(command):
    assert P.check_command(command, "unrestricted").how == "auto"


def test_unrestricted_path_has_no_old_hidden_protections(tmp_path):
    for path in [Path.home() / ".ssh" / "id_rsa", Path("C:/Windows") / "example.txt", tmp_path.parent / "outside.txt"]:
        assert P.check_path("write_file", path, "unrestricted", tmp_path).how == "auto"
        assert P.check_path("read_file", path, "unrestricted", tmp_path).how == "auto"
    assert P.check_command("", "unrestricted").how == "deny"
    assert P.check_command("a" * 2001, "unrestricted").how == "deny"
    assert P.check_command("echo hi", "bogus").how == "deny"
    assert P.check_path("write_file", tmp_path / "x", "bogus", tmp_path).how == "deny"


def test_existing_modes_remain_compatible(tmp_path):
    assert P.check_command("format D:", "full").how == "deny"
    assert P.check_command("pip install requests", "full").how == "ask"
    assert P.check_command("python build.py", "full").how == "auto"
    assert P.check_command("python build.py", "workspace").how == "ask"
    assert P.check_command("python build.py", "read").how == "deny"
    assert P.check_path("write_file", tmp_path / "x", "read", tmp_path).how == "deny"


def test_default_rules_are_visible_removable_and_not_reseeded(tmp_path):
    home = tmp_path / "agent"
    store = BlacklistStore(home, vault=str(tmp_path / "vault"))
    rules = store.list()
    assert rules and all(r["origin"] == "default" for r in rules)
    assert any(r["value"] == str((home / "blacklist.json").resolve()) for r in rules)
    assert not any(r["value"] == str(home.resolve()) for r in rules)
    assert store.check("read_file", {"path": str(home / "context" / "a.txt")}, tmp_path) is None
    assert store.check("read_file", {"path": str(home / "extensions" / "demo" / "SKILL.md")}, tmp_path) is None
    blocked = next(r for r in rules if r["kind"] == "command" and r["value"] == "shutdown")
    assert store.check("run_command", {"command": "shutdown /s"}, tmp_path).how == "deny"
    store.remove(blocked["id"])
    reloaded = BlacklistStore(home, vault=str(tmp_path / "vault"))
    assert reloaded.check("run_command", {"command": "shutdown /s"}, tmp_path) is None
    assert len(reloaded.list()) == len(rules) - 1
    assert reloaded.audit()[0]["action"] == "remove"


def test_default_format_does_not_block_format_table(tmp_path):
    store = BlacklistStore(tmp_path / "agent")
    assert store.check("run_command", {"command": "Get-Process | Format-Table Name"}, tmp_path) is None
    assert store.check("run_command", {"command": "format D:"}, tmp_path).how == "deny"


def test_assistant_can_only_add_constraints_with_provenance(store, tmp_path):
    added = store.add({"kind": "instruction", "value": "不要覆盖我的原始照片", "reason": "用户偏好"},
                      by="assistant", evidence="你以后别覆盖原始照片")
    assert added["source"] == "assistant" and added["enabled"]
    assert added["evidence"] == "你以后别覆盖原始照片"
    assert added["created"] == added["updated"]
    assert added["enforcement"] == "instruction"
    assert store.check("write_file", {"path": "original.jpg"}, tmp_path) is None
    assert added["value"] in store.instruction_text()
    assert added["evidence"] not in store.instruction_text()
    for changes in [{"enabled": False}, {"value": "别的规则"}, {"reason": "重新解释"}]:
        with pytest.raises(ValueError, match="只有用户"):
            store.update(added["id"], changes, by="assistant")
    with pytest.raises(ValueError, match="只有用户"):
        store.remove(added["id"], by="assistant")
    with pytest.raises(ValueError, match="启用"):
        store.add({"value": "不要发消息", "enabled": False}, by="assistant")
    assert len(store.list()) == 1


def test_user_can_edit_disable_and_remove_with_audit(store, tmp_path):
    added = store.add({"kind": "tool", "value": "mcp.*.send_*", "reason": "先别替我发消息"}, by="assistant")
    assert store.check("mcp.mail.send_message", {}, tmp_path).how == "deny"
    updated = store.update(added["id"], {"enabled": False})
    assert updated["source"] == "assistant"  # Original author is immutable; edit author is in audit.
    assert updated["updated"] >= added["created"]
    assert store.check("mcp.mail.send_message", {}, tmp_path) is None
    assert store.remove(added["id"])
    assert not store.remove(added["id"])
    history = store.audit()
    assert [r["action"] for r in history] == ["remove", "update", "add"]
    assert history[0]["by"] == "user" and history[-1]["by"] == "assistant"
    assert history[1]["before"]["enabled"] is True and history[1]["after"]["enabled"] is False


def test_add_is_idempotent_without_overwriting_provenance(store):
    first = store.add({"kind": "tool", "value": "mcp.*", "reason": "用户决定"}, evidence="原始说明")
    second = store.add({"kind": "tool", "value": "mcp.*", "reason": "模型解释"}, by="assistant", evidence="模型说明")
    assert second == first
    assert len(store.audit()) == 1
    store.update(first["id"], {"enabled": False})
    with pytest.raises(ValueError, match="已被停用"):
        store.add({"kind": "tool", "value": "mcp.*"}, by="assistant")
    assert len(store.list()) == 1


@pytest.mark.parametrize("tool,blocked", [("mcp.gmail.send_mail", True), ("mcp.Gmail.send_message", True),
                                           ("mcp.gmail.read_mail", False), ("write_file", False)])
def test_tool_name_globs_cover_real_mcp_tools(store, tmp_path, tool, blocked):
    store.add({"kind": "tool", "value": "mcp.*.send_*"})
    assert bool(store.check(tool, {}, tmp_path)) == blocked


def test_path_descendants_resolve_dotdot_and_do_not_match_prefix_siblings(store, tmp_path):
    protected = tmp_path / "private"
    store.add({"kind": "path", "value": str(protected)})
    for raw in [str(protected / "x.txt"), "private", "other/../private/x.txt"]:
        assert store.check("read_file", {"path": raw}, tmp_path).how == "deny"
    assert store.check("read_file", {"path": "private-copy/x.txt"}, tmp_path) is None
    assert store.check("mcp.files.read", {"input": {"paths": ["public/a", "private/a"]}}, tmp_path).how == "deny"
    assert store.check("grep", {"pattern": "password"}, tmp_path).how == "deny"
    assert store.check("list_dir", {"path": str(tmp_path)}, tmp_path) is None


def test_path_symlinks_resolve_to_protected_target(store, tmp_path):
    target = tmp_path / "private"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows account cannot create test symlinks")
    store.add({"kind": "path", "value": str(target)})
    assert store.check("read_file", {"path": str(linked / "x")}, tmp_path).how == "deny"


def test_write_only_path_rule_allows_read_tools(store, tmp_path):
    store.add({"kind": "path", "value": str(tmp_path / "original"), "operations": "write"})
    assert store.check("read_file", {"path": "original/a"}, tmp_path) is None
    assert store.check("write_file", {"path": "original/a"}, tmp_path).how == "deny"


def test_path_rule_checks_command_literals_and_environment_expansion(store, tmp_path, monkeypatch):
    private = tmp_path / "private"
    monkeypatch.setenv("WM_GOV_TEST_FOLDER", str(private))
    store.add({"kind": "path", "value": str(private)})
    for command in [f'Get-Content "{private}/x.txt"', "Get-Content $env:WM_GOV_TEST_FOLDER\\x.txt", "Get-Content %WM_GOV_TEST_FOLDER%\\x.txt"]:
        assert store.check("run_command", {"command": command}, tmp_path).how == "deny"
    assert store.check("run_command", {"command": f'Get-Content "{private}-copy/x.txt"'}, tmp_path) is None


@pytest.mark.parametrize("command,blocked", [("git push origin main", True), ("GIT   PUSH origin main", True),
                                            ("git pull", False), ("Write-Output 'git push'", True),
                                            ("mygit push", False), ("git push-extra", False)])
def test_command_rules_are_literal_phrases_not_regex(store, tmp_path, command, blocked):
    store.add({"kind": "command", "value": "git push"})
    assert bool(store.check("run_command", {"command": command}, tmp_path)) == blocked


def test_regex_metacharacters_do_not_execute_as_regex(store, tmp_path):
    store.add({"kind": "command", "value": "(a+)+$"})
    assert store.check("run_command", {"command": "a" * 200_000}, tmp_path) is None
    assert store.check("run_command", {"command": "echo '(a+)+$'"}, tmp_path).how == "deny"


@pytest.mark.parametrize("url,blocked", [("https://example.com/a", True), ("https://sub.example.com/a", True),
                                        ("https://EXAMPLE.COM./a", True), ("https://evil-example.com/a", False),
                                        ("https://example.com.evil.test/a", False), ("https://example.com@evil.test/a", False)])
def test_domain_rules_check_host_and_subdomain_boundaries(store, tmp_path, url, blocked):
    rule = store.add({"kind": "domain", "value": "https://Example.com/path"})
    assert rule["value"] == "example.com"
    assert bool(store.check("mcp.browser.open", {"request": {"url": url}}, tmp_path)) == blocked
    assert bool(store.check("run_command", {"command": f"curl '{url}'"}, tmp_path)) == blocked


def test_unicode_domain_and_wildcard_prefix_normalize(store, tmp_path):
    rule = store.add({"kind": "domain", "value": "*.例子.中国"})
    assert rule["value"].startswith("xn--")
    assert store.check("web_fetch", {"url": "https://例子.中国/a"}, tmp_path).how == "deny"


@pytest.mark.parametrize("rule", [{"kind": "regex", "value": ".*"}, {"kind": "tool", "value": "(a+)+$"},
                                   {"kind": "path", "value": "relative/path"}, {"kind": "domain", "value": "x/abc"},
                                   {"kind": "instruction", "value": ""}, {"kind": "instruction", "value": "a" * 2001},
                                   {"value": "x", "enabled": "false"}, {"value": "x", "operations": "write"},
                                   {"kind": [], "value": "x"}, {"value": "x", "operations": []},
                                   {"value": "x", "source": "user"}, {"value": "x", "id": "spoof"}])
def test_invalid_rules_cannot_change_store(store, rule):
    with pytest.raises(ValueError):
        store.add(rule)
    assert store.list() == [] and store.audit() == []


def test_corrupt_file_never_becomes_empty_blacklist(store, tmp_path):
    store.path.write_text('{"version": 1, "rules": "corrupt"}', encoding="utf-8")
    assert store.check("write_file", {"path": "a"}, tmp_path).how == "deny"
    with pytest.raises(ValueError, match="读取失败"):
        store.list()
    with pytest.raises(ValueError):
        store.add({"value": "不能覆盖损坏文件"})
    assert "corrupt" in store.path.read_text(encoding="utf-8")


def test_multiple_store_instances_do_not_lose_threaded_updates(tmp_path):
    home = tmp_path / "agent"
    stores = [BlacklistStore(home, defaults=False), BlacklistStore(home, defaults=False)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: stores[n % 2].add({"value": f"约束 {n}"}, by="assistant"), range(32)))
    assert len(stores[0].list()) == 32 and len(stores[1].audit()) == 32
    assert not list(home.glob("*.tmp"))


def test_returned_rules_are_copies_and_saved_updates_survive_reload(store):
    item = store.add({"value": "保持原始文件"})
    item["enabled"] = False
    listed = store.list()
    listed[0]["value"] = "伪造"
    assert store.list()[0]["enabled"] and store.list()[0]["value"] == "保持原始文件"
    store.update(item["id"], {"reason": "用户说明"})
    reloaded = BlacklistStore(store.home, defaults=False)
    assert reloaded.list()[0]["reason"] == "用户说明"
    assert reloaded.audit()[0]["action"] == "update"


def test_failed_atomic_save_preserves_previous_rules(store, monkeypatch):
    from wm.agent import governance
    store.add({"value": "原有规则"})
    before = store.path.read_bytes()
    def failed_replace(*args):
        raise OSError("模拟磁盘写入失败")
    monkeypatch.setattr(governance.os, "replace", failed_replace)
    with pytest.raises(OSError):
        store.add({"value": "尚未保存的规则"})
    assert store.path.read_bytes() == before
    assert len(store.list()) == 1
    assert not list(store.home.glob("*.tmp"))


def test_defaults_do_not_block_host_installed_skills(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData"
    monkeypatch.setenv("APPDATA", str(appdata))
    store = BlacklistStore(tmp_path / "agent")
    assert store.check("read_file", {"path": str(appdata / "WorldModel" / "skills" / "example" / "SKILL.md")}, tmp_path) is None
    assert store.check("read_file", {"path": str(appdata / "WorldModel" / "pet-settings.json")}, tmp_path).how == "deny"


@pytest.mark.parametrize("missing", ["id", "source", "kind", "value", "enabled", "operations"])
def test_saved_rules_require_enforcement_fields_without_creation_defaults(store, tmp_path, missing):
    store.add({"kind": "tool", "value": "write_file"})
    state = json.loads(store.path.read_text("utf-8"))
    del state["rules"][0][missing]
    store.path.write_text(json.dumps(state), encoding="utf-8")
    before = store.path.read_bytes()
    assert store.check("write_file", {"path": "x"}, tmp_path).how == "deny"
    for operation in (store.list, store.snapshot, lambda: store.add({"value": "new"}),
                      lambda: store.set_global_unrestricted(True)):
        with pytest.raises(ValueError, match="读取失败"):
            operation()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("invalid", [None, [], "unknown"])
def test_saved_invalid_kind_is_not_downgraded_to_instruction(store, tmp_path, invalid):
    store.add({"kind": "tool", "value": "write_file"})
    state = json.loads(store.path.read_text("utf-8"))
    state["rules"][0]["kind"] = invalid
    store.path.write_text(json.dumps(state), encoding="utf-8")
    assert store.check("write_file", {}, tmp_path).how == "deny"
    with pytest.raises(ValueError, match="读取失败"):
        store.snapshot()


def test_argument_node_limit_cannot_hide_tail_or_depend_on_insertion_order(store, tmp_path):
    private = str(tmp_path / "private" / "secret.txt")
    store.add({"kind": "path", "value": str(tmp_path / "private")})
    within = {"metadata": [None] * 509, "path": private}  # Root + list + 509 scalars + path = 512.
    assert "命中黑名单" in store.check("mcp.files.read", within, tmp_path).why
    for args in ({"metadata": [None] * 510, "path": private}, {"path": private, "metadata": [None] * 510}):
        denied = store.check("mcp.files.read", args, tmp_path)
        assert denied.how == "deny" and "检查限额" in denied.why
    # Oversized safe-looking args must also fail: no uninspected tail may proceed.
    assert "检查限额" in store.check("mcp.files.read", {"metadata": [None] * 512}, tmp_path).why


def test_argument_depth_limit_never_skips_nested_path(store, tmp_path):
    store.add({"kind": "path", "value": str(tmp_path / "private")})
    args = {"path": str(tmp_path / "private" / "x")}
    for _ in range(7):
        args = {"input": args}
    assert "命中黑名单" in store.check("mcp.files.read", args, tmp_path).why
    denied = store.check("mcp.files.read", {"input": args}, tmp_path)
    assert denied.how == "deny" and "检查限额" in denied.why
    circular = {}
    circular["self"] = circular
    assert store.check("mcp.files.read", circular, tmp_path).how == "deny"


def test_argument_text_limit_never_truncates_a_tail_domain(store, tmp_path):
    store.add({"kind": "domain", "value": "blocked.example"})
    url = "https://blocked.example/x"
    within = "x" * (200000 - len(url) - 1) + " " + url
    assert "命中黑名单" in store.check("mcp.browser.open", {"text": within}, tmp_path).why
    denied = store.check("mcp.browser.open", {"text": "x" * 200001 + " " + url}, tmp_path)
    assert denied.how == "deny" and "检查限额" in denied.why
    assert "检查限额" in store.check("mcp.browser.open", {"first": "x" * 100001, "second": "x" * 100001}, tmp_path).why


@pytest.mark.parametrize("args", [{"payload": {"opaque"}}, {"number": float("inf")}, {1: "not a JSON object key"}])
def test_uninspectable_arguments_fail_closed(store, tmp_path, args):
    assert store.check("mcp.unknown", args, tmp_path).how == "deny"


def test_global_snapshot_revision_and_idempotent_mutations(store, tmp_path):
    initial = store.snapshot()
    assert set(initial) == {"version", "revision", "global_unrestricted", "rules", "audit"}
    assert initial["revision"] == 1 and initial["global_unrestricted"] is False
    item = store.add({"kind": "tool", "value": "write_file"})
    assert store.snapshot()["revision"] == 2
    store.add({"kind": "tool", "value": "write_file"})
    assert store.snapshot()["revision"] == 2
    on = store.set_global_unrestricted(True)
    assert on["revision"] == 3 and on["global_unrestricted"] is True
    assert on["rules"][0]["id"] == item["id"]
    assert on["audit"][-1]["action"] == "set_global_unrestricted"
    assert on["audit"][-1]["before"] is False and on["audit"][-1]["after"] is True
    assert store.check("write_file", {}, tmp_path).how == "deny", "Global authorization never erases the blacklist"
    assert store.set_global_unrestricted(True)["revision"] == 3
    assert store.set_global_unrestricted(False)["revision"] == 4
    store.update(item["id"], {"enabled": False})
    assert store.snapshot()["revision"] == 5
    store.remove(item["id"])
    assert store.snapshot()["revision"] == 6
    assert store.remove(item["id"]) is False and store.snapshot()["revision"] == 6
    on["rules"][0]["value"] = "tampered copy"
    assert store.snapshot()["rules"] == []


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_global_switch_requires_real_boolean(store, value):
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="布尔值"):
        store.set_global_unrestricted(value)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("enabled", [False, True])
def test_assistant_cannot_change_global_authorization(store, enabled):
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="只有用户"):
        store.set_global_unrestricted(enabled, by="assistant")
    assert store.path.read_bytes() == before


def test_old_file_missing_new_fields_is_read_only_compatible_and_never_reseeded(tmp_path):
    store = BlacklistStore(tmp_path / "legacy", defaults=False)
    item = store.add({"value": "old API creation default"})
    store.remove(item["id"])
    old = json.loads(store.path.read_text("utf-8"))
    old.pop("global_unrestricted")
    old.pop("revision")
    store.path.write_text(json.dumps(old), encoding="utf-8")
    before = store.path.read_bytes()
    reloaded = BlacklistStore(store.home, defaults=True)
    state = reloaded.snapshot()
    assert state["global_unrestricted"] is False and state["revision"] == 0 and state["rules"] == []
    assert reloaded.path.read_bytes() == before
    assert reloaded.set_global_unrestricted(False)["revision"] == 0
    assert reloaded.path.read_bytes() == before
    assert reloaded.set_global_unrestricted(True)["revision"] == 1
    assert reloaded.snapshot()["rules"] == []


@pytest.mark.parametrize("field,value", [("revision", None), ("revision", True), ("revision", -1), ("revision", "1"),
                                        ("global_unrestricted", None), ("global_unrestricted", 0), ("global_unrestricted", "false")])
def test_explicit_invalid_new_persisted_fields_are_not_legacy_defaults(store, tmp_path, field, value):
    state = json.loads(store.path.read_text("utf-8"))
    state[field] = value
    store.path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="读取失败"):
        store.snapshot()
    assert store.check("read_file", {}, tmp_path).how == "deny"


def test_failed_global_atomic_save_preserves_flag_revision_rules_and_audit(store, monkeypatch):
    from wm.agent import governance
    store.add({"kind": "tool", "value": "write_file"})
    before = store.path.read_bytes()
    def failed_replace(*args):
        raise OSError("模拟磁盘写入失败")
    monkeypatch.setattr(governance.os, "replace", failed_replace)
    with pytest.raises(OSError):
        store.set_global_unrestricted(True)
    assert store.path.read_bytes() == before
    assert store.snapshot()["global_unrestricted"] is False
    assert not list(store.home.glob("*.tmp"))


def test_snapshot_is_coherent_and_multiple_instances_preserve_toggle_with_adds(tmp_path):
    stores = [BlacklistStore(tmp_path / "shared", defaults=False), BlacklistStore(tmp_path / "shared", defaults=False)]
    def change(n):
        current = stores[n % 2]
        if n == 10:
            current.set_global_unrestricted(True)
        else:
            current.add({"value": f"concurrent {n}"}, by="assistant")
        return current.snapshot()
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(change, range(20)))
    final = stores[0].snapshot()
    assert final["revision"] == 21 and final["global_unrestricted"] is True
    assert len(final["rules"]) == 19 and len(final["audit"]) == 20
    for state in snapshots:
        assert state["revision"] == 1 + len(state["audit"])
        assert len(state["rules"]) == len(state["audit"]) - int(state["global_unrestricted"])
    final["audit"].clear()
    final["global_unrestricted"] = False
    assert stores[1].snapshot()["global_unrestricted"] is True and len(stores[1].snapshot()["audit"]) == 20


def test_default_growth_evidence_remains_write_protected(tmp_path):
    store = BlacklistStore(tmp_path / "agent")
    for name in ("suites", "reports", "adoptions"):
        path = store.home / "growth" / name / "example.json"
        assert store.check("write_file", {"path": str(path)}, tmp_path).how == "deny"
        assert store.check("read_file", {"path": str(path)}, tmp_path) is None
    assert store.check("write_file", {"path": str(store.home / "growth" / "runs" / "case" / "output.txt")}, tmp_path) is None
