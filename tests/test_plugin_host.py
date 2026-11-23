"""插件宿主的装扩展流程 (平台化第一切片, 任务 A).

只测 `wm/agent/plugin_host.py` 的四个操作: 装、列、启停、卸。全程用 tmp_path 的插件目录,
不碰真 `~/.worldmodel/plugins`; 网络、执行插件代码都不涉及。
"""
import copy
import json
from pathlib import Path

import pytest

from wm.agent import plugin_host as ph
from wm.agent import plugin_manifest as pm

SAMPLES = Path(__file__).parent / "plugin_manifests"


def manifest(**plugin):
    """一份最小的、合法的非世界插件清单."""
    block = {"schema": 2, "version": "1.0.0", "author": "测试", "license": "MIT",
             "host": ">=0.4", "tier": "panel", "permissions": {}}
    block.update(plugin)
    return {"id": "demo-tool", "name": "演示工具", "description": "测试用", "plugin": block}


def space_manifest(plugin_id, prefix):
    """最小 world=space 插件: 用来测前缀冲突."""
    return {
        "id": plugin_id, "name": plugin_id, "description": "广场测试",
        "plugin": {
            "schema": 2, "version": "0.1.0", "author": "测试", "license": "MIT", "host": ">=0.4",
            "tier": "process", "permissions": {},
            "panel": {"entry": "spectate.html"},
            "world": {
                "kind": "space", "prefix": prefix,
                "events": [{"type": "moved", "sense": "saw"}],
                "actions": [],
                "perception": {"visible": ["pet_name"], "twin_fixtures": ["fixtures/twin.json"]},
                "spectate": {"panel": "spectate.html"},
            },
        },
    }


def package(tmp_path, manifest_obj=None, files=None):
    """在 tmp_path/pkg 里造一个插件包."""
    root = tmp_path / "pkg"
    root.mkdir(exist_ok=True)
    (root / ph.MANIFEST_NAME).write_text(json.dumps(manifest_obj or manifest(), ensure_ascii=False), encoding="utf-8")
    (root / "plugin.py").write_text("print('hi')\n", encoding="utf-8")
    for rel, content in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


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


def only_plugin(result):
    assert result["enabled"] is True and result["ok"] is True, result
    assert len(result["plugins"]) == 1, result
    return result["plugins"][0]


# ---- 装 ----

def test_install_copies_the_package_and_lists_it(host, tmp_path):
    pkg = package(tmp_path, files={"README.md": "说明\n"})
    result = ph.install(pkg)
    assert result["enabled"] is True and result["ok"] is True, result
    assert result["id"] == "demo-tool" and result["plugin_enabled"] is False
    installed = host / "demo-tool"
    assert (installed / "wmworld.json").is_file()
    assert (installed / "plugin.py").is_file()
    assert (installed / "README.md").is_file()
    assert (host / "registry.json").is_file()
    entry = only_plugin(ph.list())
    assert entry["id"] == "demo-tool"
    assert entry["name"] == "演示工具"
    assert entry["enabled"] is False  # 装完默认停着
    assert sorted(entry["files"]) == ["README.md", "plugin.py", "wmworld.json"]


def test_install_requires_the_manifest_in_the_package_root(host, tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    result = ph.install(root)
    assert result["ok"] is False
    assert "wmworld.json" in result["reason"]
    assert not (host / "plugin").exists()


def test_install_rejects_a_broken_manifest_with_a_readable_reason(host, tmp_path):
    bad = manifest(schema=1)
    result = ph.install(package(tmp_path, bad))
    assert result["enabled"] is True and result["ok"] is False
    assert "清单不合格" in result["reason"]
    assert "E_SCHEMA" in result["reason"] and "plugin.schema" in result["reason"]
    assert not (host / "demo-tool").exists()
    assert ph.list()["plugins"] == []


def test_install_rejects_a_duplicate_id(host, tmp_path):
    assert ph.install(package(tmp_path))["ok"] is True
    again = ph.install(package(tmp_path))
    assert again["ok"] is False
    assert "已经装了" in again["reason"] and "demo-tool" in again["reason"]


def test_install_rejects_a_third_party_builtin_claim(host, tmp_path):
    result = ph.install(package(tmp_path, manifest(tier="builtin")))
    assert result["ok"] is False
    assert "来路不允许" in result["reason"]
    codes = {i["code"] for i in result["issues"]}
    assert "E_BUILTIN_NOT_BUNDLED" in codes


def test_install_rejects_owner_tier_and_answer_cards_from_third_party(host, tmp_path):
    qq = json.loads((SAMPLES / "qq-onebot.json").read_text(encoding="utf-8"))
    qq = copy.deepcopy(qq)
    qq["id"] = "qq-copy"
    result = ph.install(package(tmp_path, qq))
    assert result["ok"] is False
    assert "来路不允许" in result["reason"]
    codes = {i["code"] for i in result["issues"]}
    assert {"E_BUILTIN_NOT_BUNDLED", "E_THIRD_PARTY_OWNER", "E_THIRD_PARTY_ANSWER_CARDS"} <= codes


def test_install_rejects_an_old_package_without_a_plugin_block(host, tmp_path):
    old = {"id": "old-tool", "name": "老包", "description": "没有 plugin 块", "mcp": None, "ui": None}
    result = ph.install(package(tmp_path, old))
    assert result["ok"] is False
    assert "老包" in result["reason"] and "plugin" in result["reason"]
    assert not (host / "old-tool").exists()


def test_the_granted_tier_is_always_text_not_what_the_manifest_claims(host, tmp_path):
    result = ph.install(package(tmp_path, manifest(tier="process")))
    assert result["ok"] is True, result
    assert result["declared_tier"] == "process"
    assert result["granted_tier"] == "text"
    entry = only_plugin(ph.list())
    assert entry["declared_tier"] == "process" and entry["granted_tier"] == "text"
    state = json.loads((host / "registry.json").read_text(encoding="utf-8"))
    assert state["plugins"]["demo-tool"]["granted_tier"] == "text"


def test_install_rejects_a_manifest_that_declares_world_session(host, tmp_path):
    bad = manifest()
    bad["plugin"]["world"] = {"kind": "space", "prefix": "dm", "events": [], "actions": [],
                              "session": {"capabilities": ["tools"]}}
    result = ph.install(package(tmp_path, bad))
    assert result["ok"] is False
    assert "world.session" in result["reason"]
    assert any(i["code"] == "E_UNKNOWN_KEY" and i["path"] == "plugin.world.session" for i in result["issues"])


def test_install_rejects_a_world_prefix_already_taken(host, tmp_path):
    first = package(tmp_path, space_manifest("plaza-one", "plaza"),
                    {"spectate.html": "<html></html>", "fixtures/twin.json": "{}"})
    assert ph.install(first)["ok"] is True
    second = tmp_path / "pkg2"
    second.mkdir()
    (second / ph.MANIFEST_NAME).write_text(json.dumps(space_manifest("plaza-two", "plaza")), encoding="utf-8")
    (second / "spectate.html").write_text("<html></html>", encoding="utf-8")
    (second / "fixtures").mkdir()
    (second / "fixtures" / "twin.json").write_text("{}", encoding="utf-8")
    result = ph.install(second)
    assert result["ok"] is False
    assert "和已装插件冲突" in result["reason"]
    assert any(i["code"] == "E_PREFIX_TAKEN" for i in result["issues"])


# ---- 关着开关 ----

def test_switch_off_returns_a_reason_and_touches_no_disk(off, tmp_path, monkeypatch):
    pkg = package(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("关着开关不该读登记表或算插件目录")

    monkeypatch.setattr(ph, "_load_registry", boom)
    monkeypatch.setattr(ph, "home", boom)
    for result in (ph.install(pkg), ph.list(), ph.set_enabled("demo-tool", True), ph.uninstall("demo-tool")):
        assert set(result) == {"enabled", "reason"}
        assert result["enabled"] is False
        assert "WM_PLUGIN_HOST=on" in result["reason"]
    assert not off.exists()  # 目录都没建


# ---- 启停 ----

def test_enable_and_disable_a_plugin(host, tmp_path):
    assert ph.install(package(tmp_path))["ok"] is True
    on = ph.set_enabled("demo-tool", True)
    assert on["ok"] is True and on["plugin_enabled"] is True and on["changed"] is True
    assert only_plugin(ph.list())["enabled"] is True
    off = ph.set_enabled("demo-tool", False)
    assert off["ok"] is True and off["plugin_enabled"] is False and off["changed"] is True
    assert only_plugin(ph.list())["enabled"] is False
    same = ph.set_enabled("demo-tool", False)
    assert same["ok"] is True and same["changed"] is False


def test_set_enabled_rejects_an_unknown_plugin(host):
    result = ph.set_enabled("not-installed", True)
    assert result["ok"] is False
    assert "没有装这个插件" in result["reason"]


def test_set_enabled_rejects_a_non_bool(host, tmp_path):
    assert ph.install(package(tmp_path))["ok"] is True
    result = ph.set_enabled("demo-tool", "yes")
    assert result["ok"] is False
    assert "true / false" in result["reason"]


# ---- 卸载 ----

def test_uninstall_removes_the_plugin_directory_and_the_registry_row(host, tmp_path):
    assert ph.install(package(tmp_path))["ok"] is True
    result = ph.uninstall("demo-tool")
    assert result["ok"] is True and result["removed"] is True
    assert result["files_removed"] >= 2
    assert not (host / "demo-tool").exists()
    assert ph.list()["plugins"] == []
    assert json.loads((host / "registry.json").read_text(encoding="utf-8"))["plugins"] == {}


def test_uninstall_of_an_unknown_plugin_says_so(host):
    result = ph.uninstall("not-installed")
    assert result["ok"] is False
    assert "没有装这个插件" in result["reason"]


# ---- 目录与环境变量 ----

def test_wm_plugin_home_env_moves_the_plugin_directory(host, tmp_path):
    assert ph.home() == host.resolve()
    assert ph.install(package(tmp_path))["ok"] is True
    assert (host / "demo-tool" / "wmworld.json").is_file()


def test_install_does_not_leave_a_half_plugin_behind(host, tmp_path):
    """包里有一个 1 MB 出头的文件: 拒装, 插件目录里不能留下 stage 或半个插件."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / ph.MANIFEST_NAME).write_text(json.dumps(manifest(), ensure_ascii=False), encoding="utf-8")
    (root / "big.bin").write_bytes(b"x" * (ph.MAX_FILE_BYTES + 1))
    result = ph.install(root)
    assert result["ok"] is False
    assert "超过单文件上限" in result["reason"]
    leftovers = [p.name for p in host.iterdir()] if host.exists() else []
    assert leftovers == [] or leftovers == ["registry.json"]


def test_the_sample_manifests_still_pass_the_contract_validator():
    """装扩展流程没有另立格式: 四份样例照旧过校验器和来路政策 (自带来路)."""
    for name in ("qq-onebot", "weixin-ilink", "local-guest", "plaza-2d"):
        m = json.loads((SAMPLES / (name + ".json")).read_text(encoding="utf-8"))
        assert pm.validate_plugin_manifest(m) == []
        assert pm.policy_issues(m, "bundled") == []
