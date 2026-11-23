"""世界接口契约接进安装流程的测试。

这里覆盖三件事：契约判定本身（install_gate）、Creator 扩展把世界插件存下来、
社区市场装一个世界包时先看报告再装、并且真的交给插件宿主。
全部用 tmp_path，不碰真实插件目录、不起子进程。
"""
import asyncio
import json
from pathlib import Path

import pytest

from wm.agent import install_gate
from wm.agent.extensions import ExtensionRegistry
from wm.platform_services import CommunityStore, canonical, digest

MANIFESTS = Path(__file__).parent / "plugin_manifests"


def plaza_manifest():
    """真实的 wmworld.json：id/name/description + plugin 块，整份就是契约输入。"""
    return json.loads((MANIFESTS / "plaza-2d.json").read_text("utf-8"))


def world_spec(**over):
    spec = {"id": "plaza-2d", "name": "广场", "description": "一个 2D 世界",
            "plugin": plaza_manifest(),
            "files": {"spectate.html": "<!doctype html><title>广场</title>",
                      "ENV_PROMPT.md": "你在广场上，能看见别人的桌宠。",
                      "fixtures/twin-far-pet.json": "{}"}}
    spec.update(over)
    return spec


def skill_spec(**over):
    spec = {"id": "sample", "name": "Sample", "description": "A skill", "files": {"SKILL.md": "# Hi"}}
    spec.update(over)
    return spec


def registry(tmp_path, name="registry"):
    return ExtensionRegistry(tmp_path / name, settings_path=False)


# ── 契约判定 ───────────────────────────────────────────────────────────────

def test_classify_separates_world_plugin_from_old_extension():
    assert install_gate.classify(world_spec()) == "plugin"
    assert install_gate.classify(skill_spec()) == "extension"
    assert install_gate.classify({"plugin": "不是字典"}) == "extension"


def test_inspect_reports_what_the_world_asks_for():
    report = install_gate.inspect(world_spec())
    assert report["ok"] is True
    assert report["kind"] == "plugin"
    assert report["contract"]["name"] == install_gate.CONTRACT_NAME
    assert report["contract"]["schema"] == 2
    summary = report["summary"]
    assert summary["world"] == "space" and summary["prefix"] == "plaza"
    assert summary["tier"] == "process"
    assert len(summary["actions"]) == 4 and len(summary["events"]) == 5
    assert summary["senses"] and summary["permissions"]["local"] == ["127.0.0.1:8801"]


def test_inspect_flags_a_declared_file_that_is_not_in_the_package():
    spec = world_spec(files={"spectate.html": "<!doctype html><title>广场</title>",
                             "fixtures/twin-far-pet.json": "{}"})
    report = install_gate.inspect(spec)
    assert report["ok"] is False
    assert any(issue["code"] == "E_FILE_MISSING" for issue in report["issues"])
    assert "ENV_PROMPT.md" in report["errors_text"]


def test_inspect_flags_a_manifest_that_breaks_the_contract():
    manifest = plaza_manifest()
    manifest["plugin"]["tier"] = "godmode"
    manifest["plugin"]["world"]["events"] = []
    report = install_gate.inspect({"id": "plaza-2d", "name": "广场", "plugin": manifest})
    assert report["ok"] is False
    assert len(report["issues"]) >= 2
    codes = {issue["code"] for issue in report["issues"]}
    assert {"E_ENUM", "E_RECEIPT_UNKNOWN"} <= codes
    assert "tier" in report["errors_text"]


def test_inspect_notices_an_id_that_does_not_match_the_package():
    report = install_gate.inspect(world_spec(id="other-world"))
    assert report["ok"] is False
    assert any(issue["code"] == "E_ID_MISMATCH" for issue in report["issues"])


def test_gate_raises_with_the_contract_name_and_count():
    manifest = plaza_manifest()
    manifest["plugin"]["version"] = "not-a-version"
    with pytest.raises(install_gate.ContractError) as err:
        install_gate.gate({"id": "plaza-2d", "name": "广场", "plugin": manifest})
    text = str(err.value)
    assert install_gate.CONTRACT_NAME in text and "处" in text


def test_gate_lets_old_extensions_through_unchanged():
    report = install_gate.gate(skill_spec())
    assert report["ok"] is True and report["kind"] == "extension"
    assert report["warnings"] == [] and report["issues"] == []


def test_inspect_flags_an_extension_with_no_files():
    report = install_gate.inspect({"id": "sample", "name": "Sample", "files": {}})
    assert report["kind"] == "extension" and report["ok"] is False
    assert any(issue["code"] == "E_FILES" for issue in report["issues"])


# ── Creator 扩展把世界插件存下来 ────────────────────────────────────────────

def test_create_stores_the_world_manifest_and_uses_the_panel_as_ui(tmp_path):
    reg = registry(tmp_path)
    result = reg.create(world_spec())
    assert result["kind"] == "plugin"
    assert result["has_ui"] is True
    assert "wmworld.json" in result["files"]
    assert result["validation"]["kind"] == "plugin"
    assert result["validation"]["contract"]["schema"] == 2
    assert result["contract"]["world"] == "space"
    folder = tmp_path / "registry" / "extensions" / "plaza-2d" / "revisions" / result["revision"]
    assert json.loads((folder / "wmworld.json").read_text("utf-8"))["plugin"]["world"]["prefix"] == "plaza"
    assert (folder / "spectate.html").exists()
    enabled = reg.set_enabled("plaza-2d", True, authorized=True)
    assert enabled["enabled"] is True


def test_create_keeps_the_old_extension_path(tmp_path):
    result = registry(tmp_path).create(skill_spec())
    assert result["kind"] == "extension" and result["has_ui"] is False
    assert "wmworld.json" not in result["files"]


def test_create_still_wants_a_body_for_an_extension(tmp_path):
    with pytest.raises(ValueError, match="至少包含"):
        registry(tmp_path).create({"id": "sample", "name": "Sample", "files": {"notes.txt": "x"}})


def test_create_refuses_a_world_package_that_breaks_the_contract(tmp_path):
    manifest = plaza_manifest()
    del manifest["plugin"]["world"]["actions"]
    with pytest.raises(install_gate.ContractError):
        registry(tmp_path).create(world_spec(plugin=manifest))


# ── 市场：先看报告再装 ─────────────────────────────────────────────────────

def export_world(tmp_path):
    reg = registry(tmp_path)
    reg.create(world_spec())
    source = CommunityStore(tmp_path / "source", reg)
    return source.export("plaza-2d", "maker", "1.0.0")


def test_marketplace_inspect_then_install_a_world_package(tmp_path):
    result = export_world(tmp_path)
    assert result["package"]["spec"]["plugin"]["plugin"]["world"]["kind"] == "space"
    target = CommunityStore(tmp_path / "target", ExtensionRegistry(tmp_path / "other", settings_path=False))
    sid = target.add_source(str(Path(result["path"]).parent / "index.json"))[0]["id"]
    asyncio.run(target.refresh(sid))
    report = asyncio.run(target.inspect(sid, "plaza-2d", "1.0.0"))
    assert report["report"]["ok"] is True and report["report"]["kind"] == "plugin"
    installed = asyncio.run(target.install(sid, "plaza-2d", "1.0.0"))
    assert installed["kind"] == "plugin" and installed["enabled"] is False
    assert installed["installed"]["kind"] == "plugin"
    assert installed["plugin"]["installed"] is False          # 测试里插件宿主没开
    assert "插件宿主" in installed["notice"]


def test_marketplace_refuses_a_package_that_breaks_the_contract(tmp_path):
    result = export_world(tmp_path)
    package = json.loads(Path(result["path"]).read_text("utf-8"))
    del package["spec"]["plugin"]["plugin"]["world"]["events"]
    blob = canonical(package)
    broken = Path(result["path"]).parent / "broken.json"
    broken.write_bytes(blob)
    index = json.loads((Path(result["path"]).parent / "index.json").read_text("utf-8"))
    index["packages"][0].update({"sha256": digest(blob), "file": "broken.json"})
    (Path(result["path"]).parent / "index.json").write_text(json.dumps(index, ensure_ascii=False), "utf-8")
    target = CommunityStore(tmp_path / "target", ExtensionRegistry(tmp_path / "other", settings_path=False))
    sid = target.add_source(str(broken.parent / "index.json"))[0]["id"]
    asyncio.run(target.refresh(sid))
    assert asyncio.run(target.inspect(sid, "plaza-2d", "1.0.0"))["report"]["ok"] is False
    with pytest.raises(ValueError, match="不符合"):
        asyncio.run(target.install(sid, "plaza-2d", "1.0.0"))
    assert target.extensions._state()["packages"] == {}


class FakeHost:
    def __init__(self):
        self.seen = None

    def switch_on(self):
        return True

    def install(self, base):
        base = Path(base)
        self.seen = {p.name: p.read_bytes() for p in base.rglob("*") if p.is_file()}
        return {"ok": True, "id": "plaza-2d", "granted_tier": "text", "enabled": False}


def test_install_plugin_package_hands_a_real_folder_to_the_host():
    host = FakeHost()
    out = install_gate.install_plugin_package(world_spec(), plugin_host=host)
    assert out["ok"] is True and out["installed"] is True and out["tier"] == "text"
    assert set(host.seen) == {"wmworld.json", "spectate.html", "ENV_PROMPT.md", "twin-far-pet.json"}
    assert json.loads(host.seen["wmworld.json"].decode("utf-8"))["id"] == "plaza-2d"


def test_install_plugin_package_says_so_when_the_host_is_off():
    class Off(FakeHost):
        def switch_on(self):
            return False

    out = install_gate.install_plugin_package(world_spec(), plugin_host=Off())
    assert out["ok"] is False and out["installed"] is False
    assert "WM_PLUGIN_HOST" in out["why"]
