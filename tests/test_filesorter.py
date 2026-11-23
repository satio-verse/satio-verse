"""文件整理插件 (plugins/filesorter, 平台化第一切片的任务 B).

只测插件自己的逻辑: 计划 (dry-run)、真移、BlacklistStore 路径保护、回执、撤销、开关。
文件都在 tmp_path 里造; 回执和 BlacklistStore 也指到临时目录, 不碰真 ~/.worldmodel。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from wm.agent import plugin_manifest as pm
from wm.agent.governance import BlacklistStore

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "filesorter"


def load_plugin():
    mod = sys.modules.get("filesorter_plugin")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("filesorter_plugin", PLUGIN / "plugin.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["filesorter_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


fs = load_plugin()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv(fs.SWITCH_ENV, "on")
    home = tmp_path / "fh"
    monkeypatch.setenv(fs.HOME_ENV, str(home))
    tree = tmp_path / "tree"
    tree.mkdir()
    return {"root": tree, "home": home, "blacklist": tmp_path / "bh"}


def make_files(root: Path, spec: dict) -> None:
    for name, data in spec.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def do_plan(rig):
    return fs.plan(rig["root"], home=rig["home"])


def do_confirm(rig):
    return fs.confirm_move(rig["root"], home=rig["home"], blacklist_home=rig["blacklist"])


def do_undo(rig, receipt_id):
    return fs.undo(receipt_id, home=rig["home"])


def protect(rig, path: Path) -> None:
    """在临时 BlacklistStore 里把某个路径加成受保护 (写也不许)."""
    store = BlacklistStore(rig["blacklist"])
    store.add({"kind": "path", "value": str(path), "operations": "all", "reason": "测试保护"}, by="user")


def receipt_of(rig, result) -> dict:
    lines = (rig["home"] / fs.RECEIPTS_NAME).read_text(encoding="utf-8").splitlines()
    items = [json.loads(line) for line in lines if line.strip()]
    return next(item for item in items if item["id"] == result["receipt"])


# ---- 计划 (dry-run) ----

def test_plan_groups_by_type_and_moves_nothing(rig):
    make_files(rig["root"], {"a.jpg": "1", "b.png": "2", "c.pdf": "3", "d.mp3": "4",
                             "e.mp4": "5", "f.zip": "6", "g.py": "7", "h.xyz": "8", "noext": "9"})
    result = do_plan(rig)
    assert result["enabled"] is True and result["ok"] is True
    assert result["dry_run"] is True and result["count"] == 9
    targets = {item["src"].split("\\")[-1]: item["target"] for item in result["moves"]}
    assert targets == {
        "a.jpg": "images/a.jpg", "b.png": "images/b.png", "c.pdf": "documents/c.pdf",
        "d.mp3": "audio/d.mp3", "e.mp4": "video/e.mp4", "f.zip": "archives/f.zip",
        "g.py": "code/g.py", "h.xyz": "other/h.xyz", "noext": "other/noext",
    }
    # 一个文件都没动, 一个子目录都没建, 一条回执都没写
    assert sorted(p.name for p in rig["root"].iterdir()) == sorted(
        ["a.jpg", "b.png", "c.pdf", "d.mp3", "e.mp4", "f.zip", "g.py", "h.xyz", "noext"])
    assert not (rig["root"] / "images").exists()
    assert not rig["home"].exists()


def test_unknown_type_goes_to_other(rig):
    make_files(rig["root"], {"mystery.zzz": "x", "README": "y"})
    result = do_plan(rig)
    assert {item["category"] for item in result["moves"]} == {"other"}
    assert sorted(item["target"] for item in result["moves"]) == ["other/README", "other/mystery.zzz"]


def test_plan_requires_an_explicit_directory(rig):
    for bad in (None, "", "   "):
        result = fs.plan(bad, home=rig["home"])
        assert result["ok"] is False and "必须" in result["reason"]
    drive_root = Path(rig["root"].anchor)
    result = fs.plan(drive_root, home=rig["home"])
    assert result["ok"] is False and "整个盘" in result["reason"]
    result = fs.plan(Path.home(), home=rig["home"])
    assert result["ok"] is False and "用户目录" in result["reason"]


def test_plan_does_not_walk_into_subdirectories(rig):
    make_files(rig["root"], {"a.jpg": "1", "sub/inner.png": "2"})
    result = do_plan(rig)
    assert result["count"] == 1
    assert result["moves"][0]["target"] == "images/a.jpg"


def test_run_defaults_to_dry_run_and_only_confirm_moves(rig):
    make_files(rig["root"], {"a.jpg": "1"})
    dry = fs.run(rig["root"], home=rig["home"])
    assert dry["dry_run"] is True and (rig["root"] / "a.jpg").is_file()
    moved = fs.run(rig["root"], confirm=True, home=rig["home"], blacklist_home=rig["blacklist"])
    assert moved["dry_run"] is False and (rig["root"] / "images" / "a.jpg").is_file()
    assert not (rig["root"] / "a.jpg").exists()


# ---- 真移与回执 ----

def test_confirm_moves_files_and_writes_a_receipt(rig):
    make_files(rig["root"], {"a.jpg": "jpg-body", "b.txt": "txt-body"})
    result = do_confirm(rig)
    assert result["enabled"] is True and result["ok"] is True
    assert result["files_moved"] == 2
    assert (rig["root"] / "images" / "a.jpg").read_bytes() == b"jpg-body"
    assert (rig["root"] / "documents" / "b.txt").read_bytes() == b"txt-body"
    assert not (rig["root"] / "a.jpg").exists()
    receipt = receipt_of(rig, result)
    assert receipt["id"] == result["receipt"]
    assert receipt["kind"] == "move" and receipt["files_moved"] == 2
    assert receipt["dir"] == str(rig["root"].resolve())
    assert isinstance(receipt["at"], str) and receipt["at"]
    assert receipt["elapsed_ms"] >= 0 and receipt["ts"] > 0
    by_name = {Path(item["src"]).name: item for item in receipt["moves"]}
    assert by_name["a.jpg"]["dst"] == str(rig["root"] / "images" / "a.jpg")
    assert by_name["a.jpg"]["target"] == "images/a.jpg" and by_name["a.jpg"]["category"] == "images"
    assert Path(result["receipt_path"]).name == fs.RECEIPTS_NAME


def test_confirm_does_not_overwrite_a_name_conflict(rig):
    make_files(rig["root"], {"a.jpg": "new-body", "images/a.jpg": "old-body"})
    planned = do_plan(rig)
    assert planned["moves"][0]["target"] == "images/a (1).jpg"
    result = do_confirm(rig)
    assert result["ok"] is True and result["files_moved"] == 1
    assert (rig["root"] / "images" / "a.jpg").read_bytes() == b"old-body"
    assert (rig["root"] / "images" / "a (1).jpg").read_bytes() == b"new-body"


def test_protected_path_is_skipped_and_reported(rig):
    make_files(rig["root"], {"secret.jpg": "keep-me", "open.txt": "move-me"})
    protect(rig, rig["root"] / "secret.jpg")
    result = do_confirm(rig)
    assert result["ok"] is True
    assert result["files_moved"] == 1
    assert (rig["root"] / "secret.jpg").read_bytes() == b"keep-me"
    assert (rig["root"] / "documents" / "open.txt").read_bytes() == b"move-me"
    skipped = {Path(item["path"]).name: item["why"] for item in result["skipped"]}
    assert "secret.jpg" in skipped
    assert "受保护" in skipped["secret.jpg"]
    receipt = receipt_of(rig, result)
    assert any(Path(item["path"]).name == "secret.jpg" for item in receipt["skipped"])


def test_empty_directory_is_not_an_error(rig):
    planned = do_plan(rig)
    assert planned["ok"] is True and planned["count"] == 0 and planned["moves"] == []
    result = do_confirm(rig)
    assert result["ok"] is True and result["files_moved"] == 0
    assert receipt_of(rig, result)["moves"] == []
    undone = do_undo(rig, result["receipt"])
    assert undone["ok"] is True and undone["files_restored"] == 0


# ---- 撤销 ----

def test_undo_moves_everything_back(rig):
    make_files(rig["root"], {"a.jpg": "1", "b.txt": "2", "c.xyz": "3"})
    result = do_confirm(rig)
    undone = do_undo(rig, result["receipt"])
    assert undone["enabled"] is True and undone["ok"] is True
    assert undone["files_restored"] == 3 and undone["undo_of"] == result["receipt"]
    assert (rig["root"] / "a.jpg").read_bytes() == b"1"
    assert (rig["root"] / "b.txt").read_bytes() == b"2"
    assert (rig["root"] / "c.xyz").read_bytes() == b"3"
    assert not (rig["root"] / "images" / "a.jpg").exists()
    kinds = [item["kind"] for item in fs.load_receipts(home=rig["home"])]
    assert kinds == ["move", "undo"]


def test_undo_twice_is_idempotent(rig):
    make_files(rig["root"], {"a.jpg": "1", "b.txt": "2"})
    result = do_confirm(rig)
    first = do_undo(rig, result["receipt"])
    second = do_undo(rig, result["receipt"])
    assert first["ok"] is True and first["files_restored"] == 2
    assert second["ok"] is True and second["files_restored"] == 0
    assert second["already_undone"] is True
    # 第二次没有把文件再挪到别处, 也没有报错
    assert (rig["root"] / "a.jpg").read_bytes() == b"1"
    assert (rig["root"] / "b.txt").read_bytes() == b"2"
    assert len(fs.load_receipts(home=rig["home"])) == 3


def test_undo_skips_files_that_no_longer_exist(rig):
    make_files(rig["root"], {"a.jpg": "1", "b.txt": "2"})
    result = do_confirm(rig)
    (rig["root"] / "images" / "a.jpg").unlink()
    undone = do_undo(rig, result["receipt"])
    assert undone["ok"] is True and undone["files_restored"] == 1
    assert (rig["root"] / "b.txt").read_bytes() == b"2"
    assert any(Path(item["path"]).name == "a.jpg" for item in undone["skipped"])


def test_undo_unknown_receipt_reports_cleanly(rig):
    result = do_undo(rig, "fs-nope")
    assert result["ok"] is False and "没有找到" in result["reason"]


# ---- 开关 ----

def test_switch_off_makes_the_plugin_unavailable(rig, monkeypatch):
    make_files(rig["root"], {"a.jpg": "1"})
    monkeypatch.delenv(fs.SWITCH_ENV, raising=False)
    for result in (fs.plan(rig["root"]), fs.confirm_move(rig["root"]), fs.undo("fs-anything")):
        assert result["enabled"] is False and result["ok"] is False
        assert "WM_PLUGIN_HOST=on" in result["reason"]
    assert (rig["root"] / "a.jpg").is_file()
    assert not rig["home"].exists()


# ---- 清单 ----

def test_manifest_passes_the_contract_validator():
    manifest = json.loads((PLUGIN / "wmworld.json").read_text(encoding="utf-8"))
    assert pm.validate_plugin_manifest(manifest, PLUGIN) == []
    assert pm.policy_issues(manifest, "third_party") == []
    assert manifest["id"] == "filesorter"
    assert manifest["plugin"]["tier"] == "process"
    # 契约里动作只能挂在 world 下, 所以第二切片给文件工具补了一个最小的 space 档 world 块:
    # 只有动作表和契约要求的 perception/spectate 占位, 它没有真的世界视图和渠道。
    world = manifest["plugin"]["world"]
    assert world["kind"] == "space" and world["prefix"] == "filesorter"
    assert [action["name"] for action in world["actions"]] == [
        "filesorter_plan", "filesorter_move", "filesorter_undo"]
    assert [action["kind"] for action in world["actions"]] == ["query", "act", "act"]
